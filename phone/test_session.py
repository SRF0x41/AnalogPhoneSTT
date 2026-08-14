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
    """A WebSocket server standing in for the stt machine.

    Follows the real server's hangup contract (`stt_port/server.py`): on `call_end` it may
    send one last transcript -- the utterance the hangup interrupted -- and then closes the
    socket itself. The phone side waits for that close, so a fake that held the connection
    open would misrepresent every call as a 6-second linger.
    """

    def __init__(self, final_transcript: str | None = None, close_on_end: bool = True) -> None:
        self.audio: list[bytes] = []
        self.control: list[dict] = []
        self.connected = asyncio.Event()
        self.final_transcript = final_transcript
        self.close_on_end = close_on_end
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
                    control = json.loads(message)
                    self.control.append(control)
                    if control.get("type") == "call_end":
                        if self.final_transcript is not None:
                            await self.send_transcript(
                                str(control.get("call_id", "")), self.final_transcript
                            )
                        if self.close_on_end:
                            break
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
    final_transcript: str | None = None
    close_on_end: bool = True

    async def asyncSetUp(self):
        self.stt = FakeStt(final_transcript=self.final_transcript, close_on_end=self.close_on_end)
        self.stt_url = await self.stt.start()
        self.transcripts: list[dict] = []
        self.finished = asyncio.Event()

        self.call = None

        async def handler(call):
            self.call = call
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


class TestFinalTranscript(SessionTestCase):
    """The utterance a hangup interrupts is usually the one worth having."""

    final_transcript = "the last thing the caller said"

    async def test_a_transcript_sent_after_call_end_still_reaches_the_sink(self):
        # Regression: `close()` used to send call_end and shut the socket in one step, so a
        # transcript the stt machine produced *after* the hangup -- which is every call's
        # last utterance, since only hangup flushes it -- had nowhere to land.
        await asyncio.wait_for(self.stt.connected.wait(), 2)
        await self.send_audio(2)
        await asyncio.sleep(0.2)
        await self.hangup()

        self.assertEqual([t["text"] for t in self.transcripts], [self.final_transcript])

    async def test_call_end_is_sent_after_the_audio_it_follows(self):
        # call_end overtaking queued frames would truncate that same final utterance.
        await asyncio.wait_for(self.stt.connected.wait(), 2)
        await self.send_audio(5)
        await self.hangup()

        self.assertEqual(len(self.stt.audio), 5)
        self.assertEqual(self.stt.control[-1]["type"], "call_end")


class TestSttNeverCloses(SessionTestCase):
    """A far side that ignores the contract must not hang the call forever."""

    close_on_end = False

    async def test_the_grace_period_is_bounded(self):
        await asyncio.wait_for(self.stt.connected.wait(), 2)
        await self.send_audio(1)
        original = session.FINAL_TRANSCRIPT_GRACE_SECONDS
        session.FINAL_TRANSCRIPT_GRACE_SECONDS = 0.3
        try:
            await self.hangup()  # would block forever if the wait were unbounded
        finally:
            session.FINAL_TRANSCRIPT_GRACE_SECONDS = original


class TestBackpressure(SessionTestCase):
    async def test_a_stalled_link_does_not_stop_the_audiosocket_being_read(self):
        # A socket nobody reads backs up into Asterisk, so a far side that stops reading
        # must cost dropped frames, not a stalled call.
        await asyncio.wait_for(self.stt.connected.wait(), 2)
        await self.send_audio(session.OUTBOUND_QUEUE_FRAMES * 3)
        await asyncio.sleep(0.3)
        await self.hangup()

        # Every frame was read off the AudioSocket even though far more were offered than
        # the outbound queue can hold.
        self.assertEqual(self.call.frames_in, session.OUTBOUND_QUEUE_FRAMES * 3)


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


class TestOutboundQueueIsBounded(unittest.IsolatedAsyncioTestCase):
    """A far side that stops draining must cost frames, not stall the call.

    Loopback is too fast to ever fill the queue, so the stall is injected directly: a
    websocket whose `send` never completes, which is what a stt machine that has wedged
    with its connection still open looks like from here.
    """

    async def test_frames_are_dropped_and_the_caller_is_never_blocked(self):
        link = session.SttLink("ws://not-used/", "call-1")

        class WedgedWebSocket:
            async def send(self, _data):
                await asyncio.sleep(3600)

        link._ws = WedgedWebSocket()
        link._start_pump()
        offered = session.OUTBOUND_QUEUE_FRAMES * 3

        loop = asyncio.get_running_loop()
        started = loop.time()
        for _ in range(offered):
            await link.send_audio(b"\x00" * 320)
        elapsed = loop.time() - started

        try:
            self.assertLess(elapsed, 0.5, "send_audio blocked on the wedged socket")
            self.assertEqual(link.frames_sent, 0, "the wedged send cannot have completed")
            # At most the queue's depth (plus one in the pump's hand) was ever held.
            self.assertGreaterEqual(
                link.frames_dropped, offered - session.OUTBOUND_QUEUE_FRAMES - 1
            )
        finally:
            await link.close()


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
