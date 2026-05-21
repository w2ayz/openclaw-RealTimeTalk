# Changelog

## v1.8.0 — 2026-05-21

Mac-version ports, complete UI redesign, acoustic self-interrupt fix, noise hallucination filters.

### Added

- **Acoustic coupling-based TTS self-interrupt guard.** `speak()` loads the WAV PCM, plays the first 1 s while simultaneously recording from the mic, and measures the ratio `coupling = guard_max_mic / guard_max_out`. The interrupt threshold is then set to `output_peak × coupling × 1.8` — proportional to room acoustics rather than a fixed constant. This eliminates false self-interrupts on loud speakers and avoids premature threshold exceedance on quiet ones. Ported from Mac version.

- **`interruptible` parameter on `speak()`.** Only Five's main reply is `interruptible=True`; all system announcements (volume confirmations, status messages) are protected from the acoustic interrupt guard.

- **`_post_busy_until` echo gate.** After TTS completes, `_mic_cb` ignores all audio for 300 ms, preventing the tail-end speaker echo from triggering a new transcript.

- **`/continue` HTTP route.** Resumes paused TTS mid-sentence via `_resume_from_http()`.

- **`/gateway-reset` HTTP route.** Drops and reconnects the OpenClaw WebSocket without restarting the daemon.

- **MONITOR_ON/OFF_PHRASES and CONTINUE_PHRASES** — wake-phrase sets for voice-activated monitoring toggle and TTS resume.

- **WAKE_PHRASES expanded** — Chinese/Cantonese variants ("五醒醒", "五你好", "喂五", etc.) and "wake up five".

- **Empty reply guard.** When the gateway history fallback returns an empty string, `speak()` voices "Sorry, I didn't get a response." instead of silently doing nothing.

- **Noise hallucination filters (layered):**
  - `langdetect` word threshold lowered from ≥ 3 to ≥ 2 — catches 2-word foreign phrases (e.g. Dutch "naar voren").
  - Short-word noise guard: single transcribed word < 6 chars not in `_SHORT_CMDS` set → dropped before routing to Five.
  - Existing script-level Unicode rejection (Arabic / Japanese / Korean / Cyrillic) unchanged.

- **CJK↔Latin boundary splitting in `_normalize`.** Regex inserts spaces at CJK↔ASCII transitions so mixed phrases like "我係wake up" tokenise correctly for wake-phrase matching.

- **`_read_raw_mic_from_agc_config()`.** Reads `target.object` from PipeWire AGC config file (`99-rtt-agc.conf`) at startup to initialise `RAW_MIC_SOURCE`. Mic selection made in the calibration UI persists across daemon restarts.

- **Per-device calibration persistence for manual ±adjustments.** `/speaker-cal/adjust` now calls `_save_device_cal()` after every step, not only on explicit save. Ensures levels survive a daemon restart even if the user never pressed Save.

- **Mac-style dashboard UI.** Complete redesign:
  - Fonts: Outfit (body) + JetBrains Mono (brand / timestamps) via Google Fonts CDN.
  - Dark design system — `#07090f` background, `#0d1119` surface, CSS custom properties throughout.
  - Header: brand pill → state badge → Calibrate button.
  - State badge is color-coded per state: ACTIVE `#34d399`, SILENT `#64748b`, THINKING `#f59e0b`, SPEAKING `#2dd4bf`, PAUSED `#a5b4fc`, MONITORING `#60a5fa`.
  - Nav buttons: rounded pill style with hover edge-highlight (box-shadow glow on pointer entry).
  - Device bar: compact single line including `Eff%` (Vol × SW effective percent).
  - Conversation rows: full-width colored bands newest-first; You `#38bdf8 / #051928`, Five `#f59e0b / #130e02`, Monitor `#a78bfa / #0e0820`.
  - Speaking / Paused banner with inline Stop / Continue buttons.
  - Microphone table uses `RAW_MIC_SOURCE` for Active detection instead of PipeWire RUNNING state (both mics showed Running under AGC-always-on design).
  - Timestamp color changed from `#253344` to `#5a7088` — readable against dark background.

- **Mac-style calibration UI.** Same design system: accent blue section headers, tab-style cal-mode selector, `Eff:` display, pill buttons with hover glow.

### Fixed

- **Gateway crash on retryable error.** `connect()` previously raised a bare `RuntimeError` when the gateway returned `retryable: true`, which was not caught by the outer retry loop (`except ConnectionRefusedError / OSError`). Fixed by re-raising as `ConnectionRefusedError` when `err.get("retryable")` is true.

- **Thinking counter stuck after voice interrupt.** The `CancelledError` path in `_handle_transcript` did not log the final `_log_entry("five", "")`, leaving the thinking spinner running on the dashboard. Fixed.

- **History fetch limit.** Increased to accommodate longer codex-harness reply extraction.

---

## v1.3 — 2026-05-14

End-to-end voice working on the Pi. Fixes for the OpenAI Realtime API GA changes,
quiet USB mic, and a Piper TTS truncation bug.

### Fixed

- **OpenAI Realtime API: switched to the GA transcription endpoint.**
  OpenAI disabled the old beta WebSocket "session shape" — the daemon was getting
  `beta_api_shape_disabled` on every connect. Replaced the old
  `wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview` URL +
  `OpenAI-Beta: realtime=v1` header path with the GA transcription endpoint
  `wss://api.openai.com/v1/realtime?intent=transcription`. The session config now
  uses the nested schema OpenAI moved to for GA:
  ```json
  { "type": "session.update",
    "session": { "type": "transcription",
                 "audio": { "input": { "transcription":  { "model": "gpt-4o-transcribe" },
                                       "turn_detection": { "type": "server_vad", ... } } } } }
  ```
  STT model is now `gpt-4o-transcribe` (the prior `whisper-1` is not exposed via this endpoint).
  Final transcripts arrive as `conversation.item.input_audio_transcription.completed` events.

- **Piper TTS truncation — single-word replies fixed.** Piper silently truncates input
  read from stdin to ~few words, then exits. The daemon's `speak()` was piping `text → piper.stdin`
  via `subprocess.PIPE`, which is why long replies played as one word. `speak()` now writes
  the text to a temp file and invokes `piper -i <file> -f <wav>`, producing the full WAV which
  is then played via `aplay`. This was the root cause of Victor hearing only one word per reply.

- **USB mic gain + noise gate.** PCM2902-based USB mics (the common "C-Media USB PnP Sound Device"
  adapter) output ~6× quieter than browsers receive, because browsers apply WebRTC AGC
  automatically. Server VAD therefore never triggered on speech. Added software gain (`MIC_GAIN=8`)
  with clip-to-int16, gated by a peak threshold (`MIC_GATE_PEAK=500`): blocks below the threshold
  are zeroed before send, so OpenAI's VAD sees real silence between words and can detect speech
  end. Without the gate, amplified noise floor caused infinite `speech_started` with no
  `speech_stopped`.

- **Boot-order race: gateway retry loop.** With `Restart=no` and the daemon starting before
  OpenClaw's gateway was listening on 18789, the daemon would die at boot with
  `ConnectionRefusedError` and never come back. `main()` now retries `gw.connect()` every 5 s
  until either the gateway is up or the stop event fires.

- **Wrong default mic on installer.** The installer wrote `--input-device 1` into the systemd
  ExecStart, but on this hardware index 1 is the USB *speaker* (0 input channels). The default is
  now empty — sounddevice uses the PipeWire default, which correctly routes to the USB mic via
  ALSA card 2.

### Changed

- `--output-device <index>` flag replaced with `--alsa-output <pcm>` (string ALSA PCM, e.g.
  `plughw:3,0`). Direct ALSA via `plughw` bypasses PipeWire's idle-suspend which had been
  silencing the speaker. Installer variable renamed from `AUDIO_OUTPUT` to `ALSA_OUTPUT`.
- `speak()` now prepends ~500 ms of silence before the speech so the USB speaker has time to
  wake from low-power state; without it the first word was eaten.
- `RealtimeSession` no longer requests `modalities` / `input_audio_format` / `create_response` —
  none are accepted on the transcription endpoint.
- Logging cleaned up: speech_started/stopped/added/done are silent (known), unknown event types
  log at DEBUG.

### Constants worth tuning

```python
OPENAI_TRANSCRIBE_MODEL = "gpt-4o-transcribe"
MIC_GAIN                = 8.0     # multiply each sample (post-decimation, pre-clip)
MIC_GATE_PEAK           = 500     # raise if room is noisy, lower if mic is unusually quiet
```

### Protocol notes (for contributors)

- The GA transcription endpoint accepts `session.update` (not `transcription_session.update`).
  The session object **must** include `type: "transcription"`; otherwise OpenAI returns
  `missing_required_parameter session.type`.
- Flat fields `input_audio_format`, `input_audio_transcription`, `turn_detection` directly on
  `session` all return `unknown_parameter` here — they must be nested under `audio.input.*`.
- Transcripts arrive both as streaming `…transcription.delta` chunks and a final
  `…transcription.completed` with the full text in `transcript`.

---

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
