# How the networking works — *superseded, kept as a retrospective*

> **This describes a design that was never in this repo.** It is carried over from
> `RedPhone`, this project's predecessor, and documents "RPA1": a hand-rolled UDP
> protocol that carried call audio from a pyVoIP process on the phone machine to a
> UDP receiver on the stt machine. None of the code it names (`main.py`,
> `redphone_sender.py`, `dictate/protocol.py`, `dictate/net.py`) exists here; it is
> readable at <https://github.com/SRF0x41/RedPhone>. The document keeps that
> project's naming throughout, since that is what things were called at the time.
>
> **What replaced it, and why.** Asterisk was already running on the phone
> machine and can hand call media to a local process over AudioSocket, so
> terminating SIP and RTP a second time in Python was solving a problem that had
> already been solved one process away. And a WebSocket between the two machines
> supplies framing, a message-type discriminator, ordering and liveness — the
> four jobs the 16-byte RPA1 header existed to do — while also being
> bidirectional, which RPA1 never was. §8 below ("why the format is written
> twice") describes a drift hazard that simply stops existing once there is no
> hand-written format.
>
> **Why it's still here.** Two things in it outlived the design:
>
> - §3 and §5 on what packet loss does to *speech*, in particular that a dropped
>   frame must be spliced, never padded with silence — inserted silence reads to
>   the segmenter as an endpoint and chops the utterance in half. That constraint
>   applies to any transport.
> - §7's debugging table and the observation that a UDP sender cannot distinguish
>   success from total failure, which is why "the sender printed no errors" was
>   never evidence of anything.
>
> Read the rest as history.

RedPhone runs on two machines that talk over the LAN. This document explains
that link from the bottom up. It assumes you can read the code but have never
had to think about sockets, ports, or packet loss — every networking term is
defined the first time it shows up, and every design decision is tied back to
something concrete in this codebase.

If you want the summary and the CLI, that's [README.md](README.md). This file
is the *why*.

---

## 1. The problem

The phone hardware and the transcription model can't live on the same machine.
The HT801 and Asterisk are wired to the Linux box (`192.168.50.1`); the
Whisper model runs on the Apple Silicon box (`192.168.50.120`), because that's
where the GPU that runs it fast enough is. Audio comes in on one machine and
has to be turned into text on the other.

So a call's audio has to cross a network. That's the whole thing this document
is about: 20ms at a time, ~50 times a second, for the length of a call.

```
        192.168.50.1                              192.168.50.120
  ┌──────────────────────────┐              ┌────────────────────────┐
  │  HT801 ── Asterisk ── SIP│              │  NetworkSource         │
  │            │             │   UDP :9099  │    │                   │
  │      CallAudioStream ────┼─────────────▶│  Segmenter             │
  │            │             │  PCM16 20ms  │    │                   │
  │      redphone_sender.py  │   frames     │  Whisper (MLX)         │
  └──────────────────────────┘              │  → stdout              │
                                            └────────────────────────┘
```

The receiving code lives on the `stt-port` branch, which is that machine's
checkout of this same repo (`git show origin/stt-port:dictate/net.py`). Two
machines, one repo, two branches.

---

## 2. Vocabulary, in the order you need it

**IP address.** A number identifying a machine on a network — `192.168.50.1`.
The `192.168.x.x` range is *private*: those addresses only mean anything
inside this LAN, and the internet won't route them. Both machines are on the
same wired switch, which is why this design gets to make optimistic
assumptions later.

**Port.** An IP address gets a packet to a *machine*; a port gets it to a
*program on that machine*. It's a 16-bit number (1–65535), so an address like
`192.168.50.120:9099` means "the program listening on port 9099 over there."
Ports below 1024 need root; 9099 is arbitrary and unprivileged. Asterisk is
already using 5060 (SIP) and 10000–20000 (RTP) on the other box, which is why
`main.py` binds SIP on 5061 — two programs cannot hold the same port.

**Socket.** The OS object you read and write to do any of this — the network's
file descriptor. You create one, optionally *bind* it to a local port, and
then send to or receive from addresses. Both `redphone_sender.py:170` and the
receiver are, at bottom, one socket and a loop.

**Packet / datagram.** Networks don't move streams, they move discrete chunks.
A datagram is one such chunk: a self-contained message with a destination
written on it, delivered whole or not at all. This matters more than it
sounds — see §4.

**MTU** (Maximum Transmission Unit). The largest a packet can be before the
network has to chop it into pieces to carry it. On Ethernet it's 1500 bytes.
Exceed it and IP *fragments* your packet into several, then reassembles them
on the far side — and if any one fragment is lost, the whole original packet
is lost. Staying under the MTU is therefore a reliability decision, not a
performance one. Our datagrams are 656 bytes.

---

## 3. Why UDP and not TCP

These are the two ways to move data over IP, and the choice drives everything
else in the design.

**TCP** is the one you've used without noticing — it's under HTTP, SSH, and
every socket library that looks like a file. It gives you a *reliable ordered
byte stream*: you write bytes, the same bytes come out the other end, in
order, with nothing missing. It achieves that by numbering everything,
acknowledging what arrived, and **retransmitting what didn't**.

**UDP** gives you almost nothing: you hand the OS a datagram and a
destination, and it makes a best effort. No connection, no acknowledgements,
no retransmission, no ordering guarantee. Datagrams can be lost, duplicated,
or delivered out of order, and you will not be told.

TCP sounds strictly better. For this workload it is strictly worse, and the
reason is *latency*, not throughput.

Consider a frame lost mid-call. TCP guarantees delivery by holding everything
behind it until the missing piece is retransmitted and arrives — a round trip
later. But the ordering guarantee is exactly the problem: frames 51, 52, 53
have already arrived and are sitting in a buffer the application isn't allowed
to see yet, because frame 50 is missing and TCP promised to deliver in order.
This is **head-of-line blocking**. By the time frame 50 finally arrives, the
audio moment it belonged to is long past. It gets played late, and so does
everything queued behind it.

The audio consequence is what settles it:

- **UDP, frame lost:** 20ms gap. The frames either side are spliced together.
  At normal speech rates that's inaudible, and Whisper transcribes right
  through it.
- **TCP, frame lost:** no gap, but every subsequent frame is delayed by a
  round trip, and the delay never recovers. The listener hears a stall.

A retransmitted frame of real-time speech arrives after it was useful. So we
choose to lose it instead, and the receiver is written to expect gaps. That is
the entire justification for the wire format in §4 — with TCP's guarantees
gone, we have to detect the problems ourselves.

The same reasoning kills the **jitter buffer**, the other thing you'd normally
add. That's a deliberate delay (say 60ms) on the receiving side so that
slightly-late packets still arrive in time to be played in order. It trades
latency for smoothness. But this is a wired LAN with one switch between the
machines, where timing variance is microseconds; we'd be paying a fixed
latency cost on every utterance to smooth out a problem this link doesn't
really have. So there isn't one.

---

## 4. The wire format, and why bytes need a header at all

My first attempt at this sent nothing but raw PCM16 samples — no header. It
would have failed completely, and the failure mode is worth understanding
because it's characteristic of UDP.

Once you accept lost, duplicated, and reordered datagrams, look at what the
receiver actually gets: 640 anonymous bytes. It cannot answer any of:

- Is this even ours? *Any* program on the LAN can send to port 9099. The
  socket accepts whatever arrives; a stray broadcast is a normal input, not an
  error.
- Where does this belong in the audio? Datagram 51 arriving after 52 is
  indistinguishable from 51 arriving on time, if all you have is samples.
- Is this a new call? Two calls back to back are one continuous byte soup
  without a boundary marker.
- Did anything go missing? Losing a frame is undetectable if frames aren't
  numbered — you'd just get slightly shorter audio and never know.

So each datagram carries a 16-byte header before its payload:

| offset | size | field | purpose |
|---|---|---|---|
| 0 | 4 | magic `RPA1` | "this is ours" — everything else is discarded |
| 4 | 1 | msg_type | audio `0x01`, call-start `0x02`, call-end `0x03`, heartbeat `0x04` |
| 5 | 1 | flags | reserved, 0 |
| 6 | 2 | reserved | reserved, 0 |
| 8 | 4 | call_id | random per call; groups frames belonging to one call |
| 12 | 4 | seq | frame counter, restarts at 0 each call |

Defined in `dictate/protocol.py` on the `stt-port` branch; `redphone_sender.py`
on this machine inlines a byte-identical copy so it can be one deployable file
(see §8).

A few details that are easy to get wrong:

**Network byte order.** The header packs with `struct.pack("!4sBBHII", ...)`,
where `!` means big-endian: the most significant byte first. Different CPUs
store multi-byte integers in different orders internally (x86 is little-endian,
so it writes `0x01020304` as `04 03 02 01`), and if the two machines disagree
you get plausible-looking garbage rather than an error. Every network protocol
picks one order and states it; the convention is big-endian, hence "network
byte order." **The audio payload is little-endian** (`<i2` on the receiving
side) because that's what PCM16 is everywhere — so this one datagram has both
byte orders in it, deliberately, and each is explicit rather than inherited
from whatever the CPU does.

**Why the magic number.** Four bytes of `RPA1` at a fixed offset is a cheap
filter against every other thing that might hit an open UDP port. `unpack()`
returns `None` rather than raising for anything that fails it — malformed
input is expected here, not exceptional. The receiver tallies them and prints
the count once at shutdown, so a chatty neighbour on the LAN doesn't spam
stderr but also doesn't go unnoticed.

**Why `call_id` is random, not sequential.** If the receiver restarts
mid-call, it comes up with no memory of what it was doing. Random ids mean the
next call can't collide with the one it was already tracking; a counter
restarting at 0 could.

**Why `seq` restarts each call.** It makes gap detection trivial arithmetic
(`seq - expected`) with no wraparound reasoning: a call would have to run for
years of 20ms frames to exhaust a uint32.

**Sizing.** A 20ms frame at 16kHz mono PCM16 is 320 samples × 2 bytes = 640
bytes, plus the 16-byte header = **656 bytes on the wire**, comfortably under
the 1500-byte MTU, so a frame is never fragmented. `split_payload()`
(`redphone_sender.py:90`) exists only so that an unusually large frame gets
split into several *whole datagrams* rather than being fragmented by IP — the
difference being that losing one of our pieces costs one piece, while losing
one IP fragment costs the entire original packet. The split is at an even
offset so pieces stay sample-aligned; splitting an int16 down the middle would
turn the rest of the frame into noise.

---

## 5. What the receiver does with all this

Given the header, the receiver (`dictate/net.py` on `stt-port`) can handle
each failure mode explicitly.

**Loss.** `seq` jumps from 50 to 52, so frame 51 is gone. The gap is left as a
gap — frames 50 and 52 are spliced directly together. It is deliberately *not*
padded with 20ms of silence, which is the obvious-looking fix and is
backwards: silence is precisely the signal the segmenter uses to decide an
utterance has ended, so padding a dropped packet with silence would chop a
sentence in half mid-word. The count is reported per call
(`frames=… lost=… late=…`). On a wired link `lost` should be 0, and a number
that isn't is worth chasing before blaming the transcripts.

**Late and duplicate frames.** `seq` goes *backwards*. Its position in the
audio has already been played past, so it's dropped and counted. Reordering it
in isn't possible without a jitter buffer, which §3 already declined.

**A call ending without saying so.** `CALL_END` is a single datagram, so it
can be the one that's lost — and the sender can also be killed mid-call, in
which case nothing is sent at all. Either way the receiver would sit holding a
half-finished utterance forever. The fix is a timeout: 2 seconds with no audio
*at the transport level* (not the audio level — actual silence on the line
still arrives as datagrams full of quiet samples) means the call is over, and
the open utterance gets flushed and transcribed rather than dropped.

**Audio for a call it never saw start.** The `CALL_START` was lost, or the
receiver was started mid-call. The audio is real either way, so it's adopted
as a new call and logged as `(mid-stream)` rather than discarded.

Notice the shape of all four: the sender is never trusted or asked to
retransmit. Every recovery is unilateral, on the receiving side, from
information in the header.

---

## 6. Silence is ambiguous: the heartbeat

Here's a problem with no equivalent in TCP. Nothing is arriving on the socket.
Does that mean:

1. No call is in progress, everything is fine; or
2. The sender crashed, the switch died, someone unplugged a cable?

With UDP there is no connection to be "up" or "down" — there's no handshake at
the start and no notification when the other side goes away, because there's
nothing that knows the other side exists. An idle socket and a dead link look
identical.

So the sender sends a `HEARTBEAT` datagram once a second while idle
(`redphone_sender.py:101`). Silence on the socket now genuinely means the link
is broken. During a call the heartbeat pauses — the audio is already proving
liveness 50 times a second, and a keepalive alongside it would be noise.

This is the standard shape of the answer whenever you need "is the other side
alive?" over a connectionless protocol: you can only learn it from traffic
that actually arrives, so you arrange for some.

---

## 7. Failure modes you'll actually hit, and how to tell them apart

The link's defining property when debugging: **the sending side cannot tell
success from total failure.** `sendto()` on a UDP socket hands the datagram to
the kernel and returns. There's no connection to refuse, no acknowledgement to
wait for. If the receiving machine is off, unplugged, firewalled, or listening
on a different port, `sendto()` still succeeds — the packets go out and
nothing comes back, because nothing was ever supposed to come back.

That's why "the sender printed no errors" proves nothing on its own, and why
verification requires reading the *receiver's* console.

| Symptom | Likely cause | How to check |
|---|---|---|
| Sender fine, receiver totally silent | wrong IP, receiver not running, or a firewall dropping UDP 9099 | `ss -ulnp` on the receiver — is anything bound to 9099? |
| Receiver logs "ignored N datagrams that weren't ours" | something is arriving but failing the magic check — a version skew between the two copies of the format, or another program on the LAN | compare `redphone_sender.py` here against `dictate/protocol.py` there |
| `lost=` climbing on a wired LAN | genuinely unusual; suspect a receive buffer overrun or a saturated link | the receiver sets a 1MB `SO_RCVBUF` for this reason |
| Call never ends, no transcript | `CALL_END` lost *and* the idle timeout disabled | the receiver's `--idle-timeout` (default 2s) |

A note on **ping**: ICMP (what `ping` uses) is a different protocol from UDP,
and hosts routinely block it while happily accepting UDP — macOS in particular.
So a failed ping is weak evidence at best. `tcpdump -i any udp port 9099` on
either end is the real answer: it shows you the actual packets, or their
absence, without either program's opinion about it.

Also worth knowing: firewalls treat inbound UDP more suspiciously than TCP,
precisely because there's no connection state to evaluate. If the receiver's
machine has its firewall on, port 9099 needs to be allowed there explicitly.

---

## 8. Why the format is written twice

`dictate/protocol.py` (on `stt-port`) defines the format. `redphone_sender.py`
(here) contains a copy of the same constants and `pack()` functions rather
than importing them.

That's a deliberate trade. The sender has to run on this machine, next to
Asterisk, where the `dictate` package and its dependencies don't exist and
shouldn't have to — so it's one self-contained file with nothing but the
stdlib and the pyVoIP that RedPhone already needs. The cost is a copy that
could drift, and a drifting wire format is an unusually nasty bug: it doesn't
crash, it just makes the receiver silently discard everything.

So the copy is checked. `test_net.py` on the other machine asserts that both
implementations pack byte-identical output, and `test_audio.py` here asserts
our copy against the documented layout independently. **Keep
`redphone_sender.py` verbatim** — it was copied from the branch unmodified,
which is what lets the other machine's test cover this file. Change it here
and that check silently stops applying to the copy that's actually running.

`main.py`'s `-net` flag (`main.py:204`) delegates to it for the same reason: a
third implementation of the format would be the one nothing compares against.

---

## 9. What's not built

**Nothing comes back.** The link is one-directional: audio out, transcripts
printed on the far machine. Getting a response back to the caller — the whole
point of the larger project — needs a return channel that neither side
implements. The format reserves room for it (message types `0x10`–`0x1F` and
the `flags` byte), so adding one won't break the current format;
`CallAudioStream.send()` is the hook on this end.

**The two machines have never actually exchanged a packet.** Both halves are
tested against loopback sockets and stubs — real sockets, real datagrams, but
each machine talking to itself. Everything above is how it's designed and
unit-tested to behave, not a report from a live link.

The first real test is worth doing in this order, because each step rules out
a layer:

1. **Is the receiver listening?** `ss -ulnp | grep 9099` on the dictation
   machine.
2. **Do packets cross the LAN at all?** Start the receiver, run
   `redphone_sender.py --dest 192.168.50.120:9099` here with no call, and
   watch for heartbeats with `tcpdump -i any udp port 9099` on the receiving
   end. This tests the network without involving Asterisk or the phone.
3. **Does the format parse?** `fake_call.py` on the `stt-port` branch replays
   a WAV over the same wire format; pointed across the LAN it exercises the
   receiver's full parse-and-transcribe path with no phone in the loop.
4. **Only then, a real call.** Lift the handset and check `lost=0` in the
   per-call summary.
