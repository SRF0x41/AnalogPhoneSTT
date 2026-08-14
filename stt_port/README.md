# stt_port — the speech-to-text machine

A fully local, always-listening transcription layer. It listens continuously, closes an
utterance on silence, transcribes it locally on the GPU, and prints the text to stdout. No
hotkey, no GUI, no text injection, no cloud, no telemetry.

**This is one of the two halves of AnalogPhoneSTT** (see the [repo README](../README.md)).
Asterisk, the HT801 and all call control live on the *phone machine*; this one does nothing
but turn audio into text. It holds no telephony state and needs no telephony dependency.

```
        phone machine                             this machine
  ┌──────────────────────────┐              ┌────────────────────────┐
  │  HT801 ── Asterisk       │              │  WebSocketSource       │
  │            │ AudioSocket │   audio ───▶ │    │                   │
  │          phone/          │   (binary)   │  Segmenter             │
  │                          │ ◀── text ─── │    │                   │
  └──────────────────────────┘  (JSON)      │  MLX Whisper           │
                                            │    │                   │
                                            │  stdout / --jsonl      │
                                            └────────────────────────┘
```

Audio sources are selected with `--source`:

- **`ws`** — call audio arriving over a WebSocket from the phone machine. What someone says
  into the handset gets transcribed as they speak, and each finished transcript is sent back
  over the same socket as well as printed here.
- **`mic`** (default) — this machine's hardware microphone, unchanged from the
  single-machine version and useful for checking the model without involving the phone.

This is scoped as a small modular experiment, not a production package — a testbed for
pieces that may get reused elsewhere. See "Scope and history" for how it got here.

## Quick start

On **this** machine:

```sh
python3.14 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python -m stt_port.main --source ws --verbose
```

On the **phone machine**:

```sh
systemctl start asterisk
python -m phone
```

Then lift the handset (the HT801 auto-dials extension 100) and talk. Transcripts print here
with a timestamp, one line per utterance — and on the phone machine too, which is the point
of the return path. Both processes stay up for the next call.

**Calibrate `--energy-threshold` before trusting silence detection** — see below. And do it
against the real line, since that's what the number describes.

## Testing this machine without the other one

`fake_call.py` replays a WAV file over the same WebSocket contract, so the whole receive
path — including the transcript coming back — can be exercised with no Asterisk, no phone
machine, and no handset:

```sh
say "hello, this is a test of the transcription layer" -o /tmp/speech.wav --data-format=LEI16@8000

./venv/bin/python -m stt_port.main --source ws --verbose      # terminal 1
./venv/bin/python -m stt_port.fake_call /tmp/speech.wav       # terminal 2
```

Note the **8kHz** in that `say` command: the analog line's native rate is what the wire
carries. Frames are paced in real time, so segmentation and hangover behave as they would
on a live call. `--drop 0.05` simulates loss, and `--no-hangup` omits the `call_end` message
to exercise the idle timeout.

## The link

One WebSocket connection per call, opened by the phone machine.

- **Binary messages are audio**: PCM16, little-endian, mono, **8kHz** — 20ms (320-byte)
  frames as they came off the line. `resample_to_target` lifts each closed *utterance* to
  the 16kHz Whisper wants, once, off the realtime path; upsampling before the network would
  have doubled the bytes without adding information.
- **Text messages are JSON control:**

  | direction | type | fields |
  |---|---|---|
  | phone → here | `call_start` | `call_id`, `rate`, `direction` |
  | phone → here | `call_end` | `reason` |
  | here → phone | `transcript` | `call`, `text`, `dur_ms`, `latency_ms` |

Binary in the here→phone direction is reserved for synthesized speech to play down the
line. Nothing sends it yet; the phone side already knows how to write it to Asterisk.

**`call_end` starts the teardown, it doesn't finish it.** Hangup is the only thing that
flushes the utterance a caller was mid-way through, so that utterance is still queued for
inference when `call_end` arrives — and inference takes a second or more, on another thread.
The connection is therefore held open after `call_end` until the call's outstanding chunks
have come back (`FINAL_DRAIN_TIMEOUT`, 5s ceiling), and **this side closes the socket**; the
phone machine waits for that close. Unregistering the socket at `call_end` instead means the
last thing the caller said is transcribed into nowhere.

### Why a WebSocket, and why this is smaller than what it replaced

The previous design carried the same audio over UDP with a hand-written 16-byte header, and
had to solve framing, sequence numbers, loss and late-frame accounting, an idle timeout, and
a heartbeat to tell "no call" apart from "link down". A WebSocket supplies framing,
ordering, a binary/text discriminator, and liveness, so none of that code exists here — and
unlike the UDP link it is bidirectional, which is what lets transcripts go home on the same
socket. [`docs/NETWORKING.md`](../docs/NETWORKING.md) is the retrospective on that design.

One thing did survive the change, because it was never about sockets: a call can go quiet
without ending, so `--idle-timeout` still flushes an utterance that a stall left open.

## Calibrating `--energy-threshold` — do this first

The `0.02` default was tuned against a hardware mic, where quiet is near-zero. **A G.711 phone
line is never digitally silent** — it carries line noise whose level depends on the ATA's gain
— so that number does not transfer, and the network default (`0.01`) is a starting guess, not
a measurement. Get it wrong in either direction and the symptom is silent failure:

| Threshold | Symptom |
|---|---|
| Too low | Every block reads as speech; no chunk ever closes until the 30s cap |
| Too high | No chunk ever opens; nothing is ever transcribed |

`--meter` settles this in one call. It loads no model, transcribes nothing, and just prints
rolling RMS to stderr:

```
./venv/bin/python -m stt_port.main --source ws --meter
```

```
[meter] rms min=0.0012 mean=0.0018 max=0.0031  over-threshold 0/10 (threshold=0.01)
[meter] rms min=0.0203 mean=0.0774 max=0.1621  over-threshold 10/10 (threshold=0.01)
```

Talk, then go quiet, and pick a threshold between the two bands.

## Expect lower accuracy than the mic

Telephony audio is 8kHz band-limited (G.711) and upsampled to 16kHz, so there is genuinely no
energy above 4kHz — that information is gone, not merely attenuated. Whisper handles narrowband
speech, but expect measurably worse results than the mic path, fricatives especially.
`--debug-save-wav` dumps each chunk to `/tmp` if you want to hear what the model heard.

## `--jsonl`

One record per utterance, for feeding something downstream instead of a human:

```json
{"t": 1786414880.426, "call": "a41c9e02", "text": "...", "dur_ms": 900.0, "latency_ms": 648.2}
```

`call` groups utterances from the same call (`null` on the mic path), `dur_ms` is the
utterance's audio length, and `latency_ms` is segment-close to printed. Startup and `--verbose`
timings go to stderr, so stdout stays clean enough to pipe either way.

## Backends

`--backend mlx` (default here) runs Whisper on the M-series GPU through Apple's MLX.
`--backend whisper` is faster-whisper/CTranslate2, which on this machine can only run on CPU;
it's kept as a fallback and as the default on a CUDA box. `--backend parakeet` (NeMo) is
implemented but has never worked in this project — see the end of this file.

**This machine has no CUDA**, so the port had to change backends, not just transport. That is
the single biggest functional difference from the single-machine version, and it is a real
latency regression: the GPU box measured **p50 = 157ms** on `large-v3-turbo`; the same model
through MLX here measures **p50 = 1405ms**.

### Measured on this machine (Apple M1, macOS 26.5, `mlx-whisper 0.4.3`, fp16)

20 warm iterations on a 3.6s clip. Whisper pads every input to a 30s window, so these times are
roughly constant per utterance regardless of how long the utterance actually was — a one-second
"yes" costs about the same as a nine-second sentence:

| `--model` | p50 | p95 |
|---|---|---|
| `mlx-community/whisper-large-v3-turbo` (default) | **1405ms** | 1494ms |
| `mlx-community/whisper-large-v3-turbo-q4` | 1439ms | 1446ms |
| `mlx-community/distil-whisper-large-v3` | 1340ms | 1345ms |
| `mlx-community/whisper-medium.en-mlx` | 1179ms | 1192ms |
| `mlx-community/whisper-small.en-mlx` | **387ms** | 397ms |
| `mlx-community/whisper-base.en-mlx` | **138ms** | 145ms |

Two things worth noticing. **Quantization buys nothing here** — the q4 turbo is a touch *slower*
than fp16, so the win people expect from it on other hardware doesn't appear on this one. And
the ladder is not smooth: the drop from `medium.en` to `small.en` is 3x, far more than the
parameter counts suggest, which makes `small.en` the interesting middle rung rather than
`medium.en`.

`base.en` at 138ms is *faster than the CUDA machine's 157ms* — the old latency budget is
reachable here, just not with the old model.

### On the accuracy of the smaller models: not established

Three synthesized sentences were band-limited to 8kHz, mu-law companded and given a noise
floor, to approximate a G.711 line, and run through every model above. All six produced
intelligible, essentially correct transcripts; the differences that showed up were formatting
(`4729` vs `four seven two nine`, `Redphone` vs `red phone`), not comprehension.

**Do not read that as "base.en is as good as large-v3-turbo."** Three clean sentences from a
speech synthesizer are the easy case — no accent, no overlap, no background, perfectly even
pacing. Where large models earn their cost is exactly the hard case this test doesn't contain.
The measurement establishes that the small models are not broken on narrowband audio, and
nothing more.

The default is therefore `large-v3-turbo`: it's what the CUDA machine ran, so transcripts stay
comparable across the port, and accuracy is the thing you can't recover after the fact. If
1.4s per utterance is too slow, `--model mlx-community/whisper-small.en-mlx` is the first thing
to try — but calibrate it against *your* line and your speakers, not against this table.

### End-to-end latency

Silence-detected-boundary → printed text is dominated by two things: `--hangover-ms` (default
500ms, a fixed delay before an utterance is even considered finished) plus inference. The
network link itself contributes ~0 on a wired LAN — a frame is 656 bytes and goes out the
moment it's read.

With the default model that's roughly **1.9s** from the end of a sentence to its transcript
(500ms hangover + ~1.4s inference, both observed). With `small.en` it's ~0.9s, and with
`base.en` ~0.64s. The original spec's sub-400ms target is not reachable on this hardware at any
model size, because the 500ms hangover alone exceeds it — lowering `--hangover-ms` is the only
lever left after that, at the cost of chopping sentences that have natural pauses in them.

## Testing

```sh
./venv/bin/python -m unittest stt_port.test_server -v
```

18 offline tests — no Asterisk, no phone, no GPU, no second machine, no model. Covers:

- the block accumulator: whatever size the sender's messages are, the segmenter sees fixed
  20ms blocks, partial frames carry over, and an odd byte count doesn't desynchronize the
  rest of the call
- PCM16→float32 conversion, including that byte order is explicit rather than
  host-dependent
- **that the move from a 16kHz to an 8kHz wire didn't change the segmenter's timing** —
  same hangover in wall-clock milliseconds, preroll and max-chunk scaling with the rate
- the server: silence-closed utterances, hangup mid-utterance, adopted mid-stream calls
  (audio before `call_start`), a socket closing without `call_end`, malformed JSON, two
  concurrent calls segmented independently, and the idle flush
- the transcript return path, including that a transcript for a call that already hung up
  is dropped rather than raising
- `consume_chunks`' sink: the phone machine receives exactly the record that gets printed,
  and empty transcripts reach neither

## What was actually verified here

The whole receive path, end to end, over a real WebSocket with `fake_call.py` replaying
paced 8kHz speech against a stub backend: two tone-separated utterances segmented
correctly, resampled 8k→16k, and both transcripts delivered back over the same socket to
the client. Also verified: MLX load, warmup and inference latency (the table above); and
that nothing in the network path needs `sounddevice`, `scipy` or any telephony dependency
to import or run.

**Not** verified, and needing hardware that isn't here:

- **No live call has ever been placed through this.** The phone machine's AudioSocket
  server is tested against a synthetic Asterisk client, not against Asterisk.
- **The two machines have never actually talked to each other.**
- The `--energy-threshold` default for the phone source is still an educated guess awaiting
  a real line. The simulated line used here had a noise floor around 0.002 RMS against
  speech around 0.14, which the `0.01` default separates cleanly — but that floor was
  chosen, not measured off an ATA.
- Transcription *accuracy* on real human speech (as opposed to macOS `say` output) on a
  real narrowband phone line — see the section above on why the small-model numbers prove
  less than they appear to.

The specific things to check on the first live call: `--meter` shows a clear gap between
speech and quiet; a normal sentence prints within ~2s of the pause; hanging up mid-sentence
still prints that last utterance; the transcript also appears on the phone machine; and
both processes stay up, ready for the next call.

## Deployment notes

- Only the caller's side of the audio is transcribed — that's what Asterisk hands to the
  phone machine. Playing synthesized speech back down the line is the reserved
  here→phone binary direction, and nothing implements it yet.
- **The phone machine may connect before this process is up, or reconnect after it
  restarts.** A refused connection is not fatal there: the call continues untranscribed
  rather than dropping. So "no transcripts" can mean this side isn't listening — check
  here before suspecting the line.
- Audio arriving before `call_start` is adopted as a new call rather than discarded, which
  is what makes restarting this process mid-call recoverable.

## CLI

```
--source mic|net             default: mic
--backend mlx|whisper|parakeet   default: mlx on macOS, whisper elsewhere
--model REPO_OR_NAME         override the backend's model (see the latency table)
--device cuda:N|cpu          whisper/parakeet only; mlx always uses the Apple GPU
--input-device N             sounddevice input device index (mic source)
--list-devices
--verbose
--jsonl                      one JSON record per utterance instead of plain text
--debug-save-wav             dump each closed chunk to /tmp/dictate_*.wav
--benchmark FILE.WAV         run 20 inferences on a file, report p50/p95, exit
--energy-threshold F         default: 0.02 (mic) / 0.01 (ws) -- calibrate with --meter
--hangover-ms F              default: 500

phone source (--source ws):
--listen-host ADDR           default: 0.0.0.0
--listen-port N              default: 9099
--idle-timeout SECONDS       flush an open utterance this long after audio stops
                             (default: 2.0); 0 disables
--meter                      print per-block RMS instead of transcribing (loads no model)
```

`python -m stt_port.fake_call`, on this machine:

```
WAV                          8kHz mono 16-bit PCM
--url URL                    default: ws://127.0.0.1:9099/
--no-pace                    send at full speed instead of real time
--drop FRACTION              drop this fraction of frames to simulate loss
--no-hangup                  omit call_end (exercises --idle-timeout)
--linger SECONDS             wait this long for transcripts before exiting (default: 5.0)
```

## Offline verification

```sh
DICTATE_OFFLINE=1 ./venv/bin/python -m stt_port.main --benchmark some.wav
```

`DICTATE_OFFLINE=1` forces `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` before the backend loads.
With the model already cached from a prior run, load still succeeds with no network access;
without a cached model, the same flag makes any accidental network call fail loudly instead of
silently succeeding.

## Scope and history

This started as a push-to-talk spec (hold a hotkey, speak, release, inject text at the cursor).
Three narrowing conversations later: native Wayland doesn't support reliable global hotkey
capture via `pynput` (a Wayland design choice, not a bug), so the hotkey was dropped in favor of
always-listening with silence-only segmentation, and text injection was dropped in favor of
printing to stdout.

That testbed was then joined to the telephony side, which had built a call-audio stream with
nothing consuming it. The join needed no audio plumbing — just a second source feeding the
existing `Segmenter`, plus the two things a phone has that a mic doesn't: hangup as a second
endpoint, and a noise floor. Both halves ran in one process on the Linux box then; that
version lives on in the predecessor repo, `RedPhone`, and is not carried forward here.

Splitting the halves across two machines moved the seam from a Python import to a socket.
The first attempt at that socket was a hand-written UDP protocol; it was replaced by the
WebSocket described above before either half ever ran against the other. Through both, the
audio pipeline is untouched: the same `Segmenter`, the same `consume_chunks`. Two things did
change — the ASR runtime, because this machine has no CUDA, and the wire rate, which is now
the line's own 8kHz rather than a pre-upsampled 16kHz.

### Why the phone source is a network source, not a `--source phone`

The single-machine version imported the telephony code and constructed a `VoIPPhone` in this
process. That's gone, and with it every SIP dependency (`pyVoIP`, `audioop-lts`) and
every SIP flag on this side. This machine now holds no Asterisk credentials and no
telephony state at all — and neither does the phone machine's Python, since Asterisk itself
does that job there. Origination moved with it: `python -m phone --call` places calls,
because only that machine can.

### Why `mlx` is the default backend here, and `parakeet` is the default nowhere

The original ask was NVIDIA Parakeet TDT 0.6B v3 via NeMo as primary. On the GPU machine's
Python 3.14 + `nemo_toolkit[asr]`, a plain install succeeded but **`import nemo.collections.asr`
crashed** through a chain of transitive dependency conflicts — a source-built `onnx` whose
protobuf gencode outran the pinned runtime, then `ml_dtypes` missing attributes that `onnx`
expected, then `numba` rejecting the numpy version that fix pulled in. Each fix revealed the
next break in the same chain, which was the signal to stop. It was never retried here: Parakeet
needs CUDA, and this machine doesn't have any. `ParakeetBackend` remains in `backends.py`
because the `Backend` protocol makes it a straight swap if it's ever worth another try on a
machine that could run it.

**Telemetry note**: `nemo_toolkit[asr]` pulls `wandb` and NVIDIA's `nv_one_logger` in as
transitive dependencies of the Lightning training stack. Neither is ever invoked here
(inference only, no `Trainer`), but they're real packages that exist to phone home, so it's
worth knowing they'd be sitting in the venv if you install it.

### Non-goals (kept from the original spec)

No LLM cleanup/punctuation pass, no streaming/partial hypotheses, no GUI/tray/config file/
installer, no multi-GPU/batching/quantization, no speaker diarization or timestamps.

The network split kept all of these. In particular it is **per-utterance, not word-streaming**:
text appears once the speaker pauses, not while they're still talking. Rolling partials were
considered and deliberately not built.

### Known cosmetic issue

Force-killing the process (e.g. via `timeout` rather than `Ctrl-C`) can print a benign
`resource_tracker: leaked semaphore` warning at shutdown from a background library's thread
pool. Harmless, does not indicate a resource leak in `stt_port/` itself.
