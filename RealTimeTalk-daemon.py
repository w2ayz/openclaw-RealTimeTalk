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
AGENT_TIMEOUT_S   = 45
MIC_GAIN          = 3.0          # headset boom mic is close-talking — 16× was over-amplifying
MIC_GATE_PEAK     = 300          # headset mic is close-talking — lower gate than desk mic
                                 # (lets OpenAI's VAD see real silence between words)
MIC_GATE_MIN      = 300          # calibration clamp — quietest usable room
MIC_GATE_MAX      = 3000         # calibration clamp — above this, use a headset
# WebRTC AGC virtual source (PipeWire module-echo-cancel). When present it
# normalizes speech level + suppresses noise upstream, so the daemon needs
# only a light trim and a minimal gate. Falls back to the static --mic-gain
# / --mic-gate values when the AGC source is unavailable.
AGC_SOURCE_NAME   = "rtt_agc_source"
AGC_MIC_GAIN      = 2.0          # AGC already normalizes; light trim only
AGC_MIC_GATE      = 60           # AGC+NS clean the signal; gate only residual
# Raw C-Media mic — speaker calibration must measure through this directly,
# NOT the AGC source (WebRTC noise-suppression kills the steady 440 Hz test
# tone and AGC distorts level → calibration impossible through AGC).
RAW_MIC_SOURCE    = "alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono"
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
# Speech-interrupt: if the mic sees this many consecutive 50ms blocks above
# the interrupt threshold while Five is speaking, kill TTS immediately.
SPEAK_INTERRUPT_PEAK   = 1200  # raw mic peak to trigger interrupt (AGC speech >> background)
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


def _get_device_status() -> dict:
    """Return current audio device info for the portal status panel."""
    import re as _re3
    result = {"mic": "?", "speaker_alsa": ALSA_OUTPUT, "spk_vol": "?",
              "sw_pct": 100, "gate": 500, "gain": 3.0}
    try:
        # Speaker ALSA from service file
        content = open(SERVICE_FILE).read()
        m = _re3.search(r'--alsa-output (\S+)', content)
        if m:
            result["speaker_alsa"] = m.group(1)
        # Speaker volume: PipeWire first (reflects live adjustments), ALSA as fallback
        sink_id = _find_usb_speaker_sink()
        if sink_id:
            vo = subprocess.run(["pactl", "get-sink-volume", sink_id],
                                 capture_output=True, text=True).stdout
            vm = _re3.search(r'(\d+)%', vo)
            if vm:
                result["spk_vol"] = f"{vm.group(1)}%"
        if result["spk_vol"] == "?":
            # ALSA fallback
            card = _re3.search(r'plughw:(\d+)', result["speaker_alsa"])
            if card:
                amix = subprocess.run(["amixer", "-c", card.group(1), "sget", "PCM"],
                                       capture_output=True, text=True).stdout
                vm = _re3.search(r'\[(\d+)%\]', amix)
                if vm:
                    result["spk_vol"] = f"{vm.group(1)}%"
        # Mic source
        src = _find_always_on_mic_source() or ""
        result["mic"] = src.rsplit(".", 1)[-1][:35] if src else "?"
    except Exception:
        pass
    result["sw_pct"] = int(_cal_sw_volume * 100)
    result["gate"]   = _mic_gate_ref[0]
    result["gain"]   = MIC_GAIN
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
                if ("hdmi" not in name.lower() and "bluez" not in name.lower()
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
                if ("hdmi" not in name.lower() and "bluez" not in name.lower()
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

        # Set PipeWire to the found level for normal use
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

WAKE_PHRASES  = {"five wake up", "5 wake up", "real time talk on", "real-time talk on", "realtimetalk on"}
SLEEP_PHRASES = {"five go to sleep", "5 go to sleep", "real time talk off", "real-time talk off", "realtimetalk off"}

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
    return True

def _normalize(text: str) -> str:
    import string
    t = text.strip().lower()
    t = t.translate(str.maketrans(string.punctuation, " " * len(string.punctuation)))
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

def speak(text: str, alsa_output: str = ALSA_OUTPUT, volume: float = -1.0):
    # volume=-1 means use the calibrated level (_cal_sw_volume); pass explicit 0-1 to override
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
    # Prepend 300ms silence — wakes USB speakers from low-power state gradually
    silence_path = tempfile.mktemp(suffix=".wav")
    import wave as _wave, struct as _struct
    with _wave.open(silence_path, 'wb') as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(PIPER_SAMPLE_RATE)
        wf.writeframes(b'\x00\x00' * int(PIPER_SAMPLE_RATE * 0.3))
    wav_parts: list[str] = [silence_path]
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

        # Play via Popen — interruptible if user speaks mid-sentence.
        # _monitor_and_play polls mic every 50ms; if speech peaks are sustained
        # above the interrupt threshold, it kills aplay immediately.
        mic_peaks_during: list[int] = []
        _interrupted   = [False]
        _aplay_rc      = [0]

        def _monitor_and_play(cmd):
            proc = subprocess.Popen(cmd, stderr=subprocess.DEVNULL)
            consec = 0
            while True:
                try:
                    _aplay_rc[0] = proc.wait(timeout=0.05)
                    break                    # finished normally
                except subprocess.TimeoutExpired:
                    pass
                with _mic_level_lock:
                    p = _mic_level_current[0]
                mic_peaks_during.append(p)
                if p > max(baseline_peak * 15, SPEAK_INTERRUPT_PEAK):
                    consec += 1
                    if consec >= SPEAK_INTERRUPT_BLOCKS:
                        log.info("Speech interrupt — stopping TTS")
                        _interrupted[0] = True
                        try: proc.kill()
                        except Exception: pass
                        break
                else:
                    consec = 0

        _m = _threading.Thread(daemon=True, target=_monitor_and_play,
                               args=(["aplay", "-D", alsa_output, "-q", final_wav],))
        _m.start(); _m.join()

        if not _interrupted[0] and _aplay_rc[0] != 0 and alsa_output != "pulse":
            log.warning("aplay failed on %s, retrying via pulse", alsa_output)
            _m2 = _threading.Thread(daemon=True, target=_monitor_and_play,
                                    args=(["aplay", "-D", "pulse", "-q", final_wav],))
            _m2.start(); _m2.join()

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
                        if parts and "hdmi" not in line.lower() and "bluez" not in line.lower():
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
        # Maps request-id → Future for chat.send acks
        self._send_acks: dict[str, asyncio.Future] = {}
        # Maps runId → Future[str] for final chat replies
        self._reply_futs: dict[str, asyncio.Future] = {}
        # Maps runId → latest assistant-stream text (fallback if chat final empty)
        self._assistant_text: dict[str, str] = {}

    async def connect(self):
        self._ws = await websockets.connect(OPENCLAW_GW_URL)
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
            raise RuntimeError(f"Gateway connect failed: {hello.get('error')}")
        scopes = hello.get("payload", {}).get("auth", {}).get("scopes", [])
        log.info("OpenClaw gateway connected (scopes: %s)", scopes)

    async def listen(self, stop_event: asyncio.Event):
        """Route incoming gateway events to waiting futures. Run as a task."""
        try:
            async for raw in self._ws:
                if stop_event.is_set():
                    break
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

        except websockets.ConnectionClosed:
            pass

    async def ask(self, message: str, session_key: str = OPENCLAW_SESSION) -> str:
        """Send a message to the agent and return its complete reply text."""
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
        if not text:
            await asyncio.sleep(0.6)  # let message-tool result persist
            text = await self._reply_from_history(session_key)
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
                "params": {"sessionKey": session_key, "limit": 8},
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
        self._active      = False             # start silent; wake phrase enables voice
        self._monitoring  = False             # passive capture-only mode (no Five, no TTS)
        self._multilang   = False             # False = only show/process EN/ZH

    def _mic_cb(self, indata, frames, time_info, status):
        raw = indata[::RESAMPLE_RATIO, 0]
        raw_peak = int(np.max(np.abs(raw)))
        with _mic_level_lock:
            _mic_level_current[0] = raw_peak
        # While calibrating, record raw peaks (no gain/gate applied, mic suppression off)
        if self._calibrating:
            self.loop.call_soon_threadsafe(self._cal_peaks.append, raw_peak)
            return
        if self._busy.is_set():
            return  # discard mic input while Five is speaking to prevent feedback
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
        new_gate = max(MIC_GATE_MIN, min(MIC_GATE_MAX, int(noise_peak * 1.25)))
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
            try:
                chunk = await asyncio.wait_for(self._mic_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if self._busy.is_set():
                continue
            await ws.send(json.dumps({
                "type":  "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode(),
            }))

    async def _handle_transcript(self, transcript: str):
        # Default to Simplified Chinese (transcriber often returns Traditional)
        transcript = _to_simplified(transcript)

        # Language gate: by default only English/Chinese are shown/processed.
        # Other languages (often noise hallucinations) are dropped unless the
        # user enables multi-language mode.
        if not self._multilang and not _is_english_or_chinese(transcript):
            log.debug("Dropped non-EN/ZH (multilang off): %r", transcript)
            return

        # Monitoring-only mode: passively log captured segments (no Five/TTS).
        # Shows everything verbatim — including noise — so capture quality is visible.
        if self._monitoring:
            t = transcript.strip()
            if t:
                log.info("Monitor: %s", t)
                _log_entry("monitor", t)
            return

        # Noise hallucination filter: drop consonant-heavy gibberish from background
        # noise that slipped past the VAD. (Monitoring mode is exempt so you can
        # still diagnose what the transcriber produces.)
        if _is_likely_noise(transcript):
            log.debug("Dropped noise hallucination: %r", transcript)
            return

        normalized = transcript.strip().rstrip(".!?,").lower()

        # Wake phrase — always checked regardless of active state
        if _matches_phrase(normalized, WAKE_PHRASES):
            self._busy.set()
            try:
                if not self._active:
                    self._active = True
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

        # All other speech: only route to Five when active
        if not self._active:
            log.debug("Silent mode — ignoring: %s", transcript)
            return

        self._busy.set()
        try:
            log.info("Routing to Five: %s", transcript)
            _log_entry("you", transcript)
            _log_entry("thinking", "Five is thinking...")  # live counter shown on dashboard
            # Prefix tells Five to ignore cron/heartbeat background context
            voice_msg = f"[voice] {transcript}"
            reply = await self.gw.ask(voice_msg, session_key=self.session_key)
            log.info("Five: %s", reply)
            _log_entry("five", reply)
            await asyncio.get_running_loop().run_in_executor(
                None, speak, reply, self.alsa_output
            )
        except asyncio.TimeoutError:
            log.error("OpenClaw agent timed out")
            await asyncio.get_running_loop().run_in_executor(
                None, speak, "Sorry, I timed out on that.", self.alsa_output
            )
        except Exception as e:
            log.error("Error routing transcript: %s", e)
        finally:
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
                if transcript and not self._busy.is_set():
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
                                "threshold":           0.6,   # raised: AGC normalises speech so 0.6 reliably catches voice but ignores background noise
                                "prefix_padding_ms":   300,   # capture sentence lead-in ("Five,…")
                                # WebRTC AGC noise-suppression turns brief
                                # inter-word gaps into hard silence; 600ms ended
                                # turns mid-sentence ("Can you" → rest lost while
                                # busy). 1100ms keeps a full sentence as one turn.
                                "silence_duration_ms": 1100,
                            },
                        },
                    },
                },
            }))
            log.info("Session active — speak now (routed through Five / OpenClaw)")

            in_stream = sd.InputStream(
                samplerate=DEVICE_RATE, channels=CHANNELS, dtype="int16",
                blocksize=DEVICE_BLOCKSIZE, callback=self._mic_cb,
                device=self.input_device,
            )

            with in_stream:
                tasks = [
                    asyncio.create_task(self._send_mic(ws)),
                    asyncio.create_task(self._recv_ws(ws)),
                    asyncio.create_task(self.stop_event.wait()),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()

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
                if sess and not sess._active:
                    sess._active = True
                    log.info("HTTP wake")
                self.send_response(302)
                self.send_header("Location", "/log")
                self.end_headers()
            elif self.path == "/sleep":
                if sess and sess._active:
                    sess._active = False
                    log.info("HTTP sleep")
                self.send_response(302)
                self.send_header("Location", "/log")
                self.end_headers()
            elif self.path in ("/monitor", "/monitor/start", "/monitor/stop"):
                # Passive capture-only monitoring (no Five, no TTS).
                # /monitor toggles; /monitor/start and /monitor/stop are explicit.
                if sess:
                    if self.path == "/monitor/start":
                        new_state = True
                    elif self.path == "/monitor/stop":
                        new_state = False
                    else:
                        new_state = not sess._monitoring
                    if new_state and not sess._monitoring:
                        sess._monitoring = True
                        sess._active = False  # ensure fully silent
                        log.info("HTTP monitor START — capture-only")
                        _log_entry("system", "Monitoring only - capture display, silent")
                    elif not new_state and sess._monitoring:
                        sess._monitoring = False
                        log.info("HTTP monitor STOP")
                        _log_entry("system", "Monitoring stopped")
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path == "/multilang":
                # Toggle multi-language mode. Off (default) = only English/
                # Chinese shown/processed; other languages dropped.
                if sess:
                    sess._multilang = not sess._multilang
                    state_txt = "ON (all languages)" if sess._multilang else "OFF (EN/ZH only)"
                    log.info("HTTP multilang %s", state_txt)
                    _log_entry("system", f"Multi-language mode: {state_txt}")
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
            elif self.path == "/calibrate":
                gate = _mic_gate_ref[0]
                body = f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mic Calibration</title>
<style>
body{{font-family:sans-serif;font-size:17px;background:#111;color:#eee;padding:16px;}}
h3{{margin:0 0 12px;}}
#wrap{{width:100%;max-width:500px;}}
canvas{{width:100%;height:44px;border-radius:6px;display:block;}}
#info{{font-size:16px;color:#aaa;margin:8px 0;min-height:20px;}}
#result{{margin-top:12px;padding:10px;background:#1a3a1a;border-radius:6px;font-size:15px;color:#7f7;display:none;}}
#countdown{{font-size:13px;color:#aaa;margin-top:4px;}}
.btnrow{{margin-top:14px;display:flex;gap:10px;}}
button{{padding:12px 26px;border:none;color:#fff;border-radius:8px;font-size:17px;cursor:pointer;}}
#btn{{background:#2a5;}} #btn:disabled{{background:#555;cursor:default;}}
#backbtn{{background:#335;}}
</style></head><body>
<h3>Mic Calibration</h3>
<div id="wrap">
  <canvas id="meter" height="44"></canvas>
  <div id="info">Stay quiet to see noise floor. Yellow line = current gate ({gate}).</div>
  <div id="result"></div>
  <div class="btnrow">
    <button id="btn" onclick="startCal()">Calibrate (3 sec quiet)</button>
    <button id="backbtn" onclick="location.href='/dashboard'">← Back to log</button>
  </div>
</div>
<script>
const MAX = 32768, gate0 = {gate};
let calRunning = false;
const canvas = document.getElementById('meter');
const ctx = canvas.getContext('2d');
const info  = document.getElementById('info');
const result= document.getElementById('result');
const btn   = document.getElementById('btn');

const grad = (w) => {{
  const g = ctx.createLinearGradient(0,0,w,0);
  g.addColorStop(0,    '#1155cc');
  g.addColorStop(0.35, '#22bb55');
  g.addColorStop(0.75, '#cc4411');
  return g;
}};

function draw(peak, gateVal){{
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='#222'; ctx.fillRect(0,0,W,H);
  const ratio = Math.min(peak/MAX,1);
  ctx.fillStyle = grad(W);
  ctx.fillRect(0,0,W*ratio,H);
  // gate line
  const gx = Math.min((gateVal/MAX)*W, W-2);
  ctx.strokeStyle='#ffee00'; ctx.lineWidth=2;
  ctx.beginPath(); ctx.moveTo(gx,0); ctx.lineTo(gx,H); ctx.stroke();
  ctx.fillStyle='#eee'; ctx.font='11px monospace';
  ctx.fillText('peak:'+peak+'  gate:'+gateVal, 6, H-6);
}}

let currentGate = gate0;
const es = new EventSource('/levels');
es.onmessage = e => {{
  const [peak, gate] = e.data.split(',').map(Number);
  currentGate = gate;
  draw(peak, gate);
  if(!calRunning) info.textContent =
    peak < gate ? 'Below gate (noise floor)' :
    peak < MAX*0.5 ? '🟢 Speech range' : '🔴 Very loud';
}};

function startCal(){{
  calRunning = true;
  btn.disabled = true;
  let secs = 3;
  info.textContent = 'Stay quiet… ' + secs + 's';
  const t = setInterval(()=>{{ secs--; info.textContent = secs>0 ? 'Stay quiet… '+secs+'s' : 'Measuring…'; }}, 1000);
  fetch('/calibrate/run').then(r=>r.json()).then(d=>{{
    clearInterval(t);
    calRunning = false;
    result.style.display='block';
    result.innerHTML = 'Done! New gate: <b>' + d.gate + '</b> &nbsp;(noise peak was ' + d.noise_peak + ')<br><small>Returning to log in 3 seconds…</small>';
    info.textContent = 'Yellow line updated.';
    btn.disabled = false;
    setTimeout(()=>{{ es.close(); location.href='/dashboard'; }}, 5000);
  }}).catch(()=>{{ clearInterval(t); calRunning=false; btn.disabled=false;
    info.textContent='Calibration failed — try again.'; }});
}}
</script></body></html>"""
                _html(self, 200, body)

            elif self.path == "/calibrate/run":
                if sess:
                    import asyncio as _aio, json as _json, time as _time
                    # collect 3s of mic samples (audio thread already fills _mic_level_current)
                    peaks = []
                    for _ in range(30):
                        _time.sleep(0.1)
                        with _mic_level_lock:
                            peaks.append(_mic_level_current[0])
                    peaks = peaks[2:]
                    noise_peak = max(peaks) if peaks else 0
                    new_gate = max(MIC_GATE_MIN, min(MIC_GATE_MAX, int(noise_peak * 1.25)))
                    _mic_gate_ref[0] = new_gate
                    MIC_GATE_PEAK = new_gate
                    log.info("HTTP calibration: noise_peak=%d → gate=%d", noise_peak, new_gate)
                    _update_service_gate(new_gate)
                    # speak confirmation in background thread (we're already in HTTP thread)
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
                else:
                    _html(self, 503, "<h2>No active session</h2>")

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
<p><a href="/dashboard">← Dashboard</a></p>
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
                                'margin-bottom:6px;">No microphone detected - connect mic and recalibrate.</div>'
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
.bAdj{{background:#335;}} .bPlay{{background:#226;}}
.bStop{{background:#622;}} .bSet{{background:#a62;}}
a{{color:#7af;}}</style></head><body>
<h3>Speaker Calibration</h3>
<div class="info">Speaker: {ds["speaker_alsa"]}</div>
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
      (d.status=='no_mic' ? 'No mic detected - connect microphone first.' :
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
                if sess:
                    import threading as _t
                    sw  = result.get("safe_sw_vol", _cal_sw_volume)
                    pw  = result.get("safe_vol", CAL_FALLBACK_PW)
                    snk = _find_usb_speaker_sink()
                    def _cal_announce(sw=sw, pw=pw, snk=snk,
                                      alsa=sess.alsa_output,
                                      st=result.get("status", "ok")):
                        if st == "no_mic":
                            msg = ("Calibration finished. The mic could not hear "
                                   "the speaker, so I set a usable default volume. "
                                   "Use Manual adjustment to fine-tune.")
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
                # Headset mode: start looping test speech
                _headset_cal_loop[0] = True
                alsa = sess.alsa_output if sess else ALSA_OUTPUT
                def _loop(alsa=alsa):
                    while _headset_cal_loop[0]:
                        speak("This is a headset volume test. Adjust until comfortable.", alsa)
                        import time as _tl; _tl.sleep(0.5)
                import threading as _t2
                _t2.Thread(target=_loop, daemon=True).start()
                _html(self, 200, "<p>Loop started.</p>")

            elif self.path == "/speaker-cal/loop-stop":
                _headset_cal_loop[0] = False
                _html(self, 200, "<p>Loop stopped.</p>")

            elif self.path.startswith("/speaker-cal/adjust"):
                import json as _json, re as _re5, urllib.parse as _up
                qs = _up.parse_qs(_up.urlparse(self.path).query)
                delta = int(qs.get("delta", ["0"])[0])
                sink = _find_usb_speaker_sink()
                if sink:
                    # Get current volume
                    cur_out = subprocess.run(["pactl", "get-sink-volume", sink],
                                             capture_output=True, text=True).stdout
                    m = _re5.search(r'(\d+)%', cur_out)
                    cur = int(m.group(1)) if m else 50
                    new_vol = max(1, min(100, cur + delta))
                    subprocess.run(["pactl", "set-sink-volume", sink, f"{new_vol}%"],
                                   capture_output=True)
                resp = _json.dumps(_get_device_status()).encode()
                self.send_response(200); self.send_header("Content-Type","application/json")
                self.send_header("Content-Length", str(len(resp))); self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/speaker-cal/vol":
                import json as _json
                resp = _json.dumps(_get_device_status()).encode()
                self.send_response(200); self.send_header("Content-Type","application/json")
                self.send_header("Content-Length", str(len(resp))); self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/speaker-cal/set":
                # Headset mode: save current PipeWire level as calibrated
                import json as _json, re as _re6
                _headset_cal_loop[0] = False
                ds = _get_device_status()
                # Persist to service file
                _update_service_alsa_output(ds["speaker_alsa"])
                sink = _find_usb_speaker_sink()
                if sink:
                    cur_out = subprocess.run(["pactl", "get-sink-volume", sink],
                                             capture_output=True, text=True).stdout
                    m = _re6.search(r'(\d+)%', cur_out)
                    pw = int(m.group(1)) if m else 50
                    _update_service_gate(pw)   # reuse gate update pattern for mic-gate; actually store vol
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
                    _audio_fingerprint[0] = new_fp
                    msg = "Audio devices changed. Please recalibrate the mic."
                    _device_change_msg[0] = msg
                    log.info("Device change detected on /log refresh")
                    if sess:
                        import threading as _t
                        def _announce_change():
                            _safe_volume_new_sinks(1)   # PipeWire at 1%
                            import time as _time; _time.sleep(0.5)
                            # Extra software attenuation on top: 1% × 5% = 0.05% of full scale
                            speak(msg, sess.alsa_output, volume=NEW_DEVICE_VOLUME)
                        _t.Thread(target=_announce_change, daemon=True).start()

                if _device_change_msg[0]:
                    device_banner = (
                        f'<div id="dbanner" style="background:#5a2200;border-radius:8px;'
                        f'padding:10px;margin-bottom:8px;font-weight:bold;">'
                        f'{_device_change_msg[0]}</div>'
                        f'<script>setTimeout(()=>{{var b=document.getElementById("dbanner");'
                        f'if(b)b.remove();}},5000);</script>'
                    )
                    _device_change_msg[0] = ""
                else:
                    device_banner = (
                        f'<div id="dbanner" style="background:#1a3a1a;border-radius:8px;'
                        f'padding:8px;margin-bottom:8px;color:#5f5;font-size:13px;">'
                        f'No device change detected.</div>'
                        f'<script>setTimeout(()=>{{var b=document.getElementById("dbanner");'
                        f'if(b)b.remove();}},5000);</script>'
                    )

                active = sess._active if sess else False
                monitoring = sess._monitoring if sess else False
                multilang  = sess._multilang if sess else False
                # Pre-compute how long each "thinking" entry waited for a Five reply.
                # None = still thinking (show live counter); float = seconds taken (show static).
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
                            thinking_dur[_ep] = None  # still waiting

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
                            # Still waiting — live counter
                            rows += (f'<div class="thinking">{ts_span}'
                                     f'Five is thinking... '
                                     f'<span class="tctr" data-start="{ep:.3f}">0</span>s</div>')
                        # else: Five replied — hide this line entirely
                    else:
                        rows += f'<div class="sys">{ts_span}{e["text"]}</div>'
                # All device info gathered outside do_GET to avoid UnboundLocalError scoping
                _ds = _get_device_status()
                device_panel = (
                    f'<div style="background:#1a1a2a;border-radius:8px;padding:8px 12px;'
                    f'margin-bottom:8px;font-size:12px;color:#aaa;line-height:1.8;">'
                    f'<b style="color:#eee">Audio devices</b><br>'
                    f'Mic: {_ds["mic"]}<br>'
                    f'Speaker: {_ds["speaker_alsa"]} &nbsp;|&nbsp; '
                    f'Vol {_ds["spk_vol"]} &nbsp;|&nbsp; SW {_ds["sw_pct"]}%<br>'
                    f'Mic gate: {_ds["gate"]} &nbsp;|&nbsp; Gain: {_ds["gain"]}x'
                    f'</div>'
                )

                state = ("MONITORING" if monitoring
                         else "ACTIVE" if active else "SILENT")
                body = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="3">
<title>RealTimeTalk Dashboard</title>
<style>
html,body{{height:100%;margin:0;}}
body{{font-family:sans-serif;font-size:17px;background:#111;color:#eee;display:flex;flex-direction:column;}}
#top{{padding:12px 12px 0;flex-shrink:0;}}
#log{{flex:1;overflow-y:auto;padding:0 12px 12px;}}
.you{{background:#1a3a1a;border-radius:8px;padding:8px;margin:6px 0;}}
.five{{background:#1a2a3a;border-radius:8px;padding:8px;margin:6px 0;}}
.mon{{background:#2a2030;border-left:3px solid #b58;border-radius:6px;padding:8px;margin:6px 0;}}
.sys{{color:#888;font-size:0.85em;text-align:center;margin:4px 0;}}
.thinking{{color:#f90;background:#1a1400;border-left:3px solid #f90;border-radius:6px;padding:8px;margin:6px 0;font-style:italic;}}
.ts{{color:#666;font-size:0.8em;font-family:monospace;}}
h3{{margin:0 0 10px;}}
a{{color:#7af;margin-right:14px;font-size:17px;}}
a.on{{color:#5f5;font-weight:bold;}}
a.reset{{color:#f86;}}
</style></head><body>
<div id="top">
<h3>RealTimeTalk Dashboard — {state}</h3>
<a href="/wake">Wake</a><a href="/sleep">Sleep</a><a href="/monitor/start" class="{'on' if monitoring else ''}">Start Monitor</a><a href="/monitor/stop" class="{'' if monitoring else 'on'}">Stop Monitor</a><a href="/multilang" class="{'on' if multilang else ''}">Multi-lang: {'ON' if multilang else 'OFF'}</a><a href="/reset" class="reset">Reset</a><a href="/calibrate">Calibrate mic</a><a href="/speaker-cal">Speaker cal</a><a href="/restart">Restart</a><a href="/dashboard">Dashboard</a>
<hr>{device_panel}{device_banner}</div>
<div id="log">{rows if rows else "<div class='sys'>No conversation yet</div>"}</div>
<script>
setInterval(function(){{
  var now=Date.now()/1000;
  document.querySelectorAll('.tctr').forEach(function(el){{
    el.textContent=Math.max(0,Math.floor(now-parseFloat(el.dataset.start)));
  }});
}},500);
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

# ── Main ──────────────────────────────────────────────────────────────────────

async def main(http_port: int, input_device=None, alsa_output: str = ALSA_OUTPUT,
               session_key: str = OPENCLAW_SESSION):
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

    session_ref: list = [None]
    start_http_server(http_port, lambda: loop.call_soon_threadsafe(stop_event.set), session_ref)
    log.info("OpenClaw RealTimeTalk daemon starting — silent mode (say 'Five wake up' to activate)")

    while not stop_event.is_set():
        session = RealtimeSession(
            api_key=openai_key, loop=loop, gw=gw,
            stop_event=stop_event,
            input_device=input_device, alsa_output=alsa_output,
            session_key=session_key,
        )
        session_ref[0] = session
        try:
            await session.run()
            log.info("Session ended.")
        except websockets.exceptions.ConnectionClosedError as e:
            log.warning("Realtime connection closed: %s", e)
        except Exception as e:
            log.error("Session error: %s", e)

        if not stop_event.is_set():
            log.info("Reconnecting in %ds…", RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)

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
    recommended = max(MIC_GATE_MIN, min(MIC_GATE_MAX, int(noise_peak * 1.25)))
    print(f"Noise floor peak: {noise_peak}  →  recommended MIC_GATE_PEAK: {recommended} (clamped {MIC_GATE_MIN}–{MIC_GATE_MAX})")
    return recommended


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="OpenClaw RealTimeTalk daemon")
    p.add_argument("--http-port",      type=int, default=DEFAULT_HTTP_PORT,
                   help=f"HTTP toggle port (default {DEFAULT_HTTP_PORT})")
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
    if _activate_agc_source():
        MIC_GAIN      = AGC_MIC_GAIN
        MIC_GATE_PEAK = AGC_MIC_GATE
        log.info("WebRTC AGC source active (%s) — adaptive gain/noise on; "
                 "using gain=%.1f gate=%d", AGC_SOURCE_NAME,
                 MIC_GAIN, MIC_GATE_PEAK)
    else:
        log.info("AGC source unavailable — fallback to static gain=%.1f "
                 "gate=%d", MIC_GAIN, MIC_GATE_PEAK)
    _mic_gate_ref[0] = MIC_GATE_PEAK

    asyncio.run(main(
        args.http_port,
        args.input_device,
        args.alsa_output,
        args.session_key,
    ))
