"""Offline tests for the AudioSocket <-> stt-machine bridge.

A fake Asterisk on one side (a plain TCP client speaking AudioSocket frames) and a fake stt
machine on the other (a loopback WebSocket server), with nothing real in between. Needs
`websockets`; needs no numpy, no model, no Asterisk, no phone.

    .venv/bin/python -m unittest phone.test_session -v
"""

from __future__ import annotations

import asyncio
import json
import unittest
import uuid

from . import audiosocket, session


def frame(kind: int, payload: bytes = b"") -> bytes:
    return bytes([kind]) + len(payload).to_bytes(2, "big") + payload


class FakeStt:
    """A WebSocket server standing in for the stt machine."""

    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.control: list[dict] = []
        self.connected = asyncio.Event()
        self._server = None
        self._ws = None

    async def start(self) -> str:
        from websockets.asyncio.server import serve

        self._server = await serve(self._handle, "127.0.0.1", 0)
        port = next(iter(self._server.sockets)).getsockname()[1]
        return f"ws://127.0.0.1:{port}/"

    async def _handle(self, websocket):
        self._ws = websocket
        self.connected.set()
        try:
            async for message in websocket:
                if isinstance(message, str):
                    self.control.append(json.loads(message))
                else:
                    self.audio.append(message)
        except Exception:
            pass

    async def send_transcript(self, call_id: str, text: str) -> None:
        await self._ws.send(json.dumps({"type": "transcript", "call": call_id, "text": text}))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


class SessionTestCase(unittest.IsolatedAsyncioTestCase):
    """Runs `handle_call` against a real AudioSocket connection and a fake stt machine."""

    stt_url_override: str | None = None

    async def asyncSetUp(self):
        self.stt = FakeStt()
        self.stt_url = await self.stt.start()
        self.transcripts: list[dict] = []
        self.finished = asyncio.Event()

        async def handler(call):
            await session.handle_call(
                call,
                stt_url=self.stt_url_override or self.stt_url,
                sink=self.transcripts.append,
                verbose=True,
            )
            self.finished.set()

        self.server = await audiosocket.serve(handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.call_uuid = uuid.uuid4()
        self.writer.write(frame(audiosocket.KIND_UUID, self.call_uuid.bytes))
        await self.writer.drain()

    async def asyncTearDown(self):
        self.writer.close()
        self.server.close()
        await self.server.wait_closed()
        await self.stt.stop()

    @property
    def label(self) -> str:
        return str(self.call_uuid).split("-")[0]

    async def send_audio(self, n: int = 3):
        for i in range(n):
            self.writer.write(frame(audiosocket.KIND_AUDIO, bytes([i]) * 320))
        await self.writer.drain()

    async def hangup(self):
        self.writer.write(frame(audiosocket.KIND_HANGUP))
        await self.writer.drain()
        await asyncio.wait_for(self.finished.wait(), 5)


class TestHappyPath(SessionTestCase):
    async def test_audio_reaches_the_stt_machine_as_binary(self):
        await asyncio.wait_for(self.stt.connected.wait(), 2)
        await self.send_audio(3)
        await asyncio.sleep(0.2)
        await self.hangup()

        self.assertEqual(len(self.stt.audio), 3)
        self.assertEqual(self.stt.audio[0], b"\x00" * 320)
        self.assertEqual(self.stt.audio[2], b"\x02" * 320)

    async def test_call_start_announces_the_id_and_the_wire_rate(self):
        await asyncio.wait_for(self.stt.connected.wait(), 2)
        await asyncio.sleep(0.2)
        await self.hangup()

        start = self.stt.control[0]
        self.assertEqual(start["type"], "call_start")
        self.assertEqual(start["call_id"], self.label)
        # 8kHz: the line's own rate. Announcing 16000 here would make the far side warn
        # and then transcribe everything at double speed.
        self.assertEqual(start["rate"], 8000)

    async def test_hangup_sends_call_end(self):
        await asyncio.wait_for(self.stt.connected.wait(), 2)
        await self.send_audio(1)
        await asyncio.sleep(0.2)
        await self.hangup()

        self.assertEqual(self.stt.control[-1]["type"], "call_end")

    async def test_transcripts_reach_the_local_sink(self):
        """The whole point: text produced on the Mac arrives on this box."""
        await asyncio.wait_for(self.stt.connected.wait(), 2)
        await self.send_audio(1)
        await asyncio.sleep(0.2)
        await self.stt.send_transcript(self.label, "hello from the other machine")
        await asyncio.sleep(0.3)
        await self.hangup()

        self.assertEqual(len(self.transcripts), 1)
        self.assertEqual(self.transcripts[0]["text"], "hello from the other machine")

    async def test_dtmf_is_not_forwarded_as_audio(self):
        await asyncio.wait_for(self.stt.connected.wait(), 2)
        self.writer.write(frame(audiosocket.KIND_DTMF, b"9"))
        await self.send_audio(1)
        await asyncio.sleep(0.2)
        await self.hangup()

        self.assertEqual(len(self.stt.audio), 1)
        self.assertNotIn(b"9", self.stt.audio)


class TestSttUnavailable(SessionTestCase):
    """A dead transcriber must not be able to drop a call."""

    stt_url_override = "ws://127.0.0.1:1/"  # nothing listens on port 1

    async def test_call_survives_a_refused_connection(self):
        await self.send_audio(5)
        await asyncio.sleep(0.2)

        # The call is still up and still draining -- a socket nobody reads backs up into
        # Asterisk, so "no transcriber" must not mean "stop reading".
        await self.hangup()

        self.assertEqual(self.transcripts, [])

    async def test_no_audio_is_buffered_up_for_a_link_that_never_arrives(self):
        await self.send_audio(50)
        await asyncio.sleep(0.2)
        await self.hangup()

        self.assertEqual(self.stt.audio, [])


class TestSttDropsMidCall(SessionTestCase):
    async def test_call_survives_the_link_dying(self):
        await asyncio.wait_for(self.stt.connected.wait(), 2)
        await self.send_audio(2)
        await asyncio.sleep(0.2)

        await self.stt.stop()  # the transcriber goes away mid-call
        await asyncio.sleep(0.3)
        await self.send_audio(2)  # the caller is still talking
        await asyncio.sleep(0.2)

        await self.hangup()  # and the call still ends cleanly

        self.assertGreaterEqual(len(self.stt.audio), 2)


class TestSinks(unittest.TestCase):
    def test_jsonl_sink_emits_one_parseable_record(self):
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            session.jsonl_transcript({"call": "abc", "text": "hi", "dur_ms": 1.0})

        self.assertEqual(json.loads(buf.getvalue())["text"], "hi")

    def test_print_sink_includes_the_text(self):
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            session.print_transcript({"call": "abc", "text": "spoken words"})

        self.assertIn("spoken words", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
