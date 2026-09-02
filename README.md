# OpenClaw RealTimeTalk

Headless voice daemon for Raspberry Pi. Captures voice from a USB mic, transcribes it via the
OpenAI Realtime Transcription API, routes the transcript through the local OpenClaw gateway so
the AI Agent answers with full memory + tools, then synthesises the reply (Piper TTS for English;
ElevenLabs → Edge TTS → OpenAI TTS → Piper for Chinese/mixed) and plays it through a USB speaker or
headset. No browser, no display required — designed for always-on deployments.

Runs as a `systemd` user service that starts automatically on boot. Controlled via a web
dashboard (port 19000) accessible from any phone browser on the local network or over Tailscale.

---

## Features

- Voice conversation routed through the AI Agent's main OpenClaw session (memory, tools, identity)
- OpenAI Realtime **Transcription** API (`gpt-4o-transcribe`) with server-side VAD
- **WebRTC AGC** — PipeWire virtual mic source applies automatic gain control + noise suppression upstream; daemon falls back to static gain/gate if unavailable
- **Adaptive mic** — no manual gain tuning needed in normal use; AGC normalises quiet USB mics (PCM2902 etc.) automatically
- Mixed-language TTS — English (`en_US-lessac-medium`) and Chinese (`zh_CN-huayan-medium`) rendered per segment; transcribed Chinese normalised to Simplified automatically
- **Language filter** — by default only English and Chinese are shown/processed; other languages (noise hallucinations) silently dropped; toggleable Multi-lang mode
- **Wake confirmation** — when a wake phrase is detected in Silent or Monitoring mode, the AI Agent asks "Yes?" before activating; a non-affirmative response or 8-second timeout is logged as a mis-fire and activation is suppressed
- **Speaker calibration** — acoustic sweep from minimum volume; finds the quietest clearly-audible level; works through PipeWire (no direct-ALSA conflict)
- **Headset detection** — auto-detects combined USB headset; switches to manual volume adjustment UI
- **Audio device hot-plug** — detects plug/unplug events, resets to safe volume, restores calibrated levels, announces the change over TTS
- Web dashboard on port **19000** — conversation log, wake/sleep/calibrate/monitor controls
- **Monitoring Only mode** — listen and display transcribed speech without routing to the AI Agent (for diagnosing capture quality)
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
        │                         AI Agent's session: memory, tools, identity
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
│  • skip if _busy (AI Agent is speaking)              │
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
       monitoring mode: log to dashboard, return (no AI Agent)
       wake/sleep/calibrate phrases: handle locally
   │
   ▼  GatewayClient.ask()  →  ws://127.0.0.1:18789  (protocol v4)
       chat.send  →  {ok, runId}
       agent.wait  →  codex harness runs (gpt-5.5, message tool)
       chat final empty (codex delivers via message tool)
       chat.history fallback  →  extract message-tool arguments.message
   │
   ▼  AI Agent's reply text
       zhconv normalise  →  split_by_script (EN/ZH segments)
       strip_markdown()  →  remove bold/links/etc.
       Chinese/mixed:  ElevenLabs → Edge TTS → OpenAI TTS → Piper
       English:        Piper TTS per segment
       →  concatenate WAVs
   │
   ▼  paplay --device=<usb-sink>  (PipeWire; no ALSA-busy conflict)
USB speaker / headset
   │
   ▼
You hear the AI Agent
```

**Key timing:** ~4–12 s end-to-end — 1100 ms VAD silence window + ~0.5 s transcription + AI Agent thinking + TTS render.

---

## Web Dashboard

Open `http://<pi-ip>:19000/dashboard` from any browser on the local network or over Tailscale.

The dashboard auto-refreshes every 3 s and shows:

- **Status** — ACTIVE / SILENT / MONITORING
- **Audio devices** — current mic, speaker, volume, mic gate, gain
- **Conversation log** — newest entries at top, timestamped, colour-coded (You / Agent / Monitor / System)

### Controls

| Link | Action |
|------|--------|
| Wake | Activate voice (same as saying "AI Agent wake up") |
| Sleep | Silence (same as "AI Agent go to sleep") |
| Start Monitor | Enter passive capture-display mode — listens and shows transcribed words, no AI Agent routing |
| Stop Monitor | Exit monitoring mode |
| Reset | Clear the on-screen log |
| Multi-lang: ON/OFF | Toggle language filter (OFF = EN/ZH only, drop noise hallucinations) |
| Calibrate mic | Measure ambient noise and set optimal noise gate |
| Speaker cal | Acoustic sweep to find minimum comfortable speaker volume |
| Owner Only / Everyone | Toggle owner-only mode — only the enrolled voice is obeyed. **Auto-disabled while Radio mode is on** (voice verification is unreliable over radio audio) and restored once radio mode turns back off; manually re-enabling it while radio mode is active is refused. |
| Restart | Restart the daemon |

Voice ID (enrollment / test page) lives on the **Calibrate** page, as the **first** button in the Radio/Monitor/Playback/DTMF group.

### Pushing text from OpenClaw (or any local process)

`GET http://localhost:19000/speak?text=...` lets any process on the same
machine make the daemon read text aloud on demand — the piece that lets an
OpenClaw agent do work triggered by keyboard/text (not voice) and still
deliver the result through RTT. Useful when the request was typed but the
answer should come back spoken — away from the keyboard, on the radio,
hands busy, etc.

```bash
curl "http://localhost:19000/speak?text=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "Your text here")"
# → {"ok": true, "queued": true, "chars": N}
```

- **Local-only** — rejects anything not from `127.0.0.1`/`::1`/`localhost`.
- **GET, URL-encoded query string** — fine for a spoken-length summary;
  don't push a raw multi-page report through it, summarize first.
- Text runs through the normal `speak()` pipeline (markdown stripped, TTS
  engine as configured) and plays on whatever output device is currently
  selected — including transmitting on-air if Radio Mode is active. The
  line is also logged into the dashboard's conversation history like any
  other reply.

**Wiring it up in OpenClaw:** add a note to the agent's `TOOLS.md` (in its
OpenClaw workspace directory — `agents.defaults.workspace` in
`~/.openclaw/openclaw.json`, often `~/.openclaw/workspace/` but can be the
agent's home directory itself) so it knows the capability exists and when
to reach for it — it won't discover the endpoint on its own. Something
like:

```markdown
### RealTimeTalk — push text to be read aloud

If <you> asks for something via keyboard/text (not voice) and wants the
result spoken through RealTimeTalk once it's ready — e.g. "look into X and
read me what you find" typed instead of said — call this instead of just
replying in text:

​```bash
curl "http://localhost:19000/speak?text=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "YOUR TEXT HERE")"
​```

- Local-only, GET, URL-encode the text, keep it to a spoken-length summary.
- Success looks like `{"ok": true, "queued": true, "chars": N}`.
- Only use this when RTT is the actual delivery channel wanted — not as a
  substitute for normal chat replies.
```

Since OpenClaw's `AGENTS.md` convention is to read the workspace fresh each
session, this takes effect on the next session with no daemon restart
required.

### Voice commands

> The agent name is configurable (`--agent-name`, default **Zeebot**) — see
> [Deployment.md §5](Deployment.md#5-agent-name--wake-phrase). This table
> uses **AI Agent** as a generic placeholder for whatever name you configure;
> substitute your own.

| Say | Effect |
|-----|--------|
| "AI Agent wake up" | Request activation — the AI Agent asks "Yes?" for confirmation |
| "Hey Jarvis" | Request activation — the AI Agent asks "Yes?" for confirmation |
| "Real Time Talk on" | Request activation — the AI Agent asks "Yes?" for confirmation |
| "Yes" / "Yeah" / "OK" / "Sure" | Confirm activation — the AI Agent says "I'm listening." |
| "AI Agent wake up" *(second time)* | Also accepted as confirmation |
| "AI Agent go to sleep" | Silence |
| "Real Time Talk off" | Silence |
| "Calibrate mic" / "Calibrate microphone" | Run mic noise calibration |

**Wake confirmation:** When the AI Agent is in Silent or Monitoring mode, a wake phrase triggers a confirmation prompt ("Yes?") rather than immediate activation. The AI Agent waits up to 8 seconds for an affirmative reply. If no clear "yes" is received the event is logged as a mis-fire and the AI Agent stays silent. This prevents accidental activation from radio noise or passing speech. DTMF 123 and the web Wake button bypass confirmation and activate immediately.

---

## Speaker verification (owner-only mode)

When enabled, the AI Agent only acts on the enrolled owner's voice — every transcript's audio segment is embedded with a bilingual (EN/ZH) speaker-recognition model and compared against the enrolled profile by cosine similarity. Non-matching speech is silently ignored and logged to the dashboard with its similarity score.

### Setup

`RealTimeTalk-install-pi.sh` installs `sherpa-onnx` and downloads the 3D-Speaker CAM++ zh-en model (~28 MB) automatically — no manual steps needed. Just restart the service after installing, then enroll:

```bash
systemctl --user restart openclaw-realtimetalk
```

Then open `http://<pi-ip>:19000/voice-enroll` to enroll.

If you're setting this up outside the installer (or need to re-fetch the model by hand):

```bash
~/.local/realtimetalk-venv/bin/pip install sherpa-onnx
mkdir -p ~/.local/share/rtt/speaker
wget -O ~/.local/share/rtt/speaker/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx
# (the release tag really is spelled "recongition" upstream)
```

### Enrollment

Open `/voice-enroll` and record the three 5-second samples (English, Chinese, free speech) **on the Pi's own mic** — this keeps the audio channel identical to runtime. Save, then use **Test my voice** to check your similarity score (expect ≥ ~0.6 for yourself, ≤ ~0.4 for others). Enable **Owner Only** from the dashboard or by saying "only listen to me".

### Voice commands

| Say | Effect |
|-----|--------|
| "Only listen to me" / "只听我的" | Enable owner-only mode (requires enrolled profile) |
| "Listen to everyone" / "听大家的" | Disable owner-only mode |

In owner-only mode **everything** — wake phrases, sleep, monitor toggles, and the mode toggles themselves — requires the owner's voice.

### Tuning

- Threshold defaults to **0.50** cosine similarity; adjust live with `/ownermode/threshold?value=0.55` or at startup with `--spk-threshold`. Every pass/reject is logged with its score (`journalctl --user -u openclaw-realtimetalk | grep "Voice check"`).
- Segments shorter than ~0.8 s can't be verified and are ignored in owner-only mode — prefer "yes please" over a bare "yes" for wake confirmation.

### Known limitations

- The web dashboard buttons and radio DTMF sequences bypass verification by design (local-network fallback).
- The "Hey Jarvis" deep-sleep wake word is not speaker-verified, but it only reconnects into Silent mode — actual activation still requires the owner's verified voice.
- Verification resists other *people*, not a **recording** of the owner's voice (replay attack) — this is out of scope.
- If the profile, model, or library is missing, the daemon accepts all speakers and the dashboard shows an amber warning.

---

## Prerequisites

Everything below except the OpenClaw gateway itself and the OpenAI API key is installed automatically by `RealTimeTalk-install-pi.sh` (see [Installation](#installation)) — listed here for reference, or for setting up outside the installer.

| Requirement | Notes |
|-------------|-------|
| Raspberry Pi OS Bookworm/Trixie | Pi 5 or Pi 4; aarch64 or x86_64 (Piper binary auto-detects architecture) |
| Python 3.9+ | Pre-installed |
| PipeWire + `pipewire-alsa` | `pipewire-alsa` is required, not just PipeWire itself — without it, ALSA's "default" device can't resample to the 24kHz/16kHz rates the Realtime API and openwakeword need, and raw hardware access conflicts with PipeWire's exclusive hold on USB audio devices |
| `pulseaudio-utils` (`pactl`) | Used throughout for PipeWire sink/source control |
| **OpenClaw gateway running locally** | Required — daemon routes all AI through it. Not installed by this script |
| OpenClaw 2026.5+ | Gateway protocol v4 required |
| OpenAI API key | Installer prompts for it (hidden input) if `talk.providers.openai.apiKey` isn't already set in `~/.openclaw/openclaw.json`. OAuth via OpenClaw `openai-codex` provider also supported |
| Piper TTS (rhasspy native binary) | `~/.local/bin/piper-native/piper` with EN + ZH voice models |
| espeak-ng | Required for Chinese TTS phonemisation |
| `mpg123` | Decodes the Edge TTS skill's MP3 output to WAV (installer adds it) |
| edge-tts skill + Node.js (optional) | Network TTS tier between ElevenLabs and OpenAI for Chinese/mixed replies — install at `~/.openclaw/workspace/skills/edge-tts/`; installer resolves it and runs `npm install` |
| `fonts-noto-color-emoji` | Dashboard's owner/everyone button icon (👤) needs a color-emoji font or it renders as a blank box |
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

Safe to re-run any time (e.g. after `git pull`) — every step checks first and skips what's already installed. The installer:
1. Installs system packages via apt: `libportaudio2`, `pulseaudio-utils`, `pipewire-alsa`, `espeak-ng`, `fonts-noto-color-emoji`, `mpg123`
2. Creates a Python venv at `~/.local/realtimetalk-venv` and installs all of `requirements.txt`
3. Downloads the Piper native binary + English/Chinese voice models (architecture-detected); resolves the optional edge-tts skill (sibling dir → `$OPENCLAW_WORKSPACE` → official path), installs Node.js + its `npm` deps if the skill is present, and records the path in the systemd unit as `RTT_EDGE_TTS_SCRIPT` — warns and continues if the skill is absent
4. Downloads the CAM++ speaker-verification model
5. Prompts (hidden input) for an OpenAI API key if `talk.providers.openai.apiKey` isn't already set
6. Lists detected audio devices for reference — no manual device index needed; the daemon follows PipeWire's own default source/sink, which you can change from the dashboard
7. Writes `~/.config/systemd/user/openclaw-realtimetalk.service`
8. Enables linger and starts the service

### 3. Check the dashboard

Open `http://<pi-ip>:19000/dashboard` in a browser. The header should show **SILENT**. Say "AI Agent wake up" to activate, then speak normally.

---

## Configuration

### Audio devices

By default the daemon uses:
- **Mic** — PipeWire default source (the WebRTC AGC virtual source `rtt_agc_source` is set as default on startup)
- **Speaker** — found automatically by scanning PipeWire sinks (non-HDMI, non-Bluetooth)

To override the ALSA speaker or mic device:
```bash
# Edit the service ExecStart line:
~/.config/systemd/user/openclaw-realtimetalk.service
# Defaults set by the installer: --input-device pipewire --alsa-output default
```
Prefer a device **name** (e.g. `pipewire`, or a specific PipeWire sink/source name from
`--list-devices`) over a raw numeric index — numeric indices shift whenever a USB audio
device is plugged or unplugged, silently pointing the daemon at the wrong hardware.
```bash
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

Or use Monitoring Only mode to see exactly what the transcriber captures before routing to the AI Agent.

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

- **Plugged in** — saves current mic source, switches AGC to the radio profile (no voice detection, no transient suppression), sets AIOC as PipeWire default sink, applies AIOC calibration, sets the AIOC's PipeWire source volume to `AIOC_SOURCE_VOLUME_PCT` (80% — kept at its original level; a boost to 130% was tried and reverted in v3.5.1 because it pushed the idle noise floor above the squelch threshold used by Playback and the DTMF listener, permanently defeating transmission detection)
- **Unplugged** — restores previous mic source, switches back to regular mic AGC profile, stops any active AIOC monitor loopback, clears PTT state; serial port number change (`ttyACM0` → `ttyACM1`) handled automatically

**Monitor vs. Playback (Calibrate page):** both work off the AIOC's RX audio, but differently —
- **Monitor** is a live PipeWire loopback (`module-loopback`, ~20ms latency) straight to a speaker you pick from the Audio Devices table. Real-time, but only as clean as the raw radio audio.
- **Playback** detects an actual transmission (carrier-operated squelch: raw peak > `DTMF_COS_THRESHOLD` with a hangover tail — the same technique the DTMF listener already uses), records it, and **transmits it back out over the radio on-air** — keys PTT, plays the recording, releases PTT once it finishes (same prekey/tail choreography as normal TTS-over-radio). If PTT/radio isn't available when the recording finishes, the capture is dropped rather than falling back to local playback. A `PLAYBACK_COOLDOWN_S` (2s) window after each transmission keeps the listener from immediately re-capturing its own tail/echo as a new transmission — but on a repeater or any path where your own signal can reach your own receiver, a longer-period echo loop is still possible; test on simplex first and keep an eye on it before leaving it running unattended. Captures under 0.6s are treated as squelch noise and discarded; a single capture is capped at 30s.

**HDMI changes** are silently ignored — display-source connect/disconnect triggers HDMI audio appearance/disappearance but is not a real speaker change and produces no announcement.

**Mic hot-plug:** If the mic stream goes silent for too long (USB mic unplugged or PortAudio cache stale), the daemon reinitialises PortAudio and reopens the stream on the newly enumerated device.

**Default sink preference:**
- Radio profile active → AIOC sink
- No radio → first `Generic_USB2.0` non-HDMI, non-AIOC sink

### Language filter

By default only **English and Chinese** are shown and routed to the AI Agent. Other languages (Japanese, Korean, Cyrillic, Arabic, etc. that `gpt-4o-transcribe` hallucinates from noise) are silently dropped.

Toggle from the dashboard: **Multi-lang: OFF → ON** to see all languages (useful for diagnosing capture).

### Chinese (Simplified)

All captured Chinese is automatically normalised from Traditional to Simplified using `zhconv`. You can speak mixed sentences naturally — TTS splits by script and voices each run separately.

TTS chain for text containing Chinese: **ElevenLabs → Edge TTS → OpenAI TTS → Piper**. Edge TTS (the [edge-tts skill](https://github.com/w2ayz/openclaw-edge-tts) — free, no API key, native `zh-CN-XiaoxiaoNeural` / `en-US-AriaNeural` neural voices) sits between the paid tiers; its MP3 output is decoded to WAV with `mpg123`. Pure-English replies go straight to the offline Piper `en_US-lessac-medium` voice as before. If the edge-tts skill or Node.js isn't installed, the chain simply skips that tier.

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

Default: `agent:main:main` (the AI Agent's primary session). Override with `--session-key`.

### OpenClaw model

The daemon connects to OpenClaw gateway at `ws://127.0.0.1:18789` (protocol v4). The AI Agent's model is configured in `~/.openclaw/openclaw.json`:

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
├── Deployment.md                    step-by-step install guide (prerequisites, folder layout, agent name/wake phrase)
├── CHANGELOG.md                     version history
├── SKILL.md                         OpenClaw skill descriptor
├── RADIO-INTERFACE.md               AIOC / Digirig radio interface reference
├── UI-BUTTONS.md                    dashboard button reference
├── requirements.txt                 Python dependencies
├── RealTimeTalk-daemon.py           main daemon
├── radio_interfaces.py              Radio Mode: interface registry, PTT/audio resolution
├── dtmf_monitor.py                  standalone DTMF Mon/Train/Retrain CLI
├── RealTimeTalk-install-pi.sh       one-command Pi deploy
└── RealTimeTalk-toggle.sh           start / stop / restart / disable / enable / status / log / devices
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Gateway connect fails: `protocol mismatch` | OpenClaw updated to v4 protocol | Daemon now negotiates `minProtocol: 4, maxProtocol: 4` — update from v1.6 |
| AI Agent not responding / empty reply | Codex harness delivers reply via message tool, not chat content | v1.7 fetches from `chat.history` as fallback — update daemon |
| Speech cut off after 2–3 words | VAD silence window too short + AGC inter-word gaps look like silence | `silence_duration_ms` raised to 1100 ms in v1.7 |
| Voice activated but transcription is garbage / wrong language | Noise hallucinations | Language filter drops non-EN/ZH by default; check mic gate in dashboard |
| Chinese shows as Traditional characters | Transcriber outputs Traditional | v1.7 normalises to Simplified via zhconv automatically |
| Speaker calibration hangs or takes 30+ seconds | Old per-step parec capture (v1.6) | v1.7 uses fast sd.rec (~6 s total) |
| Speaker calibration sets max volume (PW 60%) | Old "max tone energy" logic always picked loudest step | v1.7 picks minimum clearly-audible step (SNR knee) |
| No audio from speaker after calibration | Calibrated level too low, or wrong sink | Check dashboard device panel; use Manual adjustment on speaker-cal page |
| Dashboard button icon shows as a blank box (owner/everyone 👤 specifically) | No color-emoji font installed — that icon is outside the Basic Multilingual Plane, unlike the rest of the button glyphs | `sudo apt install fonts-noto-color-emoji` (installer does this automatically), then **fully restart Chromium** — it only re-scans system fonts at its own startup, not on page reload |
| `aplay: audio open error: Device or resource busy` | PipeWire holds USB device exclusively | v1.7 plays TTS via paplay through PipeWire; speaker-cal also PipeWire-native |
| Selected speaker shows "Idle" in the dashboard and Play test produces no sound | `_find_usb_speaker_sink()` ignored PipeWire's actual default sink and always played to the first non-HDMI sink in creation order (often the onboard jack) regardless of what's selected | Fixed in v3.10.0 — both sink-lookup helpers now prefer the current default sink |
| Mic stops working after a USB device is plugged/unplugged | `--input-device <number>` pins a numeric index that shifts whenever the set of USB audio devices changes | Use `--input-device pipewire` (installer's default since v3.10.0) instead of a numeric index — it's a name, not an index, so it survives hot-plug |
| `speech_started` fires but never `speech_stopped` | Noise floor with gain applied looks like speech | Run Calibrate mic from dashboard |
| Piper produces silence / wrong language | espeak-ng missing for Chinese | `apt install espeak-ng` (installer does this automatically) |
| Service not starting after reboot | Linger not enabled | `loginctl enable-linger $USER` (installer does this automatically) |

---

## License

MIT
