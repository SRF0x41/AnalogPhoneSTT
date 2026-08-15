"""Replay the captured call clips through the filler gates, and optionally the model.

This is the regression test for the transcript-quality work in docs/ANALOG-TUNING.md. It is
not a unit test and deliberately isn't one: the gates' thresholds are calibrated against 78
real utterances from five live calls, one of which already falsified an earlier version of the
rule, and synthetic audio cannot falsify the next one. `stt_port/test_server.py` covers the
mechanism; this covers the numbers.

    ./.venv/bin/python -m stt_port.replay                 # gates only, no model, ~instant
    ./.venv/bin/python -m stt_port.replay --transcribe    # also decode, and diff the text

`samples/` is gitignored (audio, 1.5MB) but must be kept: without it there is no way to
re-tune these thresholds against a different ATA, and no way to catch a change that quietly
starts eating real speech.

The manifest's `transcript` column is what the *current* pipeline produced, not ground truth.
Ground truth for the scored passages lives in the accuracy tables in the tuning doc.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from . import audio
from .backends import build_backend, default_backend, default_device
from .main import (
    DEFAULT_INFERENCE_TIMEOUT,
    DEFAULT_MIN_SPEECH_FLOOR_MS,
    DEFAULT_MIN_SPEECH_MS,
    DEFAULT_MIN_SPEECH_RMS,
    filler_reason,
    load_wav_mono_f32,
)

DEFAULT_SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"

# Every hallucination in the captured set is one of these -- 22 of 110 utterances across five
# calls, almost all the exact string "Thank you.". Used only to *score* the gates here; the
# gates themselves never look at the text, which is the point (see change B2 in the doc).
FILLER_TEXTS = {"thank you.", "thank you", "thanks.", "you", "bye.", "."}


def is_filler(transcript: str) -> bool:
    return transcript.strip().lower() in FILLER_TEXTS


def load_manifest(samples_dir: Path) -> list[dict]:
    manifest = samples_dir / "MANIFEST.csv"
    if not manifest.exists():
        sys.exit(
            f"no manifest at {manifest}. The clip set is gitignored -- recapture with\n"
            "  ./.venv/bin/python -m stt_port.main --source ws --verbose --debug-save-wav"
        )
    with manifest.open() as f:
        return list(csv.DictReader(f))


def evaluate_gates(rows: list[dict], samples_dir: Path, args: argparse.Namespace) -> list[dict]:
    """Recompute each clip's metrics and ask the gates what they would do with it."""
    out = []
    for row in rows:
        chunk, rate = load_wav_mono_f32(str(samples_dir / row["clip"]))
        profile = audio.speech_profile(chunk, rate)
        final = row["after_call_end"].strip().lower() == "yes"
        out.append(
            {
                **row,
                "chunk": chunk,
                "rate": rate,
                "speech_s": profile.speech_s,
                "mean_rms": profile.mean_rms,
                "longest_run_s": profile.longest_run_s,
                "final": final,
                "dropped": filler_reason(profile, final, args),
                "filler": is_filler(row["transcript"]),
            }
        )
    return out


def report_gates(results: list[dict]) -> None:
    filler = [r for r in results if r["filler"]]
    real = [r for r in results if not r["filler"]]
    caught = [r for r in filler if r["dropped"]]
    missed = [r for r in filler if not r["dropped"]]
    false_positives = [r for r in real if r["dropped"]]

    print(f"{len(results)} clips: {len(filler)} filler, {len(real)} real\n")
    print("suppressed:")
    for r in sorted(results, key=lambda r: r["speech_s"]):
        if not r["dropped"]:
            continue
        tag = "filler" if r["filler"] else "REAL <- false positive"
        print(
            f"  {r['clip']}  speech={r['speech_s']:.2f}s run={r['longest_run_s']:.2f}s "
            f"rms={r['mean_rms']:.4f}  {tag}: {r['transcript'][:40]!r}"
        )
        print(f"      {r['dropped']}")

    if missed:
        print("\nfiller that survived the gates:")
        for r in missed:
            print(
                f"  {r['clip']}  speech={r['speech_s']:.2f}s run={r['longest_run_s']:.2f}s "
                f"rms={r['mean_rms']:.4f}  {r['transcript'][:40]!r}"
            )

    print(
        f"\nfiller suppressed {len(caught)}/{len(filler)}   "
        f"real utterances lost {len(false_positives)}/{len(real)}"
    )
    if false_positives:
        print(
            "The known, accepted false positive is a 0.02s exclamation measured quieter than\n"
            "every filler clip on whole-clip RMS -- it is not separable on these axes. Any\n"
            "*other* real utterance appearing above is a regression."
        )


def report_transcripts(results: list[dict], backend, timeout: float) -> None:
    """Decode each clip the way the pipeline now would, and diff against the manifest."""
    print("\n--- decoding (trimmed, capped temperature ladder) ---")
    changed = slow = 0
    for r in sorted(results, key=lambda r: r["clip"]):
        if r["dropped"]:
            continue
        target = audio.resample_to_target(
            audio.trim_trailing_silence(r["chunk"], r["rate"]), r["rate"]
        )
        t0 = time.perf_counter()
        text = backend.transcribe(target).strip()
        elapsed = (time.perf_counter() - t0) * 1000
        over = timeout > 0 and elapsed > timeout * 1000
        slow += over
        if text != r["transcript"].strip():
            changed += 1
            print(f"  {r['clip']}  {elapsed:7.0f}ms{' OVER CEILING' if over else ''}")
            print(f"      was: {r['transcript'][:70]!r}")
            print(f"      now: {text[:70]!r}")
        elif over:
            print(f"  {r['clip']}  {elapsed:7.0f}ms OVER CEILING (text unchanged)")
    print(f"\n{changed} transcripts changed, {slow} decodes over the {timeout:.1f}s ceiling")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES_DIR)
    p.add_argument("--min-speech-ms", type=float, default=DEFAULT_MIN_SPEECH_MS)
    p.add_argument("--min-speech-rms", type=float, default=DEFAULT_MIN_SPEECH_RMS)
    p.add_argument("--min-speech-floor-ms", type=float, default=DEFAULT_MIN_SPEECH_FLOOR_MS)
    p.add_argument("--inference-timeout", type=float, default=DEFAULT_INFERENCE_TIMEOUT)
    p.add_argument(
        "--transcribe",
        action="store_true",
        help="also run the model over every clip the gates keep, and diff the text against "
        "the manifest (loads the backend; takes a minute or two)",
    )
    p.add_argument("--backend", choices=["parakeet", "whisper", "mlx"], default=default_backend())
    p.add_argument("--model", default=None)
    p.add_argument("--clip", default=None, help="replay only clips whose name contains this")
    args = p.parse_args(argv)

    rows = load_manifest(args.samples)
    if args.clip:
        rows = [r for r in rows if args.clip in r["clip"]]
        if not rows:
            sys.exit(f"no clip matching {args.clip!r}")

    results = evaluate_gates(rows, args.samples, args)
    report_gates(results)

    if args.transcribe:
        backend = build_backend(args.backend, default_device(args.backend), args.model)
        backend.load()
        backend.warmup()
        report_transcripts(results, backend, args.inference_timeout)


if __name__ == "__main__":
    main()
