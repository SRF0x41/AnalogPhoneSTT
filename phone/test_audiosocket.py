"""Offline tests for the AudioSocket framing and call lifecycle.

No Asterisk, no phone, no network beyond a loopback socket pair. The constants asserted
here are copied from `/usr/include/asterisk/res_audiosocket.h` on purpose: this file is the
only thing standing between a typo'd frame kind and audio that silently never arrives.

    python -m unittest phone.test_audiosocket -v
"""

from __future__ import annotations

import asyncio
import struct
import unittest
import uuid

from . import audiosocket as a


def frame(kind: int, payload: bytes = b"") -> bytes:
    """Build a frame the way Asterisk does, independently of pack()."""
    return bytes([kind]) + len(payload).to_bytes(2, "big") + payload


class TestFrameKinds(unittest.TestCase):
    """Against enum ast_audiosocket_msg_kind in res_audiosocket.h."""

    def test_kind_values(self):
        self.assertEqual(a.KIND_HANGUP, 0x00)
        self.assertEqual(a.KIND_UUID, 0x01)
        self.assertEqual(a.KIND_DTMF, 0x03)
        self.assertEqual(a.KIND_AUDIO, 0x10)
        self.assertEqual(a.KIND_ERROR, 0xFF)

    def test_header_is_three_bytes(self):
        self.assertEqual(a.HEADER_BYTES, 3)

    def test_frame_geometry(self):
        # 20ms of 8kHz mono PCM16, plus the header.
        self.assertEqual(a.FRAME_BYTES, 320)
        self.assertEqual(len(a.pack(a.KIND_AUDIO, b"\x00" * a.FRAME_BYTES)), 323)


class TestPacking(unittest.TestCase):
    def test_length_is_big_endian(self):
        # Asterisk packs the length with htons; get this backwards and every frame is
        # either truncated or waits forever for bytes that never come.
        packed = a.pack(a.KIND_AUDIO, b"\x00" * 320)
        self.assertEqual(packed[1:3], b"\x01\x40")
        self.assertEqual(struct.unpack("!H", packed[1:3])[0], 320)

    def test_round_trip(self):
        payload = bytes(range(256))
        packed = a.pack(a.KIND_AUDIO, payload)
        kind, length = a.unpack_header(packed[: a.HEADER_BYTES])
        self.assertEqual(kind, a.KIND_AUDIO)
        self.assertEqual(length, len(payload))
        self.assertEqual(packed[a.HEADER_BYTES :], payload)

    def test_empty_payload(self):
        self.assertEqual(a.pack(a.KIND_HANGUP), b"\x00\x00\x00")

    def test_payload_too_long(self):
        with self.assertRaises(ValueError):
            a.pack(a.KIND_AUDIO, b"\x00" * 0x10000)

    def test_short_header_rejected(self):
        with self.assertRaises(a.ProtocolError):
            a.unpack_header(b"\x10\x01")


class CallTestCase(unittest.IsolatedAsyncioTestCase):
    """Drives a real `Call` over a loopback socket, standing in for Asterisk."""

    async def asyncSetUp(self):
        self._server = await asyncio.start_server(self._on_connect, "127.0.0.1", 0)
        self._connected = asyncio.Event()
        self.port = self._server.sockets[0].getsockname()[1]
        # The test acts as Asterisk (the client); `self.call` is our server side.
        self.peer_r, self.peer_w = await asyncio.open_connection("127.0.0.1", self.port)
        await self._connected.wait()

    async def _on_connect(self, reader, writer):
        self.call = a.Call(reader, writer)
        self._connected.set()

    async def asyncTearDown(self):
        # Close our side too: since 3.12, Server.wait_closed() blocks until every
        # accepted connection is gone, so leaving `call` open hangs the teardown.
        await self.call.close()
        self.peer_w.close()
        self._server.close()
        await self._server.wait_closed()

    async def send_as_asterisk(self, data: bytes):
        self.peer_w.write(data)
        await self.peer_w.drain()


class TestReceive(CallTestCase):
    async def test_uuid_frame_sets_call_id_and_is_not_yielded(self):
        call_uuid = uuid.uuid4()
        await self.send_as_asterisk(frame(a.KIND_UUID, call_uuid.bytes))
        await self.send_as_asterisk(frame(a.KIND_AUDIO, b"\xff" * 320))

        got = await self.call.recv()

        self.assertEqual(got, b"\xff" * 320)  # the UUID frame did not surface as audio
        self.assertEqual(self.call.call_id, str(call_uuid))
        self.assertEqual(self.call.label, str(call_uuid).split("-")[0])

    async def test_hangup_ends_the_stream(self):
        await self.send_as_asterisk(frame(a.KIND_AUDIO, b"\x01\x02" * 160))
        await self.send_as_asterisk(frame(a.KIND_HANGUP))

        self.assertEqual(len(await self.call.recv()), 320)
        self.assertIsNone(await self.call.recv())
        self.assertIsNone(await self.call.recv())  # stays ended

    async def test_dtmf_is_distinguishable_from_audio(self):
        await self.send_as_asterisk(frame(a.KIND_DTMF, b"7"))

        got = await self.call.recv()

        self.assertIsInstance(got, a.Dtmf)
        self.assertEqual(got, "7")

    async def test_closed_socket_ends_the_stream(self):
        # Asterisk crashing mid-call looks like this: no hangup frame, just EOF.
        await self.send_as_asterisk(frame(a.KIND_AUDIO, b"\x00" * 320))
        self.peer_w.close()

        self.assertEqual(len(await self.call.recv()), 320)
        self.assertIsNone(await self.call.recv())

    async def test_error_frame_raises(self):
        await self.send_as_asterisk(frame(a.KIND_ERROR, b"\x02"))

        with self.assertRaises(a.ProtocolError):
            await self.call.recv()

    async def test_unknown_kind_is_skipped_not_fatal(self):
        # A kind we don't handle (e.g. slin16 audio via chan_audiosocket) must not
        # desynchronize the stream -- the length field tells us how to step over it.
        await self.send_as_asterisk(frame(0x77, b"whatever"))
        await self.send_as_asterisk(frame(a.KIND_AUDIO, b"\xab" * 320))

        self.assertEqual(await self.call.recv(), b"\xab" * 320)

    async def test_ready_populates_call_id_before_any_audio(self):
        call_uuid = uuid.uuid4()
        await self.send_as_asterisk(frame(a.KIND_UUID, call_uuid.bytes))
        await self.send_as_asterisk(frame(a.KIND_AUDIO, b"\x33" * 320))

        await self.call.ready()

        self.assertEqual(self.call.call_id, str(call_uuid))
        # ready() had to read an audio frame to get there; it must not swallow it.
        self.assertEqual(await self.call.recv(), b"\x33" * 320)
        self.assertEqual(self.call.frames_in, 1)  # counted once, not twice

    async def test_ready_pushes_back_when_no_uuid_ever_arrives(self):
        await self.send_as_asterisk(frame(a.KIND_AUDIO, b"\x44" * 320))

        await self.call.ready()

        self.assertIsNone(self.call.call_id)
        self.assertEqual(await self.call.recv(), b"\x44" * 320)
        self.assertEqual(self.call.frames_in, 1)

    async def test_ready_on_an_already_ended_call(self):
        await self.send_as_asterisk(frame(a.KIND_HANGUP))
        await self.call.recv()

        await self.call.ready()  # must not block waiting for an id that can't come

        self.assertIsNone(await self.call.recv())

    async def test_frames_in_counts_only_audio(self):
        await self.send_as_asterisk(frame(a.KIND_UUID, uuid.uuid4().bytes))
        await self.send_as_asterisk(frame(a.KIND_AUDIO, b"\x00" * 320))
        await self.send_as_asterisk(frame(a.KIND_DTMF, b"1"))
        await self.send_as_asterisk(frame(a.KIND_AUDIO, b"\x00" * 320))
        for _ in range(3):
            await self.call.recv()

        self.assertEqual(self.call.frames_in, 2)


class TestSend(CallTestCase):
    async def test_send_frames_audio(self):
        await self.call.send(b"\x11\x22" * 160)

        header = await self.peer_r.readexactly(3)
        kind, length = a.unpack_header(header)
        self.assertEqual(kind, a.KIND_AUDIO)
        self.assertEqual(length, 320)
        self.assertEqual(await self.peer_r.readexactly(320), b"\x11\x22" * 160)

    async def test_oversized_buffer_splits_into_whole_frames(self):
        # Splitting mid-sample would turn the remainder into noise, so the split must
        # land on an even byte offset -- which FRAME_BYTES guarantees.
        await self.call.send(b"\x00\x01" * 400)  # 800 bytes = 2.5 frames

        sizes = []
        for _ in range(3):
            kind, length = a.unpack_header(await self.peer_r.readexactly(3))
            self.assertEqual(kind, a.KIND_AUDIO)
            await self.peer_r.readexactly(length)
            sizes.append(length)

        self.assertEqual(sizes, [320, 320, 160])
        self.assertTrue(all(n % 2 == 0 for n in sizes))
        self.assertEqual(self.call.frames_out, 3)

    async def test_hangup_sends_hangup_frame(self):
        await self.call.hangup()

        self.assertEqual(await self.peer_r.readexactly(3), b"\x00\x00\x00")

    async def test_send_after_end_is_a_no_op(self):
        await self.call.hangup()
        await self.call.send(b"\x00" * 320)  # must not raise

        self.assertEqual(await self.peer_r.readexactly(3), b"\x00\x00\x00")
        self.assertEqual(self.call.frames_out, 0)


class TestServe(unittest.IsolatedAsyncioTestCase):
    async def test_handler_runs_per_connection_and_echoes(self):
        """The M1 proof, minus Asterisk: connect, send audio, get the same audio back."""
        done = asyncio.Event()

        async def echo(call: a.Call) -> None:
            while (item := await call.recv()) is not None:
                if not isinstance(item, a.Dtmf):
                    await call.send(item)
            done.set()

        server = await a.serve(echo, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(frame(a.KIND_UUID, uuid.uuid4().bytes))
            writer.write(frame(a.KIND_AUDIO, b"\x7f\x00" * 160))
            await writer.drain()

            kind, length = a.unpack_header(await reader.readexactly(3))
            self.assertEqual(kind, a.KIND_AUDIO)
            self.assertEqual(await reader.readexactly(length), b"\x7f\x00" * 160)

            writer.write(frame(a.KIND_HANGUP))
            await writer.drain()
            await asyncio.wait_for(done.wait(), 2)
            writer.close()
        finally:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
