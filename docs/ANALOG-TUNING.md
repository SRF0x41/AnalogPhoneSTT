# Tuning the transcription for an analog line

A plan for improving transcript quality on the phone source, written against measurements
from five live calls on 2026-08-14/15 rather than against general expectations about G.711.

Every number below comes from **78 captured utterances** on a real HT801 handset — recorded
with `--debug-save-wav`, kept in `samples/`, and paired against the server's timing log.
Later calls were scripted against known reference text so accuracy could be scored rather
than eyeballed.

Four claims in earlier drafts were falsified by later measurements and are marked where they
appear, rather than quietly removed — the corrections are the most useful part of the
document, because every wrong answer was the intuitive one. The third was found during
implementation, by replaying the clip set against the code rather than against the prose. The
fourth was found by a live call *after* the code shipped, and could not have been found any
other way: it was an assumption about how quickly someone hangs up.

**Changes A–D are implemented.** See "Implementation status" at the end for what each one
measured afterwards.

## The headline: the audio is not the problem

The instinct with a phone line is to reach for noise reduction. **On this line that would
be wasted effort**, and possibly harmful. Measured across all captured audio:

| Measurement | Value | Reading |
|---|---|---|
| SNR | **37.5 dB** | Excellent. Broadcast-adjacent. |
| Noise floor | 0.0015 RMS | Very quiet |
| Speech level | 0.1127 RMS | Healthy, ~75× the floor |
| DC offset | +0.00002 | Absent |
| Clipping | 39 samples in ~441,000 | 0.009%, inaudible |
| Frame loss | 0 across all five calls | The wire is lossless |

The spectrum is exactly the narrowband shape you would predict, and nothing worse:

```
   0-100 Hz   -21.3 dB   ██████████████████
 100-300 Hz   -10.0 dB   █████████████████████████████
 300-600 Hz    -4.5 dB   ███████████████████████████████████  ← voice energy
 600-1000Hz    -9.6 dB   ██████████████████████████████
1000-2000Hz   -14.7 dB   █████████████████████████
2000-3000Hz   -21.4 dB   ██████████████████
3000-3400Hz   -23.2 dB   ████████████████
3400-4000Hz   -28.1 dB   ███████████     ← the G.711 cliff
```

**So the accuracy ceiling here is set by bandwidth, not by noise.** Everything above
~3.4kHz is gone, which is where the fricatives live — `s`/`f`/`th` are hard to tell apart,
and so are `b`/`v`/`d`/`p`. No amount of processing recovers information the codec discarded.
A denoiser applied to a 37.5 dB SNR signal mostly removes speech.

Accuracy on the live calls was in fact good. **19 of 20 spoken digits correct**, with
`eighteen` → `810` the sole error. A read reference passage came back essentially clean,
including the cases chosen to be hard for a narrowband line:

| Spoken | Transcribed |
|---|---|
| pangram | `The quick brown fox jumps over the lazy dog.` — verbatim |
| invoice / date / name / extension | `Please confirm invoice number 4729, dated August 15th, and forward it to Sarah Fitzgerald, at extension 613.` |
| the classic narrowband confusion | `Verify whether it was 50 or 15 because the difference matters.` |

Proper nouns (`Phoenix`, `Baltimore`, `Sarah Fitzgerald`), four-digit numbers and the
`50`/`15` pair — the exact things that lose their distinguishing energy above 3.4kHz — all
survived. **Recognition is not the weak point of this system.**

A dedicated minimal-pair test found the real limits, and they are narrow:

| Test | Result |
|---|---|
| One-word answers (`Yes` `No` `Eight` `Stop` `Go` `Now` `Four` `Hi`) | **8 / 8** |
| Teen/ty pairs (`fifteen` `fifty` `sixteen` `sixty`) | **4 / 4** — the classic confusion, clean |
| Spoken letters (`B` `V` `D` `P` `T` `G`) | **4 / 6** |
| Minimal pairs (`thin` `fin` `sin` `vest` `best` `dime` `time`) | **5 / 7** |

Scored against known reference text, with numerals normalised so digit-vs-word rendering is
not counted as an error:

| Passage | WER |
|---|---|
| `Pack my box with five dozen liquor jugs.` | **0.0%** |
| `The quick brown fox jumps over the lazy dog.` | **0.0%** |
| The branch / surplus / four hundred units sentence | **0.0%** |
| The northern-depot sentence | 22.2% |
| **Overall** | **8.7%** |

**Every error in that total is the single word `depot`**, which came back as `D-Pot` both
times it appeared — a deterministic lexical failure, not acoustic noise, and it inflates the
count because one word becomes two tokens. Excluding that word, the scored passages are
**0% WER**.

> **Correction: "deterministic" was too strong.** On a later call the sentence *"Claude, the
> northern depot has fifteen or fifty crates, and Sarah can't tell which"* came back verbatim,
> `depot` included. The same word, the same line, the same model. What changed was the
> surrounding context: both failures put `depot` in a short phrase (`the northern depot`),
> while the success embedded it in a long sentence with strong lexical neighbours.
>
> So it is context-dependent, not deterministic — which puts it in the same category as
> everything else on this line. `Claude` behaves identically: standalone it came back as
> `Quad`, and in the middle of that sentence it was correct. **Context is the mechanism that
> rescues narrowband audio**, and the practical advice in the minimal-pair section — keep
> meaning inside sentences, never in isolated tokens — is if anything better supported than
> when it was written.

The teen/ty result is the surprise: `fifteen` vs `fifty` is the canonical narrowband failure
and this line handles it perfectly. The weakness is elsewhere and it is consistent —
**consonant discrimination on isolated short tokens**:

| Spoken | Heard | Confusion |
|---|---|---|
| `V` | `the` | voiced fricative |
| `B` | `Be.` | homophone, not the letter |
| `thin` | `fin` | th → f |
| `best` | `vest` | b → v |

All four are the same failure: the release burst and high-frequency frication that separate
these sounds live above 3.4kHz, and G.711 discarded them. **This is the true accuracy
ceiling of the line, and no amount of processing on this machine can lift it** — the
information is not in the signal.

**Practical consequence:** do not build anything that spells things out letter by letter over
this line, and do not rely on isolated minimal-pair words carrying meaning on their own. Both
succeed inside a sentence, where context disambiguates — `dime` and `time` were both correct,
and the pangrams were verbatim. If letters are ever unavoidable, a phonetic alphabet
(`Bravo`, `Victor`) moves the discrimination into the vowel space that survives the codec.

## What is actually wrong

Four defects, none of which is audio quality. Ranked by damage.

### 1. A single degenerate decode can stall the pipeline for 20+ seconds

The worst event of the session. One 1.16s clip of the word "eight" produced:

```
[00:18:57] 8 check check check check check check check ... (~70×)
inference=22432.9ms
```

**22.4 seconds of inference for 1.16 seconds of audio** — 19× realtime, and **35.6% of all
inference time in a 147-second call** spent on one clip. The consequence was not the garbage
line; it was the backlog. Queue wait (`segment=`) behind it went to 22s and drained down
through 21, 20, 19 as the caller kept counting. Every subsequent number was transcribed
*correctly* and arrived up to 22 seconds late.

On a live call, a 20-second delay is indistinguishable from the system being broken.

The cause is Whisper's temperature-fallback path. `compression_ratio_threshold` detects the
repetition and retries the decode at successively higher temperatures — the default ladder is
six attempts — and here every attempt looped, so it paid the full cost and returned the
garbage anyway. The guard fires but cannot win, and nothing bounds what it costs to lose.

Re-running the same clip offline produced `eight` in 4132ms rather than the 22s loop, so the
failure is **non-deterministic** and cannot be reproduced on demand. It must be defended
against structurally rather than fixed by finding the one bad input.

### 2. Short non-speech chunks reliably produce hallucinated filler

**22 of 110 utterances across five calls — one in five — were pure invention**, and almost
every one was the exact string `Thank you.` Whisper does not return empty for input
containing no speech; it returns the phrase it saw most often over near-silence in training,
and it returns it confidently.

Pairing each saved clip against the text it produced shows **three distinct sources**, which
matters because no single rule catches all of them:

| Source | speech duration | mean RMS | example | catchable by |
|---|---|---|---|---|
| Near-silence | 0.02–0.06s | 0.004–0.006 | breath, faint line transient | B1 (energy gate) |
| Short loud transient | 0.20–0.28s | 0.07–0.12 | handset hitting the cradle | B2 (post-`call_end`) |
| **Mid-sentence pause** | short | varies | a deliberate pause inside a sentence | **neither** |

The third source is the worst and was found last. Reading a sentence with deliberate pauses
produced this:

```
[11:49:04] The branch reported a surplus.
[11:49:07] of 400 units.
[11:49:09] Thank you.          ← hallucinated into the pause
[11:49:10] which nobody predicted.
```

The pause itself opened a chunk and the model filled it. This lands **in the middle of real
content**, where no structural rule applies — it is not at hangup, and it is not necessarily
quiet enough for an energy gate. It is the residual case that B1 and B2 do not cover, and the
honest position is that the filler-phrase denylist is the only thing that catches it. That is
the strongest argument for keeping the denylist as a backstop rather than dropping it.

The first source is trivially separable — an order of magnitude below any real speech. The
second is not. The clip at the end of the third call measured `speech=0.28s`, `rms=0.072`,
`peak=0.980`: louder than most genuine speech, and *longer* than the shortest real utterance
(0.24s, the word "eight"). **A duration-and-energy gate cannot separate a hook click from a
one-word answer**, because on those two axes they are the same object.

#### The model's own confidence signals do not help — measured, not assumed

The obvious next move is to ask Whisper how sure it is, and discard low-confidence segments.
That was tested against the captured clips and **it does not work**:

| clip | `no_speech_prob` | `avg_logprob` | `compression_ratio` |
|---|---|---|---|
| filler (quiet) | 0.000 | −0.781 | 0.56 |
| filler (loud) | 0.000 | −0.559 | 0.56 |
| filler (call 2) | 0.000 | −0.865 | 0.56 |
| real — pangram | 0.000 | −0.131 | 0.86 |
| real — "Eight." | 0.000 | **−0.752** | 0.43 |

`no_speech_prob` is **0.000 for every clip including pure filler** — the model is not
uncertain, it is confidently wrong. `avg_logprob` separates *long* real speech (−0.13) but
overlaps filler completely at the short end: genuine `Eight.` scores −0.752, squarely inside
the filler range of −0.56 to −0.87. `compression_ratio` reflects the output string, not the
input audio.

So thresholding on decoder confidence would discard real one-word answers at the same rate
as filler. **Do not build on these signals**; the plan below routes around them.

Note also that the looping clip in defect 1 had **0.24s** of speech — the shortest of the
real clips. Degenerate decodes cluster at the short end. Defects 1 and 2 share a boundary,
and the short-utterance region is simply where this model is unreliable.

### 3. Formatting drifts between utterances

Counting to twenty produced `One.` `two.` `3` `four` `5,` `six` `Seven.` — the same class of
token rendered five different ways. Each utterance is a separate decode with
`condition_on_previous_text=False` (correct — conditioning invites runaway hallucination), so
nothing carries casing, numeral style or punctuation across chunk boundaries.

This is cosmetic for a human reader and material for anything parsing the transcript.

The read passage showed the same drift in casing — `but thirty five boxes were still sitting
in baltimore` came back entirely lowercase with `baltimore` uncapitalised, while the
neighbouring utterances were sentence-cased and capitalised `Phoenix`, `Baltimore` and
`Sarah Fitzgerald` correctly. And numerals rendered three different ways within one passage:
`thirty five` as words, `4729` and `613` as digits, `50 or 15` as digits.

### 4. Natural pauses fragment sentences across utterances

One sentence in the read passage arrived as three separate transcripts:

```
[00:24:38] the Southern Branch reports both a surplus
[00:24:40] and a shortfall.
[00:24:42] which seemed thoroughly implausible.
```

The speaker paused mid-sentence for longer than `HANGOVER_MS` (500ms), so the segmenter
closed the utterance three times. Each fragment is transcribed correctly; the sentence is
lost.

A controlled repeat of this — one sentence read with deliberate pauses at marked points —
reproduced it exactly, and added a hallucination in one of the pauses (see defect 2). Worth
noting what it did *not* cost: the fragments concatenate to the reference sentence with
**0% WER**. Fragmentation destroys sentence structure, not words.

The complementary test settles the other half. The same kind of sentence read **straight
through with no pauses** arrived as a **single utterance**, correctly transcribed, with no
fragmentation and no decode loop. Dense continuous speech is the safe case for this
pipeline; it is pauses that cause trouble, in two different ways at once.

This is the direct cost of the 500ms hangover, and it is a genuine trade-off rather than a
bug — a longer hangover preserves sentences but adds latency to every utterance, and
`condition_on_previous_text=False` means the model gets no help reassembling them. Whether
it matters depends entirely on what consumes the transcripts: for a human reading a live
feed it is barely noticeable, for anything doing intent parsing it is fatal.

Worth noting the lowercase leading `the` and `and` above: the model itself signals that these
are continuations rather than sentence starts, which is a usable reassembly hint if fragments
ever need stitching.

## What is *not* wrong, and should be left alone

**`--energy-threshold 0.01` is correctly calibrated.** This was assumed to be the problem
before the measurements and it is not. 14 seconds of deliberate silence with the handset
live produced **zero chunks**; the quietest 100ms window of every captured clip sits at
0.0016–0.0039, a 5× margin under the threshold. Line noise never opens an utterance.
Running `--meter` would change nothing. The `stt_port` README's warning about the default
being an untested guess has now been tested, and the guess was right for this line.

**Segmentation is otherwise sound.** Chunks open on speech and close on silence as designed,
and hangup flushes the in-progress utterance correctly.

## Proposed changes

Ordered by value per unit of risk. The first three are small, local, and independently
testable; nothing here requires touching the model or the network protocol.

### A. Bound the cost of a degenerate decode — *highest value*

Two independent guards, because the failure is non-deterministic:

1. **Shorten the temperature-fallback ladder.** Pass an explicit, shorter `temperature`
   tuple to `mlx_whisper.transcribe` in `MLXWhisperBackend.transcribe`
   (`stt_port/backends.py:145`) — e.g. `(0.0, 0.2, 0.4)`. Whisper retries a suspected loop at
   each rung; capping the ladder caps the worst case. A retry that has failed three times is
   not going to succeed on the sixth.
2. **Add a watchdog on inference wall time.** If a decode exceeds a ceiling (~4s, comfortably
   above the 1365ms p90), abandon that utterance and log it rather than let it hold the
   worker. Losing one utterance is strictly better than delaying the next twenty.

**Verification:** replay the captured clips; assert no decode exceeds the ceiling and that
queue wait stays under ~500ms throughout.

### B. Suppress filler in layers, because the sources differ

An earlier draft of this plan proposed a single duration gate at 0.25s, on the strength of a
clean split in the first 32 utterances. **The next call broke it** — a 0.28s, `rms=0.072`
hook click landed above the gate and produced `Thank you.` anyway. The lesson is recorded
here rather than quietly dropped: the clean rule came from n=4 and did not survive n=6.

Two layers, each aimed at the source it can actually catch:

**B1 — an energy-and-duration gate, for the near-silence source.** Discard a chunk before
inference when `speech duration < 0.25s AND mean RMS < 0.025`. Both conditions, not either:
the conjunction is what stops it swallowing a genuine short answer, since real speech at
0.24s measured 0.05–0.12 RMS, an order of magnitude clear.

> **Correction — the third falsified claim, and the most embarrassing one.** This rule
> originally read `mean RMS < 0.01`, and it **cannot fire**. Mean RMS is defined below as the
> mean over frames *already above 0.01*, so it is either exactly 0.0 or strictly greater than
> 0.01 — never in between. Replaying all 78 clips against the implementation suppressed **0 of
> 10** filler utterances, not the 4 of 7 predicted. The threshold was read off the wrong
> column: the `0.004–0.006` in the sources table above is whole-clip RMS, while the
> `0.078–0.167` in the safety-margin table below is the speech-frame mean. They are different
> metrics and the plan mixed them.
>
> Re-fitted against the real numbers, the quiet filler measures **0.0102–0.0163** speech-frame
> RMS, so the gate is now **0.025** — clear of the filler and far below the 0.078 floor of real
> one-word answers.
>
> **And the safety claim was wrong too.** "Provably cannot touch anything as loud as real
> speech" is false: the genuine 0.02s exclamation noted below measures 0.0144 speech-frame RMS
> and **0.0038 whole-clip RMS — quieter than every single filler clip** on that axis, and
> inside their range on the other. It is not separable from filler at any threshold on these
> two axes. The gate takes it. That is a deliberate trade — six hallucinations removed for one
> short exclamation lost — not an oversight, and it is why every suppression is logged.
>
> The lesson repeats the one in the paragraph above: a threshold is meaningless without the
> metric that produced it, and the metric has to be the one the code actually computes.
> `stt_port/replay.py` exists so the next threshold is checked against the clips, not the prose.

**B2 — suppress the terminal hook click, which is structurally identifiable.** The loud
short filler is not random: **all five calls ended with `Thank you.` as their final
transcript**, arriving immediately after `[ws] call ... ended (hangup)`. It is the handset
hitting the cradle, captured as a chunk and flushed by the drain path that exists to preserve
the last real utterance.

```
[ws] call 6b4d8c86 ended (hangup) blocks=7359 (147.2s)
[00:20:43] Thank you.
[ws] call 2ebea227 ended (hangup) blocks=2337 (46.7s)
[00:24:52] Thank you.
[ws] call c20a88b4 ended (hangup) blocks=2825 (56.5s)
[00:31:02] Thank you.
```

So the rule needs no phrase list and no acoustic threshold: **discard a post-`call_end`
chunk holding less than ~0.3s of speech.** It is precise (it can only ever fire once per
call, at the moment the line is being torn down), it is safe (a caller mid-sentence at
hangup produces far more than 0.3s — call 1's final real utterance was a full sentence), and
it removes the single most reliable source of filler in the system.

> **Correction — the fourth falsified claim, found by a live call after B2 shipped.** B2 is
> right but **not sufficient**, and the reason is timing nobody had varied. It identifies the
> click as *the chunk hangup flushed*, which only holds when `call_end` arrives inside the
> 500ms hangover. All five calls measured above hung up promptly. Hang up two seconds before
> the socket closes — a slow hand on the cradle — and the click closes on silence like any
> ordinary chunk, arrives with `final=False`, and comes back as `Thank you.` regardless.
>
> Two live calls did exactly that, and the second defeated the first attempted fix:
>
> | click | speech | mean RMS | longest unbroken run | caught by |
> |---|---|---|---|---|
> | call at 12:35 | 0.06s | 0.2372 | 0.06s | a total-duration floor |
> | call at 12:39 | **0.20s** | 0.1225 | **0.12s** | only a *continuity* floor |
>
> The second is the instructive one. 0.20s of speech is too much for a duration floor (the
> shortest real utterance is 0.24s) and 0.1225 RMS is five times too loud for B1's energy
> half. But its loud frames sat at positions 15–16, 26–27 and 51–56 of 81 — **three isolated
> taps across 1.64s**, a handset resting, shifting, then seating. It is not one sound.
>
> That is the axis where a settling handset and a short word genuinely differ, and it is the
> one this document never measured: **a word is continuous.** Across all 78 clips and five
> live calls, clicks run 0.02–0.12s unbroken and real utterances 0.14s or longer. The floor is
> `--min-speech-floor-ms`, default **130**, and it consults energy not at all.
>
> The margin is thin where the corpus is thinnest: 0.13s sits 10ms above the shortest real run
> in the clip set (the word `fourteen`) and 10ms below the longest click. Live calls put real
> utterances at 0.20s and up, so the practical margin is wider — but this is the **first number
> to re-measure on different hardware**, ahead of the energy thresholds.
>
> The general lesson is the same one as the `0.01` threshold above, one level up: a structural
> rule is only as good as the invariant it rests on, and "hangup flushes the click" was an
> invariant of *how fast the tester hung up*.

Prefer this to a filler-phrase denylist. A denylist was the earlier proposal here, justified
by 100% of filler being the exact string `Thank you.`; it is still available as a backstop,
applied only to utterances under ~1s so a genuine "thank you" is never touched. But matching
on structure beats matching on strings when the structure is this clean.

> **The denylist turned out to be unnecessary.** At its re-fitted 0.025 threshold B1 catches
> **both** mid-sentence-pause hallucinations — they measure 0.0117 and 0.0163 speech-frame RMS,
> quiet enough to gate — which the "residual case" argument above assumed only a denylist could
> reach. Measured over the whole clip set, B1 and B2 together suppress **10 of 10** filler
> utterances with no phrase matching at all. Nothing in this system inspects the text of a
> transcript to decide whether to keep it, and it should stay that way.

Both layers should **log what they suppress rather than silently dropping it**. A caller who
really does say nothing but "thank you" and sees it vanish has no way to tell the difference
between a suppression rule and a broken system, and the log is what distinguishes them later.
Make the duration and RMS floors CLI flags (`--min-speech-ms`, `--min-speech-rms`) so the
boundary can be re-tuned against a different ATA without a code change.

#### B1's safety margin, measured against real short answers

The false-positive risk in B1 is entirely about one-word replies, so eight of them were
recorded deliberately. Every one sits far clear of the gate:

| Spoken | speech | mean RMS | gate fires? |
|---|---|---|---|
| Yes. | 0.58s | 0.084 | no |
| No. | 0.36s | 0.086 | no |
| Eight. | 0.38s | 0.078 | no |
| Stop. | 0.38s | 0.167 | no |
| Go. | 0.36s | 0.146 | no |
| Now. | 0.46s | 0.118 | no |
| Four. | 0.36s | 0.149 | no |
| Hi. | 0.38s | 0.147 | no |

**8 of 8 transcribed correctly, and 0 of 8 would be dropped.** The shortest is 0.36s against
a 0.25s gate, and the quietest is 0.078 RMS against a 0.01 floor — roughly 8× clear on the
axis that matters. B1 is safe.

**But it is also weaker than first estimated.** On the fourth call it would have removed
*none* of the filler, because the only filler was the loud hook click. B1 catches the
near-silence source and nothing else; B2 is doing the real work.

One caution the data forced. A genuine exclamation measured **0.02s of "speech" at 0.014
RMS** and was still transcribed correctly — a short, quiet, plosive-heavy word barely
registers on a 20ms-frame energy metric. It survives the gate only because of the `AND`:
its RMS is above the floor. **This is why the conjunction matters** and why a duration-only
gate — the original proposal — would have eaten a real word. Do not simplify B1 back into
a single condition.

Expected effect over all four calls: B1 removes 4 of 7 filler utterances with no false
positives; B2 removes the remaining 3. Neither touches any of the 60 real utterances.

> **Measured, over all five calls and all 78 clips** (`python -m stt_port.replay`): B1 removes
> **6 of 10** filler utterances and one real one; B2 removes the remaining **4**. 67 of 68 real
> utterances are untouched. Better than predicted on filler, and not free, as the correction
> above explains.

### C. Trim trailing silence before inference — *moderate, and not for the reason it appears*

Every chunk carries a ~0.50s silent tail — exactly `HANGOVER_MS` — which works out to **29%
of all audio handed to the model**.

The obvious argument for trimming it is speed, and that argument is wrong: Whisper pads
everything to a 30s window, so a shorter clip does not decode faster. Measured directly, the
two filler clips took 1310ms untrimmed and 1303ms trimmed — no difference.

The real argument is that **the silent tail is what degenerate decodes latch onto**. The
looping clip went from 4132ms/`eight` untrimmed to 1285ms/`8` trimmed — a 3.2× improvement,
entirely from not entering temperature fallback. Trimming does not make normal decodes
faster; it makes pathological ones less likely.

Keep the hangover for *endpointing* — it is what decides the utterance has ended — and trim
it only from the buffer handed to the model.

### D. Normalise formatting after inference — *cosmetic, cheap*

A post-processing pass over each transcript: consistent casing, a single numeral convention,
stripped trailing commas. Prefer this to `initial_prompt`, which nudges the decoder without
guaranteeing anything and adds tokens to every decode.

If the vocabulary is ever known in advance — names, street names, a fixed command set —
`initial_prompt` becomes worth revisiting, since it is the only cheap lever on *recognition*
rather than presentation.

### E. Decide the hangover trade-off deliberately — *depends on the consumer*

Defect 4 (sentences fragmenting across utterances) has no free fix, only a choice:

| `--hangover-ms` | Effect |
|---|---|
| 500 (current) | Sentences split at natural pauses; lowest latency |
| ~800–1000 | Most mid-sentence pauses survive; adds that delay to *every* utterance |

Since ~500ms of endpoint lag is already baked in before processing starts, raising it to
1000ms makes the system noticeably less responsive for a live conversation. **Leave it at 500
if a human is reading the feed; raise it if a parser is consuming whole sentences.**

The cheaper alternative is to stitch afterwards rather than wait longer: a fragment beginning
with a lowercase word and following the previous one within ~1s is almost certainly a
continuation. The model's own casing supplies the signal, as noted in defect 4. This keeps
latency where it is and can be done entirely downstream.

### F. A high-pass filter — *low value, listed for completeness*

There is measurable energy below 100Hz (-21.3 dB) that is outside the voice band and cannot
help recognition. A one-line high-pass at ~80Hz before resampling is nearly free.

Expect no accuracy change. The energy is well below the speech band and Whisper's mel
front-end largely ignores it. Do this only if a future line turns out to carry mains hum;
it is not indicated by anything measured here.

## Explicitly not recommended

- **Noise reduction / spectral subtraction / RNNoise.** At 37.5 dB SNR there is nothing to
  remove, and denoisers introduce artifacts that Whisper handles worse than the original
  noise. This is the intuitive fix for "phone audio" and it is the wrong one here.
- **Automatic gain control.** Levels are already healthy (0.11 RMS speech, peak 0.98) and
  AGC would pump the noise floor up during pauses — directly worsening defect 2.
- **Upsampling earlier, or a higher-rate link.** The information above 3.4kHz was destroyed
  by the codec before it reached this machine. Resampling cannot restore it, and the current
  design already resamples once per utterance off the realtime path.
- **Fine-tuning on narrowband audio.** Plausible eventually, but unjustified while 19/20
  digits are already correct and the top three defects are all decode-side plumbing.
- **Filtering on decoder confidence** (`no_speech_prob`, `avg_logprob`,
  `compression_ratio`). Measured above and it does not separate filler from short real
  speech on this data — `no_speech_prob` is 0.000 even for pure hallucination. This is the
  most natural-looking fix in the whole list and it is empirically a dead end.

## The gap that is not about accuracy at all

Both live calls stalled on the same thing: **the caller has no way to receive a response.**
Testing repeatedly reached "let me know when you're ready" with no channel to answer on,
because transcripts return as JSON to the phone machine's terminal, not to the handset.

The receiving half already exists. `Call.send()` (`phone/audiosocket.py:219`) is implemented
and documented to play PCM16 8kHz down the line, and `phone/session.py:173` ignores inbound
binary with the comment *"Reserved for synthesized speech. Nothing sends it yet."* The
missing piece is entirely on the stt side: synthesize to 8kHz PCM16 and send it as a binary
WebSocket message.

This is out of scope for transcript *quality* and is likely the highest-value next feature in
the project, because without it the system cannot be exercised conversationally by the person
holding the handset.

## Suggested order

1. **A** (bound decode cost) — biggest reliability win; a 20s stall makes the system look
   broken even when every transcript is correct
2. **B** (suppress filler) — biggest visible quality win, ~12% of utterances
3. **C** (trim tails) — reduces how often A has to fire
4. **D** (normalise formatting) — cosmetic, but cheap
5. **E** (hangover) — only once it is known what consumes the transcripts
6. **F** (high-pass) — only if a future line turns out to need it

A–D together should be well under a hundred lines. The captured clips in `/tmp/dictate_*.wav`
plus `--benchmark` make each one verifiable offline against the exact audio that provoked
the defect, with no phone call required.

> The line estimate was optimistic: A–D came to ~350 added lines across four modules, roughly
> half of it comment and docstring. The logic really is small — `filler_reason` is eight lines
> — but four CLI flags, a watchdog class, a numeral converter and the reasoning behind each
> threshold are not. The offline-verification claim held exactly as written.

**Do not implement B without keeping the captured clips.** Its thresholds are calibrated to a
sample of 43 utterances, one of which already falsified an earlier version of the rule. The
regression test for this work is the clip set, not a unit test with synthetic audio.

## Implementation notes

Everything above is *what* and *why*. This section is *where*, and exists so this work can be
picked up cold without re-deriving the pipeline.

### The path a chunk takes

```
phone machine
   │  binary WebSocket frames (PCM16 LE 8kHz, 20ms)
   ▼
stt_port/server.py   segmenter closes a chunk on silence
   │                 server.py:243  queue.put((chunk, WIRE_RATE, closed_at, call_id))
   ▼
   Queue  (crossing from the asyncio thread to the worker thread)
   │
   ▼
stt_port/main.py     consume_chunks()  — the worker loop
   │                   q.get()
   │                   save_debug_wav()      if --debug-save-wav
   │                   audio.resample_to_target()   8k → 16k
   │                   backend.transcribe()  ← the expensive call
   │                   emit_transcript()     if text is non-empty
   │                   sink(call_id, record) ← ALWAYS, even when record is None
   ▼
stdout + transcript sent back over the same socket
```

### Where each change goes

| Change | Location |
|---|---|
| **A1** temperature ladder | `MLXWhisperBackend.transcribe`, `stt_port/backends.py:145` — the `mlx_whisper.transcribe(...)` call, which currently passes only `language` and `condition_on_previous_text` |
| **A2** inference watchdog | `consume_chunks`, around the `backend.transcribe(target)` call in `stt_port/main.py` |
| **B1** energy/duration gate | `consume_chunks`, after `q.get()` and **before** `backend.transcribe` — see the hazard below |
| **B2** post-`call_end` drop | same place, but needs plumbing — see below |
| **D** formatting normalisation | `emit_transcript` in `stt_port/main.py`, so stdout, the JSONL record and the returned copy all agree |
| **E** hangover | `HANGOVER_MS`, `stt_port/audio.py:24`, already exposed as `--hangover-ms` |

### The hazard: do not drop a chunk before the queue

The obvious place to gate a chunk is `server.py:243`, before it is ever queued. **Do not.**
`consume_chunks` documents why:

> The sink is called for every chunk, with `record=None` for one that transcribed to
> nothing. [...] a call whose last utterance was line noise would otherwise hold its socket
> open waiting for a transcript that never comes.

The drain path (`FINAL_DRAIN_TIMEOUT`, `server.py:264-281`) waits on an event that is only
set when the sink is called for a chunk. A chunk filtered out before the queue never reaches
the sink, so the event never fires and the socket hangs until the 5s timeout on **every**
call whose final chunk is a hook click — which is every call.

**Gate inside `consume_chunks` instead**, after dequeue, by skipping the `transcribe` call and
letting `text` be empty. That reuses the existing empty-transcript path, which already calls
`sink(call_id, None)` correctly and already prints `[skipped: empty transcript]` under
`--verbose`. The saving is the same — the expensive call is `backend.transcribe`, not the
queue hop.

### B2 needs a signal that does not exist yet

`consume_chunks` receives `(chunk, rate, closed_at, call_id)`. Nothing in that tuple says
whether the chunk was flushed by `call_end`. Either extend the tuple at `server.py:243`, or
have the server mark the call as ending and let the worker consult it. The tuple is the
smaller change and keeps the worker source-agnostic, which `consume_chunks` is deliberate
about — the mic source pushes the same shape.

### How the thresholds were measured

B1's numbers are meaningless without the metric that produced them. **Speech duration** is
the count of non-overlapping **20ms frames** whose RMS exceeds **0.01**, times 0.02.
**Mean RMS** is the mean over those frames only, not the whole clip. Both are computed on the
raw 8kHz audio, before resampling.

```python
h = sr // 50                                    # 20ms hop at 8kHz = 160 samples
v = np.array([np.sqrt((x[i:i+h]**2).mean()) for i in range(0, len(x)-h, h)])
loud = v[v > 0.01]
speech_seconds = len(loud) * 0.02
mean_rms       = loud.mean() if len(loud) else 0.0
```

Implement B1 against *this* definition or recalibrate the thresholds against whatever
replaces it. A different frame size or floor moves the numbers.

### The regression set

`samples/` holds **78 clips** (gitignored — audio, and 1.5MB), plus:

- **`samples/MANIFEST.csv`** — one row per clip: `clip, call_id, time, after_call_end,
  speech_s, speech_rms, transcript`. The `after_call_end` column is precomputed, so B2 can be
  evaluated offline without re-parsing anything.
- **`samples/server.log`** — the raw server output for all five calls, including every
  `inference=` timing and the `[ws] call ... ended` lines.

This is the regression set the plan is calibrated against. Verify changes by replaying clips
through the backend and diffing against the `transcript` column — the filler rows are the
ones that should change and nothing else should.

`stt_port/replay.py` does exactly that:

```sh
./.venv/bin/python -m stt_port.replay              # gates only, no model, instant
./.venv/bin/python -m stt_port.replay --transcribe # also decode, and diff every transcript
./.venv/bin/python -m stt_port.replay --clip 001834_596 --transcribe   # one clip
```

It takes the same `--min-speech-ms` / `--min-speech-rms` flags as the server, so a threshold
can be re-fitted against the clips in seconds. Any real utterance appearing in its suppressed
list other than the known 0.02s exclamation is a regression.

Ground truth for the scored passages lives in the accuracy tables above; the reference text
was read deliberately for that purpose and is not recoverable from the clips alone.

### Do not break the existing tests

Run from the repo root, as module paths. `unittest discover` does **not** work here — it
fails on import, because the tests import their package:

```sh
# this side only — 59 tests (24 before this work)
.venv/bin/python -m unittest stt_port.test_server

# both packages — 97 tests, all passing as of 2026-08-15 (62 before this work)
.venv/bin/python -m unittest stt_port.test_server phone.test_audiosocket phone.test_session
```

`stt_port/test_server.py` covers segmentation at 8kHz, the WebSocket contract, and the drain
behaviour that the hazard above concerns. But note the limit: a B1 implementation that filters
in the wrong place will most likely **still pass all 62 tests** and fail on a live call, since
the drain path only misbehaves when a real socket is waiting on it. Green tests are not
sufficient evidence for that change — replay the clips, and make a real call.

The 35 tests added with A–D cover the gates at their measured boundaries, the watchdog, the
trimming and the normaliser — and, specifically, that a gated chunk still reaches the sink
(`TestGatedChunksStillReachTheSink`). That last one is the hazard above written down as an
assertion, but it is still a stub sink rather than a socket: **it remains true that only a
real call proves this.**

## Implementation status

A–D are implemented; E and F are not, deliberately.

| Change | Where it landed | Measured afterwards |
|---|---|---|
| **A1** ladder | `TEMPERATURE_LADDER` in `stt_port/backends.py`, passed to both Whisper backends | with C, the pathological clip decodes in **1291ms**, down from 22433ms |
| **A2** watchdog | `InferenceWatchdog` in `stt_port/main.py`, `--inference-timeout` (default 4s) | no clip in the set exceeds the ceiling; worst decode is 1462ms |
| **B1** energy gate | `filler_reason`, `--min-speech-ms` / `--min-speech-rms` | 6 of 10 filler suppressed, 1 real utterance lost |
| **B2** hangup click | `final` flag on the queue tuple, set by `_end_call` in `server.py` | 4 of 4 terminal clicks suppressed, no false positives — but see the correction: insufficient on its own |
| **B3** continuity floor | `--min-speech-floor-ms` (130), added after B2 was defeated live | catches every click that closes on silence before hangup |
| **C** trim tails | `audio.trim_trailing_silence`, applied before resampling | normal decodes unchanged (~1.3s), as predicted |
| **D** normalisation | `normalise_transcript`, applied in `emit_transcript`; `--numerals` | casing and trailing commas consistent; numeral conversion opt-in |
| **E** hangover | not implemented — still depends on what consumes the transcripts | — |
| **F** high-pass | not implemented — nothing measured here indicates it | — |

Two decisions worth recording, because neither is what the plan above proposed:

**A2 cannot do what this document claimed it would.** "Abandon that utterance and log it rather
than let it hold the worker" is not achievable in-process: a decode already running cannot be
cancelled, so the abandoned one keeps the GPU and the next one queues behind it. What the
ceiling actually guarantees is that the *pipeline* stops waiting — the garbage never reaches
the transcript, the hangup drain path is never held by an unbounded decode, and an overrun
appears in the log instead of as a system that has silently died. **A1 and C are what bound the
wall time.** A true hard bound needs the model in a separate process that can be killed, which
costs a full model reload on every kill; not worth it while A1 and C hold.

**D returns the continuation hint rather than destroying it.** Capitalising every utterance
would erase the lowercase leading word that defect 4 identifies as the reassembly signal, so
`normalise_transcript` returns it separately and it rides along as `continues_previous` on the
record and on the wire. Whatever eventually implements E's downstream stitching gets the signal
for free.

**Numeral conversion is off by default** (`--numerals asis`). It is right for a parser and wrong
for a reader — it renders `five dozen liquor jugs` as `5 dozen liquor jugs` — and since E is
already blocked on knowing what consumes the transcripts, so is this.

### Verified on live hardware, 2026-08-15

Four calls through a real HT801 after A–D shipped, reading a fixed script. The first call went
to a stale server still running the old code, which turned into a matched before/after on
identical text.

| Check | Before | After |
|---|---|---|
| Six one-word answers | all transcribed | all transcribed, **none gated** — B1's false-positive risk did not materialise |
| Mid-sentence pause | `Thank you.` hallucinated into it | `[gate] near-silence`, nothing printed |
| Sentence read straight through | one utterance | one utterance |
| `fifty` / `fifteen` | `50 or 15` | `50 or 15` — no regression |
| Invoice line | full, with `4729`/`613`/`Sarah Fitzgerald` | same |
| Hangup mid-sentence (×2) | — | tail transcribed, socket closed immediately, gate silent |
| Terminal `Thank you.` | on **every** call ever recorded | **gone** |

The last row took three attempts and is the origin of the fourth correction above. The final
confirming call ended like this — three real answers, three suppressed fragments of one
deliberately clumsy hangup, and nothing after the call ended:

```
[12:45:01] Yes.
[12:45:04] No.
[12:45:07] Eight.
[gate] dropped chunk before inference -- no continuous speech (longest run 0.02s < 130ms, at any volume)
[gate] dropped chunk before inference -- no continuous speech (longest run 0.08s < 130ms, at any volume)
[gate] dropped chunk before inference -- no continuous speech (longest run 0.12s < 130ms, at any volume)
[ws] call 8d803f90 ended (hangup) blocks=777 (15.5s)
```

Across all 38 clips captured on those calls: 8 dropped, every one of them filler or a hangup
fragment, and all 30 real utterances kept.

### What the clip set says now

`python -m stt_port.replay --transcribe` re-decodes every clip the gates keep and diffs against
the manifest. 17 of 67 transcripts changed; **none lost content**. The changes are the same
punctuation and numeral drift defect 3 describes (`six` → `Six.`, `13` → `13.`), one
improvement (`baltimore` → `Baltimore.`), and the headline fix (`8 check check check…` → `8`).
The manifest is deliberately *not* regenerated: it records what provoked each defect, and
overwriting it would erase the evidence these thresholds were fitted to.

## Reproducing the measurements

```sh
# capture a call's audio, one wav per closed utterance
./.venv/bin/python -m stt_port.main --source ws --verbose --debug-save-wav

# afterwards, per clip: total duration, above-threshold speech duration, RMS
# pair each against the transcript it produced in the server log
```

The two things worth re-measuring on any new line or ATA are the **noise floor** (does the
threshold still have its 5× margin?) and the **speech/filler duration boundary** in defect 2,
since the gate in change B is calibrated to it.
