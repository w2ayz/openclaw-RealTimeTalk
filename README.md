# OpenClaw RealTimeTalk

Headless voice daemon for Raspberry Pi. Captures voice from a USB mic, transcribes it via the
OpenAI Realtime Transcription API, routes the transcript through the local OpenClaw gateway so
Five answers with full memory + tools, then synthesises the reply with Piper TTS and plays it
through a USB speaker or headset. No browser, no display required — designed for always-on deployments.

Runs as a `systemd` user service that starts automatically on boot. Controlled via a web
dashboard (port 19000) accessible from any phone browser on the local network or over Tailscale.

---

## Features

- Voice conversation routed through Five's main OpenClaw session (memory, tools, identity)
- OpenAI Realtime **Transcription** API (`gpt-4o-transcribe`) with server-side VAD
- **WebRTC AGC** — PipeWire virtual mic source applies automatic gain control + noise suppression upstream; daemon falls back to static gain/gate if unavailable
- **Adaptive mic** — no manual gain tuning needed in normal use; AGC normalises quiet USB mics (PCM2902 etc.) automatically
- Mixed-language TTS — English (`en_US-lessac-medium`) and Chinese (`zh_CN-huayan-medium`) rendered per segment; transcribed Chinese normalised to Simplified automatically
- **Language filter** — by default only English and Chinese are shown/processed; other languages (noise hallucinations) silently dropped; toggleable Multi-lang mode
- **Wake confirmation** — when a wake phrase is detected in Silent or Monitoring mode, Five asks "Yes?" before activating; a non-affirmative response or 8-second timeout is logged as a mis-fire and activation is suppressed
- **Speaker calibration** — acoustic sweep from minimum volume; finds the quietest clearly-audible level; works through PipeWire (no direct-ALSA conflict)
- **Headset detection** — auto-detects combined USB headset; switches to manual volume adjustment UI
- **Audio device hot-plug** — detects plug/unplug events, resets to safe volume, restores calibrated levels, announces the change over TTS
- Web dashboard on port **19000** — conversation log, wake/sleep/calibrate/monitor controls
- **Monitoring Only mode** — listen and display transcribed speech without routing to Five (for diagnosing capture quality)
- Boot-order safe — retries gateway connection until OpenClaw is up
- Gateway protocol v4 compatible (OpenClaw 2026.5+)

---

## Architecture

```
Raspberry Pi (headless)
│
├── PipeWire
│       └── rtt_agc_source  (WebRTC AGC virtual mic, loaded from ~/.config/pipewire/pipewire.conf.d/99-rtt-agc.conf)
│               captures C-Media USB mic → applies AGC + noise suppression
│
├── systemd user service: openclaw-realtimetalk
│       starts on boot, retries gateway every 5s until ready
│
└── RealTimeTalk-daemon.py
        │
        ├── OpenClaw gateway ─► ws://127.0.0.1:18789  (protocol v4)
        │   (persistent WS)       chat.send + agent.wait + chat.history
        │                         Five's session: memory, tools, identity
        │                         Model: openai/gpt-5.5 (OAuth, codex harness)
        │
        ├── OpenAI Realtime ──► wss://api.openai.com/v1/realtime?intent=transcription
        │   (transcription)        server VAD + gpt-4o-transcribe
        │                          session.type: "transcription"
        │
        ├── Audio IN ─────────► PipeWire AGC source (rtt_agc_source)
        │                        → static fallback: raw USB mic + 16x gain + gate
        │
        ├── Piper TTS ────────► ~/.local/bin/piper-native/piper
        │                        EN: en_US-lessac-medium  |  ZH: zh_CN-huayan-medium
        │                        mixed-language: split by script, concatenate WAVs
        │
        ├── Audio OUT ────────► paplay to USB sink (PipeWire) or aplay fallback
        │
        └── HTTP :19000 ──────► /dashboard  — conversation log + controls
                                 /wake       — activate voice
                                 /sleep      — silence
                                 /monitor/start  — passive capture display
                                 /monitor/stop
                                 /reset      — clear screen
                                 /multilang  — toggle language filter
                                 /calibrate  — mic calibration
                                 /speaker-cal — speaker calibration
                                 /restart    — restart daemon
```

---

## Signal chain (detailed)

```
You speak
   │
   ▼
USB mic (C-Media PCM2902) ─► PipeWire rtt_agc_source
   │   WebRTC AGC: auto-gain to target level
   │   Noise suppression: ambient noise filtered
   │   High-pass filter + Voice detection
   │
   ▼  sd.InputStream  24 kHz mono int16
┌─────────────── daemon mic callback ─────────────────┐
│  • noise gate: if peak < 60 (AGC mode) → zeros      │
│  • gain 2x trim (AGC already normalised)             │
│  • skip if _busy (Five is speaking)                  │
│  • asyncio.Queue → send to OpenAI                    │
└─────────────────────────────────────────────────────┘
   │
   ▼  ws.send {input_audio_buffer.append, base64 PCM}
wss://api.openai.com/v1/realtime?intent=transcription
   │
   ▼  server-side VAD (threshold 0.3, 1100ms silence to end turn)
gpt-4o-transcribe  →  transcription.completed
   │
   ▼  _handle_transcript()
       zhconv: Traditional Chinese → Simplified
       language gate: drop non-EN/ZH unless multilang mode on
       monitoring mode: log to dashboard, return (no Five)
       wake/sleep/calibrate phrases: handle locally
   │
   ▼  GatewayClient.ask()  →  ws://127.0.0.1:18789  (protocol v4)
       chat.send  →  {ok, runId}
       agent.wait  →  codex harness runs (gpt-5.5, message tool)
       chat final empty (codex delivers via message tool)
       chat.history fallback  →  extract message-tool arguments.message
   │
   ▼  Five's reply text
       zhconv normalise  →  split_by_script (EN/ZH segments)
       strip_markdown()  →  remove bold/links/etc.
       Piper TTS per segment  →  concatenate WAVs
   │
   ▼  paplay --device=<usb-sink>  (PipeWire; no ALSA-busy conflict)
USB speaker / headset
   │
   ▼
You hear Five
```

**Key timing:** ~4–12 s end-to-end — 1100 ms VAD silence window + ~0.5 s transcription + Five thinking + TTS render.

---

## Web Dashboard

Open `http://<pi-ip>:19000/dashboard` from any browser on the local network or over Tailscale.

The dashboard auto-refreshes every 3 s and shows:

- **Status** — ACTIVE / SILENT / MONITORING
- **Audio devices** — current mic, speaker, volume, mic gate, gain
- **Conversation log** — newest entries at top, timestamped, colour-coded (You / Five / Monitor / System)

### Controls

| Link | Action |
|------|--------|
| Wake | Activate voice (same as saying "Five wake up") |
| Sleep | Silence (same as "Five go to sleep") |
| Start Monitor | Enter passive capture-display mode — listens and shows transcribed words, no Five routing |
| Stop Monitor | Exit monitoring mode |
| Reset | Clear the on-screen log |
| Multi-lang: ON/OFF | Toggle language filter (OFF = EN/ZH only, drop noise hallucinations) |
| Calibrate mic | Measure ambient noise and set optimal noise gate |
| Speaker cal | Acoustic sweep to find minimum comfortable speaker volume |
| Restart | Restart the daemon |

### Voice commands

| Say | Effect |
|-----|--------|
| "Five wake up" | Request activation — Five asks "Yes?" for confirmation |
| "Hey Jarvis" | Request activation — Five asks "Yes?" for confirmation |
| "Real Time Talk on" | Request activation — Five asks "Yes?" for confirmation |
| "Yes" / "Yeah" / "OK" / "Sure" | Confirm activation — Five says "I'm listening." |
| "Five wake up" *(second time)* | Also accepted as confirmation |
| "Five go to sleep" | Silence |
| "Real Time Talk off" | Silence |
| "Calibrate mic" / "Calibrate microphone" | Run mic noise calibration |

**Wake confirmation:** When Five is in Silent or Monitoring mode, a wake phrase triggers a confirmation prompt ("Yes?") rather than immediate activation. Five waits up to 8 seconds for an affirmative reply. If no clear "yes" is received the event is logged as a mis-fire and Five stays silent. This prevents accidental activation from radio noise or passing speech. DTMF 123 and the web Wake button bypass confirmation and activate immediately.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Raspberry Pi OS Bookworm | Pi 5 or Pi 4 |
| Python 3.9+ | Pre-installed on Bookworm |
| PipeWire | Default on Bookworm; used for AGC virtual source |
| **OpenClaw gateway running locally** | Required — daemon routes all AI through it |
| OpenClaw 2026.5+ | Gateway protocol v4 required |
| OpenAI API key or OAuth | For STT (Realtime API). OAuth via OpenClaw `openai-codex` provider supported |
| Piper TTS (rhasspy native binary) | `~/.local/bin/piper-native/piper` with EN + ZH voice models |
| espeak-ng | Required for Chinese TTS phonemisation (`apt install espeak-ng`) |
| zhconv | Python package — installed automatically by installer (`pip install zhconv`) |
| USB microphone | C-Media PCM2902 or similar; AGC compensates for quiet hardware |
| Speaker or headset | USB or 3.5mm; headset auto-detected |

### Piper voices

| Language | Model path |
|----------|------------|
| English | `~/.local/share/piper/voices/en_US-lessac-medium/en_US-lessac-medium.onnx` |
| Chinese | `~/.local/share/piper/voices/zh_CN-huayan-medium/zh_CN-huayan-medium.onnx` |

---

## Installation

### 1. Clone

```bash
git clone git@github.com:w2ayz/openclaw-RealTimeTalk.git ~/openclaw-RealTimeTalk
```

### 2. Run the installer

```bash
bash ~/openclaw-RealTimeTalk/RealTimeTalk-install-pi.sh
```

The installer:
1. Creates a Python venv at `~/.local/realtimetalk-venv` and installs deps
2. Installs `libportaudio2` and `espeak-ng` via apt
3. Installs `zhconv` for Simplified Chinese normalisation
4. Writes the PipeWire AGC config to `~/.config/pipewire/pipewire.conf.d/99-rtt-agc.conf`
5. Reloads PipeWire so the AGC source is available immediately
6. Writes `~/.config/systemd/user/openclaw-realtimetalk.service`
7. Enables linger and starts the service

### 3. Check the dashboard

Open `http://<pi-ip>:19000/dashboard` in a browser. The header should show **SILENT**. Say "Five wake up" to activate, then speak normally.

---

## Configuration

### Audio devices

By default the daemon uses:
- **Mic** — PipeWire default source (the WebRTC AGC virtual source `rtt_agc_source` is set as default on startup)
- **Speaker** — found automatically by scanning PipeWire sinks (non-HDMI, non-Bluetooth)

To override the ALSA speaker output:
```bash
# Edit the service ExecStart line:
~/.config/systemd/user/openclaw-realtimetalk.service
# Add: --alsa-output plughw:3,0
systemctl --user daemon-reload && systemctl --user restart openclaw-realtimetalk
```

List ALSA cards:
```bash
aplay -l
```

### WebRTC AGC (adaptive mic)

The daemon loads a PipeWire WebRTC module that creates a virtual mic source (`rtt_agc_source`) with:
- **Automatic Gain Control** — speech normalised to a consistent level regardless of distance or hardware
- **Noise suppression** — ambient noise filtered before transcription
- **High-pass filter + VAD**

This replaces the manual `--mic-gain` / `--mic-gate` values for normal use.

**Fallback:** If `rtt_agc_source` is unavailable at startup, the daemon logs `AGC source unavailable — fallback to static` and uses the raw mic with `--mic-gain 16 --mic-gate 300` (suitable for quiet C-Media adapters).

To force the fallback (e.g. for testing), unload the PipeWire module:
```bash
pactl list short modules | grep echo-cancel   # find module ID
pactl unload-module <id>
systemctl --user restart openclaw-realtimetalk
```

### Mic calibration

If voice capture is choppy or you get noise hallucinations, run mic calibration:
1. Open the dashboard → **Calibrate mic**
2. Keep quiet for 3 seconds while it measures the noise floor
3. The daemon updates the gate and saves it to the service file

Or use Monitoring Only mode to see exactly what the transcriber captures before routing to Five.

### Speaker calibration

The speaker calibration finds the minimum comfortable volume by playing a 440 Hz tone and measuring mic pickup. It starts at PipeWire 1% + software 0.2% and steps up until the mic hears it clearly, then announces the result at a guaranteed-audible level.

- Works through PipeWire (not direct ALSA) — no "device busy" errors
- Detects headsets automatically — switches to manual volume adjustment
- After calibration, all TTS plays at the calibrated level via software attenuation

### Audio device hot-plug

The daemon watches connected audio devices via a PipeWire fingerprint polled every few seconds. When the set of devices changes it:

1. **Resets all PipeWire sinks to 1%** immediately — prevents a newly-connected speaker from blasting at 100%
2. **Restores calibrated levels** for every known sink from the calibration store (after a 0.5 s settle delay)
3. **Announces "Audio devices changed."** via TTS — suppressed when Radio profile is active (won't transmit over the air)
4. **Shows a banner** on the web dashboard for 5 seconds

**Volume applied on device connect:**

| Device state | PipeWire | SW gain |
|---|---|---|
| Known (previously calibrated) | saved value | saved value |
| Unknown / first connect | 1% | 10% |
| Fallback (error) | 25% | 0.70 |

**AIOC (radio interface) plug/unplug:**

- **Plugged in** — saves current mic source, switches AGC to the radio profile (no voice detection, no transient suppression), sets AIOC as PipeWire default sink, applies AIOC calibration
- **Unplugged** — restores previous mic source, switches back to regular mic AGC profile, stops any active AIOC monitor loopback, clears PTT state; serial port number change (`ttyACM0` → `ttyACM1`) handled automatically

**HDMI changes** are silently ignored — display-source connect/disconnect triggers HDMI audio appearance/disappearance but is not a real speaker change and produces no announcement.

**Mic hot-plug:** If the mic stream goes silent for too long (USB mic unplugged or PortAudio cache stale), the daemon reinitialises PortAudio and reopens the stream on the newly enumerated device.

**Default sink preference:**
- Radio profile active → AIOC sink
- No radio → first `Generic_USB2.0` non-HDMI, non-AIOC sink

### Language filter

By default only **English and Chinese** are shown and routed to Five. Other languages (Japanese, Korean, Cyrillic, Arabic, etc. that `gpt-4o-transcribe` hallucinates from noise) are silently dropped.

Toggle from the dashboard: **Multi-lang: OFF → ON** to see all languages (useful for diagnosing capture).

### Chinese (Simplified)

All captured Chinese is automatically normalised from Traditional to Simplified using `zhconv`. TTS automatically selects the Chinese Piper voice for CJK segments and English voice for the rest — you can speak mixed sentences naturally.

### VAD / STT settings

| Parameter | Value | Notes |
|-----------|-------|-------|
| Model | `gpt-4o-transcribe` | Set by `OPENAI_TRANSCRIBE_MODEL` |
| VAD type | `server_vad` | OpenAI server-side |
| VAD threshold | 0.3 | Lower = more sensitive |
| Silence window | 1100 ms | Long enough for natural sentence pauses; shorter values cut sentences mid-phrase with AGC gaps |
| Prefix padding | 300 ms | Lead-in captured before speech detected |

### HTTP port

Default is `19000`. Override:

```bash
~/.local/realtimetalk-venv/bin/python RealTimeTalk-daemon.py --http-port 8080
```

Update `ExecStart` in the service file accordingly, then reload.

### OpenClaw session

Default: `agent:main:main` (Five's primary session). Override with `--session-key`.

### OpenClaw model

The daemon connects to OpenClaw gateway at `ws://127.0.0.1:18789` (protocol v4). Five's model is configured in `~/.openclaw/openclaw.json`:

```json
"agents": {
  "defaults": {
    "model": { "primary": "openai/gpt-5.5" }
  }
}
```

The `openai-codex` OAuth provider (ChatGPT consumer API) is also supported — the daemon extracts replies via `chat.history` since the codex harness delivers via a message tool rather than the chat event content.

---

## File structure

```
RealTimeTalk/
├── README.md                        this file
├── CHANGELOG.md                     version history
├── SKILL.md                         OpenClaw skill descriptor
├── requirements.txt                 Python dependencies
├── RealTimeTalk-daemon.py           main daemon
├── RealTimeTalk-install-pi.sh       one-command Pi deploy
└── RealTimeTalk-toggle.sh           start / stop / status / log / devices
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Gateway connect fails: `protocol mismatch` | OpenClaw updated to v4 protocol | Daemon now negotiates `minProtocol: 4, maxProtocol: 4` — update from v1.6 |
| Five not responding / empty reply | Codex harness delivers reply via message tool, not chat content | v1.7 fetches from `chat.history` as fallback — update daemon |
| Speech cut off after 2–3 words | VAD silence window too short + AGC inter-word gaps look like silence | `silence_duration_ms` raised to 1100 ms in v1.7 |
| Voice activated but transcription is garbage / wrong language | Noise hallucinations | Language filter drops non-EN/ZH by default; check mic gate in dashboard |
| Chinese shows as Traditional characters | Transcriber outputs Traditional | v1.7 normalises to Simplified via zhconv automatically |
| Speaker calibration hangs or takes 30+ seconds | Old per-step parec capture (v1.6) | v1.7 uses fast sd.rec (~6 s total) |
| Speaker calibration sets max volume (PW 60%) | Old "max tone energy" logic always picked loudest step | v1.7 picks minimum clearly-audible step (SNR knee) |
| No audio from speaker after calibration | Calibrated level too low, or wrong sink | Check dashboard device panel; use Manual adjustment on speaker-cal page |
| Dashboard squares/boxes in text | Browser font has no emoji | v1.7 uses plain ASCII throughout |
| `aplay: audio open error: Device or resource busy` | PipeWire holds USB device exclusively | v1.7 plays TTS via paplay through PipeWire; speaker-cal also PipeWire-native |
| AGC source not appearing after reboot | PipeWire config not loaded | Check `~/.config/pipewire/pipewire.conf.d/99-rtt-agc.conf` exists; run `systemctl --user restart pipewire` |
| `speech_started` fires but never `speech_stopped` | Noise floor with gain applied looks like speech | Run Calibrate mic from dashboard |
| Piper produces silence / wrong language | espeak-ng missing for Chinese | `apt install espeak-ng` |
| Service not starting after reboot | Linger not enabled | `loginctl enable-linger $USER` |

---

## License

MIT
