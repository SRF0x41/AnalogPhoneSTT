"""Local always-listening transcription: audio -> ASR -> stdout. No hotkey, no injection.

Two audio sources, selected with `--source`:

- `mic` (default): the hardware microphone, via sounddevice. Capture starts the moment the
  process starts and silence alone closes an utterance -- no way to pause short of Ctrl-C.
- `ws`: a live analog phone call arriving over a WebSocket from the phone machine, which
  owns Asterisk and the handset. Utterances are closed by silence *or* by hangup, and each
  finished transcript is sent back over the same socket as well as printed here.

Both sources push identical `(audio, rate, closed_at, call_id, final)` tuples onto one queue,
so `consume_chunks` -- the transcribe-and-print half -- is shared verbatim between them.

`consume_chunks` also carries the transcript-quality work from docs/ANALOG-TUNING.md: two
gates that drop hallucinated filler before it reaches the model, a watchdog on runaway
decodes, and the formatting normalisation that makes consecutive utterances agree.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from queue import Queue

import numpy as np

from . import audio, server
from .backends import build_backend, default_backend, default_device
from .server import DEFAULT_PORT

OFFLINE_ENV_VARS = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
}

# --- suppressing hallucinated filler (docs/ANALOG-TUNING.md, change B) --------------------
#
# Whisper does not return empty for input containing no speech. It returns the phrase it saw
# most often over near-silence in training, confidently: 22 of 110 utterances across five
# measured calls were pure invention, and almost every one was the exact string "Thank you.".
# Two gates, because the sources differ and no single rule catches both.

# B1, for the near-silence source (breath, a faint line transient, a mid-sentence pause the
# segmenter opened a chunk on). Both conditions must hold -- see `filler_reason`.
#
# 300, not the 250 originally fitted: a live call produced a hallucinated `Thank you.` from a
# chunk holding 0.28s of speech at 0.0153 RMS, which escaped the 250 boundary by 30ms while
# being an order of magnitude quieter than any real speech. The same call then produced a
# *genuine* `Thank you.` at 0.72s and 0.0994 RMS -- 2.6x longer and 6.5x louder -- so the
# widened window still clears real speech comfortably. The shortest real one-word answer in
# the corpus is 0.36s, and every one of them is loud, so the conjunction protects them twice.
DEFAULT_MIN_SPEECH_MS = 300.0
DEFAULT_MIN_SPEECH_RMS = 0.025

# An unconditional floor on *contiguous* speech, added after two live calls defeated B2 (see
# `filler_reason`). Measured over 78 captured clips plus five live calls: every cradle click
# runs 0.02-0.12s unbroken, every real utterance 0.14s or longer. Energy is not consulted --
# the clicks that provoked this measured 0.1225 and 0.2372 RMS at peak 0.98, louder than most
# genuine speech.
DEFAULT_MIN_SPEECH_FLOOR_MS = 130.0

# B2, for the terminal hook click: the handset hitting its cradle, flushed by hangup. Every
# measured call ended with one, transcribed as "Thank you.". It is louder and longer than the
# shortest genuine one-word answer, so it is identified structurally rather than acoustically.
# A caller still speaking at hangup produces far more than this.
B2_MAX_SPEECH_SECONDS = 0.30

# A2: the wall-clock ceiling on one decode, comfortably above the measured 1365ms p90.
DEFAULT_INFERENCE_TIMEOUT = 4.0


def save_debug_wav(chunk: np.ndarray, native_rate: int) -> str:
    path = f"/tmp/dictate_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.wav"
    pcm16 = np.clip(chunk, -1.0, 1.0)
    pcm16 = (pcm16 * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(native_rate)
        f.writeframes(pcm16.tobytes())
    return path


def load_wav_mono_f32(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as f:
        rate = f.getframerate()
        n = f.getnframes()
        raw = f.readframes(n)
        width = f.getsampwidth()
        channels = f.getnchannels()
    if width != 2:
        raise ValueError(f"{path}: only 16-bit PCM wav is supported, got {width * 8}-bit")
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def run_benchmark(backend, wav_path: str, iterations: int = 20) -> None:
    chunk, native_rate = load_wav_mono_f32(wav_path)
    target = audio.resample_to_target(chunk, native_rate)
    latencies = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        backend.transcribe(target)
        latencies.append((time.perf_counter() - t0) * 1000)
    latencies.sort()
    p50 = statistics.median(latencies)
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
    print(f"iterations={iterations} p50={p50:.1f}ms p95={p95:.1f}ms min={latencies[0]:.1f}ms max={latencies[-1]:.1f}ms")


def filler_reason(p: audio.SpeechProfile, final: bool, args: argparse.Namespace) -> str | None:
    """Why this chunk should be dropped before inference, or None to transcribe it.

    B2 first, because it is the precise rule: a chunk hangup flushed, holding almost no
    speech, is the cradle click. It can only ever fire once per call, at the moment the line
    is being torn down.

    B2 is necessary but *not sufficient*, which two live calls proved after the fact. It
    assumes the click is the chunk hangup flushed, and that only holds when `call_end` arrives
    within the hangover window. Hang up a couple of seconds before the socket closes and the
    click closes on silence like any other chunk, arrives with `final=False`, and is
    transcribed as `Thank you.` -- the exact line B2 exists to remove. All five originally
    measured calls happened to hang up promptly, so the rule looked airtight; it was resting
    on timing nobody had varied.

    Hence the floor, and hence it measures *contiguous* speech. The first defeating click held
    0.06s of speech, which a total-duration floor catches. The second held 0.20s at 0.1225 RMS
    -- too long for that floor, far too loud for B1's energy half -- but its loud frames were
    three isolated taps, longest run 0.12s. That is the axis where a settling handset and a
    short word genuinely differ: a word is continuous. Across 78 clips and five calls, clicks
    run 0.02-0.12s and real utterances 0.14s or more.

    The margin there is thin -- 0.13s sits 10ms above the shortest real run in the corpus (the
    word "fourteen") and 10ms below the longest click. Live calls put real utterances at 0.20s
    and up, so the practical margin is wider, but this is the number to re-measure first on
    different hardware.

    B1 second, and it needs *both* conditions. A duration-only gate was the original proposal
    and the captured audio falsified it: a genuine short exclamation measured 0.02s of speech
    -- a plosive-heavy word barely registers on a 20ms-frame energy metric -- and survives
    only because its energy clears the floor. Real one-word answers measured 0.36s and up at
    0.078-0.167 RMS, so the conjunction leaves them roughly 8x clear on the axis that matters.

    Both thresholds are flags because they are calibrated to one ATA on one line. Setting
    either to 0 disables B1: no chunk has negative duration or negative energy.
    """
    if final and p.speech_s < B2_MAX_SPEECH_SECONDS:
        return f"hangup click ({p.speech_s:.2f}s speech < {B2_MAX_SPEECH_SECONDS:.2f}s, flushed by hangup)"
    if p.longest_run_s * 1000 < args.min_speech_floor_ms:
        return (
            f"no continuous speech (longest run {p.longest_run_s:.2f}s < "
            f"{args.min_speech_floor_ms:.0f}ms, at any volume)"
        )
    if p.speech_s * 1000 < args.min_speech_ms and p.mean_rms < args.min_speech_rms:
        return (
            f"near-silence ({p.speech_s:.2f}s speech < {args.min_speech_ms:.0f}ms "
            f"and rms {p.mean_rms:.4f} < {args.min_speech_rms})"
        )
    return None


class InferenceWatchdog:
    """Runs one decode at a time and gives up on it after `timeout_s` (A2).

    Be precise about what this buys, because it is less than it looks. A decode cannot be
    cancelled once started -- MLX offers no abort -- so the abandoned one keeps running and
    the next one queues behind it on this single worker. What the ceiling actually guarantees
    is that the *pipeline* stops waiting: the garbage never reaches the transcript, the
    hangup drain path is never held by an unbounded decode, and an overrun is visible in the
    log instead of appearing as a system that has silently died. Bounding the wall time
    itself is the job of the shortened temperature ladder (A1) and the trimmed silent tails
    (C), which together took the one captured pathological clip from 22s to ~1.3s.

    Deliberately one worker: two concurrent decodes would contend for the same GPU and the
    same model object, which is neither faster nor known to be safe.

    `timeout_s <= 0` disables the watchdog and transcribes inline, with no thread at all.
    """

    def __init__(self, timeout_s: float = DEFAULT_INFERENCE_TIMEOUT) -> None:
        self._timeout = timeout_s
        self._pool = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr") if timeout_s > 0 else None
        )

    def transcribe(self, backend, target: np.ndarray) -> str | None:
        """The transcript, or None if the decode blew through the ceiling and was abandoned."""
        if self._pool is None:
            return backend.transcribe(target)
        started = time.perf_counter()
        future = self._pool.submit(backend.transcribe, target)
        try:
            return future.result(timeout=self._timeout)
        except FutureTimeout:
            # Still running, and still holding the worker. Log it when it finally lands, so
            # the cost of the overrun is on the record rather than inferred from the gap.
            future.add_done_callback(lambda f: self._log_late(f, started))
            return None

    def _log_late(self, future, started: float) -> None:
        elapsed = (time.perf_counter() - started) * 1000
        try:
            text = future.result()
        except Exception as exc:  # noqa: BLE001 - the decode failed; it was abandoned anyway
            print(f"[watchdog] abandoned decode failed after {elapsed:.0f}ms: {exc!r}", file=sys.stderr)
            return
        print(f"[watchdog] abandoned decode finished after {elapsed:.0f}ms: {text[:60]!r}", file=sys.stderr)

    def close(self) -> None:
        if self._pool is not None:
            # Don't join: a runaway decode would hold shutdown for as long as it likes, and
            # the process is on its way out regardless.
            self._pool.shutdown(wait=False)


# --- normalising what comes back (change D) ----------------------------------------------

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}
_COMPOUND_RE = re.compile(
    rf"\b({'|'.join(_TENS)})[\s-]({'|'.join(k for k, v in _UNITS.items() if 1 <= v <= 9)})\b",
    re.IGNORECASE,
)
_HUNDRED_RE = re.compile(
    rf"\b({'|'.join(k for k, v in _UNITS.items() if 1 <= v <= 9)})\s+hundred\b", re.IGNORECASE
)
_SINGLE_RE = re.compile(rf"\b({'|'.join(list(_UNITS) + list(_TENS))})\b", re.IGNORECASE)


def words_to_digits(text: str) -> str:
    """Spelled-out cardinals to digits, up to 99 plus `N hundred`. Deliberately bounded.

    Compounds first, then hundreds, then bare words -- otherwise "four hundred" is half
    converted to "4 hundred". Anything larger or more compositional ("one hundred and five")
    is left partly in words on purpose: a half-correct number parser is worse than none, and
    the measured calls contain nothing beyond this range.
    """
    text = _COMPOUND_RE.sub(lambda m: str(_TENS[m[1].lower()] + _UNITS[m[2].lower()]), text)
    text = _HUNDRED_RE.sub(lambda m: str(_UNITS[m[1].lower()] * 100), text)
    return _SINGLE_RE.sub(lambda m: str((_UNITS | _TENS)[m[1].lower()]), text)


def normalise_transcript(text: str, numerals: str = "asis") -> tuple[str, bool]:
    """One utterance, rendered consistently. Returns `(text, continuation)`.

    Each utterance is a separate decode with `condition_on_previous_text=False`, so nothing
    carries casing, numeral style or punctuation across chunk boundaries: counting to twenty
    produced `One.` `two.` `3` `four` `5,` `six` `Seven.` -- the same token five ways. That is
    cosmetic for a human and material for anything parsing the transcript.

    `continuation` is true when the model rendered the first word in lowercase, which is its
    own signal that this fragment continues the previous utterance rather than starting a
    sentence -- exactly what happens when a mid-sentence pause outlasts the hangover and
    splits one sentence into three. Capitalising here would destroy that signal, so it is
    returned instead, for a downstream consumer that wants to stitch fragments back together.

    Numeral conversion is opt-in. It is right for a parser and wrong for a reader: it would
    render "five dozen liquor jugs" as "5 dozen liquor jugs".
    """
    text = text.strip()
    if not text:
        return "", False
    continuation = text[0].isalpha() and text[0].islower()
    text = re.sub(r"[,\s]+$", "", text)
    if numerals == "digits":
        text = words_to_digits(text)
    if text and text[0].isalpha():
        text = text[0].upper() + text[1:]
    return text, continuation


def emit_transcript(text: str, target: np.ndarray, closed_at: float, call_id, args) -> dict:
    """Print one finished utterance, as plain text or as a JSONL record.

    Returns the record either way, so a caller that also has somewhere to *send* the
    transcript (the phone machine, over the same socket the audio came in on) doesn't have
    to rebuild it or re-measure the latency.

    Normalisation happens here, once, for the same reason the strip does: stdout, the JSONL
    record and the copy sent back to the phone machine must not disagree about the text.
    """
    text, continues_previous = normalise_transcript(text, args.numerals)
    dur_ms = len(target) / audio.TARGET_SAMPLE_RATE * 1000
    latency_ms = (time.perf_counter() - closed_at) * 1000
    record = {
        "t": round(time.time(), 3),
        "call": call_id,
        "text": text,
        "dur_ms": round(dur_ms, 1),
        "latency_ms": round(latency_ms, 1),
        # The model's own casing said this fragment continues the previous utterance. See
        # `normalise_transcript`; a consumer that wants whole sentences stitches on this.
        "continues_previous": continues_previous,
    }

    if args.jsonl:
        print(json.dumps(record, ensure_ascii=False), flush=True)
    elif call_id is not None:
        print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)
    else:
        print(text, flush=True)
    return record


def consume_chunks(backend, q: Queue, args: argparse.Namespace, sink=None) -> None:
    """Transcribe and emit closed utterances until the source signals it is finished.

    Source-agnostic on purpose: the mic stream and WebSocketSource push the same tuples
    onto the same queue, and only the phone source ever pushes the `None` end-of-source
    sentinel (the mic runs until Ctrl-C).

    `sink(call_id, record)` is an optional second destination for each transcript, used by
    the `ws` source to return it to the phone machine. It runs on this thread, so it must
    not block -- `WebSocketSource.emit` only schedules the send.

    The sink is called for every chunk, with `record=None` for one that transcribed to
    nothing. That is not a transcript and nothing is sent for it, but the sink is how the
    `ws` source learns a chunk is finished with, and a call whose last utterance was line
    noise would otherwise hold its socket open waiting for a transcript that never comes.

    That contract is also why the filler gates live *here*, after the dequeue, rather than at
    the obvious place -- before the chunk is ever queued. The drain path waits on an event
    only the sink sets, so a chunk filtered out upstream would never reach the sink and every
    call ending on a cradle click (which is every call) would hang its socket until the drain
    timeout. Gating here skips only `backend.transcribe`, which is the whole cost anyway, and
    reuses the existing empty-transcript path that already reports the chunk as finished.
    """
    watchdog = InferenceWatchdog(args.inference_timeout)
    try:
        while True:
            item = q.get()
            if item is None:
                return
            chunk, chunk_native_rate, closed_at, call_id, final = item
            t_dequeued = time.perf_counter()

            # Saved untrimmed and ungated, so the debug set stays comparable with the clips
            # the thresholds below were calibrated against.
            if args.debug_save_wav:
                path = save_debug_wav(chunk, chunk_native_rate)
                print(f"[debug] saved {path}", file=sys.stderr)

            t0 = time.perf_counter()
            dropped = filler_reason(audio.speech_profile(chunk, chunk_native_rate), final, args)
            if dropped is not None:
                # Always logged, never silent, and not behind --verbose: a caller who really
                # did say only "thank you" and watched it vanish has no way to tell a
                # suppression rule from a broken system. This line is that difference.
                print(f"[gate] dropped chunk before inference -- {dropped}", file=sys.stderr)
                if sink is not None:
                    sink(call_id, None)
                continue

            target = audio.resample_to_target(
                audio.trim_trailing_silence(chunk, chunk_native_rate), chunk_native_rate
            )
            t_resample = time.perf_counter()

            text = watchdog.transcribe(backend, target)
            t_inference = time.perf_counter()
            if text is None:
                print(
                    f"[watchdog] gave up on a {len(target) / audio.TARGET_SAMPLE_RATE:.2f}s "
                    f"utterance after {args.inference_timeout:.1f}s -- dropping it rather than "
                    "delaying the ones behind it",
                    file=sys.stderr,
                )
                text = ""
            # Stripped once, here, so every consumer gets the same string: stdout, the JSONL
            # record, and the copy sent back to the phone machine.
            text = text.strip()

            # Non-speech that crossed the energy threshold (line noise, a door, a cough)
            # transcribes to nothing. Emitting a blank line for it is just noise of another
            # kind -- especially on a phone line, where the noise floor guarantees some.
            record = None
            if text:
                record = emit_transcript(text, target, closed_at, call_id, args)
            elif args.verbose:
                print("  [skipped: empty transcript]", file=sys.stderr)
            if sink is not None:
                sink(call_id, record)
            if record is None:
                continue
            t_print = time.perf_counter()

            if args.verbose:
                segment_ms = (t_dequeued - closed_at) * 1000
                resample_ms = (t_resample - t0) * 1000
                inference_ms = (t_inference - t_resample) * 1000
                print_ms = (t_print - t_inference) * 1000
                total_ms = (t_print - t0) * 1000
                print(
                    f"  segment={segment_ms:.1f}ms resample={resample_ms:.1f}ms "
                    f"inference={inference_ms:.1f}ms print={print_ms:.1f}ms total={total_ms:.1f}ms",
                    file=sys.stderr,
                )
    finally:
        watchdog.close()


def print_hangover_note(args: argparse.Namespace) -> None:
    if args.verbose:
        print(
            f"[note] each utterance has an inherent ~{args.hangover_ms:.0f}ms endpoint-detection "
            "lag baked in before processing even starts (silence must persist that long before "
            "a chunk is considered closed)",
            file=sys.stderr,
        )


def run_listen_loop(backend, args: argparse.Namespace) -> None:
    """Mic source: open the input stream, then hand off to the shared consumer."""
    q: Queue = Queue()
    stream, native_rate = audio.open_stream(
        args.device_index, q, energy_threshold=args.energy_threshold, hangover_ms=args.hangover_ms
    )
    print(
        f"listening at {native_rate} Hz (energy_threshold={args.energy_threshold}, "
        f"hangover={args.hangover_ms:.0f}ms) -- Ctrl-C to stop",
        file=sys.stderr,
    )
    print_hangover_note(args)

    with stream:
        consume_chunks(backend, q, args)


async def _run_ws_loop(backend, args: argparse.Namespace) -> None:
    q: Queue = Queue()
    source = server.WebSocketSource(
        q,
        host=args.listen_host,
        port=args.listen_port,
        energy_threshold=args.energy_threshold,
        hangover_ms=args.hangover_ms,
        meter=args.meter,
        idle_timeout=args.idle_timeout,
        verbose=args.verbose,
        # Only when something is actually transcribing: --meter drains no queue, so a call
        # would wait out the whole timeout on every hangup for a transcript that isn't coming.
        final_drain_timeout=0.0 if args.meter else server.FINAL_DRAIN_TIMEOUT,
    )
    async with source:
        if args.meter:
            print("[meter] calibration mode -- no transcription, Ctrl-C to stop", file=sys.stderr)
        else:
            print(
                f"listening on ws://{args.listen_host}:{source.bound_port}/ "
                f"(energy_threshold={args.energy_threshold}, hangover={args.hangover_ms:.0f}ms) "
                "-- start `python -m phone` on the phone machine; Ctrl-C to stop",
                file=sys.stderr,
            )
        print_hangover_note(args)
        # Transcription blocks for as long as inference takes, so it runs on a worker
        # thread: the event loop has to stay free to keep draining audio off the socket
        # while the GPU works. `emit` hands transcripts back across that boundary.
        await asyncio.to_thread(consume_chunks, backend, q, args, source.emit)


def run_ws_loop(backend, args: argparse.Namespace) -> None:
    """WebSocket source: serve the phone machine, then hand off to the shared consumer."""
    asyncio.run(_run_ws_loop(backend, args))


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Local always-listening transcription (prints to stdout).")
    p.add_argument(
        "--source",
        choices=["mic", "ws"],
        default="mic",
        help="mic (default) = hardware microphone; ws = call audio over a WebSocket from "
        "the phone machine",
    )
    p.add_argument(
        "--backend",
        choices=["parakeet", "whisper", "mlx"],
        default=default_backend(),
        help=f"default on this machine: {default_backend()} "
        "-- see README for why parakeet (NeMo) isn't the default anywhere",
    )
    p.add_argument(
        "--device",
        default=None,
        help="cuda:N or cpu for the whisper/parakeet backends; ignored by mlx, which always "
        "uses the Apple Silicon GPU (default: chosen from the backend and platform)",
    )
    p.add_argument(
        "--model",
        default=None,
        help="override the backend's model (mlx: an mlx-community HF repo; whisper: a "
        "faster-whisper model name). Smaller models trade accuracy for latency -- see README",
    )
    p.add_argument("--input-device", dest="device_index", type=int, default=None, help="sounddevice input device index")
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--jsonl", action="store_true", help="emit one JSON record per utterance instead of plain text")
    p.add_argument("--debug-save-wav", action="store_true")
    p.add_argument("--benchmark", metavar="FILE.WAV", default=None)
    p.add_argument(
        "--energy-threshold",
        type=float,
        default=None,
        help=f"default: {audio.DEFAULT_ENERGY_THRESHOLD} for mic, "
        f"{server.DEFAULT_PHONE_ENERGY_THRESHOLD} for ws (tune it with --meter)",
    )
    p.add_argument("--hangover-ms", type=float, default=audio.HANGOVER_MS)

    t = p.add_argument_group(
        "transcript quality",
        "Calibrated against one HT801 on one analog line (docs/ANALOG-TUNING.md). Re-measure "
        "before trusting the defaults on different hardware.",
    )
    t.add_argument(
        "--min-speech-ms",
        type=float,
        default=DEFAULT_MIN_SPEECH_MS,
        help="drop a chunk before inference if it holds less speech than this AND is quieter "
        f"than --min-speech-rms (default: {DEFAULT_MIN_SPEECH_MS:.0f}; 0 disables the gate)",
    )
    t.add_argument(
        "--min-speech-rms",
        type=float,
        default=DEFAULT_MIN_SPEECH_RMS,
        help=f"the energy half of that gate (default: {DEFAULT_MIN_SPEECH_RMS}; 0 disables it). "
        "Measured over speech frames only, so a value at or below "
        f"{audio.SPEECH_FRAME_FLOOR} can never fire",
    )
    t.add_argument(
        "--min-speech-floor-ms",
        type=float,
        default=DEFAULT_MIN_SPEECH_FLOOR_MS,
        help="drop a chunk whose longest *continuous* run of speech is shorter than this, "
        "whatever its energy -- a settling handset is loud but comes in isolated taps "
        f"(default: {DEFAULT_MIN_SPEECH_FLOOR_MS:.0f}; 0 disables)",
    )
    t.add_argument(
        "--inference-timeout",
        type=float,
        default=DEFAULT_INFERENCE_TIMEOUT,
        help="abandon a decode that exceeds this many seconds, rather than let it hold the "
        f"worker and delay the utterances behind it (default: {DEFAULT_INFERENCE_TIMEOUT}; "
        "0 disables)",
    )
    t.add_argument(
        "--numerals",
        choices=["asis", "digits"],
        default="asis",
        help="asis (default) leaves the model's mixed rendering alone; digits converts "
        "spelled-out numbers up to 99 (and 'N hundred') for a parsing consumer, at the cost "
        "of turning 'five dozen' into '5 dozen'",
    )

    g = p.add_argument_group("phone source (--source ws)")
    g.add_argument(
        "--meter",
        action="store_true",
        help="print per-block RMS instead of transcribing, to calibrate --energy-threshold "
        "against a real line (no model is loaded)",
    )
    g.add_argument(
        "--listen-host",
        default="0.0.0.0",
        help="address to bind for incoming call audio (default: all interfaces)",
    )
    g.add_argument("--listen-port", type=int, default=DEFAULT_PORT, help=f"default: {DEFAULT_PORT}")
    g.add_argument(
        "--idle-timeout",
        type=float,
        default=server.DEFAULT_IDLE_TIMEOUT,
        help="flush an open utterance after this many seconds without audio, in case a call "
        f"stalls without closing; 0 disables (default: {server.DEFAULT_IDLE_TIMEOUT})",
    )
    return p


def maybe_force_offline() -> None:
    """Set DICTATE_OFFLINE=1 to force huggingface_hub/transformers into offline mode.
    With the model already cached from a prior run, backend.load() should still succeed;
    if it raises instead, that's the verifiable proof this run made no network call."""
    import os

    if os.environ.get("DICTATE_OFFLINE") == "1":
        for key, val in OFFLINE_ENV_VARS.items():
            os.environ[key] = val
        print("[startup] DICTATE_OFFLINE=1 -- forcing offline mode, no network calls allowed", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.list_devices:
        audio.list_devices()
        return

    if args.source != "ws" and args.meter:
        parser.error("--meter only applies to --source ws")
    if args.meter and args.benchmark:
        parser.error("--meter loads no model, so it can't run --benchmark")

    if args.energy_threshold is None:
        args.energy_threshold = (
            server.DEFAULT_PHONE_ENERGY_THRESHOLD
            if args.source == "ws"
            else audio.DEFAULT_ENERGY_THRESHOLD
        )
    if args.device is None:
        args.device = default_device(args.backend)

    maybe_force_offline()

    # --meter never transcribes, so don't spend ~2s and a few GB of VRAM loading a model.
    backend = None
    if not args.meter:
        backend = build_backend(args.backend, args.device, args.model)

        t0 = time.perf_counter()
        backend.load()
        t_load = time.perf_counter()
        model = getattr(backend, "model_repo", None) or getattr(backend, "model_name", "")
        print(
            f"[startup] backend={args.backend} device={args.device} model={model} "
            f"load={(t_load - t0) * 1000:.0f}ms",
            file=sys.stderr,
        )

        backend.warmup()
        t_warm = time.perf_counter()
        print(f"[startup] warmup={(t_warm - t_load) * 1000:.0f}ms -- model resident and ready", file=sys.stderr)

    if args.benchmark:
        run_benchmark(backend, args.benchmark)
        return

    try:
        if args.source == "ws":
            run_ws_loop(backend, args)
        else:
            run_listen_loop(backend, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
