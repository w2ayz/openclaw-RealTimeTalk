# RealTimeTalk — Button Text & Hint Reference

All button labels, states, and hover hint texts as of v2.0.1.
Icons use BMP Unicode symbols (render on Pi/Chromium without a color emoji font).

---

## Dashboard (`/dashboard`)

### Header Row

| Symbol | Label | Hint text |
|--------|-------|-----------|
| ✏ | **Calibrate** | Open speaker & mic level calibration |

### Navigation Row

| Symbol | Label | State | Hint text |
|--------|-------|-------|-----------|
| ⚡ | **Wake** | — | Activate voice — the agent will listen and respond |
| ☾ | **Sleep** | — | Silence voice and stop monitoring. Say Hey Jarvis or press Wake to resume |
| ◎ | **Monitor** | inactive | Now: OFF. Click → start passive monitoring (transcribes without routing to agent) |
| ◎ | **Monitor On** | active (highlighted) | Now: Monitoring ON. Click → stop monitoring |
| ⊕ | **Multi-lang** | multilang = off (inactive) | Now: OFF — EN/ZH only, auto-sleep on. Click → EN/ZH mode (auto-sleep off) |
| ⊕ | **Lang: EN/ZH** | multilang = en-zh (highlighted) | Now: EN/ZH — auto-sleep off. Click → Whitelist (EN/ZH/KO/JA/ES/MS) |
| ⊕ | **Lang: List** | multilang = whitelist (highlighted) | Now: Whitelist — EN/ZH/KO/JA/ES/MS, auto-sleep off. Click → Any language |
| ⊕ | **Lang: Any** | multilang = any (highlighted) | Now: Any language — auto-sleep off. Click → OFF |
| ✖ | **Clear Log** | danger style | Clear the conversation log (does not affect the agent's memory) |
| ↻ | **Restart** | — | Restart the RealTimeTalk daemon (reconnects OpenAI and gateway) |
| ⚠ | **Gateway Reset** | danger style | Drop and reconnect the OpenClaw gateway WebSocket without restarting |

> **Multi-lang button** cycles through 4 states on each click: OFF → EN/ZH → Whitelist → Any → OFF. Label shows "Multi-lang" when off, or "Lang: EN/ZH" / "Lang: List" / "Lang: Any" when active. Button is highlighted whenever state is not OFF.  
> **Monitor button** is a 2-state toggle: Monitor ↔ Monitor On.

### State Pill (header, not clickable)

| State label | Background | Text color |
|-------------|-----------|------------|
| ACTIVE | #051a0e | #34d399 (green) |
| SILENT | #0e1420 | #64748b (slate) |
| THINKING | #130e02 | #f59e0b (amber) |
| SPEAKING | #021513 | #2dd4bf (teal) |
| PAUSED | #150d2e | #a5b4fc (lavender) |
| MONITORING | #071a2e | #60a5fa (blue) |
| SLEEP | #0e0e14 | #475569 (dark slate) |

### Banners (shown above conversation log)

| Condition | Banner text | Inline action button |
|-----------|-------------|----------------------|
| Agent speaking | ♪ Five is speaking… | ✕ Stop |
| Agent paused | ❚❚ Paused | ▶ Continue |

---

## Calibration Page (`/calibration`)

### Header / Footer

| Symbol | Label | Action |
|--------|-------|--------|
| ● | **Calibration** | (brand label, not clickable) |
| ← | **Dashboard** | Navigate back to `/dashboard` |

### Cal Mode Selector (top of page)

| Label | Description |
|-------|-------------|
| **Headset** | Force headset mode (mic + speaker on same device) |
| **Speaker** | Force speaker mode (external speaker, acoustic calibration available) |
| **Auto** | Auto-detect mode from device names |

### Device Panel (expand toggle)

| Label | Action |
|-------|--------|
| **Audio Devices** / **▼ expand** | Toggle visibility of audio device list |

### Speaker Manual Adjustment

| Label | Action |
|-------|--------|
| − Quieter | Decrease PipeWire volume by 10 |
| + Louder | Increase PipeWire volume by 10 |
| − Softer | Decrease software volume (SW) by 10% |
| + Louder | Increase software volume (SW) by 10% |
| **Play test** | Start looping test audio sentence |
| **Stop** | Stop looping test audio |
| **Set this level** | Save current Vol + SW as the calibrated level |

### Speaker Auto Calibration (Speaker mode only)

| Label | Action |
|-------|--------|
| **Run auto calibration** | Play 440 Hz tone sweep, measure mic leakage via FFT, set safe level |

### Mic Calibration

| Label | Action |
|-------|--------|
| Gate slider | Drag to set mic gate threshold (noise floor cutoff) |
| **Auto-calibrate (3 sec quiet)** | Record ambient noise for 3 sec and set gate automatically |

### Headset-only Calibration Page (legacy `/speaker-cal`)

| Label | Action |
|-------|--------|
| − Quieter / + Louder | Adjust volume |
| **Play test** | Start test loop |
| **Stop** | Stop test loop |
| ✓ **Set this level** | Save level and return to dashboard after 3 sec |
| **Check Device Status** | Fetch and display current audio device info |

---

## Device Bar (read-only display, not buttons)

```
◉ {mic name}   ♪ {speaker name} · Vol {vol} · SW {sw}% · Gate {gate} · Gain {gain}x
```

`◉` = mic input  
`♪` = speaker output

---

## Unicode Symbol Reference

| Symbol | Codepoint | Name | Used for |
|--------|-----------|------|----------|
| ⚡ | U+26A1 | Lightning | Wake button |
| ☾ | U+263E | Crescent Moon | Sleep button |
| ◎ | U+25CE | Bullseye | Monitor button |
| ⊕ | U+2295 | Circled Plus | Language button |
| ◉ | U+25C9 | Fisheye | Mic in device bar |
| ♪ | U+266A | Eighth Note | Speaker in device bar / Speaking banner |
| ✏ | U+270F | Pencil | Calibrate button |
| ✖ | U+2716 | Heavy Multiplication X | Clear Log / Stop TTS |
| ↻ | U+21BB | Clockwise Arrow | Restart |
| ⚠ | U+26A0 | Warning Sign | Gateway Reset |
| ● | U+25CF | Black Circle | Brand dot |
| ← | U+2190 | Leftward Arrow | Back to Dashboard |

All symbols are in the Basic Multilingual Plane (U+0000–U+FFFF) and render
on Chromium/Linux without a color emoji font installed.
