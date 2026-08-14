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
        self.frames_sent = 0
        self.transcripts = 0

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def connect(self) -> bool:
        from websockets.asyncio.client import connect

        try:
            self._ws = await asyncio.wait_for(connect(self._url), CONNECT_TIMEOUT_SECONDS)
        except (OSError, asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
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
        return True

    async def send_audio(self, pcm: bytes) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(pcm)
            self.frames_sent += 1
        except Exception as exc:  # noqa: BLE001 - the link died mid-call; the call has not
            if self._verbose:
                print(f"[session] call {self._call_id}: stt link lost ({exc})", file=sys.stderr)
            self._ws = None

    async def _send_text(self, payload: dict) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(payload))
        except Exception:  # noqa: BLE001
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

    async def close(self, reason: str = "hangup") -> None:
        if self._ws is None:
            return
        await self._send_text({"type": "call_end", "reason": reason})
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
        await link.close(reason)
        # The stt machine may still be transcribing the last utterance when the caller
        # hangs up -- that final sentence is usually the one worth having, so give the
        # already-closing socket a moment to deliver it rather than cancelling outright.
        try:
            await asyncio.wait_for(reader, timeout=3)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            reader.cancel()
        print(
            f"[session] call {call_id} ended ({reason}) "
            f"frames={call.frames_in} sent={link.frames_sent} transcripts={link.transcripts}",
            file=sys.stderr,
        )
