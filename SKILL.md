---
name: RealTimeTalk
description: >
  Headless RealTime Talk daemon for Raspberry Pi. Connects directly to the
  OpenAI Realtime API via WebSocket — no browser or display required.
  Runs as a systemd user service; auto-starts on boot, stopped manually
  via HTTP endpoint (phone browser over Tailscale) or SSH.
---

# RealTimeTalk — OpenClaw Skill Implementation Guide

This skill runs a persistent voice conversation session between a headless Raspberry Pi and the OpenAI Realtime API. It is completely browser-independent: audio I/O is handled directly via PortAudio (sounddevice), and the WebSocket connection to OpenAI is managed in Python asyncio.

---

## Why not use the Control UI `toggleRealtimeTalk()`?

The Control UI's `toggleRealtimeTalk()` method creates a **browser-side WebRTC session**. It requires a live browser tab, which is not viable on a headless Pi. The OpenAI Realtime API also supports a **WebSocket transport** (server-to-server) that needs no browser. This skill uses that transport exclusively.

---

## Core Design

### `RealTimeTalk-daemon.py`

Single Python asyncio process with three concurrent concerns:

#### 1. WebSocket session (`TalkSession`)

Connects to `wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview` with:
```
Authorization: Bearer <api_key>
OpenAI-Beta: realtime=v1
```

On connect, sends `session.update` to configure:
- Modalities: `["text", "audio"]`
- Audio format: `pcm16` both directions
- Turn detection: `server_vad` (threshold 0.5, silence 800ms)
- Transcription: `whisper-1`

#### 2. Audio I/O (PortAudio callbacks)

Two `sounddevice` streams, both callback-driven (PortAudio's own thread):

**Input (`_mic_cb`):**
- Called every 100ms with 2400 frames (24kHz × 0.1s) of `int16` mono PCM
- `loop.call_soon_threadsafe()` puts bytes into an `asyncio.Queue`
- `_send_mic()` coroutine drains the queue and sends `input_audio_buffer.append` messages

**Output (`_spk_cb`):**
- Called every 100ms requesting 2400 frames
- Reads from `AudioOutputBuffer` (thread-safe `bytearray` + `threading.Lock`)
- Pads with silence if no audio buffered yet (avoids glitches during silence)
- `_recv_ws()` feeds `response.audio.delta` bytes into the buffer

#### 3. HTTP toggle server (daemon thread)

`HTTPServer` running in a daemon thread. Two endpoints:
- `GET /stop` or `GET /toggle` — calls `loop.call_soon_threadsafe(stop_event.set)` to trigger graceful shutdown
- `GET /status` — returns `{"status": "running"}`

#### Shutdown flow

1. SIGTERM (from systemd) or HTTP `/stop` → sets `asyncio.Event stop_event`
2. A watcher coroutine propagates this to the active `TalkSession._stop` event
3. `asyncio.wait(FIRST_COMPLETED)` in `TalkSession.run()` cancels remaining tasks
4. PortAudio streams are closed via context manager
5. WebSocket closes cleanly
6. Main loop exits

#### Reconnect behaviour

On any `ConnectionClosedError` or unhandled exception, the outer `while not stop_event.is_set()` loop waits `RECONNECT_DELAY` (5s) and starts a new `TalkSession`. A deliberate stop (SIGTERM or HTTP) skips the reconnect.

---

## Key constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `OPENAI_MODEL` | `gpt-4o-realtime-preview` | Realtime model |
| `SAMPLE_RATE` | `24000` | Required by OpenAI PCM16 format |
| `BLOCKSIZE` | `2400` | 100ms chunks at 24kHz |
| `DEFAULT_HTTP_PORT` | `18790` | Toggle endpoint |
| `RECONNECT_DELAY` | `5` | Seconds between reconnects |

---

## API key source

The daemon reads the OpenAI API key from the openclaw config directly:

```python
cfg["talk"]["providers"]["openai"]["apiKey"]
```

Path: `~/.openclaw/openclaw.json`

No ACP WebSocket call to the gateway is needed — the API key is available at rest on disk.

---

## systemd service

Managed by `RealTimeTalk-install-pi.sh`. Written to `~/.config/systemd/user/openclaw-realtimetalk.service`.

Key service settings:

| Setting | Value | Reason |
|---------|-------|--------|
| `After=network-online.target` | — | Wait for network before starting |
| `Restart=no` | — | Only stop manually; no auto-restart |
| `PYTHONUNBUFFERED=1` | env | Live log output to journald |
| `loginctl enable-linger` | — | Service starts at boot without login |

### Why `Restart=no`

The user requirement is "can only be toggled off manually." `Restart=no` at the systemd level ensures the service does not auto-restart after a manual stop. The daemon handles its own reconnect loop for transient network failures — systemd only starts it once at boot.

---

## Audio device selection

sounddevice defaults to the system default ALSA device. On a Pi with USB mic + 3.5mm speaker, the default output is usually the 3.5mm jack (device 0) and the USB mic is a numbered device.

Find indices:
```bash
python3 RealTimeTalk-daemon.py --list-devices
```

Pass via flags:
```bash
python3 RealTimeTalk-daemon.py --input-device 2 --output-device 0
```

Or set `AUDIO_INPUT` / `AUDIO_OUTPUT` in `RealTimeTalk-install-pi.sh` before running it.

---

## Thread safety notes

- `AudioOutputBuffer._buf` is a `bytearray` protected by `threading.Lock` — safe for concurrent writes from `_recv_ws` (asyncio thread) and reads from `_spk_cb` (PortAudio thread)
- `asyncio.Queue` for mic data is written via `loop.call_soon_threadsafe` from the PortAudio callback thread — safe
- HTTP handler calls `loop.call_soon_threadsafe(stop_event.set)` from the HTTP thread — safe

---

## Deployment flow

```
Mac (development)                    Raspberry Pi (target)
─────────────────                    ────────────────────
git push →                     →     git pull
RealTimeTalk-install-pi.sh     →     bash RealTimeTalk-install-pi.sh
                                      ↓
                               ~/.config/systemd/user/openclaw-realtimetalk.service
                                      ↓
                               systemctl --user enable + start
                                      ↓
                               loginctl enable-linger  (survives reboot)
```

---

## Dependencies

Python packages (`requirements.txt`):
- `sounddevice>=0.4.0` — PortAudio Python bindings
- `websockets>=12.0` — async WebSocket client (`additional_headers` param requires >=12)
- `numpy>=1.20.0` — PCM array manipulation

System package:
- `libportaudio2` — PortAudio native library (`apt install libportaudio2`)
