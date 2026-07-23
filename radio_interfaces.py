"""Shared radio-interface registry for RealTimeTalk.

Identifies which ham-radio USB dongle (AIOC, Digirig, ...) is currently
connected, and how to talk to it: its serial/PTT port VID:PID, which serial
line keys PTT, and how to find its PipeWire sink/source. Imported by both
RealTimeTalk-daemon.py and dtmf_monitor.py so detection logic isn't
duplicated per interface.

Two audio-identification strategies are supported, chosen per interface:
- `audio_name_hints`: substring match against pactl sink/source names. Only
  valid when the device's USB product string is actually unique (AIOC's
  firmware reports a custom "AIOC"/"All-In-One-Cable" string).
- `audio_usbid`: correlates the ALSA card's "vid:pid" (via
  /proc/asound/cardN/usbid) to the card index PipeWire reports in each
  sink/source's `alsa.card` property. Required for interfaces built on
  generic off-the-shelf audio chips whose product string collides with
  other hardware — confirmed necessary for Digirig, whose CM108 codec
  reports the identical "C-Media Electronics Inc. USB PnP Sound Device"
  string as unrelated desk mics; PipeWire only tells them apart with a
  `.2`-style suffix based on plug-in order, which isn't stable.
"""

from dataclasses import dataclass
import glob
import re
import subprocess


@dataclass
class RadioInterface:
    name: str                    # "AIOC" / "Digirig" — used in log lines and UI labels
    usb_vid: int
    usb_pid: int                 # VID:PID of the serial/PTT port
    ptt_line: str                # "dtr" or "rts"
    ptt_prekey_ms: int
    ptt_tail_ms: int
    source_volume_pct: int
    fallback_source: str | None = None   # known-stable pactl name, used only when it's
                                          # actually safe to trust one (see per-entry notes)
    fallback_sink: str | None = None
    audio_name_hints: list[str] | None = None
    audio_usbid: str | None = None
    alsa_mixer_fixups: list[tuple[str, str]] | None = None
    # (control_name, amixer value) pairs applied via `amixer -c <card> sset <control>
    # <value>` once per hotplug-connect. Needed for interfaces whose ALSA card resets
    # to noisy hardware defaults on every plug-in/reboot — PipeWire's source_volume_pct
    # above is a software gain stage downstream of this and can't compensate for it.
    cos_threshold: int = 200
    # Raw int16 peak threshold used by both the DTMF listener and Playback listener
    # to decide "a transmission is present" (there is no real hardware squelch/COS
    # signal on either AIOC or Digirig — see radio_interfaces.py module docstring
    # history / project notes. This is pure audio-level inference). Per-interface
    # because each interface's idle noise floor is different — sharing one global
    # value (as before Digirig existed) means tuning one for noisy hardware can
    # break sensitivity or safety margin on the other.


RADIO_INTERFACES = [
    RadioInterface(
        name="AIOC", usb_vid=0x1209, usb_pid=0x7388,
        ptt_line="dtr", ptt_prekey_ms=250, ptt_tail_ms=400,
        # 80% (not higher): 130% was tried (v3.5.0) and reverted same day (v3.5.1) —
        # it pushed the idle noise floor from ~112 to ~500, above DTMF_COS_THRESHOLD
        # (200), which broke squelch detection for both Playback and the DTMF
        # listener. 100% alone already measured ~225 (unsafe). No headroom to raise
        # this without also raising DTMF_COS_THRESHOLD, which needs a live
        # transmission to verify real "open squelch" peaks still clear it.
        source_volume_pct=80,
        audio_name_hints=["AIOC", "All-In-One-Cable"],
        fallback_source="alsa_input.usb-AIOC_All-In-One-Cable_f5250b7a-00.mono-fallback",
        # No fallback_sink: current code never had one either — _apply_agc_profile
        # just skips the sink switch if find_radio_sink() returns None.
    ),
    RadioInterface(
        name="Digirig", usb_vid=0x10c4, usb_pid=0xea60,  # CP2102N serial/PTT port
        ptt_line="rts", ptt_prekey_ms=250, ptt_tail_ms=400,  # timing not yet live-tested
        # 100 (not 80, unlike AIOC): Digirig's PipeWire source is HARDWARE +
        # DECIBEL_VOLUME flagged, meaning `pactl set-source-volume` writes straight
        # to the SAME ALSA "Mic" capture register as alsa_mixer_fixups below —
        # confirmed by reproduction: setting source_volume_pct=80 was silently
        # clobbering the amixer-set capture level of 16/16 back down to ~12/16
        # every time _apply_agc_profile or startup ran. They must be kept aligned
        # (both "max") or the two code paths fight over the one physical control.
        source_volume_pct=100,
        audio_usbid="0d8c:013c",  # CM108 codec — NOT usable via name hints, see module
                                  # docstring: identical product string to an unrelated
                                  # desk mic already on this Pi.
        # No fallback_source/sink: no static name is safe to trust here — the `.2`
        # suffix that currently disambiguates it from the desk mic depends on
        # plug-in order and can shift (or land on the desk mic instead) across
        # reboots. Must always resolve fresh via audio_usbid correlation.
        alsa_mixer_fixups=[("Auto Gain Control", "on"), ("Mic", "16")],
        # Explicit "on" (not just leaving it alone) because this is a hardware mixer
        # default that resets on every unplug/replug and reboot — must be reasserted
        # on each connect, same as the capture level.
        #
        # History (2026-07-23): the CM108's onboard AGC pumps the idle noise floor
        # up into a noisy, non-stationary band (measured over 30s: ~114-372, avg
        # ~220, still climbing/fluctuating rather than settling) — this used to sit
        # right on top of the old shared DTMF_COS_THRESHOLD=200, causing spurious
        # squelch-open + garbage Goertzel digit reads (DTMF wake/sleep codes never
        # assembled) and a Playback echo loop. Turning AGC off gave a rock-solid
        # ~45-90 floor with a much larger safety margin — but Victor asked to keep
        # AGC on and raise the threshold instead (see cos_threshold below), trading
        # some of that margin for AGC's adaptive gain. If false triggers recur,
        # AGC-off is the more robust fallback; the code to do that is just flipping
        # this tuple back to ("Auto Gain Control", "off").
        cos_threshold=500,
        # ~130 headroom over the highest idle peak observed (372 over 30s) — not as
        # safe as AGC-off would give (which had >100 margin over a ~45-90 floor), and
        # real transmission peak levels on Digirig haven't been measured yet (Stage B,
        # needs a live radio) — this value may need revisiting once that's known: if
        # real signals don't clear 500 with margin, either the threshold needs to
        # come down (risking noise triggers again) or AGC needs to go back off.
    ),
]


def _find_alsa_card_by_usbid(usbid: str) -> str | None:
    """Return the ALSA card index (as a string) whose /proc/asound/cardN/usbid
    matches `usbid` (format "vid:pid", e.g. "0d8c:013c"), or None."""
    for path in glob.glob("/proc/asound/card*/usbid"):
        try:
            with open(path) as f:
                if f.read().strip().lower() == usbid.lower():
                    m = re.search(r"/card(\d+)/", path)
                    if m:
                        return m.group(1)
        except OSError:
            continue
    return None


def _pactl_blocks(plural: str, kind: str) -> list[dict]:
    """Parse `pactl list sinks`/`sources` (long form) into per-device dicts with
    name/alsa.card — mirrors the parsing RealTimeTalk-daemon.py already does for
    its /device-status endpoint."""
    try:
        raw = subprocess.run(["pactl", "list", plural],
                              capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    blocks: list[dict] = []
    cur: dict = {}
    for line in raw.splitlines():
        s = line.strip()
        if line.startswith(f"\t{kind} #") or line.startswith(f"{kind} #"):
            if cur:
                blocks.append(cur)
            cur = {}
        elif s.startswith("Name:"):
            cur["name"] = s.split(":", 1)[1].strip()
        elif "alsa.card =" in s:
            m = re.search(r'"(\d+)"', s)
            if m:
                cur["card"] = m.group(1)
    if cur:
        blocks.append(cur)
    return blocks


def _find_by_card(plural: str, kind: str, card: str) -> str | None:
    names = [b["name"] for b in _pactl_blocks(plural, kind)
             if b.get("card") == card and b.get("name")]
    if kind == "Source":
        # Prefer the real capture device over a monitor-of-sink sharing the same card
        non_monitor = [n for n in names if "monitor" not in n]
        if non_monitor:
            return non_monitor[0]
    return names[0] if names else None


def apply_alsa_mixer_fixups(iface: RadioInterface) -> None:
    """Apply `iface.alsa_mixer_fixups` (if any) to its ALSA card via amixer.
    Safe to call any time; a no-op if the interface has no fixups or its card
    can't currently be resolved (e.g. audio_name_hints-only interfaces, where
    we have no card-index lookup path — none currently need this).

    Applies the fixup list twice with a short settle delay in between: on the
    CM108 (Digirig), toggling "Auto Gain Control" off triggers an async driver
    reset that can clobber a "Mic" level set immediately beforehand if there's
    no gap — confirmed reproducible (the first pass alone left Capture at its
    stale pre-fixup value while Playback took the new one). A second pass
    after the driver has settled reliably corrects it."""
    if not iface.alsa_mixer_fixups or not iface.audio_usbid:
        return
    card = _find_alsa_card_by_usbid(iface.audio_usbid)
    if card is None:
        return
    import time
    for _pass in range(2):
        for control, value in iface.alsa_mixer_fixups:
            try:
                subprocess.run(["amixer", "-c", card, "sset", control, value],
                               capture_output=True, timeout=3)
            except Exception:
                pass
        if _pass == 0:
            time.sleep(0.3)


def find_radio_port() -> tuple[RadioInterface, str] | None:
    """Return (interface, device_path) for whichever registered interface's
    serial/PTT port is currently plugged in, or None if none are."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    ports = list(list_ports.comports())
    for iface in RADIO_INTERFACES:
        for p in ports:
            if p.vid == iface.usb_vid and p.pid == iface.usb_pid:
                return iface, p.device
    return None


def _find_audio(plural: str, kind: str, iface: RadioInterface) -> str | None:
    if iface.audio_name_hints:
        try:
            out = subprocess.run(["pactl", "list", "short", plural],
                                  capture_output=True, text=True, timeout=3).stdout
        except Exception:
            return None
        for line in out.splitlines():
            if any(h in line for h in iface.audio_name_hints) and \
               (kind != "Source" or "monitor" not in line):
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1]
        return None
    if iface.audio_usbid:
        card = _find_alsa_card_by_usbid(iface.audio_usbid)
        return _find_by_card(plural, kind, card) if card is not None else None
    return None


def find_radio_sink(iface: RadioInterface | None = None) -> str | None:
    """Return the PipeWire sink name for `iface` (or, if None, whichever
    registered interface is found first) currently connected, else None."""
    for i in ([iface] if iface else RADIO_INTERFACES):
        name = _find_audio("sinks", "Sink", i)
        if name:
            return name
    return None


def find_radio_source(iface: RadioInterface | None = None) -> str | None:
    """Return the PipeWire source name for `iface` (or, if None, whichever
    registered interface is found first) currently connected, else None."""
    found = find_radio_source_with_iface(iface)
    return found[1] if found else None


def find_radio_source_with_iface(
    iface: RadioInterface | None = None,
) -> tuple[RadioInterface, str] | None:
    """Like find_radio_source, but also returns which interface matched —
    needed by callers (DTMF/Playback listeners) that must read a per-interface
    setting such as cos_threshold for whichever device they end up capturing
    from, not just its device name."""
    for i in ([iface] if iface else RADIO_INTERFACES):
        name = _find_audio("sources", "Source", i)
        if name:
            return i, name
    return None


def is_radio_device_name(name: str, resolved_names: set[str] = frozenset()) -> bool:
    """True if `name` (an exact pactl sink/source name) is recognizable as a
    connected radio interface's own audio device — either by substring hint
    (AIOC) or by exact match against `resolved_names` (names most recently
    resolved via audio_usbid correlation for interfaces like Digirig, whose
    name isn't recognizable out of context)."""
    if name in resolved_names:
        return True
    for iface in RADIO_INTERFACES:
        if iface.audio_name_hints and any(h in name for h in iface.audio_name_hints):
            return True
    return False
