#!/usr/bin/env python3
"""Replay a WAV file at this machine as if it were a phone call. No phone machine required.

Speaks the same WebSocket contract `phone/session.py` does, so the server cannot tell the
difference -- which makes it the way to check that this half works end to end before, or
without, involving Asterisk and the physical handset:

    ./venv/bin/python -m stt_port.main --source ws --verbose     # terminal 1
    ./venv/bin/python -m stt_port.fake_call speech.wav           # terminal 2

Frames are paced in real time by default, so segmentation, hangover and the idle timeout
all behave as they would on a live call. `--no-pace` sends as fast as the socket accepts
for a quick smoke test, `--drop` deliberately loses frames, and `--no-hangup` omits the
call_end message so the idle timeout has to close the call instead.

Input must be **8kHz** mono 16-bit PCM -- the analog line's rate, which is what the wire
carries. On macOS:

    say "some words" -o /tmp/speech.wav --data-format=LEI16@8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
import uuid
import wave

from .server import BLOCK_SAMPLES, DEFAULT_PORT, WIRE_RATE

FRAME_BYTES = BLOCK_SAMPLES * 2       # 320: one 20ms frame
FRAME_SECONDS = BLOCK_SAMPLES / WIRE_RATE  # 0.02


def read_wav(path: str) -> bytes:
    with wave.open(path, "rb") as f:
        if f.getnchannels() != 1 or f.getsampwidth() != 2 or f.getframerate() != WIRE_RATE:
            raise SystemExit(
                f"{path}: need {WIRE_RATE}Hz mono 16-bit PCM (the wire format), got "
                f"{f.getframerate()}Hz {f.getnchannels()}ch {f.getsampwidth() * 8}-bit"
            )
        return f.readframes(f.getnframes())


def frames(pcm: bytes):
    for start in range(0, len(pcm), FRAME_BYTES):
        chunk = pcm[start : start + FRAME_BYTES]
        if len(chunk) == FRAME_BYTES:
            yield chunk


async def replay(args) -> None:
    from websockets.asyncio.client import connect

    pcm = read_wav(args.wav)
    call_id = str(uuid.uuid4()).split("-")[0]
    total = len(pcm) // FRAME_BYTES
    sent = dropped = 0

    async with connect(args.url) as ws:
        # Print transcripts as the server returns them: this is the same return path the
        # phone machine uses, so exercising it here proves both directions.
        async def show_transcripts() -> None:
            try:
                async for message in ws:
                    if isinstance(message, str):
                        record = json.loads(message)
                        if record.get("type") == "transcript":
                            print(f"[transcript] {record.get('text', '')}", flush=True)
            except Exception:
                pass

        listener = asyncio.create_task(show_transcripts())

        await ws.send(json.dumps(
            {"type": "call_start", "call_id": call_id, "rate": WIRE_RATE, "direction": "inbound"}
        ))
        print(
            f"[fake] call {call_id}: {total} frames "
            f"({total * FRAME_SECONDS:.1f}s) -> {args.url}",
            file=sys.stderr,
        )

        started = time.monotonic()
        for i, frame in enumerate(frames(pcm)):
            if args.drop and random.random() < args.drop:
                dropped += 1
            else:
                await ws.send(frame)
                sent += 1
            if args.pace:
                # Absolute schedule, not a per-frame sleep: sleeping 20ms per frame drifts
                # slower than real time once send latency is counted, and the segmenter's
                # hangover is measured in wall-clock silence.
                await asyncio.sleep(max(0.0, started + (i + 1) * FRAME_SECONDS - time.monotonic()))

        if args.hangup:
            await ws.send(json.dumps({"type": "call_end", "reason": "hangup"}))
        else:
            print("[fake] no call_end sent -- the idle timeout should close it", file=sys.stderr)
            await asyncio.sleep(args.linger)

        print(f"[fake] sent {sent} frames, dropped {dropped}", file=sys.stderr)
        await asyncio.sleep(args.linger)
        listener.cancel()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("wav", help=f"{WIRE_RATE}Hz mono 16-bit PCM wav file")
    p.add_argument("--url", default=f"ws://127.0.0.1:{DEFAULT_PORT}/", help="the stt server")
    p.add_argument("--no-pace", dest="pace", action="store_false", help="send as fast as possible")
    p.add_argument("--drop", type=float, default=0.0, metavar="P", help="drop each frame with probability P")
    p.add_argument("--no-hangup", dest="hangup", action="store_false", help="omit call_end")
    p.add_argument("--linger", type=float, default=5.0, help="seconds to wait for transcripts before exiting")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    try:
        asyncio.run(replay(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
