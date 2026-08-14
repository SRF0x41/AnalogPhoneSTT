# phone — the telephony machine

Runs on the Linux box with the HT801 and Asterisk attached. It takes each answered call's
audio from Asterisk, forwards it to the [`stt_port`](../stt_port/) machine, and receives the
transcripts back — so the text of a phone conversation lands *here*, on the machine that
owns the call.

```
HT801 ── Asterisk ──AudioSocket (localhost TCP)──▶ python -m phone
                                                       │   ▲
                                       audio (binary)  │   │  transcripts (JSON)
                                                       ▼   │
                                                   stt machine
   python -m phone --call ── ARI ──▶ Asterisk
```

**No SIP, no RTP, no resampling, no audio processing.** Asterisk terminates SIP, negotiates
codecs, absorbs jitter and decodes to raw samples one process away; this package reads 20ms
frames off a socket and moves them. That's why `requirements.txt` is two lines.

## Files

| | |
|---|---|
| `audiosocket.py` | the AudioSocket protocol: frame codec, and a `Call` object with `async recv()` / `send()` / `hangup()` |
| `session.py` | one call's bridge — audio out to the stt machine, transcripts back to a local sink |
| `originate.py` | ring the handset, via Asterisk's REST interface (ARI) |
| `config.py` | addresses and credentials; every value overridable from the environment |

## Setup

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r phone/requirements.txt
```

Then apply the Asterisk config — both need root, and neither is done for you:

- [`asterisk/extensions.conf.sample`](../asterisk/extensions.conf.sample) — the dialplan.
  Hands calls to this package instead of to the old pyVoIP SIP endpoint.
- [`asterisk/ari.conf.sample`](../asterisk/ari.conf.sample) — only needed for `--call`.

```sh
sudo cp ... /etc/asterisk/extensions.conf     # merge it, don't clobber
asterisk -rx "dialplan reload"
```

## Running

```sh
python -m phone                        # bridge calls; transcripts print here
python -m phone --echo                 # hear yourself -- no stt machine involved
python -m phone --call                 # ring the handset, then bridge as usual
python -m phone --jsonl                # one JSON record per transcript, for piping
python -m phone --stt-url ws://host:9099/
```

Asterisk connects to *us*, so this is a server in every mode: start it first, then lift the
handset. It stays up across calls.

**Start with `--echo`.** It proves the entire media path — Asterisk answered, connected,
picked a codec, decoded to slin, and your frames land back in your own ear — without the
network or the stt machine being involved at all. If echo works and transcripts don't, the
problem is on the link or the Mac, and you've halved the search space.

## The AudioSocket link

Asterisk's `AudioSocket()` dialplan application connects **out** to a TCP server and streams
the channel's audio over it in both directions for the life of the call. One connection is
one call.

```
[kind: 1 byte][length: 2 bytes, big-endian][payload]
```

| kind | | |
|---|---|---|
| `0x00` | hangup | we send it to end the call; Asterisk sends it when the call ends |
| `0x01` | UUID | 16 raw bytes, first frame — this is `${UUID()}` from the dialplan |
| `0x03` | DTMF | one ASCII digit the caller pressed |
| `0x10` | audio | slin: PCM16, mono, 8kHz — 320 bytes per 20ms frame |
| `0xFF` | error | one-byte Asterisk-side error code |

Two things are easy to get backwards:

- **The length is big-endian** (Asterisk packs it with `htons`) but **the audio payload is
  little-endian**, because Asterisk's slin is host byte order and it memcpys the frame out
  without swapping. So one frame carries both orders, deliberately.
- **8kHz is not a limitation to fix.** The dialplan application is fixed at slin 8kHz, which
  is the analog line's own rate. Upsampling here would double the bytes on the LAN without
  adding information; the stt machine lifts each closed utterance to 16kHz instead, once,
  off the realtime path.

Verified against `/usr/include/asterisk/res_audiosocket.h` and `res_audiosocket.c` on
Asterisk 23.4.1 — `phone/test_audiosocket.py` asserts the constants so a typo can't turn
into audio that silently never arrives.

## The link to the stt machine

One WebSocket per call, opened by us. Binary messages are audio (PCM16 LE 8kHz), text
messages are JSON:

| direction | type | fields |
|---|---|---|
| → stt | `call_start` | `call_id`, `rate`, `direction` |
| → stt | `call_end` | `reason` |
| ← stt | `transcript` | `call`, `text`, `dur_ms`, `latency_ms` |

Binary from the stt machine is reserved for synthesized speech to play down the line.
Nothing sends it yet; `Call.send()` is the hook that would write it to Asterisk.

**Who closes the socket, and why it matters.** `call_end` is not the end of the
conversation. Only hangup flushes the utterance the caller was still mid-way through, so the
last transcript of every call is produced *after* `call_end` — which means we send
`call_end` and then keep reading. The stt machine closes the socket once it has sent that
final transcript, and that close is what ends the call here.
`FINAL_TRANSCRIPT_GRACE_SECONDS` is only a backstop for a far side that never closes, and is
deliberately longer than the stt machine's own `FINAL_DRAIN_TIMEOUT` so that side wins the
race. Closing here on `call_end` instead would throw away the sentence most worth having.

**Audio is queued, and dropped rather than waited on.** Frames go to the stt machine through
a bounded queue (`OUTBOUND_QUEUE_FRAMES`, one second) drained by its own task. If the far
side stops reading while its connection stays open, frames are dropped and counted
(`dropped=` in the per-call summary) instead of the send blocking — a stalled write would
reach back through the read loop into the AudioSocket, and a socket nobody reads backs up
into Asterisk. A 20ms hole is spliced out and transcribes through; a stalled call does not.

**The stt machine being down never drops a call.** A refused connection is logged once and
the call continues untranscribed — someone is mid-sentence on the handset, and a dead
transcriber is not a reason to hang up on them. The audio pump keeps draining either way,
because a socket nobody reads is a socket that backs up into Asterisk.

## Testing

```sh
.venv/bin/python -m unittest phone.test_audiosocket phone.test_session -v
```

33 offline tests — no Asterisk, no phone, no stt machine, no network beyond loopback:

- frame kinds against the values in `res_audiosocket.h`, big-endian lengths, round-trips,
  truncated headers, oversized payloads
- the call lifecycle: UUID before audio, hangup, a socket closing without a hangup frame,
  DTMF, error frames, and an unknown frame kind not desynchronizing the stream
- oversized sends splitting into whole *sample-aligned* frames
- the bridge: audio arriving as binary, `call_start` announcing 8000 not 16000, transcripts
  reaching the local sink, DTMF not being forwarded as audio
- the stt machine refusing the connection, and dying mid-call — neither ends the call

Not covered, because it needs the hardware: Asterisk, the dialplan, the HT801, and ARI. Run
`--echo` and lift the handset for that.
