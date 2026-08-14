"""Continuous mic capture with RMS-energy chunk segmentation.

No hotkey, no arm/disarm state: audio flows into the segmenter from the moment the
stream opens. A chunk opens when block RMS energy crosses `energy_threshold` and closes
after `hangover_ms` of consecutive below-threshold blocks -- silence is the only endpoint.

`Segmenter` is shared with the network source, which needs neither a sound card nor a
resampler (its audio already arrives at the target rate), so `sounddevice` and `scipy` are
imported where they're used rather than at module level. That keeps `--source net` -- and the
whole test suite -- runnable on a machine with no audio stack installed at all.
"""

from __future__ import annotations

import sys
import time
from queue import Queue

import numpy as np

TARGET_SAMPLE_RATE = 16000
BLOCK_MS = 30
PREROLL_MS = 300
HANGOVER_MS = 500
MAX_CHUNK_SECONDS = 30
DEFAULT_ENERGY_THRESHOLD = 0.02


def list_devices() -> None:
    import sounddevice as sd

    print(sd.query_devices())


def default_input_device() -> tuple[int, float]:
    import sounddevice as sd

    idx = sd.default.device[0]
    if idx is None or idx < 0:
        raise RuntimeError("No default input device found. Use --list-devices to see options.")
    info = sd.query_devices(idx)
    return idx, float(info["default_samplerate"])


def resample_to_target(audio: np.ndarray, native_rate: int) -> np.ndarray:
    if native_rate == TARGET_SAMPLE_RATE:
        return audio
    from math import gcd

    from scipy.signal import resample_poly

    g = gcd(native_rate, TARGET_SAMPLE_RATE)
    up, down = TARGET_SAMPLE_RATE // g, native_rate // g
    return resample_poly(audio, up, down).astype(np.float32)


class Segmenter:
    """Feeds fixed-size native-rate blocks in, returns a finished chunk when silence closes it.

    The preroll and chunk buffers are pre-allocated once at construction and reused across
    utterances (cleared by resetting a write index, never reallocated) so segmentation never
    allocates on the audio callback's hot path.
    """

    def __init__(
        self,
        native_rate: int,
        block_size: int,
        energy_threshold: float = DEFAULT_ENERGY_THRESHOLD,
        hangover_ms: float = HANGOVER_MS,
        preroll_ms: float = PREROLL_MS,
        max_chunk_seconds: float = MAX_CHUNK_SECONDS,
    ) -> None:
        self.native_rate = native_rate
        self.block_size = block_size
        self.energy_threshold = energy_threshold
        self.hangover_blocks = max(1, round(hangover_ms / 1000 * native_rate / block_size))
        self.preroll_samples = max(1, round(preroll_ms / 1000 * native_rate))
        self.max_chunk_samples = round(max_chunk_seconds * native_rate)

        self._preroll = np.zeros(self.preroll_samples, dtype=np.float32)
        self._preroll_pos = 0
        # How much of the ring is audio from *this* gap between utterances. Reset on close,
        # so a chunk can never be handed audio that predates the previous chunk.
        self._preroll_filled = 0
        self._chunk = np.zeros(self.max_chunk_samples, dtype=np.float32)
        self._chunk_pos = 0
        self._open = False
        self._silence_run = 0

    def _write_preroll(self, block: np.ndarray) -> None:
        n = len(block)
        if n >= self.preroll_samples:
            self._preroll[:] = block[-self.preroll_samples:]
            self._preroll_pos = 0
            self._preroll_filled = self.preroll_samples
            return
        end = self._preroll_pos + n
        if end <= self.preroll_samples:
            self._preroll[self._preroll_pos:end] = block
        else:
            first = self.preroll_samples - self._preroll_pos
            self._preroll[self._preroll_pos:] = block[:first]
            self._preroll[: end - self.preroll_samples] = block[first:]
        self._preroll_pos = end % self.preroll_samples
        self._preroll_filled = min(self.preroll_samples, self._preroll_filled + n)

    def _preroll_ordered(self) -> np.ndarray:
        """The audio immediately preceding the current block, oldest first.

        Returns only what has actually been written since the last close -- a short gap
        between utterances yields a short preroll rather than being padded out with
        whatever the ring happened to still hold from an earlier part of the call.
        """
        if self._preroll_filled < self.preroll_samples:
            # Not wrapped yet, so `_preroll_pos` is also the fill count: the valid samples
            # are the leading ones, already in order.
            return self._preroll[: self._preroll_filled]
        return np.concatenate([self._preroll[self._preroll_pos :], self._preroll[: self._preroll_pos]])

    def _reset_preroll(self) -> None:
        self._preroll_pos = 0
        self._preroll_filled = 0

    def _append_chunk(self, block: np.ndarray) -> None:
        n = len(block)
        end = min(self._chunk_pos + n, self.max_chunk_samples)
        take = end - self._chunk_pos
        if take > 0:
            self._chunk[self._chunk_pos : end] = block[:take]
        self._chunk_pos = end

    def _close(self) -> np.ndarray:
        finished = self._chunk[: self._chunk_pos].copy()
        self._open = False
        self._chunk_pos = 0
        self._silence_run = 0
        # The ring went unwritten for the whole utterance, so it still holds audio from
        # before that utterance began. Dropping it matters most when a chunk closes on
        # `max_chunk_samples` mid-sentence: the next block is loud, so without this the
        # next chunk would open with up to `preroll_ms` of audio from 30 seconds ago
        # spliced into the middle of continuous speech.
        self._reset_preroll()
        return finished

    def flush(self) -> np.ndarray | None:
        """Close and return an in-progress utterance without waiting for silence.

        Only the phone source needs this: a mic stream just stops, but a hangup can land
        mid-sentence, and without an explicit close that final utterance would be dropped.
        Returns None when no chunk is open. The segmenter stays usable afterwards.
        """
        if not self._open:
            return None
        return self._close()

    def process(self, block: np.ndarray) -> np.ndarray | None:
        rms = float(np.sqrt(np.mean(np.square(block)))) if len(block) else 0.0
        loud = rms >= self.energy_threshold

        if not self._open:
            if not loud:
                self._write_preroll(block)
                return None
            # The opening block is appended below, so it must *not* also go through the
            # preroll first -- doing both put a duplicate copy of it at the head of every
            # utterance. The preroll holds strictly the audio preceding this block.
            self._open = True
            self._silence_run = 0
            pre = self._preroll_ordered()
            self._chunk[: len(pre)] = pre
            self._chunk_pos = len(pre)
            self._append_chunk(block)
            return None

        self._append_chunk(block)
        self._silence_run = 0 if loud else self._silence_run + 1

        if self._silence_run >= self.hangover_blocks or self._chunk_pos >= self.max_chunk_samples:
            return self._close()
        return None


def open_stream(
    device: int | None,
    out_queue: Queue,
    energy_threshold: float = DEFAULT_ENERGY_THRESHOLD,
    hangover_ms: float = HANGOVER_MS,
) -> tuple["sounddevice.InputStream", int]:  # noqa: F821 - imported lazily inside
    """Build a configured (not yet started) InputStream. Use as a context manager.

    Finished chunks are pushed to `out_queue` as (audio: np.ndarray[float32] @ native_rate,
    native_rate: int, closed_at: float, call_id: None) from the audio callback thread. The
    trailing None is what the phone source uses to tag which call a chunk came from; the mic
    has no such notion, but both sources share one queue shape so `consume_chunks` doesn't
    have to care which one is feeding it.
    """
    import sounddevice as sd

    if device is None:
        device, native_rate_f = default_input_device()
    else:
        native_rate_f = float(sd.query_devices(device)["default_samplerate"])
    native_rate = int(round(native_rate_f))
    block_size = max(1, round(BLOCK_MS / 1000 * native_rate))

    segmenter = Segmenter(native_rate, block_size, energy_threshold, hangover_ms)

    def callback(indata, frames, time_info, status) -> None:
        if status:
            print(f"[audio] {status}", file=sys.stderr)
        finished = segmenter.process(indata[:, 0])
        if finished is not None:
            out_queue.put((finished, native_rate, time.perf_counter(), None))

    stream = sd.InputStream(
        device=device,
        channels=1,
        samplerate=native_rate,
        blocksize=block_size,
        dtype="float32",
        callback=callback,
    )
    return stream, native_rate
