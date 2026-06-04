#!/usr/bin/env python3
"""
AIOC Real-Time DTMF Monitor with training mode.

Normal:   python3 dtmf_monitor.py
Training: python3 dtmf_monitor.py --train
Profiles: ~/.config/rtt/dtmf_profiles.json

COS detection via raw AIOC audio (pacat, bypasses AGC).
Detection: custom Goertzel with learned profiles, or multimon-ng fallback.
"""
import subprocess, threading, time, re, sys, argparse, os, json, numpy as np
from pathlib import Path
from collections import defaultdict

# ── Config ─────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument('--train',         action='store_true', help='Enter training mode')
parser.add_argument('--digits',        default='1234567890*#', help='Digits to train (default all)')
parser.add_argument('--samples',       type=int, default=5,   help='Samples per digit in training (default 5)')
parser.add_argument('--wake',          default='123')
parser.add_argument('--sleep',         default='321')
parser.add_argument('--cos-threshold', type=int,   default=200,  help='Raw int16 COS threshold (default 200)')
parser.add_argument('--cos-tail',      type=float, default=0.5,  help='COS hold-open seconds (default 0.5)')
parser.add_argument('--profiles',      default=os.path.expanduser('~/.config/rtt/dtmf_profiles.json'))
args = parser.parse_args()

WAKE_SEQ      = args.wake
SLEEP_SEQ     = args.sleep
COS_THRESHOLD = args.cos_threshold
COS_TAIL_S    = args.cos_tail
SEQ_TIMEOUT   = 8.0
DIGIT_COOLDOWN= 0.4
PROFILE_FILE  = args.profiles
RATE          = 48000
CHUNK_BYTES   = RATE * 2 * 50 // 1000   # 50ms s16le

# Standard DTMF frequencies (fallback)
STD_ROWS = [697, 770, 852, 941]
STD_COLS = [1209, 1336, 1477, 1633]
STD_MAP  = {
    (697,1209):'1',(697,1336):'2',(697,1477):'3',(697,1633):'A',
    (770,1209):'4',(770,1336):'5',(770,1477):'6',(770,1633):'B',
    (852,1209):'7',(852,1336):'8',(852,1477):'9',(852,1633):'C',
    (941,1209):'*',(941,1336):'0',(941,1477):'#',(941,1633):'D',
}

# ── Profile I/O ────────────────────────────────────────────────────────────
def load_profiles():
    try:
        with open(PROFILE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_profiles(profiles):
    os.makedirs(os.path.dirname(PROFILE_FILE), exist_ok=True)
    with open(PROFILE_FILE, 'w') as f:
        json.dump(profiles, f, indent=2)
    print(f"\n  Profiles saved → {PROFILE_FILE}")

# ── Goertzel ───────────────────────────────────────────────────────────────
def goertzel_energy(samples, freq, rate):
    n = len(samples)
    k = int(0.5 + n * freq / rate)
    w = 2 * np.pi * k / n
    c = 2 * np.cos(w)
    q1 = q2 = 0.0
    for s in samples:
        q0 = s + c*q1 - q2; q2=q1; q1=q0
    return q2*q2 + q1*q1 - c*q1*q2

def decode_with_profiles(frame, profiles):
    """Decode a DTMF digit using learned frequency profiles."""
    samples = frame.astype(np.float64).tolist()
    scores = {}
    for digit, prof in profiles.items():
        row_e = goertzel_energy(samples, prof['row_hz'], RATE)
        col_e = goertzel_energy(samples, prof['col_hz'], RATE)
        scores[digit] = row_e + col_e
    if not scores:
        return None
    best = max(scores, key=scores.get)
    best_e = scores[best]
    # Must be significantly above median
    median_e = sorted(scores.values())[len(scores)//2]
    if best_e < 1e6 or (median_e > 0 and best_e / median_e < 3.0):
        return None
    return best

def decode_frame_fft(frame):
    """Extract dominant row + col frequencies via FFT."""
    f = frame.astype(np.float64)
    fft = np.abs(np.fft.rfft(f))
    freqs = np.fft.rfftfreq(len(f), 1/RATE)
    # Row band 600-1000 Hz
    row_mask = (freqs >= 600) & (freqs <= 1000)
    col_mask = (freqs >= 1100) & (freqs <= 1700)
    if not row_mask.any() or not col_mask.any():
        return None, None
    row_freq = freqs[row_mask][np.argmax(fft[row_mask])]
    col_freq = freqs[col_mask][np.argmax(fft[col_mask])]
    row_e = np.max(fft[row_mask])
    col_e = np.max(fft[col_mask])
    if row_e < 100 or col_e < 100:
        return None, None
    return float(row_freq), float(col_freq)

# ── AIOC source finder ─────────────────────────────────────────────────────
def find_aioc_source():
    try:
        out = subprocess.run(["pactl","list","short","sources"],
                             capture_output=True, text=True).stdout
        for l in out.splitlines():
            if ("AIOC" in l or "All-In-One" in l) and "monitor" not in l:
                return l.split()[1]
    except: pass
    return None

# ── Shared state ───────────────────────────────────────────────────────────
state = {'cos':False,'level':0,'digits':[],'seq':'','last_digit':None,
         'last_time':0.0,'actions':[]}
lock = threading.Lock()
cos_until = [0.0]
raw_buf = []
raw_lock = threading.Lock()

# ── Raw audio capture (shared by COS + training) ──────────────────────────
_pacat_proc = [None]

def raw_capture_thread(src):
    global _pacat_proc
    while True:
        if not src:
            src = find_aioc_source()
            if not src: time.sleep(2); continue
        try:
            proc = subprocess.Popen(
                ["pacat","--record","--raw","--format=s16le",
                 "--rate=48000","--channels=1","--latency-msec=50",
                 f"--device={src}"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            _pacat_proc[0] = proc
            buf = b""
            while True:
                chunk = proc.stdout.read(CHUNK_BYTES)
                if not chunk: break
                buf += chunk
                while len(buf) >= CHUNK_BYTES:
                    frame_bytes = buf[:CHUNK_BYTES]; buf=buf[CHUNK_BYTES:]
                    frame = np.frombuffer(frame_bytes, dtype=np.int16)
                    peak  = int(np.max(np.abs(frame)))
                    now   = time.time()
                    with lock:
                        state['level'] = peak
                        if peak > COS_THRESHOLD: cos_until[0] = now + COS_TAIL_S
                        state['cos'] = now < cos_until[0]
                    with raw_lock:
                        raw_buf.append((now, frame.copy()))
                        # Keep 10s of audio
                        cutoff = now - 10
                        while raw_buf and raw_buf[0][0] < cutoff:
                            raw_buf.pop(0)
        except Exception: pass
        finally:
            try: proc.kill()
            except: pass
            _pacat_proc[0] = None
        time.sleep(1)

# ── DTMF detection thread ──────────────────────────────────────────────────
def dtmf_thread(profiles):
    pat = re.compile(r'DTMF:\s*([0-9A-D*#])')
    use_profiles = bool(profiles)

    if use_profiles:
        # Custom Goertzel loop using learned profiles
        last_frame_time = [0.0]
        FRAME = RATE // 10  # 100ms analysis window
        prev_digit = [None]
        hold = [0]
        while True:
            time.sleep(0.025)
            if not state['cos']:
                prev_digit[0] = None; hold[0] = 0; continue
            with raw_lock:
                recent = [(t,f) for t,f in raw_buf if t > time.time()-0.15]
            if not recent: continue
            frames = np.concatenate([f for _,f in recent])
            if len(frames) < FRAME: continue
            digit = decode_with_profiles(frames[-FRAME:], profiles)
            if digit == prev_digit[0]:
                hold[0] += 1
            else:
                prev_digit[0] = digit; hold[0] = 1
            if digit and hold[0] == 3:  # stable for ~75ms
                _accept_digit(digit)
    else:
        # Fallback: multimon-ng
        while True:
            src = find_aioc_source()
            if not src: time.sleep(2); continue
            try:
                pacat = subprocess.Popen(
                    ["pacat","--record","--raw","--format=s16le",
                     "--rate=48000","--channels=1","--latency-msec=100",
                     f"--device={src}"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                sox = subprocess.Popen(
                    ["sox","-t","raw","-r","48000","-e","signed-integer","-b","16","-c","1","-",
                     "-t","raw","-r","22050","-","highpass","200"],
                    stdin=pacat.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                mmng = subprocess.Popen(
                    ["multimon-ng","-a","DTMF","-t","raw","-"],
                    stdin=sox.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                pacat.stdout.close(); sox.stdout.close()
                for line in mmng.stdout:
                    if not state['cos']: continue
                    m = pat.search(line.decode(errors='ignore'))
                    if m: _accept_digit(m.group(1))
            except Exception: pass
            finally:
                for p in (mmng, sox, pacat):
                    try: p.kill()
                    except: pass
            time.sleep(1)

def _accept_digit(digit):
    now = time.time()
    with lock:
        if digit == state['last_digit'] and now-state['last_time'] < DIGIT_COOLDOWN: return
        if state['seq'] and now-state['last_time'] > SEQ_TIMEOUT: state['seq'] = ""
        state['last_digit'] = digit; state['last_time'] = now
        if not state['seq'] or state['seq'][-1] != digit:
            state['seq'] += digit
            state['digits'].append((now, digit))
        ml = max(len(WAKE_SEQ), len(SLEEP_SEQ))
        if len(state['seq']) > ml: state['seq'] = state['seq'][-ml:]
        if WAKE_SEQ in state['seq']:
            state['seq'] = ""
            msg = f"[{time.strftime('%H:%M:%S')}] *** WAKE '{WAKE_SEQ}' detected!"
            state['actions'].append(msg)
            print(f"\n\033[32m{msg}\033[0m")
        elif SLEEP_SEQ in state['seq']:
            state['seq'] = ""
            msg = f"[{time.strftime('%H:%M:%S')}] *** SLEEP '{SLEEP_SEQ}' detected!"
            state['actions'].append(msg)
            print(f"\n\033[33m{msg}\033[0m")

# ── Display thread ─────────────────────────────────────────────────────────
def display_thread(profiles):
    mode_str = ("\033[32m[LEARNED]\033[0m" if profiles
                else "\033[33m[STANDARD multimon-ng]\033[0m")
    while True:
        with lock:
            cos=state['cos']; level=state['level']
            digs=[d for t,d in state['digits'] if time.time()-t<10]
            seq=state['seq']
        bar_n=min(level*20//2000,20)
        bar="█"*bar_n+"░"*(20-bar_n)
        cos_s="\033[32mOPEN  \033[0m" if cos else "\033[31mCLOSED\033[0m"
        dig_s=" ".join(digs[-8:]) if digs else "-"
        seq_s=" ".join(seq) if seq else "_"
        sys.stdout.write(
            f"\r  COS:{cos_s}[{bar}]{level:6d} | "
            f"DTMF:{dig_s:<16}| Seq:{seq_s:<6} {mode_str}   ")
        sys.stdout.flush()
        time.sleep(0.1)

# ══════════════════════════════════════════════════════════════════════════
# TRAINING MODE
# ══════════════════════════════════════════════════════════════════════════
def run_training():
    src = find_aioc_source()
    if not src:
        print("AIOC not connected."); sys.exit(1)

    profiles = load_profiles()
    digits   = list(args.digits)
    needed   = args.samples

    print(f"\n╔══════════════════════════════════════════════╗")
    print(f"║          DTMF TRAINING MODE                 ║")
    print(f"║  Digits to train : {args.digits:<26}║")
    print(f"║  Samples needed  : {needed:<26}║")
    print(f"║  Profile file    : {PROFILE_FILE[-26:]:<26}║")
    print(f"╚══════════════════════════════════════════════╝\n")

    # Start raw audio capture
    threading.Thread(target=raw_capture_thread, args=(src,), daemon=True).start()
    time.sleep(1)

    for digit in digits:
        samples_row = []
        samples_col = []
        print(f"\n── Digit  \033[93m{digit}\033[0m  ─────────────────────────────────")
        print(f"   Transmit DTMF {digit} at various durations ({needed} times)")
        if digit in profiles:
            p = profiles[digit]
            print(f"   (existing: row={p['row_hz']:.0f}Hz col={p['col_hz']:.0f}Hz "
                  f"n={p['samples']})")
        print()

        burst_active = False
        burst_frames = []
        last_cos = False
        collected = 0

        while collected < needed:
            time.sleep(0.05)
            cos  = state['cos']
            lvl  = state['level']

            # Print live level bar
            bar_n = min(lvl*30//5000, 30)
            cos_s = "\033[32m●\033[0m" if cos else "○"
            sys.stdout.write(
                f"\r  {cos_s} [{('█'*bar_n).ljust(30,'░')}] {lvl:6d}  "
                f"collected {collected}/{needed}   ")
            sys.stdout.flush()

            if cos and not last_cos:
                # COS rising edge — start collecting burst
                burst_active = True
                burst_frames = []
            if burst_active and cos:
                with raw_lock:
                    burst_frames += [f for t,f in raw_buf if t > time.time()-0.05]
            if not cos and last_cos and burst_active:
                # COS falling edge — analyse burst
                burst_active = False
                if burst_frames:
                    full = np.concatenate(burst_frames)
                    # Use middle 60% to avoid key-up/down transients
                    trim = len(full)//5
                    mid  = full[trim:-trim] if len(full) > trim*3 else full
                    if len(mid) > RATE//20:
                        row_hz, col_hz = decode_frame_fft(mid)
                        if row_hz and col_hz:
                            samples_row.append(row_hz)
                            samples_col.append(col_hz)
                            collected += 1
                            sys.stdout.write(
                                f"\r  ✓ Sample {collected}: "
                                f"row={row_hz:.0f}Hz  col={col_hz:.0f}Hz          \n")
                            sys.stdout.flush()
            last_cos = cos

        # Average and store
        avg_row = float(np.median(samples_row))
        avg_col = float(np.median(samples_col))
        profiles[digit] = {
            'row_hz':  round(avg_row, 1),
            'col_hz':  round(avg_col, 1),
            'samples': collected,
        }
        std = STD_MAP.get((min(STD_ROWS, key=lambda r: abs(r-avg_row)),
                           min(STD_COLS, key=lambda c: abs(c-avg_col))), '?')
        print(f"\n  ✓ {digit}  row={avg_row:.1f}Hz  col={avg_col:.1f}Hz  "
              f"(std DTMF={std})  n={collected}")

    save_profiles(profiles)
    print("\n\033[32mTraining complete!\033[0m")
    print(f"Run without --train to use learned profiles.\n")

# ══════════════════════════════════════════════════════════════════════════
# NORMAL MONITOR MODE
# ══════════════════════════════════════════════════════════════════════════
def run_monitor():
    src = find_aioc_source()
    if not src:
        print("AIOC not connected."); sys.exit(1)

    profiles = load_profiles()
    mode = "LEARNED PROFILES" if profiles else "STANDARD (multimon-ng fallback)"

    print(f"AIOC DTMF Monitor")
    print(f"Source  : {src}")
    print(f"Mode    : {mode}")
    if profiles:
        print(f"Profiles: {len(profiles)} digits trained")
    print(f"Wake={WAKE_SEQ}  Sleep={SLEEP_SEQ}  COS≥{COS_THRESHOLD}")
    print("─"*60)

    threading.Thread(target=raw_capture_thread, args=(src,), daemon=True).start()
    threading.Thread(target=dtmf_thread,        args=(profiles,), daemon=True).start()
    threading.Thread(target=display_thread,     args=(profiles,), daemon=True).start()
    time.sleep(0.5)

    import tty, termios, select as _sel
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            if _sel.select([sys.stdin], [], [], 0.1)[0]:
                ch = sys.stdin.read(1)
                if ch in ('\x1b', 'q', 'Q'):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print("\n\nStopped.")

# ── Entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if args.train:
        run_training()
    else:
        run_monitor()
