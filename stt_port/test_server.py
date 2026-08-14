"""Offline tests for the WebSocket STT service.

No phone, no Asterisk, no model: a real WebSocket over loopback, a stub backend, and
synthetic audio. Needs numpy and websockets, but neither mlx-whisper nor sounddevice.

    ./venv/bin/python -m unittest stt_port.test_server -v
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import queue
import threading
import unittest

import numpy as np

from . import server
from .audio import HANGOVER_MS, Segmenter


def tone(samples: int, amplitude: float = 0.4) -> bytes:
    """Loud PCM16 the segmenter will treat as speech."""
    t = np.arange(samples, dtype=np.float32)
    wave = np.sin(2 * np.pi * 300 * t / server.WIRE_RATE) * amplitude
    return (wave * 32767).astype("<i2").tobytes()


def silence(samples: int) -> bytes:
    return b"\x00\x00" * samples


class TestBlockAccumulator(unittest.TestCase):
    """`_Call.blocks_from` must emit fixed-size blocks whatever the sender's chunking."""

    def setUp(self):
        self.call = server._Call("test", 0.01, HANGOVER_MS)

    def test_exact_frame_yields_one_block(self):
        blocks = list(self.call.blocks_from(tone(server.BLOCK_SAMPLES)))

        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(blocks[0]), server.BLOCK_SAMPLES)

    def test_partial_frames_are_carried_over_not_dropped(self):
        # 240 samples, then 80: one block should come out, split across the two sends.
        first = list(self.call.blocks_from(tone(240)))
        second = list(self.call.blocks_from(tone(80)))

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(self.call.blocks, 2)

    def test_coalesced_frames_split_into_whole_blocks(self):
        blocks = list(self.call.blocks_from(tone(server.BLOCK_SAMPLES * 5)))

        self.assertEqual(len(blocks), 5)
        self.assertTrue(all(len(b) == server.BLOCK_SAMPLES for b in blocks))

    def test_odd_byte_count_does_not_desync(self):
        # A truncated send must not shift every later sample by one byte, which would
        # turn the rest of the call into noise.
        list(self.call.blocks_from(tone(server.BLOCK_SAMPLES) + b"\x01"))
        blocks = list(self.call.blocks_from(b"\x02" + tone(server.BLOCK_SAMPLES)))

        self.assertEqual(len(blocks), 1)

    def test_pcm16_to_float32_is_little_endian_and_normalized(self):
        # 0x7FFF little-endian = +1.0-ish; get the byte order wrong and this is -0.004.
        self.assertAlmostEqual(float(server.pcm16_to_float32(b"\xff\x7f")[0]), 1.0, places=3)
        self.assertAlmostEqual(float(server.pcm16_to_float32(b"\x00\x80")[0]), -1.0, places=3)


class TestSegmenterAt8k(unittest.TestCase):
    """The wire rate changed from 16k to 8k; the segmenter's timing must not have."""

    def test_hangover_is_the_same_wall_clock_duration_at_either_rate(self):
        at_8k = Segmenter(native_rate=8000, block_size=160, hangover_ms=HANGOVER_MS)
        at_16k = Segmenter(native_rate=16000, block_size=320, hangover_ms=HANGOVER_MS)

        # Both are 20ms blocks, so the same number of them makes up the hangover.
        self.assertEqual(at_8k.hangover_blocks, at_16k.hangover_blocks)
        self.assertEqual(at_8k.hangover_blocks * 0.02 * 1000, HANGOVER_MS)

    def test_preroll_and_max_chunk_scale_with_rate(self):
        at_8k = Segmenter(native_rate=8000, block_size=160)
        at_16k = Segmenter(native_rate=16000, block_size=320)

        self.assertEqual(at_16k.preroll_samples, at_8k.preroll_samples * 2)
        self.assertEqual(at_16k.max_chunk_samples, at_8k.max_chunk_samples * 2)

    def test_speech_then_silence_closes_a_chunk(self):
        seg = Segmenter(native_rate=8000, block_size=160, energy_threshold=0.01)
        block = server.pcm16_to_float32(tone(160))
        quiet = server.pcm16_to_float32(silence(160))

        for _ in range(10):
            self.assertIsNone(seg.process(block))
        closed = None
        for _ in range(seg.hangover_blocks + 1):
            result = seg.process(quiet)
            if result is not None:
                closed = result

        self.assertIsNotNone(closed)
        self.assertGreater(len(closed), 0)


class TestPreroll(unittest.TestCase):
    """The preroll must contribute the audio just before an utterance -- and nothing else."""

    @staticmethod
    def seg(**kw) -> Segmenter:
        return Segmenter(native_rate=8000, block_size=160, energy_threshold=0.1,
                         hangover_ms=100, preroll_ms=300, **kw)

    @staticmethod
    def block(value: float) -> np.ndarray:
        """A constant-valued block, so its samples can be counted in the finished chunk."""
        return np.full(160, value, dtype=np.float32)

    def close_with_silence(self, seg: Segmenter) -> np.ndarray:
        for _ in range(seg.hangover_blocks + 2):
            chunk = seg.process(self.block(0.0))
            if chunk is not None:
                return chunk
        self.fail("silence did not close the chunk")

    def test_the_opening_block_is_not_duplicated(self):
        # Regression: the triggering block was written into the preroll ring *and* appended
        # after it, so every utterance began with a stutter of its own first 20ms.
        seg = self.seg()
        for _ in range(20):
            seg.process(self.block(0.0))
        seg.process(self.block(0.5))  # opens the chunk
        chunk = self.close_with_silence(seg)

        self.assertEqual(int(np.sum(np.isclose(chunk, 0.5))), 160, "exactly one block's worth")

    def test_a_chunk_never_starts_with_audio_from_before_the_previous_one(self):
        # Regression: the ring goes unwritten while a chunk is open, so after a close it
        # still held pre-previous-utterance audio. Worst case is a close on max_chunk_samples
        # mid-sentence, where the next block is loud and the whole stale preroll is spliced
        # into the middle of continuous speech.
        seg = self.seg(max_chunk_seconds=1.0)
        for _ in range(20):
            seg.process(self.block(0.02))  # quiet, distinctive, and long past by the end
        first = None
        while first is None:
            first = seg.process(self.block(0.7))  # runs into the 1s cap
        seg.process(self.block(0.7))  # speech continues; opens the next chunk
        second = self.close_with_silence(seg)

        self.assertEqual(int(np.sum(np.isclose(second, 0.02))), 0, "stale audio was spliced in")

    def test_the_preroll_still_captures_a_soft_onset(self):
        # The fix must not cost the preroll its purpose: quiet audio immediately before the
        # threshold is crossed is the leading consonant, and belongs in the chunk.
        seg = self.seg()
        for _ in range(5):
            seg.process(self.block(0.05))  # below threshold, but real audio
        seg.process(self.block(0.5))
        chunk = self.close_with_silence(seg)

        self.assertEqual(int(np.sum(np.isclose(chunk, 0.05))), 5 * 160)
        # And it is prepended, not appended: the onset precedes the loud block.
        self.assertTrue(np.isclose(chunk[0], 0.05))

    def test_a_short_gap_yields_a_short_preroll_not_a_padded_one(self):
        seg = self.seg()
        for _ in range(20):
            seg.process(self.block(0.03))
        seg.process(self.block(0.6))
        self.close_with_silence(seg)  # resets the ring

        seg.process(self.block(0.04))  # one block of gap
        seg.process(self.block(0.6))
        chunk = self.close_with_silence(seg)

        self.assertEqual(int(np.sum(np.isclose(chunk, 0.04))), 160)
        self.assertEqual(int(np.sum(np.isclose(chunk, 0.03))), 0)


class ServerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.q: queue.Queue = queue.Queue()
        self.source = server.WebSocketSource(
            self.q, host="127.0.0.1", port=0, energy_threshold=0.01, idle_timeout=0
        )
        await self.source.start()
        self.url = f"ws://127.0.0.1:{self.source.bound_port}/"

    async def asyncTearDown(self):
        await self.source.stop()

    async def connect(self):
        from websockets.asyncio.client import connect

        return await connect(self.url)

    def drain_queue(self) -> list:
        items = []
        while not self.q.empty():
            item = self.q.get_nowait()
            if item is not None:
                items.append(item)
        return items


class TestReceive(ServerTestCase):
    async def test_call_start_then_speech_then_hangup_yields_a_chunk(self):
        async with await self.connect() as ws:
            await ws.send(json.dumps({"type": "call_start", "call_id": "abc123", "rate": 8000}))
            for _ in range(20):
                await ws.send(tone(server.BLOCK_SAMPLES))
            await ws.send(json.dumps({"type": "call_end", "reason": "hangup"}))
            await asyncio.sleep(0.2)

        items = self.drain_queue()

        self.assertEqual(len(items), 1, "hangup should flush the open utterance")
        chunk, rate, _closed_at, call_id = items[0]
        self.assertEqual(rate, server.WIRE_RATE)
        self.assertEqual(call_id, "abc123")
        self.assertGreater(len(chunk), 0)

    async def test_audio_before_call_start_is_adopted(self):
        # The phone machine restarted mid-call, or a client didn't announce itself. The
        # audio is real; dropping it would lose a whole call.
        async with await self.connect() as ws:
            for _ in range(20):
                await ws.send(tone(server.BLOCK_SAMPLES))
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.2)

        items = self.drain_queue()

        self.assertEqual(len(items), 1)
        self.assertTrue(items[0][3].startswith("anon-"))

    async def test_socket_close_without_call_end_still_flushes(self):
        ws = await self.connect()
        await ws.send(json.dumps({"type": "call_start", "call_id": "dropped"}))
        for _ in range(20):
            await ws.send(tone(server.BLOCK_SAMPLES))
        await asyncio.sleep(0.1)
        await ws.close()
        await asyncio.sleep(0.2)

        items = self.drain_queue()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][3], "dropped")

    async def test_malformed_text_message_does_not_kill_the_call(self):
        async with await self.connect() as ws:
            await ws.send("this is not json")
            await ws.send(json.dumps({"type": "call_start", "call_id": "ok"}))
            for _ in range(20):
                await ws.send(tone(server.BLOCK_SAMPLES))
            await ws.send(json.dumps({"type": "call_end"}))
            await asyncio.sleep(0.2)

        self.assertEqual(len(self.drain_queue()), 1)

    async def test_two_calls_are_segmented_independently(self):
        async def one_call(call_id: str):
            async with await self.connect() as ws:
                await ws.send(json.dumps({"type": "call_start", "call_id": call_id}))
                for _ in range(20):
                    await ws.send(tone(server.BLOCK_SAMPLES))
                await ws.send(json.dumps({"type": "call_end"}))
                await asyncio.sleep(0.2)

        await asyncio.gather(one_call("first"), one_call("second"))
        await asyncio.sleep(0.2)

        ids = sorted(item[3] for item in self.drain_queue())
        self.assertEqual(ids, ["first", "second"])


class TestTranscriptReturnPath(ServerTestCase):
    async def test_emit_sends_a_transcript_back_to_the_right_call(self):
        async with await self.connect() as ws:
            await ws.send(json.dumps({"type": "call_start", "call_id": "xyz789"}))
            await ws.send(tone(server.BLOCK_SAMPLES))
            await asyncio.sleep(0.2)

            # Stands in for consume_chunks finishing an utterance on its worker thread.
            await asyncio.to_thread(
                self.source.emit, "xyz789", {"call": "xyz789", "text": "hello there", "dur_ms": 1.0}
            )

            message = json.loads(await asyncio.wait_for(ws.recv(), 2))

        self.assertEqual(message["type"], "transcript")
        self.assertEqual(message["text"], "hello there")
        self.assertEqual(message["call"], "xyz789")

    async def test_emit_for_an_unknown_call_is_silently_dropped(self):
        # The call hung up before inference finished. Nothing to send to, nothing to fix.
        await asyncio.to_thread(self.source.emit, "gone", {"text": "too late"})


class TestFinalTranscriptSurvivesHangup(unittest.IsolatedAsyncioTestCase):
    """Regression: the last utterance of a call was transcribed but never sent back.

    Hangup is the only thing that flushes a partial utterance, and the socket used to be
    unregistered in the same breath -- so by the time inference finished on the worker
    thread, `emit` had nowhere to send the one sentence the flush existed to rescue.
    """

    async def asyncSetUp(self):
        self.q: queue.Queue = queue.Queue()
        self.source = server.WebSocketSource(
            self.q,
            host="127.0.0.1",
            port=0,
            energy_threshold=0.01,
            idle_timeout=0,
            final_drain_timeout=5.0,
        )
        await self.source.start()
        self.stop_consumer = threading.Event()
        self.transcribed: list[str] = []
        threading.Thread(target=self._consume, daemon=True).start()

    async def asyncTearDown(self):
        self.stop_consumer.set()
        self.q.put(None)
        await self.source.stop()

    def _consume(self) -> None:
        """Stands in for `consume_chunks`: slow inference, then hand the record to `emit`."""
        while not self.stop_consumer.is_set():
            item = self.q.get()
            if item is None:
                return
            _chunk, _rate, _closed_at, call_id = item
            self.stop_consumer.wait(0.3)  # inference is never instant
            self.transcribed.append(call_id)
            self.source.emit(call_id, {"call": call_id, "text": f"utterance {len(self.transcribed)}"})

    async def test_the_utterance_hangup_flushed_is_delivered(self):
        from websockets.asyncio.client import connect

        received: list[str] = []
        async with await connect(f"ws://127.0.0.1:{self.source.bound_port}/") as ws:
            async def listen():
                with contextlib.suppress(Exception):
                    async for message in ws:
                        if isinstance(message, str):
                            record = json.loads(message)
                            if record.get("type") == "transcript":
                                received.append(record["text"])

            listener = asyncio.create_task(listen())
            await ws.send(json.dumps({"type": "call_start", "call_id": "last-word"}))
            for _ in range(20):
                await ws.send(tone(server.BLOCK_SAMPLES))
            # Hangup mid-utterance: no trailing silence, so only the flush closes it.
            await ws.send(json.dumps({"type": "call_end", "reason": "hangup"}))
            await asyncio.wait_for(listener, 5)

        self.assertEqual(self.transcribed, ["last-word"], "the tail should be transcribed")
        self.assertEqual(received, ["utterance 1"], "and it should reach the phone machine")

    async def test_a_call_whose_tail_is_silence_does_not_wait_out_the_timeout(self):
        # Nothing to flush means nothing to wait for; the socket should close promptly
        # rather than lingering for `final_drain_timeout`.
        from websockets.asyncio.client import connect

        async with await connect(f"ws://127.0.0.1:{self.source.bound_port}/") as ws:
            await ws.send(json.dumps({"type": "call_start", "call_id": "quiet"}))
            await ws.send(silence(server.BLOCK_SAMPLES))
            await ws.send(json.dumps({"type": "call_end", "reason": "hangup"}))
            start = asyncio.get_running_loop().time()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.wait_closed(), 3)
            elapsed = asyncio.get_running_loop().time() - start

        self.assertLess(elapsed, 2.0, f"closed after {elapsed:.1f}s; should not have waited")


class TestIdleFlush(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_call_is_flushed_without_a_call_end(self):
        q: queue.Queue = queue.Queue()
        source = server.WebSocketSource(
            q, host="127.0.0.1", port=0, energy_threshold=0.01, idle_timeout=0.3
        )
        await source.start()
        try:
            from websockets.asyncio.client import connect

            async with await connect(f"ws://127.0.0.1:{source.bound_port}/") as ws:
                await ws.send(json.dumps({"type": "call_start", "call_id": "stalled"}))
                for _ in range(20):
                    await ws.send(tone(server.BLOCK_SAMPLES))
                # Go quiet at the transport level -- no frames at all, not even silence.
                await asyncio.sleep(1.0)

                self.assertFalse(q.empty(), "idle timeout should have flushed the utterance")
                chunk, _rate, _t, call_id = q.get_nowait()
                self.assertEqual(call_id, "stalled")
                self.assertGreater(len(chunk), 0)
        finally:
            await source.stop()


class TestConsumeChunksSink(unittest.TestCase):
    """`consume_chunks` must feed the sink as well as stdout, without changing mic behavior."""

    def test_sink_receives_the_same_record_that_is_printed(self):
        from .main import consume_chunks

        class StubBackend:
            def transcribe(self, audio):
                return "  transcribed text  "

        q: queue.Queue = queue.Queue()
        q.put((np.zeros(16000, dtype=np.float32), 16000, 0.0, "call-1"))
        q.put(None)
        args = argparse.Namespace(
            debug_save_wav=False, verbose=False, jsonl=True, hangover_ms=HANGOVER_MS
        )
        seen = []

        consume_chunks(StubBackend(), q, args, sink=lambda cid, rec: seen.append((cid, rec)))

        self.assertEqual(len(seen), 1)
        call_id, record = seen[0]
        self.assertEqual(call_id, "call-1")
        self.assertEqual(record["text"], "transcribed text")
        self.assertEqual(record["call"], "call-1")

    def test_empty_transcript_is_signalled_to_the_sink_as_None(self):
        """No transcript is produced, but the chunk must still be reported as finished.

        `WebSocketSource` holds a hung-up call's socket open until its queued chunks come
        back; a chunk that transcribed to nothing has to count, or a call whose last
        utterance was line noise waits out the whole drain timeout for nothing.
        """
        from .main import consume_chunks

        class SilentBackend:
            def transcribe(self, audio):
                return "   "

        q: queue.Queue = queue.Queue()
        q.put((np.zeros(16000, dtype=np.float32), 16000, 0.0, "call-2"))
        q.put(None)
        args = argparse.Namespace(
            debug_save_wav=False, verbose=False, jsonl=True, hangover_ms=HANGOVER_MS
        )
        seen = []
        printed = io.StringIO()

        with contextlib.redirect_stdout(printed):
            consume_chunks(SilentBackend(), q, args, sink=lambda cid, rec: seen.append(rec))

        self.assertEqual(seen, [None], "the chunk is finished with, but there is no transcript")
        self.assertEqual(printed.getvalue(), "", "nothing empty should reach stdout")


if __name__ == "__main__":
    unittest.main()
