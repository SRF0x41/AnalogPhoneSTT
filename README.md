# AnalogPhoneSTT

Networked analog phone to speech-to-text.

A Grandstream HT801 ATA puts a real handset on the LAN; Asterisk answers it and
hands the call's audio to a small Python gateway; the audio crosses to a second
machine that turns it into text and hands the text back. The pieces are
deliberately generic — an audio pipe and a transcript feed — so whatever consumes
the transcripts can sit on either machine.

The work is split across two machines because the two halves want different
hardware, and each half is its own package.

```
   phone machine (Linux, 192.168.50.1)              stt machine (Apple Silicon, .120)
 ┌───────────────────────────────────────┐        ┌──────────────────────────────────┐
 │  HT801 ── Asterisk                    │        │  stt_port/                       │
 │             │  AudioSocket            │        │    WebSocket server              │
 │             │  (localhost TCP)        │        │      │                           │
 │             ▼                         │        │    Segmenter                     │
 │          phone/  ──── audio (binary) ─┼───────▶│      │                           │
 │             ▲   ◀──── transcripts ────┼────────│    MLX Whisper                   │
 │             │         (JSON)          │        └──────────────────────────────────┘
 │             └── ARI ──▶ Asterisk      │
 │                 (originate calls)     │
 └───────────────────────────────────────┘
```

The phone machine stays in charge: it holds call control, the call audio, and the
finished transcripts. The stt machine is a stateless worker — PCM in, text out.

## The two packages

| | runs on | what it is |
|---|---|---|
| [`phone/`](phone/) | the Linux box, next to Asterisk | AudioSocket server, the bridge to the stt machine, ARI call origination |
| [`stt_port/`](stt_port/) | the Mac | WebSocket STT service: segment on silence, transcribe with MLX Whisper, emit JSON |

Each installs and runs only on its own machine, from its own `requirements.txt`.
There are no cross-imports between them — the only thing coupling them is the
WebSocket message contract, which both READMEs document.

## Why it's built this way

Asterisk already terminates SIP, negotiates codecs, absorbs jitter, and can hand
raw call media to a local process over
[AudioSocket](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/).
So `phone/` doesn't speak SIP, doesn't touch RTP, and doesn't resample: it reads
20ms frames of 8kHz signed-linear PCM off a TCP socket and forwards them. Its
entire dependency list is `websockets` and `requests`.

The link between the machines is a plain WebSocket rather than a custom protocol.
Binary messages are audio, text messages are JSON — which gives framing, a
message-type discriminator, ordering, and liveness for free, and lets one
connection carry the audio going out and the transcripts coming back.

Audio stays at the line's native **8kHz** all the way to the stt machine, which
upsamples to the 16kHz Whisper wants once per utterance, off the realtime path.

[`docs/NETWORKING.md`](docs/NETWORKING.md) is a retrospective, carried over from
this project's predecessor `RedPhone`, on an earlier design: a hand-rolled UDP
protocol between two pyVoIP/Asterisk processes. It explains what that had to solve
and why the current design doesn't have to solve it. Its reasoning about packet
loss and silence detection still applies.

[`docs/LINK-TROUBLESHOOTING.md`](docs/LINK-TROUBLESHOOTING.md) covers failures of
the network path between the two machines — in particular the one where a wrong
netmask on the stt machine makes the stt layer look dead while neither side logs
anything at all.

## Getting started

On the phone machine:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r phone/requirements.txt
python -m phone --echo        # start here; see phone/README.md
```

On the stt machine:

```sh
python3 -m venv venv && . venv/bin/activate
pip install -r stt_port/requirements.txt
python -m stt_port.main --source ws    # see stt_port/README.md
```

Asterisk config samples (dialplan, ARI) live in [`asterisk/`](asterisk/) and have
to be applied by hand — they need root.

## Status

Both halves are unit- and integration-tested offline: 51 tests covering the
AudioSocket frame codec, the call lifecycle, the bridge, the WebSocket server, and
segmentation at 8kHz — plus an end-to-end run of a synthetic Asterisk through both
packages to a transcript arriving back on the phone machine.

**The first live call has now run** — real Asterisk, a real HT801, a real handset,
across both machines. A 150-second call carried `blocks=7500 (150.0s)`: 7500 frames
of 20ms each, exactly the call duration, so nothing was lost on the wire. Speech
transcribed accurately at a median 1.34s inference per utterance, transcripts
arrived back on the phone machine, and the utterance interrupted by hanging up was
still transcribed after `call_end` — the drain behaviour the design exists for.

Two things that live hardware exposed, neither of them yet fixed:

- **`--energy-threshold` is still uncalibrated**, and it matters more than the
  offline tests could show: 12 of 32 utterances on that call were Whisper filler
  (`so`, `Thank you.`) hallucinated onto segments that line noise had opened. See
  [`stt_port/README.md`](stt_port/README.md).
- **The link is fragile in a way that logs nothing.** A wrong netmask on the stt
  machine silently misroutes the reply packets, and both machines then look
  healthy while no audio moves. See
  [`docs/LINK-TROUBLESHOOTING.md`](docs/LINK-TROUBLESHOOTING.md).

Still unverified: transcription accuracy against a known reference text, and
anything about call origination through ARI.

Machine-specific notes (device passwords, service paths) belong in an untracked
`local-notes.md`, not in this repo.
