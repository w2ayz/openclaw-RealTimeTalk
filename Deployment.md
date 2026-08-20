# Pi Deployment Guide

> **RealTimeTalk v3.11.0** — for the full change history see [CHANGELOG.md](CHANGELOG.md).

Step-by-step reference for installing RealTimeTalk on a Raspberry Pi. Written
to be followed by someone who hasn't seen this repo before. For a feature
overview see [README.md](README.md); for internals see [SKILL.md](SKILL.md);
for radio (AIOC/Digirig) setup see [RADIO-INTERFACE.md](RADIO-INTERFACE.md).

This repo shares a codebase with the [Mac fork](https://github.com/w2ayz/openclaw-RealTimeTalk-mac)
— same daemon design, ported features in both directions. This guide covers
the Pi-specific install path (systemd instead of launchd, `apt` instead of
`brew`, no microphone-permission wrapper needed).

---

## 1. Prerequisites

### Accounts / services

| Requirement | Notes |
|---|---|
| [OpenClaw](https://openclaw.ai) gateway running locally | RealTimeTalk routes all AI through it (`ws://127.0.0.1:18789`, protocol v4). Won't start without it. |
| OpenClaw 2026.5+ | Gateway protocol v4 required. |
| OpenAI API key | Installer prompts for it (hidden input) if `talk.providers.openai.apiKey` isn't already set in `~/.openclaw/openclaw.json`. Unlike the Mac fork, the `openai-codex` OAuth provider **is** supported here too — the daemon falls back to `chat.history` since the codex harness delivers replies via a message tool rather than chat content. |
| ElevenLabs API key (optional) | Only used as a first-try Chinese-TTS voice; falls back to Piper if unset or the request fails. Not read from `openclaw.json` — see [§3.5](#35-optional-elevenlabs-key-for-chinese-tts). |

### System

| Requirement | Install |
|---|---|
| Raspberry Pi OS Bookworm/Trixie | Pi 5 or Pi 4; aarch64 or x86_64 (Piper binary auto-detects architecture) |
| Python 3.9+ | Pre-installed |
| PipeWire + `pipewire-alsa` | `pipewire-alsa` specifically, not just PipeWire — without it, ALSA's "default"/"pipewire" pseudo-devices report 0 channels and can't resample to the 24kHz/16kHz rates the Realtime API and openwakeword need (installer does this for you) |
| `pulseaudio-utils`, `espeak-ng`, `fonts-noto-color-emoji`, `sox`, `multimon-ng` | All installed automatically by the installer — see [§4](#4-installing) |

### Hardware

| Requirement | Notes |
|---|---|
| USB microphone | C-Media PCM2902 or similar; the WebRTC AGC virtual mic compensates for quiet hardware — no manual gain tuning needed in normal use |
| Speaker or headset | USB or 3.5mm; headset auto-detected |
| AIOC or Digirig Mobile dongle (optional) | Only for Radio Mode — see [§8](#8-radio-mode-optional) and [RADIO-INTERFACE.md](RADIO-INTERFACE.md). Unlike the Mac fork, **both** interfaces are supported on Pi. |

---

## 2. File structure

### Repo layout (after clone)

```
~/openclaw-RealTimeTalk/
├── RealTimeTalk-daemon.py          # the daemon — everything runs from this one file
├── radio_interfaces.py             # Radio Mode: AIOC/Digirig registry, PTT/audio device resolution, SquelchTracker
├── dtmf_monitor.py                 # standalone DTMF Mon/Train/Retrain CLI (launched from the Calibrate page)
├── requirements.txt                # Python deps (installed into venv/, see below)
├── RealTimeTalk-install-pi.sh      # installer — run once, safe to re-run any time
├── RealTimeTalk-toggle.sh          # start/stop/restart/status/log/devices — day-to-day control
├── README.md, SKILL.md, CHANGELOG.md, RADIO-INTERFACE.md, UI-BUTTONS.md
├── Deployment.md                   # this file
└── LICENSE
```

### Files the installer creates outside the repo

| Path | Purpose |
|---|---|
| `~/.local/realtimetalk-venv/` | Python venv — created fresh by the installer, not in git |
| `~/.config/systemd/user/openclaw-realtimetalk.service` | The systemd unit — installer writes and reloads this every run |
| `~/.local/bin/piper-native/` | Piper TTS binary |
| `~/.local/share/piper/voices/` | English + Chinese Piper voice models |
| `~/.local/share/rtt/speaker/` | CAM++ speaker-verification model (owner-only mode) |
| `~/.config/pipewire/pipewire.conf.d/99-rtt-agc*.conf` | WebRTC AGC virtual-mic PipeWire config, written at runtime by the daemon itself, not the installer |

### Runtime state (created by the daemon itself, first run)

All under `~/.openclaw/workspace/` or `~/.config/rtt/` — none of this is in
git, all of it is safe to delete to reset that specific piece of state:

| File | Purpose |
|---|---|
| `~/.openclaw/workspace/speaker_cal_store.json` | Per-speaker volume/SW calibration |
| `~/.openclaw/workspace/rtt_sleep_state.json` | Whether the daemon was asleep at last shutdown (restored on restart) |
| `~/.openclaw/workspace/rtt_voice_profile.json` | Owner-only voice enrollment (mic) |
| `~/.openclaw/workspace/rtt_voice_profile_radio.json` | Owner-only voice enrollment (radio path — voice characteristics differ enough over radio that a mic-enrolled profile won't reliably match) |
| `~/.openclaw/workspace/rtt_voice_mode.json` | Owner-only on/off + similarity threshold |
| `~/.config/rtt/dtmf_profiles.json` | Learned DTMF tone frequencies (Radio Mode) |

---

## 3. Adding API keys

### 3.1 OpenAI

The installer prompts for this (hidden input) if it's not already set. To set
it by hand:

```bash
python3 - <<'PY'
import json, os
KEY = "sk-..."   # your OpenAI key — regular sk-... or the openai-codex OAuth profile both work
path = os.path.expanduser("~/.openclaw/openclaw.json")
d = json.load(open(path))
d.setdefault("talk", {}).setdefault("providers", {}).setdefault("openai", {})["apiKey"] = KEY
json.dump(d, open(path, "w"), indent=2)
os.chmod(path, 0o600)
PY
```

### 3.5. Optional: ElevenLabs key for Chinese TTS

Piper's `zh_CN-huayan-medium` voice is the default and requires no key. If
you'd rather use ElevenLabs for Chinese/mixed-language segments (falls back
to Piper automatically on any failure), drop a plain-text key at:

```bash
mkdir -p ~/.openclaw/secrets
echo -n "your-elevenlabs-key" > ~/.openclaw/secrets/elevenlabs
chmod 600 ~/.openclaw/secrets/elevenlabs
```

Note this is a different mechanism than `openclaw.json` — the daemon reads
this key from a standalone file, not `talk.providers.elevenlabs.apiKey`.

---

## 4. Installing

```bash
git clone git@github.com:w2ayz/openclaw-RealTimeTalk.git ~/openclaw-RealTimeTalk
bash ~/openclaw-RealTimeTalk/RealTimeTalk-install-pi.sh
```

Safe to re-run any time (e.g. after `git pull`) — every step checks first and
skips what's already installed. The installer:

1. Installs system packages via `apt`: `libportaudio2`, `pulseaudio-utils`, `pipewire-alsa`, `espeak-ng`, `fonts-noto-color-emoji`, `sox`, `multimon-ng`
2. Creates a Python venv at `~/.local/realtimetalk-venv` and installs everything in `requirements.txt`
3. Downloads the Piper native binary + English/Chinese voice models (architecture-detected: aarch64/x86_64/armv7l)
4. Downloads the CAM++ speaker-verification model (~28MB)
5. Prompts (hidden input) for an OpenAI API key if `talk.providers.openai.apiKey` isn't already set
6. Lists detected audio devices for reference — no manual device index needed; PipeWire's own default source/sink is followed at runtime, changeable from the dashboard
7. **Prompts for agent name and wake phrase** (see [§5](#5-agent-name--wake-phrase) below), then writes `~/.config/systemd/user/openclaw-realtimetalk.service`
8. Enables linger and starts the service

### 4.1 Check the dashboard

Open `http://<pi-ip>:19000/dashboard`. The header should show **SILENT**.
Say your configured wake phrase (default: "Zeebot wake up") to activate,
then speak normally.

---

## 5. Agent name & wake phrase

As of v3.11.0, the agent's name and wake phrase are configurable per
deployment instead of hardcoded — ported from the Mac fork.

**During install**, the installer prompts for both:

```
Agent name  [Enter for 'Zeebot']:
Wake phrase [Enter for 'zeebot wake up']:
```

- Leave both blank to use the default agent name **Zeebot** and wake phrase
  **"Zeebot wake up"**.
- Enter any other name (e.g. `Grogu`, `Jarvis`, `Five`) to use that instead —
  it's substituted throughout the wake/sleep/monitor/owner-only-mode phrase
  sets, the dashboard, the voice-enrollment pages, and the conversation log.
- `--wake-phrase` is optional even if you type it — it only overrides the
  *primary* wake phrase (e.g. a phonetic respelling if the plain `<name> wake
  up` form gets misheard); `<name> wake up` is always kept as an additional
  recognised phrase regardless.

**Re-running the installer** (e.g. after `git pull`) preserves whatever
name/phrase is already configured — it reads the existing systemd unit and
offers that as the default, so pressing Enter never renames a live
deployment. If you're upgrading a pre-v3.11.0 install (no `--agent-name` in
the unit file yet), the prompt defaults to `Five` — this repo's historical
hardcoded name — not the new `Zeebot` default, for the same reason.

**To change it manually after install**, edit the `ExecStart` line in
`~/.config/systemd/user/openclaw-realtimetalk.service`:

```ini
ExecStart=/home/pi/.local/realtimetalk-venv/bin/python /home/pi/openclaw-RealTimeTalk/RealTimeTalk-daemon.py --input-device pipewire --alsa-output default --agent-name "Grogu" --wake-phrase "grogu wake up"
```

then:

```bash
systemctl --user daemon-reload
systemctl --user restart openclaw-realtimetalk
```

**One exception — "Hey Jarvis" always stays "Hey Jarvis".** It's a separate,
always-on *deep-sleep* wake word (works even when the daemon has fully
disconnected from OpenAI), detected locally via openwakeword's pretrained
`hey_jarvis_v0.1.onnx` model. That's a fixed model file, not a phrase string
this daemon controls — renaming it would require training and bundling a
custom wake-word model, which is out of scope here. Saying "Hey Jarvis" only
ever brings the daemon from deep sleep back to Silent mode; your configured
`<name> wake up` phrase is still required to actually activate it.

---

## 6. Speaker verification model (Voice ID)

Unlike the Mac fork, this is downloaded automatically by the installer
(§4, step 4) — no manual step needed. Just restart the service after
installing:

```bash
systemctl --user restart openclaw-realtimetalk
```

Then open `http://<pi-ip>:19000/voice-enroll` to enroll. If you're setting
this up outside the installer, or need to re-fetch the model by hand:

```bash
~/.local/realtimetalk-venv/bin/pip install sherpa-onnx
mkdir -p ~/.local/share/rtt/speaker
wget -O ~/.local/share/rtt/speaker/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx
# (the release tag really is spelled "recongition" upstream)
```

Enroll at `/voice-enroll` (3×5s samples: English, Chinese, free speech) —
read the phrase shown on the page, which now reflects your configured agent
name. Enable **Owner Only** from the dashboard, or say "only listen to me".

---

## 7. Verifying the install

1. `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:19000/dashboard` → `200`
2. Say your configured wake phrase near the mic → the daemon should respond
3. Check the log for a clean startup, no repeated errors:
   ```bash
   journalctl --user -u openclaw-realtimetalk -f
   ```
4. Confirm mic/speaker on the dashboard's device panel show real device names
5. `aplay -l` to cross-check ALSA sees your audio hardware at all

---

## 8. Radio Mode (optional)

Requires an AIOC or Digirig Mobile dongle connected via USB — both are
supported on Pi (Digirig is **not** supported on the Mac fork). Radio Mode
auto-enables within a few seconds of plugging in and auto-disables on
unplug, restoring your normal mic profile either way. See
[RADIO-INTERFACE.md](RADIO-INTERFACE.md) and the README's Radio Mode
section for Monitor/Playback/DTMF details and known limitations
(no hardware-PTT-line detection on AIOC hardware rev < v1.1, echo-loop risk
on repeaters, etc.).

---

## 9. Day-to-day control

```bash
cd ~/openclaw-RealTimeTalk
./RealTimeTalk-toggle.sh start      # systemctl --user start
./RealTimeTalk-toggle.sh stop       # systemctl --user stop
./RealTimeTalk-toggle.sh restart    # systemctl --user restart
./RealTimeTalk-toggle.sh status     # systemctl --user status
./RealTimeTalk-toggle.sh log        # journalctl --user -u ... -f
./RealTimeTalk-toggle.sh devices    # list audio devices the daemon can see
```

### Wiring OpenClaw up to push text for readout

RTT has a local-only `GET http://localhost:19000/speak?text=...` endpoint
(see README.md's ["Pushing text from OpenClaw (or any local
process)"](README.md#pushing-text-from-openclaw-or-any-local-process) for
full detail) that any process on this Pi can call to have RTT read
arbitrary text aloud — the piece that lets an OpenClaw agent finish a
keyboard-typed task and deliver the result through RTT instead of just
replying in text.

OpenClaw won't discover this on its own — add a note to the agent's
`TOOLS.md` (its OpenClaw workspace directory, per `agents.defaults.workspace`
in `~/.openclaw/openclaw.json`) so it knows the capability exists and when
to use it:

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

---

## 10. Troubleshooting

See the README's [Troubleshooting](README.md#troubleshooting) table for the
full list. Install-specific additions:

| Symptom | Likely cause / fix |
|---|---|
| Re-running the installer reset my agent name to `Zeebot` | Shouldn't happen — file a bug. It's designed to read the existing `--agent-name` (or fall back to `Five` for a pre-v3.11.0 install) as the prompt default. Check `~/.config/systemd/user/openclaw-realtimetalk.service` for what actually got written. |
| Wake phrase not recognised after renaming the agent | `_matches_phrase()` does exact-substring plus fuzzy (≥60% word overlap) matching — very short or generic names are more prone to false negatives/positives than a distinctive multi-syllable one. Prefer names like `Zeebot` or `Grogu` over single ambiguous syllables. |
| Saying "Hey Jarvis" doesn't fully activate the agent | Expected — see [§5](#5-agent-name--wake-phrase). It only wakes from deep sleep to Silent; you still need your configured `<name> wake up` phrase to activate. |
| `EXTRA_ARGS[@]`-style unbound variable during install | Not applicable here — that was a Mac-fork (bash 3.2 / macOS default shell) issue. Pi's installer targets `bash` from `apt`, which doesn't have this bug. |

---

## 11. Updating an existing install

```bash
cd ~/openclaw-RealTimeTalk
git pull
bash RealTimeTalk-install-pi.sh
```

Re-running the installer is the supported update path — it's idempotent,
picks up new Python dependencies, and (as of v3.11.0) preserves your
existing agent name/wake phrase rather than reprompting with new defaults.
