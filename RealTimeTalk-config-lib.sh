#!/usr/bin/env bash
# RealTimeTalk-config-lib.sh — shared interview functions for the STT/TTS/
# vocabulary setup steps, sourced by both RealTimeTalk-install-pi.sh (as part
# of a fresh install) and RTT-Config.sh (re-runnable anytime, no
# apt/venv/systemd-unit steps). Keeping this in one file means the two entry
# points can't drift out of sync with each other. Mirrors the Mac fork's
# RealTimeTalk-config-lib.sh in structure, adapted to this fork's idioms
# (plain echo with symbols, not ANSI colors — this installer never used
# them; PYTHON/OPENCLAW_CONFIG naming, not VENV_PY/OPENCLAW_JSON).
#
# Callers must set these before sourcing/calling into this file:
#   PYTHON          — path to the venv's python3 ($HOME/.local/realtimetalk-venv/bin/python)
#   OPENCLAW_CONFIG — path to ~/.openclaw/openclaw.json
#   STT_CFG         — path to ~/.openclaw/workspace/rtt_stt_config.json
#   TTS_CFG         — path to ~/.openclaw/workspace/rtt_tts_config.json
#   SKILL_DIR       — this skill's directory (for the "re-run this" hint text)

has_openai_key() {
    "$PYTHON" - "$OPENCLAW_CONFIG" <<'PYEOF'
import json, sys
sys.exit(0 if json.load(open(sys.argv[1])).get("talk", {}).get("providers", {}).get("openai", {}).get("apiKey", "") else 1)
PYEOF
}

has_gemini_key() {
    "$PYTHON" - "$OPENCLAW_CONFIG" <<'PYEOF'
import json, sys
sys.exit(0 if json.load(open(sys.argv[1])).get("talk", {}).get("providers", {}).get("gemini", {}).get("apiKey", "") else 1)
PYEOF
}

has_any_stt_key() {
    has_openai_key || has_gemini_key
}

# ensure_provider_key <provider> — prompts (hidden) if needed and writes the
# key to $OPENCLAW_CONFIG (openai/gemini/elevenlabs all live in
# talk.providers.<name>.apiKey as of v3.23.0 — elevenlabs used to be a flat
# ~/.openclaw/secrets/elevenlabs file; migrated to unify storage with the
# Mac fork). Checks the environment ($OPENAI_API_KEY/$GEMINI_API_KEY,
# falling back to $GOOGLE_API_KEY for gemini — Google's more common name for
# the same credential; $ELEVENLABS_API_KEY for elevenlabs) before prompting,
# since a fresh Pi can already have one exported for other tools. Returns 0
# if the provider has a usable key afterwards, 1 otherwise.
ensure_provider_key() {
    local prov="$1" prefix="^sk-"
    [ "$prov" = "gemini" ] && prefix="^AIza"
    [ "$prov" = "elevenlabs" ] && prefix=""
    local existing
    existing=$("$PYTHON" - "$OPENCLAW_CONFIG" "$prov" <<'PYEOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
k = cfg.get("talk", {}).get("providers", {}).get(sys.argv[2], {}).get("apiKey", "")
print("yes" if k else "no")
PYEOF
)
    local envvar="OPENAI_API_KEY" env_val="" src_var=""
    [ "$prov" = "gemini" ] && envvar="GEMINI_API_KEY"
    [ "$prov" = "elevenlabs" ] && envvar="ELEVENLABS_API_KEY"
    env_val="${!envvar:-}"
    src_var="$envvar"
    if [ -z "$env_val" ] && [ "$prov" = "gemini" ]; then
        env_val="${GOOGLE_API_KEY:-}"
        src_var="GOOGLE_API_KEY"
    fi

    local KEY=""
    if [ -n "$env_val" ]; then
        if [ "$existing" = "yes" ]; then
            read -rp "      Found \$$src_var in your environment, and $prov already has a configured key — use the environment value instead? [y/N]: " USE_ENV
            case "$USE_ENV" in [Yy]*) KEY="$env_val" ;; esac
        else
            read -rp "      Found \$$src_var in your environment — use it for $prov? [Y/n]: " USE_ENV
            case "${USE_ENV:-Y}" in [Yy]*) KEY="$env_val" ;; esac
        fi
    fi

    if [ -z "$KEY" ]; then
        if [ "$existing" = "yes" ]; then
            read -rp "      $prov key already configured — Enter to keep it, or paste a replacement: " KEY
            if [ -z "$KEY" ]; then echo "      ✓ kept existing $prov key"; return 0; fi
        else
            read -rsp "      Enter $prov API key (hidden input, Enter to skip): " KEY
            echo ""
            if [ -z "$KEY" ]; then
                echo "      → no $prov key entered — $prov will not be usable"
                return 1
            fi
        fi
    fi

    if [ -n "$prefix" ] && ! echo "$KEY" | grep -qE "$prefix"; then
        local reply
        read -rp "      ⚠ That doesn't look like a $prov key (expected ${prefix#^}...). Use it anyway? [y/N]: " reply
        case "$reply" in [Yy]|yes|Yes) ;; *) return 1 ;; esac
    fi

    # Best-effort live verification — warn-only so an offline install still works.
    local code
    code=$("$PYTHON" - "$prov" "$KEY" <<'PYEOF'
import sys, urllib.request, urllib.error
prov, key = sys.argv[1], sys.argv[2]
if prov == "openai":
    url, hdr = "https://api.openai.com/v1/models", {"Authorization": "Bearer " + key}
elif prov == "elevenlabs":
    url, hdr = "https://api.elevenlabs.io/v1/user", {"xi-api-key": key}
else:
    url, hdr = "https://generativelanguage.googleapis.com/v1beta/models", {"x-goog-api-key": key}
try:
    urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=10)
    print(200)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print(0)
PYEOF
)
    case "$code" in
        200)     echo "      ✓ $prov key verified against the provider API" ;;
        0)       echo "      → could not reach the $prov API to verify (offline?) — continuing" ;;
        401|403) echo "      ✗ $prov key was REJECTED by the provider (HTTP $code) — not saving it"
                 return 1 ;;
        *)       echo "      → provider returned HTTP $code — saving anyway (can re-run this later)" ;;
    esac
    "$PYTHON" - "$OPENCLAW_CONFIG" "$prov" "$KEY" <<'PYEOF'
import json, os, sys
path, prov, key = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = json.load(open(path))
cfg.setdefault("talk", {}).setdefault("providers", {}).setdefault(prov, {})["apiKey"] = key
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
os.chmod(path, 0o600)
PYEOF
    echo "      ✓ $prov key written to openclaw.json"
    return 0
}

# write_stt_engine <provider> <fallback-or-empty> — merges into rtt_stt_config.json
# instead of overwriting it outright, so a reinstall/reconfigure never wipes a
# "vocabulary" list the daemon seeded (_ensure_stt_config_seeded) or the user
# has since customized. provider "none" (no fallback) marks the STT-bypass,
# TTS-only choice — see run_stt_setup's Skip option.
write_stt_engine() {
    "$PYTHON" - "$1" "$2" <<PYEOF
import json, os, sys
provider, fallback = sys.argv[1], sys.argv[2]
path = "$STT_CFG"
os.makedirs(os.path.dirname(path), exist_ok=True)
cfg = {}
if os.path.isfile(path):
    try:
        with open(path) as f:
            existing = json.load(f)
        if isinstance(existing, dict):
            cfg = existing
    except Exception:
        pass
cfg["provider"] = provider
if fallback:
    cfg["fallback"] = fallback
else:
    cfg.pop("fallback", None)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
PYEOF
    echo "      ✓ STT engine → $1${2:+ (fallback: $2)} written to $STT_CFG"
}

# run_stt_setup — interview for which STT provider key(s) to use, or skip STT
# entirely for TTS-only (text-only) mode. Re-runnable: every choice defaults
# to "keep what's already there."
run_stt_setup() {
    echo "STT setup — live speech-to-text (mic wake word / voice commands)"
    if [ ! -f "$OPENCLAW_CONFIG" ]; then
        echo "      ⚠ $OPENCLAW_CONFIG not found — is OpenClaw installed and configured yet?"
        echo "        Re-run this once it exists."
        return 1
    fi
    echo "      STT providers currently configured in $OPENCLAW_CONFIG:"
    "$PYTHON" - "$OPENCLAW_CONFIG" <<'PYEOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
prov = cfg.get("talk", {}).get("providers", {})
for name in ("openai", "gemini"):
    k = (prov.get(name) or {}).get("apiKey", "")
    print(f"        {name:8s} {'configured' if k else '- not set'}")
PYEOF
    echo ""
    echo "      STT engine setup — which provider key(s) do you want to use?"
    echo "        [1] OpenAI Realtime        (regular sk-... API key — NOT the ChatGPT OAuth profile)"
    echo "        [2] Gemini Transcribe Live (Gemini API key, AIza...)"
    echo "        [3] Both                   (pick the default engine; the other becomes the fallback)"
    echo "        [4] Keep existing configuration"
    echo "        [5] Skip — no STT, TTS-only (text-only) mode. OpenClaw can still push text to"
    echo "            RealTimeTalk to read aloud (POST /speak) — there's just no mic/wake-word listening."
    echo "      (Enter to keep existing configuration; nothing here is final — re-run this anytime.)"
    while true; do
        read -rp "      Choice [1/2/3/4/5, Enter = keep existing]: " STT_CHOICE
        STT_CHOICE="${STT_CHOICE:-4}"
        case "$STT_CHOICE" in 1|2|3|4|5) break ;; esac
        echo "      → enter 1, 2, 3, 4 or 5"
    done

    case "$STT_CHOICE" in
        1)
            ensure_provider_key openai || true
            if has_openai_key; then
                if has_gemini_key; then write_stt_engine openai gemini; else write_stt_engine openai ""; fi
            fi
            ;;
        2)
            ensure_provider_key gemini || true
            if has_gemini_key; then
                if has_openai_key; then write_stt_engine gemini openai; else write_stt_engine gemini ""; fi
            fi
            ;;
        3)
            ensure_provider_key openai || true
            ensure_provider_key gemini || true
            if has_openai_key && has_gemini_key; then
                while true; do
                    read -rp "      Default STT engine [gemini/openai, Enter = openai]: " DEFAULT_ENGINE
                    DEFAULT_ENGINE="$(echo "${DEFAULT_ENGINE:-openai}" | tr '[:upper:]' '[:lower:]')"
                    case "$DEFAULT_ENGINE" in gemini|openai) break ;; esac
                    echo "      → enter 'gemini' or 'openai'"
                done
                if [ "$DEFAULT_ENGINE" = "gemini" ]; then write_stt_engine gemini openai
                else write_stt_engine openai gemini; fi
            else
                echo "      → 'Both' needs both keys — set the engine manually in $STT_CFG"
            fi
            ;;
        4) echo "      ✓ keeping existing configuration" ;;
        5)
            write_stt_engine none ""
            echo "      ✓ STT bypassed — RealTimeTalk will run TTS-only. OpenClaw can still speak via /speak."
            return 0
            ;;
    esac

    if [ "$STT_CHOICE" != "4" ] && ! has_any_stt_key; then
        echo "      → No STT provider key ended up configured (openai or gemini)."
        read -rp "      Continue anyway in TTS-only (no STT) mode? [Y/n]: " CONT_TEXT_ONLY
        case "$CONT_TEXT_ONLY" in [Nn]*)
            echo "      ✗ No STT key configured. Re-run this when you have one:"
            echo "        bash \"$SKILL_DIR/RTT-Config.sh\""
            return 1
            ;;
        esac
        write_stt_engine none ""
        echo "      ✓ STT bypassed — RealTimeTalk will run TTS-only."
    elif [ "$STT_CHOICE" != "4" ]; then
        echo "      ✓ STT provider key(s) ready"
    fi
}

# run_tts_setup — ElevenLabs key + reorderable/droppable TTS engine chain.
# v3.23.0: TTS_ORDER now applies uniformly to English and Chinese/mixed text
# alike (previously English always went straight to Piper) — see
# RealTimeTalk-daemon.py's _synthesize()/_resolve_tts_order().
run_tts_setup() {
    echo ""
    echo "TTS setup — voice output engines"
    echo "      ElevenLabs gives the best multilingual quality and is tried first by default."
    echo "      Skip it (Enter at the prompt) and the chain below just falls back automatically."
    ensure_provider_key elevenlabs || true

    local existing_order
    existing_order=$("$PYTHON" - <<PYEOF
import json
try:
    with open("$TTS_CFG") as f:
        order = json.load(f).get("order") or []
except Exception:
    order = []
print(",".join(order) if order else "elevenlabs,edge,openai,piper")
PYEOF
)
    echo ""
    echo "      TTS engine chain (tried in this order until one produces audio, for ALL text —"
    echo "      English included, not just Chinese/mixed):"
    echo "        Current: $existing_order"
    echo "        Known engines: elevenlabs, edge, openai, piper"
    echo "        Drop any you don't want, e.g. 'piper' alone to go back to fully offline/local."
    echo "        'piper' is always kept as a last-resort fallback even if you leave it out —"
    echo "        it's the only engine that needs no key or network."
    local NEW_ORDER
    while true; do
        read -rp "      TTS order [Enter to keep current: $existing_order]: " NEW_ORDER
        NEW_ORDER="${NEW_ORDER:-$existing_order}"
        if "$PYTHON" - "$NEW_ORDER" <<'PYEOF'
import sys
known = {"elevenlabs", "edge", "openai", "piper"}
terms = [t.strip().lower() for t in sys.argv[1].split(",") if t.strip()]
bad = [t for t in terms if t not in known]
sys.exit(1 if (bad or not terms) else 0)
PYEOF
        then
            break
        fi
        echo "      → use only: elevenlabs, edge, openai, piper (comma-separated)"
    done

    local SAVED_ORDER
    SAVED_ORDER=$("$PYTHON" - "$NEW_ORDER" <<PYEOF
import json
import sys
terms = []
for t in sys.argv[1].split(","):
    t = t.strip().lower()
    if t and t not in terms:
        terms.append(t)
if "piper" not in terms:
    terms.append("piper")
try:
    with open("$TTS_CFG") as f:
        cfg = json.load(f)
except Exception:
    cfg = {}
cfg["order"] = terms
json.dump(cfg, open("$TTS_CFG", "w"), indent=2)
print(",".join(terms))
PYEOF
)
    echo "      ✓ TTS order saved to $TTS_CFG: $SAVED_ORDER"
}

# run_vocabulary_setup — review/extend the STT custom-vocabulary hint list.
run_vocabulary_setup() {
    echo ""
    echo "STT vocabulary — words the speech engine should recognize better"
    echo "      Seeded by default with the agent name plus OpenClaw/RealTimeTalk/RTT/STT/TTS."
    echo "      Add more any time: names, place names, call signs, jargon — it's a hint, not a"
    echo "      guarantee (an unusual term can still come through imperfectly)."
    local current
    current=$("$PYTHON" - <<PYEOF
import json
try:
    with open("$STT_CFG") as f:
        vocab = json.load(f).get("vocabulary") or []
except Exception:
    vocab = []
print(", ".join(vocab) if vocab else "(none yet — seeded automatically on first daemon start)")
PYEOF
)
    echo "      Current vocabulary: $current"
    read -rp "      Add extra words (comma-separated, Enter to skip): " NEW_TERMS
    if [ -z "$NEW_TERMS" ]; then
        echo "      ✓ vocabulary unchanged"
        return 0
    fi
    "$PYTHON" - "$NEW_TERMS" <<PYEOF
import json, os, sys
raw = sys.argv[1]
path = "$STT_CFG"
os.makedirs(os.path.dirname(path), exist_ok=True)
cfg = {}
if os.path.isfile(path):
    try:
        existing = json.load(open(path))
        if isinstance(existing, dict):
            cfg = existing
    except Exception:
        pass
vocab = cfg.get("vocabulary") or []
added = [t.strip() for t in raw.split(",") if t.strip()]
cfg["vocabulary"] = list(dict.fromkeys(vocab + added))
json.dump(cfg, open(path, "w"), indent=2)
print(", ".join(cfg["vocabulary"]))
PYEOF
    echo "      ✓ vocabulary updated in $STT_CFG"
}
