# Changelog

## v2.1.0 — 2026-06-01

### Added

- **AIOC ham radio integration — full TX/RX over the air.** When an AIOC (All-In-One-Cable, USB ID 1209:7388) is connected, the daemon automatically opens `/dev/ttyACM0` and enables PTT mode. On every TTS reply, it asserts serial DTR (keying the radio's PTT) for a 250 ms pre-key delay, plays audio through the AIOC's PipeWire sink, holds PTT for a 400 ms tail, then releases. Audio is routed dynamically via `paplay --device=<aioc_sink>` so the AIOC sink is found at runtime — no hard-coded card numbers. If the AIOC is absent the daemon starts normally with PTT silently disabled.

- **TX transcript gate.** While PTT is asserted (`_is_tx = True`), all incoming transcripts from the mic are suppressed. This prevents Five's own transmitted voice (or radio sidetone) from being picked up and re-routed as a new command.

- **AIOC warning banner on dashboard.** When the AIOC serial port is open (PTT mode active), the announcement bar shows a persistent highlighted red banner: **📡 AIOC ACTIVE — audio output transmits LIVE OVER THE AIR**. This replaces the normal idle/device-change messages for as long as the AIOC is connected.

- **TRANSMITTING speaking banner.** While PTT is keyed and Five is speaking, the SPEAKING banner changes from the normal green "♩ Five is speaking…" to a red "📡 TRANSMITTING…" banner so the dashboard clearly shows on-air state.

### Notes

- Requires `pyserial` in the RTT venv (`pip install pyserial`).
- AIOC provides line-level radio audio on input (~10–30× higher than a microphone). Lower `--mic-gain` to `2` and re-run mic auto-calibration after connecting AIOC as the mic source.
- AIOC default PTT mapping: `SERIALDTRNRTS` (DTR=True, RTS=False keys PTT). This matches the AIOC firmware default and requires no HID configuration.

---

## v2.0.4 — 2026-06-01

### Fixed

- **OWW false-positive wakes activating Five unintentionally.** When the wake-word detector fired (e.g. from ambient TV or background speech), the session started in Active mode — Five would listen and route transcripts for 10 minutes before auto-sleeping. OWW wakes now enter **Silent mode** instead; the user must explicitly say "Five wake up" to go Active. The HTTP Wake button retains its direct-to-Active behaviour.

- **Play test silent during SLEEPING state.** The `/speaker-cal/loop-start` handler used `ALSA_OUTPUT` (hardcoded `"plughw:3,0"`) when no session was active. PipeWire holds exclusive ownership of the hardware device, so `aplay -D plughw:3,0` failed silently. `main()` now updates the global `ALSA_OUTPUT` to the CLI `--alsa-output` value at startup so all HTTP handlers always use the correct device path.

### Changed

- **OpenClaw agent timeout doubled from 45 s to 90 s.** Gives Five more time to complete longer tool calls or multi-step responses before RTT gives up waiting.

---

## v2.0.3 — 2026-05-30

### Fixed

- **SLEEPING state lost on service restart.** Changing the mic input device triggers `systemctl restart`, which reset all in-memory state including `_idle_disconnected`. Sleep state is now persisted to `~/.openclaw/workspace/rtt_sleep_state.json` and restored on startup. The main loop now checks for sleeping state at the top of the session loop, so a restart while sleeping never makes an unwanted OpenAI connection before the wake signal arrives.

- **Mic level meter flat while sleeping.** During auto-sleep the session `sd.InputStream` is closed, so `_mic_level_current` stayed at 0 and the calibration page mic meter appeared dead. The OWW wake-word listener (which runs its own audio stream continuously) now also writes the raw peak to `_mic_level_current`, keeping the meter live regardless of sleep state.

- **Auto-calibration left speaker muted after running while sleeping.** The `_cal_announce` block that restores PipeWire volume after the calibration sweep was gated on `if sess:`, which is `None` during SLEEPING. The sweep finished, set the sink to PW=1% (the minimum test level), and never restored it. The announce thread now always runs; only the TTS confirmation is skipped when sleeping.

- **Auto-calibration result not reflected in Manual Adjustment fields.** After calibration, `#volval` and `#swval` only updated on the next `setInterval(upd, 2000)` tick — up to 2 seconds late — and could briefly show the wrong value (45%) because `_cal_announce` temporarily boosts PipeWire volume for the TTS announcement. The `runCal()` fetch callback now writes `safe_vol` and `safe_sw_vol` directly to those fields immediately when the JSON response arrives.

- **Mic Auto-calibrate returning 503 during session reconnects.** `/calibrate/run` was gated on `if sess:` and returned "No active session" whenever the OpenAI WebSocket was reconnecting. Since the OWW listener now keeps `_mic_level_current` alive at all times, the session guard was unnecessary. The handler now always measures noise from `_mic_level_current`; TTS confirmation is skipped only when sleeping.

- **Monitoring mode blocking auto-sleep.** Every transcript logged in monitoring mode called `_last_activity[0] = time()`, resetting the idle timer indefinitely — even single-word ambient words picked up from a nearby conversation. Monitoring is passive capture and must not prevent idle sleep. Removed the `_last_activity` update from the monitoring branch (`_idle_watcher` already turns monitoring off when sleep fires).

- **Wake word false positives at threshold 0.50.** Score 0.50 (floor of threshold) triggered spurious wakes from ambient conversation. Raised `OWW_THRESHOLD` from 0.50 → 0.60. Extracted as a named top-level constant for easy tuning. The genuine detection at score 0.91 recorded the same day would still fire at any reasonable threshold.

### Changed

- **Mic Auto-calibrate button label.** Renamed from "Auto-calibrate (3 sec quiet)" to "Mic Auto-calibrate (3 sec quiet)" to distinguish it from the speaker auto-calibration button.

---

## v2.0.2 — 2026-05-24

### Fixed

- **Self-waking from sleep.** The "Going to sleep" TTS announcement said "hey Jarvis" out loud, which the wake-word detector heard through the mic and immediately re-triggered. Shortened announcement to "Going to sleep."
- **Stuck SLEEPING state after wake word during shutdown.** Wake word fired while auto-sleep was still closing the OpenAI WebSocket; `_idle_disconnected` wasn't set yet so the wake event was silently dropped and the system got stuck waiting. Fixed by setting `_idle_disconnected = True` before the TTS, so any wake word during the announcement correctly triggers reconnect.
- **Stuck "Resuming…" banner.** `_handle_transcript` runs as concurrent `create_task` instances. Repeated "I said continue" phrases each found `_paused_speech[0]` still set and launched another `speak()` in the executor, deadlocking sounddevice. Fixed by clearing `_paused_speech[0]` immediately on entry and guarding with `_busy.is_set()`. Same fix applied to HTTP `/continue`.
- **Foreign language hallucinations with multilang OFF.** Single-word pure-ASCII foreign words ("Esquece", "Senhores", "Legjeni", "Adineu") bypassed the character-level filter and the langdetect check (which only ran on ≥2-word texts). Extended the short-word noise guard from `< 6` to `< 9` chars; whitelisted useful English single-word responses (thanks, please, repeat, exactly, correct, alright, right, great).
- **Five interrupting itself during TTS.** Two-part fix: (1) raised `SPEAK_INTERRUPT_PEAK` floor from 1200 → 4000 to clear AGC-boosted ambient noise on the Pi/headset; (2) raised interrupt safety margin `_SAFETY` from 1.8× → 3.5× to account for reverb building after the guard period.
- **Garbled/wavering Bluetooth audio.** TTS was played via `aplay -D default` which routes through the ALSA→PipeWire compat layer with poor resampling (Piper 22050 Hz mono → Bluetooth 48000 Hz stereo via SBC). Switched to `paplay` for `default`/`pulse` output — PipeWire-native, handles resampling and Bluetooth codec path correctly.
- **Multi-lang button label.** Button now shows active mode in the label: "Multi-lang" (off), "Lang: EN/ZH", "Lang: List", "Lang: Any" — consistent with Monitor button pattern.

### Changed

- **SLEEP → SLEEPING.** State pill label renamed for clarity.
- **SLEEPING banner.** When sleeping, the device banner shows `Say "Hey Jarvis" or press Wake to resume.` instead of being blank.
- **TTS interrupt floor and safety margin.** `SPEAK_INTERRUPT_PEAK` 1200 → 4000; `_SAFETY` 1.8 → 3.5.

---

## v2.0.1 — 2026-05-23

### Fixed

- **Emoji rendering on Pi/Chromium.** Replaced all SMP-range emoji (U+1F000+) in the dashboard HTML with BMP Unicode symbols that render on Chromium without a color emoji font installed: 🔔→☾ (Sleep), 👂→◎ (Monitor), 🌐→⊕ (Language), 🎤→◉ (mic), 🔊→♪ (speaker).

---

## v2.0.0 — 2026-05-22

Multi-language whitelist, monitoring UX overhaul, PAUSED-state listening fix, dashboard button hints, and a round of reliability fixes.

### Added

- **4-state multi-lang cycle.** Dashboard button cycles OFF → EN/ZH → Whitelist → Any → OFF.
  - **OFF** — EN/ZH only, auto-sleep on (original default).
  - **EN/ZH** — EN/ZH only, auto-sleep suppressed.
  - **Whitelist** — accept only `MULTILANG_WHITELIST_LANGS` (default: `en`, `zh-cn`, `zh-tw`, `zh`, `ko`, `ja`, `es`, `ms`), auto-sleep suppressed.
  - **Any** — all languages pass, auto-sleep suppressed.
  - `_is_in_multilang_whitelist()` uses Unicode script ranges (Hangul → ko, kana → ja, CJK → zh, Arabic, Cyrillic, Devanagari) then falls back to langdetect for Latin-script text. Add/remove codes from `MULTILANG_WHITELIST_LANGS` to extend the list.

- **Monitor button combined.** Single toggle button replaces separate Monitor On / Monitor Off buttons. Label shows `Monitor` (inactive) or `Monitor On` highlighted (active).

- **Sleep button clears monitoring.** Clicking Sleep now also turns off monitoring in one tap.

- **Button hover hints.** Hovering any nav button shows a description in the device banner area. The noisy "No device change detected." green bar is replaced by this silent hint zone. Hints are state-aware (Monitor and Lang describe current state). Page reload is paused while hovering so hints stay visible; resumes on mouseleave.

- **JS-controlled page reload.** `<meta http-equiv="refresh">` replaced with a `setTimeout`-based reload (3 s) that can be `clearTimeout`'d on hover — giving reliable hint display without changing refresh cadence.

### Fixed

- **Wake-from-sleep immediately activates voice.** openwakeword detection and HTTP `/wake` both set `_wake_activate` flag before signalling `_wake_event`. New session starts with `_active=True` so the user can speak immediately without needing a second wake phrase.

- **Voice input works immediately after TTS interrupt (PAUSED state).** Echo gate reduced from 600 ms → 150 ms when TTS is interrupted (speaker stops instantly when killed). `input_audio_buffer.clear` sent to OpenAI on resume so stale audio doesn't confuse VAD. Full 600 ms gate still applies after normal TTS completion.

- **Multi-lang state persists across OpenAI 60-min session reconnects.** OpenAI Realtime API has a hard 60-min session limit. New sessions now restore `_multilang` from `_persist_multilang` (same pattern as `_persist_monitoring`), preventing auto-sleep from firing immediately after reconnect when multi-lang was suppressing it.

- **Monitoring state badge persists across reconnects.** `_persist_monitoring` restored in `RealtimeSession.__init__` so MONITORING badge survives the 60-min OpenAI session expiry reconnect.

- **Wake phrase / Wake button exits monitoring mode.** Saying a wake phrase or clicking Wake while monitoring now clears `_monitoring` and `_persist_monitoring` so Five becomes voice-active rather than staying in capture-only mode.

- **Auto-sleep fires during monitoring after 10 min idle.** Monitoring transcripts update `_last_activity`; if the room is silent for `IDLE_SLEEP_MINS`, auto-sleep triggers and monitoring is cleared cleanly.

- **Gateway reconnect loop no longer crashes on RuntimeError.** Inner reconnect `except` clause broadened from `(ConnectionRefusedError, OSError)` to `Exception` so any startup error is caught and retried rather than escaping and leaving `_ready` permanently unset.

- **Stale reply no longer served after gateway queue backup.** `ask()` compares new history reply against `_last_five_reply`; if identical, treats it as a stale cache hit and returns empty so the user is prompted to retry rather than receiving a recycled answer.

---

## v1.9.0 — 2026-05-22

Auto-sleep cost saving and local "Hey Jarvis" wake word so Five can be woken without a phone.

### Added

- **Auto-sleep after 10 min idle.** `_idle_watcher()` coroutine inside `RealtimeSession` disconnects from the OpenAI Realtime WebSocket after `IDLE_SLEEP_MINS` (default 10) minutes of inactivity (no wake phrase, no routed transcript). Dashboard shows a new `SLEEP` state badge (slate). Five announces "Going to sleep. Say hey Jarvis to wake me up." before disconnecting. The main loop waits on `_wake_event` instead of sleeping the normal reconnect delay.

- **Wake-from-sleep via HTTP `/wake`.** The existing `/wake` button on the dashboard sets `_wake_event` when in SLEEP state, reconnecting to OpenAI immediately.

- **`openwakeword` local wake word listener (`_oww_wakeword_listener`).** Background daemon thread started at launch — runs a 16 kHz sounddevice input stream and feeds 80 ms frames to the `hey_jarvis_v0.1` ONNX model. Operates independently of the OpenAI session, so it works even during SLEEP. On detection (score ≥ 0.5, 3 s debounce), sets `_wake_event` (if sleeping) or refreshes `_last_activity` (if active, preventing premature auto-sleep). Thread stops cleanly on daemon exit.

- **"Hey Jarvis" added to `WAKE_PHRASES`.** After waking from sleep and reconnecting to OpenAI, if the user says "hey Jarvis" again it is also treated as a wake command (not routed to Five) — consistent with openwakeword behaviour.

- **`_oww_stop_flag` global.** Signals the openwakeword thread to exit when the daemon shuts down.

- **SLEEP state in dashboard state badge.** Color: `#475569` (slate) on `#0e0e14` background.

---

## v1.8.1 — 2026-05-21

Gateway WebSocket drops silently after inactivity — transcripts routed to Five failed with `sent 1011 keepalive ping timeout`.

### Fixed

- **Gateway WebSocket keepalive.** Added `ping_interval=25, ping_timeout=10` to `websockets.connect()` for the gateway connection. The client now sends a ping every 25 s, preventing the server from timing out and closing the socket during quiet periods.

- **Gateway auto-reconnect.** `GatewayClient.listen()` is now a reconnect loop. On `ConnectionClosed` or any other error it clears all in-flight futures (so `ask()` doesn't hang), then retries `connect()` with 5 s backoff until the gateway is reachable again. The daemon no longer needs a restart to recover from a dropped gateway connection.

- **`ask()` waits for connection ready.** Added `_ready: asyncio.Event` (set on successful handshake, cleared on disconnect). `ask()` awaits `_ready` with a 20 s timeout before sending, so any message queued during a reconnect window is delivered once the connection is restored rather than failing immediately.

---

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
