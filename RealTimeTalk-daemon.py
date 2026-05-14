#!/usr/bin/env python3
"""
RealTimeTalk-daemon.py — OpenClaw headless RealTime Talk daemon.

Connects directly to the OpenAI Realtime API via WebSocket.
No browser or display required — designed for Raspberry Pi.

Reads the OpenAI API key from ~/.openclaw/openclaw.json.

Stop via:
  http://<pi-ip>:18790/stop          — phone browser (over Tailscale)
  systemctl --user stop openclaw-realtimetalk  — SSH
  SIGTERM / Ctrl-C

Usage:
  python3 RealTimeTalk-daemon.py [options]
  python3 RealTimeTalk-daemon.py --list-devices
  python3 RealTimeTalk-daemon.py --input-device 2 --output-device 0

Requires:
  pip install "websockets>=12" sounddevice numpy
  sudo apt install libportaudio2      # Raspberry Pi OS
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import sounddevice as sd
import websockets

# ── Constants ─────────────────────────────────────────────────────────────────

OPENCLAW_CONFIG  = os.path.expanduser("~/.openclaw/openclaw.json")
OPENAI_MODEL     = "gpt-4o-realtime-preview"
OPENAI_WS_URL    = f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"
SAMPLE_RATE      = 24000        # OpenAI Realtime API rate
DEVICE_RATE      = 48000        # Hardware rate (USB mic + speaker)
RESAMPLE_RATIO   = DEVICE_RATE // SAMPLE_RATE   # 2
CHANNELS         = 1
BLOCKSIZE        = 2400         # 100 ms at 24 kHz (API frames)
DEVICE_BLOCKSIZE = BLOCKSIZE * RESAMPLE_RATIO   # 4800 hardware frames
DEFAULT_HTTP_PORT = 18790
RECONNECT_DELAY   = 5    # seconds between reconnect attempts

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RTT] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("RealTimeTalk")


# ── Config ────────────────────────────────────────────────────────────────────

def load_openai_key() -> str:
    with open(OPENCLAW_CONFIG) as f:
        cfg = json.load(f)
    key = (
        cfg.get("talk", {})
           .get("providers", {})
           .get("openai", {})
           .get("apiKey", "")
    )
    # Resolve OpenClaw SecretRef: {"source":"file","provider":"...","id":"/a/b/c"}
    if isinstance(key, dict) and key.get("source") == "file":
        provider_name = key.get("provider", "")
        secret_path = (
            cfg.get("secrets", {})
               .get("providers", {})
               .get(provider_name, {})
               .get("path", "")
        )
        secret_path = os.path.expanduser(secret_path)
        with open(secret_path) as sf:
            secrets = json.load(sf)
        # Navigate id path: "/providers/openai/apiKey" → secrets["providers"]["openai"]["apiKey"]
        parts = [p for p in key.get("id", "").split("/") if p]
        for part in parts:
            secrets = secrets[part]
        key = secrets
    if not key:
        raise RuntimeError(
            "No OpenAI API key at talk.providers.openai.apiKey in openclaw.json"
        )
    return key


# ── Thread-safe audio output buffer ──────────────────────────────────────────

class AudioOutputBuffer:
    """Accumulates PCM16 bytes from the WebSocket; drained by the PortAudio output callback."""

    def __init__(self):
        self._buf  = bytearray()
        self._lock = threading.Lock()

    def write(self, data: bytes):
        with self._lock:
            self._buf.extend(data)

    def read(self, n_frames: int) -> np.ndarray:
        n_bytes = n_frames * 2  # int16 = 2 bytes per sample
        with self._lock:
            if len(self._buf) >= n_bytes:
                out = bytes(self._buf[:n_bytes])
                del self._buf[:n_bytes]
            else:
                # Not enough data yet — pad with silence
                out = bytes(self._buf) + bytes(n_bytes - len(self._buf))
                self._buf.clear()
        return np.frombuffer(out, dtype=np.int16)


# ── Talk session ──────────────────────────────────────────────────────────────

class TalkSession:
    def __init__(self, api_key: str, loop: asyncio.AbstractEventLoop,
                 input_device=None, output_device=None):
        self.api_key       = api_key
        self.loop          = loop
        self.input_device  = input_device
        self.output_device = output_device
        self._mic_q  = asyncio.Queue(maxsize=200)
        self._spk_buf = AudioOutputBuffer()
        self._stop   = asyncio.Event()

    def stop(self):
        self.loop.call_soon_threadsafe(self._stop.set)

    # ── PortAudio callbacks (run in PortAudio thread) ─────────────────────────

    def _mic_cb(self, indata, frames, time_info, status):
        # Decimate DEVICE_RATE→SAMPLE_RATE by taking every RESAMPLE_RATIO-th sample
        decimated = indata[::RESAMPLE_RATIO, 0].tobytes()
        try:
            self.loop.call_soon_threadsafe(self._mic_q.put_nowait, decimated)
        except asyncio.QueueFull:
            pass

    def _spk_cb(self, outdata, frames, time_info, status):
        # Read API-rate frames then upsample to device rate via sample-and-hold
        api_frames = frames // RESAMPLE_RATIO
        pcm_24k = self._spk_buf.read(api_frames)
        outdata[:, 0] = np.repeat(pcm_24k, RESAMPLE_RATIO)

    # ── WebSocket send / receive ──────────────────────────────────────────────

    async def _send_mic(self, ws):
        while not self._stop.is_set():
            try:
                chunk = await asyncio.wait_for(self._mic_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            await ws.send(json.dumps({
                "type":  "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode(),
            }))

    async def _recv_ws(self, ws):
        async for raw in ws:
            if self._stop.is_set():
                break
            msg = json.loads(raw)
            t   = msg.get("type", "")

            if t == "response.audio.delta":
                self._spk_buf.write(base64.b64decode(msg["delta"]))

            elif t == "conversation.item.input_audio_transcription.completed":
                text = msg.get("transcript", "").strip()
                if text:
                    log.info(f"You: {text}")

            elif t == "response.done":
                for item in msg.get("response", {}).get("output", []):
                    for c in item.get("content", []):
                        if c.get("type") == "text":
                            log.info(f"AI: {c['text'].strip()}")

            elif t == "error":
                log.error(f"OpenAI error: {msg.get('error', msg)}")

    # ── Session runner ────────────────────────────────────────────────────────

    async def run(self):
        log.info("Connecting to OpenAI Realtime API…")
        async with websockets.connect(
            OPENAI_WS_URL,
            additional_headers={
                "Authorization": f"Bearer {self.api_key}",
                "OpenAI-Beta":   "realtime=v1",
            },
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "modalities":            ["text", "audio"],
                    "input_audio_format":    "pcm16",
                    "output_audio_format":   "pcm16",
                    "turn_detection": {
                        "type":                "server_vad",
                        "threshold":           0.5,
                        "prefix_padding_ms":   300,
                        "silence_duration_ms": 800,
                    },
                    "input_audio_transcription": {"model": "whisper-1"},
                },
            }))
            log.info("Session active — speak now")

            in_stream  = sd.InputStream(
                samplerate=DEVICE_RATE, channels=CHANNELS, dtype="int16",
                blocksize=DEVICE_BLOCKSIZE, callback=self._mic_cb,
                device=self.input_device,
            )
            out_stream = sd.OutputStream(
                samplerate=DEVICE_RATE, channels=CHANNELS, dtype="int16",
                blocksize=DEVICE_BLOCKSIZE, callback=self._spk_cb,
                device=self.output_device,
            )

            with in_stream, out_stream:
                tasks = [
                    asyncio.create_task(self._send_mic(ws)),
                    asyncio.create_task(self._recv_ws(ws)),
                    asyncio.create_task(self._stop.wait()),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()


# ── HTTP toggle server ────────────────────────────────────────────────────────

def start_http_server(port: int, on_stop):
    """Serves /stop and /status in a daemon thread."""

    def _html(handler, code: int, body: str):
        data = body.encode()
        handler.send_response(code)
        handler.send_header("Content-Type",   "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("[http] %s", fmt % args)

        def do_GET(self):
            if self.path in ("/stop", "/toggle"):
                _html(self, 200, "<h2>OpenClaw RealTimeTalk: stopping…</h2>")
                on_stop()
            elif self.path == "/status":
                body = json.dumps({"status": "running"}).encode()
                self.send_response(200)
                self.send_header("Content-Type",   "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                _html(self, 404, "<h2>Not found</h2>")

    server = HTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Toggle: http://<pi-ip>:%d/stop  |  /status", port)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(http_port: int, input_device=None, output_device=None):
    loop       = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        api_key = load_openai_key()
    except Exception as e:
        log.error(str(e))
        sys.exit(1)

    start_http_server(http_port, lambda: loop.call_soon_threadsafe(stop_event.set))
    log.info("OpenClaw RealTimeTalk daemon starting")

    while not stop_event.is_set():
        session = TalkSession(api_key, loop, input_device, output_device)

        # Propagate global stop → session stop
        async def _watch_stop():
            await stop_event.wait()
            session.stop()

        watcher = asyncio.create_task(_watch_stop())
        try:
            await session.run()
            log.info("Session ended.")
        except websockets.exceptions.ConnectionClosedError as e:
            log.warning("Connection closed: %s", e)
        except Exception as e:
            log.error("Session error: %s", e)
        finally:
            watcher.cancel()

        if not stop_event.is_set():
            log.info("Reconnecting in %ds…", RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)

    log.info("Daemon stopped.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="OpenClaw RealTimeTalk daemon")
    p.add_argument("--http-port",      type=int, default=DEFAULT_HTTP_PORT,
                   help=f"HTTP toggle port (default {DEFAULT_HTTP_PORT})")
    p.add_argument("--input-device",   type=int, default=None,
                   help="sounddevice input device index (see --list-devices)")
    p.add_argument("--output-device",  type=int, default=None,
                   help="sounddevice output device index (see --list-devices)")
    p.add_argument("--list-devices",   action="store_true",
                   help="Print available audio devices and exit")
    args = p.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        sys.exit(0)

    asyncio.run(main(args.http_port, args.input_device, args.output_device))
