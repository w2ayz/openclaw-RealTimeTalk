# CLAUDE.md — RealTimeTalk (Pi fork)

- Two version-locked forks: this one (Pi, PipeWire/`aplay`/`paplay`, local
  Piper) and the Mac fork
  (github.com/w2ayz/openclaw-RealTimeTalk-mac, `sounddevice`/PortAudio,
  macOS `say` in Piper's place). Bump `__version__` in
  `RealTimeTalk-daemon.py` and add matching CHANGELOG entries in both when
  a fix or feature applies to both fork — port by adapting to each fork's
  idioms, not by cherry-picking the diff. The two implementations have
  genuinely diverged internally — check the actual code on both sides
  before assuming a fix ports 1:1. Confirmed example, fixed in v3.23.0: this
  fork read the ElevenLabs key from a flat `~/.openclaw/secrets/elevenlabs`
  file (`_elevenlabs_tts()`'s own lazy-load), while the Mac fork already
  used `talk.providers.elevenlabs.apiKey` via the shared
  `_resolve_provider_api_key()` helper this fork also already had (for
  openai/gemini) — migrated onto that same helper via a new
  `load_elevenlabs_key()`, the old file is no longer read at all.

- As of v3.23.0: STT is optional (`rtt_stt_config.json`'s
  `"provider": "none"`, or simply no OpenAI/Gemini key at all, resolves to
  `STT_ENGINE_NONE` in `_resolve_stt_engine()` — `main()` then skips mic/STT
  session setup entirely and just serves `/speak` — TTS-only, no more
  crash-looping under systemd waiting for a key). And the TTS engine order
  is user-configurable (`rtt_tts_config.json`'s `"order"`, resolved once per
  session into `TTS_ORDER` by `_resolve_tts_order()`) — **and, on request,
  now applies uniformly to all text**: previously this fork routed English
  straight to Piper and only tried the ElevenLabs → Edge → OpenAI network
  tiers for Chinese/mixed text; that language-conditional split is gone,
  `_synthesize()` now runs the same `TTS_ORDER` chain over the whole text
  regardless of language, matching the Mac fork exactly. This is a real
  behavior change worth watching after deploy: English replies will now
  attempt network TTS first by default (unless reconfigured to
  `["piper"]`), where they previously never did. Both features are set via
  `RealTimeTalk-configure.sh` (re-runnable anytime) or the installer's §5,
  which now calls into `RealTimeTalk-config-lib.sh`'s
  `run_stt_setup`/`run_tts_setup`/`run_vocabulary_setup` — ported from the
  Mac fork, which got these first; only static-checked here (syntax +
  `pyflakes`/`ruff --select F821,E9` equivalent, plus the config-lib
  functions exercised against scratch configs), **not yet verified on real
  Pi hardware** — see the point below on why that matters.

- **Voice-command phrase matching: `_matches_phrase`'s fuzzy pass is only
  safe for `WAKE_PHRASES`.** It counts a match at ≥60% of a *phrase*'s
  words present anywhere in the transcript — fine for wake, which asks
  "Yes?" and self-corrects on a false positive, but every other phrase set
  (`SLEEP_PHRASES`, `MONITOR_ON/OFF_PHRASES`, `OWNER_ONLY_ON/OFF_PHRASES`,
  `CONTINUE_PHRASES`) fires immediately with no confirmation. Ported from
  the Mac fork in v3.23.1, caught live there: short phrases combining the
  agent name (in nearly every utterance) with one common word ("on",
  "start", "to") false-fired on ordinary questions — "what's on your
  keyword list?" → monitoring ON; "go to the store website" → sleep (worst
  case: silences the daemon, no fallback). Use `_matches_phrase_exact()`
  (substring-only) for any new phrase set unless it has its own
  confirmation gate like wake's. Note this fork's phrase sets are built by
  `_build_phrase_sets(name_lc, wake_phrase)` — a cleaner, already-
  refactored single-function design, unlike the Mac fork's inline literal
  set + a separate manual-rebuild block in `main()` that has to be kept in
  sync by hand (and did drift once — the Mac fork's `WAKE_PHRASES` rebuild
  silently drops its STT-mishearing variants like "zibob wake up" that the
  base set defines, since it replaces rather than merges).

- This fork has shipped multiple bugs that only a *live* Pi run would
  catch (v3.22.0–v3.22.5: `load_gemini_key()` called but never defined —
  `NameError` on every single startup, undetected for 6 versions because
  nothing exercised that code path; fixed in v3.22.6). Getting a real
  build onto real hardware periodically catches classes of bug that
  static analysis structurally cannot.

- Before committing a change to `RealTimeTalk-daemon.py` (or any `.py`
  file here): run `git config core.hooksPath .githooks` once per clone —
  this is a manual, one-time step; nothing here automates it, since
  `.claude/` is gitignored entirely (see below) and can't self-activate
  it via a `PreToolUse` hook. Once active, `.githooks/pre-commit` runs
  `ruff check --select F821,E9` on staged files — undefined names and
  syntax errors. It exists because of the `load_gemini_key` bug above,
  and the Mac fork independently shipped the same failure mode once too
  (a bare `time.monotonic()` with no top-level `import time`, v3.21.0).
  Both are syntactically valid Python that `py_compile` doesn't catch. If
  it's not catching something it should, widen the `--select` list —
  don't bypass it with `--no-verify`.

- `.claude/` is fully gitignored — this repo is public, and `.claude/`
  can hold session artifacts (transcripts, worktrees created by
  `EnterWorktree`, scratch scripts) that must never be committed. A
  blanket `git add -A` from inside a Claude Code session is the failure
  mode this guards against — it very nearly happened here. Never narrow
  this rule to "just ignore the risky files" — track nothing under
  `.claude/` at all. The pre-commit gate above needs none of it to work;
  a `.claude/settings.json` PreToolUse convenience hook is fine to keep
  *locally, untracked*, but never stage or commit it here.

- No test suite exists yet beyond exercising the daemon manually:
  `systemctl --user restart openclaw-realtimetalk` +
  `journalctl --user -u openclaw-realtimetalk -f`, or
  `$HOME/.local/realtimetalk-venv/bin/python RealTimeTalk-daemon.py
  --list-devices` as a cheap "does it even import" smoke check. Note
  `--list-devices` exits before STT-engine resolution runs, so it would
  *not* have caught the `load_gemini_key` bug above — that's what the
  pre-commit hook is for, and it's not a substitute for an actual boot on
  real hardware given the point above.

- `openclaw.json`'s `talk` schema has no `stt` key. STT engine choice
  lives in `~/.openclaw/workspace/rtt_stt_config.json` (the daemon's own
  config file, not `openclaw.json`) — see README's "STT engine selection"
  section.

- **OpenAI's engine runs on `gpt-live-transcribe`, not `gpt-4o-transcribe`.**
  Verified live against the real API before choosing this: `gpt-4o-transcribe`
  hard-rejects the `keywords` field outright, so custom vocabulary requires
  the newer model. That model in turn hard-rejects *all* automatic turn
  detection (`server_vad` and `semantic_vad` both rejected, confirmed live) —
  only `turn_detection: null` works, so `OpenAIRealtimeSession` drives its
  own client-side speech start/stop from mic chunk peak level in
  `_send_audio_chunk`, reusing the existing calibrated `_mic_gate_ref`
  rather than a new threshold. If OpenAI transcripts start feeling
  cut-off or laggy, check `CLIENT_VAD_START_DEBOUNCE_SECS`/
  `CLIENT_VAD_STOP_SILENCE_SECS` on that class before assuming anything
  else broke — this is a homegrown VAD, not OpenAI's.

- **`rtt_stt_config.json`'s `"vocabulary"` list feeds both STT engines**,
  not just Gemini — see `OPENAI_TRANSCRIPTION_KEYWORDS` alongside
  `GEMINI_CUSTOM_VOCABULARY` in `main()`. Verified live: it measurably
  helps ("Annabel" → "Annabelle") but is a *hint*, not a guarantee — an
  unusual term (a call sign in testing) still came through imperfectly
  with the hint active. Don't oversell it in user-facing docs.
