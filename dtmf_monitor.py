#!/usr/bin/env python3
"""
AIOC Real-Time DTMF Monitor
COS detection via raw AIOC audio level (pacat direct, bypasses AGC).
DTMF decoding via pacat|sox|multimon-ng.
Usage: python3 dtmf_monitor.py [--wake 123] [--sleep 321]

COS method: raw AIOC source level (squelch closed ~120, open ~12000+).
Serial DCD and HID VCOS are non-functional in AIOC firmware v1.0.
"""
import subprocess, threading, time, re, sys, argparse, numpy as np

# ── Config ─────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument('--wake',  default='123')
parser.add_argument('--sleep', default='321')
parser.add_argument('--cos-threshold', type=int, default=200,
                    help='Raw int16 peak threshold for COS open (closed~120, sustained carrier~300-450, default 200)')
parser.add_argument('--cos-tail', type=float, default=0.5,
                    help='Seconds to hold COS open after signal drops (default 0.5)')
args = parser.parse_args()

WAKE_SEQ       = args.wake
SLEEP_SEQ      = args.sleep
COS_THRESHOLD  = args.cos_threshold   # raw int16 peak; closed~120, open~12000+
COS_TAIL_S     = args.cos_tail
SEQ_TIMEOUT    = 8.0
DIGIT_COOLDOWN = 0.4

# ── Shared state ───────────────────────────────────────────────────────────
state = {
    'cos': False, 'level': 0,
    'digits': [],          # list of (time, digit) tuples
    'seq': '',
    'last_digit': None, 'last_time': 0.0,
    'actions': [],         # log of wake/sleep events
}
lock = threading.Lock()
cos_until = [0.0]

def find_aioc_source():
    try:
        out = subprocess.run(["pactl","list","short","sources"],
                             capture_output=True, text=True).stdout
        for l in out.splitlines():
            if ("AIOC" in l or "All-In-One" in l) and "monitor" not in l:
                return l.split()[1]
    except: pass
    return None

# ── COS thread: raw AIOC audio level via pacat ────────────────────────────
# Reads directly from the raw AIOC source (bypasses WebRTC AGC).
# Raw levels: squelch closed ~120 int16, squelch open ~12000+  → unambiguous.
# Serial DCD and HID VCOS are non-functional in AIOC firmware v1.0.
def cos_thread():
    CHUNK_BYTES = 48000 * 2 * 50 // 1000   # 50ms of s16le at 48kHz = 4800 bytes
    while True:
        src = find_aioc_source()
        if not src:
            time.sleep(2); continue
        try:
            proc = subprocess.Popen(
                ["pacat","--record","--raw","--format=s16le",
                 "--rate=48000","--channels=1","--latency-msec=50",
                 f"--device={src}"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            buf = b""
            while True:
                chunk = proc.stdout.read(CHUNK_BYTES)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= CHUNK_BYTES:
                    frame = np.frombuffer(buf[:CHUNK_BYTES], dtype=np.int16)
                    buf = buf[CHUNK_BYTES:]
                    peak = int(np.max(np.abs(frame)))
                    now = time.time()
                    with lock:
                        state['level'] = peak
                        if peak > COS_THRESHOLD:
                            cos_until[0] = now + COS_TAIL_S
                        state['cos'] = now < cos_until[0]
        except Exception:
            pass
        finally:
            try: proc.kill()
            except: pass
        time.sleep(1)

# ── DTMF thread: pacat|sox|multimon-ng ────────────────────────────────────
def dtmf_thread():
    pat = re.compile(r'DTMF:\s*([0-9A-D*#])')
    while True:
        src = find_aioc_source()
        if not src:
            time.sleep(2); continue
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
                m = pat.search(line.decode(errors='ignore'))
                if not m: continue
                digit = m.group(1)
                now = time.time()
                with lock:
                    if not state['cos']:
                        continue  # gate: only accept when COS open
                    if digit == state['last_digit'] and now - state['last_time'] < DIGIT_COOLDOWN:
                        continue
                    if state['seq'] and now - state['last_time'] > SEQ_TIMEOUT:
                        state['seq'] = ""
                    state['last_digit'] = digit
                    state['last_time'] = now
                    if not state['seq'] or state['seq'][-1] != digit:
                        state['seq'] += digit
                        state['digits'].append((now, digit))
                    ml = max(len(WAKE_SEQ), len(SLEEP_SEQ))
                    if len(state['seq']) > ml:
                        state['seq'] = state['seq'][-ml:]
                    # Check sequences
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
        except Exception:
            pass
        finally:
            for p in (mmng, sox, pacat):
                try: p.kill()
                except: pass
        time.sleep(1)

# ── Display: simple single-line update ────────────────────────────────────
def display_thread():
    while True:
        with lock:
            cos   = state['cos']
            level = state['level']
            seq   = state['seq']
            digs  = [d for t,d in state['digits'] if time.time()-t < 10]

        bar_n = min(level * 20 // 2000, 20)
        bar   = "█"*bar_n + "░"*(20-bar_n)
        cos_s = "\033[32mOPEN  \033[0m" if cos else "\033[31mCLOSED\033[0m"
        dig_s = " ".join(digs[-8:]) if digs else "-"
        seq_s = " ".join(seq) if seq else "_"

        line = (f"\r  COS:{cos_s} [{bar}] {level:5d} | "
                f"DTMF: {dig_s:<16} | Seq: {seq_s:<6}")
        sys.stdout.write(line)
        sys.stdout.flush()
        time.sleep(0.1)

# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    src = find_aioc_source()
    if not src:
        print("AIOC not connected — plug in cable and retry"); sys.exit(1)

    print(f"AIOC DTMF Monitor")
    print(f"Source : {src}")
    print(f"Wake   : {WAKE_SEQ}  |  Sleep: {SLEEP_SEQ}")
    print(f"COS threshold: {COS_THRESHOLD}  |  Tail: {COS_TAIL_S}s")
    print("─"*60)
    print("  COS      Level Bar       Level | DTMF digits     | Seq")
    print("─"*60)

    threading.Thread(target=cos_thread,     daemon=True).start()
    threading.Thread(target=dtmf_thread,    daemon=True).start()
    threading.Thread(target=display_thread, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\nStopped.")
