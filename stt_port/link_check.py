#!/usr/bin/env python3
"""Prove the phone machine can reach the stt machine, and print every host:port involved.

This exists because the audio takes *two* hops with different protocols, and the failure
modes look identical from the handset -- silence either way:

    HT801 handset          Linux phone box                     Mac stt box
    192.168.50.110  --1->  192.168.50.1        --2->           192.168.50.120
                    SIP    Asterisk                  WebSocket
                    :5060    |  ^                    :9099
                             |  | AudioSocket (TCP)
                             v  |  127.0.0.1:9092
                           python -m phone

    hop 1  HT801 <-> Asterisk .............. SIP/RTP, configured in pjsip.conf
    hop 2  Asterisk <-> python -m phone .... AudioSocket, 127.0.0.1:9092
    hop 3  python -m phone <-> this machine  WebSocket, 192.168.50.120:9099

**Asterisk never connects to the Mac.** It only ever reaches `phone/config.py`'s
AUDIOSOCKET_HOST:PORT on its own loopback; the `phone` process is what opens hop 3. So the
HT801's SIP address (`192.168.50.110:5060`) has no business in any stt-side setting, and
`--echo` working proves hops 1 and 2 while saying nothing at all about hop 3.

This script tests **hop 3 only**, in both directions, with no model loaded and no WAV file.

    # on the Mac -- a bare receiver that prints what lands, loading no model
    ./venv/bin/python -m stt_port.link_check --serve

    # on the Linux phone box -- dial the Mac and send synthetic audio
    ./venv/bin/python -m stt_port.link_check --url ws://192.168.50.120:9099/

Point the client at `--serve` to test the wire alone, or at the real
`stt_port.main --source ws` to drive the actual transcriber. The client speaks the same
contract as `fake_call.py`, so the real server cannot tell the difference -- but unlike
`fake_call` it synthesises its own tone, so there is no WAV to find first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import socket
import struct
import sys
import time
import uuid

from .server import BLOCK_SAMPLES, DEFAULT_PORT, WIRE_RATE

FRAME_BYTES = BLOCK_SAMPLES * 2            # 320: one 20ms frame on the wire
FRAME_SECONDS = BLOCK_SAMPLES / WIRE_RATE  # 0.02

# Loud enough to clear DEFAULT_PHONE_ENERGY_THRESHOLD (0.01), so a real server segments it
# into an utterance and answers with a transcript. The words will be nonsense -- it is a
# sine wave -- but a transcript coming back is what proves the return path.
TONE_HZ = 440.0
TONE_AMPLITUDE = 0.3


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def tone_frames(seconds: float, silence_seconds: float):
    """Yield 20ms PCM16 frames: a tone, then silence to close the utterance.

    The trailing silence matters -- the segmenter closes an utterance on hangover, so a
    clip that ends while still loud is only flushed later by the idle timeout.
    """
    total = int((seconds + silence_seconds) / FRAME_SECONDS)
    voiced = int(seconds / FRAME_SECONDS)
    phase = 0.0
    step = 2 * math.pi * TONE_HZ / WIRE_RATE
    for index in range(total):
        samples = []
        for _ in range(BLOCK_SAMPLES):
            value = math.sin(phase) * TONE_AMPLITUDE if index < voiced else 0.0
            phase += step
            samples.append(int(value * 32767))
        yield struct.pack(f"<{BLOCK_SAMPLES}h", *samples)


def tcp_preflight(host: str, port: int, timeout: float) -> bool:
    """Plain TCP connect before any WebSocket, because the two fail very differently.

    Refused (instant RST) means the box is up and nothing holds the port -- the server is
    not running. Timed out means the packets vanished, which is a firewall, not a crash.
    Telling these apart from the WebSocket error alone is guesswork.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        log(f"  DNS: cannot resolve {host!r}: {exc}")
        return False

    family, socktype, proto, _canon, sockaddr = infos[0]
    log(f"  resolved {host}:{port} -> {sockaddr[0]}:{sockaddr[1]}")

    sock = socket.socket(family, socktype, proto)
    sock.settimeout(timeout)
    started = time.monotonic()
    try:
        sock.connect(sockaddr)
    except socket.timeout:
        log(f"  TCP: no answer in {timeout:.0f}s -- FILTERED. Packets are being dropped,")
        log("       which is a firewall on the far side, not a stopped server.")
        return False
    except ConnectionRefusedError:
        log(f"  TCP: refused in {(time.monotonic() - started) * 1000:.0f}ms -- REACHABLE but")
        log(f"       nothing is listening on {port}. Start the server over there.")
        return False
    except OSError as exc:
        log(f"  TCP: {exc}")
        return False
    else:
        local = sock.getsockname()
        peer = sock.getpeername()
        log(f"  TCP: open in {(time.monotonic() - started) * 1000:.0f}ms  "
            f"{local[0]}:{local[1]} -> {peer[0]}:{peer[1]}")
        return True
    finally:
        sock.close()


async def probe(args) -> int:
    from websockets.asyncio.client import connect

    host, port = split_url(args.url)
    log(f"[probe] target {args.url}")
    if not tcp_preflight(host, port, args.timeout):
        return 1

    call_id = str(uuid.uuid4())
    received = {"text": 0, "binary": 0, "bytes": 0}

    try:
        async with connect(args.url, open_timeout=args.timeout) as ws:
            log(f"  WS:  handshake ok  {fmt_addr(ws.local_address)} -> "
                f"{fmt_addr(ws.remote_address)}")

            async def reader() -> None:
                async for message in ws:
                    if isinstance(message, str):
                        received["text"] += 1
                        received["bytes"] += len(message.encode())
                        log(f"  <-   text {len(message)}B  {message[:200]}")
                    else:
                        received["binary"] += 1
                        received["bytes"] += len(message)
                        log(f"  <-   binary {len(message)}B  first16={message[:16].hex(' ')}")

            listener = asyncio.create_task(reader())

            if args.no_audio:
                log("  --no-audio: connected only, sending nothing")
            else:
                await ws.send(json.dumps({
                    "type": "call_start",
                    "call_id": call_id,
                    "rate": WIRE_RATE,
                    "direction": "inbound",
                }))
                log(f"  ->   call_start call_id={call_id}")

                sent = 0
                for frame in tone_frames(args.seconds, args.silence):
                    await ws.send(frame)
                    sent += 1
                    if args.pace:
                        await asyncio.sleep(FRAME_SECONDS)
                log(f"  ->   {sent} audio frames, {sent * FRAME_BYTES}B "
                    f"({sent * FRAME_SECONDS:.1f}s of {WIRE_RATE}Hz PCM16)")

                await ws.send(json.dumps({"type": "call_end", "reason": "link_check"}))
                log("  ->   call_end")

            await asyncio.sleep(args.linger)
            listener.cancel()
    except asyncio.TimeoutError:
        log(f"  WS:  handshake timed out after {args.timeout:.0f}s")
        return 1
    except OSError as exc:
        log(f"  WS:  {exc}")
        return 1

    log(f"[probe] received {received['text']} text + {received['binary']} binary "
        f"messages, {received['bytes']}B total")
    if not args.no_audio and received["text"] == 0:
        log("[probe] link works, but nothing came back. With --serve on the far side that")
        log("        is expected; against stt_port.main it means the model never answered.")
    return 0


async def serve(args) -> int:
    from websockets.asyncio.server import serve as ws_serve

    async def handler(websocket) -> None:
        peer = fmt_addr(websocket.remote_address)
        log(f"[serve] open  {peer} -> {fmt_addr(websocket.local_address)}")
        frames = audio_bytes = 0
        try:
            async for message in websocket:
                if isinstance(message, str):
                    log(f"[serve] {peer} text {len(message)}B  {message[:200]}")
                    # Answer control messages so the client proves the return path too.
                    record = json.loads(message) if message.startswith("{") else {}
                    if record.get("type") == "call_start":
                        await websocket.send(json.dumps({
                            "type": "transcript",
                            "call": record.get("call_id", "?"),
                            "text": "[link_check] server received call_start",
                            "dur_ms": 0,
                            "latency_ms": 0,
                        }))
                else:
                    frames += 1
                    audio_bytes += len(message)
                    if frames <= 3 or frames % 50 == 0:
                        log(f"[serve] {peer} binary #{frames} {len(message)}B  "
                            f"first16={message[:16].hex(' ')}")
        finally:
            log(f"[serve] close {peer}: {frames} audio frames, {audio_bytes}B "
                f"({audio_bytes / 2 / WIRE_RATE:.1f}s)")

    server = await ws_serve(handler, args.listen_host, args.listen_port)
    bound = {sock.getsockname() for sock in server.sockets}
    for addr in sorted(bound, key=str):
        log(f"[serve] listening on ws://{addr[0]}:{addr[1]}/")
    log("[serve] no model loaded; this only prints what arrives. ctrl-c to stop.")
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        pass
    finally:
        server.close()
        await server.wait_closed()
    return 0


def fmt_addr(addr) -> str:
    if not addr:
        return "?"
    return f"{addr[0]}:{addr[1]}"


def split_url(url: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("ws", "wss"):
        raise SystemExit(f"{url}: expected a ws:// or wss:// url")
    if not parsed.hostname:
        raise SystemExit(f"{url}: no host in url")
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "wss" else 80)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--serve", action="store_true",
                   help="receive instead of send: print what arrives, load no model")
    p.add_argument("--url", default=f"ws://127.0.0.1:{DEFAULT_PORT}/",
                   help="client mode: the stt server to dial (default: %(default)s)")
    p.add_argument("--listen-host", default="0.0.0.0", help="--serve bind address")
    p.add_argument("--listen-port", type=int, default=DEFAULT_PORT, help="--serve port")
    p.add_argument("--seconds", type=float, default=1.5, help="tone length (default: %(default)s)")
    p.add_argument("--silence", type=float, default=0.8,
                   help="trailing silence, to close the utterance (default: %(default)s)")
    p.add_argument("--no-audio", action="store_true", help="connect and disconnect, send no audio")
    p.add_argument("--no-pace", dest="pace", action="store_false",
                   help="send as fast as the socket accepts instead of in real time")
    p.add_argument("--linger", type=float, default=5.0,
                   help="seconds to wait for replies before exiting (default: %(default)s)")
    p.add_argument("--timeout", type=float, default=8.0, help="connect timeout")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        return asyncio.run(serve(args) if args.serve else probe(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
