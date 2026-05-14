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
- Single `install-pi.sh` deployment — runs on fresh Raspberry Pi OS

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
        │
        ├── WebSocket ────────► wss://api.openai.com/v1/realtime
        │                         Authorization: Bearer <api_key>
        │                         OpenAI-Beta: realtime=v1
        │
        ├── Audio IN ─────────► USB microphone (PortAudio / ALSA)
        ├── Audio OUT ────────► 3.5mm / USB speaker (PortAudio / ALSA)
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
| Raspberry Pi OS (Bookworm recommended) | Pi 5 or Pi 4 |
| Python 3.9+ | Pre-installed on Bookworm |
| OpenClaw gateway running locally | Provides the OpenAI API key |
| OpenAI API key configured in openclaw | `talk.providers.openai.apiKey` in `openclaw.json` |
| USB microphone | Any USB mic; index configurable |
| Speaker | 3.5mm or USB; index configurable |
| Tailscale (recommended) | For remote stop without keyboard |

---

## Installation

### 1. Copy skill to Pi

```bash
scp -r ~/.openclaw/workspace/skills/RealTimeTalk \
    pi@<pi-ip>:~/.openclaw/workspace/skills/
```

Or clone directly on the Pi:

```bash
git clone https://github.com/w2ayz/openclaw-RealTimeTalk \
    ~/.openclaw/workspace/skills/RealTimeTalk
```

### 2. Run the installer

```bash
bash ~/.openclaw/workspace/skills/RealTimeTalk/RealTimeTalk-install-pi.sh
```

The installer will:
1. Install Python deps (`sounddevice`, `websockets>=12`, `numpy`)
2. Install `libportaudio2` via apt
3. Print all available audio devices
4. Write `~/.config/systemd/user/openclaw-realtimetalk.service`
5. Enable linger (service survives without login session)
6. Start the service immediately

### 3. Set audio devices (if defaults don't work)

List devices:
```bash
bash ~/.openclaw/workspace/skills/RealTimeTalk/RealTimeTalk-toggle.sh devices
```

Edit the two lines near the top of `RealTimeTalk-install-pi.sh`:
```bash
AUDIO_INPUT="2"    # index of USB microphone
AUDIO_OUTPUT="0"   # index of speaker
```

Re-run the installer:
```bash
bash ~/.openclaw/workspace/skills/RealTimeTalk/RealTimeTalk-install-pi.sh
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

Pass device indices as flags or set them in `RealTimeTalk-install-pi.sh`:

```bash
python3 RealTimeTalk-daemon.py --input-device 2 --output-device 0
```

### HTTP port

Default is `18790`. Override:

```bash
python3 RealTimeTalk-daemon.py --http-port 8080
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
| "No OpenAI API key" error | openclaw.json missing `talk.providers.openai` | Configure Talk in openclaw: `openclaw config` |
| Service not starting after reboot | Linger not enabled | `loginctl enable-linger $USER` |
| Connection keeps dropping | Network instability | Daemon auto-reconnects every 5s; check `toggle.sh log` |
| `/stop` endpoint unreachable | Tailscale not connected | Verify `tailscale status` on Pi |

---

## License

MIT
