"""`python -m phone` -- the gateway on the phone machine.

    python -m phone                 bridge calls to the stt machine, print transcripts here
    python -m phone --echo          hear yourself; proves Asterisk <-> Python audio
    python -m phone --call          ring the handset first, then bridge as usual

Asterisk connects to us, not the other way round, so this is a server in every mode: start
it, then lift the handset (or pass `--call`). It stays up across calls.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import sys

from . import audiosocket, config, session


async def echo_handler(call: audiosocket.Call) -> None:
    """Play every frame straight back down the line.

    The smallest thing that proves the whole media path: Asterisk answered, connected,
    negotiated a codec, decoded to slin, and our frames are landing back in the caller's
    ear. If this works, nothing between the handset and Python is at fault -- so it is the
    first thing to run when transcripts aren't appearing.
    """
    print(f"[echo] call {call.label} connected", file=sys.stderr)
    while (item := await call.recv()) is not None:
        if isinstance(item, audiosocket.Dtmf):
            print(f"[echo] call {call.label} dtmf {item}", file=sys.stderr)
            continue
        await call.send(item)
    print(
        f"[echo] call {call.label} ended (in={call.frames_in} out={call.frames_out} frames)",
        file=sys.stderr,
    )


async def run(args: argparse.Namespace) -> None:
    if args.echo:
        handler = echo_handler
    else:
        handler = functools.partial(
            session.handle_call,
            stt_url=args.stt_url,
            sink=session.jsonl_transcript if args.jsonl else session.print_transcript,
            verbose=args.verbose,
            responder=session.echo_responder if args.responder == "echo" else None,
        )

    server = await audiosocket.serve(handler, args.listen_host, args.listen_port)
    host, port = server.sockets[0].getsockname()[:2]
    print(
        f"[phone] audiosocket listening on {host}:{port}"
        + ("  (echo mode)" if args.echo else f"  stt={args.stt_url}"),
        file=sys.stderr,
    )

    async with server:
        if args.call is not None:
            # Originate after the server is up: Asterisk connects back the moment the
            # handset is answered, and there has to be something here to accept it.
            from . import originate

            await originate.call_phone(args.call or None, verbose=args.verbose)
        print("[phone] ready -- Ctrl-C to stop", file=sys.stderr)
        await server.serve_forever()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m phone",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--echo",
        action="store_true",
        help="play the caller's audio back to them instead of transcribing it",
    )
    p.add_argument(
        "--call",
        nargs="?",
        const="",
        default=None,
        metavar="ENDPOINT",
        help="ring the handset on startup instead of waiting for it; bare --call uses "
        f"{config.PHONE_ENDPOINT}",
    )
    p.add_argument("--stt-url", default=config.STT_URL, help=f"default: {config.STT_URL}")
    p.add_argument("--listen-host", default=config.AUDIOSOCKET_HOST)
    p.add_argument("--listen-port", type=int, default=config.AUDIOSOCKET_PORT)
    p.add_argument(
        "--jsonl",
        action="store_true",
        help="print one JSON record per transcript instead of plain text",
    )
    p.add_argument(
        "--responder",
        choices=["none", "echo"],
        default="none",
        help="speak a reply back down the line: echo repeats what you said, which proves the "
        "whole loop. Needs the stt machine started with --tts (default: none)",
    )
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
