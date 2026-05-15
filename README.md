# OpenClaw RealTimeTalk

Headless RealTime Talk daemon for Raspberry Pi. Captures voice from a USB mic, transcribes it
via the OpenAI Realtime Transcription API (GA `?intent=transcription`), routes the transcript
through the local OpenClaw gateway so Five answers with full memory + tools, then synthesises
the reply with Piper TTS and plays it through a USB speaker. No browser, no display required —
designed for battery-powered, always-on deployments.

Runs as a `systemd` user service that starts automatically on boot and stays active until manually stopped via HTTP (phone browser over Tailscale) or SSH.

---

## Features

- Voice conversation routed through Five's main OpenClaw session (memory, tools, identity)
- OpenAI Realtime **Transcription** API (`gpt-4o-transcribe`) with server-side VAD
- Software mic gain + noise gate compensates for quiet USB mics (PCM2902 etc.)
- Direct ALSA playback via `aplay -D plughw:3,0` — bypasses PipeWire idle-suspend
- HTTP toggle on port 18790 — `/stop` from any phone browser
- Boot-order safe — daemon retries gateway connection until OpenClaw is up
- Single `install-pi.sh` deployment — runs on fresh Raspberry Pi OS Bookworm

---

## Architecture

```
Raspberry Pi (headless, battery-powered)
│
├── systemd user service: openclaw-realtimetalk
│       starts on boot, retries gateway every 5s until ready
│
└── RealTimeTalk-daemon.py
        │
        ├── OpenClaw gateway ─► ws://127.0.0.1:18789 (GatewayClient)
        │   (persistent WS)       chat.send + agent.wait
        │                         ↕ Five's session: memory, tools, identity
        │
        ├── OpenAI Realtime ──► wss://api.openai.com/v1/realtime?intent=transcription
        │   (transcription only)   server VAD + gpt-4o-transcribe
        │                          session.type: "transcription"
        │
        ├── Audio IN ─────────► USB microphone (48 kHz → 8× gain → noise gate → 24 kHz)
        │
        ├── Piper TTS ────────► ~/.local/bin/piper  (text file → 22 kHz mono WAV)
        │
        ├── Audio OUT ────────► USB speaker via aplay -D plughw:3,0 (direct ALSA)
        │
        └── HTTP :18790 ──────► GET /stop    → graceful shutdown
                                 GET /status  → {"status":"running"}
```

Voice conversations are part of Five's main session — the same context, memory,
and tool access as Telegram messages.

---

## Signal chain (detailed)

```
You speak
   │
   ▼
USB mic (PCM2902) ─► ALSA card 2 ─► PipeWire ─► PortAudio (sounddevice)
   │
   ▼  sd.InputStream  48 kHz mono int16, 100 ms blocks
┌─────────────── daemon mic callback ─────────────────┐
│  • decimate 48k → 24k (every-other sample)          │
│  • noise gate:  if peak < 500 → output zeros        │
│  • else gain 8× (clip to int16)                     │
│  • asyncio.Queue (drops on overflow)                │
└─────────────────────────────────────────────────────┘
   │
   ▼  ws.send {input_audio_buffer.append, base64 PCM}
wss://api.openai.com/v1/realtime?intent=transcription
   │
   ▼  server-side VAD (0.5 threshold, 800 ms silence to end)
gpt-4o-transcribe  →  transcription.delta…  →  transcription.completed
   │
   ▼  daemon._handle_transcript()
GatewayClient.ask()  →  ws://127.0.0.1:18789
   │  chat.send  { sessionKey: agent:main:main, idempotencyKey, message }
   │             → {ok: true, runId}
   │  agent.wait { runId, timeoutMs: 45000 }
   ▼
OpenClaw gateway (Node) routes to Five's main session
   │
   ▼  Five thinks with memory + tools + OpenClaw agent  (model: openai-codex/gpt-5.5)
chat event { state: "final", message.content[].text }
   │
   ▼  daemon receives reply, _busy event blocks mic during playback
strip_markdown()  →  piper -i text.txt -f reply.wav  (22050 Hz mono)
   │
   ▼  aplay -D plughw:3,0 reply.wav   (plughw upsamples 22k→48k, mono→stereo)
USB speaker (UACDemoV1.0, card 3)
   │
   ▼
You hear Five
```

**Step-by-step:**

1. **Mic capture.** USB mic ADC samples at 48 kHz mono. ALSA card 2 → PipeWire → PortAudio. The daemon's `sd.InputStream` calls `_mic_cb` every 100 ms with a 4800-frame block.
2. **Block processing.** Decimate 2:1 to 24 kHz (OpenAI's required rate). Peak below 500 (~1.5 % full-scale) is treated as silence and zeroed — this lets the server's VAD see real silence between words. Otherwise multiply by 8× and clip — counters the PCM2902's low output (browsers' WebRTC AGC normally compensates).
3. **Send to OpenAI.** Persistent WebSocket to `wss://api.openai.com/v1/realtime?intent=transcription`. Bearer token only — no beta header. Initial `session.update` sets `session.type: "transcription"` with `audio.input.transcription.model: "gpt-4o-transcribe"` and `audio.input.turn_detection: server_vad`. Each block is sent as `input_audio_buffer.append` with base64 PCM.
4. **Server transcription.** Events arrive in order: `speech_started` → `speech_stopped` → `input_audio_buffer.committed` → `conversation.item.added/done` → streaming `…transcription.delta` chunks → `…transcription.completed` carrying the full transcript.
5. **Daemon routing.** `_handle_transcript` sets `_busy` (mute mic to prevent feedback). `GatewayClient.ask()` sends `chat.send` over the loopback gateway WebSocket (`ws://127.0.0.1:18789`) with an `idempotencyKey`; gateway returns `runId`. Daemon registers a Future in `_reply_futs[runId]` and fires `agent.wait`.
6. **OpenClaw → Five.** Gateway routes the message into Five's main session. Five loads the workspace context (`AGENTS.md`, `SOUL.md`, `USER.md`, `MEMORY.md`) and any active plugin tools. The configured model (`openai-codex/gpt-5.5`) generates the reply. On turn end the gateway emits a `chat` event with `state: "final"` and the reply text.
7. **Daemon receives reply.** `GatewayClient.listen` matches the `runId`, resolves the Future. `gw.ask` returns the text.
8. **TTS synthesis.** `strip_markdown()` removes `**bold**`, code fences, list markers, headings, link syntax. `speak()` writes the cleaned text to a temp `.txt` file, runs `piper -i <text> -f <wav>` to produce a 22 kHz mono WAV. **(File-based input is required — Piper silently truncates stdin input after a few words.)**
9. **Playback.** `aplay -D plughw:3,0 <wav>` plays the file. `plughw` handles the 22050 → 48000 Hz upsampling and mono → stereo expansion in the ALSA plug layer, bypassing PipeWire entirely (so the speaker doesn't go silent when PipeWire idle-suspends its sink).
10. **Cleanup.** `aplay` exits, `_busy` clears, mic samples flow again.

**Typical end-to-end latency** is 4–13 s — about 1 s for capture + 800 ms VAD silence + 0.3–0.7 s transcription + a few seconds for Five to think, then near-real-time TTS.

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
| **OpenClaw gateway running locally** | Required — daemon routes all AI through it |
| OpenAI API key configured in OpenClaw | At `talk.providers.openai.apiKey` — used only for STT (Realtime API) |
| Piper TTS installed | `~/.local/bin/piper` with a voice model in `~/.local/share/piper/voices/` |
| USB microphone | Any USB mic; 44100 / 48 kHz hardware fine — resampling is automatic |
| Speaker | 3.5mm or USB; 48 kHz hardware fine |
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
AUDIO_INPUT="none"        # sounddevice index of USB mic — "none" uses PipeWire default
ALSA_OUTPUT="plughw:3,0"  # ALSA PCM for the USB speaker (find your card with `aplay -l`)
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

The installer defaults to `AUDIO_INPUT="none"` (PipeWire default → USB mic) and `ALSA_OUTPUT="plughw:3,0"`
(USB speaker is typically card 3). List devices to verify:

```bash
~/.local/realtimetalk-venv/bin/python RealTimeTalk-daemon.py --list-devices  # sounddevice indices
aplay -l                                                                     # ALSA cards
```

Then edit `AUDIO_INPUT` / `ALSA_OUTPUT` in `RealTimeTalk-install-pi.sh` and re-run it.

You can also pass flags directly for testing:
```bash
~/.local/realtimetalk-venv/bin/python RealTimeTalk-daemon.py \
    --input-device 1 --alsa-output plughw:3,0
```

`--alsa-output` is an ALSA PCM string (e.g. `plughw:3,0`, `default`, `pulse`). `plughw:<card>,<device>`
is recommended — it gives direct ALSA access with automatic rate/channel conversion in the
plug layer, and bypasses PipeWire's idle-suspend behaviour that can silence output on Pi OS Bookworm.

### Audio sample rates

| Stage | Rate | Why |
|-------|------|-----|
| Mic capture (sounddevice) | 48 kHz mono | USB mics on Pi OS Bookworm only support 44100/48000 |
| Sent to OpenAI | 24 kHz mono PCM16 | Realtime API requirement — daemon decimates 2:1 |
| Piper TTS output | 22050 Hz mono | Lessac medium voice native rate |
| Played to speaker | 48 kHz stereo | `aplay -D plughw:3,0` resamples/expands automatically |

No manual configuration needed unless your hardware is unusual.

### Mic gain / noise gate

USB mics that lack onboard AGC (e.g. C-Media PCM2902 adapters) produce signal so quiet that
OpenAI's VAD never triggers. Browsers normally compensate with WebRTC AGC; this daemon does it
in software:

```python
MIC_GAIN      = 8.0    # multiply each sample after decimation
MIC_GATE_PEAK = 500    # blocks with pre-gain peak below this → silence (true zero)
```

The gate is essential — without it, amplified noise floor fools VAD into thinking speech never
ends, and you'll see endless `speech_started` events with no `speech_stopped`. Tune `MIC_GATE_PEAK`
upward if your room is noisy, downward if the mic is unusually quiet.

### HTTP port

Default is `18790`. Override:

```bash
~/.local/realtimetalk-venv/bin/python RealTimeTalk-daemon.py --http-port 8080
```

Update `ExecStart` in the service file accordingly, then:
```bash
systemctl --user daemon-reload && systemctl --user restart openclaw-realtimetalk
```

### OpenClaw session

By default the daemon talks to `agent:main:main` (Five's primary session). Override:

```bash
~/.local/realtimetalk-venv/bin/python RealTimeTalk-daemon.py --session-key agent:main:main
```

Update `ExecStart` in the service file and reload if you want a different session permanently.

### Voice / TTS

Piper voice model is set by `PIPER_VOICE` near the top of `RealTimeTalk-daemon.py`.
Default: `en_US-lessac-medium`. Change the path to any installed Piper model and restart.

### STT / VAD settings

STT model and VAD parameters are in the `session.update` call in `RealtimeSession.run()`:

- **STT model:** `gpt-4o-transcribe` (set by `OPENAI_TRANSCRIBE_MODEL` near the top)
- **VAD threshold:** 0.5
- **Silence window:** 800 ms
- **Endpoint:** `wss://api.openai.com/v1/realtime?intent=transcription` (GA transcription session, no beta header)

The session payload uses the GA nested schema:

```json
{ "type": "session.update",
  "session": { "type": "transcription",
               "audio": { "input": { "transcription":   { "model": "gpt-4o-transcribe" },
                                     "turn_detection":  { "type": "server_vad", ... } } } } }
```

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
| `beta_api_shape_disabled` from OpenAI | Old beta endpoint / `OpenAI-Beta: realtime=v1` header | Daemon now uses `?intent=transcription` (GA) — no beta header. Update from v1.2 → v1.3 |
| `speech_started` fires but never `speech_stopped`; no transcript | Mic noise floor with gain applied looks like speech to VAD | Confirm `MIC_GATE_PEAK` is set (default 500); raise if noisy room |
| Transcript captured but Five's reply is cut to one word | Piper truncates **stdin** input silently | v1.3 writes text to a temp file and runs `piper -i <file>` — fixed |
| Speaker silent even after Piper reports success | PipeWire idle-suspended the sink | Use `aplay -D plughw:<card>,<dev>` — bypasses PipeWire entirely |
| First word of speech inaudible | USB speaker still waking from low-power state | `speak()` prepends a half-second of silence before audio |
| Daemon dies at boot with `ConnectionRefusedError 18789` | OpenClaw gateway not yet listening | v1.3 retries connection every 5s instead of crashing |
| `transcript: null` on `conversation.item.done` | Session config not enabling transcription | Use GA nested schema `audio.input.transcription.model` (not flat `input_audio_transcription`) |
| No audio input | Wrong device index (or device 1 is your speaker, not mic) | Leave `--input-device` unset to use the PipeWire default (the real mic) |
| "No OpenAI API key" error | `talk.providers.openai.apiKey` missing or empty | Add a plain string or SecretRef — see API key formats above |
| "invalid_api_key" from OpenAI | Wrong key, or SecretRef path is incorrect | Check `secrets.json` exists and the `id` path resolves correctly |
| Service not starting after reboot | Linger not enabled | `loginctl enable-linger $USER` |
| Connection keeps dropping | Network instability | Daemon auto-reconnects every 5s; check `toggle.sh log` |
| `/stop` endpoint unreachable | Tailscale not connected | Verify `tailscale status` on Pi |
| Five replies in non-English / with emojis → garbled TTS | Piper voice model is English-only | Tell Five to reply in plain English for voice, or change `PIPER_VOICE` |

---

## License

MIT
