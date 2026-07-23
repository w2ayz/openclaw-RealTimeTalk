# RealTimeTalk — Radio Interface Reference

As of v3.7.0. Covers how RealTimeTalk talks to a ham radio, and what's involved
in supporting more than one type of radio dongle.

---

## Overview

RealTimeTalk can bridge Five to a ham radio, letting it be used as a
base-station voice assistant reachable over the air, not just via local mic.
Two USB radio dongles are supported today — **AIOC** (All-In-One-Cable) and
**Digirig Mobile** — auto-detected on hotplug via a shared registry in
`radio_interfaces.py`. There is no manual selector: whichever one is plugged
in is used, matching how AIOC-only detection always worked.

Adding a third interface means adding one entry to `RADIO_INTERFACES` in
`radio_interfaces.py`, not touching the rest of the daemon.

## Detection

Each interface is identified by two things:

- **Serial/PTT port**: matched by USB VID:PID via `pyserial`'s
  `list_ports.comports()`.
- **Audio device**: matched one of two ways, depending on whether the
  hardware's USB product string is actually unique.
  - **Name-hint matching** (AIOC): AIOC's firmware reports a custom
    `"AIOC"` / `"All-In-One-Cable"` product string, so a simple substring
    match against `pactl` sink/source names is reliable.
  - **ALSA-card `usbid` correlation** (Digirig): Digirig's CM108 audio codec
    reports the *generic* product string `"C-Media Electronics Inc. USB PnP
    Sound Device"` — the exact same string an unrelated desk mic on the same
    Pi also reports. PipeWire only tells them apart with an unstable `.2`-style
    suffix based on plug-in order. Confirmed on real hardware, not
    theoretical. Digirig is instead resolved by reading its ALSA card's
    `usbid` from `/proc/asound/cardN/usbid` and correlating that to the
    `alsa.card` property PipeWire reports for each sink/source.

## PTT

Both interfaces key PTT via a serial control line, asserted for a short
"prekey" delay before audio starts and held for a "tail" delay after audio
ends, then released — same choreography either way:

| Interface | Serial line | Chip |
|-----------|------------|------|
| AIOC | DTR | custom composite USB device |
| Digirig Mobile | RTS | Silicon Labs CP2102N |

## No hardware squelch/COS feedback

Neither interface gives RealTimeTalk a real "the radio thinks this is a valid
signal" line. AIOC's true hardware carrier-operated-squelch requires a PCB
revision (v1.1+) this project doesn't have; Digirig Mobile has no such input
at all. So **all transmission detection is software-inferred from raw audio
peak level** — both the DTMF wake/sleep listener and EchoTest (the automatic
on-air echo feature, renamed from "Playback" in v3.7.0) work by watching a
peak threshold, not a real squelch signal.

That threshold (`cos_threshold` in `radio_interfaces.py`) is **per-interface**,
not a single global value, because each interface's idle noise floor differs:

- **AIOC**: threshold 200, idle floor ~112-116.
- **Digirig**: threshold 500, with its onboard CM108 "Auto Gain Control" left
  on (idle floor is a noisier, non-stationary ~114-372 with AGC on — AGC-off
  gives a much larger safety margin, ~45-90, and is documented in
  `radio_interfaces.py` as the more robust fallback if 500 ever proves
  insufficient once Digirig sees real transmissions over the air).

## Known limitation: TX-into-RX crosstalk

Digirig has measurable electrical crosstalk between its own transmit and
receive audio paths — confirmed by capturing its own mic input during a
self-triggered transmission and observing the level rise. Two protections
exist for this:

- EchoTest's listener is fully gated (not just cooldown-gated) for the
  **entire duration** of any active PTT-keyed transmission, from any source
  — not just its own — plus a settle window afterward. Without this, the
  listener would watch straight through its own transmission, capture the
  bleed-through as a same-length "new" signal, and queue it for replay the
  instant PTT released — a self-sustaining, decaying echo loop.
- Mic audio sent to OpenAI's transcription API is muted for the same window,
  via a shared PTT-active flag checked in the mic capture callback — this
  covers both normal TTS replies (already true before Digirig existed) and
  EchoTest's on-air retransmission (a gap closed in v3.7.0, since EchoTest
  keys PTT from a background thread outside the normal conversational
  session object).

This is mitigation, not elimination. It's still worth watching a live radio
test, especially on a repeater or simplex setup where the **radio itself**
could hear its own transmission over RF — a separate, hardware-level
phenomenon no amount of software gating on the Pi side can fully rule out.

## Everything else works identically across both interfaces

Radio Mode toggle, Monitor (live RX loopback), DTMF training/retraining, and
device calibration on the Calibrate page all work the same regardless of
which interface is plugged in — swapping hardware doesn't change the UI or
workflow.
