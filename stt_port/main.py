"""Local always-listening transcription: audio -> ASR -> stdout. No hotkey, no injection.

Two audio sources, selected with `--source`:

- `mic` (default): the hardware microphone, via sounddevice. Capture starts the moment the
  process starts and silence alone closes an utterance -- no way to pause short of Ctrl-C.
- `ws`: a live analog phone call arriving over a WebSocket from the phone machine, which
  owns Asterisk and the handset. Utterances are closed by silence *or* by hangup, and each
  finished transcript is sent back over the same socket as well as printed here.

Both sources push identical `(audio, rate, closed_at, call_id)` tuples onto one queue, so
`consume_chunks` -- the transcribe-and-print half -- is shared verbatim between them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import wave
from queue import Queue

import numpy as np

from . import audio, server
from .backends import build_backend, default_backend, default_device
from .server import DEFAULT_PORT

OFFLINE_ENV_VARS = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
}


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


def emit_transcript(text: str, target: np.ndarray, closed_at: float, call_id, args) -> dict:
    """Print one finished utterance, as plain text or as a JSONL record.

    Returns the record either way, so a caller that also has somewhere to *send* the
    transcript (the phone machine, over the same socket the audio came in on) doesn't have
    to rebuild it or re-measure the latency.
    """
    dur_ms = len(target) / audio.TARGET_SAMPLE_RATE * 1000
    latency_ms = (time.perf_counter() - closed_at) * 1000
    record = {
        "t": round(time.time(), 3),
        "call": call_id,
        "text": text,
        "dur_ms": round(dur_ms, 1),
        "latency_ms": round(latency_ms, 1),
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
    """
    while True:
        item = q.get()
        if item is None:
            return
        chunk, chunk_native_rate, closed_at, call_id = item
        t_dequeued = time.perf_counter()

        if args.debug_save_wav:
            path = save_debug_wav(chunk, chunk_native_rate)
            print(f"[debug] saved {path}", file=sys.stderr)

        t0 = time.perf_counter()
        target = audio.resample_to_target(chunk, chunk_native_rate)
        t_resample = time.perf_counter()

        # Stripped once, here, so every consumer gets the same string: stdout, the JSONL
        # record, and the copy sent back to the phone machine.
        text = backend.transcribe(target).strip()
        t_inference = time.perf_counter()

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
