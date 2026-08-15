"""WebSocket audio source: call audio arriving from the phone machine.

The phone machine terminates the call (Asterisk hands it the media over AudioSocket) and
forwards each answered call's audio here. This module is the receiving half: it converts
PCM16 payloads to float32, runs them through the same `Segmenter` the mic path uses, and
pushes closed utterances onto the same queue -- so `consume_chunks` cannot tell a phone
call from a microphone. Finished transcripts go back out on the same socket.

## The link

One WebSocket connection per call, opened by the phone machine.

- **Binary messages are audio**: PCM16, little-endian, mono, 8kHz -- 20ms (320-byte)
  frames as they came off the line. Not resampled in transit: 8kHz is the analog line's
  native rate, and upsampling before the network would double the bytes without adding
  information. `resample_to_target` lifts each *closed utterance* to 16kHz for Whisper,
  once, off the realtime path.
- **Text messages are JSON control**, `{"type": ...}`:

  | direction | type | fields |
  |---|---|---|
  | phone -> here | `call_start` | `call_id`, `rate`, `direction` |
  | phone -> here | `call_end`   | `reason` |
  | here -> phone | `transcript` | `call`, `text`, `dur_ms`, `latency_ms`, `continues_previous` |
  | here -> phone | `error`      | `message` |

Binary in the here->phone direction is reserved for synthesized audio to play down the
line; nothing sends it yet, and the phone side already knows how to write it to Asterisk.

## Why this is so much smaller than the UDP receiver it replaces

The previous design (`docs/NETWORKING.md`, tag `archive/pyvoip`) carried the same audio
over UDP with a hand-written 16-byte header, and had to solve framing, sequence numbers,
loss and late-frame accounting, an idle timeout, and a heartbeat to distinguish "no call"
from "link down". A WebSocket supplies message framing, ordering, a binary/text
discriminator, and liveness (ping/pong plus TCP's own connection state), so none of that
code exists here. What survives is the part that was always about *speech* rather than
about sockets: a call can go quiet without ending, so `idle_timeout` still flushes an
utterance that hangup left open.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid
from queue import Queue
from typing import Any, Callable

import numpy as np

from .audio import HANGOVER_MS, TARGET_SAMPLE_RATE, Segmenter

# The rate the phone machine sends at: the analog line's own rate, unresampled.
WIRE_RATE = 8000

# 20ms at WIRE_RATE. One AudioSocket frame maps to exactly one of these, but audio is
# re-blocked rather than trusted to arrive that way, so the segmenter's timing math holds
# regardless of how the sender chunks it.
BLOCK_SAMPLES = 160

DEFAULT_PORT = 9099

# Starting point only -- expect to tune this per line with --meter. A phone line's noise
# floor sits well above a microphone's, but how far above depends on the ATA's gain.
DEFAULT_PHONE_ENERGY_THRESHOLD = 0.01

# A call with no audio for this long is treated as ended. TCP tells us when the phone
# machine goes away, so unlike the UDP version this is not about detecting a dead link --
# it is about a call that stalls mid-utterance without closing, whose last words would
# otherwise never be flushed.
DEFAULT_IDLE_TIMEOUT = 2.0

# Meter mode prints one aggregated line per this many blocks (10 * 20ms = 200ms).
_METER_BLOCKS = 10

# How long a finished call's socket is held open waiting for its last utterance to come back
# from the transcriber. Hangup flushes a partial utterance (`Segmenter.flush`), but inference
# runs on another thread and takes a second or more -- close the socket the moment the call
# ends and that final sentence, usually the one worth having, is transcribed into a socket
# nobody is holding. Must be shorter than the phone machine's own grace period
# (`session.FINAL_TRANSCRIPT_GRACE_SECONDS`) so this side is the one that closes.
FINAL_DRAIN_TIMEOUT = 5.0


def pcm16_to_float32(payload: bytes) -> np.ndarray:
    """Wire PCM16 bytes -> the float32 in [-1, 1] the backends and Segmenter expect."""
    return np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0


class _Call:
    """Per-call receive state: its segmenter, its block accumulator, its tallies."""

    def __init__(self, call_id: str, energy_threshold: float, hangover_ms: float) -> None:
        self.call_id = call_id
        self.segmenter = Segmenter(
            native_rate=WIRE_RATE,
            block_size=BLOCK_SAMPLES,
            energy_threshold=energy_threshold,
            hangover_ms=hangover_ms,
        )
        self.blocks = 0
        self.last_audio = time.monotonic()
        self._residue = b""

    def blocks_from(self, payload: bytes):
        """Yield fixed-size float32 blocks, carrying any partial block to the next call.

        Fixed blocks matter because `Segmenter` counts *blocks* of silence, not
        milliseconds: feed it uneven blocks and the hangover stops meaning 500ms.
        """
        self.last_audio = time.monotonic()
        data = self._residue + payload
        step = BLOCK_SAMPLES * 2
        whole = len(data) - (len(data) % step)
        self._residue = data[whole:]
        for start in range(0, whole, step):
            self.blocks += 1
            yield pcm16_to_float32(data[start : start + step])

    def stats(self) -> str:
        return f"blocks={self.blocks} ({self.blocks * BLOCK_SAMPLES / WIRE_RATE:.1f}s)"


class WebSocketSource:
    """Serves call audio from the phone machine into the dictation queue.

    Mirrors `audio.open_stream`'s contract -- a background producer pushing closed
    utterances onto a shared queue -- so `consume_chunks` works against either source
    unchanged. A `None` on the queue means this source is finished.

    Transcription happens on `consume_chunks`' thread, not here, so a two-second inference
    never stalls the socket: frames keep draining into the segmenter while the GPU works.
    """

    def __init__(
        self,
        out_queue: Queue,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        energy_threshold: float = DEFAULT_PHONE_ENERGY_THRESHOLD,
        hangover_ms: float = HANGOVER_MS,
        meter: bool = False,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        verbose: bool = False,
        final_drain_timeout: float = 0.0,
    ) -> None:
        self._queue = out_queue
        self._host = host
        self._port = port
        self._energy_threshold = energy_threshold
        self._hangover_ms = hangover_ms
        self._meter = meter
        self._idle_timeout = idle_timeout
        self._verbose = verbose
        # Off unless a transcriber is attached: with nothing draining the queue, waiting for
        # a transcript that can never arrive would just stall every hangup by the timeout.
        self._final_drain_timeout = final_drain_timeout

        self._server: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sockets: dict[str, Any] = {}  # call_id -> websocket, for the transcript path
        self._meter_window: list[float] = []
        # Chunks queued but not yet transcribed, per call. Written from the event loop
        # (queueing) and from `consume_chunks`' thread (completion), hence the lock.
        self._pending: dict[str, int] = {}
        self._drained: dict[str, asyncio.Event] = {}
        self._pending_lock = threading.Lock()

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self) -> "WebSocketSource":
        from websockets.asyncio.server import serve

        self._loop = asyncio.get_running_loop()
        self._server = await serve(self._handle, self._host, self._port)
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self._queue.put(None)

    async def __aenter__(self) -> "WebSocketSource":
        return await self.start()

    async def __aexit__(self, *exc_info) -> None:
        await self.stop()

    @property
    def bound_port(self) -> int:
        """The port actually bound -- meaningful when port 0 was requested (the tests do)."""
        if self._server is None:
            return self._port
        return next(iter(self._server.sockets)).getsockname()[1]

    # --- transcripts, back out to the phone machine ------------------------------------

    def emit(self, call_id: str | None, record: dict | None) -> None:
        """Send one finished transcript back. Called from `consume_chunks`' thread.

        Thread-affine on purpose: websockets objects belong to the event loop, so the send
        is scheduled onto it rather than performed here. Failures are logged and dropped --
        a call whose socket has already closed still transcribed fine, and there is nobody
        left to tell.

        `record` is None when the chunk transcribed to nothing (line noise that crossed the
        energy threshold). Nothing is sent, but it still counts as that chunk being finished
        with -- otherwise a call whose last utterance was noise would hold its socket open
        for the full drain timeout waiting for a transcript that is never coming.
        """
        try:
            websocket = self._sockets.get(call_id) if call_id is not None else None
            if record is None or websocket is None or self._loop is None:
                return
            message = json.dumps({"type": "transcript", **record})
            try:
                future = asyncio.run_coroutine_threadsafe(websocket.send(message), self._loop)
            except RuntimeError:
                return  # loop already closed; shutting down
            # Retrieve the result, or a failed send surfaces later as an "exception was
            # never retrieved" warning from the GC instead of being handled here.
            future.add_done_callback(self._log_send_failure)
        finally:
            self._chunk_done(call_id)

    def _log_send_failure(self, future) -> None:
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001 - the call hung up mid-send; nothing to do
            if self._verbose:
                print(f"[ws] transcript send failed: {exc!r}", file=sys.stderr)

    # --- tracking work in flight, so hangup can wait for it ----------------------------

    def _queue_chunk(self, call_id: str, chunk, final: bool = False) -> None:
        """Queue a closed utterance. `final` marks the one hangup flushed.

        The worker needs that distinction and cannot derive it: every call measured ended with
        the handset hitting its cradle, captured as a short loud chunk and transcribed as
        `Thank you.`. It is louder and longer than the shortest genuine one-word answer, so no
        acoustic rule separates them -- but it is the chunk hangup flushed, which is a fact only
        this side knows. Carrying it on the tuple keeps `consume_chunks` source-agnostic; the
        mic source sets it False and is otherwise unchanged.
        """
        with self._pending_lock:
            self._pending[call_id] = self._pending.get(call_id, 0) + 1
        self._queue.put((chunk, WIRE_RATE, time.perf_counter(), call_id, final))

    def _chunk_done(self, call_id: str | None) -> None:
        """One queued chunk has been transcribed. Runs on `consume_chunks`' thread."""
        if call_id is None:
            return
        with self._pending_lock:
            remaining = self._pending.get(call_id, 0) - 1
            if remaining > 0:
                self._pending[call_id] = remaining
                return
            self._pending.pop(call_id, None)
            event = self._drained.get(call_id)
        if event is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass  # loop already closed; shutting down

    async def _wait_for_pending(self, call_id: str) -> None:
        """Hold the connection open until this call's queued chunks have been transcribed."""
        if self._final_drain_timeout <= 0:
            return
        with self._pending_lock:
            if not self._pending.get(call_id):
                return
            event = self._drained.setdefault(call_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), self._final_drain_timeout)
        except asyncio.TimeoutError:
            if self._verbose:
                print(
                    f"[ws] call {call_id}: gave up waiting for the last transcript after "
                    f"{self._final_drain_timeout:.0f}s",
                    file=sys.stderr,
                )
        finally:
            with self._pending_lock:
                self._drained.pop(call_id, None)
                self._pending.pop(call_id, None)

    # --- receiving ---------------------------------------------------------------------

    async def _handle(self, websocket) -> None:
        """One connection: one call, from the phone machine's call_start to hangup."""
        call: _Call | None = None
        reason = "socket closed"
        idle_task = None
        try:
            async for message in websocket:
                if isinstance(message, str):
                    control = self._parse_control(message)
                    kind = control.get("type")
                    if kind == "call_start":
                        # Cancelling first matters if a client sends call_start twice on one
                        # connection: two watchers on one call double-flush its utterances.
                        if idle_task is not None:
                            idle_task.cancel()
                        call = self._start_call(control)
                        self._sockets[call.call_id] = websocket
                        idle_task = asyncio.create_task(self._watch_idle(lambda: call))
                    elif kind == "call_end":
                        reason = str(control.get("reason", "hangup"))
                        break
                    continue

                if call is None:
                    # Audio before call_start: the phone machine restarted mid-call, or is
                    # a test client that doesn't bother. The audio is real -- adopt it.
                    call = self._start_call({"call_id": f"anon-{uuid.uuid4().hex[:6]}"})
                    self._sockets[call.call_id] = websocket
                    idle_task = asyncio.create_task(self._watch_idle(lambda: call))
                    print(f"[ws] call {call.call_id} started (mid-stream)", file=sys.stderr)

                for block in call.blocks_from(message):
                    self._on_block(call, block)
        except Exception as exc:  # noqa: BLE001 - one bad call must not stop the service
            reason = f"error: {exc}"
            if self._verbose:
                print(f"[ws] connection failed: {exc!r}", file=sys.stderr)
        finally:
            if idle_task is not None:
                idle_task.cancel()
            if call is not None:
                # Order matters: flush the tail utterance, then wait for it to come back
                # from the transcriber, and only then drop the socket. Unregistering first
                # means `emit` has nowhere to send the last thing the caller said.
                self._end_call(call, reason)
                try:
                    await self._wait_for_pending(call.call_id)
                finally:
                    self._sockets.pop(call.call_id, None)

    def _parse_control(self, message: str) -> dict:
        try:
            parsed = json.loads(message)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            if self._verbose:
                print(f"[ws] ignoring non-JSON text message: {message[:80]!r}", file=sys.stderr)
            return {}

    def _start_call(self, control: dict) -> _Call:
        # Random, not time-derived: two unannounced calls starting in the same second would
        # otherwise share an id, and `_sockets` would route one call's transcripts to the
        # other -- or drop them when the first to end unregisters the shared key.
        call_id = str(control.get("call_id") or f"call-{uuid.uuid4().hex[:6]}")
        rate = control.get("rate", WIRE_RATE)
        if rate != WIRE_RATE:
            # Everything downstream assumes the wire rate; a sender at another rate would
            # transcribe as gibberish at the wrong speed, which is worth more than silence.
            print(
                f"[ws] WARNING call {call_id} announced {rate}Hz, expected {WIRE_RATE}Hz "
                "-- audio will be misinterpreted; fix the sender",
                file=sys.stderr,
            )
        print(f"[ws] call {call_id} started", file=sys.stderr)
        return _Call(call_id, self._energy_threshold, self._hangover_ms)

    def _on_block(self, call: _Call, block: np.ndarray) -> None:
        if self._meter:
            self._meter_block(block)
            return
        chunk = call.segmenter.process(block)
        if chunk is not None:
            self._queue_chunk(call.call_id, chunk)

    def _meter_block(self, block: np.ndarray) -> None:
        self._meter_window.append(float(np.sqrt(np.mean(np.square(block)))) if len(block) else 0.0)
        if len(self._meter_window) < _METER_BLOCKS:
            return
        window = self._meter_window
        loud = sum(1 for r in window if r >= self._energy_threshold)
        print(
            f"[meter] rms min={min(window):.4f} mean={sum(window) / len(window):.4f} "
            f"max={max(window):.4f}  over-threshold {loud}/{len(window)} "
            f"(threshold={self._energy_threshold})",
            file=sys.stderr,
        )
        self._meter_window = []

    async def _watch_idle(self, get_call: Callable[[], _Call | None]) -> None:
        """Flush an utterance left open by a call that stalled without closing."""
        if self._idle_timeout <= 0:
            return
        while True:
            await asyncio.sleep(self._idle_timeout / 2)
            call = get_call()
            if call is None:
                return
            if time.monotonic() - call.last_audio >= self._idle_timeout:
                tail = call.segmenter.flush()
                if tail is not None and not self._meter:
                    # Not `final`: the call has stalled, not hung up. Whatever is in the buffer
                    # is speech the caller is still in the middle of, not a cradle click.
                    self._queue_chunk(call.call_id, tail)
                call.last_audio = time.monotonic()  # don't re-flush every tick

    def _end_call(self, call: _Call, reason: str) -> None:
        """Close the call, flushing an utterance left open by hangup.

        The flushed tail is queued like any other chunk, which is what lets `_handle` wait
        for its transcript before the socket goes away.
        """
        self._meter_window = []
        tail = call.segmenter.flush()
        if tail is not None and not self._meter:
            self._queue_chunk(call.call_id, tail, final=True)
        print(f"[ws] call {call.call_id} ended ({reason}) {call.stats()}", file=sys.stderr)
