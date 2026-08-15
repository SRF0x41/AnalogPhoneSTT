# Troubleshooting the link between the machines

Notes on failures of the network path between the phone machine and the stt machine —
the ones that present as "the stt layer isn't getting data" with nothing in any log.

The addresses used throughout are the ones from the repo README: the phone machine at
`192.168.50.1`, the stt machine at `192.168.50.120`, joined by a wired link.

## The silent failure: a `/32` netmask on the stt machine

**Symptom.** The phone machine reports the call is up. `stt_port` prints its startup
banner and then nothing at all — no `[ws] call ... started`, no connection error, no
traceback. Both processes look healthy. From the caller's side the handset is simply
dead: no transcripts anywhere.

This is the worst-behaved failure in the system, because *neither* side logs anything.
The stt server never gets a completed TCP handshake, so it has no connection to report
on; the phone machine's connect attempt hangs rather than being refused.

**Cause.** The stt machine's wired interface has address `192.168.50.120` with netmask
`255.255.255.255` (`0xffffffff`) instead of `255.255.255.0` (`0xffffff00`).

On macOS this happens when the interface's service is configured *"Manually Using DHCP
Router"* — it takes the manual IP address but expects the mask and router from DHCP.
There is no DHCP server on this link, so it gets neither, and falls back to a `/32`.

A `/32` means "this address and nothing else is on this link." The kernel therefore has
no on-link route for `192.168.50.0/24`, and the default route wins for everything
including the phone machine:

```
$ route -n get 192.168.50.1
   route to: 192.168.50.1
destination: default
    gateway: 10.0.0.1        ← the Wi-Fi router
  interface: en0             ← Wi-Fi, not the wired link
```

Inbound SYN packets from the phone machine still *arrive* on the wired interface — the
link is up and the NIC accepts them. But the kernel routes the SYN-ACK by destination
address, sends it out Wi-Fi to the ISP gateway, and it is dropped there. The handshake
never completes in either direction.

The `/32` also breaks outbound traffic, which is what makes it quick to confirm.

### Diagnosis

Run these on the stt machine. Substitute the wired interface name — `en5` here, but it
depends on which adapter and port; find it with `networksetup -listallhardwareports`.

```sh
ifconfig en5 | grep 'inet '            # want netmask 0xffffff00, not 0xffffffff
route -n get 192.168.50.1              # want "interface: en5", not the Wi-Fi interface
ping -c 3 192.168.50.1                 # want replies
arp -an | grep 192.168.50              # want an entry for .1, not just our own address
```

A healthy link looks like this:

```
inet 192.168.50.120 netmask 0xffffff00 broadcast 192.168.50.255
   route to: 192.168.50.1
destination: 192.168.50.0
  interface: en5
3 packets transmitted, 3 packets received, 0.0% packet loss
? (192.168.50.1) at a0:ad:9f:89:e4:91 on en5 ifscope [ethernet]
```

If the netmask is right and ping still fails, the problem is not this one — check that
the cable is in the right port, that the phone machine is up, and that nothing else on
the LAN has claimed `.120`.

### Telling "nothing is listening" apart from "nothing can get there"

The phone machine treats every connection failure the same way — it logs one line and
carries the call untranscribed, which is the right behaviour for a caller mid-sentence
but does hide the distinction. The line is:

```
[session] call <id>: no stt at ws://192.168.50.120:9099/ (<exc>) -- continuing untranscribed
```

**The exception in the parentheses is the diagnosis**, and the two cases are far apart:

| What you see | What it means |
|---|---|
| `ConnectionRefusedError`, printed immediately | Packets are arriving and the host is actively rejecting them. The route is fine; `stt_port` is not running, or is on another port. Start it. |
| A timeout, printed after a ~2s pause (`CONNECT_TIMEOUT_SECONDS`) | Packets are going nowhere and nothing is answering. The route is wrong, the cable is out, or the far machine is down. **This is the netmask signature.** |

`asyncio.TimeoutError` stringifies to nothing, so the timeout case tends to print an
empty pair of parentheses — an easy detail to skim past. **Time the line rather than
read it:** instant means refused, a two-second stall means unreachable.

A refused connection is a normal, recoverable state that the system is designed to
tolerate. A timeout means the two machines cannot reach each other at all, and no
amount of restarting `stt_port` will change it.

### Immediate fix

```sh
sudo ifconfig en5 inet 192.168.50.120 netmask 255.255.255.0
```

Takes effect at once and needs no restart of either the stt server or the call — but it
is **not persistent**. It is undone by a reboot, and by unplugging and replugging the
USB ethernet adapter. When it reverts, the symptom returns looking like a brand-new bug.

### Permanent fix

Write the mask into the network service configuration, with an **empty router**:

```sh
sudo networksetup -setmanual "USB3.0 Displaylink" 192.168.50.120 255.255.255.0 ""
```

**Verified working on 2026-08-15.** `networksetup -getinfo "USB3.0 Displaylink"` afterwards
reports exactly this, and the default route stays on Wi-Fi:

```
Manual Configuration
IP address: 192.168.50.120
Subnet mask: 255.255.255.0
Router: (null)
```

The empty-string router argument is accepted rather than rejected, which is the part worth
confirming — `Router: (null)` is the desired outcome, not a sign the command half-failed.

This failure recurred once before the permanent fix was applied: the `ifconfig` command was
run, the link worked for three calls, and the adapter then reverted to `/32` between
sessions. The symptom on its return was indistinguishable from a fresh bug — the stt server
was running and listening the whole time. **Apply the permanent fix the first time.**

**The empty router is the important part, and is not an oversight.** Check the service
order first:

```sh
networksetup -listnetworkserviceorder
```

If the wired service sits above Wi-Fi — it is `(1)` on this machine — then giving it a
router address makes it the primary service and hands it the default route. Every
packet bound for the internet would then be aimed at the phone machine. The link to the
phone machine needs no router, because both ends are on the same subnet and nothing is
being routed *through* it; an empty router leaves Wi-Fi holding the default route.

The alternative, if the service must have a router for some other reason, is to reorder
the services so Wi-Fi is first:

```sh
sudo networksetup -ordernetworkservices "Wi-Fi" "USB3.0 Displaylink" "Thunderbolt Bridge"
```

Prefer the empty router. Reordering has effects well beyond this project.

### If the service disappears

USB ethernet adapters can come back as a *new* network service after being moved to a
different port, in which case the old service keeps the configuration and the new one
starts at DHCP. `networksetup -listallhardwareports` will show the new device name.
Either apply the permanent fix to the new service, or delete the stale one in System
Settings → Network so the configuration is not split across two entries.

## Once the link is up

A working link is necessary but not sufficient. Two further things are worth checking
before concluding the system is healthy, both covered in
[`stt_port/README.md`](../stt_port/README.md):

- **Frame accounting.** `[ws] call ... ended` reports a block count and a duration.
  Blocks are 20ms each, so `blocks × 20ms` should equal the duration almost exactly —
  `blocks=7500 (150.0s)` is a lossless call. A shortfall means audio was lost on the
  wire, which is a different problem from this one.
- **`--energy-threshold`.** The default is calibrated for a hardware microphone and is
  below the noise floor of a G.711 line. Too low, and line noise opens utterances that
  contain no speech; Whisper then emits filler for them — `so`, `Thank you.`, `Thanks
  for watching!` — interleaved with the real transcript. Calibrate with `--meter`
  against the real line.
