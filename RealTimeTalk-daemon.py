#!/usr/bin/env python3
"""
RealTimeTalk-daemon.py — OpenClaw RealTimeTalk daemon (gateway-integrated).

Audio flow:
  Mic → OpenAI Realtime API (VAD + STT only) → transcript
  transcript → OpenClaw gateway (chat.send / agent.wait) → Five's reply
  Five's reply → Piper TTS → speaker

Stop via:
  http://<pi-ip>:19000/dashboard          — phone browser (over Tailscale)
  systemctl --user stop openclaw-realtimetalk  — SSH
  SIGTERM / Ctrl-C

Usage:
  python3 RealTimeTalk-daemon.py [options]
  python3 RealTimeTalk-daemon.py --list-devices
  python3 RealTimeTalk-daemon.py --input-device 1 --output-device 2

Requires:
  pip install "websockets>=12" sounddevice numpy
  sudo apt install libportaudio2 alsa-utils
  piper installed at ~/.local/bin/piper with a voice model
"""

__version__ = "1.8.1"

import argparse
import asyncio
import base64
import datetime
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import sounddevice as sd
import websockets

try:
    from zhconv import convert as _zh_convert   # traditional → simplified
except Exception:                                # pragma: no cover
    _zh_convert = None

try:
    from langdetect import detect as _langdetect, LangDetectException as _LangDetectException
    _HAVE_LANGDETECT = True
except ImportError:
    _HAVE_LANGDETECT = False

# ── Constants ─────────────────────────────────────────────────────────────────

OPENCLAW_CONFIG   = os.path.expanduser("~/.openclaw/openclaw.json")
OPENCLAW_GW_URL   = "ws://127.0.0.1:18789"
OPENCLAW_SESSION  = "agent:main:main"

PIPER_CMD         = os.path.expanduser("~/.local/bin/piper-native/piper")
PIPER_ENV         = {**os.environ, "LD_LIBRARY_PATH": os.path.expanduser("~/.local/bin/piper-native")}
PIPER_VOICE_EN    = os.path.expanduser(
    "~/.local/share/piper/voices/en_US-lessac-medium/en_US-lessac-medium.onnx"
)
PIPER_VOICE_ZH    = os.path.expanduser(
    "~/.local/share/piper/voices/zh_CN-huayan-medium/zh_CN-huayan-medium.onnx"
)
PIPER_VOICE       = PIPER_VOICE_EN   # default; speak() will pick based on language
PIPER_SAMPLE_RATE = 22050
ALSA_OUTPUT       = "plughw:3,0"   # USB speaker; plughw handles rate conversion

OPENAI_TRANSCRIBE_MODEL = "gpt-4o-transcribe"
OPENAI_WS_URL     = "wss://api.openai.com/v1/realtime?intent=transcription"
SAMPLE_RATE       = 24000        # OpenAI Realtime API rate
DEVICE_RATE       = 24000        # capture at 24 kHz — PipeWire resamples from native
                                 # (BT700 is 16 kHz; one clean 16k→24k hop vs old 16k→48k→24k)
RESAMPLE_RATIO    = 1            # no decimation needed — DEVICE_RATE == SAMPLE_RATE
CHANNELS          = 1
BLOCKSIZE         = 2400         # 100 ms at 24 kHz
DEVICE_BLOCKSIZE  = BLOCKSIZE    # same as BLOCKSIZE when RESAMPLE_RATIO == 1
DEFAULT_HTTP_PORT = 19000
RECONNECT_DELAY   = 5
IDLE_SLEEP_MINS   = 10           # disconnect from OpenAI after this many minutes of silence
OWW_THRESHOLD     = 0.60         # openwakeword confidence threshold (0–1); raise to reduce false wakes

# AIOC ham radio interface — PTT control via serial DTR.
# Port is found dynamically by USB VID:PID (1209:7388) so the ttyACM number
# does not need to be hardcoded (it changes across plug/unplug cycles).
AIOC_USB_VID       = 0x1209
AIOC_USB_PID       = 0x7388
AIOC_PTT_PREKEY_MS = 250   # ms to hold PTT before audio starts (radio key-up time)
AIOC_PTT_TAIL_MS   = 400   # ms to hold PTT after audio ends (prevents TX clipping)

# Languages accepted in multi-lang WHITELIST mode.
# Add/remove langdetect codes as needed.  Special tokens used for script matching:
#   "ko"="ko"  "ja"="ja"  "zh"=any CJK  "ar"=Arabic script  "ru"=Cyrillic  "hi"=Devanagari
MULTILANG_WHITELIST_LANGS: list[str] = ["en", "zh-cn", "zh-tw", "zh", "ko", "ja", "es", "ms"]
AGENT_TIMEOUT_S   = 90
MIC_GAIN          = 3.0          # headset boom mic is close-talking — 16× was over-amplifying
MIC_GATE_PEAK     = 300          # headset mic is close-talking — lower gate than desk mic
                                 # (lets OpenAI's VAD see real silence between words)
MIC_GATE_MIN      = 30           # calibration clamp — quietest usable room
MIC_GATE_MAX      = 15000        # calibration clamp — raised for AIOC line-level input
# WebRTC AGC virtual source (PipeWire module-echo-cancel). When present it
# normalizes speech level + suppresses noise upstream, so the daemon needs
# only a light trim and a minimal gate. Falls back to the static --mic-gain
# / --mic-gate values when the AGC source is unavailable.
AGC_SOURCE_NAME   = "rtt_agc_source"
AGC_MIC_GAIN       = 2.0          # mic mode: gain_control ON so WebRTC normalises; light trim only
AGC_MIC_GAIN_RADIO = 2.0         # radio mode: gain_control ON; AGC normalises same as mic
AGC_MIC_GATE       = 60          # AGC+NS clean the signal; gate only residual
# Raw physical mic that AGC captures from. Read from the PipeWire AGC config
# so the user's mic selection (via device picker) survives daemon restarts.
def _read_raw_mic_from_agc_config() -> str:
    """Read target.object from the PipeWire AGC config file."""
    import re as _re_mc
    _agc_conf = os.path.expanduser("~/.config/pipewire/pipewire.conf.d/99-rtt-agc.conf")
    try:
        with open(_agc_conf) as _f:
            _m = _re_mc.search(r'target\.object\s*=\s*"([^"]+)"', _f.read())
            if _m:
                return _m.group(1)
    except Exception:
        pass
    return "alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono"

RAW_MIC_SOURCE = _read_raw_mic_from_agc_config()
NEW_DEVICE_VOLUME = 0.05         # software attenuation for new-speaker announcement (5% of signal)
                                 # combined with PipeWire 1% = ~0.05% of full scale
# When mic-leakage calibration can't measure a level (mic can't hear the
# speaker — e.g. headphones, or separate non-coupled mic+speaker), fall back
# to a moderate USABLE level instead of an inaudible minimum, so the system
# is immediately usable and the user can fine-tune via Manual adjustment.
CAL_FALLBACK_PW   = 25           # PipeWire % — audible but not deafening
CAL_FALLBACK_SW   = 0.70         # software gain
# Result announcement always plays at this guaranteed-audible level,
# regardless of the calibration outcome, so the user always hears it.
CAL_ANNOUNCE_PW   = 45
CAL_ANNOUNCE_SW   = 0.75
# Weak-coupling speakers never reach the strict SNR target. Pick the FIRST
# step whose SNR clears this modest "clearly audible above noise" threshold —
# that's the MINIMUM comfortable volume (the function's stated goal), instead
# of the loudest step. Tuned to the knee where the tone becomes unambiguous.
CAL_AUDIBLE_SNR   = 80.0
# Per-device calibration store — persists Vol/SW levels across restarts.
# New/unknown devices start at minimum safe levels; known devices restore
# their previously calibrated settings automatically on connect.
CAL_STORE_FILE    = os.path.expanduser("~/.openclaw/workspace/speaker_cal_store.json")
SLEEP_STATE_FILE  = os.path.expanduser("~/.openclaw/workspace/rtt_sleep_state.json")
CAL_NEW_DEV_PW    = 1      # PipeWire % for unknown device (minimum safe)
CAL_NEW_DEV_SW    = 0.10   # SW for unknown device (10% — clearly audible but not loud)
# Speech-interrupt: if the mic sees this many consecutive 50ms blocks above
# the interrupt threshold while Five is speaking, kill TTS immediately.
SPEAK_INTERRUPT_PEAK   = 4000  # raw mic peak to trigger interrupt (AGC speech >> background)
SPEAK_INTERRUPT_BLOCKS = 6     # × 50 ms = 300 ms sustained speech → interrupt

CONVERSATION_LOG: list[dict] = []   # {"role":"you"/"five"/"system", "text":...}

import threading as _threading
_mic_level_lock = _threading.Lock()
_mic_level_current = [0]   # latest raw pre-gain peak, written by audio thread
_mic_gate_ref     = [500]  # mutable wrapper for MIC_GATE_PEAK, readable across threads

def _detect_headset() -> bool:
    """Return True if a single USB device appears as both a sink (output) and source (input).
    Compares the USB device ID embedded in PipeWire node names."""
    import re as _re4
    try:
        sinks   = subprocess.run(["pactl", "list", "short", "sinks"],
                                  capture_output=True, text=True).stdout
        sources = subprocess.run(["pactl", "list", "short", "sources"],
                                  capture_output=True, text=True).stdout
        # Extract USB serial/ID: the part between 'usb-' and the last '-NN.' in the name
        def _usb_ids(text):
            ids = set()
            for line in text.splitlines():
                m = _re4.search(r'usb-(.+)-\d{2}\.', line)
                if m and "hdmi" not in line.lower() and "monitor" not in line.lower():
                    ids.add(m.group(1))
            return ids
        common = _usb_ids(sinks) & _usb_ids(sources)
        return len(common) > 0
    except Exception:
        return False


_headset_cal_loop = [False]    # flag to stop the looping playback
_headset_cal_proc = [None]     # current aplay subprocess so stop can kill it immediately


def _alsa_card_info(source_name: str) -> str:
    """Return 'card N: <name>' for a PipeWire source, or '' if not found."""
    try:
        out = subprocess.run(["pactl", "list", "sources"],
                              capture_output=True, text=True, timeout=5).stdout
        in_block = False
        card_num = card_name = ""
        for line in out.splitlines():
            stripped = line.strip()
            if f"Name: {source_name}" in line:
                in_block = True
                card_num = card_name = ""
            elif in_block:
                if stripped.startswith("Name:") and source_name not in line:
                    break  # moved to next block
                if 'alsa.card "' in stripped or "alsa.card = " in stripped:
                    import re as _re
                    m = _re.search(r'"(\d+)"', stripped)
                    if m:
                        card_num = m.group(1)
                if 'alsa.card_name' in stripped:
                    import re as _re
                    m = _re.search(r'"([^"]+)"', stripped)
                    if m:
                        card_name = m.group(1)
        if card_num:
            return f"card {card_num}: {card_name}" if card_name else f"card {card_num}"
    except Exception:
        pass
    return ""


def _friendly_pw_name(device_type: str, node_name: str) -> str:
    """Return a human-readable description for a PipeWire sink or source.

    Parses the Description field from `pactl list sinks/sources`.
    Falls back to a shortened node name if not found.
    """
    try:
        out = subprocess.run(["pactl", "list", f"{device_type}s"],
                              capture_output=True, text=True, timeout=5).stdout
        in_block = False
        desc = ""
        for line in out.splitlines():
            stripped = line.strip()
            if f"Name: {node_name}" in line:
                in_block = True
                desc = ""
            elif in_block and stripped.startswith("Description:"):
                desc = stripped.split(":", 1)[1].strip()
                break
            elif in_block and stripped.startswith("Name:") and node_name not in line:
                in_block = False  # moved to next block
        if desc:
            return desc[:40]
    except Exception:
        pass
    # Shorten the raw name: bluez_output.AA_BB_... → BT:AA:BB..., else last segment
    if node_name.startswith("bluez_output."):
        mac = node_name.split(".")[1].replace("_", ":").upper()
        return f"BT {mac}"
    return node_name.rsplit(".", 1)[-1][:35] or node_name[:35]


def _get_device_status() -> dict:
    """Return current audio device info for the portal status panel."""
    import re as _re3
    result = {"mic": "?", "speaker_alsa": ALSA_OUTPUT, "speaker_name": ALSA_OUTPUT,
              "spk_vol": "?", "sw_pct": 100, "gate": 500, "gain": 3.0}
    try:
        # Speaker: use the calibrated/active non-HDMI sink, not the PipeWire default
        # Always use the PipeWire default sink — the actually selected speaker.
        # _find_usb_speaker_sink() picks the first available which may not be
        # the one the user chose.
        default_sink = subprocess.run(
            ["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        sink_id = default_sink  # pactl accepts sink names, not just indices
        if default_sink:
            result["speaker_name"] = _friendly_pw_name("sink", default_sink)
        # Keep speaker_alsa as the actual ALSA output string (from service file)
        try:
            content = open(SERVICE_FILE).read()
            m = _re3.search(r'--alsa-output (\S+)', content)
            if m:
                result["speaker_alsa"] = m.group(1)
        except Exception:
            pass

        # Speaker volume from the calibrated sink
        if sink_id:
            vo = subprocess.run(["pactl", "get-sink-volume", sink_id],
                                 capture_output=True, text=True).stdout
            vm = _re3.search(r'(\d+)%', vo)
            if vm:
                result["spk_vol"] = f"{vm.group(1)}%"

        # Mic: show the physical capture device.
        # When AGC is active the daemon reads rtt_agc_source; the underlying
        # hardware mic is RAW_MIC_SOURCE. Show both clearly.
        default_src = subprocess.run(
            ["pactl", "get-default-source"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if default_src == AGC_SOURCE_NAME:
            raw_friendly = _friendly_pw_name("source", RAW_MIC_SOURCE)
            card_info    = _alsa_card_info(RAW_MIC_SOURCE)
            card_str     = f" [{card_info}]" if card_info else ""
            result["mic"] = f"AGC ({raw_friendly}{card_str})"
        elif default_src:
            card_info = _alsa_card_info(default_src)
            card_str  = f" [{card_info}]" if card_info else ""
            result["mic"] = f"{_friendly_pw_name('source', default_src)}{card_str}"
        else:
            src = _find_always_on_mic_source() or ""
            result["mic"] = _friendly_pw_name("source", src) if src else "?"
    except Exception:
        pass
    result["sw_pct"] = int(_cal_sw_volume * 100)
    result["gate"]   = _mic_gate_ref[0]
    result["gain"]   = MIC_GAIN
    # Effective volume = PipeWire% × SW (combined attenuation visible to speaker)
    try:
        _pct = int(result["spk_vol"].rstrip("%")) if isinstance(result["spk_vol"], str) else 0
        result["effective_pct"] = int(_pct * _cal_sw_volume)
    except Exception:
        result["effective_pct"] = 0
    return result


def _get_audio_fingerprint() -> str:
    """Return a fingerprint of connected audio devices — names only, no state.
    Strips SUSPENDED/RUNNING/IDLE so auto-refresh doesn't re-announce on state changes."""
    try:
        cards = subprocess.run(["cat", "/proc/asound/cards"],
                               capture_output=True, text=True).stdout.strip()
        pw_raw = subprocess.run(["pactl", "list", "short", "sources"],
                                capture_output=True, text=True).stdout.strip()
        # Keep only device name — drop state column (SUSPENDED/RUNNING/IDLE)
        pw = "\n".join(
            "\t".join(line.split("\t")[:3])   # id, name, driver — drop state
            for line in pw_raw.splitlines() if line.strip()
        )
        return cards + "\n---\n" + pw
    except Exception:
        return ""

def _safe_volume_new_sinks(safe_pct: int = 70):
    """Cap all PipeWire sinks to safe_pct% — protects against newly connected speakers at 100%."""
    try:
        sinks = subprocess.run(["pactl", "list", "short", "sinks"],
                               capture_output=True, text=True).stdout.strip()
        for line in sinks.splitlines():
            parts = line.split()
            if parts:
                subprocess.run(
                    ["pactl", "set-sink-volume", parts[0], f"{safe_pct}%"],
                    capture_output=True,
                )
        log.info("Set all sinks to %d%% (device change safety)", safe_pct)
    except Exception as e:
        log.warning("Could not set sink volumes: %s", e)

_audio_fingerprint = [_get_audio_fingerprint()]   # [0] = last known state
_device_change_msg = [""]                          # [0] = pending announcement or ""
_speaker_cal_result: dict = {}                     # last calibration result
_cal_mode_override = [None]  # None=auto-detect, "headset"=force headset, "speaker"=force speaker
_paused_speech:       list = [None]   # (clean_text, alsa_output) saved when TTS is interrupted; None otherwise
_http_interrupt:      list = [False]  # set by /interrupt to cut TTS mid-playback
_last_mic_cb:         list = [0.0]    # epoch of last _mic_cb call — used for hot-plug detection
_post_busy_until:     list = [0.0]    # timestamp: mic sends silence until this time after TTS ends
_is_speaking:         list = [False]  # True while speak() is playing audio
_current_think_task:  list = [None]   # asyncio.Task for current gw.ask(); cancelled by /interrupt
_last_activity:       list = [0.0]    # epoch of last wake/route event; seeded in main()
_idle_disconnected:   list = [False]  # True when auto-sleep closed the OpenAI WebSocket
_wake_event:          list = [None]   # threading.Event; set by /wake to reconnect from sleep
_oww_stop_flag:       list = [False]  # set True to stop the openwakeword listener thread
# DTMF detection — sequences transmitted over radio to wake/sleep Five
DTMF_WAKE_SEQ      = "123"   # transmit DTMF 1-2-3 to wake Five
DTMF_SLEEP_SEQ     = "321"   # transmit DTMF 3-2-1 to put Five to silent
DTMF_DEEPSLEEP_SEQ  = "987"   # transmit DTMF 9-8-7 to disconnect immediately (skip 10-min wait)
DTMF_MONITOR_ON_SEQ  = "456"   # transmit DTMF 4-5-6 to start monitoring (passive transcription only)
DTMF_MONITOR_OFF_SEQ = "654"   # transmit DTMF 6-5-4 to stop monitoring
DTMF_WAKE_SILENT_SEQ = "789"   # transmit DTMF 7-8-9 to wake from deep sleep into Silent (no routing)
DTMF_SAMPLE_RATE   = 8000    # Hz — standard for DTMF; AIOC audio downsampled from 48kHz
DTMF_PROFILE_FILE  = os.path.expanduser("~/.config/rtt/dtmf_profiles.json")
DTMF_COS_THRESHOLD = 200     # raw int16 peak above this = squelch open (closed~120, open~300+)
DTMF_COS_TAIL_S    = 0.5     # seconds to hold COS open after signal drops
_persist_active:      list = [False]  # active (voice-routing) state persisted across reconnects
_persist_monitoring:  list = [False]  # monitoring state persisted across session reconnects
_last_five_reply:     list = [""]     # last reply returned from Five — used to detect stale history
_wake_activate:       list = [False]  # set True when waking from sleep so new session starts active
_clear_audio_buffer:  list = [False]  # set True after TTS interrupt so _send_mic clears OpenAI VAD
_persist_multilang:   list = ["off"]  # multilang state: "off"|"en-zh"|"whitelist"|"any"
_ptt_serial:          list = [None]   # open serial.Serial for AIOC PTT; None when unavailable
_dtmf_force_silent:   list = [False]  # set True by DTMF 321 to silence current session immediately
_dtmf_force_active:    list = [False]  # set True by DTMF 123 to activate current silent session immediately
_dtmf_force_deepsleep: list = [False]  # set True by DTMF 987 to disconnect from OpenAI immediately
_dtmf_force_monitor:   list = [None]   # set True/False by DTMF 456/654 to toggle monitoring mode
_is_tx:               list = [False]  # True while PTT is asserted (suppresses mic transcripts)
_pre_aioc_mic:        list = [None]   # mic source active before AIOC connected; restored on unplug
_radio_profile_active: list = [False] # True when AGC is routing AIOC (radio mode)
_aioc_monitor_module: list = [None]  # PipeWire loopback module ID when AIOC monitor is active
_aioc_monitor_sink:   list = [None]  # sink name currently used for AIOC monitor loopback
_tx_display_until:    list = [0.0]   # epoch until which Manual Adjustment shows TX (AIOC) levels


def _find_always_on_mic_source() -> str | None:
    """Return the PipeWire source name of the always-on USB mic (RUNNING state)."""
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            if "RUNNING" in line and "monitor" not in line.lower():
                return line.split()[1]
    except Exception:
        pass
    return None


def _find_usb_speaker_sink() -> str | None:
    """Return the PipeWire sink index of the first non-HDMI, non-Bluetooth USB sink."""
    try:
        out = subprocess.run(["pactl", "list", "short", "sinks"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                name = parts[1]
                if ("hdmi" not in name.lower()
                        and "monitor" not in name.lower()
                        and not name.startswith("rtt_agc")):
                    return parts[0]   # sink index
    except Exception:
        pass
    return None


def _find_usb_speaker_sink_name() -> str | None:
    """Return the PipeWire sink NAME of the USB speaker (for paplay --device).

    PipeWire holds USB devices exclusively, so direct-ALSA `aplay -D plughw`
    fails with 'device busy'. Speaker calibration must play through PipeWire.
    """
    try:
        out = subprocess.run(["pactl", "list", "short", "sinks"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                name = parts[1]
                if ("hdmi" not in name.lower()
                        and "monitor" not in name.lower()
                        and not name.startswith("rtt_agc")):
                    return name
    except Exception:
        pass
    return None


def _cal_capture(n_samples: int, sample_rate: int) -> "np.ndarray":
    """Capture mono int16 for speaker calibration — FAST path via sd.rec.

    run_speaker_calibration temporarily makes the RAW C-Media mic the
    PipeWire default (bypassing the WebRTC AGC source, which suppresses the
    steady test tone), so sd.rec(device=None) reads the raw mic. This is
    ~4× faster than spawning a parec client per step.
    """
    try:
        rec = sd.rec(n_samples, samplerate=sample_rate, channels=1,
                     dtype="int16", device=None, blocking=True)
        return rec[:n_samples, 0].copy()
    except Exception as e:
        log.warning("Cal capture error: %s", e)
        return np.zeros(n_samples, dtype=np.int16)


def _find_aioc_sink() -> str | None:
    """Return the PipeWire sink name for the AIOC if currently connected, else None."""
    try:
        out = subprocess.run(["pactl", "list", "short", "sinks"],
                             capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            if "AIOC" in line or "All-In-One-Cable" in line:
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1]
    except Exception:
        pass
    return None


def _find_aioc_source() -> str | None:
    """Return the PipeWire source name for the AIOC mic if currently connected, else None."""
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"],
                             capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            if ("AIOC" in line or "All-In-One-Cable" in line) and "monitor" not in line:
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1]
    except Exception:
        pass
    return None


def _find_aioc_port() -> str | None:
    """Return the ttyACM device path for the AIOC by USB VID:PID, or None if absent."""
    try:
        from serial.tools import list_ports as _lp
        for p in _lp.comports():
            if p.vid == AIOC_USB_VID and p.pid == AIOC_USB_PID:
                return p.device
    except Exception:
        pass
    return None


def _ptt_open() -> None:
    """Open AIOC serial port for PTT. Non-fatal — logs warning if unavailable."""
    import serial as _ser
    port = _find_aioc_port()
    if not port:
        log.warning("AIOC PTT unavailable (device 1209:7388 not found) — PTT disabled")
        _ptt_serial[0] = None
        return
    try:
        s = _ser.Serial(port, timeout=0)
        s.dtr = False   # PTT released at open
        s.rts = False
        _ptt_serial[0] = s
        log.info("AIOC PTT ready on %s — audio output will transmit over the air", port)
    except Exception as exc:
        log.warning("AIOC PTT unavailable (%s) — PTT disabled", exc)
        _ptt_serial[0] = None


_AGC_CONF      = os.path.expanduser("~/.config/pipewire/pipewire.conf.d/99-rtt-agc.conf")
_AGC_CONF_RADIO = os.path.expanduser("~/.config/pipewire/pipewire.conf.d/99-rtt-agc-radio.conf")

_AGC_PROFILE_RADIO = """\
# RealTimeTalk AGC — Radio mode (AIOC).
# voice_detection=false: WebRTC VAD suppresses audio that doesn't match
#   its close-mic speech model; radio audio (FM pre-emphasis, weak signals,
#   pauses between words) gets falsely attenuated, causing choppy playback.
# transient_suppression=false: designed to remove keyboard clicks; for radio
#   it silences the squelch key-up click (useful signal onset cue) and clips
#   sharp consonants (P/T/K) in transmitted speech.
# extended_filter=false: long-tail AEC for speaker echo; no real echo exists
#   in radio RX and it can spuriously cancel parts of the signal.
context.modules = [
    {{   name = libpipewire-module-echo-cancel
        args = {{
            aec.method = webrtc
            source.props = {{ node.name = "rtt_agc_source" node.description = "RTT AGC Mic (WebRTC)" }}
            sink.props   = {{ node.name = "rtt_agc_sink"   node.description = "RTT AGC Sink (unused reference)" }}
            capture.props = {{ target.object = "{aioc_src}" }}
            aec.args = {{
                webrtc.gain_control = true webrtc.noise_suppression = true
                webrtc.high_pass_filter = true webrtc.voice_detection = false
                webrtc.extended_filter = false webrtc.transient_suppression = false
            }}
        }}
    }}
]
"""

_AGC_PROFILE_MIC = """\
# RealTimeTalk AGC — Mic mode. gain_control=true for adaptive amplification.
context.modules = [
    {{   name = libpipewire-module-echo-cancel
        args = {{
            aec.method = webrtc
            source.props = {{ node.name = "rtt_agc_source" node.description = "RTT AGC Mic (WebRTC)" }}
            sink.props   = {{ node.name = "rtt_agc_sink"   node.description = "RTT AGC Sink (unused reference)" }}
            capture.props = {{ target.object = "{mic_src}" }}
            aec.args = {{
                webrtc.gain_control = true webrtc.noise_suppression = true
                webrtc.high_pass_filter = true webrtc.voice_detection = true
                webrtc.extended_filter = true webrtc.transient_suppression = true
            }}
        }}
    }}
]
"""

def _apply_agc_profile(radio: bool) -> None:
    """Swap the PipeWire AGC config between radio mode (gain_control=false)
    and mic mode (gain_control=true), then hot-reload the echo-cancel module."""
    import time as _ta
    try:
        # Reliable fallback sources — physical device names are stable across reboots
        _FALLBACK_MIC  = "alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono"
        _FALLBACK_AIOC = "alsa_input.usb-AIOC_All-In-One-Cable_f5250b7a-00.mono-fallback"
        if radio:
            aioc_src = _find_aioc_source() or _FALLBACK_AIOC
            content = _AGC_PROFILE_RADIO.format(aioc_src=aioc_src)
        else:
            # Prefer: saved pre-AIOC source → any running physical mic → known C-Media fallback
            # Never allow the virtual AGC source itself as source_master (self-referential loop)
            _candidates = [
                _pre_aioc_mic[0],
                _find_always_on_mic_source(),
                _FALLBACK_MIC,
            ]
            mic_src = next(
                (s for s in _candidates
                 if s and "rtt_agc" not in s and "AIOC" not in s and "All-In-One" not in s),
                _FALLBACK_MIC
            )
            content = _AGC_PROFILE_MIC.format(mic_src=mic_src)

        with open(_AGC_CONF, "w") as f:
            f.write(content)

        # Hot-swap echo-cancel module
        mods = subprocess.run(["pactl", "list", "short", "modules"],
                              capture_output=True, text=True).stdout
        for line in mods.splitlines():
            if "echo-cancel" in line:
                subprocess.run(["pactl", "unload-module", line.split()[0]],
                               capture_output=True)
        _ta.sleep(0.5)
        subprocess.run(["pactl", "load-module", "module-echo-cancel",
                        "aec_method=webrtc",
                        f"source_name={AGC_SOURCE_NAME}",
                        f"source_master={aioc_src if radio else mic_src}",
                        "sink_name=rtt_agc_sink",
                        f"aec_args=webrtc.gain_control=1 "
                        "webrtc.noise_suppression=1 webrtc.high_pass_filter=1 "
                        + ("webrtc.voice_detection=0 webrtc.extended_filter=0 "
                           "webrtc.transient_suppression=0"
                           if radio else
                           "webrtc.voice_detection=1 webrtc.extended_filter=1 "
                           "webrtc.transient_suppression=1"),
                       ], capture_output=True)
        _ta.sleep(0.3)
        subprocess.run(["pactl", "set-default-source", AGC_SOURCE_NAME], capture_output=True)
        # Restore default sink: AIOC when radio, USB speaker when mic
        if radio:
            aioc_sink = _find_aioc_sink()
            if aioc_sink:
                subprocess.run(["pactl", "set-default-sink", aioc_sink], capture_output=True)
                _apply_device_cal(aioc_sink)
        else:
            usb_spk = next((
                l.split()[1] for l in subprocess.run(
                    ["pactl", "list", "short", "sinks"], capture_output=True, text=True
                ).stdout.splitlines()
                if "Generic_USB2.0" in l or ("USB2.0" in l and "AIOC" not in l)
            ), None)
            if usb_spk:
                subprocess.run(["pactl", "set-default-sink", usb_spk], capture_output=True)
                _apply_device_cal(usb_spk)
        globals()['RAW_MIC_SOURCE'] = aioc_src if radio else mic_src
        globals()['MIC_GAIN'] = AGC_MIC_GAIN_RADIO if radio else AGC_MIC_GAIN
        _radio_profile_active[0] = radio
        log.info("AGC profile → %s (gain_control=%s, MIC_GAIN=%.0fx)",
                 "radio" if radio else "mic", not radio,
                 AGC_MIC_GAIN_RADIO if radio else AGC_MIC_GAIN)
    except Exception as exc:
        log.warning("AGC profile switch failed: %s", exc)


def _ptt_alive() -> bool:
    """Return True if the AIOC serial port is open and the device is still connected.
    Auto-opens and switches AGC mic to AIOC on hotplug; restores previous mic on unplug.
    Reopens if the ttyACM port number changed after a reconnect."""
    current_port = _find_aioc_port()
    if _ptt_serial[0]:
        # Check if port path changed (e.g. ttyACM0 → ttyACM1 after replug)
        if current_port and _ptt_serial[0].port != current_port:
            log.info("AIOC port changed %s → %s — reopening", _ptt_serial[0].port, current_port)
            try: _ptt_serial[0].close()
            except Exception: pass
            _ptt_serial[0] = None
    if not _ptt_serial[0]:
        if current_port:
            _ptt_open()
            if _ptt_serial[0]:
                # AIOC just appeared — save current mic, switch AGC to AIOC + radio profile
                _pre_aioc_mic[0] = globals().get("RAW_MIC_SOURCE")
                _radio_profile_active[0] = True
                aioc_src = _find_aioc_source()
                if aioc_src:
                    import threading as _tptt
                    _tptt.Thread(target=_apply_agc_profile, args=(True,), daemon=True).start()
        return _ptt_serial[0] is not None
    if not _find_aioc_port():
        try:
            _ptt_serial[0].close()
        except Exception:
            pass
        _ptt_serial[0] = None
        _is_tx[0] = False
        log.info("AIOC disconnected — PTT disabled, restoring mic AGC profile")
        _radio_profile_active[0] = False
        # Stop monitor loopback — source device is gone
        if _aioc_monitor_module[0] is not None:
            try:
                subprocess.run(["pactl", "unload-module",
                                str(_aioc_monitor_module[0])], capture_output=True)
            except Exception:
                pass
            _aioc_monitor_module[0] = None
            _aioc_monitor_sink[0] = None
            log.info("AIOC monitor loopback stopped (AIOC disconnected)")
        import threading as _tptt2
        _tptt2.Thread(target=_apply_agc_profile, args=(False,), daemon=True).start()
        _pre_aioc_mic[0] = None
        return False
    return True


def _ptt_key() -> None:
    """Assert PTT via AIOC serial DTR. Sets _is_tx so transcripts are suppressed."""
    s = _ptt_serial[0]
    if s:
        try:
            s.dtr = True
            s.rts = False
        except Exception as exc:
            log.warning("PTT key failed: %s — reopening port", exc)
            try: s.close()
            except Exception: pass
            _ptt_serial[0] = None
            _ptt_open()   # reopen on new port number
            s2 = _ptt_serial[0]
            if s2:
                try: s2.dtr = True; s2.rts = False
                except Exception as exc2: log.warning("PTT key retry failed: %s", exc2)
    _is_tx[0] = True


def _ptt_release() -> None:
    """Release PTT via AIOC serial DTR."""
    import time as _ptr
    if _is_tx[0] and _radio_profile_active[0]:
        # Only trigger the TX display window when we were actually transmitting
        _tx_display_until[0] = _ptr.time() + 10
    s = _ptt_serial[0]
    if s:
        try:
            s.dtr = False
        except Exception as exc:
            log.warning("PTT release failed: %s", exc)
    _is_tx[0] = False


def run_speaker_calibration(alsa_output: str = None,
                             test_freq: float = 440.0,
                             duration: float = 0.2,
                             snr_target: float = 50000.0) -> dict:
    """
    Find the MINIMUM usable speaker volume by starting at absolute minimum
    (PipeWire 1% + software attenuation 1%) and stepping up until the mic
    detects the tone with adequate SNR.

    Steps: (PW=1%, SW=0.01), (0.02), (0.05), (0.1), (0.2), (0.5), (1.0),
           then PW=5%,10%,20%,30%,40%,50%,60% at SW=1.0.

    Returns dict with: safe_vol (PipeWire %), safe_sw_vol (software 0-1),
                       measurements, mic_source, status.
    """
    import wave as _wave, tempfile as _tf, time as _t

    mic_source = _find_always_on_mic_source()
    speaker_sink = _find_usb_speaker_sink()

    # Find working ALSA output
    def _find_working_alsa_out() -> str:
        candidates = ([alsa_output] if alsa_output else []) + [f"plughw:{c},0" for c in range(6)]
        test_path = _tf.mktemp(suffix=".wav")
        try:
            with _wave.open(test_path, 'wb') as wf:
                wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(48000)
                wf.writeframes(b'\x00\x00' * 480)
            for c in candidates:
                if subprocess.run(["aplay", "-D", c, "-q", test_path],
                                  capture_output=True).returncode == 0:
                    log.info("Speaker cal: using output %s", c)
                    return c
        finally:
            try: os.unlink(test_path)
            except FileNotFoundError: pass
        return "default"

    speaker_alsa = _find_working_alsa_out()

    # PipeWire holds USB devices exclusively, so play the tone THROUGH
    # PipeWire (paplay → USB sink) rather than direct-ALSA aplay (which
    # fails 'device busy' and silently falls back to inaudible default).
    cal_sink = _find_usb_speaker_sink_name()
    # FAST capture: make the raw C-Media mic the default for the duration of
    # calibration so sd.rec reads it directly (bypassing the AGC source that
    # would suppress the test tone). Restored in finally. ~4× faster than
    # spawning a parec client per step.
    _cal_prev_src = _get_default_source()
    if _agc_source_available() or _cal_prev_src == AGC_SOURCE_NAME:
        _set_default_source(RAW_MIC_SOURCE)
    log.info("Speaker cal: tone via PipeWire sink=%s, capture raw mic=%s",
             cal_sink or "(none)", RAW_MIC_SOURCE)

    # Absolute minimum first — force EVERY sink to PipeWire 1% (the practical
    # floor) before any sound plays, so a powered speaker can't blast at full
    # volume the instant calibration starts.
    _safe_volume_new_sinks(1)
    _t.sleep(0.3)

    sample_rate = 48000
    n_samples   = int(sample_rate * duration)
    freq_idx    = int(np.round(test_freq * n_samples / sample_rate))

    # Steps: (pw_pct, sw_volume). Start as quiet as audibly possible — PW 1%
    # plus tiny software gain (0.002 ≈ 0.001% of full scale) — then ramp up.
    # This protects loud powered speakers: the very first tone is barely
    # audible, and it only gets louder if the mic genuinely can't hear it.
    steps = (
        [(1, sw) for sw in [0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0]] +
        [(pw, 1.0) for pw in [5, 10, 20, 30, 40, 50, 60]]
    )

    # Measure mic noise floor at absolute minimum (silence reference)
    try:
        ref_rec   = _cal_capture(n_samples, sample_rate)
        ref_data  = ref_rec.astype(np.float32) / 32768.0
        ref_fft   = np.abs(np.fft.rfft(ref_data)) / n_samples
        noise_floor = float(np.median(ref_fft))
    except Exception:
        noise_floor = 1e-6

    measurements: list[dict] = []
    found_pw, found_sw = 1, 0.002   # safest fallback = quietest step
    status = "ok"
    cur_pw = 1

    try:
        for pw_pct, sw_vol in steps:
            # Update PipeWire only when it changes
            if pw_pct != cur_pw:
                if speaker_sink:
                    subprocess.run(["pactl", "set-sink-volume", speaker_sink, f"{pw_pct}%"],
                                   capture_output=True)
                _t.sleep(0.05)
                cur_pw = pw_pct

            # Generate tone at this software level
            t_arr   = np.linspace(0, duration, n_samples, endpoint=False)
            tone_16 = (0.5 * sw_vol * np.sin(2 * np.pi * test_freq * t_arr) * 32767).astype(np.int16)
            tone_path = _tf.mktemp(suffix=".wav")
            with _wave.open(tone_path, 'wb') as wf:
                wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sample_rate)
                wf.writeframes(tone_16.tobytes())

            # Play (PipeWire) + record (raw mic) simultaneously
            recording = np.zeros(n_samples, dtype=np.int16)
            done_ev   = _threading.Event()

            def _rec(buf=recording, ev=done_ev):
                try:
                    buf[:] = _cal_capture(n_samples, sample_rate)
                except Exception as e:
                    log.warning("Cal mic error: %s", e)
                finally:
                    ev.set()

            _threading.Thread(target=_rec, daemon=True).start()
            _t.sleep(0.05)  # let capture spin up before the tone
            if cal_sink:
                subprocess.run(["paplay", f"--device={cal_sink}", tone_path],
                               capture_output=True)
            else:
                subprocess.run(["aplay", "-D", speaker_alsa, "-q", tone_path],
                               capture_output=True)
            done_ev.wait(timeout=duration + 2.0)
            try: os.unlink(tone_path)
            except FileNotFoundError: pass

            # SNR: how much louder is the tone vs noise floor?
            data    = recording.astype(np.float32) / 32768.0
            fft_mag = np.abs(np.fft.rfft(data)) / n_samples
            tone_energy = float(fft_mag[freq_idx])
            snr = tone_energy / noise_floor if noise_floor > 0 else 0.0

            measurements.append({"pw": pw_pct, "sw": round(sw_vol, 3),
                                  "tone": round(tone_energy, 7), "snr": round(snr, 2)})
            log.info("Speaker cal: PW=%d%% SW=%.2f tone=%.6f SNR=%.1f",
                     pw_pct, sw_vol, tone_energy, snr)

            if snr >= snr_target:
                found_pw, found_sw = pw_pct, sw_vol
                log.info("Speaker cal: adequate SNR %.1f at PW=%d%% SW=%.2f → using this level",
                         snr, pw_pct, sw_vol)
                break
        else:
            if measurements:
                best = max(measurements, key=lambda m: m["tone"])
                # First step that is clearly audible above noise = the
                # MINIMUM comfortable volume (matches the docstring goal).
                audible = next(
                    (m for m in measurements if m["snr"] >= CAL_AUDIBLE_SNR),
                    None,
                )
                if audible:
                    found_pw, found_sw = audible["pw"], audible["sw"]
                    log.info("Speaker cal: minimum clearly-audible level "
                             "PW=%d%% SW=%.2f (SNR=%.1f, knee≥%.0f)",
                             found_pw, found_sw, audible["snr"], CAL_AUDIBLE_SNR)
                elif best["tone"] < 0.00005:
                    # Mic genuinely can't hear the speaker (headphones /
                    # non-coupled). Use a moderate USABLE default so the
                    # system still works; user fine-tunes via Manual.
                    log.warning("Speaker cal: no acoustic signal — mic can't hear "
                                "speaker; using usable default PW=%d%% SW=%.2f",
                                CAL_FALLBACK_PW, CAL_FALLBACK_SW)
                    status = "no_mic"
                    found_pw, found_sw = CAL_FALLBACK_PW, CAL_FALLBACK_SW
                else:
                    # Faint coupling that never gets clearly audible — best
                    # we can do is the strongest step.
                    found_pw, found_sw = best["pw"], best["sw"]
                    log.info("Speaker cal: weak coupling, best energy PW=%d%% "
                             "SW=%.2f (SNR=%.1f)", found_pw, found_sw, best["snr"])
            else:
                found_pw, found_sw = CAL_FALLBACK_PW, CAL_FALLBACK_SW

        # Set PipeWire to the found level and make this sink the default
        # so TTS playback goes to the calibrated device (not HDMI default)
        if cal_sink:
            subprocess.run(["pactl", "set-default-sink", cal_sink],
                           capture_output=True)
        if speaker_sink:
            subprocess.run(["pactl", "set-sink-volume", speaker_sink, f"{found_pw}%"],
                           capture_output=True)

        # Update global speak() software volume so all subsequent TTS uses calibrated level
        global _cal_sw_volume
        _cal_sw_volume = found_sw
        log.info("Speaker cal complete: PW=%d%% SW=%.2f alsa=%s — speak() will use this level",
                 found_pw, found_sw, speaker_alsa)

        # Persist the found ALSA output card to the service file so restarts use the right device
        if status == "ok":
            _update_service_alsa_output(speaker_alsa)
        # Save calibrated levels to per-device store (even fallback/no_mic levels)
        _default_s = subprocess.run(["pactl","get-default-sink"],
                                    capture_output=True,text=True).stdout.strip()
        if _default_s:
            _save_device_cal(_default_s, found_pw, found_sw)

    except Exception as e:
        log.error("Speaker calibration error: %s", e)
        status = f"error: {e}"
        found_pw, found_sw = CAL_FALLBACK_PW, CAL_FALLBACK_SW
    finally:
        # Restore the AGC source as default for normal speech capture
        if _cal_prev_src and _get_default_source() != _cal_prev_src:
            _set_default_source(_cal_prev_src)
            log.info("Speaker cal: restored default source %s", _cal_prev_src)

    return {
        "safe_vol": found_pw,
        "safe_sw_vol": found_sw,
        "speaker_alsa": speaker_alsa,
        "measurements": measurements,
        "mic_source": mic_source or "unknown",
        "speaker_sink": speaker_sink or "unknown",
        "test_freq": test_freq,
        "snr_target": snr_target,
        "status": status,
    }

_cal_sw_volume: float = 1.0   # updated after calibration; used by speak() for normal TTS
MAX_LOG_ENTRIES = 40

def _log_entry(role: str, text: str):
    now = datetime.datetime.now()
    ts  = now.strftime("%H:%M:%S")
    CONVERSATION_LOG.append({"role": role, "text": text, "ts": ts,
                              "epoch": now.timestamp()})
    if len(CONVERSATION_LOG) > MAX_LOG_ENTRIES:
        CONVERSATION_LOG.pop(0)

CALIBRATE_PHRASES = {
    "calibrate mic", "calibrate microphone", "calibrate noise",
    "recalibrate mic", "recalibrate microphone",
    "mic calibration", "microphone calibration",
    "adjust mic for noise", "adjust microphone for noise",
}

WAKE_PHRASES  = {"five wake up", "5 wake up", "real time talk on", "real-time talk on", "realtimetalk on",
                 "five 醒来", "five 醒", "five 开始", "wake up five", "wake up 5",
                 "hey jarvis", "hey jarvis wake up"}  # also caught by openwakeword in SLEEP state
SLEEP_PHRASES = {"five go to sleep", "5 go to sleep", "real time talk off", "real-time talk off", "realtimetalk off",
                 "five 睡觉", "five 休息", "five 停", "sleep five"}
MONITOR_ON_PHRASES  = {"five start monitoring", "start monitoring", "five monitor on",
                       "monitor on", "five monitoring on"}
MONITOR_OFF_PHRASES = {"five stop monitoring", "stop monitoring", "five monitor off",
                       "monitor off", "five monitoring off"}
CONTINUE_PHRASES    = {"continue", "five continue", "please continue", "go on", "go ahead",
                       "keep going", "继续", "继续说", "你继续", "请继续"}

def _is_english_or_chinese(text: str) -> bool:
    """Return True only if the transcript appears to be English or Chinese.
    Filters out Japanese (hiragana/katakana), Arabic, Cyrillic, Korean, etc.
    that gpt-4o-transcribe hallucinates when audio is noisy.
    """
    # Reject if it contains Japanese kana, Arabic, Cyrillic, Korean, etc.
    reject_ranges = (
        (0x3040, 0x30FF),   # hiragana + katakana (Japanese)
        (0x0600, 0x06FF),   # Arabic
        (0x0400, 0x04FF),   # Cyrillic
        (0xAC00, 0xD7AF),   # Korean Hangul
        (0x0900, 0x097F),   # Devanagari
    )
    for ch in text:
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in reject_ranges):
            return False
    # Accept if all characters are ASCII or CJK (Chinese/Japanese kanji — kanji
    # without kana means it's Chinese in practice here)
    for ch in text:
        cp = ord(ch)
        if cp <= 0x7F:
            continue  # ASCII = English
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            continue  # CJK unified ideographs = Chinese
        if ch in ' \t\n\r':
            continue
        # Anything else (accented Latin for German/French/etc.) → reject
        return False
    # Pure ASCII — use langdetect on ≥2-word texts to catch other Latin-script
    # languages (French, Dutch, German, etc.) that GPT-4o hallucinates.
    # Single words are handled by the extended short-word noise guard downstream
    # (single words < 9 chars are dropped unless whitelisted).
    # langdetect is unreliable on single short words so we skip it for them.
    if _HAVE_LANGDETECT and len(text.split()) >= 2:
        try:
            lang = _langdetect(text)
            if lang not in ("en", "zh-cn", "zh-tw"):
                log.info("langdetect rejected %r as %r", text[:60], lang)
                return False
        except _LangDetectException:
            pass  # inconclusive — let it through
    return True

def _is_in_multilang_whitelist(text: str) -> bool:
    """Return True if text appears to be in a MULTILANG_WHITELIST_LANGS language.

    Script ranges are checked first (fast, unambiguous); langdetect is used for
    Latin-script text.  Inconclusive → let through.  To add a language, append
    its langdetect code to MULTILANG_WHITELIST_LANGS.
    """
    has_hangul = has_kana = has_arabic = has_cyril = has_deva = has_cjk = False
    for ch in text:
        cp = ord(ch)
        if   0xAC00 <= cp <= 0xD7AF:                             has_hangul = True
        elif 0x3040 <= cp <= 0x30FF:                             has_kana   = True
        elif 0x0600 <= cp <= 0x06FF:                             has_arabic = True
        elif 0x0400 <= cp <= 0x04FF:                             has_cyril  = True
        elif 0x0900 <= cp <= 0x097F:                             has_deva   = True
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:  has_cjk    = True

    if has_hangul: return "ko" in MULTILANG_WHITELIST_LANGS
    if has_kana:   return "ja" in MULTILANG_WHITELIST_LANGS
    if has_arabic: return "ar" in MULTILANG_WHITELIST_LANGS
    if has_cyril:  return any(c in MULTILANG_WHITELIST_LANGS for c in ("ru","uk","bg","sr","mk"))
    if has_deva:   return any(c in MULTILANG_WHITELIST_LANGS for c in ("hi","mr","ne"))
    if has_cjk:    return any(c in MULTILANG_WHITELIST_LANGS for c in ("zh","zh-cn","zh-tw"))

    # Pure Latin-script — use langdetect to distinguish EN/ES/MS/FR/etc.
    if _HAVE_LANGDETECT and len(text.split()) >= 2:
        try:
            lang = _langdetect(text)
            if lang not in MULTILANG_WHITELIST_LANGS:
                log.info("whitelist rejected %r as %r", text[:60], lang)
                return False
        except _LangDetectException:
            pass  # inconclusive → let through
    return True

def _normalize(text: str) -> str:
    import string
    t = text.strip().lower()
    t = t.translate(str.maketrans(string.punctuation, " " * len(string.punctuation)))
    # Insert space at CJK↔Latin boundaries so "我係wake" → "我係 wake"
    t = re.sub(r'([一-鿿㐀-䶿])([a-zA-Z0-9])', r'\1 \2', t)
    t = re.sub(r'([a-zA-Z0-9])([一-鿿㐀-䶿])', r'\1 \2', t)
    # treat digit "5" as "five"
    t = re.sub(r'\b5\b', 'five', t)
    return " ".join(t.split())

def _matches_phrase(transcript: str, phrases: set) -> bool:
    """True if the transcript contains any trigger phrase, or is a fuzzy word-overlap match.

    Two-pass:
    1. Exact substring after normalisation.
    2. Fuzzy: if the transcript shares ≥ 60% of a phrase's words it counts as a match
       (handles car-noise garbling like 'five wake up' → 'five break up').
    """
    t = _normalize(transcript)
    for phrase in phrases:
        p = _normalize(phrase)
        # Pass 1: substring
        if p in t:
            return True
        # Pass 2: word overlap ratio
        t_words = set(t.split())
        p_words  = set(p.split())
        if p_words and len(t_words & p_words) / len(p_words) >= 0.6:
            return True
    return False

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RTT] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("RealTimeTalk")

# ── Config / secrets ──────────────────────────────────────────────────────────

def _load_json(path: str) -> dict:
    with open(path) as f:
        return json.load(f)

def load_openai_key() -> str:
    cfg = _load_json(OPENCLAW_CONFIG)
    key = (
        cfg.get("talk", {})
           .get("providers", {})
           .get("openai", {})
           .get("apiKey", "")
    )
    # Resolve OpenClaw SecretRef: {"source":"file","provider":"...","id":"/a/b/c"}
    if isinstance(key, dict) and key.get("source") == "file":
        provider_name = key.get("provider", "")
        secret_path = os.path.expanduser(
            cfg.get("secrets", {})
               .get("providers", {})
               .get(provider_name, {})
               .get("path", "")
        )
        secrets = _load_json(secret_path)
        for part in [p for p in key.get("id", "").split("/") if p]:
            secrets = secrets[part]
        key = secrets
    if not key:
        raise RuntimeError(
            "No OpenAI API key at talk.providers.openai.apiKey in openclaw.json"
        )
    return key

def load_gateway_token() -> str:
    cfg = _load_json(OPENCLAW_CONFIG)
    token = cfg.get("gateway", {}).get("auth", {}).get("token", "")
    if not token:
        raise RuntimeError("No gateway.auth.token in openclaw.json")
    return token

def _agc_source_available() -> bool:
    """True if the WebRTC AGC virtual source is loaded in PipeWire."""
    try:
        out = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return AGC_SOURCE_NAME in out
    except Exception:
        return False


def _update_agc_capture_source(physical_source: str) -> bool:
    """Redirect the WebRTC AGC module to capture from a different physical mic.

    Updates the PipeWire config file and hot-swaps the echo-cancel module
    (no PipeWire restart needed). AGC remains the daemon's default source;
    only the underlying hardware input changes.
    """
    import re as _re7
    config = os.path.expanduser(
        "~/.config/pipewire/pipewire.conf.d/99-rtt-agc.conf")
    try:
        with open(config) as f:
            content = f.read()
        content = _re7.sub(
            r'target\.object\s*=\s*"[^"]*"',
            f'target.object = "{physical_source}"',
            content,
        )
        with open(config, "w") as f:
            f.write(content)
        # Update RAW_MIC_SOURCE so speaker-cal captures from the right mic
        globals()['RAW_MIC_SOURCE'] = physical_source
        # Hot-swap: unload old echo-cancel module, load new one
        mods = subprocess.run(["pactl", "list", "short", "modules"],
                              capture_output=True, text=True).stdout
        for line in mods.splitlines():
            if "echo-cancel" in line:
                mid = line.split()[0]
                subprocess.run(["pactl", "unload-module", mid],
                               capture_output=True)
        import time as _t2; _t2.sleep(0.5)
        subprocess.run([
            "pactl", "load-module", "module-echo-cancel",
            "aec_method=webrtc",
            f"source_name={AGC_SOURCE_NAME}",
            f"source_master={physical_source}",
            "sink_name=rtt_agc_sink",
            ('aec_args=webrtc.gain_control=1 webrtc.noise_suppression=1 '
             'webrtc.high_pass_filter=1 webrtc.voice_detection=1 '
             'webrtc.extended_filter=1 webrtc.transient_suppression=1'),
        ], capture_output=True)
        _t2.sleep(0.5)
        subprocess.run(["pactl", "set-default-source", AGC_SOURCE_NAME],
                       capture_output=True)
        log.info("AGC capture redirected to %s (AGC still active as default)",
                 physical_source)
        return True
    except Exception as e:
        log.warning("Could not redirect AGC capture: %s", e)
        return False


def _activate_agc_source() -> bool:
    """Make the WebRTC AGC source the PipeWire default if it exists.

    Returns True when AGC is active (daemon should use AGC-tuned gain/gate),
    False when it should fall back to the static --mic-gain / --mic-gate.
    """
    if not _agc_source_available():
        return False
    try:
        subprocess.run(
            ["pactl", "set-default-source", AGC_SOURCE_NAME],
            check=False, timeout=5,
        )
        return True
    except Exception:
        return False


def _get_default_source() -> str:
    """Current PipeWire default source name (empty string on failure)."""
    try:
        return subprocess.run(
            ["pactl", "get-default-source"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def _set_default_source(name: str) -> bool:
    """Set the PipeWire default source. Returns True on success."""
    if not name:
        return False
    try:
        return subprocess.run(
            ["pactl", "set-default-source", name],
            capture_output=True, timeout=5,
        ).returncode == 0
    except Exception:
        return False

# ── Per-device calibration store ─────────────────────────────────────────────

_cal_store: dict = {}   # {sink_name: {"pw_pct": int, "sw_vol": float, "name": str}}

def _load_cal_store() -> None:
    global _cal_store
    try:
        with open(CAL_STORE_FILE) as f:
            _cal_store = json.load(f)
        log.info("Loaded calibration store: %d device(s)", len(_cal_store))
    except (FileNotFoundError, json.JSONDecodeError):
        _cal_store = {}

def _save_cal_store() -> None:
    try:
        os.makedirs(os.path.dirname(CAL_STORE_FILE), exist_ok=True)
        with open(CAL_STORE_FILE, "w") as f:
            json.dump(_cal_store, f, indent=2)
    except Exception as e:
        log.warning("Could not save calibration store: %s", e)

def _save_sleep_state(sleeping: bool) -> None:
    """Persist sleep state to disk so it survives service restarts."""
    try:
        os.makedirs(os.path.dirname(SLEEP_STATE_FILE), exist_ok=True)
        with open(SLEEP_STATE_FILE, "w") as f:
            json.dump({"sleeping": sleeping}, f)
    except Exception as e:
        log.warning("Could not save sleep state: %s", e)

def _load_sleep_state() -> bool:
    """Return True if the daemon was sleeping when it last stopped."""
    try:
        with open(SLEEP_STATE_FILE) as f:
            return bool(json.load(f).get("sleeping", False))
    except (FileNotFoundError, json.JSONDecodeError):
        return False

def _save_device_cal(sink_name: str, pw_pct: int, sw_vol: float) -> None:
    """Record calibrated levels for a speaker device and persist to disk."""
    friendly = _friendly_pw_name("sink", sink_name)
    _cal_store[sink_name] = {"pw_pct": pw_pct, "sw_vol": sw_vol, "name": friendly}
    _save_cal_store()
    log.info("Saved calibration for %r: PW=%d%% SW=%.2f", friendly, pw_pct, sw_vol)

def _apply_device_cal(sink_name: str) -> bool:
    """Apply saved calibration levels for a sink, or minimum safe levels if unknown.

    Returns True if a previously calibrated level was found and applied,
    False if minimum/default levels were applied (new/unknown device).
    """
    import re as _rec
    if sink_name in _cal_store:
        entry   = _cal_store[sink_name]
        pw      = entry.get("pw_pct", CAL_FALLBACK_PW)
        sw      = entry.get("sw_vol", CAL_FALLBACK_SW)
        subprocess.run(["pactl", "set-sink-volume", sink_name, f"{pw}%"],
                       capture_output=True)
        globals()['_cal_sw_volume'] = sw
        log.info("Restored calibration for %r: PW=%d%% SW=%.2f",
                 entry.get("name", sink_name), pw, sw)
        return True
    else:
        # Unknown device — start at minimum to protect ears/speakers
        subprocess.run(["pactl", "set-sink-volume", sink_name, f"{CAL_NEW_DEV_PW}%"],
                       capture_output=True)
        globals()['_cal_sw_volume'] = CAL_NEW_DEV_SW
        log.info("New/unknown device %r — set minimum safe levels PW=%d%% SW=%.2f",
                 sink_name, CAL_NEW_DEV_PW, CAL_NEW_DEV_SW)
        return False

# ── Service file helpers ──────────────────────────────────────────────────────

SERVICE_FILE = os.path.expanduser(
    "~/.config/systemd/user/openclaw-realtimetalk.service"
)

def _update_service_alsa_output(new_alsa: str):
    """Persist --alsa-output <device> in the systemd service ExecStart line."""
    try:
        with open(SERVICE_FILE) as f:
            content = f.read()
        import re as _re
        content = _re.sub(r" --alsa-output \S+", "", content)
        content = content.replace(
            "\nRestart=no",
            f" --alsa-output {new_alsa}\nRestart=no",
        )
        with open(SERVICE_FILE, "w") as f:
            f.write(content)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        log.info("Service updated: --alsa-output %s", new_alsa)
    except Exception as e:
        log.warning("Could not update service alsa-output: %s", e)


def _update_service_input_source(source_name: str):
    """Persist --input-source <name> in the systemd service ExecStart line."""
    try:
        with open(SERVICE_FILE) as f:
            content = f.read()
        import re as _re
        content = _re.sub(r" --input-source \S+", "", content)
        if source_name:
            content = content.replace(
                "\nRestart=no",
                f" --input-source {source_name}\nRestart=no",
            )
        with open(SERVICE_FILE, "w") as f:
            f.write(content)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        log.info("Service updated: --input-source %s", source_name)
    except Exception as e:
        log.warning("Could not update service input-source: %s", e)


def _update_service_gate(new_gate: int):
    """Persist --mic-gate <n> in the systemd service ExecStart line."""
    try:
        with open(SERVICE_FILE) as f:
            content = f.read()
        import re as _re
        content = _re.sub(r" --mic-gate \d+", "", content)
        content = content.replace(
            "\nRestart=no",
            f" --mic-gate {new_gate}\nRestart=no",
        )
        with open(SERVICE_FILE, "w") as f:
            f.write(content)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    except Exception as e:
        log.warning("Could not update service file: %s", e)

# ── Text helpers ──────────────────────────────────────────────────────────────

def strip_markdown(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`{1,3}[^`\n]*`{1,3}', '', text)
    text = re.sub(r'^\s*#+\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text)
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    # Strip emoji and symbol characters — Piper reads them as their Unicode names
    # (e.g. Five's ⚡ becomes "high voltage"). Keep CJK for Chinese TTS.
    text = re.sub(
        r'[\U0001F000-\U0001FFFF'   # emoji / pictographs
        r'☀-➿'            # misc symbols, dingbats (includes ⚡ U+26A1)
        r'⬀-⯿'            # misc symbols & arrows
        r'︀-️]',          # variation selectors
        '', text
    )
    return text.strip()

# ── Piper TTS ─────────────────────────────────────────────────────────────────

def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (0x4E00 <= cp <= 0x9FFF or
            0x3400 <= cp <= 0x4DBF or
            0x20000 <= cp <= 0x2A6DF)

def _is_chinese_text(text: str) -> bool:
    return any(_is_cjk(c) for c in text)

def _is_likely_noise(text: str) -> bool:
    """Return True if the transcript looks like a noise hallucination.

    Two checks:
    1. Any word ≥ 4 Latin letters with ZERO standard vowels (a/e/i/o/u) —
       impossible in real English (e.g. 'Dyftm', 'ftm', 'knopk').
    2. Whole-text vowel ratio < 10% across 10+ Latin letters — catches
       dense consonant hallucinations even when split across short words.
    Skipped entirely for mostly-CJK text (Chinese has no Latin vowels).
    """
    cjk_count = sum(1 for c in text if _is_cjk(c))
    all_latin  = [c for c in text if c.isalpha() and ord(c) < 256]
    if cjk_count > len(all_latin):
        return False                            # mostly Chinese — skip

    # Check 1: any individual word with zero vowels
    for word in text.split():
        letters = [c for c in word if c.isalpha() and ord(c) < 256]
        if len(letters) >= 4 and not any(c.lower() in "aeiou" for c in letters):
            return True

    # Check 2: extremely low overall vowel density
    if len(all_latin) >= 10:
        vowels = sum(1 for c in all_latin if c.lower() in "aeiou")
        if vowels / len(all_latin) < 0.10:
            return True

    return False

def _to_simplified(text: str) -> str:
    """Normalize captured Chinese to Simplified. gpt-4o-transcribe often
    returns Traditional; convert deterministically (zhconv, pure-Python).
    Non-Chinese text passes through unchanged."""
    if not text or _zh_convert is None or not _is_chinese_text(text):
        return text
    try:
        return _zh_convert(text, "zh-cn")
    except Exception:
        return text

def _split_by_script(text: str) -> list[tuple[str, str]]:
    """Split text into [(segment, 'zh'|'en')] so each segment uses its correct Piper voice."""
    segments: list[tuple[str, str]] = []
    current_chars: list[str] = []
    current_lang = None
    for ch in text:
        lang = 'zh' if _is_cjk(ch) else 'en'
        # Chinese punctuation stays with Chinese; spaces/ASCII punct follow current lang
        if ch in ' \t\n\r，。！？；：、""‘’「」《》':
            lang = current_lang or 'en'
        if lang != current_lang and current_chars:
            seg = ''.join(current_chars).strip()
            if seg:
                segments.append((seg, current_lang or 'en'))
            current_chars = []
        current_lang = lang
        current_chars.append(ch)
    if current_chars:
        seg = ''.join(current_chars).strip()
        if seg:
            segments.append((seg, current_lang or 'en'))
    return segments

    if current_chars:
        segments.append((''.join(current_chars).strip(), current_lang or 'en'))

    return [(s, l) for s, l in segments if s]

def speak(text: str, alsa_output: str = ALSA_OUTPUT, volume: float = -1.0, silence_ms: int = 300,
          resumable: bool = False, interruptible: bool = False):
    # volume=-1 means use the calibrated level (_cal_sw_volume); pass explicit 0-1 to override
    # resumable=True: if interrupted, save (text, alsa_output) to _paused_speech for /continue
    # interruptible=True: enable user-voice interrupt detection (only for Five's main reply)
    """Synthesise text with Piper and play via aplay.

    Writes text to a temp file and runs Piper with `-i <file>` rather than piping
    via stdin — Piper silently truncates stdin input after a few words, but reads
    file input completely. Output is captured to a WAV temp file (so aplay reads
    from disk, avoiding any streaming buffer underruns on the USB speaker).
    """
    import tempfile
    if volume < 0:
        volume = _cal_sw_volume   # use calibrated level
    clean = strip_markdown(text)
    if not clean:
        return
    segments = _split_by_script(clean)
    # Pad playback so USB/PipeWire sinks do not clip the first or last phoneme.
    import wave as _wave, struct as _struct
    wav_parts: list[str] = []
    if silence_ms > 0:
        silence_path = tempfile.mktemp(suffix=".wav")
        with _wave.open(silence_path, 'wb') as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(PIPER_SAMPLE_RATE)
            wf.writeframes(b'\x00\x00' * int(PIPER_SAMPLE_RATE * silence_ms / 1000))
        wav_parts.append(silence_path)
    try:
        for seg_text, lang in segments:
            voice = PIPER_VOICE_ZH if lang == 'zh' else PIPER_VOICE_EN
            part_path = tempfile.mktemp(suffix=".wav")
            result = subprocess.run(
                [PIPER_CMD, "--model", voice, "-f", part_path, "-q"],
                input=seg_text.encode("utf-8"),
                capture_output=True, env=PIPER_ENV,
            )
            if result.returncode != 0 or not os.path.exists(part_path):
                log.error("Piper failed for %r (rc=%d): %s",
                          seg_text[:30], result.returncode,
                          result.stderr.decode(errors="replace")[:120])
                continue
            wav_parts.append(part_path)

        if silence_ms > 0 and len(wav_parts) > 1:
            tail_silence_path = tempfile.mktemp(suffix=".wav")
            with _wave.open(tail_silence_path, 'wb') as wf:
                wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(PIPER_SAMPLE_RATE)
                wf.writeframes(b'\x00\x00' * int(PIPER_SAMPLE_RATE * silence_ms / 1000))
            wav_parts.append(tail_silence_path)

        if not wav_parts:
            return

        if len(wav_parts) == 1:
            final_wav = wav_parts[0]
            wav_parts = []
        else:
            # Concatenate all WAV parts into one
            import wave as _wave
            final_wav = tempfile.mktemp(suffix=".wav")
            with _wave.open(final_wav, 'wb') as out_wf:
                for i, part in enumerate(wav_parts):
                    with _wave.open(part, 'rb') as in_wf:
                        if i == 0:
                            out_wf.setparams(in_wf.getparams())
                        out_wf.writeframes(in_wf.readframes(in_wf.getnframes()))

        # Software volume attenuation — multiply PCM samples (bypasses PipeWire floor)
        if volume < 1.0:
            import wave as _wv2
            with _wv2.open(final_wav, 'rb') as _wf:
                _params = _wf.getparams()
                _data = np.frombuffer(_wf.readframes(_wf.getnframes()), dtype=np.int16)
            _data = np.clip(_data.astype(np.float32) * volume, -32768, 32767).astype(np.int16)
            with _wv2.open(final_wav, 'wb') as _wf:
                _wf.setparams(_params)
                _wf.writeframes(_data.tobytes())

        # Sample mic level before playback (ambient baseline)
        import time as _spk_time
        with _mic_level_lock:
            baseline_peak = _mic_level_current[0]

        # Load output PCM for coupling measurement (Mac-style acoustic coupling).
        # The coupling ratio (mic_peak / output_peak) scales the interrupt threshold
        # to the actual room acoustics — tight headset → high threshold, separate
        # mic/speaker → lower threshold, always above SPEAK_INTERRUPT_PEAK floor.
        import wave as _wpcm
        try:
            with _wpcm.open(final_wav, 'rb') as _wf:
                _sr = _wf.getframerate()
                _final_pcm = np.frombuffer(_wf.readframes(_wf.getnframes()), dtype=np.int16)
        except Exception:
            _final_pcm = np.array([], dtype=np.int16)
            _sr = PIPER_SAMPLE_RATE
        _output_peak   = int(np.max(np.abs(_final_pcm))) if len(_final_pcm) else 0
        _TICK_SAMPLES  = max(1, _sr * 50 // 1000)   # samples per 50ms tick
        _GUARD_TICKS   = 20                          # 1s guard: 300ms silence + 700ms audio
        _SAFETY        = 3.5                         # threshold = echo × 3.5 (reverb can be 3-4× guard measurement)

        mic_peaks_during: list[int] = []
        _interrupted   = [False]
        _aplay_rc      = [0]

        def _monitor_and_play(cmd):
            proc = subprocess.Popen(cmd, stderr=subprocess.DEVNULL)
            consec      = 0
            guard       = _GUARD_TICKS
            guard_max_out = 0   # peak output PCM during guard
            guard_max_mic = 0   # peak mic echo during guard
            interrupt_threshold = [SPEAK_INTERRUPT_PEAK]
            tick_idx    = 0
            while True:
                try:
                    _aplay_rc[0] = proc.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    pass
                with _mic_level_lock:
                    p = _mic_level_current[0]
                mic_peaks_during.append(p)

                if guard > 0:
                    # Measure output PCM for this tick to compute coupling
                    s0 = tick_idx * _TICK_SAMPLES
                    s1 = s0 + _TICK_SAMPLES
                    if len(_final_pcm) and s1 <= len(_final_pcm):
                        tick_out = int(np.max(np.abs(_final_pcm[s0:s1])))
                        if tick_out > guard_max_out:
                            guard_max_out = tick_out
                    if p > guard_max_mic:
                        guard_max_mic = p
                    tick_idx += 1
                    guard -= 1
                    if guard == 0:
                        if guard_max_out > 200:
                            coupling = guard_max_mic / guard_max_out
                            interrupt_threshold[0] = max(
                                int(_output_peak * coupling * _SAFETY),
                                SPEAK_INTERRUPT_PEAK,
                            )
                            log.info("TTS coupling=%.3f echo=%d out=%d → threshold=%d",
                                     coupling, guard_max_mic, guard_max_out,
                                     interrupt_threshold[0])
                        else:
                            log.info("TTS no coupling data → threshold=%d (floor)",
                                     interrupt_threshold[0])
                    continue

                if _http_interrupt[0]:
                    _http_interrupt[0] = False
                    _interrupted[0] = True
                    try: proc.kill()
                    except Exception: pass
                    break
                if p > interrupt_threshold[0]:
                    consec += 1
                    if consec >= SPEAK_INTERRUPT_BLOCKS:
                        log.info("Speech interrupt — stopping TTS (peak=%d thr=%d)",
                                 p, interrupt_threshold[0])
                        _interrupted[0] = True
                        _clear_audio_buffer[0] = True
                        try: proc.kill()
                        except Exception: pass
                        break
                else:
                    consec = 0

        # If AIOC PTT is available, route audio to the AIOC sink and key the radio.
        import time as _ptt_t
        _aioc_sink = _find_aioc_sink() if (_ptt_alive() and _radio_profile_active[0]) else None
        _use_ptt   = bool(_aioc_sink)

        # Use paplay (PipeWire-native) for default/pulse sink — better resampling and
        # Bluetooth handling than aplay -D default (ALSA compat layer).
        # When AIOC is active, always route through its PipeWire sink via paplay.
        if _use_ptt:
            _play_cmd = ["paplay", f"--device={_aioc_sink}", final_wav]
        elif alsa_output in ("default", "pulse"):
            _play_cmd = ["paplay", final_wav]
        else:
            _play_cmd = ["aplay", "-D", alsa_output, "-q", final_wav]
        _play_fallback = ["paplay", final_wav]

        if _use_ptt:
            _ptt_key()
            _ptt_t.sleep(AIOC_PTT_PREKEY_MS / 1000)
            log.info("PTT keyed — transmitting")

        _is_speaking[0] = True
        if interruptible:
            _m = _threading.Thread(daemon=True, target=_monitor_and_play,
                                   args=(_play_cmd,))
            _m.start(); _m.join()
            if not _interrupted[0] and _aplay_rc[0] != 0 and _play_cmd != _play_fallback:
                log.warning("playback failed on %s, retrying via paplay", alsa_output)
                _m2 = _threading.Thread(daemon=True, target=_monitor_and_play,
                                        args=(_play_fallback,))
                _m2.start(); _m2.join()
        else:
            # Non-interruptible: play to completion, no interrupt monitor
            rc = subprocess.call(_play_cmd, stderr=subprocess.DEVNULL)
            if rc != 0 and _play_cmd != _play_fallback:
                subprocess.call(_play_fallback, stderr=subprocess.DEVNULL)
        _is_speaking[0] = False

        if _use_ptt:
            _ptt_t.sleep(AIOC_PTT_TAIL_MS / 1000)
            _ptt_release()
            log.info("PTT released")

        # Save text for /continue if interrupted mid-sentence; clear on normal finish.
        if _interrupted[0] and resumable:
            _paused_speech[0] = (strip_markdown(text), alsa_output)
            log.info("TTS interrupted — saved %d chars for /continue", len(strip_markdown(text)))
        elif not _interrupted[0]:
            _paused_speech[0] = None

        # Auto-reduce: only fire on genuinely loud bleed (20× baseline AND >2000
        # absolute). With WebRTC AGC the baseline is near-zero so the old 5×/500
        # thresholds fired on every normal playback and collapsed the volume.
        if _interrupted[0] or speak.__globals__.get("_skip_auto_reduce", False):
            pass
        elif mic_peaks_during:
            avg_during = sum(mic_peaks_during) / len(mic_peaks_during)
            if baseline_peak > 0 and avg_during > baseline_peak * 40 and avg_during > 4000:
                try:
                    import re as _re
                    sinks = subprocess.run(["pactl", "list", "short", "sinks"],
                                           capture_output=True, text=True).stdout
                    for line in sinks.splitlines():
                        parts = line.split()
                        if (len(parts) >= 2 and "hdmi" not in parts[1].lower()
                                and "monitor" not in parts[1].lower()
                                and not parts[1].startswith("rtt_agc")):
                            cur = subprocess.run(["pactl", "get-sink-volume", parts[0]],
                                                 capture_output=True, text=True).stdout
                            m = _re.search(r'(\d+)%', cur)
                            if m:
                                cur_pct = int(m.group(1))
                                new_pct = max(10, cur_pct - 10)
                                subprocess.run(["pactl", "set-sink-volume", parts[0],
                                                f"{new_pct}%"], capture_output=True)
                                log.info("Auto-reduced speaker %d%%→%d%% "
                                         "(bleed %.0f > 20×baseline %.0f)",
                                         cur_pct, new_pct, avg_during, baseline_peak)
                except Exception as e:
                    log.debug("Auto-volume error: %s", e)

    except Exception as e:
        log.error("speak() error: %s", e)
    finally:
        _is_speaking[0] = False
        # Short gate when interrupted (speaker stops instantly, no echo tail).
        # Full 600 ms gate when TTS completes normally (speaker rings down).
        import time as _t_sp
        _post_busy_until[0] = _t_sp.time() + (0.15 if _interrupted[0] else 0.6)
        for p in wav_parts:
            try: os.unlink(p)
            except FileNotFoundError: pass
        try: os.unlink(final_wav)
        except (FileNotFoundError, UnboundLocalError): pass

# ── OpenClaw gateway client ───────────────────────────────────────────────────

class GatewayClient:
    """
    Persistent WebSocket operator connection to the local OpenClaw gateway.

    Uses the trusted backend-client path (client.id="gateway-client",
    client.mode="backend") which bypasses device-pairing scope upgrades for
    loopback connections authenticated with the shared gateway token.
    """

    def __init__(self, token: str):
        self.token = token
        self._ws = None
        self._ready = asyncio.Event()   # set when WS is fully handshaked
        # Maps request-id → Future for chat.send acks
        self._send_acks: dict[str, asyncio.Future] = {}
        # Maps runId → Future[str] for final chat replies
        self._reply_futs: dict[str, asyncio.Future] = {}
        # Maps runId → latest assistant-stream text (fallback if chat final empty)
        self._assistant_text: dict[str, str] = {}

    async def connect(self):
        self._ready.clear()
        self._ws = await websockets.connect(
            OPENCLAW_GW_URL, ping_interval=25, ping_timeout=10
        )
        await self._ws.recv()  # connect.challenge — backend clients skip signing
        await self._ws.send(json.dumps({
            "type": "req", "id": "gw-connect", "method": "connect",
            "params": {
                "minProtocol": 4, "maxProtocol": 4,
                "client": {
                    "id": "gateway-client", "version": "1.2.0",
                    "platform": "linux", "mode": "backend",
                },
                "role": "operator",
                "scopes": ["operator.read", "operator.write"],
                "caps": [], "commands": [], "permissions": {},
                "auth": {"token": self.token},
                "locale": "en-US",
                "userAgent": "realtimetalk/1.2",
            },
        }))
        hello = json.loads(await self._ws.recv())
        if not hello.get("ok"):
            err = hello.get("error") or {}
            if err.get("retryable"):
                raise ConnectionRefusedError(err.get("message", "gateway not ready"))
            raise RuntimeError(f"Gateway connect failed: {hello.get('error')}")
        scopes = hello.get("payload", {}).get("auth", {}).get("scopes", [])
        log.info("OpenClaw gateway connected (scopes: %s)", scopes)
        self._ready.set()

    async def listen(self, stop_event: asyncio.Event):
        """Route incoming gateway events to waiting futures. Auto-reconnects on drop."""
        while not stop_event.is_set():
            try:
                async for raw in self._ws:
                    if stop_event.is_set():
                        return
                    msg = json.loads(raw)
                    mtype = msg.get("type", "")
                    event = msg.get("event", "")
                    payload = msg.get("payload") or {}
                    msg_id = msg.get("id", "")

                    # Resolve chat.send acks
                    if mtype == "res" and msg_id in self._send_acks:
                        fut = self._send_acks.pop(msg_id)
                        if not fut.done():
                            fut.set_result(msg)

                    # Track assistant-stream text as a reliable reply source
                    elif event == "agent" and payload.get("stream") == "assistant":
                        rid = payload.get("runId")
                        atext = (payload.get("data") or {}).get("text", "")
                        if rid and atext:
                            self._assistant_text[rid] = atext

                    # Resolve agent replies on final chat event
                    elif event == "chat" and payload.get("state") == "final":
                        run_id = payload.get("runId")
                        cmsg = payload.get("message", {}) or {}
                        content = cmsg.get("content", []) or []
                        # Standard content array (type=text)
                        text = " ".join(
                            c.get("text", "") for c in content if c.get("type") == "text"
                        ).strip()
                        # Fallback: Responses API output_text items
                        if not text:
                            text = " ".join(
                                c.get("text", "") for c in content
                                if c.get("type") in ("output_text", "text_delta")
                            ).strip()
                        # Fallback: top-level text / deltaText
                        if not text:
                            text = (cmsg.get("text") or payload.get("deltaText") or "").strip()
                        # Fallback: assistant-stream text captured during the run
                        if not text:
                            text = self._assistant_text.get(run_id, "").strip()
                        if not text:
                            log.warning("chat final empty: payload=%s",
                                        json.dumps(payload)[:600])
                        self._assistant_text.pop(run_id, None)
                        fut = self._reply_futs.pop(run_id, None)
                        if fut and not fut.done():
                            fut.set_result(text)

            except websockets.ConnectionClosed as e:
                if stop_event.is_set():
                    return
                log.warning("Gateway connection dropped (%s) — reconnecting…", e)
            except Exception as e:
                if stop_event.is_set():
                    return
                log.warning("Gateway listen error (%s) — reconnecting…", e)

            if stop_event.is_set():
                return

            # Fail any in-flight futures so ask() doesn't hang
            self._ready.clear()
            for fut in list(self._send_acks.values()):
                if not fut.done():
                    fut.set_exception(ConnectionError("Gateway reconnecting"))
            self._send_acks.clear()
            for fut in list(self._reply_futs.values()):
                if not fut.done():
                    fut.set_exception(ConnectionError("Gateway reconnecting"))
            self._reply_futs.clear()

            while not stop_event.is_set():
                try:
                    await self.connect()
                    log.info("Gateway reconnected.")
                    _log_entry("system", "Gateway reconnected.")
                    break
                except Exception as e:
                    log.warning("Gateway reconnect failed (%s) — retrying in 5s…", e)
                    await asyncio.sleep(5)

    async def ask(self, message: str, session_key: str = OPENCLAW_SESSION) -> str:
        """Send a message to the agent and return its complete reply text."""
        await asyncio.wait_for(self._ready.wait(), timeout=20)
        loop = asyncio.get_running_loop()
        idem = str(uuid.uuid4())
        req_id = f"send:{idem}"

        ack_fut: asyncio.Future = loop.create_future()
        self._send_acks[req_id] = ack_fut

        await self._ws.send(json.dumps({
            "type": "req", "id": req_id, "method": "chat.send",
            "params": {
                "sessionKey": session_key,
                "message": message,
                "idempotencyKey": idem,
            },
        }))

        ack = await asyncio.wait_for(ack_fut, timeout=10)
        if not ack.get("ok"):
            raise RuntimeError(f"chat.send failed: {ack.get('error')}")

        run_id = ack.get("payload", {}).get("runId")
        if not run_id:
            raise RuntimeError("chat.send returned no runId")

        reply_fut: asyncio.Future = loop.create_future()
        self._reply_futs[run_id] = reply_fut

        # Register with agent.wait so the gateway tracks this run
        await self._ws.send(json.dumps({
            "type": "req", "id": f"wait:{run_id}", "method": "agent.wait",
            "params": {"runId": run_id, "timeoutMs": AGENT_TIMEOUT_S * 1000},
        }))

        text = await asyncio.wait_for(reply_fut, timeout=AGENT_TIMEOUT_S + 5)
        # Codex harness delivers replies via the `message` tool, not chat
        # content — the chat-final event is empty. Pull the reply from
        # chat.history where the message-tool call arguments are persisted.
        # Also catch short gateway status tokens ("Sent.", "Done.", "OK", etc.)
        # that surface as the chat-final text instead of the real reply.
        _stripped = (text or "").strip().rstrip(".")
        _is_status_token = (
            len(text or "") < 25
            and _stripped.lower() in ("sent", "ok", "done", "error", "failed",
                                      "accepted", "received")
        )
        if not text or _is_status_token:
            if _is_status_token:
                log.info("Status token %r — fetching reply from history", text)
            await asyncio.sleep(1.2)  # let message-tool result fully persist
            text = await self._reply_from_history(session_key)
            # Reject stale history: if it matches the last reply we already
            # delivered, the agent hasn't produced a new response yet.
            if text and text == _last_five_reply[0]:
                log.warning("History returned same reply as last time — treating as stale")
                text = ""
        if text:
            _last_five_reply[0] = text
        return text

    async def _reply_from_history(self, session_key: str) -> str:
        """Fetch the latest assistant reply from chat.history.

        Handles the codex harness `message`-tool delivery as well as plain
        assistant text (automatic mode).
        """
        loop = asyncio.get_running_loop()
        hid = f"hist:{uuid.uuid4()}"
        hfut: asyncio.Future = loop.create_future()
        self._send_acks[hid] = hfut
        try:
            await self._ws.send(json.dumps({
                "type": "req", "id": hid, "method": "chat.history",
                "params": {"sessionKey": session_key, "limit": 12},
            }))
            resp = await asyncio.wait_for(hfut, timeout=10)
        except (asyncio.TimeoutError, Exception) as e:
            self._send_acks.pop(hid, None)
            log.warning("chat.history fetch failed: %s", e)
            return ""
        msgs = resp.get("payload", {}).get("messages", []) or []
        for m in reversed(msgs):
            if m.get("role") != "assistant":
                continue
            content = m.get("content", [])
            if isinstance(content, str):
                if content.strip():
                    return content.strip()
                continue
            if not isinstance(content, list):
                continue
            # Codex message-tool call
            for c in content:
                if c.get("type") == "toolCall" and c.get("name") == "message":
                    args = c.get("arguments") or c.get("input") or {}
                    txt = (args.get("message") or "").strip()
                    if txt:
                        return txt
            # Plain assistant text (automatic / non-codex)
            txt = " ".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ).strip()
            if txt:
                return txt
        log.warning("chat.history: no assistant reply found in %d msgs", len(msgs))
        return ""

    async def close(self):
        if self._ws:
            await self._ws.close()

# ── OpenAI Realtime session (VAD + STT only) ──────────────────────────────────

class RealtimeSession:
    """
    Connects to OpenAI Realtime API solely for voice activity detection and
    speech-to-text. Does not generate AI responses (create_response: false).
    """

    def __init__(self, api_key: str, loop: asyncio.AbstractEventLoop,
                 gw: GatewayClient, stop_event: asyncio.Event,
                 input_device=None, alsa_output: str = ALSA_OUTPUT,
                 session_key: str = OPENCLAW_SESSION):
        self.api_key      = api_key
        self.loop         = loop
        self.gw           = gw
        self.stop_event   = stop_event
        self.input_device = input_device
        self.alsa_output  = alsa_output
        self.session_key  = session_key
        self._mic_q       = asyncio.Queue(maxsize=200)
        self._busy        = asyncio.Event()   # set while Five is speaking
        self._cal_peaks: list[int] = []       # raw peaks collected during calibration
        self._calibrating = False
        self._active      = _persist_active[0]           # restored from last session
        self._monitoring  = _persist_monitoring[0]     # restored from last session
        self._multilang   = _persist_multilang[0]  # restored from last session
        self._mic_stream_ref: list = [None]   # current sd.InputStream; swapped on hot-plug

    def _mic_cb(self, indata, frames, time_info, status):
        import time as _tcb
        _last_mic_cb[0] = _tcb.time()
        raw = indata[::RESAMPLE_RATIO, 0]
        raw_peak = int(np.max(np.abs(raw)))
        with _mic_level_lock:
            _mic_level_current[0] = raw_peak
        # While calibrating, record raw peaks (no gain/gate applied, mic suppression off)
        if self._calibrating:
            self.loop.call_soon_threadsafe(self._cal_peaks.append, raw_peak)
            return
        import time as _tcb2
        if self._busy.is_set() or _tcb2.time() < _post_busy_until[0]:
            return  # discard mic input while Five is speaking or during echo gate
        if raw_peak < MIC_GATE_PEAK:
            out_arr = np.zeros_like(raw)
        else:
            boosted = raw.astype(np.float32) * MIC_GAIN
            out_arr = np.clip(boosted, -32768, 32767).astype(np.int16)
        self.loop.call_soon_threadsafe(self._enqueue_mic, out_arr.tobytes())

    def _enqueue_mic(self, data: bytes):
        try:
            self._mic_q.put_nowait(data)
        except asyncio.QueueFull:
            pass

    async def _watch_mic_stream(self):
        """Detect USB mic hot-unplug and reopen the stream when replugged."""
        import time as _wm
        await asyncio.sleep(5.0)   # let stream settle before watching
        while not self.stop_event.is_set():
            await asyncio.sleep(2.0)
            if self.stop_event.is_set():
                break
            elapsed = _wm.time() - _last_mic_cb[0]
            if elapsed < 4.0:
                continue
            log.warning("Mic silent %.1fs — hot-plug recovery starting", elapsed)
            old = self._mic_stream_ref[0]
            try:
                if old:
                    old.stop()
                    old.close()
            except Exception:
                pass
            self._mic_stream_ref[0] = None
            # PortAudio caches device list at init — need terminate + reinitialize to see new device
            await asyncio.sleep(1.5)
            try:
                sd._terminate()
                sd._initialize()
                log.info("PortAudio reinitialized for hot-plug")
            except Exception as e:
                log.warning("PortAudio reinit error: %s", e)
            try:
                new_stream = sd.InputStream(
                    samplerate=DEVICE_RATE, channels=CHANNELS, dtype="int16",
                    blocksize=DEVICE_BLOCKSIZE, callback=self._mic_cb,
                    device=self.input_device,
                )
                new_stream.start()
                self._mic_stream_ref[0] = new_stream
                _last_mic_cb[0] = _wm.time()
                log.info("Mic stream reopened after hot-plug")
                _log_entry("system", "Mic reconnected.")
            except Exception as e:
                log.warning("Mic reconnect failed (%s) — will retry", e)
                _last_mic_cb[0] = _wm.time()  # back off

    async def _resume_from_http(self, text: str, alsa_output: str):
        """Resume paused TTS triggered by the /continue HTTP button."""
        if self._busy.is_set():
            return
        self._busy.set()
        try:
            _log_entry("system", "Resuming…")
            await asyncio.get_running_loop().run_in_executor(
                None, speak, text, alsa_output
            )
        finally:
            import time as _t_gate
            _post_busy_until[0] = _t_gate.time() + 0.6
            self._busy.clear()

    async def _run_calibration(self):
        """Measure ambient noise via the live mic stream and update MIC_GATE_PEAK."""
        global MIC_GATE_PEAK
        await asyncio.get_running_loop().run_in_executor(
            None, speak, "Calibrating mic. Stay quiet for three seconds.", self.alsa_output
        )
        self._cal_peaks.clear()
        self._calibrating = True
        await asyncio.sleep(3.0)
        self._calibrating = False
        peaks = self._cal_peaks[2:]  # discard startup frames
        if not peaks:
            await asyncio.get_running_loop().run_in_executor(
                None, speak, "Calibration failed. No mic data.", self.alsa_output
            )
            return
        noise_peak = max(peaks)
        new_gate = max(MIC_GATE_MIN, min(MIC_GATE_MAX, int(noise_peak * 1.5)))
        MIC_GATE_PEAK = new_gate
        log.info("Calibration: noise_peak=%d → MIC_GATE_PEAK=%d", noise_peak, new_gate)
        # Persist to service file so it survives restarts
        _update_service_gate(new_gate)
        await asyncio.get_running_loop().run_in_executor(
            None, speak,
            f"Done. Noise gate set to {new_gate}. Speak normally now.",
            self.alsa_output
        )

    async def _send_mic(self, ws):
        while not self.stop_event.is_set():
            # Apply DTMF force flags immediately (don't wait for next transcript)
            if _dtmf_force_active[0]:
                _dtmf_force_active[0] = False
                if not self._active:
                    self._active = True
                    _last_activity[0] = __import__("time").time()
                    _log_entry("system", "Voice activated")
                    log.info("DTMF force-active applied to session")
            if _dtmf_force_monitor[0] is not None:
                _mon = _dtmf_force_monitor[0]
                _dtmf_force_monitor[0] = None
                if _mon and not self._monitoring:
                    self._monitoring = True
                    self._active = False   # monitoring is passive
                    _log_entry("system", "Monitoring started")
                    log.info("DTMF force-monitor ON")
                elif not _mon and self._monitoring:
                    self._monitoring = False
                    _log_entry("system", "Monitoring stopped")
                    log.info("DTMF force-monitor OFF")
            if _dtmf_force_deepsleep[0]:
                _dtmf_force_deepsleep[0] = False
                _persist_active[0] = False
                _persist_monitoring[0] = False
                self._monitoring = False   # turn off monitoring on current session
                _idle_disconnected[0] = True
                _save_sleep_state(True)
                log.info("DTMF deep-sleep — closing WebSocket")
                if self._ws:
                    # Close WebSocket; session.run() will end and main loop enters sleep-wait
                    asyncio.get_event_loop().call_soon_threadsafe(
                        asyncio.ensure_future,
                        self._ws.close()
                    )
            if _dtmf_force_silent[0]:
                _dtmf_force_silent[0] = False
                self._active = False
                _log_entry("system", "Voice silenced")
                log.info("DTMF force-silent applied to session")
            try:
                chunk = await asyncio.wait_for(self._mic_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if self._busy.is_set():
                continue
            # After TTS interrupt, clear OpenAI's audio buffer so stale data
            # doesn't confuse VAD — let the user start fresh.
            if _clear_audio_buffer[0]:
                _clear_audio_buffer[0] = False
                await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
            await ws.send(json.dumps({
                "type":  "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode(),
            }))

    async def _handle_transcript(self, transcript: str):
        # Apply DTMF force flags (belt-and-suspenders alongside _send_mic)
        if _dtmf_force_active[0]:
            _dtmf_force_active[0] = False
            if not self._active:
                self._active = True
                _last_activity[0] = __import__("time").time()
                _log_entry("system", "Voice activated")
        if _dtmf_force_silent[0]:
            _dtmf_force_silent[0] = False
            self._active = False
            _log_entry("system", "Voice silenced")

        # Default to Simplified Chinese (transcriber often returns Traditional)
        transcript = _to_simplified(transcript)

        # Normalize and check control phrases BEFORE the language gate so that
        # wake/sleep/calibrate always work even if the transcriber produces a
        # slightly garbled or accented variant that _is_english_or_chinese rejects.
        normalized = transcript.strip().rstrip(".!?,").lower()

        # Wake phrase — always checked regardless of active state
        if _matches_phrase(normalized, WAKE_PHRASES):
            self._busy.set()
            try:
                if self._monitoring:
                    self._monitoring = False
                    _persist_monitoring[0] = False
                    log.info("Wake phrase detected — exiting monitoring, voice active")
                if not self._active:
                    self._active = True
                    _persist_active[0] = True
                    import time as _tact; _last_activity[0] = _tact.time()
                    log.info("Wake phrase detected — voice active")
                    _log_entry("system", "Voice activated")
                    await asyncio.get_running_loop().run_in_executor(
                        None, speak, "I'm listening.", self.alsa_output
                    )
                else:
                    log.info("Wake phrase detected — already active")
                    await asyncio.get_running_loop().run_in_executor(
                        None, speak, "Yes, I'm here.", self.alsa_output
                    )
            finally:
                self._busy.clear()
            return

        # Sleep phrase — only meaningful when active
        if _matches_phrase(normalized, SLEEP_PHRASES):
            if self._active:
                self._active = False
                _persist_active[0] = False
                log.info("Sleep phrase detected — going silent")
                _log_entry("system", "Voice silenced")
                self._busy.set()
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, speak, "Going silent now. Say Five wake up to resume.", self.alsa_output
                    )
                finally:
                    self._busy.clear()
            return

        # Calibration — works in both modes (audio feedback either way)
        if normalized in CALIBRATE_PHRASES:
            log.info("Voice command: calibrate mic")
            asyncio.create_task(self._run_calibration())
            return

        # Monitoring toggle — works regardless of active state
        if _matches_phrase(normalized, MONITOR_ON_PHRASES):
            if not self._monitoring:
                self._monitoring = True
                _persist_monitoring[0] = True
                log.info("Voice command: monitoring ON")
                _log_entry("system", "Monitoring started.")
                await asyncio.get_running_loop().run_in_executor(
                    None, speak, "Monitoring started.", self.alsa_output
                )
            return
        if _matches_phrase(normalized, MONITOR_OFF_PHRASES):
            if self._monitoring:
                self._monitoring = False
                _persist_monitoring[0] = False
                log.info("Voice command: monitoring OFF")
                _log_entry("system", "Monitoring stopped.")
                await asyncio.get_running_loop().run_in_executor(
                    None, speak, "Monitoring stopped.", self.alsa_output
                )
            return

        # Continue phrase — resume paused TTS without asking Five again
        if _matches_phrase(normalized, CONTINUE_PHRASES):
            saved = _paused_speech[0]
            if saved and not self._busy.is_set():
                _paused_speech[0] = None  # clear immediately so concurrent tasks don't re-enter
                saved_text, saved_dev = saved
                log.info("Voice continue — resuming %d chars", len(saved_text))
                _log_entry("system", "Resuming…")
                self._busy.set()
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, speak, saved_text, saved_dev
                    )
                finally:
                    import time as _t_gate2
                    _post_busy_until[0] = _t_gate2.time() + 0.6
                    self._busy.clear()
            return

        # Language gate — behaviour depends on multi-lang mode:
        #   "off" / "en-zh" → EN/ZH only   "whitelist" → MULTILANG_WHITELIST_LANGS
        #   "any"           → all languages pass through
        if self._multilang in ("off", "en-zh"):
            if not _is_english_or_chinese(transcript):
                log.debug("Dropped non-EN/ZH (mode=%s): %r", self._multilang, transcript)
                return
        elif self._multilang == "whitelist":
            if not _is_in_multilang_whitelist(transcript):
                log.debug("Dropped off-whitelist: %r", transcript)
                return

        # Monitoring-only mode: passively log captured segments (no Five/TTS).
        # Intentionally does NOT update _last_activity — monitoring is passive
        # and must not prevent auto-sleep from firing.
        if self._monitoring:
            t = transcript.strip()
            if t:
                log.info("Monitor: %s", t)
                _log_entry("monitor", t)
            return

        # Suppress transcripts while PTT is asserted — prevents Five's own
        # transmitted voice from being picked up and re-routed as a new command.
        if _is_tx[0]:
            log.debug("PTT TX active — suppressing transcript: %r", transcript)
            return

        # Noise hallucination filter: drop consonant-heavy gibberish.
        if _is_likely_noise(transcript):
            log.debug("Dropped noise hallucination: %r", transcript)
            return

        # All other speech: only route to Five when active
        if not self._active:
            log.debug("Silent mode — ignoring: %s", transcript)
            return

        # Short-word noise guard: single words under 9 characters that aren't
        # known commands are almost always noise hallucinations or foreign-word
        # hallucinations (e.g. "Esquece", "Senhores", "Legjeni") that slip past
        # the character-level language filter. langdetect is unreliable on single
        # short words so we handle them here instead.
        _SHORT_CMDS = {"ok", "okay", "yes", "no", "sure", "go", "stop", "wait",
                       "help", "hey", "hi", "bye", "right", "great", "thanks",
                       "please", "repeat", "exactly", "correct", "alright",
                       "好", "是", "否", "不", "对", "继续", "再来", "谢谢", "好的"}
        _norm_words = normalized.split()
        if len(_norm_words) == 1 and len(normalized) < 9 and normalized not in _SHORT_CMDS:
            log.info("Short noise guard — dropped single word: %r", transcript)
            return

        # New request — discard any previously paused speech
        _paused_speech[0] = None

        import time as _tact2; _last_activity[0] = _tact2.time()
        self._busy.set()
        try:
            log.info("Routing to Five: %s", transcript)
            _log_entry("you", transcript)
            _log_entry("thinking", "Five is thinking...")  # live counter shown on dashboard
            # Prefix tells Five to ignore cron/heartbeat background context
            voice_msg = f"[voice] {transcript}"
            _think_task = asyncio.ensure_future(
                self.gw.ask(voice_msg, session_key=self.session_key)
            )
            _current_think_task[0] = _think_task
            try:
                reply = await _think_task
            except asyncio.CancelledError:
                log.info("Thinking interrupted via /interrupt")
                _log_entry("five", "")   # clears thinking counter
                _log_entry("system", "Interrupted.")
                return
            finally:
                _current_think_task[0] = None
            if not reply:
                log.warning("History fallback also empty — no reply from Five")
                _log_entry("system", "No reply from Five — please try again.")
                await asyncio.get_running_loop().run_in_executor(
                    None, speak, "Sorry, I didn't get a response. Please try again.",
                    self.alsa_output
                )
                return
            log.info("Five: %s", reply)
            _log_entry("five", reply)
            await asyncio.get_running_loop().run_in_executor(
                None, speak, reply, self.alsa_output, -1.0, 300, True, True  # resumable, interruptible
            )
        except asyncio.TimeoutError:
            log.error("OpenClaw agent timed out")
            _log_entry("five", "")   # clears the thinking counter on dashboard
            await asyncio.get_running_loop().run_in_executor(
                None, speak, "Sorry, I timed out on that.", self.alsa_output
            )
        except Exception as e:
            log.error("Error routing transcript: %s", e)
            _log_entry("five", "")   # clears the thinking counter on dashboard
        finally:
            # Short gate when TTS was interrupted (speaker stops instantly — 150 ms
            # is enough for room echo to clear).  Full gate after normal completion.
            import time as _t_gate3
            _post_busy_until[0] = _t_gate3.time() + (0.15 if _paused_speech[0] is not None else 0.6)
            self._busy.clear()

    async def _recv_ws(self, ws):
        async for raw in ws:
            if self.stop_event.is_set():
                break
            msg = json.loads(raw)
            t   = msg.get("type", "")

            if t in ("conversation.item.done", "conversation.item.input_audio_transcription.completed"):
                # transcription endpoint: transcript in item.content[].transcript
                # old realtime endpoint: transcript in top-level .transcript
                transcript = msg.get("transcript", "")
                if not transcript:
                    for chunk in msg.get("item", {}).get("content", []):
                        if chunk.get("type") == "input_audio" and chunk.get("transcript"):
                            transcript = chunk["transcript"]
                            break
                transcript = transcript.strip()
                if transcript and not self._busy.is_set() and not _is_speaking[0]:
                    log.info("You: %s", transcript)
                    asyncio.create_task(self._handle_transcript(transcript))

            elif t == "error":
                log.error("OpenAI error: %s", msg.get("error", msg))

            elif t not in (
                "input_audio_buffer.speech_started",
                "input_audio_buffer.speech_stopped",
                "input_audio_buffer.committed",
                "conversation.item.created",
                "conversation.item.added",
                "conversation.item.done",
                "conversation.item.input_audio_transcription.delta",
                "transcription_session.updated",
                "session.updated",
                "session.created",
            ):
                log.debug("OpenAI event: %s", t)

    async def _idle_watcher(self, ws):
        """Close the OpenAI WebSocket after IDLE_SLEEP_MINS of no activity."""
        import time as _ti
        while not self.stop_event.is_set():
            await asyncio.sleep(30)
            if self._multilang != "off":
                continue  # any non-off state keeps session alive indefinitely
            idle = _ti.time() - _last_activity[0]
            if idle >= IDLE_SLEEP_MINS * 60:
                mins = int(idle / 60)
                log.info("Auto-sleep: idle %d min — disconnecting from OpenAI", mins)
                _log_entry("system", f"Auto-sleep after {mins} min idle. Say 'Hey Jarvis' or press Wake to resume.")
                if self._monitoring:
                    self._monitoring = False
                    _persist_monitoring[0] = False
                    log.info("Auto-sleep: monitoring turned off")
                if self._multilang != "off":
                    self._multilang = "off"
                    _persist_multilang[0] = "off"
                    log.info("Auto-sleep: multi-lang reset to off")
                _persist_active[0] = False
                _idle_disconnected[0] = True
                _save_sleep_state(True)
                await asyncio.get_running_loop().run_in_executor(
                    None, speak, "Going to sleep.", self.alsa_output
                )
                await ws.close()
                return

    async def run(self):
        log.info("Connecting to OpenAI Realtime API (STT mode)…")
        async with websockets.connect(
            OPENAI_WS_URL,
            additional_headers={
                "Authorization": f"Bearer {self.api_key}",
            },
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "type": "transcription",
                    "audio": {
                        "input": {
                            "transcription": {"model": OPENAI_TRANSCRIBE_MODEL},
                            "turn_detection": {
                                "type":                "server_vad",
                                "threshold":           0.35,  # more sensitive; AGC+noise-suppression keeps false triggers low
                                "prefix_padding_ms":   500,   # capture speech onset better ("Five,…")
                                "silence_duration_ms": 700,   # faster end-of-utterance; AGC keeps inter-word gaps short
                            },
                        },
                    },
                },
            }))
            log.info("Session active — speak now (routed through Five / OpenClaw)")

            import time as _st
            _last_mic_cb[0] = _st.time()   # seed so watchdog doesn't fire immediately
            in_stream = sd.InputStream(
                samplerate=DEVICE_RATE, channels=CHANNELS, dtype="int16",
                blocksize=DEVICE_BLOCKSIZE, callback=self._mic_cb,
                device=self.input_device,
            )
            in_stream.start()
            self._mic_stream_ref[0] = in_stream
            try:
                tasks = [
                    asyncio.create_task(self._send_mic(ws)),
                    asyncio.create_task(self._recv_ws(ws)),
                    asyncio.create_task(self.stop_event.wait()),
                    asyncio.create_task(self._watch_mic_stream()),
                    asyncio.create_task(self._idle_watcher(ws)),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
            finally:
                try:
                    in_stream.stop()
                    in_stream.close()
                except Exception:
                    pass
                self._mic_stream_ref[0] = None

# ── HTTP toggle server ────────────────────────────────────────────────────────

def start_http_server(port: int, on_stop, session_ref: list):
    """session_ref is a one-element list holding the current RealtimeSession (or None)."""
    def _html(handler, code: int, body: str):
        data = body.encode()
        handler.send_response(code)
        handler.send_header("Content-Type",   "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("[http] %s", fmt % args)

        def do_GET(self):
            sess = session_ref[0]
            if self.path == "/stop":
                _html(self, 200, "<h2>OpenClaw RealTimeTalk: stopping…</h2>")
                on_stop()
            elif self.path == "/restart":
                _html(self, 200, "<h2>Restarting…</h2><p>Page will reload in 5 seconds.</p><script>setTimeout(()=>location.href='/dashboard',5000)</script>")
                threading.Thread(target=lambda: (
                    __import__('time').sleep(1),
                    __import__('subprocess').run(['systemctl','--user','restart','openclaw-realtimetalk'])
                ), daemon=True).start()
            elif self.path == "/wake":
                if _idle_disconnected[0] and _wake_event[0]:
                    # Reconnect from auto-sleep
                    _last_activity[0] = __import__("time").time()
                    _wake_activate[0] = True
                    _save_sleep_state(False)
                    _wake_event[0].set()
                    log.info("HTTP wake — reconnecting from auto-sleep")
                elif sess:
                    sess._active = True
                    _persist_active[0] = True
                    if sess._monitoring:
                        sess._monitoring = False
                        _persist_monitoring[0] = False
                        log.info("HTTP wake — exiting monitoring mode")
                    _last_activity[0] = __import__("time").time()
                    log.info("HTTP wake")
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path == "/sleep":
                if sess:
                    sess._active = False
                    if sess._monitoring:
                        sess._monitoring = False
                        _persist_monitoring[0] = False
                        log.info("HTTP sleep: monitoring cleared")
                    log.info("HTTP sleep")
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path in ("/monitor", "/monitor/start", "/monitor/stop"):
                # Passive capture-only monitoring (no Five, no TTS).
                # /monitor toggles; /monitor/start and /monitor/stop are explicit.
                _want_monitor = (self.path == "/monitor/start" or
                                 (self.path == "/monitor" and not _persist_monitoring[0]))
                if not sess and _idle_disconnected[0] and _wake_event[0]:
                    # SLEEPING with no session — pre-arm monitoring and wake
                    if _want_monitor:
                        _persist_monitoring[0] = True
                        _persist_active[0] = False   # monitoring is silent, not active
                        import time as _tmon_w; _last_activity[0] = _tmon_w.time()
                        _wake_event[0].set()
                        log.info("HTTP monitor START — waking from sleep into monitoring")
                        _log_entry("system", "Waking into monitoring mode…")
                    else:
                        _persist_monitoring[0] = False
                        log.info("HTTP monitor STOP (was sleeping)")
                elif sess:
                    if self.path == "/monitor/start":
                        new_state = True
                    elif self.path == "/monitor/stop":
                        new_state = False
                    else:
                        new_state = not sess._monitoring
                    if new_state and not sess._monitoring:
                        sess._monitoring = True
                        _persist_monitoring[0] = True
                        sess._active = False  # ensure fully silent
                        log.info("HTTP monitor START — capture-only")
                        _log_entry("system", "Monitoring only - capture display, silent")
                        # Wake from sleep if needed — monitoring requires OpenAI connection
                        if _idle_disconnected[0] and _wake_event[0]:
                            import time as _tmon_w; _last_activity[0] = _tmon_w.time()
                            _wake_event[0].set()
                            log.info("HTTP monitor START — waking from sleep")
                    elif not new_state and sess._monitoring:
                        sess._monitoring = False
                        _persist_monitoring[0] = False
                        log.info("HTTP monitor STOP")
                        _log_entry("system", "Monitoring stopped")
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path == "/multilang":
                # Cycle: off → en-zh → whitelist → any → off
                _MULTILANG_CYCLE = ("off", "en-zh", "whitelist", "any")
                _MULTILANG_LABELS = {
                    "off":      "OFF (EN/ZH, auto-sleep on)",
                    "en-zh":    "EN/ZH (auto-sleep off)",
                    "whitelist": f"Whitelist ({', '.join(MULTILANG_WHITELIST_LANGS[:4])}…)",
                    "any":      "Any language",
                }
                if sess:
                    cur = sess._multilang
                    nxt = _MULTILANG_CYCLE[(_MULTILANG_CYCLE.index(cur) + 1) % len(_MULTILANG_CYCLE)]
                    sess._multilang = nxt
                    _persist_multilang[0] = nxt
                    log.info("HTTP multilang: %s → %s", cur, nxt)
                    _log_entry("system", f"Multi-language: {_MULTILANG_LABELS[nxt]}")
                    if nxt == "off":
                        # Reset idle clock so auto-sleep starts fresh, not immediately.
                        import time as _tms; _last_activity[0] = _tms.time()
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path == "/continue":
                saved = _paused_speech[0]
                if saved and sess and sess.loop:
                    _paused_speech[0] = None  # clear immediately so double-clicks don't re-enter
                    saved_text, saved_dev = saved
                    def _resume():
                        asyncio.run_coroutine_threadsafe(
                            sess._resume_from_http(saved_text, saved_dev), sess.loop
                        )
                    _threading.Thread(target=_resume, daemon=True).start()
                    log.info("HTTP continue — resuming %d chars", len(saved_text))
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()

            elif self.path == "/interrupt":
                # Cancel the current think task if pending
                task = _current_think_task[0]
                if task is not None and sess and sess.loop:
                    sess.loop.call_soon_threadsafe(task.cancel)
                # Stop TTS if currently speaking
                _http_interrupt[0] = True
                if sess:
                    sess._busy.clear()
                    sess._active = True   # ensure back in listening mode
                _log_entry("five", "")           # clears thinking counter
                _log_entry("system", "Interrupted — listening.")
                log.info("HTTP interrupt — cancelled thinking + TTS")
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path == "/reset":
                # Clear the on-screen conversation/capture log
                CONVERSATION_LOG.clear()
                log.info("HTTP reset — conversation log cleared")
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path == "/gateway-reset":
                # Disconnect and reconnect to the OpenClaw gateway
                if sess and sess.gw and sess.loop:
                    async def _gw_reset():
                        try:
                            await sess.gw.close()
                        except Exception:
                            pass
                        await asyncio.sleep(1)
                        await sess.gw.connect()
                        log.info("Gateway reconnected after manual reset")
                        _log_entry("system", "Gateway reconnected.")
                    asyncio.run_coroutine_threadsafe(_gw_reset(), sess.loop)
                log.info("HTTP gateway-reset requested")
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path in ("/dtmf-monitor", "/dtmf-train", "/dtmf-retrain"):
                profiles = _load_dtmf_profiles()
                _script  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dtmf_monitor.py")
                _python  = os.path.join(os.path.dirname(sys.executable), "python3")
                _mode    = {"dtmf-monitor": "monitor", "dtmf-train": "train",
                            "dtmf-retrain": "retrain"}[self.path.lstrip("/")]
                _args    = {"monitor": "", "train": "--train", "retrain": "--retrain"}[_mode]
                _titles  = {"monitor": "DTMF Monitor", "train": "DTMF Train", "retrain": "DTMF Retrain"}
                _colors  = {"monitor": "#60a5fa", "train": "#f59e0b", "retrain": "#a78bfa"}
                _launched = False
                _disp = os.environ.get("DISPLAY", ":0")
                if os.environ.get("DISPLAY") or os.path.exists("/tmp/.X11-unix/X0"):
                    try:
                        subprocess.Popen(
                            ["xterm", "-title", _titles[_mode], "-fg", "white", "-bg", "#07090f",
                             "-e", f"{_python} {_script} {_args}; echo; read -p 'Press Enter to close'"],
                            env={**os.environ, "DISPLAY": _disp})
                        _launched = True
                    except Exception: pass
                _n = len(profiles)
                _prof_rows = "".join(
                    f"<tr><td style='padding:3px 10px;font-weight:bold'>{_d}</td>"
                    f"<td style='padding:3px 10px;'>{_p['row_hz']:.1f}</td>"
                    f"<td style='padding:3px 10px;'>{_p['col_hz']:.1f}</td>"
                    f"<td style='padding:3px 10px;'>{_p['samples']}</td></tr>"
                    for _d, _p in sorted(profiles.items())) if profiles else ""
                _prof_html = (f"<table style='border-collapse:collapse;font-size:13px;margin:10px 0;'>"
                              f"<tr><th>Digit</th><th>Row Hz</th><th>Col Hz</th><th>Samples</th></tr>"
                              f"{_prof_rows}</table>") if _prof_rows else "<p style='color:#475569'>No profiles trained yet.</p>"
                _cmd_map = {"monitor": f"python3 {_script}",
                            "train":   f"python3 {_script} --train",
                            "retrain": f"python3 {_script} --retrain"}
                _body = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>{_titles[_mode]} — RealTimeTalk</title>
<style>body{{font-family:monospace;background:#07090f;color:#dde4ef;padding:20px;max-width:600px;}}
h2{{color:{_colors[_mode]};}} a{{color:#38bdf8;text-decoration:none;}}
table{{border:1px solid #1a2535;}} th{{background:#0d1119;padding:6px 12px;color:#64748b;}}
.nav{{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;}}
.nav a{{padding:5px 12px;border:1px solid #1a2535;border-radius:6px;font-size:13px;color:#64748b;}}
.nav a.active{{color:{_colors[_mode]};border-color:{_colors[_mode]};}}
.cmd{{background:#0d1119;border:1px solid #1a2535;padding:10px 14px;border-radius:6px;
      font-size:14px;color:#34d399;margin:10px 0;display:flex;align-items:center;gap:10px;}}
.copybtn{{padding:3px 10px;font-size:12px;border:1px solid #1a2535;border-radius:5px;
          background:#0d1119;color:#64748b;cursor:pointer;font-family:monospace;white-space:nowrap;}}
.copybtn:hover{{border-color:#34d399;color:#34d399;}}
.copybtn.copied{{color:#34d399;border-color:#34d399;}}</style></head><body>
<div class='nav'>
  <a href='/calibration'>← Calibration</a>
  <a href='/dtmf-monitor' {'class="active"' if _mode=='monitor' else ''}>&#128225; Monitor</a>
  <a href='/dtmf-train'   {'class="active"' if _mode=='train'   else ''}>&#9881; Train</a>
  <a href='/dtmf-retrain' {'class="active"' if _mode=='retrain' else ''}>&#8635; Retrain</a>
</div>
<h2>{_titles[_mode]}</h2>
<p>Profiles: <b>{_n} digit(s) trained</b>  |  File: <code style='font-size:11px'>{DTMF_PROFILE_FILE}</code></p>
{_prof_html}
<hr style='border-color:#1a2535;margin:14px 0;'>
{'<p style="color:#34d399;">&#10003; Terminal launched (xterm)</p>' if _launched else '<p style="color:#64748b;font-size:12px;">xterm not available — run from terminal:</p>'}
<div class='cmd'><span id='cmd'>{_cmd_map[_mode]}</span><button class='copybtn' onclick='copyCmd()'>Copy</button></div>
<script>
function copyCmd(){{
  var t=document.getElementById('cmd').textContent;
  navigator.clipboard.writeText(t).then(function(){{
    var b=document.querySelector('.copybtn');
    b.textContent='Copied!';b.classList.add('copied');
    setTimeout(function(){{b.textContent='Copy';b.classList.remove('copied');}},1500);
  }});
}}
</script>
<p style='color:#475569;font-size:12px;margin-top:12px;'>
Wake={DTMF_WAKE_SEQ} &nbsp;|&nbsp; Sleep={DTMF_SLEEP_SEQ} &nbsp;|&nbsp;
COS≥{DTMF_COS_THRESHOLD} &nbsp;|&nbsp; Tail={DTMF_COS_TAIL_S}s &nbsp;|&nbsp;
Restart daemon after training to reload profiles.</p>
</body></html>"""
                _enc = _body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(_enc)))
                self.end_headers()
                self.wfile.write(_enc)

            elif self.path in ("/calibrate", "/speaker-cal") and "/" not in self.path[1:]:
                # Legacy top-level routes redirect to combined page (sub-routes like /speaker-cal/run pass through)
                # Note: /calibrate and /speaker-cal exactly (no sub-path)
                self.send_response(302)
                self.send_header("Location", "/calibration")
                self.end_headers()
            elif self.path == "/calibration":
                # Determine headset mode: manual override > auto-detection
                _override = _cal_mode_override[0]
                if _override == "headset":
                    is_headset = True
                elif _override == "speaker":
                    is_headset = False
                else:
                    is_headset = _detect_headset()
                _mode_label = ("Headset" if is_headset else "Speaker") + \
                              (" (auto)" if _override is None else " (manual)")
                _dtmf_btns = (
                    '<a href="/dtmf-monitor" style="padding:4px 11px;font-size:13px;'
                    'text-decoration:none;border:1px solid #334155;border-radius:8px;'
                    'color:#60a5fa;background:#071a2e;" title="DTMF signal monitor">'
                    '&#128225; DTMF Mon</a>'
                    '<a href="/dtmf-train" style="padding:4px 11px;font-size:13px;'
                    'text-decoration:none;border:1px solid #334155;border-radius:8px;'
                    'color:#f59e0b;background:#130e02;" title="Train DTMF profiles">'
                    '&#9881; DTMF Train</a>'
                    '<a href="/dtmf-retrain" style="padding:4px 11px;font-size:13px;'
                    'text-decoration:none;border:1px solid #334155;border-radius:8px;'
                    'color:#a78bfa;background:#0e0820;" title="Retrain specific digits">'
                    '&#8635; DTMF Retrain</a>'
                ) if _radio_profile_active[0] else ""
                ds = _get_device_status()
                gate = _mic_gate_ref[0]
                prev = _speaker_cal_result
                prev_html = ""
                if prev:
                    snr_target = prev.get("snr_target", 5.0)
                    def _row(m):
                        snr = m.get("snr", 0)
                        col = "#5f5" if snr >= snr_target else "#aaa"
                        return (f'<tr><td>PW {m.get("pw","-")}% SW {int(m.get("sw",1)*100)}%</td>'
                                f'<td style="color:{col}">SNR {snr:.1f}x</td></tr>')
                    spk_rows = "".join(_row(m) for m in prev.get("measurements", []))
                    sw_pct = int(prev.get("safe_sw_vol", 1.0) * 100)
                    warn = ('<div class="warn">Mic cannot hear speaker — use Manual adjustment below.</div>'
                            ) if prev.get("status") == "no_mic" else ""
                    prev_html = (warn +
                        f'<p>Last result: PW <b>{prev.get("safe_vol")}%</b> + software <b>{sw_pct}%</b></p>'
                        f'<table class="snrtbl"><tr><th>Level</th><th>Mic SNR</th></tr>{spk_rows}</table>')
                headset_notice = ('<p class="info" style="margin:4px 0;color:#fa0;">'
                    'Headset mode — use Manual adjustment to set volume.</p>'
                    ) if is_headset else ""
                spk_adj_section = f"""
<div class="sect"><h4>Manual adjustment</h4>
{headset_notice}
<table style="border-collapse:collapse;margin:4px 0;width:100%;">
  <tr>
    <td style="color:#aaa;font-size:13px;width:32px;">Vol</td>
    <td style="font-weight:bold;font-size:1.1em;width:52px;" id="volval">{ds["spk_vol"]}</td>
    <td><div class="row" style="margin:0;gap:5px;">
      <button class="bQ" onclick="adjVol(-10)">− Quieter</button>
      <button class="bL" onclick="adjVol(+10)">+ Louder</button>
    </div></td>
  </tr>
  <tr>
    <td style="color:#aaa;font-size:13px;">SW</td>
    <td style="font-weight:bold;font-size:1.1em;" id="swval">{ds["sw_pct"]}%</td>
    <td><div class="row" style="margin:0;gap:5px;">
      <button class="bQ" onclick="adjSW(-10)">− Softer</button>
      <button class="bL" onclick="adjSW(+10)">+ Louder</button>
    </div></td>
  </tr>
</table>
<div class="row" style="margin:4px 0;">
  <button class="bP" onclick="startLoop()">Play test</button>
  <button class="bS" onclick="stopLoop()">Stop</button>
  <button class="bSet" onclick="setLevel()">Set this level</button>
</div>
<div id="mstatus" class="info"></div></div>"""
                auto_cal_section = ("" if is_headset else f"""
<div class="sect"><h4>Auto calibration (mic leakage)</h4>
<p class="info">Plays 440 Hz tone at increasing volumes and measures mic response.</p>
<div id="calstatus">Ready.</div>
{prev_html}
<div class="row"><button id="acbtn" onclick="runCal()">Run auto calibration</button></div>
</div>""")
                body = f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Calibration — RealTimeTalk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#07090f;--sf:#0d1119;--sf2:#121925;--bd:#1a2535;--tx:#dde4ef;--mu:#5a7088;--di:#253344;--you:#38bdf8;--bot:#f59e0b;--bb:#130e02;--rd:#ef4444;--rdb:#150303;--gn:#34d399;--gnb:#021a0e;--r:8px;}}
body{{font-family:'Outfit',system-ui,sans-serif;font-size:15px;background:var(--bg);color:var(--tx);padding:12px 16px;max-width:680px;-webkit-text-size-adjust:100%;}}
.ph{{display:flex;align-items:center;gap:10px;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--bd);}}
.pt{{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:600;color:var(--tx);letter-spacing:.08em;text-transform:uppercase;}}
a.back{{margin-left:auto;display:inline-flex;align-items:center;gap:4px;padding:5px 12px;border-radius:8px;font-size:13px;font-weight:500;color:var(--mu);background:var(--sf2);border:1px solid var(--bd);text-decoration:none;transition:border-color .12s,color .12s;}}
a.back:hover{{border-color:var(--you);color:var(--you);box-shadow:0 0 0 2px rgba(56,189,248,.25);}}
.devpanel{{font-family:'JetBrains Mono',monospace;font-size:12px;color:#8aa0b8;line-height:1.7;padding:7px 10px;background:var(--bg);border-radius:5px;border:1px solid var(--di);margin-bottom:10px;}}
.devpanel b{{color:var(--tx);}}
.sect{{border-top:1px solid var(--bd);margin-top:14px;padding-top:10px;}}
h4{{font-family:'Outfit',sans-serif;font-size:14px;font-weight:600;color:var(--you);margin:0 0 6px;}}
.info{{color:var(--mu);font-size:13px;margin:3px 0;}}
.warn{{background:#3a1500;border:1px solid #7a3000;border-radius:6px;padding:6px 10px;margin-bottom:6px;font-size:13px;color:var(--bot);}}
canvas{{width:100%;height:38px;border-radius:5px;display:block;margin:6px 0;}}
#micinfo{{font-size:12px;color:var(--mu);margin:2px 0;min-height:16px;font-family:'JetBrains Mono',monospace;}}
#micresult{{margin-top:6px;padding:7px 10px;background:var(--gnb);border:1px solid var(--gn);border-radius:6px;font-size:13px;color:var(--gn);display:none;}}
#calstatus{{margin:4px 0;font-size:13px;min-height:16px;color:var(--mu);font-family:'JetBrains Mono',monospace;}}
#mstatus{{margin-top:4px;font-size:13px;color:var(--mu);}}
.row{{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0;}}
button{{padding:7px 14px;border:1px solid var(--bd);color:var(--mu);background:var(--sf2);border-radius:8px;font-family:'Outfit',sans-serif;font-size:14px;font-weight:500;cursor:pointer;transition:border-color .12s,color .12s,background .12s;}}
button:hover{{border-color:var(--you);color:var(--you);background:#1e2d3d;box-shadow:0 0 0 2px rgba(56,189,248,.25);}}
button:disabled{{opacity:.4;cursor:default;border-color:var(--bd);color:var(--mu);background:var(--sf2);box-shadow:none;}}
#micbtn,#acbtn{{color:var(--gn);border-color:var(--gn);background:var(--gnb);}}
#micbtn:hover,#acbtn:hover{{background:#042e18;box-shadow:0 0 0 2px rgba(52,211,153,.25);}}
.bL{{color:var(--gn);border-color:var(--gn);background:var(--gnb);}}
.bL:hover{{background:#042e18;box-shadow:0 0 0 2px rgba(52,211,153,.25);}}
.bP{{color:var(--you);border-color:var(--you);background:#051928;}}
.bP:hover{{background:#0a2840;box-shadow:0 0 0 2px rgba(56,189,248,.25);}}
.bQ{{color:var(--mu);border-color:var(--bd);background:var(--sf2);}}
.bS{{color:var(--rd);border-color:var(--rd);background:var(--rdb);}}
.bS:hover{{background:#2a0808;box-shadow:0 0 0 2px rgba(239,68,68,.25);}}
.bSet{{color:var(--bot);border-color:var(--bot);background:var(--bb);}}
.bSet:hover{{background:#261b03;box-shadow:0 0 0 2px rgba(245,158,11,.25);}}
.snrtbl{{border-collapse:collapse;font-size:12px;margin:6px 0;width:100%;font-family:'JetBrains Mono',monospace;}}
.snrtbl th{{background:var(--sf2);color:var(--mu);font-weight:600;border:1px solid var(--bd);padding:4px 8px;text-align:left;}}
.snrtbl td{{border:1px solid var(--bd);padding:4px 8px;color:var(--tx);}}
.snrtbl tr.active-row{{background:var(--gnb);}}
.use-btn{{padding:4px 10px;font-size:12px;background:var(--sf2);border:1px solid var(--bd);color:var(--mu);border-radius:5px;cursor:pointer;white-space:nowrap;font-family:'Outfit',sans-serif;transition:border-color .12s,color .12s;}}
.use-btn:hover{{border-color:var(--you);color:var(--you);box-shadow:0 0 0 2px rgba(56,189,248,.2);}}
.use-btn.active{{background:var(--gnb);border-color:var(--gn);color:var(--gn);cursor:default;}}
#devbtn{{color:var(--you);border-color:var(--you);background:#051928;}}
#devbtn:hover{{background:#0a2840;}}
#devtoggle{{color:var(--you);cursor:pointer;font-size:13px;background:none;border:none;padding:0;margin-left:6px;font-family:'Outfit',sans-serif;}}
#devlist{{margin-top:8px;}}
#devmsg{{font-size:13px;color:var(--bot);font-family:'JetBrains Mono',monospace;}}
a{{color:var(--you);text-decoration:none;}}
a:hover{{text-decoration:underline;}}
</style></head><body>
<div class="ph">
  <span class="pt">&#9679;&nbsp;Calibration</span>
  <a href="/dashboard" class="back">&#8592; Dashboard</a>
</div>
<div class="devpanel" id="curdev">
  <b>Mic:</b> <span id="panelmic">{ds["mic"]}</span> &nbsp;&middot;&nbsp; Gate: <span id="panelgate">{ds["gate"]}</span> &nbsp;&middot;&nbsp; Gain: <span id="panelgain">{ds["gain"]}</span>x<br>
  <b>Speaker:</b> <span id="panelspk">{ds["speaker_name"]}</span> &nbsp;&middot;&nbsp; Vol: <span id="panelvol">{ds["spk_vol"]}</span> &nbsp;&middot;&nbsp; SW: <span id="panelsw">{ds["sw_pct"]}%</span> &nbsp;&middot;&nbsp; <b>Eff: <span id="paneleff" style="color:var(--gn)">{ds["effective_pct"]}%</span></b>
</div>
<div style="display:flex;align-items:center;gap:8px;margin:4px 0 10px;flex-wrap:wrap;">
  <span style="font-size:12px;color:var(--mu);font-family:'JetBrains Mono',monospace;">Cal mode:</span>
  <b style="font-size:13px;color:{'#f59e0b' if is_headset else '#34d399'};">{_mode_label}</b>
  <button onclick="setCalMode('headset')" style="padding:4px 11px;font-size:13px;{'color:#f59e0b;border-color:#f59e0b;background:#130e02;' if is_headset and _override else ''}">Headset</button>
  <button onclick="setCalMode('speaker')" style="padding:4px 11px;font-size:13px;{'color:#34d399;border-color:#34d399;background:#021a0e;' if not is_headset and _override else ''}">Speaker</button>
  <button onclick="setCalMode('auto')" style="padding:4px 11px;font-size:13px;{'color:#38bdf8;border-color:#38bdf8;background:#051928;' if _override is None else ''}">Auto</button>
  <button id="radiobtn" onclick="toggleRadio()" style="padding:4px 11px;font-size:13px;{'color:#dc2626;border-color:#dc2626;background:#3b0000;' if _radio_profile_active[0] else 'color:#475569;border-color:#334155;'}">&#128225; Radio{'&nbsp;&#10003;' if _radio_profile_active[0] else ''}</button>
  <button id="monitorbtn" onclick="toggleAiocMonitor()" style="padding:4px 11px;font-size:13px;{'color:#34d399;border-color:#34d399;background:#021a0e;' if _aioc_monitor_module[0] is not None else 'color:#475569;border-color:#334155;'}">&#128266; Monitor{'&nbsp;&#10003;' if _aioc_monitor_module[0] is not None else ''}</button>
  {_dtmf_btns}
</div>
{spk_adj_section}
<div style="margin:10px 0 4px;display:flex;align-items:center;gap:10px;">
  <button id="devbtn" onclick="toggleDevices()">Audio Devices</button>
  <span id="devtoggle" onclick="toggleDevices()">▼ expand</span>
</div>
<div id="devlist" style="display:none;">
  <div id="devout" style="font-size:14px;">Loading…</div>
</div>


<div class="sect"><h4>Mic calibration</h4>
<p class="info">Yellow line = gate threshold. Speech above the line passes; noise below is silenced.</p>
<canvas id="meter" height="36"></canvas>
<div id="micinfo" style="font-size:12px;color:#aaa;margin:2px 0;min-height:14px;"></div>
<div style="display:flex;align-items:center;gap:8px;margin:6px 0;">
  <span style="font-size:12px;color:var(--mu);white-space:nowrap;font-family:'JetBrains Mono',monospace;">Gate:</span>
  <input type="range" id="gateslider" min="{MIC_GATE_MIN}" max="{MIC_GATE_MAX}" step="25"
         value="{gate}" style="flex:1;accent-color:#f59e0b;" oninput="onGateSlide(this.value)"
         onchange="saveGate(this.value)">
  <span id="gateval" style="font-size:13px;color:#f59e0b;font-weight:bold;width:40px;text-align:right;font-family:'JetBrains Mono',monospace;">{gate}</span>
</div>
<div id="micresult"></div>
<div class="row">
  <button id="micbtn" onclick="startMicCal()">Mic Auto-calibrate (3 sec quiet)</button>
</div>
</div>
{auto_cal_section}
<p style="margin-top:14px;"><a href="/dashboard" style="color:var(--you);">&#8592; Dashboard</a></p>
<script>
/* --- Mic level meter --- */
const MAX=32768, gate0={gate};
let calRunning=false;
const canvas=document.getElementById('meter');
const ctx=canvas.getContext('2d');
const micinfo=document.getElementById('micinfo');
const micresult=document.getElementById('micresult');
const micbtn=document.getElementById('micbtn');
const grad=(w)=>{{const g=ctx.createLinearGradient(0,0,w,0);
  g.addColorStop(0,'#1155cc');g.addColorStop(0.35,'#22bb55');g.addColorStop(0.75,'#cc4411');return g;}};
function draw(peak,gateVal){{
  const W=canvas.width,H=canvas.height;
  ctx.clearRect(0,0,W,H);ctx.fillStyle='#222';ctx.fillRect(0,0,W,H);
  const ratio=Math.min(peak/MAX,1);
  ctx.fillStyle=grad(W);ctx.fillRect(0,0,W*ratio,H);
  const gx=Math.min((gateVal/MAX)*W,W-2);
  ctx.strokeStyle='#ffee00';ctx.lineWidth=2;
  ctx.beginPath();ctx.moveTo(gx,0);ctx.lineTo(gx,H);ctx.stroke();
  ctx.fillStyle='#eee';ctx.font='11px monospace';
  ctx.fillText('peak:'+peak+'  gate:'+gateVal,6,H-6);
}}
const es=new EventSource('/levels');
es.onmessage=e=>{{
  const [peak,gate]=e.data.split(',').map(Number);
  draw(peak,gate);
  // Keep slider in sync with live gate (e.g. after auto-calibrate)
  const sl=document.getElementById('gateslider');
  const gv=document.getElementById('gateval');
  if(sl && !sl.matches(':active')){{ sl.value=gate; if(gv) gv.textContent=gate; }}
  if(!calRunning) micinfo.textContent=
    peak<gate?'Below gate — noise silenced':
    peak<MAX*0.5?'Speech range':'Very loud';
}};
let _gateTimer=null;
function onGateSlide(val){{
  document.getElementById('gateval').textContent=val;
  // Clear stale auto-calibrate result when user manually adjusts
  const r=document.getElementById('micresult');
  if(r) r.style.display='none';
  clearTimeout(_gateTimer);
  _gateTimer=setTimeout(()=>fetch('/mic-gate/set?value='+val),150);
}}
function saveGate(val){{
  // Persist to service file on mouseup
  clearTimeout(_gateTimer);
  fetch('/mic-gate/set?value='+val).then(r=>r.json()).then(d=>{{
    document.getElementById('gateval').textContent=d.gate;
  }});
}}
function startMicCal(){{
  calRunning=true; micbtn.disabled=true;
  let secs=3; micinfo.textContent='Stay quiet… '+secs+'s';
  const t=setInterval(()=>{{secs--;micinfo.textContent=secs>0?'Stay quiet… '+secs+'s':'Measuring…';}},1000);
  fetch('/calibrate/run').then(r=>r.json()).then(d=>{{
    clearInterval(t); calRunning=false;
    micresult.style.display='block';
    micresult.innerHTML='Done! New gate: <b>'+d.gate+'</b> (noise peak: '+d.noise_peak+')';
    micinfo.textContent='Yellow line updated.'; micbtn.disabled=false;
    // Auto-hide after announcement has played (~4s)
    setTimeout(()=>{{micresult.style.display='none';}},4000);
    // Sync slider to the new gate from auto-calibrate
    const sl=document.getElementById('gateslider');
    const gv=document.getElementById('gateval');
    if(sl){{ sl.value=d.gate; }} if(gv){{ gv.textContent=d.gate; }}
  }}).catch(()=>{{clearInterval(t);calRunning=false;micbtn.disabled=false;
    micinfo.textContent='Calibration failed — try again.';}});
}}
/* --- Speaker controls --- */
function upd(){{fetch('/speaker-cal/vol').then(r=>r.json()).then(d=>{{
  const vv=document.getElementById('volval');
  const sv=document.getElementById('swval');
  const txMode=d.tx_mode||false;
  const txSec=d.tx_remaining||0;
  const txColor='#dc2626';
  const txLabel=txMode?' <span style="font-size:.75em;font-weight:normal;color:'+txColor+'">TX'+(txSec>0?' '+txSec+'s':'')+' ↑</span>':'';
  if(vv){{
    vv.innerHTML=d.spk_vol+txLabel;
    vv.style.color=txMode?txColor:'';
  }}
  if(sv){{
    sv.textContent=d.sw_pct+'%';
    sv.style.color=txMode?txColor:'';
  }}
  // Update row label to show what device is being adjusted
  const spkLabel=document.querySelector('td[style*="color:#aaa"][style*="width:32px"]');
  // Keep top panel in sync
  const pv=document.getElementById('panelvol');
  const ps=document.getElementById('panelsw');
  const pg=document.getElementById('panelgate');
  if(pv) pv.textContent=d.spk_vol;
  if(ps) ps.textContent=d.sw_pct+'%';
  if(pg) pg.textContent=d.gate;
}});}}
function adjVol(d){{fetch('/speaker-cal/adjust?type=vol&delta='+d).then(()=>upd());}}
function adjSW(d){{fetch('/speaker-cal/adjust?type=sw&delta='+d).then(()=>upd());}}
function adj(d){{adjVol(d);}}
function setAiocMonitor(sink){{
  fetch('/aioc-monitor?sink='+encodeURIComponent(sink)).then(r=>r.json()).then(d=>{{
    const mb=document.getElementById('monitorbtn');
    if(mb){{
      if(d.active){{
        mb.innerHTML='&#128266; Monitor&nbsp;&#10003;';
        mb.style.color='#34d399';mb.style.borderColor='#34d399';mb.style.background='#021a0e';
      }} else {{
        mb.innerHTML='&#128266; Monitor';
        mb.style.color='#475569';mb.style.borderColor='#334155';mb.style.background='';
      }}
    }}
    loadDevices();  // refresh table to show updated checkmark
  }});
}}
function toggleAiocMonitor(){{
  fetch('/aioc-monitor').then(r=>r.json()).then(d=>{{
    const mb=document.getElementById('monitorbtn');
    if(!mb) return;
    if(d.active){{
      mb.innerHTML='&#128266; Monitor&nbsp;&#10003;';
      mb.style.color='#34d399';mb.style.borderColor='#34d399';mb.style.background='#021a0e';
    }} else {{
      mb.innerHTML='&#128266; Monitor';
      mb.style.color='#475569';mb.style.borderColor='#334155';mb.style.background='';
    }}
  }});
}}
function toggleRadio(){{
  fetch('/radio/profile').then(r=>r.json()).then(d=>{{
    // Reload page so server re-renders DTMF buttons based on new profile state
    location.reload();
  }});
}}
function adj(d){{fetch('/speaker-cal/adjust?delta='+d).then(()=>upd());}}
function startLoop(){{fetch('/speaker-cal/loop-start').then(()=>{{
  const m=document.getElementById('mstatus');if(m)m.textContent='Playing test loop…';}});}}
function stopLoop(){{fetch('/speaker-cal/loop-stop').then(()=>{{
  const m=document.getElementById('mstatus');if(m)m.textContent='Stopped.';}});}}
function setLevel(){{fetch('/speaker-cal/set').then(r=>r.json()).then(d=>{{
  const m=document.getElementById('mstatus');
  if(m)m.textContent='Level saved: '+d.spk_vol+' PW, '+d.sw_pct+'% SW';
  stopLoop(); setTimeout(()=>location.href='/dashboard',3000);}});}}
function runCal(){{
  stopLoop();
  const btn=document.getElementById('acbtn');
  const st=document.getElementById('calstatus');
  if(btn)btn.disabled=true; if(st)st.textContent='Calibrating…';
  fetch('/speaker-cal/run').then(r=>r.json()).then(d=>{{
    if(btn)btn.disabled=false;
    if(st)st.innerHTML=d.status=='no_mic'?'Mic cannot hear speaker — adjust manually.':
      'Set to PW <b>'+d.safe_vol+'%</b> SW <b>'+Math.round(d.safe_sw_vol*100)+'%</b>';
    const vv=document.getElementById('volval');
    const sv=document.getElementById('swval');
    if(vv) vv.textContent=d.safe_vol+'%';
    if(sv) sv.textContent=Math.round(d.safe_sw_vol*100)+'%';
    setTimeout(()=>location.reload(),4000);
  }}).catch(e=>{{if(btn)btn.disabled=false;if(st)st.textContent='Error: '+e;}});
}}
setInterval(upd,2000);
/* --- Device selection --- */
let _devExpanded=false, _devTimer=null;
function toggleDevices(){{
  _devExpanded=!_devExpanded;
  const list=document.getElementById('devlist');
  const tog=document.getElementById('devtoggle');
  list.style.display=_devExpanded?'block':'none';
  tog.textContent=_devExpanded?'▲ collapse':'▼ expand';
  if(_devExpanded){{
    loadDevices();
    _devTimer=setInterval(loadDevices, 2000);
  }} else {{
    if(_devTimer){{ clearInterval(_devTimer); _devTimer=null; }}
  }}
}}
function loadDevices(){{
  const out=document.getElementById('devout');
  if(!out) return;
  // Don't show "Loading…" on refresh — only on first open (when empty)
  if(!out.dataset.loaded) out.textContent='Loading…';
  fetch('/device-status').then(r=>r.json()).then(d=>{{
    if(d.error){{out.innerHTML='<span style="color:#f55">Error: '+d.error+'</span>';return;}}
    let h='';
    const monSink=d.monitor_sink||null;
    const aiocAvail=!!(d.sinks||[]).find(s=>s.name.includes('AIOC')||s.name.includes('All-In-One'));
    const showMonCol=aiocAvail||!!monSink; // show Monitor column if AIOC present OR loopback active
    h+='<p style="margin:4px 0 8px;color:#9cf;font-weight:bold">Speakers</p>';
    h+='<table class="snrtbl"><tr><th>Name</th><th>Card</th><th>State</th><th></th>'
      +(showMonCol?'<th style="color:#34d399;white-space:nowrap">&#128266; Monitor</th>':'')
      +'</tr>';
    (d.sinks||[]).forEach(s=>{{
      if(s.name.startsWith('rtt_agc')||s.name.includes('monitor')) return;
      const active=(s.name===d.default_sink);
      const monitoring=(s.name===monSink);
      const isAioc=(s.name.includes('AIOC')||s.name.includes('All-In-One'));
      const stateLabel=monitoring
        ?'<span style="color:#34d399;font-weight:bold">Monitoring</span>'
        :s.state==='SUSPENDED'?'Idle'
        :s.state==='RUNNING'?'<span style="color:#5f5">Running</span>'
        :s.state;
      h+='<tr'+(active?' class="active-row"':'')+'>'
        +'<td>'+(s.desc||s.name)+(active?' <span style="color:#5f5">✓</span>':'')+'</td>'
        +'<td style="white-space:nowrap">'+(s.card?'card '+s.card:'BT')+'</td>'
        +'<td>'+stateLabel+'</td>'
        +'<td>'+(isAioc?'':('<button class="use-btn'+(active?' active':'')+'"'
          +' data-dtype="sink" data-dname="'+s.name+'"'
          +' onclick="setDevice(this.dataset.dtype,this.dataset.dname)"'
          +(active?' disabled':'')
          +'>'+(active?'Active':'Use')+'</button>'))+'</td>'
        +(showMonCol?'<td style="text-align:center">'
          +(isAioc?'—':('<button class="use-btn'+(monitoring?' active':'')+'"'
            +' data-sink="'+s.name+'"'
            +' onclick="setAiocMonitor(this.dataset.sink)" style="padding:3px 10px;'
            +(monitoring?'color:#34d399;border-color:#34d399;background:#021a0e;':'')
            +'">'+(monitoring?'&#10003; On':'Off')+'</button>'))
          +'</td>':'')
        +'</tr>';
    }});
    h+='</table>';
    h+='<p style="margin:12px 0 8px;color:#9cf;font-weight:bold">Microphones</p>';
    h+='<table class="snrtbl"><tr><th>Name</th><th>Card</th><th>State</th><th></th></tr>';
    (d.sources||[]).forEach(s=>{{
      if(s.name.includes('monitor')||s.name==='rtt_agc_sink'||s.name==='rtt_agc_source') return;
      // AGC is always on — active mic is whichever physical device AGC captures from,
      // not raw PipeWire RUNNING state (other devices can appear RUNNING as side-effect).
      const active=(s.name===d.raw_mic_source);
      const stateLabel=active?'<span style="color:#5f5">Running</span>':'Idle';
      h+='<tr'+(active?' class="active-row"':'')+'>'
        +'<td>'+(s.desc||s.name)+(active?' <span style="color:#5f5">✓</span>':'')+'</td>'
        +'<td style="white-space:nowrap">'+(s.card?'card '+s.card:'-')+'</td>'
        +'<td>'+stateLabel+'</td>'
        +'<td><button class="use-btn'+(active?' active':'')+'"'
        +' data-dtype="source" data-dname="'+s.name+'"'
        +' onclick="setDevice(this.dataset.dtype,this.dataset.dname)"'
        +(active?' disabled':'')
        +'>'+(active?'Active':'Use')+'</button></td></tr>';
    }});
    h+='</table>';
    // Reserved status area — fixed min-height so no layout shift when message appears/clears
    h+='<div id="devmsg" style="min-height:52px;padding:6px 0;font-size:14px;color:#fa0;"></div>';
    if((d.alsa_cards||[]).length){{
      h+='<p style="margin:6px 0 2px;font-size:12px;color:#666;">ALSA: '
        +d.alsa_cards.map(c=>'<span style="color:#888">'+c.num+'</span> '+c.name).join(' &nbsp;|&nbsp; ')+'</p>';
    }}
    out.innerHTML=h;
    out.dataset.loaded='1';
  }}).catch(e=>{{out.innerHTML='<span style="color:#f55">Failed: '+e+'</span>';}});
}}
function setDevice(type,name){{
  const msg=document.getElementById('devmsg');
  msg.textContent=(type==='sink'?'Switching speaker':'Setting mic')+': '+name+'…';
  fetch('/device-set?type='+type+'&name='+encodeURIComponent(name))
    .then(r=>r.json()).then(d=>{{
      msg.textContent=d.msg||'Done.';
      if(d.ok){{
        if(d.restart){{
          // Mic switch requires daemon restart — reload page after restart settles
          sessionStorage.setItem('devExpanded','1');
          if(_devTimer){{ clearInterval(_devTimer); _devTimer=null; }}
          setTimeout(()=>location.reload(),4500);
        }} else {{
          // Speaker switch: no restart needed, test loop keeps playing on new device.
          // Device bar updates via the 2s polling interval automatically.
          setTimeout(()=>{{ msg.textContent=''; }}, 3000);
        }}
      }} else msg.style.color='#f55';
    }}).catch(e=>{{msg.textContent='Error: '+e; msg.style.color='#f55';}});
}}
// Restore expanded state after a device-switch reload
if(sessionStorage.getItem('devExpanded')){{
  sessionStorage.removeItem('devExpanded');
  toggleDevices();
}}
function setCalMode(mode){{
  fetch('/cal-mode?mode='+mode).then(()=>location.reload());
}}
setInterval(function(){{
  fetch('/speaker-cal/vol').then(function(r){{return r.json();}}).then(function(d){{
    var f=function(id,v){{var e=document.getElementById(id);if(e)e.textContent=v;}};
    f('panelspk', d.speaker_name);
    f('panelmic', d.mic);
    f('panelvol', d.spk_vol);
    f('panelsw',  d.sw_pct+'%');
    f('paneleff', d.effective_pct+'%');
    f('panelgate',d.gate);
    f('panelgain',d.gain);
  }}).catch(function(){{}});
}}, 2000);
</script></body></html>"""
                _html(self, 200, body)
            elif self.path.startswith("/cal-mode"):
                import json as _json, urllib.parse as _up
                qs   = _up.parse_qs(_up.urlparse(self.path).query)
                mode = qs.get("mode", ["auto"])[0]   # "auto", "headset", "speaker"
                if mode in ("auto", "headset", "speaker"):
                    _cal_mode_override[0] = None if mode == "auto" else mode
                    log.info("Cal mode override → %s", mode)
                resp = _json.dumps({"mode": mode}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path.startswith("/mic-gate/set"):
                import json as _json, urllib.parse as _up
                qs   = _up.parse_qs(_up.urlparse(self.path).query)
                val  = int(qs.get("value", [_mic_gate_ref[0]])[0])
                val  = max(MIC_GATE_MIN, min(MIC_GATE_MAX, val))
                _mic_gate_ref[0] = val
                globals()['MIC_GATE_PEAK'] = val
                _update_service_gate(val)
                log.info("Mic gate set to %d via slider", val)
                resp = _json.dumps({"gate": val}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/calibrate/run":
                import json as _json, time as _time
                # _mic_level_current is kept alive by OWW listener even without a session
                peaks = []
                for _ in range(30):
                    _time.sleep(0.1)
                    with _mic_level_lock:
                        peaks.append(_mic_level_current[0])
                peaks = peaks[2:]
                noise_peak = max(peaks) if peaks else 0
                new_gate = max(MIC_GATE_MIN, min(MIC_GATE_MAX, int(noise_peak * 1.5)))
                _mic_gate_ref[0] = new_gate
                MIC_GATE_PEAK = new_gate
                log.info("HTTP calibration: noise_peak=%d → gate=%d", noise_peak, new_gate)
                _update_service_gate(new_gate)
                if sess and not _idle_disconnected[0]:
                    import threading as _t
                    _t.Thread(target=speak,
                              args=(f"Noise gate set to {new_gate}.", sess.alsa_output),
                              daemon=True).start()
                resp = _json.dumps({"gate": new_gate, "noise_peak": noise_peak}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/speaker-cal":
                is_headset = _detect_headset()
                ds = _get_device_status()
                if is_headset:
                    # Headset mode: interactive play+adjust (can't use mic leakage measurement)
                    body = f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Speaker Calibration — Headset</title>
<style>body{{font-family:sans-serif;font-size:17px;background:#111;color:#eee;padding:16px;}}
h3{{margin:0 0 8px;}} .info{{color:#aaa;font-size:16px;margin:6px 0;}}
#vol{{font-size:2em;font-weight:bold;margin:16px 0;text-align:center;}}
.row{{display:flex;gap:10px;justify-content:center;margin:8px 0;}}
button{{padding:12px 24px;border:none;color:#fff;border-radius:6px;font-size:16px;cursor:pointer;}}
#btnLouder{{background:#2a5;}} #btnQuieter{{background:#555;}}
#btnPlay{{background:#226;}} #btnStop{{background:#622;}} #btnSet{{background:#a62;}}
a{{color:#7af;}}</style></head><body>
<h3>Speaker Calibration — Headset</h3>
<div class="info">Headset detected: mic + speaker on same device.</div>
<div class="info">Acoustic leakage measurement is not suitable for headphones.<br>
Play the test sentence and adjust until comfortable.</div>
<div id="vol">Vol: {ds["spk_vol"]}  SW: {ds["sw_pct"]}%</div>
<div class="row">
  <button id="btnQuieter" onclick="adj(-10)">− Quieter</button>
  <button id="btnLouder"  onclick="adj(+10)">+ Louder</button>
</div>
<div class="row">
  <button id="btnPlay" onclick="startLoop()">Play test</button>
  <button id="btnStop" onclick="stopLoop()">Stop</button>
</div>
<div class="row">
  <button id="btnSet" onclick="setLevel()">✓ Set this level</button>
</div>
<div id="status" style="margin-top:12px;color:#aaa;font-size:13px;"></div>
<div class="sect">
<h4>Device status</h4>
<div class="row"><button id="devbtn" onclick="checkDevices()">Check Device Status</button></div>
<div id="devout" style="margin-top:10px;display:none;font-size:14px;"></div>
</div>
<p style="margin-top:14px;"><a href="/dashboard" style="color:var(--you);">&#8592; Dashboard</a></p>
<script>
function upd(){{fetch('/speaker-cal/vol').then(r=>r.json()).then(d=>{{
  document.getElementById('vol').textContent='Vol: '+d.spk_vol+'  SW: '+d.sw_pct+'%';
}});}}
function adj(d){{fetch('/speaker-cal/adjust?delta='+d).then(()=>upd());}}
function startLoop(){{fetch('/speaker-cal/loop-start').then(()=>{{
  document.getElementById('status').textContent='Playing test sentence in loop…';
}});}}
function stopLoop(){{fetch('/speaker-cal/loop-stop').then(()=>{{
  document.getElementById('status').textContent='Stopped.';
}});}}
function setLevel(){{fetch('/speaker-cal/set').then(r=>r.json()).then(d=>{{
  document.getElementById('status').textContent='✓ Level saved: '+d.spk_vol+' PW, '+d.sw_pct+'% SW';
  stopLoop();
  setTimeout(()=>location.href='/dashboard',3000);
}});}}
setInterval(upd, 2000);
</script></body></html>"""
                else:
                    # Speaker mode: acoustic calibration via mic leakage
                    prev = _speaker_cal_result
                    prev_html = ""
                    if prev:
                        snr_target = prev.get("snr_target", 5.0)
                        def _row(m):
                            snr = m.get("snr", 0)
                            col = "#5f5" if snr >= snr_target else "#aaa"
                            return (f'<tr><td>PW {m.get("pw","-")}% SW {int(m.get("sw",1)*100)}%</td>'
                                    f'<td style="color:{col}">SNR {snr:.1f}×</td></tr>')
                        rows = "".join(_row(m) for m in prev.get("measurements", []))
                        sw_pct = int(prev.get("safe_sw_vol", 1.0) * 100)
                        warn = ('<div style="background:#5a1a00;border-radius:6px;padding:8px;'
                                'margin-bottom:6px;">Mic cannot hear speaker — use Manual adjustment below.</div>'
                                ) if prev.get("status") == "no_mic" else ""
                        prev_html = (
                            warn +
                            f'<h4>Last result: PW <b>{prev.get("safe_vol")}%</b> + '
                            f'software <b>{sw_pct}%</b></h4>'
                            f'<table border=1 style="border-collapse:collapse;font-size:12px">'
                            f'<tr><th>Level</th><th>Mic SNR</th></tr>{rows}</table>'
                        )
                    body = f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Speaker Calibration</title>
<style>body{{font-family:sans-serif;font-size:17px;background:#111;color:#eee;padding:16px;}}
h3,h4{{margin:0 0 8px;}} .info{{color:#aaa;font-size:13px;margin:4px 0;}}
#status{{margin:10px 0;font-size:14px;min-height:18px;}}
.sect{{border-top:1px solid #333;margin-top:16px;padding-top:12px;}}
#vol{{font-size:1.6em;font-weight:bold;margin:8px 0;}}
.row{{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0;}}
button{{padding:12px 22px;border:none;color:#fff;border-radius:8px;font-size:17px;cursor:pointer;}}
#btn{{background:#2a5;}} #btn:disabled{{background:#555;}}
#devbtn{{background:#446;}} #devbtn:disabled{{background:#555;cursor:default;}}
.bAdj{{background:#335;}} .bPlay{{background:#226;}}
.bStop{{background:#622;}} .bSet{{background:#a62;}}
a{{color:#7af;}}</style></head><body>
<h3>Speaker Calibration</h3>
<div class="info">Speaker: {ds["speaker_name"]}</div>
<h4>Auto calibration (mic leakage)</h4>
<div class="info">Plays 440 Hz tone at increasing volumes, measures mic leakage via FFT.</div>
<div id="status">Ready.</div>
{prev_html}
<div class="row"><button id="btn" onclick="runCal()">Run auto calibration</button></div>
<div class="sect">
<h4>Manual adjustment</h4>
<div class="info">Play test sound and adjust until comfortable.</div>
<div id="vol">Vol: {ds["spk_vol"]}</div>
<div class="row">
  <button class="bAdj" onclick="adj(-10)">− Quieter</button>
  <button class="bAdj" onclick="adj(+10)">+ Louder</button>
  <button class="bPlay" onclick="startLoop()">Play test</button>
  <button class="bStop" onclick="stopLoop()">Stop</button>
  <button class="bSet"  onclick="setLevel()">✓ Set this level</button>
</div>
<div id="mstatus" style="color:#aaa;font-size:13px;margin-top:6px;"></div>
</div>
<p><a href="/dashboard">← Back</a></p>
<script>
function upd(){{fetch('/speaker-cal/vol').then(r=>r.json()).then(d=>{{
  document.getElementById('vol').textContent='Vol: '+d.spk_vol;
}});}}
function adj(d){{fetch('/speaker-cal/adjust?delta='+d).then(()=>upd());}}
function startLoop(){{fetch('/speaker-cal/loop-start').then(()=>{{
  document.getElementById('mstatus').textContent='Playing test loop…';
}});}}
function stopLoop(){{fetch('/speaker-cal/loop-stop').then(()=>{{
  document.getElementById('mstatus').textContent='Stopped.';
}});}}
function setLevel(){{fetch('/speaker-cal/set').then(r=>r.json()).then(d=>{{
  document.getElementById('mstatus').textContent='✓ Level saved: '+d.spk_vol;
  stopLoop();
  setTimeout(()=>location.href='/dashboard',3000);
}});}}
function runCal(){{
  stopLoop();
  document.getElementById('btn').disabled=true;
  document.getElementById('status').textContent='Calibrating…';
  fetch('/speaker-cal/run').then(r=>r.json()).then(d=>{{
    document.getElementById('btn').disabled=false;
    document.getElementById('status').innerHTML=
      (d.status=='no_mic' ? 'Mic cannot hear speaker — adjust manually.' :
      'Set to PW <b>'+d.safe_vol+'%</b> SW <b>'+Math.round(d.safe_sw_vol*100)+'%</b>');
    setTimeout(()=>location.reload(),4000);
  }}).catch(e=>{{
    document.getElementById('btn').disabled=false;
    document.getElementById('status').textContent='Error: '+e;
  }});
}}
setInterval(upd, 2000);
</script></body></html>"""
                _html(self, 200, body)

            elif self.path == "/speaker-cal/run":
                import json as _json
                result = run_speaker_calibration(
                    alsa_output=sess.alsa_output if sess else ALSA_OUTPUT
                    # calibration will auto-find the working output device
                )
                _speaker_cal_result.clear()
                _speaker_cal_result.update(result)
                # Update live session's alsa_output immediately (no restart needed)
                if sess and result.get("status") == "ok":
                    new_alsa = result.get("speaker_alsa", sess.alsa_output)
                    if new_alsa != sess.alsa_output:
                        log.info("Updating live session alsa_output: %s → %s",
                                 sess.alsa_output, new_alsa)
                        sess.alsa_output = new_alsa

                # Announce result ALWAYS at a guaranteed-audible level (the
                # calibrated level may be near-silent), then drop the speaker
                # to the calibrated operating level for normal use.
                # Run even when sleeping — volume restore must happen regardless.
                import threading as _t
                sw  = result.get("safe_sw_vol", _cal_sw_volume)
                pw  = result.get("safe_vol", CAL_FALLBACK_PW)
                snk = _find_usb_speaker_sink()
                _alsa = sess.alsa_output if sess else ALSA_OUTPUT
                _sleeping = _idle_disconnected[0]
                def _cal_announce(sw=sw, pw=pw, snk=snk,
                                  alsa=_alsa,
                                  st=result.get("status", "ok"),
                                  sleeping=_sleeping):
                    if st == "no_mic":
                        msg = ("Auto calibration could not measure the speaker — "
                               "the microphone and speaker are not acoustically coupled. "
                               f"Speaker set to {pw} percent. Use Manual adjustment to fine-tune.")
                    elif st == "ok":
                        msg = (f"Calibration done. Speaker set to {pw} percent.")
                    else:
                        msg = ("Calibration had a problem. Speaker set to a "
                               "safe default. Use Manual adjustment.")
                    speak.__globals__["_skip_auto_reduce"] = True
                    try:
                        # Force an audible level for the announcement itself
                        if snk:
                            subprocess.run(["pactl", "set-sink-volume", snk,
                                            f"{CAL_ANNOUNCE_PW}%"],
                                           capture_output=True)
                        if not sleeping:
                            speak(msg, alsa, volume=CAL_ANNOUNCE_SW)
                        # Settle to the calibrated operating level
                        if snk:
                            subprocess.run(["pactl", "set-sink-volume", snk,
                                            f"{pw}%"], capture_output=True)
                    finally:
                        speak.__globals__["_skip_auto_reduce"] = False
                _t.Thread(target=_cal_announce, daemon=True).start()
                resp = _json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/speaker-cal/loop-start":
                # Headset mode: start looping test speech.
                # When Radio profile is active, keys PTT before each playback iteration.
                _headset_cal_loop[0] = True
                _use_radio  = _radio_profile_active[0]
                _aioc_sink_name  = _find_aioc_sink() if _use_radio else None
                _mon_sink_name   = _aioc_monitor_sink[0]   # monitor device if active
                alsa = sess.alsa_output if sess else ALSA_OUTPUT
                def _loop(alsa=alsa, radio=_use_radio,
                          aioc_sink=_aioc_sink_name, mon_sink=_mon_sink_name):
                    import tempfile as _tf, os as _os, time as _tl
                    _pre = _tf.mktemp(suffix=".wav")
                    try:
                        import subprocess as _sp
                        _sp.run(
                            [PIPER_CMD, "--model", PIPER_VOICE_EN, "-f", _pre, "-q"],
                            input=b"This is an audio test. 1, 2, 3, 4, 5.",
                            capture_output=True, env=PIPER_ENV,
                        )
                        while _headset_cal_loop[0]:
                            if radio and aioc_sink:
                                # Radio profile active → PTT + transmit over AIOC
                                _ptt_key()
                                _tl.sleep(AIOC_PTT_PREKEY_MS / 1000)
                                proc = _sp.Popen(["paplay", f"--device={aioc_sink}", _pre],
                                                 stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                            elif mon_sink:
                                # Monitor device active → play to monitoring speaker
                                proc = _sp.Popen(["paplay", f"--device={mon_sink}", _pre],
                                                 stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                            else:
                                proc = _sp.Popen(["aplay", "-D", alsa, "-q", _pre],
                                                 stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                            _headset_cal_proc[0] = proc
                            proc.wait()
                            _headset_cal_proc[0] = None
                            if radio and aioc_sink:
                                _tl.sleep(AIOC_PTT_TAIL_MS / 1000)
                                _ptt_release()
                                _tl.sleep(1.0)   # pause between transmissions
                    finally:
                        try: _os.unlink(_pre)
                        except FileNotFoundError: pass
                import threading as _t2
                _t2.Thread(target=_loop, daemon=True).start()
                via = ("radio (PTT)" if _use_radio
                       else f"monitor ({_mon_sink_name.split('.')[-1][:20]})" if _mon_sink_name
                       else "speaker")
                _html(self, 200, f"<p>Loop started via {via}.</p>")

            elif self.path == "/speaker-cal/loop-stop":
                _headset_cal_loop[0] = False
                proc = _headset_cal_proc[0]
                if proc and proc.poll() is None:
                    proc.kill()
                _headset_cal_proc[0] = None
                _ptt_release()   # ensure PTT is released if loop was stopped mid-transmission
                _html(self, 200, "<p>Loop stopped.</p>")

            elif self.path.startswith("/speaker-cal/adjust"):
                import json as _json, re as _re5, urllib.parse as _up
                qs    = _up.parse_qs(_up.urlparse(self.path).query)
                delta = int(qs.get("delta", ["0"])[0])
                kind  = qs.get("type", ["vol"])[0]   # "vol" or "sw"

                def _snap10(val, d):
                    """Snap to nearest multiple of 10, then step by 10; min 1.
                    When AIOC is active allow up to 500% (radio TX needs boosted levels)."""
                    snapped = round(val / 10) * 10
                    result  = snapped + d
                    max_vol = 500 if _ptt_alive() else 100
                    return max(1, min(max_vol, result))

                import time as _tadj
                _in_tx = _tx_display_until[0] > _tadj.time() or _is_tx[0]
                # Target: TX window → AIOC sink; monitor active → monitor sink; else → default
                if _in_tx:
                    sink = _find_aioc_sink() or subprocess.run(
                        ["pactl","get-default-sink"], capture_output=True,text=True).stdout.strip()
                elif _aioc_monitor_sink[0]:
                    sink = _aioc_monitor_sink[0]
                else:
                    sink = subprocess.run(["pactl","get-default-sink"],
                                          capture_output=True,text=True).stdout.strip()
                if kind == "sw":
                    # Adjust software gain (_cal_sw_volume)
                    cur_sw  = int(_cal_sw_volume * 100)
                    new_sw  = _snap10(cur_sw, delta)
                    globals()['_cal_sw_volume'] = new_sw / 100.0
                else:
                    # Adjust PipeWire volume of the default sink
                    if sink:
                        cur_out = subprocess.run(["pactl", "get-sink-volume", sink],
                                                 capture_output=True, text=True).stdout
                        m = _re5.search(r'(\d+)%', cur_out)
                        cur = int(m.group(1)) if m else 50
                        new_vol = _snap10(cur, delta)
                        subprocess.run(["pactl", "set-sink-volume", sink, f"{new_vol}%"],
                                       capture_output=True)

                # Persist adjusted levels so they restore on reconnect/restart
                if sink:
                    _cur_out = subprocess.run(["pactl", "get-sink-volume", sink],
                                              capture_output=True, text=True).stdout
                    _m2 = _re5.search(r'(\d+)%', _cur_out)
                    _pw_now = int(_m2.group(1)) if _m2 else 50
                    _save_device_cal(sink, _pw_now, _cal_sw_volume)

                resp = _json.dumps(_get_device_status()).encode()
                self.send_response(200); self.send_header("Content-Type","application/json")
                self.send_header("Content-Length", str(len(resp))); self.end_headers()
                self.wfile.write(resp)

            elif self.path.startswith("/radio/profile"):
                import json as _json, urllib.parse as _up2
                qs2    = _up2.parse_qs(_up2.urlparse(self.path).query)
                action = qs2.get("set", [None])[0]   # "radio" | "mic" | None (toggle)
                if action == "radio":
                    go_radio = True
                elif action == "mic":
                    go_radio = False
                else:
                    go_radio = not _radio_profile_active[0]  # toggle based on actual profile state
                _radio_profile_active[0] = go_radio  # update immediately so page reflects new state
                import threading as _trad
                _trad.Thread(target=_apply_agc_profile, args=(go_radio,), daemon=True).start()
                resp = _json.dumps({
                    "profile": "radio" if go_radio else "mic",
                    "aioc_connected": _ptt_serial[0] is not None,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path.startswith("/aioc-monitor"):
                import json as _json, urllib.parse as _uam
                _qs_am = _uam.parse_qs(_uam.urlparse(self.path).query)
                _req_sink = _qs_am.get("sink", [None])[0]  # specific sink or None

                def _start_loopback(target_sink):
                    aioc_src = _find_aioc_source()
                    if not aioc_src or not target_sink:
                        return False
                    result = subprocess.run(
                        ["pactl", "load-module", "module-loopback",
                         f"source={aioc_src}", f"sink={target_sink}",
                         "latency_msec=20"],
                        capture_output=True, text=True)
                    mod_id = result.stdout.strip()
                    if mod_id.isdigit():
                        _aioc_monitor_module[0] = int(mod_id)
                        _aioc_monitor_sink[0]   = target_sink
                        log.info("AIOC monitor → %s (module %s)",
                                 target_sink.split(".")[-1][:30], mod_id)
                        return True
                    return False

                def _stop_loopback():
                    if _aioc_monitor_module[0] is not None:
                        subprocess.run(["pactl", "unload-module",
                                        str(_aioc_monitor_module[0])], capture_output=True)
                        _aioc_monitor_module[0] = None
                        _aioc_monitor_sink[0]   = None
                        log.info("AIOC monitor loopback stopped")

                if _req_sink:
                    # Specific sink requested — toggle: if already on this sink, stop; else switch
                    if _aioc_monitor_sink[0] == _req_sink:
                        _stop_loopback()
                        active = False
                    else:
                        _stop_loopback()   # unload previous if any
                        active = _start_loopback(_req_sink)
                else:
                    # No sink specified — toggle on/off using last sink or USB speaker fallback
                    if _aioc_monitor_module[0] is not None:
                        _stop_loopback()
                        active = False
                    else:
                        # Use first available non-AIOC, non-monitor, non-agc sink
                        fallback = next(
                            (l.split()[1] for l in subprocess.run(
                                ["pactl","list","short","sinks"],
                                capture_output=True, text=True).stdout.splitlines()
                             if l.split()[1:2] and
                                "AIOC" not in l and "All-In-One" not in l and
                                "monitor" not in l and "rtt_agc" not in l),
                            None)
                        active = _start_loopback(fallback) if fallback else False

                resp = _json.dumps({
                    "active": _aioc_monitor_module[0] is not None,
                    "sink":   _aioc_monitor_sink[0],
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/device-status":
                import json as _json, re as _re5
                try:
                    sinks_raw   = subprocess.run(["pactl", "list", "sinks"],
                                                  capture_output=True, text=True, timeout=8).stdout
                    sources_raw = subprocess.run(["pactl", "list", "sources"],
                                                  capture_output=True, text=True, timeout=8).stdout
                    cards_pb    = subprocess.run(["aplay",   "-l"],
                                                  capture_output=True, text=True, timeout=5).stdout
                    cards_cap   = subprocess.run(["arecord", "-l"],
                                                  capture_output=True, text=True, timeout=5).stdout
                    cards_raw   = cards_pb + cards_cap
                    def _parse_pw_blocks(raw, kind):
                        blocks = []
                        cur = {}
                        for line in raw.splitlines():
                            s = line.strip()
                            if line.startswith(f"\t{kind} #") or line.startswith(f"{kind} #"):
                                if cur:
                                    blocks.append(cur)
                                cur = {}
                            elif s.startswith("Name:"):
                                cur["name"] = s.split(":",1)[1].strip()
                            elif s.startswith("Description:"):
                                cur["desc"] = s.split(":",1)[1].strip()
                            elif s.startswith("State:"):
                                cur["state"] = s.split(":",1)[1].strip()
                            elif "alsa.card =" in s:
                                m = _re5.search(r'"(\d+)"', s)
                                if m: cur["card"] = m.group(1)
                        if cur:
                            blocks.append(cur)
                        return [b for b in blocks if "name" in b]
                    default_sink   = subprocess.run(["pactl","get-default-sink"],
                                                    capture_output=True,text=True).stdout.strip()
                    default_source = subprocess.run(["pactl","get-default-source"],
                                                    capture_output=True,text=True).stdout.strip()
                    sinks   = _parse_pw_blocks(sinks_raw, "Sink")
                    sources = _parse_pw_blocks(sources_raw, "Source")
                    # Parse ALSA cards — deduplicate by card number (aplay+arecord both list each card)
                    _seen_cards = set()
                    alsa_cards = []
                    for line in cards_raw.splitlines():
                        if line.startswith("card "):
                            m = _re5.match(r'card (\d+): (\S+) \[([^\]]+)\]', line)
                            if m and m.group(1) not in _seen_cards:
                                _seen_cards.add(m.group(1))
                                alsa_cards.append({"num": m.group(1), "id": m.group(2), "name": m.group(3)})
                    data = {
                        "default_sink":   default_sink,
                        "default_source": default_source,
                        "raw_mic_source": RAW_MIC_SOURCE,  # physical mic behind AGC
                        "monitor_sink":   _aioc_monitor_sink[0],  # active AIOC loopback sink
                        "agc_source":     AGC_SOURCE_NAME,
                        "sinks":   sinks,
                        "sources": sources,
                        "alsa_cards": alsa_cards,
                    }
                except Exception as e:
                    data = {"error": str(e)}
                resp = _json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path.startswith("/device-set"):
                import json as _json, urllib.parse as _up
                qs  = _up.parse_qs(self.path.split("?",1)[1] if "?" in self.path else "")
                dev_type = qs.get("type",[""])[0]   # "source" or "sink"
                dev_name = _up.unquote(qs.get("name",[""])[0])
                result   = {"ok": False, "msg": ""}
                try:
                    if dev_type == "sink" and dev_name:
                        subprocess.run(["pactl","set-default-sink", dev_name],
                                        check=True, capture_output=True)
                        # Ensure the new sink has an audible volume — speaker-cal safety
                        # resets all sinks to 1%, which would leave it inaudible.
                        # Apply saved calibration levels, or minimum safe if unknown
                        _known = _apply_device_cal(dev_name)
                        log.info("HTTP device-set: default sink → %s (%s)",
                                 dev_name, "calibrated" if _known else "new/unknown → minimum")
                        result["ok"]  = True
                        result["msg"] = (
                            f"Speaker set to {dev_name}. "
                            + ("Restored calibrated levels. " if _known
                               else "New device — starting at minimum. Use Manual adjustment. ")
                            + "Restarting audio…"
                        )
                    elif dev_type == "source" and dev_name:
                        # AGC is always the daemon's default source.
                        # Selecting a physical mic redirects AGC to capture
                        # from it — AGC never gets bypassed.
                        if dev_name == AGC_SOURCE_NAME:
                            # User picked the AGC source explicitly — no change needed
                            subprocess.run(["pactl","set-default-source", AGC_SOURCE_NAME],
                                           capture_output=True)
                            log.info("HTTP device-set: AGC source confirmed as default")
                            result["ok"]  = True
                            result["msg"] = "AGC mic is already active. No change needed."
                        else:
                            # Redirect AGC to capture from the chosen physical mic
                            ok = _update_agc_capture_source(dev_name)
                            # AGC gain/gate always applies (AGC normalises)
                            g = globals()
                            g['MIC_GAIN']      = AGC_MIC_GAIN
                            g['MIC_GATE_PEAK'] = AGC_MIC_GATE
                            _mic_gate_ref[0]   = AGC_MIC_GATE
                            # Clear any --input-source override so AGC stays active on restart
                            _update_service_input_source("")
                            log.info("HTTP device-set: AGC redirected to %s", dev_name)
                            result["ok"]  = True
                            result["msg"] = (
                                f"AGC mic redirected to {dev_name}. "
                                "WebRTC AGC still active. Restarting audio…"
                                if ok else
                                f"Could not redirect AGC — check PipeWire. Restarting…"
                            )
                    else:
                        result["msg"] = "Missing type or name"
                    if result["ok"]:
                        if dev_type == "sink":
                            # Speaker switch: PipeWire handles it immediately.
                            # No daemon restart needed — aplay -D default picks up the
                            # new sink on the next call, so test loops continue uninterrupted.
                            result["restart"] = False
                            result["msg"] = result["msg"].replace(" Restarting audio…", "")
                        else:
                            # Mic switch: sd.InputStream must be restarted to pick up new source.
                            result["restart"] = True
                            threading.Thread(target=lambda: (
                                __import__("time").sleep(0.5),
                                __import__("subprocess").run(
                                    ["systemctl","--user","restart","openclaw-realtimetalk"])
                            ), daemon=True).start()
                except Exception as e:
                    result["msg"] = str(e)
                resp = _json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/speaker-cal/vol":
                import json as _json, time as _tvol, re as _rvol
                _status = _get_device_status()
                _now = _tvol.time()
                _in_tx_window = _tx_display_until[0] > _now
                _tx_remaining = max(0, int(_tx_display_until[0] - _now)) if _in_tx_window else 0

                if _in_tx_window or _is_tx[0]:
                    # Show AIOC TX levels in red during and for 10s after transmission
                    _aioc_snk = _find_aioc_sink()
                    if _aioc_snk:
                        _vo = subprocess.run(["pactl","get-sink-volume",_aioc_snk],
                                             capture_output=True,text=True).stdout
                        _vm = _rvol.search(r'(\d+)%', _vo)
                        _status["spk_vol"] = (_vm.group(1)+'%') if _vm else _status["spk_vol"]
                        _status["speaker_name"] = "All-In-One-Cable (TX)"
                    _status["tx_mode"] = True
                    _status["tx_remaining"] = _tx_remaining
                elif _aioc_monitor_sink[0]:
                    # Show monitor device levels so user can adjust monitoring volume
                    _mon = _aioc_monitor_sink[0]
                    _vo = subprocess.run(["pactl","get-sink-volume",_mon],
                                         capture_output=True,text=True).stdout
                    _vm = _rvol.search(r'(\d+)%', _vo)
                    _status["spk_vol"] = (_vm.group(1)+'%') if _vm else _status["spk_vol"]
                    _status["speaker_name"] = _status.get("speaker_name","") + " (monitor)"
                    _status["tx_mode"] = False
                    _status["tx_remaining"] = 0
                else:
                    _status["tx_mode"] = False
                    _status["tx_remaining"] = 0

                resp = _json.dumps(_status).encode()
                self.send_response(200); self.send_header("Content-Type","application/json")
                self.send_header("Content-Length", str(len(resp))); self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/speaker-cal/set":
                # Headset mode: save current PipeWire level as calibrated
                import json as _json, re as _re6
                _headset_cal_loop[0] = False
                ds = _get_device_status()
                _update_service_alsa_output(ds["speaker_alsa"])
                # Always use the PipeWire default sink (currently selected speaker)
                sink = subprocess.run(["pactl","get-default-sink"],
                                      capture_output=True,text=True).stdout.strip()
                if sink:
                    cur_out = subprocess.run(["pactl", "get-sink-volume", sink],
                                             capture_output=True, text=True).stdout
                    m = _re6.search(r'(\d+)%', cur_out)
                    pw = int(m.group(1)) if m else 50
                    # Save to per-device calibration store
                    _save_device_cal(sink, pw, _cal_sw_volume)
                log.info("Headset cal: saved level PW=%s SW=%d%%", ds["spk_vol"], ds["sw_pct"])
                # Announce
                if sess:
                    import threading as _t3
                    _t3.Thread(target=speak,
                               args=("Headset volume saved.", sess.alsa_output),
                               daemon=True).start()
                resp = _json.dumps(ds).encode()
                self.send_response(200); self.send_header("Content-Type","application/json")
                self.send_header("Content-Length", str(len(resp))); self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/levels":
                import time as _time
                self.send_response(200)
                self.send_header("Content-Type",  "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection",    "keep-alive")
                self.end_headers()
                try:
                    while True:
                        with _mic_level_lock:
                            peak = _mic_level_current[0]
                        msg = f"data: {peak},{_mic_gate_ref[0]}\n\n".encode()
                        self.wfile.write(msg)
                        self.wfile.flush()
                        _time.sleep(0.1)
                except Exception:
                    pass
            elif self.path == "/log":
                # Legacy redirect
                self.send_response(301)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path in ("/dashboard", "/"):
                # Check for device changes on every page load
                new_fp = _get_audio_fingerprint()
                device_banner = ""
                if new_fp and new_fp != _audio_fingerprint[0]:
                    old_fp = _audio_fingerprint[0]
                    _audio_fingerprint[0] = new_fp
                    # Suppress announcement if only HDMI changed — display-source
                    # switches connect/disconnect HDMI audio but are not real
                    # speaker changes and should not interrupt with audio.
                    def _non_hdmi(fp):
                        return "\n".join(
                            l for l in fp.splitlines()
                            if "hdmi" not in l.lower()
                        )
                    hdmi_only = (_non_hdmi(new_fp) == _non_hdmi(old_fp))
                    msg = "Audio devices changed."
                    _device_change_msg[0] = msg
                    log.info("Device change detected%s", " (HDMI only — silent)" if hdmi_only else "")
                    # Check AIOC connect/disconnect immediately on device change —
                    # don't wait for the elif _ptt_alive() branch which is skipped
                    # when _device_change_msg is set.
                    _ptt_alive()
                    if sess and not hdmi_only:
                        import threading as _t
                        def _announce_change():
                            _safe_volume_new_sinks(1)   # PipeWire at 1% safety reset
                            import time as _time; _time.sleep(0.5)
                            # Restore calibrated levels for ALL known sinks, not just default
                            _cal_sinks = subprocess.run(
                                ["pactl","list","short","sinks"],
                                capture_output=True, text=True).stdout.splitlines()
                            for _sl in _cal_sinks:
                                _sn = _sl.split()[1] if len(_sl.split()) > 1 else None
                                if _sn:
                                    _apply_device_cal(_sn)
                            # Don't announce over the air — skip TTS when Radio profile active
                            if not _radio_profile_active[0]:
                                speak(msg, sess.alsa_output, volume=_cal_sw_volume)
                        _t.Thread(target=_announce_change, daemon=True).start()

                if _device_change_msg[0]:
                    device_banner = (
                        f'<div id="dbanner" style="background:#3a1500;border:1px solid #7a3000;'
                        f'color:#f59e0b;padding:3px 8px;">'
                        f'{_device_change_msg[0]}</div>'
                        f'<script>setTimeout(()=>{{var b=document.getElementById("dbanner");'
                        f'if(b){{b.textContent="";b.removeAttribute("style");}}}},5000);</script>'
                    )
                    _device_change_msg[0] = ""
                elif _radio_profile_active[0]:
                    # Persistent warning only when Radio profile is active (not just AIOC connected)
                    device_banner = (
                        '<div id="dbanner" style="background:#3b0000;border:1px solid #dc2626;'
                        'color:#fca5a5;padding:4px 10px;font-weight:bold;letter-spacing:.03em;">'
                        '&#128225; AIOC ACTIVE &mdash; audio output transmits LIVE OVER THE AIR'
                        '</div>'
                    )
                elif _idle_disconnected[0]:
                    device_banner = (
                        '<div id="dbanner" style="color:#475569;font-style:italic;">'
                        'Say &#8220;Hey Jarvis&#8221; or press Wake to resume.</div>'
                    )
                else:
                    device_banner = '<div id="dbanner"></div>'

                active     = sess._active if sess else False
                monitoring = sess._monitoring if sess else False
                multilang  = sess._multilang if sess else "off"
                paused     = _paused_speech[0] is not None
                speaking   = _is_speaking[0]
                thinking   = _current_think_task[0] is not None

                state = ("SLEEPING"   if _idle_disconnected[0]
                         else "MONITORING" if monitoring
                         else "SPEAKING"   if speaking
                         else "THINKING"   if thinking
                         else "PAUSED"     if (active and paused)
                         else "ACTIVE"     if active else "SILENT")
                _sc = {"ACTIVE":("#0d2818","#34d399"),"SILENT":("#141d2b","#64748b"),
                       "THINKING":("#1c1304","#f59e0b"),"SPEAKING":("#031a10","#2dd4bf"),
                       "PAUSED":("#150d2e","#a5b4fc"),"MONITORING":("#071a2e","#60a5fa"),
                       "SLEEPING":("#0e0e14","#475569"),
                       }.get(state,("#141d2b","#64748b"))
                state_pill_style = f"background:{_sc[0]};color:{_sc[1]};border-color:{_sc[1]};"

                speaking_banner = (
                    '<div class="spkbanner" style="background:#3b0000;border-color:#dc2626;color:#fca5a5;">'
                    '&#128225; TRANSMITTING&hellip;'
                    ' &nbsp;<a href="/interrupt" class="irupt">&#10005; Stop</a></div>'
                    if (_is_tx[0] and speaking) else
                    '<div class="spkbanner">&#9834; Five is speaking&hellip;'
                    ' &nbsp;<a href="/interrupt" class="irupt">&#10005; Stop</a></div>'
                    if speaking else
                    '<div class="spkbanner paused">&#9646;&#9646; Paused'
                    ' &nbsp;<a href="/continue" class="cont">&#9654; Continue</a></div>'
                    if (active and paused) else ""
                )

                # Pre-compute thinking durations
                thinking_dur: dict = {}
                for _i, _e in enumerate(CONVERSATION_LOG):
                    if _e["role"] == "thinking":
                        _ep = _e.get("epoch", 0.0)
                        for _j in range(_i + 1, len(CONVERSATION_LOG)):
                            if CONVERSATION_LOG[_j]["role"] == "five":
                                thinking_dur[_ep] = (
                                    CONVERSATION_LOG[_j].get("epoch", _ep) - _ep
                                )
                                break
                        else:
                            thinking_dur[_ep] = None

                rows = ""
                for e in reversed(CONVERSATION_LOG):
                    ts = e.get("ts", "")
                    ts_span = f'<span class="ts">{ts}</span> ' if ts else ""
                    if e["role"] == "you":
                        rows += f'<div class="you">{ts_span}<b>You:</b> {e["text"]}</div>'
                    elif e["role"] == "five":
                        rows += f'<div class="five">{ts_span}<b>Five:</b> {e["text"]}</div>'
                    elif e["role"] == "monitor":
                        rows += f'<div class="mon">{ts_span}{e["text"]}</div>'
                    elif e["role"] == "thinking":
                        ep  = e.get("epoch", 0.0)
                        dur = thinking_dur.get(ep)
                        if dur is None:
                            rows += (f'<div class="thinking">{ts_span}'
                                     f'Five is thinking&hellip; '
                                     f'<span class="tctr" data-start="{ep:.3f}">0</span>s'
                                     f' &nbsp;<a href="/interrupt" class="irupt">&#10005; Interrupt</a>'
                                     f'</div>')
                    else:
                        rows += f'<div class="sys">{ts_span}{e["text"]}</div>'

                _ds = _get_device_status()
                device_panel = (
                    f'<div id="dp">&#9673; {_ds["mic"]} &ensp;'
                    f'&#9834; {_ds["speaker_name"]} &middot; Vol {_ds["spk_vol"]} &middot; SW {_ds["sw_pct"]}%'
                    f' &ensp;Gate {_ds["gate"]} &middot; Gain {_ds["gain"]}x</div>'
                )

                body = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>RealTimeTalk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#07090f;--sf:#0d1119;--sf2:#121925;--bd:#1a2535;--tx:#dde4ef;--mu:#5a7088;--di:#253344;--you:#38bdf8;--yb:#051928;--bot:#f59e0b;--bb:#130e02;--mon:#a78bfa;--mb:#0e0820;--sy:#304558;--rd:#ef4444;--rdb:#150303;--gn:#34d399;--gnb:#021a0e;--r:8px;}}
html,body{{height:100%;}}
body{{font-family:'Outfit',system-ui,'Noto Color Emoji',sans-serif;font-size:16px;background:var(--bg);color:var(--tx);display:flex;flex-direction:column;overflow:hidden;-webkit-text-size-adjust:100%;}}
#top{{flex-shrink:0;background:var(--sf);border-bottom:1px solid var(--bd);padding:10px 14px 8px;}}
.hrow{{display:flex;align-items:center;gap:8px;margin-bottom:8px;}}
.brand{{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:600;color:var(--tx);letter-spacing:.08em;text-transform:uppercase;}}
.spill{{margin-left:10px;font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;padding:5px 14px;border-radius:20px;border:2px solid transparent;white-space:nowrap;}}
.nav{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:7px;}}
a.btn{{display:inline-flex;align-items:center;gap:3px;padding:7px 14px;border-radius:8px;font-family:'Outfit','Noto Color Emoji',sans-serif;font-size:14px;font-weight:500;color:var(--mu);background:var(--sf2);border:1px solid var(--bd);text-decoration:none;min-height:36px;white-space:nowrap;transition:background .12s,border-color .12s,color .12s;}}
a.btn:hover{{background:#1e2d3d;border-color:var(--you);color:var(--you);box-shadow:0 0 0 2px rgba(56,189,248,.25);}}
a.btn.on{{background:var(--gnb);border-color:var(--gn);color:var(--gn);}}
a.btn.on:hover{{background:#053d20;color:#fff;box-shadow:0 0 0 2px rgba(52,211,153,.25);}}
a.btn.danger{{color:var(--rd);}}
a.btn.danger:hover{{background:var(--rdb);border-color:var(--rd);box-shadow:0 0 0 2px rgba(239,68,68,.25);}}
#dp{{font-family:'JetBrains Mono',monospace;font-size:12px;color:#8aa0b8;padding:6px 10px;background:var(--bg);border-radius:5px;border:1px solid var(--di);margin-top:4px;}}
#dbanner{{border-radius:6px;padding:6px 10px;margin-top:6px;font-size:13px;font-family:'JetBrains Mono',monospace;}}
#log{{flex:1;overflow-y:auto;padding:10px 14px;}}
.you{{background:var(--yb);border-left:3px solid var(--you);border-radius:var(--r);padding:8px 10px;margin:3px 0;}}
.you b{{color:var(--you);}}
.five{{background:var(--bb);border-left:3px solid var(--bot);border-radius:var(--r);padding:8px 10px;margin:3px 0;}}
.five b{{color:var(--bot);}}
.mon{{background:var(--mb);border-left:3px solid var(--mon);border-radius:var(--r);padding:8px 10px;margin:3px 0;}}
.sys{{color:var(--sy);font-size:.8em;text-align:center;margin:3px 0;font-family:'JetBrains Mono',monospace;}}
.thinking{{background:var(--bb);border-left:3px solid var(--bot);border-radius:var(--r);padding:8px 10px;margin:3px 0;color:var(--bot);font-style:italic;}}
.ts{{font-family:'JetBrains Mono',monospace;font-size:.75em;color:var(--mu);margin-right:4px;}}
a.irupt{{color:var(--rd);background:var(--rdb);border:1px solid var(--rd);border-radius:4px;padding:2px 8px;font-size:.82em;font-style:normal;text-decoration:none;margin-left:8px;}}
a.irupt:hover{{background:var(--rd);color:#fff;}}
a.cont{{color:var(--gn);background:var(--gnb);border:1px solid var(--gn);border-radius:4px;padding:2px 8px;font-size:.82em;font-style:normal;text-decoration:none;margin-left:8px;}}
a.cont:hover{{background:var(--gn);color:#000;}}
.spkbanner{{background:var(--gnb);border-left:3px solid var(--gn);border-radius:var(--r);padding:8px 10px;margin:3px 0;color:var(--gn);font-style:italic;}}
.spkbanner.paused{{background:var(--mb);border-color:var(--mon);color:var(--mon);}}
#dbanner{{min-height:1.2em;font-size:.8em;font-family:'JetBrains Mono',monospace;padding:3px 8px;transition:color .1s;}}
@media(max-width:520px){{body{{font-size:15px;}}#top{{padding:8px 10px 6px;}}a.btn{{padding:9px 12px;font-size:13px;}}}}
@media(min-width:900px){{body{{font-size:17px;}}#top{{padding:14px 24px 10px;}}a.btn{{font-size:15px;padding:8px 16px;}}#dp{{font-size:13px;}}#log{{padding:14px 24px;}}}}
</style></head><body>
<div id="top">
<div class="hrow"><span class="brand">&#9679;&nbsp;RealTimeTalk</span><span class="spill" style="{state_pill_style}">{state}</span><a href="/calibration" class="btn" data-hint="Open speaker &amp; mic level calibration">&#9999; Calibrate</a></div>
<div class="nav"><a href="/wake" class="btn" data-hint="Activate voice — the agent will listen and respond">&#9889; Wake</a><a href="/sleep" class="btn" data-hint="Silence voice and stop monitoring. Say Hey Jarvis or press Wake to resume">&#9790; Sleep</a><a href="/monitor/{'stop' if monitoring else 'start'}" class="btn {'on' if monitoring else ''}" data-hint="{'Now: Monitoring ON. Click → stop monitoring' if monitoring else 'Now: OFF. Click → start passive monitoring (transcribes without routing to agent)'}">&#9678; {'Monitor On' if monitoring else 'Monitor'}</a><a href="/multilang" class="btn {'on' if multilang != 'off' else ''}" data-hint="{'Now: OFF — EN/ZH only, auto-sleep on. Click → EN/ZH mode (auto-sleep off)' if multilang == 'off' else 'Now: EN/ZH — auto-sleep off. Click → Whitelist (EN/ZH/KO/JA/ES/MS)' if multilang == 'en-zh' else 'Now: Whitelist — EN/ZH/KO/JA/ES/MS, auto-sleep off. Click → Any language' if multilang == 'whitelist' else 'Now: Any language — auto-sleep off. Click → OFF'}">&#8853; {'Multi-lang' if multilang == 'off' else 'Lang: EN/ZH' if multilang == 'en-zh' else 'Lang: List' if multilang == 'whitelist' else 'Lang: Any'}</a><a href="/reset" class="btn danger" data-hint="Clear the conversation log (does not affect the agent&apos;s memory)">&#10006; Clear Log</a><a href="/restart" class="btn" data-hint="Restart the RealTimeTalk daemon (reconnects OpenAI and gateway)">&#8635; Restart</a><a href="/gateway-reset" class="btn danger" data-hint="Drop and reconnect the OpenClaw gateway WebSocket without restarting">&#9888; Gateway Reset</a></div>
{device_panel}{device_banner}</div>
<div id="log">{speaking_banner}{rows if rows else "<div class='sys'>No conversation yet</div>"}</div>
<script>
setInterval(function(){{
  var now=Date.now()/1000;
  document.querySelectorAll('.tctr').forEach(function(el){{
    el.textContent=Math.max(0,Math.floor(now-parseFloat(el.dataset.start)));
  }});
}},500);
setInterval(function(){{
  fetch('/speaker-cal/vol').then(function(r){{return r.json();}}).then(function(d){{
    var dp=document.getElementById('dp');
    if(dp) dp.innerHTML='&#9673; '+d.mic+' &ensp;&#9834; '+d.speaker_name+' &middot; Vol '+d.spk_vol+' &middot; SW '+d.sw_pct+'% &ensp;Gate '+d.gate+' &middot; Gain '+d.gain+'x';
  }}).catch(function(){{}});
}}, 5000);
(function(){{
  var _rt=setTimeout(()=>location.reload(),3000);
  function _cancelReload(){{clearTimeout(_rt);}}
  function _scheduleReload(){{_rt=setTimeout(()=>location.reload(),3000);}}
  document.querySelectorAll('.btn[data-hint]').forEach(function(b){{
    b.addEventListener('mouseenter',function(){{
      _cancelReload();
      var h=document.getElementById('dbanner');
      if(h&&!h.style.background){{h.textContent=b.dataset.hint;h.style.color='#64748b';}}
    }});
    b.addEventListener('mouseleave',function(){{
      _scheduleReload();
      var h=document.getElementById('dbanner');
      if(h&&!h.style.background){{h.textContent='';h.style.color='';}}
    }});
  }});
}})();
</script>
</body></html>"""
                _html(self, 200, body)
            elif self.path == "/status":
                sess = session_ref[0]
                active = sess._active if sess else False
                body = json.dumps({"status": "running", "voice": "active" if active else "silent"}).encode()
                self.send_response(200)
                self.send_header("Content-Type",   "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                _html(self, 404, "<h2>Not found</h2>")

    from socketserver import ThreadingMixIn
    class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
    server = _ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Toggle: http://<pi-ip>:%d/stop  |  /wake  |  /sleep  |  /status", port)

# ── openwakeword listener ─────────────────────────────────────────────────────

def _load_dtmf_profiles() -> dict:
    """Load learned DTMF frequency profiles from disk."""
    try:
        with open(DTMF_PROFILE_FILE) as f:
            import json as _j
            p = _j.load(f)
            log.info("DTMF: loaded %d learned profiles from %s", len(p), DTMF_PROFILE_FILE)
            return p
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("DTMF: could not load profiles: %s", e)
        return {}


def _goertzel_energy(samples: list, freq: float, rate: int) -> float:
    """Return Goertzel energy at freq in samples."""
    import math
    n = len(samples)
    k = int(0.5 + n * freq / rate)
    w = 2 * math.pi * k / n
    c = 2 * math.cos(w)
    q1 = q2 = 0.0
    for s in samples:
        q0 = s + c * q1 - q2; q2 = q1; q1 = q0
    return q2 * q2 + q1 * q1 - c * q1 * q2


def _decode_with_profiles(frame, profiles: dict):
    """Decode DTMF digit from int16 frame using learned profiles. Returns digit or None."""
    import numpy as _np_dtmf
    samples = frame.astype(_np_dtmf.float64).tolist()
    scores  = {d: (_goertzel_energy(samples, p['row_hz'], DTMF_SAMPLE_RATE) +
                   _goertzel_energy(samples, p['col_hz'], DTMF_SAMPLE_RATE))
               for d, p in profiles.items()}
    if not scores:
        return None
    best   = max(scores, key=scores.get)
    best_e = scores[best]
    median = sorted(scores.values())[len(scores) // 2]
    if best_e < 1e6 or (median > 0 and best_e / median < 3.0):
        return None
    return best


def _dtmf_listener() -> None:
    """Monitor AIOC audio for DTMF sequences.

    When learned profiles exist (~/.config/rtt/dtmf_profiles.json):
      Uses custom Goertzel detector with radio-specific frequencies.
      COS gated: only accepts digits when raw audio > DTMF_COS_THRESHOLD.

    When no profiles:
      Falls back to pacat → sox → multimon-ng pipeline.

    Runs as a daemon thread; restarts automatically on AIOC disconnect.
    """
    import time as _td, re as _re_dtmf
    import numpy as _np_dtmf

    _SEQ_TIMEOUT    = 10.0
    _DIGIT_COOLDOWN = 0.3
    _DTMF_PAT       = _re_dtmf.compile(r'^DTMF:\s*([0-9A-D*#])$')
    _RATE48         = 48000
    _FRAME_8K       = DTMF_SAMPLE_RATE // 10      # 100ms window at 8kHz = 800 samples
    _FRAME_48K      = _RATE48 // 10               # 100ms window at 48kHz = 4800 samples
    _CHUNK48        = _RATE48 * 2 * 50 // 1000    # 50ms chunk at 48kHz = 4800 bytes

    def _handle_digit(digit, now, seq_ref):
        seq = seq_ref[0]
        if seq and now - _state_time[0] > _SEQ_TIMEOUT:
            seq = ""
        if digit == _last_dig[0] and now - _last_dig_t[0] < _DIGIT_COOLDOWN:
            return seq
        _last_dig[0] = digit; _last_dig_t[0] = now; _state_time[0] = now
        if not seq or seq[-1] != digit:
            seq += digit
            log.info("DTMF digit: %s → seq=%s", digit, seq)
        max_len = max(len(DTMF_WAKE_SEQ), len(DTMF_SLEEP_SEQ), len(DTMF_DEEPSLEEP_SEQ),
                      len(DTMF_MONITOR_ON_SEQ), len(DTMF_MONITOR_OFF_SEQ), len(DTMF_WAKE_SILENT_SEQ))
        if len(seq) > max_len:
            seq = seq[-max_len:]
        if DTMF_WAKE_SEQ in seq:
            seq = ""
            log.info("DTMF wake '%s' received", DTMF_WAKE_SEQ)
            _log_entry("system", f"DTMF {DTMF_WAKE_SEQ} — waking Five")
            if _idle_disconnected[0] and _wake_event[0]:
                _last_activity[0] = now; _wake_activate[0] = True
                _persist_active[0] = True; _save_sleep_state(False)
                _wake_event[0].set()
            elif _wake_event[0]:
                _persist_active[0] = True; _wake_activate[0] = True
                _dtmf_force_active[0] = True   # signal current silent session to go active immediately
        elif DTMF_SLEEP_SEQ in seq:
            seq = ""
            log.info("DTMF sleep '%s' received", DTMF_SLEEP_SEQ)
            _log_entry("system", f"DTMF {DTMF_SLEEP_SEQ} — Five silent")
            _persist_active[0] = False
            _dtmf_force_silent[0] = True
        elif DTMF_DEEPSLEEP_SEQ in seq:
            seq = ""
            log.info("DTMF deep-sleep '%s' received", DTMF_DEEPSLEEP_SEQ)
            _log_entry("system", f"DTMF {DTMF_DEEPSLEEP_SEQ} — Five sleeping (disconnecting)")
            _persist_active[0] = False
            _persist_monitoring[0] = False   # clear monitoring when going to deep sleep
            _dtmf_force_deepsleep[0] = True
        elif DTMF_MONITOR_ON_SEQ in seq:
            seq = ""
            log.info("DTMF monitor-on '%s' received", DTMF_MONITOR_ON_SEQ)
            _log_entry("system", f"DTMF {DTMF_MONITOR_ON_SEQ} — monitoring on")
            _persist_monitoring[0] = True
            _persist_active[0] = False       # monitoring is passive, not active
            _dtmf_force_monitor[0] = True
            if _idle_disconnected[0] and _wake_event[0]:
                # Sleeping → wake into monitoring (no _wake_activate so session starts silent+monitoring)
                _last_activity[0] = now
                _save_sleep_state(False)
                _wake_event[0].set()
        elif DTMF_MONITOR_OFF_SEQ in seq:
            seq = ""
            log.info("DTMF monitor-off '%s' received", DTMF_MONITOR_OFF_SEQ)
            _log_entry("system", f"DTMF {DTMF_MONITOR_OFF_SEQ} — monitoring off")
            _persist_monitoring[0] = False
            _dtmf_force_monitor[0] = False
        elif DTMF_WAKE_SILENT_SEQ in seq:
            seq = ""
            log.info("DTMF wake-silent '%s' received", DTMF_WAKE_SILENT_SEQ)
            _log_entry("system", f"DTMF {DTMF_WAKE_SILENT_SEQ} — waking to silent")
            _persist_active[0] = False       # silent, not active
            _persist_monitoring[0] = False   # not monitoring
            if _idle_disconnected[0] and _wake_event[0]:
                _last_activity[0] = now
                _save_sleep_state(False)
                _wake_event[0].set()
            # If already awake (silent/monitoring), nothing extra needed
        return seq

    _last_dig   = [None];  _last_dig_t = [0.0];  _state_time = [0.0]
    _cos_until  = [0.0]

    while True:
        aioc_src = _find_aioc_source()
        if not aioc_src:
            _td.sleep(3); continue

        profiles = _load_dtmf_profiles()

        if profiles:
            # ── Goertzel path with learned profiles ──────────────────────
            log.info("DTMF listener ready via learned profiles "
                     "(wake=%s sleep=%s COS≥%d)",
                     DTMF_WAKE_SEQ, DTMF_SLEEP_SEQ, DTMF_COS_THRESHOLD)
            try:
                proc = subprocess.Popen(
                    ["pacat", "--record", "--raw", "--format=s16le",
                     "--rate=48000", "--channels=1", "--latency-msec=50",
                     f"--device={aioc_src}"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

                buf = b""; seq_ref = [""]
                prev_dig = [None]; hold = [0]
                acc = _np_dtmf.array([], dtype=_np_dtmf.int16)  # rolling 48kHz accumulator

                while _find_aioc_source():
                    chunk = proc.stdout.read(_CHUNK48)
                    if not chunk: break
                    raw   = _np_dtmf.frombuffer(chunk, dtype=_np_dtmf.int16)
                    peak  = int(_np_dtmf.max(_np_dtmf.abs(raw)))
                    now   = _td.time()
                    # COS detection on raw 48kHz level
                    if peak > DTMF_COS_THRESHOLD:
                        _cos_until[0] = now + DTMF_COS_TAIL_S
                    cos_open = now < _cos_until[0]
                    if not cos_open:
                        prev_dig[0] = None; hold[0] = 0
                        acc = _np_dtmf.array([], dtype=_np_dtmf.int16)
                        continue
                    # Accumulate until we have a 100ms window at 48kHz
                    acc = _np_dtmf.concatenate([acc, raw])
                    if len(acc) < _FRAME_48K:
                        continue
                    # Keep last 100ms, decimate 6:1 → 8kHz for Goertzel
                    frame_48k = acc[-_FRAME_48K:]
                    acc       = frame_48k  # slide window
                    frame_8k  = frame_48k[::6].copy()
                    digit = _decode_with_profiles(frame_8k, profiles)
                    if digit == prev_dig[0]:
                        hold[0] += 1
                    else:
                        prev_dig[0] = digit; hold[0] = 1
                    if digit and hold[0] == 3:
                        seq_ref[0] = _handle_digit(digit, now, seq_ref)
            except Exception as exc:
                log.warning("DTMF Goertzel error: %s", exc)
            finally:
                try: proc.kill()
                except Exception: pass

        else:
            # ── multimon-ng fallback ──────────────────────────────────────
            log.info("DTMF listener ready via multimon-ng "
                     "(no profiles — run dtmf_monitor.py --train to improve) "
                     "wake=%s sleep=%s", DTMF_WAKE_SEQ, DTMF_SLEEP_SEQ)
            try:
                pacat = subprocess.Popen(
                    ["pacat", "--record", "--raw", "--format=s16le",
                     "--rate=48000", "--channels=1", "--latency-msec=100",
                     f"--device={aioc_src}"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                sox = subprocess.Popen(
                    ["sox", "-t", "raw", "-r", "48000",
                     "-e", "signed-integer", "-b", "16", "-c", "1", "-",
                     "-t", "raw", "-r", "22050", "-"],
                    stdin=pacat.stdout, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL)
                mmng = subprocess.Popen(
                    ["multimon-ng", "-a", "DTMF", "-t", "raw", "-"],
                    stdin=sox.stdout, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL)
                pacat.stdout.close(); sox.stdout.close()
                seq_ref = [""]
                for raw_line in mmng.stdout:
                    now  = _td.time()
                    line = raw_line.decode(errors="ignore").strip()
                    m    = _DTMF_PAT.match(line)
                    if not m: continue
                    seq_ref[0] = _handle_digit(m.group(1), now, seq_ref)
                    if not _find_aioc_source(): break
            except Exception as exc:
                log.warning("DTMF multimon-ng error: %s", exc)
            finally:
                for p in (mmng, sox, pacat):
                    try: p.kill()
                    except Exception: pass

        _td.sleep(5)


def _oww_wakeword_listener(input_device, stop_flag: list) -> None:
    """Background thread: always-on local wake word detection via openwakeword.

    Runs independently of the OpenAI session so wake detection works even in
    SLEEP state (when the OpenAI WebSocket is closed).  Says "Hey Jarvis" to
    wake Five from sleep; also refreshes _last_activity while awake so a user
    speaking is never mis-counted as idle.
    """
    import queue as _q
    import time as _ti

    try:
        import openwakeword as _oww_pkg
        from openwakeword.model import Model as _OWWModel
    except ImportError:
        log.warning("openwakeword not installed — local wake word detection disabled")
        return

    _models_dir = os.path.join(os.path.dirname(_oww_pkg.__file__), "resources", "models")
    _model_path = os.path.join(_models_dir, "hey_jarvis_v0.1.onnx")
    if not os.path.exists(_model_path):
        log.warning("hey_jarvis model not found at %s — local wake word disabled", _model_path)
        return

    try:
        oww = _OWWModel(wakeword_models=[_model_path], inference_framework='onnx')
    except Exception as exc:
        log.error("openwakeword model load failed: %s", exc)
        return

    log.info("openwakeword listener ready — say 'Hey Jarvis' to wake from sleep")

    OWW_RATE  = 16000
    OWW_CHUNK = 1280   # 80 ms at 16 kHz — openwakeword's native frame size
    _THRESHOLD = OWW_THRESHOLD
    _DEBOUNCE  = 3.0   # seconds to ignore detections after a trigger

    audio_q: _q.Queue = _q.Queue(maxsize=50)

    def _cb(indata, frames, t, status):
        if not audio_q.full():
            audio_q.put_nowait(indata[:, 0].copy())
        # Feed mic level meter so calibration page stays alive during sleep
        raw_peak = int(np.max(np.abs(indata[:, 0])))
        with _mic_level_lock:
            _mic_level_current[0] = raw_peak

    last_trigger = 0.0
    buf = np.array([], dtype=np.int16)

    try:
        with sd.InputStream(samplerate=OWW_RATE, channels=1, dtype='int16',
                            blocksize=OWW_CHUNK, callback=_cb,
                            device=input_device):
            while not stop_flag[0]:
                try:
                    chunk = audio_q.get(timeout=0.15)
                except _q.Empty:
                    continue
                buf = np.concatenate([buf, chunk])
                while len(buf) >= OWW_CHUNK:
                    frame = buf[:OWW_CHUNK]
                    buf   = buf[OWW_CHUNK:]
                    try:
                        preds = oww.predict(frame)
                    except Exception:
                        continue
                    score = preds.get('hey_jarvis_v0.1', 0.0)
                    now   = _ti.time()
                    if score >= _THRESHOLD and (now - last_trigger) >= _DEBOUNCE:
                        last_trigger = now
                        log.info("Wake word detected (score=%.2f)", score)
                        if _idle_disconnected[0] and _wake_event[0]:
                            _log_entry("system", "Wake word detected — entering silent mode. Say 'Five wake up' to activate.")
                            _last_activity[0] = now
                            # Intentionally NOT setting _wake_activate — OWW wakes to Silent,
                            # not Active. User must say the wake phrase to go Active.
                            _save_sleep_state(False)
                            _wake_event[0].set()
                        else:
                            _last_activity[0] = now
    except Exception as exc:
        log.error("openwakeword listener crashed: %s", exc)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(http_port: int, input_device=None, alsa_output: str = ALSA_OUTPUT,
               session_key: str = OPENCLAW_SESSION):
    global ALSA_OUTPUT
    ALSA_OUTPUT = alsa_output   # sync global to CLI arg so HTTP handlers use the right device
    # Recover or clean up loopback modules left from a previous run.
    # Keep valid loopbacks (source still exists); kill only stale ones.
    try:
        _mods = subprocess.run(["pactl","list","short","modules"],
                               capture_output=True, text=True).stdout
        _sources = subprocess.run(["pactl","list","short","sources"],
                                  capture_output=True, text=True).stdout
        _source_names = {l.split()[1] for l in _sources.splitlines() if len(l.split())>1}
        import re as _re_lb
        for _ml in _mods.splitlines():
            if "module-loopback" not in _ml:
                continue
            _mid = _ml.split()[0]
            _src_m = _re_lb.search(r'source=(\S+)', _ml)
            _snk_m = _re_lb.search(r'sink=(\S+)', _ml)
            _src = _src_m.group(1) if _src_m else None
            _snk = _snk_m.group(1) if _snk_m else None
            if _src and _src in _source_names:
                # Valid loopback — restore tracking state
                _aioc_monitor_module[0] = int(_mid)
                _aioc_monitor_sink[0]   = _snk
                log.info("Restored monitor loopback module %s → %s",
                         _mid, (_snk or '?').split('.')[-1][:30])
            else:
                # Source gone — stale module
                subprocess.run(["pactl","unload-module", _mid], capture_output=True)
                log.info("Cleaned up stale loopback module %s (source gone)", _mid)
    except Exception:
        pass
    _ptt_open()                 # open AIOC serial port if present; non-fatal if absent
    if _ptt_serial[0] is not None:
        # Check conf file to determine actual profile (user may have switched to mic mode)
        try:
            import re as _re_agc
            _agc_content = open(_AGC_CONF).read()
            _is_radio_conf = ("gain_control = false" in _agc_content or
                              "AIOC" in _agc_content or "All-In-One" in _agc_content)
        except Exception:
            _is_radio_conf = True  # default to radio if can't read conf
        if _is_radio_conf:
            globals()['MIC_GAIN'] = AGC_MIC_GAIN_RADIO
            _radio_profile_active[0] = True
            log.info("AIOC present at startup — radio profile active, MIC_GAIN=%.0fx",
                     AGC_MIC_GAIN_RADIO)
        else:
            _radio_profile_active[0] = False
            log.info("AIOC present at startup — mic profile active (conf file), MIC_GAIN=%.0fx",
                     AGC_MIC_GAIN)
    loop       = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        openai_key = load_openai_key()
        gw_token   = load_gateway_token()
    except Exception as e:
        log.error(str(e))
        sys.exit(1)

    gw = GatewayClient(gw_token)
    while not stop_event.is_set():
        try:
            await gw.connect()
            break
        except (ConnectionRefusedError, OSError) as e:
            log.warning("Gateway not ready (%s) — retrying in 5s…", e)
            await asyncio.sleep(5)
    if stop_event.is_set():
        return
    gw_task = asyncio.create_task(gw.listen(stop_event))

    import threading as _thr
    _wake_event[0] = _thr.Event()
    _last_activity[0] = __import__("time").time()
    _oww_stop_flag[0] = False
    _thr.Thread(
        target=_oww_wakeword_listener,
        args=(input_device, _oww_stop_flag),
        daemon=True,
        name="oww-wakeword",
    ).start()
    _thr.Thread(target=_dtmf_listener, daemon=True, name="dtmf-radio").start()

    # Restore sleep state persisted across service restarts (e.g. mic device change)
    if _load_sleep_state():
        _idle_disconnected[0] = True
        log.info("Restored sleep state from disk — waiting for wake signal…")

    session_ref: list = [None]
    start_http_server(http_port, lambda: loop.call_soon_threadsafe(stop_event.set), session_ref)
    log.info("OpenClaw RealTimeTalk daemon starting — silent mode (say 'Hey Jarvis' or 'Five wake up' to activate)")

    while not stop_event.is_set():
        # If sleeping (restored from disk or just auto-slept), wait for wake before connecting
        _woke_from_sleep = False
        if _idle_disconnected[0]:
            log.info("Auto-sleep active — waiting for wake signal…")
            await loop.run_in_executor(None, _wake_event[0].wait)
            _wake_event[0].clear()
            _idle_disconnected[0] = False
            _last_activity[0] = __import__("time").time()
            log.info("Wake signal received — reconnecting to OpenAI…")
            _woke_from_sleep = True
            if stop_event.is_set():
                break

        session = RealtimeSession(
            api_key=openai_key, loop=loop, gw=gw,
            stop_event=stop_event,
            input_device=input_device, alsa_output=alsa_output,
            session_key=session_key,
        )
        if _wake_activate[0]:
            session._active = True
            _wake_activate[0] = False
            log.info("Wake-from-sleep: session started active (HTTP wake)")
        elif _woke_from_sleep:
            log.info("Wake-from-sleep: session started silent (OWW wake) — say 'Five wake up' to activate")
        session_ref[0] = session
        try:
            await session.run()
            log.info("Session ended.")
        except websockets.exceptions.ConnectionClosedError as e:
            log.warning("Realtime connection closed: %s", e)
        except Exception as e:
            log.error("Session error: %s", e)

        if stop_event.is_set():
            break
        if _idle_disconnected[0]:
            # loop back to top — sleep check at start of loop handles it
            continue
        else:
            log.info("Reconnecting in %ds…", RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)

    _oww_stop_flag[0] = True
    if _wake_event[0]:
        _wake_event[0].set()   # unblock any waiting executor
    gw_task.cancel()
    await gw.close()
    log.info("Daemon stopped.")


def calibrate_mic(input_device=None, duration: float = 3.0) -> int:
    """Record ambient noise and return a recommended MIC_GATE_PEAK value (2× noise peak)."""
    print(f"Calibrating mic — measuring ambient noise for {duration:.0f}s. Stay quiet.")
    peaks = []
    def cb(indata, frames, t, s):
        raw = indata[::RESAMPLE_RATIO, 0]
        peaks.append(int(np.max(np.abs(raw))))
    with sd.InputStream(samplerate=DEVICE_RATE, channels=CHANNELS, dtype="int16",
                        blocksize=DEVICE_BLOCKSIZE, callback=cb,
                        device=input_device):
        import time; time.sleep(duration)
    peaks = peaks[2:]  # discard first two frames (hardware warmup)
    noise_peak = max(peaks) if peaks else 0
    recommended = max(MIC_GATE_MIN, min(MIC_GATE_MAX, int(noise_peak * 1.5)))
    print(f"Noise floor peak: {noise_peak}  →  recommended MIC_GATE_PEAK: {recommended} (clamped {MIC_GATE_MIN}–{MIC_GATE_MAX})")
    return recommended


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="OpenClaw RealTimeTalk daemon")
    p.add_argument("--http-port",      type=int, default=DEFAULT_HTTP_PORT,
                   help=f"HTTP toggle port (default {DEFAULT_HTTP_PORT})")
    p.add_argument("--input-source",   type=str, default=None,
                   help="PipeWire source name to use as mic (overrides AGC auto-select)")
    p.add_argument("--input-device",   type=int, default=None,
                   help="sounddevice input device index (see --list-devices)")
    p.add_argument("--alsa-output",    type=str, default=ALSA_OUTPUT,
                   help=f"ALSA output device for TTS playback (default: {ALSA_OUTPUT})")
    p.add_argument("--session-key",    type=str, default=OPENCLAW_SESSION,
                   help=f"OpenClaw session key (default: {OPENCLAW_SESSION})")
    p.add_argument("--mic-gain",       type=float, default=MIC_GAIN,
                   help=f"Software mic gain multiplier (default: {MIC_GAIN})")
    p.add_argument("--mic-gate",       type=int, default=MIC_GATE_PEAK,
                   help=f"Noise gate threshold — pre-gain peak below this → silence (default: {MIC_GATE_PEAK})")
    p.add_argument("--list-devices",   action="store_true",
                   help="Print available audio devices and exit")
    p.add_argument("--calibrate",      action="store_true",
                   help="Measure ambient noise and print recommended --mic-gate value, then exit")
    args = p.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        sys.exit(0)

    if args.calibrate:
        val = calibrate_mic(input_device=args.input_device)
        print(f"\nRun with:  --mic-gate {val}")
        print(f"Or update service:  systemctl --user edit --force openclaw-realtimetalk")
        sys.exit(0)

    MIC_GAIN      = args.mic_gain
    MIC_GATE_PEAK = args.mic_gate
    if args.input_source:
        # User explicitly chose a physical mic — set it as PipeWire default
        # and use direct (non-AGC) gain/gate settings.
        _set_default_source(args.input_source)
        MIC_GAIN      = 6.0
        MIC_GATE_PEAK = max(MIC_GATE_MIN, args.mic_gate)
        log.info("Explicit --input-source %s — direct mode gain=%.1f gate=%d",
                 args.input_source, MIC_GAIN, MIC_GATE_PEAK)
    elif _activate_agc_source():
        MIC_GAIN      = AGC_MIC_GAIN
        MIC_GATE_PEAK = AGC_MIC_GATE
        log.info("WebRTC AGC source active (%s) — adaptive gain/noise on; "
                 "using gain=%.1f gate=%d", AGC_SOURCE_NAME,
                 MIC_GAIN, MIC_GATE_PEAK)
    else:
        log.info("AGC source unavailable — fallback to static gain=%.1f "
                 "gate=%d", MIC_GAIN, MIC_GATE_PEAK)
    _mic_gate_ref[0] = MIC_GATE_PEAK

    # Load per-device calibration store and apply to current default sink
    _load_cal_store()
    _default_sink = subprocess.run(["pactl","get-default-sink"],
                                   capture_output=True,text=True).stdout.strip()
    if _default_sink:
        _known = _apply_device_cal(_default_sink)
        if not _known:
            log.info("Unknown speaker at startup — using minimum safe levels")

    asyncio.run(main(
        args.http_port,
        args.input_device,
        args.alsa_output,
        args.session_key,
    ))
