# Next steps: turn-taking

Written 2026-08-15, after the session that built the speech return path. Assumes no prior
context.

## What this project is

An analog telephone as an interface to an LLM. Two machines:

- **Linux** (`192.168.50.1`) runs Asterisk and an HT801 ATA with a real handset attached. The
  `phone/` package bridges Asterisk's AudioSocket to a WebSocket. It is deliberately tiny —
  stdlib plus `websockets`, no numpy, no audio library. It forwards bytes it never inspects.
- **Mac** (`192.168.50.120`, 8GB unified memory) runs `stt_port/`, which owns every model:
  `whisper-large-v3-turbo` for transcription (~1.6GB) and `Kokoro-82M-4bit` for speech
  (~165MB), both under MLX on the Apple GPU.

```
Linux (Asterisk, HT801)                    Mac (models)
  caller audio    ──── binary ─────────>   transcribe
  transcript      <─── JSON text ──────
  reply text      ──── JSON "speak" ───>   synthesize
  play to handset <─── binary PCM16 ───
```

Both directions work today. `docs/ANALOG-TUNING.md` is the measurement record and is worth
reading before changing any threshold.

**The goal is a working model of a conversational interface** — a natural way to talk to an
LLM. It is explicitly *not* a transcription-accuracy project.

## Running it

```sh
# Mac
./.venv/bin/python -m stt_port.main --source ws --verbose --debug-save-wav --tts kokoro

# Linux
python -m phone --responder echo

# tests, from the repo root (unittest discover does NOT work -- imports fail)
.venv/bin/python -m unittest stt_port.test_server phone.test_audiosocket phone.test_session
```

130 tests, all passing as of this writing. `python -m stt_port.replay` re-runs the filler
gates over the 78 captured clips in `samples/` in seconds, without loading a model.

## Where things stand

Working, measured on live calls:

- transcription at ~1.3s per utterance; **8.7% WER** on 435 words of *Frankenstein* scored
  against the Project Gutenberg text, about a fifth of which is spelling conventions
  (`travelled`/`traveled`, `splendour`/`splendor`). Clean prose passages scored **0%**.
- synthesis at ~7x realtime; 25.8s of continuous speech delivered intact
- filler suppression: 2 phantom `Thank you.` in 72 utterances (~3%), down from ~20%
- both models resident in 8GB with no swap pressure

**Accuracy is already far better than this needs to be. Do not spend time on it.** The
failures that remain are proper nouns (`Margaret`→`Magret`, `Mrs. Saville`→`Mississaville`,
`Claude`→`Quad` when said alone) and homophones (`effected`/`affected`). Names are unreliable
on a 3.4kHz line — do not design a flow that depends on hearing one correctly the first time.

## The actual problem: the system has no idea whose turn it is

Four failures seen on live calls, all the same defect:

| Observed | Cause |
|---|---|
| A reply arrived **25 seconds late**, long after it mattered | every transcript gets a reply, and replies serialize behind playback |
| It replied to `Um`, `So...`, `Make...` | no notion of an unfinished thought |
| Talking over it did nothing | playback always runs to completion |
| Phantom `Thank you.` at hangup, 3 times in one session | it speaks when it has nothing to say |

## The work

### A. Tell the transcript whether the machine was speaking — `stt_port/`

The Mac knows when it is talking and never says so. Have `speak()` in `main.py` return the
duration it sent; `consume_chunks` keeps a per-call `speaking_until` deadline on the same
`time.perf_counter()` clock the queue tuple's `closed_at` already uses; `emit_transcript`
marks the record `during_playback: true` when the utterance closed inside that window.

No new clock and nothing for the phone side to guess. It composes with the existing
`continues_previous` flag, which is already the model's own signal (a lowercase opening word)
that an utterance is a mid-thought fragment.

### B. A responder that knows when not to speak — `phone/session.py`

`echo_responder` answers everything. Put a policy in front of it:

- skip `during_playback` — the caller was talking over it; listen, don't queue a reply
- skip `continues_previous` — a fragment, not a finished thought
- skip utterances under ~3 words with no terminal punctuation (`Um`, `So...`)
- allow **at most one reply in flight**; drop rather than queue, so a reply is never stale

This is where the 25-second backlog dies. **Keep `echo_responder` as the reply content** — it
stays deliberately dumb so any bad behaviour is unambiguously the policy's fault. A model goes
in this seam only once the turn logic is trustworthy; debugging both at once is a trap.

### C. Barge-in — `phone/audiosocket.py`, `phone/session.py`

Stop talking the moment the caller starts. This is the change that most makes it feel like a
phone call.

- `Call.stop_playback()` sets a flag that `send()`'s paced loop checks between frames, so an
  in-flight reply stops within 20ms. This is only cheap because playback is *already*
  frame-paced (see the history note below).
- `handle_call`'s read loop measures each inbound frame's RMS and calls it after ~3
  consecutive loud frames (60ms) while playback is active.

**The safe threshold is already measured.** Playback bleeding back into the line never once
crossed the segmenter's 0.01 threshold, across 15s and 25s playbacks on a live call. So a
barge-in threshold at **0.02** sits clear of the machine's own voice and well below real
speech at 0.078+. No echo cancellation is needed. Expose `--barge-in-rms` and
`--barge-in-frames` so this can be re-measured on other hardware.

Compute RMS with `struct.unpack` and a sum of squares — 8,000 multiply-adds a second. The
`phone/` package must stay free of numpy and any audio library; its README explains why.

### D. The phantom `Thank you.` — `stt_port/`

Three occurrences in one session. It belongs here rather than in a cleanup pass: a system that
reports words the caller never said is a turn-taking failure, and anything downstream acts on
them.

The existing rule (B2 in the tuning doc) identifies the cradle click as *the chunk hangup
flushed*, which only holds when `call_end` arrives inside the 500ms hangover. When the hand is
slow the click arrives as an ordinary silence-closed chunk instead, with a longest continuous
run of 0.18s — above the 0.13s continuity floor. **The floor cannot be raised**: the shortest
real word run measured is 0.14s.

Stop chasing it acoustically. The click is always the *last* chunk of a call. Quarantine
click-shaped chunks (longest run < 0.30s) for ~1s before emitting: if `call_end` arrives in
that window, drop it; otherwise emit. That costs up to a second of latency on short, loud,
isolated chunks only — rarely meaningful — and retires the whole class instead of moving a
threshold again.

## Verification

Unit tests first, in the style of the existing 130:

- a transcript closing inside a playback window is marked `during_playback`
- the responder stays silent for `during_playback`, `continues_previous`, and `Um`
- only one reply is ever in flight; a second is dropped, not queued
- `stop_playback()` halts a paced send within a frame or two (`frames_out` proves it)
- loud inbound frames trigger barge-in; frames at echo level (< 0.02 RMS) do not
- a quarantined chunk is dropped when `call_end` follows, emitted when it does not

Then a live call, which is the only thing that has ever caught the real bugs:

- talk over a long reply — it must **stop within a beat**, not finish
- say `Um`, `So...` — nothing should be spoken back
- ask something and wait — exactly one reply, promptly
- hang up slowly and awkwardly — **no `Thank you.`**
- hang up mid-playback — clean teardown, no stall

## Things learned the hard way

Each of these cost a live call to find. None would have been caught offline.

- **`Call.send()` must stay frame-paced.** It originally wrote every frame at once, because
  Asterisk paces playback itself — but only within a bounded queue. A 5.5s reply is 272 frames
  arriving instantly; the tail was silently discarded while the log reported it all sent. Short
  replies fit the queue, which disguised a truncation as a fade-out.
- **A synthesis failure must never reach the worker loop.** misaki raises
  `TypeError: NoneType + str` on a word it cannot phonemize, and because synthesis shares the
  worker thread with transcription, that killed the entire STT server mid-call. The word was
  "Claude". `espeak-ng` is now installed (`brew install espeak-ng`) so out-of-dictionary words
  phonemize instead of failing, but the exception handler is the real fix.
- **Fragmentation makes the model invent endings.** A pause split *"…by an undertaking such as
  mine"*, and the first fragment came back completed as *"…by the undertaking of the world"* —
  fluent, confident, and wrong — with `Such as mine.` arriving separately after. The tuning doc
  says fragmentation "destroys sentence structure, not words"; this is a counterexample, and
  the strongest argument for not replying to fragments.
- **A threshold is meaningless without the metric that produced it.** Two filler thresholds in
  the tuning doc were fitted against a differently-computed statistic and could never fire.
  Both are documented there as corrections. Use `python -m stt_port.replay` to check any new
  threshold against real audio before trusting it.
- **macOS `say` is off-limits**, including as a fallback. The voice model must be explicitly
  chosen and loaded.

## Deliberately not in scope

- **Accuracy work.** See above.
- **A model in the responder seam.** After turn-taking, not with it.
- **Folding the session's clips into `samples/`** and writing the newer findings into
  `docs/ANALOG-TUNING.md`. Both worth doing; neither blocks this.
