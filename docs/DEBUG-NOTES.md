# Debug notes

Observations from working on this system, written for whoever debugs it next. Opinionated on
purpose — the point is what helped and what got in the way, not a description of the design.

First entry: 2026-08-15, from the session that implemented the tuning changes and built the
speech return path.

## What made debugging possible

**`--debug-save-wav` plus `samples/MANIFEST.csv` is the best thing in this repo.** Being able to
re-measure a threshold against the exact audio that provoked a defect, in seconds and without a
phone call, is what turned two arguments into settled questions. Two thresholds in
`docs/ANALOG-TUNING.md` were *arithmetically incapable of firing* and nobody noticed until the
clips were replayed against the code rather than against the prose. Keep the clips. Keep the
manifest. `python -m stt_port.replay` exists so this stays cheap.

**Logging the quantity, not just the event.** `[gate] dropped chunk -- near-silence (0.16s speech
< 300ms and rms 0.0119 < 0.025)` is worth ten times `[gate] dropped chunk`, because the numbers
are what let you decide whether the rule was right without going back to the audio. Same for
`[tts] sent 2.45s of audio (synth=532ms)`. Every log line that fires on a judgement call should
print the values the judgement was made on.

## What got in the way

**Correlating a clip with the transcript it produced is manual.** The wav filename is a
timestamp, the transcript is a different line in a different stream, and matching them means
eyeballing clocks. `MANIFEST.csv` was assembled by hand. A `--debug-save-wav` that also wrote a
sidecar `.json` with the profile, the transcript and the gate decision would remove the single
most tedious step in every investigation here.

**One stderr stream for everything.** Transcripts, gate decisions, TTS sends, timings and
lifecycle lines all interleave for all calls. Grepping by timestamp works but is fragile. A
per-call log file, or even just the call id on every line, would make a bad call reviewable
after the fact instead of in real time.

**Two clocks in play.** Chunk timing uses `time.perf_counter()`; records carry `time.time()`.
Both are correct in context and the mixture is easy to trip over — `closed_at` is not comparable
with `record["t"]`. Worth knowing before writing anything that compares them.

## What green tests cannot tell you

**Every bug that mattered this session was invisible offline**, with 130 tests passing:

- a filler threshold fitted against a differently-computed statistic, which could never fire
- audio truncated because a whole reply was written to the socket at once and Asterisk's queue
  silently discarded the tail — the log reported every frame sent
- the entire STT server dying mid-call because a word could not be phonemized
- the model *fabricating* a plausible ending for a fragment created by a pause

Unit tests here verify mechanism. They cannot verify calibration, and they cannot verify
anything about how Asterisk, an ATA and a handset behave. **A change to a threshold or to the
audio path is not validated until a real call has been made.** The tuning doc says this; it is
worth repeating because the tests are good enough to be misleading.

**Test arithmetic is a real hazard when the units are bytes.** One test asserted 25 frames and
got 13, and the first suspicion was the implementation — wrongly. `b"\x00\x01" * 80` is 160
bytes, which is *half* a 320-byte frame. Build test buffers from `FRAME_SAMPLES`, never from
raw repeat counts.

## Traps specific to this system

**The filler thresholds are fitted to one HT801, one line, one voice.** They are not physics.
`--min-speech-ms`, `--min-speech-rms` and `--min-speech-floor-ms` all exist so they can be
re-measured rather than argued about. On different hardware, re-measure the continuity floor
first: it is the tightest, sitting 10ms from the shortest real word run in the corpus.

**Structural rules rest on invariants that may only be habits.** The hangup-click rule assumed
the click is always the chunk hangup flushed. True for five measured calls, false the moment
someone put the handset down slowly — the click then closes on silence like any other chunk.
When a rule depends on ordering or timing, ask what happens if the human is slower.

**Transcription and synthesis share one worker thread.** That is deliberate: two models must
never be resident on the GPU at once on an 8GB machine. The consequence is that a slow synthesis
delays the next transcription. Currently invisible (synthesis is 300ms–3s, ~7x realtime), but it
is a real coupling and the first place to look if transcripts start lagging after a long reply.

**An abandoned decode is not a cancelled decode.** `InferenceWatchdog` stops the *pipeline*
waiting; the runaway keeps the GPU until it finishes. It bounds what gets reported, not wall
time. The shortened temperature ladder and trimmed silent tails are what bound wall time.

## Process notes

**Ask what the tool is for before optimizing it.** A good stretch of this session went into
scoring word error rate against *Frankenstein* — 8.7%, mostly spelling conventions — when the
project is a conversational interface where a 25-second-late reply matters enormously and
`effected`/`affected` does not at all. Accuracy was already sufficient before the session
started. The turn-taking problems were the real ones and were visible from the first two-way
call.

**Check the process, not the artifact.** `MANIFEST.csv`'s last entry was 11:49, so a server was
described as idle since then. It was in fact serving live calls; `lsof` and `ps` would have said
so in one command. Verify the state of the world before acting on an inference about it.

**Prefer measurement to plausibility, especially when the plausible answer is available.** "The
audio gets quiet toward the end" sounds like a fade, and a fade sounds like a model problem.
Measuring the synthesized RMS envelope took thirty seconds and showed it flat end to end, which
moved the search to the transport and found the real bug immediately.
