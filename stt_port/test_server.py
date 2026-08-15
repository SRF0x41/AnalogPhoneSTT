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
import time
import unittest

import numpy as np

from . import audio, main, server
from .audio import HANGOVER_MS, Segmenter


def wave_f32(samples: int, rate: int = server.WIRE_RATE, amplitude: float = 0.4) -> np.ndarray:
    t = np.arange(samples, dtype=np.float32)
    return np.sin(2 * np.pi * 300 * t / rate).astype(np.float32) * amplitude


def tone(samples: int, amplitude: float = 0.4) -> bytes:
    """Loud PCM16 the segmenter will treat as speech."""
    return (wave_f32(samples, amplitude=amplitude) * 32767).astype("<i2").tobytes()


def silence(samples: int) -> bytes:
    return b"\x00\x00" * samples


def consumer_args(**overrides) -> argparse.Namespace:
    """The args `consume_chunks` reads, defaulted the way the CLI defaults them.

    Built from the same constants the parser uses, so a changed default is exercised here
    rather than quietly diverging from what actually ships.
    """
    base = {
        "debug_save_wav": False,
        "verbose": False,
        "jsonl": True,
        "hangover_ms": HANGOVER_MS,
        "min_speech_ms": main.DEFAULT_MIN_SPEECH_MS,
        "min_speech_rms": main.DEFAULT_MIN_SPEECH_RMS,
        "min_speech_floor_ms": main.DEFAULT_MIN_SPEECH_FLOOR_MS,
        "inference_timeout": main.DEFAULT_INFERENCE_TIMEOUT,
        "numerals": "asis",
    }
    return argparse.Namespace(**{**base, **overrides})


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
        chunk, rate, _closed_at, call_id, final = items[0]
        self.assertEqual(rate, server.WIRE_RATE)
        self.assertEqual(call_id, "abc123")
        self.assertGreater(len(chunk), 0)
        self.assertTrue(final, "hangup flushed it, which is what the B2 gate keys on")

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
            _chunk, _rate, _closed_at, call_id, _final = item
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
                chunk, _rate, _t, call_id, final = q.get_nowait()
                self.assertEqual(call_id, "stalled")
                self.assertGreater(len(chunk), 0)
                self.assertFalse(
                    final,
                    "a stalled call has not hung up: this is speech in progress, not a "
                    "cradle click, and B2 must not treat it as one",
                )
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
        q.put((wave_f32(16000, rate=16000), 16000, 0.0, "call-1", False))
        q.put(None)
        args = consumer_args()
        seen = []

        consume_chunks(StubBackend(), q, args, sink=lambda cid, rec: seen.append((cid, rec)))

        self.assertEqual(len(seen), 1)
        call_id, record = seen[0]
        self.assertEqual(call_id, "call-1")
        # Stripped *and* normalised on the way through -- see TestNormaliseTranscript.
        self.assertEqual(record["text"], "Transcribed text")
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
        q.put((wave_f32(16000, rate=16000), 16000, 0.0, "call-2", False))
        q.put(None)
        args = consumer_args()
        seen = []
        printed = io.StringIO()

        with contextlib.redirect_stdout(printed):
            consume_chunks(SilentBackend(), q, args, sink=lambda cid, rec: seen.append(rec))

        self.assertEqual(seen, [None], "the chunk is finished with, but there is no transcript")
        self.assertEqual(printed.getvalue(), "", "nothing empty should reach stdout")


class TestSpeechProfile(unittest.TestCase):
    """The metrics the filler gates are calibrated against (docs/ANALOG-TUNING.md, change B)."""

    def test_loud_speech_measures_its_own_duration_and_level(self):
        # A 1s sine at amplitude 0.4 has RMS 0.4/sqrt(2); every frame clears the floor.
        p = audio.speech_profile(wave_f32(8000), 8000)

        self.assertAlmostEqual(p.speech_s, 1.0, delta=0.03)
        self.assertAlmostEqual(p.mean_rms, 0.4 / np.sqrt(2), delta=0.01)
        self.assertAlmostEqual(p.longest_run_s, 1.0, delta=0.03)

    def test_silence_measures_as_no_speech_at_all(self):
        p = audio.speech_profile(np.zeros(8000, dtype=np.float32), 8000)

        self.assertEqual(tuple(p), (0.0, 0.0, 0.0))

    def test_only_frames_above_the_floor_are_counted(self):
        # Half loud, half below the floor: the duration halves, the level does not sag.
        x = np.concatenate([wave_f32(4000), wave_f32(4000, amplitude=0.001)])

        p = audio.speech_profile(x, 8000)

        self.assertAlmostEqual(p.speech_s, 0.5, delta=0.03)
        self.assertAlmostEqual(p.mean_rms, 0.4 / np.sqrt(2), delta=0.01)

    def test_scattered_taps_measure_a_short_run_despite_a_long_total(self):
        """The handset-settling signature, and why total duration cannot see it.

        Three isolated 60ms taps spread over 1.4s: 0.18s of speech in total, but nothing
        continuous. A real word of the same total duration is one unbroken run.
        """
        tap = wave_f32(480)  # 60ms
        gap = np.zeros(4000, dtype=np.float32)  # 500ms
        x = np.concatenate([gap, tap, gap, tap, gap, tap])

        p = audio.speech_profile(x, 8000)

        self.assertAlmostEqual(p.speech_s, 0.18, delta=0.04)
        self.assertLess(p.longest_run_s, 0.10)
        self.assertGreater(p.mean_rms, 0.2, "and it is loud, so no energy gate reaches it")

    def test_continuous_speech_runs_as_long_as_it_lasts(self):
        p = audio.speech_profile(np.concatenate([np.zeros(800, dtype=np.float32), wave_f32(2400)]), 8000)

        self.assertAlmostEqual(p.longest_run_s, p.speech_s, delta=0.03)

    def test_mean_rms_can_never_land_between_zero_and_the_floor(self):
        """The arithmetic that made the first version of the B1 gate inert.

        The mean is taken over frames *already* above `SPEECH_FRAME_FLOOR`, so it is either
        exactly 0.0 or strictly above the floor -- never in between. A gate threshold at or
        below the floor therefore fires only on total silence, which is why B1 originally
        suppressed nothing at all. Anything measuring speech at all must land above it.
        """
        for amplitude in (0.001, 0.005, 0.014, 0.02, 0.4):
            p = audio.speech_profile(wave_f32(8000, amplitude=amplitude), 8000)

            self.assertFalse(
                0.0 < p.mean_rms <= audio.SPEECH_FRAME_FLOOR,
                f"amplitude {amplitude} produced an impossible mean_rms of {p.mean_rms}",
            )


class TestTrimTrailingSilence(unittest.TestCase):
    """Change C: shorten what the model sees, without touching what closed the utterance."""

    def test_the_hangover_tail_is_removed_but_a_pad_is_kept(self):
        chunk = np.concatenate([wave_f32(4000), np.zeros(4000, dtype=np.float32)])

        trimmed = audio.trim_trailing_silence(chunk, 8000, keep_ms=100)

        # 0.5s of speech, plus 100ms of the 0.5s tail, give or take a frame.
        self.assertAlmostEqual(len(trimmed) / 8000, 0.6, delta=0.03)
        self.assertLess(len(trimmed), len(chunk))

    def test_speech_is_never_truncated(self):
        chunk = np.concatenate([wave_f32(4000), np.zeros(4000, dtype=np.float32)])

        trimmed = audio.trim_trailing_silence(chunk, 8000, keep_ms=0)

        self.assertGreaterEqual(len(trimmed), 4000)

    def test_an_all_quiet_chunk_is_returned_whole_not_emptied(self):
        """Handing the model a zero-length array is worse than handing it silence."""
        chunk = np.zeros(8000, dtype=np.float32)

        self.assertEqual(len(audio.trim_trailing_silence(chunk, 8000)), 8000)

    def test_a_chunk_with_no_tail_is_left_alone(self):
        chunk = wave_f32(8000)

        self.assertEqual(len(audio.trim_trailing_silence(chunk, 8000)), 8000)


def profile(speech_s: float, mean_rms: float, run_s: float | None = None) -> audio.SpeechProfile:
    """A measured chunk. `run_s` defaults to `speech_s` -- i.e. continuous speech."""
    return audio.SpeechProfile(speech_s, mean_rms, speech_s if run_s is None else run_s)


class TestFillerGates(unittest.TestCase):
    """Change B, at the boundaries. Every number here comes from a measured clip."""

    def setUp(self):
        self.args = consumer_args()

    def test_near_silence_is_dropped(self):
        # The measured quiet filler: 0.02-0.16s of speech at 0.0102-0.0163 RMS.
        self.assertIsNotNone(main.filler_reason(profile(0.02, 0.0102), False, self.args))
        self.assertIsNotNone(main.filler_reason(profile(0.16, 0.0117), False, self.args))

    def test_a_real_one_word_answer_is_kept(self):
        # "Yes." 0.58s/0.084, "No." 0.36s/0.086, "Four." 0.36s/0.149 -- 8 of 8 measured
        # replies sit clear of the gate on the duration axis, the energy axis, or both.
        for speech_s, rms in [(0.58, 0.084), (0.36, 0.086), (0.36, 0.149), (0.38, 0.078)]:
            self.assertIsNone(main.filler_reason(profile(speech_s, rms), False, self.args))

    def test_short_but_loud_is_kept(self):
        """The conjunction is the whole point: a duration-only gate ate a real word."""
        self.assertIsNone(main.filler_reason(profile(0.24, 0.0823), False, self.args))

    def test_long_but_quiet_is_kept(self):
        self.assertIsNone(main.filler_reason(profile(1.20, 0.0150), False, self.args))

    def test_either_threshold_at_zero_disables_the_conjunction(self):
        """B1's two halves are an AND, so zeroing either one is an off switch for it.

        The floor is a separate rule with its own off switch, hence zeroing it here too --
        otherwise this only proves the floor is doing the work.
        """
        for off in (
            consumer_args(min_speech_ms=0, min_speech_floor_ms=0),
            consumer_args(min_speech_rms=0, min_speech_floor_ms=0),
        ):
            self.assertIsNone(main.filler_reason(profile(0.02, 0.0001), False, off))

    def test_the_conjunction_still_catches_what_the_floor_does_not(self):
        """0.16s of continuous speech clears the floor, but is quiet enough for B1."""
        quiet_pause = profile(0.16, 0.0117)

        self.assertIsNotNone(main.filler_reason(quiet_pause, False, self.args))
        self.assertIsNone(main.filler_reason(quiet_pause, False, consumer_args(min_speech_ms=0)))

    def test_the_hangup_click_is_dropped_however_loud_it_is(self):
        """B2 is structural, not acoustic: the measured clicks peak at 0.98 and 0.15 RMS.

        Louder than most genuine speech and longer than the shortest real utterance, so no
        energy threshold reaches them -- only the fact that hangup is what flushed them.
        """
        self.assertIsNotNone(main.filler_reason(profile(0.20, 0.1524), True, self.args))
        self.assertIsNotNone(main.filler_reason(profile(0.28, 0.1173), True, self.args))

    def test_a_continuous_chunk_of_the_same_size_mid_call_is_kept(self):
        self.assertIsNone(main.filler_reason(profile(0.20, 0.1524), False, self.args))

    def test_a_caller_still_speaking_at_hangup_is_kept(self):
        """Hanging up mid-sentence must not cost the sentence."""
        self.assertIsNone(main.filler_reason(profile(1.80, 0.0900), True, self.args))

    def test_a_loud_click_that_missed_b2_is_caught_by_the_continuity_floor(self):
        """Regression, from two live calls: B2 alone is not enough.

        Hang up a couple of seconds before the socket closes and the click closes on silence
        like any other chunk, arriving with `final=False`. The first such click held 0.06s of
        speech at 0.2372 RMS; the second held 0.20s at 0.1225 -- long enough to clear a
        duration floor and far too loud for B1's energy half -- but in three isolated taps
        whose longest run was 0.12s. Both were transcribed as `Thank you.`.
        """
        self.assertIsNotNone(main.filler_reason(profile(0.06, 0.2372), False, self.args))
        self.assertIsNotNone(main.filler_reason(profile(0.20, 0.1225, run_s=0.12), False, self.args))

    def test_the_floor_does_not_reach_any_real_utterance(self):
        """Shortest real run measured: 0.14s in the corpus, 0.20s across the live calls."""
        self.assertIsNone(main.filler_reason(profile(0.24, 0.0823, run_s=0.14), False, self.args))
        self.assertIsNone(main.filler_reason(profile(0.20, 0.1791), False, self.args))

    def test_the_floor_can_be_disabled(self):
        off = consumer_args(min_speech_floor_ms=0, min_speech_ms=0)

        self.assertIsNone(main.filler_reason(profile(0.06, 0.2372), False, off))


class TestGatedChunksStillReachTheSink(unittest.TestCase):
    """The hazard the gates had to be written around.

    `WebSocketSource` holds a hung-up call's socket open until every queued chunk has come
    back, and the only thing that reports one as finished is the sink. Since *every* call ends
    with a chunk B2 drops, a gate that skipped the sink would stall every single hangup for
    the full drain timeout. Green unit tests elsewhere would not show it -- only a real socket
    waiting on the other end would.
    """

    def _run(self, item, **overrides):
        calls = []

        class CountingBackend:
            def transcribe(self, audio):
                calls.append(audio)
                return "should not have been called"

        q: queue.Queue = queue.Queue()
        q.put(item)
        q.put(None)
        seen = []
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(io.StringIO()) as err:
            main.consume_chunks(
                CountingBackend(), q, consumer_args(**overrides), sink=lambda cid, rec: seen.append(rec)
            )
        return calls, seen, printed.getvalue(), err.getvalue()

    def test_a_gated_chunk_is_reported_finished_and_never_transcribed(self):
        quiet = wave_f32(1600, rate=8000, amplitude=0.004)  # 0.2s, well under both thresholds

        calls, seen, printed, _err = self._run((quiet, 8000, 0.0, "call-x", False))

        self.assertEqual(calls, [], "the whole point is not paying for the decode")
        self.assertEqual(seen, [None], "but the chunk must still count as finished")
        self.assertEqual(printed, "")

    def test_the_hangup_click_is_gated_and_reported(self):
        click = wave_f32(1600, rate=8000, amplitude=0.4)  # 0.2s, loud -- only B2 catches it

        calls, seen, _printed, _err = self._run((click, 8000, 0.0, "call-x", True))

        self.assertEqual(calls, [])
        self.assertEqual(seen, [None])

    def test_every_suppression_is_logged_even_without_verbose(self):
        """A vanished utterance must be distinguishable from a broken system."""
        quiet = wave_f32(1600, rate=8000, amplitude=0.004)

        _calls, _seen, _printed, err = self._run((quiet, 8000, 0.0, "call-x", False))

        self.assertIn("[gate]", err)

    def test_real_speech_is_not_gated(self):
        speech = wave_f32(8000, rate=8000)

        calls, seen, _printed, _err = self._run((speech, 8000, 0.0, "call-x", False))

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0])


class TestInferenceWatchdog(unittest.TestCase):
    """Change A2. It cannot cancel a decode; it can stop the pipeline waiting on one."""

    def test_a_runaway_decode_is_abandoned(self):
        class StuckBackend:
            def transcribe(self, audio):
                time.sleep(5)
                return "eight check check check"

        watchdog = main.InferenceWatchdog(timeout_s=0.2)
        try:
            self.assertIsNone(watchdog.transcribe(StuckBackend(), np.zeros(16000, dtype=np.float32)))
        finally:
            watchdog.close()

    def test_a_normal_decode_returns_its_text(self):
        class FastBackend:
            def transcribe(self, audio):
                return "Yes."

        watchdog = main.InferenceWatchdog(timeout_s=4.0)
        try:
            self.assertEqual(watchdog.transcribe(FastBackend(), np.zeros(16, dtype=np.float32)), "Yes.")
        finally:
            watchdog.close()

    def test_a_zero_timeout_transcribes_inline_with_no_thread(self):
        class FastBackend:
            def transcribe(self, audio):
                return "Yes."

        watchdog = main.InferenceWatchdog(timeout_s=0)
        try:
            self.assertIsNone(watchdog._pool)
            self.assertEqual(watchdog.transcribe(FastBackend(), np.zeros(16, dtype=np.float32)), "Yes.")
        finally:
            watchdog.close()

    def test_an_abandoned_utterance_still_reports_the_chunk_as_finished(self):
        """Same drain-path contract as a gated chunk: the socket must not hang on it."""

        class StuckBackend:
            def transcribe(self, audio):
                time.sleep(5)
                return "eight check check check"

        q: queue.Queue = queue.Queue()
        q.put((wave_f32(8000, rate=8000), 8000, 0.0, "call-y", False))
        q.put(None)
        seen = []
        printed = io.StringIO()

        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(io.StringIO()) as err:
            main.consume_chunks(
                StuckBackend(),
                q,
                consumer_args(inference_timeout=0.2),
                sink=lambda cid, rec: seen.append(rec),
            )

        self.assertEqual(seen, [None])
        self.assertEqual(printed.getvalue(), "", "the garbage must not reach the transcript")
        self.assertIn("[watchdog]", err.getvalue())


class TestNormaliseTranscript(unittest.TestCase):
    """Change D: consecutive utterances must agree about casing, numerals and punctuation.

    Counting to twenty on a measured call produced `One.` `two.` `3` `four` `5,` `six`
    `Seven.` -- the same class of token rendered five ways, because each utterance is an
    independent decode with no memory of the last.
    """

    def test_casing_is_made_consistent(self):
        self.assertEqual(main.normalise_transcript("two.")[0], "Two.")
        self.assertEqual(main.normalise_transcript("Seven.")[0], "Seven.")

    def test_trailing_commas_are_stripped(self):
        self.assertEqual(main.normalise_transcript("5,")[0], "5")
        self.assertEqual(main.normalise_transcript("  six ,  ")[0], "Six")

    def test_a_comma_inside_the_sentence_is_left_alone(self):
        text, _ = main.normalise_transcript("Please confirm invoice number 4729, dated August 15th.")

        self.assertEqual(text, "Please confirm invoice number 4729, dated August 15th.")

    def test_a_lowercase_opening_word_is_flagged_as_a_continuation(self):
        """The model's own casing is the reassembly hint for a pause-split sentence."""
        text, continues = main.normalise_transcript("and a shortfall.")

        self.assertEqual(text, "And a shortfall.")
        self.assertTrue(continues, "capitalising must not destroy the signal it overwrites")

    def test_a_sentence_start_is_not_flagged(self):
        self.assertFalse(main.normalise_transcript("The branch reported a surplus.")[1])

    def test_a_leading_digit_carries_no_casing_signal(self):
        self.assertFalse(main.normalise_transcript("400 units.")[1])

    def test_numerals_are_left_alone_by_default(self):
        self.assertEqual(main.normalise_transcript("but thirty five boxes")[0], "But thirty five boxes")

    def test_numerals_can_be_converted_for_a_parsing_consumer(self):
        for spoken, expected in [
            ("but thirty five boxes", "But 35 boxes"),
            ("thirty-five", "35"),
            ("four hundred units", "400 units"),
            ("one.", "1."),
            ("fifteen", "15"),
        ]:
            self.assertEqual(main.normalise_transcript(spoken, "digits")[0], expected)

    def test_conversion_does_not_touch_numbers_already_in_digits(self):
        text, _ = main.normalise_transcript("Verify whether it was 50 or 15.", "digits")

        self.assertEqual(text, "Verify whether it was 50 or 15.")

    def test_an_empty_transcript_stays_empty(self):
        self.assertEqual(main.normalise_transcript("   "), ("", False))

    def test_the_record_carries_the_normalised_text_and_the_hint(self):
        record = main.emit_transcript(
            "and a shortfall,",
            np.zeros(16000, dtype=np.float32),
            time.perf_counter(),
            "call-1",
            consumer_args(),
        )

        self.assertEqual(record["text"], "And a shortfall")
        self.assertTrue(record["continues_previous"])


if __name__ == "__main__":
    unittest.main()
