# Changelog

## v1.2 — 2026-05-14

Full OpenClaw gateway integration — voice now routes through Five.

### Changed

- **Architecture: direct OpenAI chat → OpenClaw gateway + Piper TTS.**
  The daemon no longer generates AI responses directly through the OpenAI Realtime API.
  Instead it uses the Realtime API solely as a VAD + STT front-end (`create_response: false`),
  routes every transcript through the OpenClaw gateway (`chat.send` / `agent.wait`),
  and speaks Five's reply with Piper TTS. Voice conversations now share Five's session,
  memory, tools, and personality with all other channels (e.g. Telegram).

- **New `GatewayClient` class** — persistent WebSocket to the local gateway using the
  trusted backend-client path (`client.id: "gateway-client"`, `client.mode: "backend"`),
  which bypasses device-pairing scope checks on loopback connections. Handles
  `chat.send` (idempotency-keyed) → `runId`, `agent.wait`, and routes `chat` events
  with `state: "final"` back to the calling coroutine via `asyncio.Future`.

- **`RealtimeSession` simplified** — output stream and `AudioOutputBuffer` removed.
  Session config: `modalities: ["text"]`, `create_response: false`. Mic input is
  suppressed while Five is speaking to prevent feedback (`_busy` event flag).

- **`speak()` function** — strips markdown from Five's reply then synthesises via Piper,
  resamples 22050 → 48000 Hz for the USB speaker.

- **New `--session-key` CLI flag** — overrides the default OpenClaw session
  (`agent:main:main`).

- **`load_gateway_token()`** — reads `gateway.auth.token` from `openclaw.json`
  so no extra config is needed.

### Protocol notes (for contributors)

- `chat.send` requires `idempotencyKey` (not `runId`); returns `{runId, status: "started"}`.
- `agent.wait` takes `{runId, timeoutMs}` and resolves when the agent turn ends.
- Final reply text is in the `chat` event with `state: "final"`, at
  `payload.message.content[].text`.
- The backend-client connect path omits `device` signing; token auth is sufficient
  on loopback.

---

## v1.1 — 2026-05-14

Raspberry Pi OS Bookworm deployment fixes — first successful live deployment on Pi 5.

### Fixed

- **`load_openai_key()` now resolves OpenClaw SecretRef objects.**
  The daemon previously expected `talk.providers.openai.apiKey` in `openclaw.json` to be a
  plain string. Current OpenClaw configurations store secrets as a SecretRef:
  `{"source": "file", "provider": "filemain", "id": "/providers/openai/apiKey"}`.
  The function now detects this pattern, reads the referenced secrets file
  (`secrets.providers.<provider>.path`), and navigates the `id` path to extract the key.

- **Audio resampling between hardware (48 kHz) and OpenAI Realtime API (24 kHz).**
  USB audio devices on Pi OS Bookworm (tested: USB PnP Sound Device mic, UACDemoV1.0 speaker)
  only support 44100 / 48000 Hz — not the 24000 Hz the daemon was requesting, causing
  `paInvalidSampleRate` on every session start.
  Added `DEVICE_RATE = 48000`, `RESAMPLE_RATIO = 2`, and `DEVICE_BLOCKSIZE = BLOCKSIZE * 2`.
  Mic input is now captured at 48 kHz and decimated 2:1 (every other sample) before sending
  to OpenAI. Speaker output received at 24 kHz is upsampled 2:1 via sample-and-hold before
  writing to the hardware stream.

- **Installer uses a virtualenv instead of `pip3 --user`.**
  Raspberry Pi OS Bookworm enforces PEP 668 (externally-managed Python), which blocks
  `pip3 install --user`. The installer now creates a dedicated venv at
  `~/.local/realtimetalk-venv` and points the systemd `ExecStart` at its Python binary.

---

## v1.0 — 2026-05-14

Initial release.

### Added
- `RealTimeTalk-daemon.py` — Python asyncio daemon connecting directly to OpenAI Realtime API WebSocket (no browser)
- Callback-based audio I/O via sounddevice / PortAudio; `AudioOutputBuffer` for thread-safe PCM streaming
- Server-side VAD turn detection; `whisper-1` transcription; live transcript logging
- HTTP toggle server on port 18790 (`/stop`, `/status`) for phone-browser control over Tailscale
- Internal reconnect loop — recovers from network drops, stops cleanly on SIGTERM or HTTP stop
- `RealTimeTalk-install-pi.sh` — one-command deploy: installs deps, writes systemd user service, enables linger, starts service
- `RealTimeTalk-toggle.sh` — `start | stop | restart | status | log | devices` SSH wrapper
- `--list-devices` flag to enumerate available PortAudio devices on target hardware
- `--input-device` / `--output-device` flags for explicit audio device selection
- `--http-port` flag to override default toggle port
