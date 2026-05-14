# OpenClaw RealTimeTalk

Headless RealTime Talk daemon for Raspberry Pi. Connects directly to the OpenAI Realtime API over WebSocket — no browser, no display required. Designed for battery-powered, always-on deployments.

Runs as a `systemd` user service that starts automatically on boot and stays active until manually stopped via HTTP (phone browser over Tailscale) or SSH.

---

## Features

- Bi-directional voice conversation with OpenAI Realtime API
- Server-side VAD (Voice Activity Detection) — no push-to-talk
- Audio transcripts logged via `journald`
- HTTP toggle on port 18790 — `/stop` from any phone browser
- Internal reconnect loop — recovers from network drops without restarting
- Single `install-pi.sh` deployment — runs on fresh Raspberry Pi OS Bookworm

---

## Architecture

```
Raspberry Pi (headless, battery-powered)
│
├── systemd user service: openclaw-realtimetalk
│       starts on boot, Restart=no
│
└── RealTimeTalk-daemon.py
        │
        ├── Reads API key ─────► ~/.openclaw/openclaw.json
        │                         talk.providers.openai.apiKey
        │                         (plain string or OpenClaw SecretRef)
        │                         SecretRef → ~/.openclaw/secrets.json
        │
        ├── WebSocket ────────► wss://api.openai.com/v1/realtime
        │                         Authorization: Bearer <api_key>
        │                         OpenAI-Beta: realtime=v1
        │
        ├── Audio IN ─────────► USB microphone (48 kHz capture → 24 kHz to API)
        ├── Audio OUT ────────► 3.5mm / USB speaker (24 kHz from API → 48 kHz playback)
        │
        └── HTTP :18790 ──────► GET /stop    → graceful shutdown
                                 GET /status  → {"status":"running"}
```

**Toggle options (no keyboard/screen):**

| Method | How |
|--------|-----|
| Phone browser | `http://<pi-tailscale-ip>:18790/stop` |
| SSH | `ssh pi "systemctl --user stop openclaw-realtimetalk"` |
| Toggle script | `bash RealTimeTalk-toggle.sh stop` |

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Raspberry Pi OS Bookworm | Pi 5 or Pi 4 |
| Python 3.9+ | Pre-installed on Bookworm |
| OpenClaw gateway running locally | Provides the OpenAI API key |
| OpenAI API key configured in OpenClaw | Plain string or SecretRef at `talk.providers.openai.apiKey` — see below |
| USB microphone | Any USB mic; 44100 / 48 kHz hardware is fine — resampling is automatic |
| Speaker | 3.5mm or USB; 48 kHz hardware is fine |
| Tailscale (recommended) | For remote stop without keyboard |

### OpenAI API key formats

The daemon accepts either format in `~/.openclaw/openclaw.json`:

**Plain string:**
```json
"talk": {
  "providers": {
    "openai": { "apiKey": "sk-proj-..." }
  }
}
```

**OpenClaw SecretRef** (recommended — keeps the key out of the main config):
```json
"talk": {
  "providers": {
    "openai": {
      "apiKey": { "source": "file", "provider": "filemain", "id": "/providers/openai/apiKey" }
    }
  }
},
"secrets": {
  "providers": {
    "filemain": { "source": "file", "path": "~/.openclaw/secrets.json", "mode": "json" }
  }
}
```

With the corresponding `~/.openclaw/secrets.json` (chmod 600):
```json
{ "providers": { "openai": { "apiKey": "sk-proj-..." } } }
```

---

## Installation

### 1. Clone the repo on the Pi

```bash
git clone git@github.com:w2ayz/openclaw-RealTimeTalk.git ~/openclaw-RealTimeTalk
```

Or to install as an OpenClaw skill:

```bash
git clone git@github.com:w2ayz/openclaw-RealTimeTalk.git \
    ~/.openclaw/workspace/skills/RealTimeTalk
```

### 2. Run the installer

```bash
bash ~/openclaw-RealTimeTalk/RealTimeTalk-install-pi.sh
```

The installer will:
1. Create a Python venv at `~/.local/realtimetalk-venv` and install deps (`sounddevice`, `websockets>=12`, `numpy`)
2. Install `libportaudio2` via apt
3. Print all available audio devices
4. Write `~/.config/systemd/user/openclaw-realtimetalk.service`
5. Enable linger (service survives without a login session)
6. Start the service immediately

> **Note:** The installer uses a virtualenv because Raspberry Pi OS Bookworm (PEP 668) blocks
> system-wide `pip install`. The systemd service points at `~/.local/realtimetalk-venv/bin/python`.

### 3. Set audio devices (if defaults don't work)

List devices:
```bash
bash ~/openclaw-RealTimeTalk/RealTimeTalk-toggle.sh devices
```

Edit the two lines near the top of `RealTimeTalk-install-pi.sh`:
```bash
AUDIO_INPUT="1"    # index of USB microphone
AUDIO_OUTPUT="2"   # index of speaker
```

Re-run the installer:
```bash
bash ~/openclaw-RealTimeTalk/RealTimeTalk-install-pi.sh
```

---

## Usage

The daemon starts automatically on boot. No interaction needed.

**Check status:**
```bash
bash RealTimeTalk-toggle.sh status
# or
systemctl --user status openclaw-realtimetalk
```

**Watch live transcript (SSH):**
```bash
bash RealTimeTalk-toggle.sh log
# or
journalctl --user -u openclaw-realtimetalk -f
```

**Stop (phone browser over Tailscale):**
```
http://<pi-tailscale-ip>:18790/stop
```

**Stop (SSH):**
```bash
bash RealTimeTalk-toggle.sh stop
```

**Start again after manual stop:**
```bash
bash RealTimeTalk-toggle.sh start
```

---

## Configuration

### Audio devices

The installer defaults to `AUDIO_INPUT="none"` / `AUDIO_OUTPUT="none"`, which lets sounddevice
pick the system defaults. If the wrong device is chosen, list devices and set explicit indices:

```bash
~/.local/realtimetalk-venv/bin/python RealTimeTalk-daemon.py --list-devices
```

Then edit `AUDIO_INPUT` / `AUDIO_OUTPUT` in `RealTimeTalk-install-pi.sh` and re-run it.

You can also pass flags directly for testing:
```bash
~/.local/realtimetalk-venv/bin/python RealTimeTalk-daemon.py --input-device 1 --output-device 2
```

### Audio sample rate

The OpenAI Realtime API uses 24 kHz PCM16. Most USB audio hardware on Pi OS Bookworm only
supports 44100 / 48000 Hz. The daemon handles this automatically:

- **Capture:** records at 48 kHz, decimates 2:1 → 24 kHz before sending to OpenAI
- **Playback:** receives 24 kHz from OpenAI, upsamples 2:1 → 48 kHz for the speaker

No manual configuration is needed.

### HTTP port

Default is `18790`. Override:

```bash
~/.local/realtimetalk-venv/bin/python RealTimeTalk-daemon.py --http-port 8080
```

Update `ExecStart` in the service file accordingly, then:
```bash
systemctl --user daemon-reload && systemctl --user restart openclaw-realtimetalk
```

### OpenAI model / voice

The session is configured in `RealTimeTalk-daemon.py` at the `session.update` call. Defaults:

- **Model:** `gpt-4o-realtime-preview`
- **Turn detection:** server VAD, threshold 0.5, silence 800 ms
- **Transcription:** `whisper-1`

Edit those values in the daemon and restart the service.

---

## File Structure

```
RealTimeTalk/
├── README.md                       # this file
├── SKILL.md                        # OpenClaw skill descriptor + implementation notes
├── CHANGELOG.md                    # version history
├── requirements.txt                # Python dependencies
├── RealTimeTalk-daemon.py          # main daemon: OpenAI WS + audio I/O + HTTP toggle
├── RealTimeTalk-install-pi.sh      # one-command Pi deploy (systemd user service)
└── RealTimeTalk-toggle.sh          # start / stop / status / log / devices
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| No audio input | Wrong device index | `toggle.sh devices`, set `AUDIO_INPUT` in install script |
| No audio output | Wrong device index | Same as above for `AUDIO_OUTPUT` |
| `paInvalidSampleRate` error | Shouldn't happen — resampling is automatic | Check that `DEVICE_RATE=48000` is set in the daemon; verify hardware with `arecord -l` |
| "No OpenAI API key" error | `talk.providers.openai.apiKey` missing or empty | Add a plain string or SecretRef — see API key formats above |
| "invalid_api_key" from OpenAI | Wrong key, or SecretRef path is incorrect | Check `secrets.json` exists and the `id` path resolves correctly |
| Service not starting after reboot | Linger not enabled | `loginctl enable-linger $USER` |
| Connection keeps dropping | Network instability | Daemon auto-reconnects every 5s; check `toggle.sh log` |
| `/stop` endpoint unreachable | Tailscale not connected | Verify `tailscale status` on Pi |

---

## License

MIT
