"""Bridge one call between Asterisk and the speech-to-text machine.

Asterisk hands us a call over AudioSocket; this opens a WebSocket to the stt machine, pumps
the caller's audio across it as binary messages, and hands each transcript that comes back
to a local sink. That sink is where this box finally has the text -- printing it is the
default, but it is the seam anything else plugs into.

The two directions are independent tasks over one connection rather than a request/response
pair: audio flows continuously while transcripts arrive whenever an utterance closes,
typically a second or two behind the speech that produced it.

    phone machine                         stt machine
      AudioSocket  --- binary audio --->  segment, transcribe
                   <--- JSON text -----   transcript

## The stt machine not being there is not an error

A refused or dropped WebSocket must never take the call down with it. Someone on the
handset is mid-sentence; the correct response to "the transcriber is down" is a call that
carries on untranscribed, not a dead line. So the connection is attempted once per call, its
failure is logged, and the audio pump keeps draining the AudioSocket either way -- draining
matters even with nowhere to send, because a socket nobody reads is a socket that backs up
into Asterisk.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any, Awaitable, Callable

from . import audiosocket, config

# How long to wait for the stt machine before giving up and running the call untranscribed.
# Short on purpose: this is a LAN, and the caller is already talking.
CONNECT_TIMEOUT_SECONDS = 2.0

# After hangup, how long to keep the socket open for the last utterance's transcript. The
# stt machine flushes the partial utterance hangup left open, transcribes it, sends it, and
# then closes -- so this is a backstop, not the normal path, and must be longer than that
# machine's own `server.FINAL_DRAIN_TIMEOUT` or we would hang up on it mid-answer.
FINAL_TRANSCRIPT_GRACE_SECONDS = 6.0

# Audio waiting to go to the stt machine, in 20ms frames. One second is far more slack than
# a LAN needs; the point is the bound, not the depth -- see `send_audio`.
OUTBOUND_QUEUE_FRAMES = 50

# How long to let queued audio finish sending before announcing the call is over, so the
# last frames of speech don't lose the race with `call_end`.
FLUSH_TIMEOUT_SECONDS = 2.0

# Ceiling on an inline control-message send. See `_send_text`.
TEXT_SEND_TIMEOUT_SECONDS = 2.0

TranscriptSink = Callable[[dict], Any]


def print_transcript(record: dict) -> None:
    """Default sink: one line per utterance on stdout."""
    print(f"[{time.strftime('%H:%M:%S')}] {record.get('text', '')}", flush=True)


def jsonl_transcript(record: dict) -> None:
    """Sink for feeding something downstream rather than a human."""
    print(json.dumps(record, ensure_ascii=False), flush=True)


class SttLink:
    """The WebSocket to the stt machine, for the duration of one call.

    Absent-by-default: if `connect()` fails, every other method becomes a no-op and the
    caller does not have to branch on it.
    """

    def __init__(self, url: str, call_id: str, verbose: bool = False) -> None:
        self._url = url
        self._call_id = call_id
        self._verbose = verbose
        self._ws: Any = None
        self._outbox: asyncio.Queue | None = None
        self._pump: asyncio.Task | None = None
        self.frames_sent = 0
        self.frames_dropped = 0
        self.transcripts = 0

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def connect(self) -> bool:
        from websockets.asyncio.client import connect

        try:
            self._ws = await asyncio.wait_for(connect(self._url), CONNECT_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - refused, timed out, bad URL: all the same
            print(
                f"[session] call {self._call_id}: no stt at {self._url} ({exc}) -- "
                "continuing untranscribed",
                file=sys.stderr,
            )
            self._ws = None
            return False
        await self._send_text({
            "type": "call_start",
            "call_id": self._call_id,
            "rate": audiosocket.RATE,
            "direction": "inbound",
        })
        self._start_pump()
        return True

    def _start_pump(self) -> None:
        self._outbox = asyncio.Queue(maxsize=OUTBOUND_QUEUE_FRAMES)
        self._pump = asyncio.create_task(self._drain_outbox())

    async def send_audio(self, pcm: bytes) -> None:
        """Hand one frame to the stt machine, dropping it rather than waiting for room.

        Sending straight down the socket would make the caller's audio path only as fast as
        the slowest thing on the far end: a stt machine that stops reading while its
        connection stays open would block this call's read loop, and an AudioSocket nobody
        reads backs up into Asterisk -- the exact failure this module exists to avoid. So
        frames go through a bounded queue, and when it fills the frame is dropped.

        Dropping is the right loss: a 20ms hole is spliced out and transcribes through
        (`docs/NETWORKING.md` §5), while stalling the read loop degrades the live call.
        """
        if self._ws is None or self._outbox is None:
            return
        try:
            self._outbox.put_nowait(pcm)
        except asyncio.QueueFull:
            self.frames_dropped += 1

    async def _drain_outbox(self) -> None:
        """Own the socket's write side, so a slow send can never reach the read loop."""
        assert self._outbox is not None
        while True:
            pcm = await self._outbox.get()
            try:
                if self._ws is not None:
                    await self._ws.send(pcm)
                    self.frames_sent += 1
            except Exception as exc:  # noqa: BLE001 - the link died mid-call; the call has not
                if self._verbose:
                    print(f"[session] call {self._call_id}: stt link lost ({exc})", file=sys.stderr)
                self._ws = None
            finally:
                self._outbox.task_done()

    async def _send_text(self, payload: dict) -> None:
        """Send one control message, bounded.

        Bounded because audio has a queue to be dropped into but control messages are sent
        inline: a far side that has wedged with its connection still open would otherwise
        make `call_start` stall the call's first frames, and `call_end` stall its teardown.
        """
        if self._ws is None:
            return
        try:
            await asyncio.wait_for(self._ws.send(json.dumps(payload)), TEXT_SEND_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - timed out or the socket is gone; same outcome
            self._ws = None

    async def receive_transcripts(self, sink: TranscriptSink) -> None:
        """Feed the sink until the stt machine stops sending. Ends when the socket closes."""
        if self._ws is None:
            return
        try:
            async for message in self._ws:
                if not isinstance(message, str):
                    # Reserved for synthesized speech to play down the line. Nothing sends
                    # it yet; ignoring it keeps this forward-compatible rather than fatal.
                    continue
                try:
                    record = json.loads(message)
                except ValueError:
                    continue
                if record.get("type") == "transcript":
                    self.transcripts += 1
                    sink(record)
                elif record.get("type") == "error" and self._verbose:
                    print(f"[session] stt error: {record.get('message')}", file=sys.stderr)
        except Exception:  # noqa: BLE001 - a closed socket is the normal way out
            pass

    async def finish(self, reason: str = "hangup") -> None:
        """Announce the call is over, but leave the socket open.

        Split from `close` on purpose. The stt machine answers `call_end` by flushing the
        utterance hangup interrupted, transcribing it, sending it back, and only then
        closing -- so closing here would cut off the final sentence, which is usually the
        one worth having. `handle_call` waits for the far side to close instead.
        """
        if self._pump is not None:
            # Queued audio first: `call_end` overtaking the last frames of speech would
            # truncate the very utterance this whole handshake exists to preserve.
            if self._outbox is not None and self._ws is not None:
                try:
                    await asyncio.wait_for(self._outbox.join(), FLUSH_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    pass
            self._pump.cancel()
            self._pump = None
            self._outbox = None  # any late frame is now a cheap no-op, not a silent queue
        await self._send_text({"type": "call_end", "reason": reason})

    async def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        if self._ws is None:
            return
        ws, self._ws = self._ws, None
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


async def handle_call(
    call: audiosocket.Call,
    stt_url: str = config.STT_URL,
    sink: TranscriptSink = print_transcript,
    verbose: bool = False,
    on_dtmf: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Run one call end to end. Suitable as the handler passed to `audiosocket.serve`."""
    call_id = call.label
    print(f"[session] call {call_id} connected", file=sys.stderr)

    link = SttLink(stt_url, call_id, verbose=verbose)
    await link.connect()
    # Started even when the link is absent: it returns immediately, and keeping the shape
    # the same means the teardown path below has no special case.
    reader = asyncio.create_task(link.receive_transcripts(sink))

    reason = "hangup"
    try:
        while (item := await call.recv()) is not None:
            if isinstance(item, audiosocket.Dtmf):
                print(f"[session] call {call_id} dtmf {item}", file=sys.stderr)
                if on_dtmf is not None:
                    await on_dtmf(str(item))
                continue
            await link.send_audio(item)
    except audiosocket.ProtocolError as exc:
        reason = str(exc)
        print(f"[session] call {call_id}: {exc}", file=sys.stderr)
    finally:
        # Announce the hangup but keep the socket open: the stt machine still owes us the
        # transcript of the utterance the hangup interrupted, and it closes the connection
        # itself once that has been sent -- which is what ends `reader` below.
        await link.finish(reason)
        try:
            await asyncio.wait_for(reader, timeout=FINAL_TRANSCRIPT_GRACE_SECONDS)
        except asyncio.TimeoutError:
            pass  # wait_for has already cancelled it
        except asyncio.CancelledError:
            # Ours to propagate, not to swallow -- catching it here would make this call
            # uncancellable during shutdown.
            reader.cancel()
            raise
        finally:
            await link.close()
            dropped = f" dropped={link.frames_dropped}" if link.frames_dropped else ""
            print(
                f"[session] call {call_id} ended ({reason}) frames={call.frames_in} "
                f"sent={link.frames_sent}{dropped} transcripts={link.transcripts}",
                file=sys.stderr,
            )
