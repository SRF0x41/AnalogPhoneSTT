"""Asterisk AudioSocket, server side. Stdlib only.

Asterisk's `AudioSocket()` dialplan application connects *out* to a TCP server and
streams the channel's audio over it, in both directions, for the life of the call. So
this module listens, and each accepted connection is one call.

That inverts the usual arrangement -- we are the server but Asterisk is the one placing
and ending calls -- and it is the whole reason this package needs no SIP stack. Asterisk
terminates SIP and RTP, negotiates codecs, and absorbs jitter; what arrives here is
already decoded, already de-jittered, 20ms at a time.

Wire format (`/usr/include/asterisk/res_audiosocket.h`, Asterisk 23.4.1)::

    [kind: 1 byte][length: 2 bytes, big-endian][payload: `length` bytes]

`length` is big-endian (Asterisk packs it with `htons`), but the *audio payload* is
signed-linear PCM in host byte order, which on x86 means little-endian -- Asterisk
memcpys the frame straight out without swapping (`res_audiosocket.c:255`). Both machines
in this project are little-endian, so the payload is passed through untouched; see
`SLIN_BYTEORDER` if that ever stops being true.

The dialplan application is fixed at **slin, 8kHz, mono** (`res_audiosocket.c:229`), so a
20ms frame is 160 samples / 320 bytes. That is the analog line's native rate, so nothing
is gained by resampling it here -- see `docs/NETWORKING.md` and the `stt_port` package,
which upsamples once per utterance instead.
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from typing import AsyncIterator, Awaitable, Callable

# --- frame kinds ---------------------------------------------------------------------
# Values from `enum ast_audiosocket_msg_kind`. Only the ones this side can meet are named;
# KIND_AUDIO_SLIN12..SLIN192 (0x11-0x18) only occur via chan_audiosocket's codec
# negotiation, which the dialplan application never uses.

KIND_HANGUP = 0x00  # we send this to ask Asterisk to hang up; it sends it when the call ends
KIND_UUID = 0x01    # first frame Asterisk sends: the 16-byte call id from ${UUID()}
KIND_DTMF = 0x03    # one ASCII digit the caller pressed
KIND_AUDIO = 0x10   # slin, 8kHz, mono
KIND_ERROR = 0xFF   # one-byte Asterisk-side error code

HEADER_FORMAT = "!BH"
HEADER_BYTES = struct.calcsize(HEADER_FORMAT)  # 3

RATE = 8000          # slin, fixed by the dialplan application
FRAME_SAMPLES = 160  # 20ms
FRAME_BYTES = FRAME_SAMPLES * 2
SLIN_BYTEORDER = "little"  # host order on x86; Asterisk does not swap

# Asterisk sends a frame every 20ms. Silence still arrives as frames, so a real stall in
# the audio means the far side is gone -- there is no "quiet call" that looks like this.
READ_TIMEOUT_SECONDS = 5.0

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9092


class ProtocolError(Exception):
    """The peer sent something that isn't AudioSocket, or Asterisk reported an error."""


def pack(kind: int, payload: bytes = b"") -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError(f"payload too long for a 16-bit length: {len(payload)}")
    return struct.pack(HEADER_FORMAT, kind, len(payload)) + payload


def unpack_header(header: bytes) -> tuple[int, int]:
    """(kind, payload_length) from exactly `HEADER_BYTES` bytes."""
    if len(header) != HEADER_BYTES:
        raise ProtocolError(f"header must be {HEADER_BYTES} bytes, got {len(header)}")
    return struct.unpack(HEADER_FORMAT, header)


class Dtmf(str):
    """A digit the caller pressed, yielded inline by `Call.recv()`.

    A `str` subclass so it prints and compares as the digit itself, but is still
    distinguishable from an audio frame by type rather than by length -- callers that only
    want audio can `isinstance`-check instead of guessing.
    """

    __slots__ = ()


class Call:
    """One AudioSocket connection: one call, from answer to hangup.

    `recv()` yields 20ms `bytes` frames of 8kHz PCM16 and `Dtmf` digits as they arrive, and
    returns `None` once the call has ended. `send()` writes audio back down the line. Both
    are safe to use from separate tasks -- the reader and writer are independent.

    Usage::

        async with Call(reader, writer) as call:
            while (frame := await call.recv()) is not None:
                if isinstance(frame, Dtmf):
                    ...
                else:
                    await call.send(frame)   # echo
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self.call_id: str | None = None  # set from the UUID frame Asterisk sends first
        self.frames_in = 0
        self.frames_out = 0
        self._ended = False
        self._pushback: bytes | Dtmf | None = None  # see ready()

    @property
    def label(self) -> str:
        """Short, stable tag for log lines. The first block of the UUID is plenty."""
        return self.call_id.split("-")[0] if self.call_id else "????????"

    async def __aenter__(self) -> "Call":
        return self

    async def ready(self) -> "Call":
        """Wait until `call_id` is known, so handlers can log and label from the start.

        Asterisk sends the UUID frame before any audio, but "before any audio" is only
        true of the dialplan application -- rather than trust that, this reads until
        either the id arrives or real content does, and pushes that content back so
        nothing is lost if the id never comes.
        """
        if self.call_id is not None or self._ended:
            return self
        # Deliberately stops at the UUID frame rather than reading on to the first audio
        # frame: on a call that is answered but momentarily silent, waiting for content
        # here would stall until READ_TIMEOUT_SECONDS gave up and ended the call.
        item = await self._recv(stop_after_uuid=True)
        if item is not None:
            self._pushback = item  # already counted; recv() returns it without recounting
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def recv(self) -> bytes | Dtmf | None:
        """Next audio frame or DTMF digit; None once the call has ended.

        The UUID frame is consumed here rather than surfaced, since it is call metadata
        rather than call content -- it lands on `self.call_id` before the first frame is
        returned.
        """
        return await self._recv()

    async def _read_frame(self) -> tuple[int, bytes] | None:
        """One (kind, payload) off the wire, or None once the call is over."""
        try:
            header = await asyncio.wait_for(
                self._reader.readexactly(HEADER_BYTES), READ_TIMEOUT_SECONDS
            )
        except asyncio.IncompleteReadError:
            # Asterisk closed the socket without a hangup frame -- a crash, or the channel
            # going away underneath it. Same outcome for us as a clean hangup.
            self._ended = True
            return None
        except (asyncio.TimeoutError, ConnectionError):
            # Asterisk sends a frame every 20ms for the life of the call, and silence on
            # the line still arrives as frames of quiet samples. Nothing at all for this
            # long means the far end is gone.
            self._ended = True
            return None

        kind, length = unpack_header(header)
        try:
            payload = await self._reader.readexactly(length) if length else b""
        except asyncio.IncompleteReadError:
            self._ended = True
            return None
        return kind, payload

    async def _recv(self, stop_after_uuid: bool = False) -> bytes | Dtmf | None:
        if self._pushback is not None:
            item, self._pushback = self._pushback, None
            return item
        while not self._ended:
            frame = await self._read_frame()
            if frame is None:
                return None
            kind, payload = frame

            if kind == KIND_AUDIO:
                self.frames_in += 1
                return payload
            if kind == KIND_HANGUP:
                self._ended = True
                return None
            if kind == KIND_UUID:
                # 16 raw bytes, not a formatted string.
                self.call_id = str(uuid.UUID(bytes=payload)) if len(payload) == 16 else None
                if stop_after_uuid:
                    return None
                continue
            if kind == KIND_DTMF:
                return Dtmf(payload.decode("ascii", "replace"))
            if kind == KIND_ERROR:
                code = payload[0] if payload else 0
                self._ended = True
                raise ProtocolError(f"Asterisk reported AudioSocket error 0x{code:02x}")
            # Unknown kind: skip it. The payload length told us how, and refusing to parse
            # a frame we don't recognize would be a worse failure than ignoring it.
        return None

    async def send(self, pcm: bytes) -> None:
        """Play PCM16 (8kHz, mono, little-endian) down the line.

        Asterisk paces playback itself, one frame per 20ms, so this returns as soon as the
        bytes are handed to the socket. Oversized buffers are split into whole frames;
        splitting mid-sample would turn the rest into noise, so the split is even.
        """
        if self._ended:
            return
        for start in range(0, len(pcm), FRAME_BYTES):
            self._writer.write(pack(KIND_AUDIO, pcm[start : start + FRAME_BYTES]))
            self.frames_out += 1
        await self._drain()

    async def hangup(self) -> None:
        """Ask Asterisk to end the call."""
        if self._ended:
            return
        self._ended = True
        self._writer.write(pack(KIND_HANGUP))
        await self._drain()

    async def _drain(self) -> None:
        try:
            await self._writer.drain()
        except (ConnectionError, RuntimeError):
            # The call ended while we were mid-write. Nothing to recover, and nothing the
            # caller can usefully do about it either.
            self._ended = True

    async def close(self) -> None:
        self._ended = True
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except (ConnectionError, RuntimeError):
            pass

    async def __aiter__(self) -> AsyncIterator[bytes | Dtmf]:
        while (item := await self.recv()) is not None:
            yield item


async def serve(
    handler: Callable[[Call], Awaitable[None]],
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> asyncio.Server:
    """Listen for AudioSocket connections, running `handler` once per call.

    Returns the started server so the caller decides how long to run
    (`async with server: await server.serve_forever()`). Each connection is handled in its
    own task by asyncio, so overlapping calls do not block each other -- which matters
    because a handler that reaches across the network to the stt machine is not fast.
    """

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with Call(reader, writer) as call:
            await handler(await call.ready())

    return await asyncio.start_server(on_connect, host, port)
