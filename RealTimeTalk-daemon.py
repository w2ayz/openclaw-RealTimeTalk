#!/usr/bin/env python3
"""
RealTimeTalk-daemon.py — OpenClaw RealTimeTalk daemon (gateway-integrated).

Audio flow:
  Mic → OpenAI Realtime API (VAD + STT only) → transcript
  transcript → OpenClaw gateway (chat.send / agent.wait) → Five's reply
  Five's reply → Piper TTS → speaker

Stop via:
  http://<pi-ip>:18790/stop          — phone browser (over Tailscale)
  systemctl --user stop openclaw-realtimetalk  — SSH
  SIGTERM / Ctrl-C

Usage:
  python3 RealTimeTalk-daemon.py [options]
  python3 RealTimeTalk-daemon.py --list-devices
  python3 RealTimeTalk-daemon.py --input-device 1 --output-device 2

Requires:
  pip install "websockets>=12" sounddevice numpy
  sudo apt install libportaudio2 alsa-utils
  piper installed at ~/.local/bin/piper with a voice model
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import sounddevice as sd
import websockets

# ── Constants ─────────────────────────────────────────────────────────────────

OPENCLAW_CONFIG   = os.path.expanduser("~/.openclaw/openclaw.json")
OPENCLAW_GW_URL   = "ws://127.0.0.1:18789"
OPENCLAW_SESSION  = "agent:main:main"

PIPER_CMD         = os.path.expanduser("~/.local/bin/piper")
PIPER_VOICE       = os.path.expanduser(
    "~/.local/share/piper/voices/en_US-lessac-medium/en_US-lessac-medium.onnx"
)
PIPER_SAMPLE_RATE = 22050
ALSA_OUTPUT       = "plughw:3,0"   # USB speaker; plughw handles rate conversion

OPENAI_TRANSCRIBE_MODEL = "gpt-4o-transcribe"
OPENAI_WS_URL     = "wss://api.openai.com/v1/realtime?intent=transcription"
SAMPLE_RATE       = 24000        # OpenAI Realtime API rate
DEVICE_RATE       = 48000        # USB hardware rate (mic capture)
RESAMPLE_RATIO    = DEVICE_RATE // SAMPLE_RATE   # 2
CHANNELS          = 1
BLOCKSIZE         = 2400         # 100 ms at 24 kHz
DEVICE_BLOCKSIZE  = BLOCKSIZE * RESAMPLE_RATIO   # 4800 hardware frames
DEFAULT_HTTP_PORT = 18790
RECONNECT_DELAY   = 5
AGENT_TIMEOUT_S   = 45
MIC_GAIN          = 8.0          # software gain — PCM2902 USB mic is very quiet
MIC_GATE_PEAK     = 500          # pre-gain peak below this is treated as silence
                                 # (lets OpenAI's VAD see real silence between words)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RTT] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("RealTimeTalk")

# ── Config / secrets ──────────────────────────────────────────────────────────

def _load_json(path: str) -> dict:
    with open(path) as f:
        return json.load(f)

def load_openai_key() -> str:
    cfg = _load_json(OPENCLAW_CONFIG)
    key = (
        cfg.get("talk", {})
           .get("providers", {})
           .get("openai", {})
           .get("apiKey", "")
    )
    # Resolve OpenClaw SecretRef: {"source":"file","provider":"...","id":"/a/b/c"}
    if isinstance(key, dict) and key.get("source") == "file":
        provider_name = key.get("provider", "")
        secret_path = os.path.expanduser(
            cfg.get("secrets", {})
               .get("providers", {})
               .get(provider_name, {})
               .get("path", "")
        )
        secrets = _load_json(secret_path)
        for part in [p for p in key.get("id", "").split("/") if p]:
            secrets = secrets[part]
        key = secrets
    if not key:
        raise RuntimeError(
            "No OpenAI API key at talk.providers.openai.apiKey in openclaw.json"
        )
    return key

def load_gateway_token() -> str:
    cfg = _load_json(OPENCLAW_CONFIG)
    token = cfg.get("gateway", {}).get("auth", {}).get("token", "")
    if not token:
        raise RuntimeError("No gateway.auth.token in openclaw.json")
    return token

# ── Text helpers ──────────────────────────────────────────────────────────────

def strip_markdown(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`{1,3}[^`\n]*`{1,3}', '', text)
    text = re.sub(r'^\s*#+\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text)
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    return text.strip()

# ── Piper TTS ─────────────────────────────────────────────────────────────────

def speak(text: str, alsa_output: str = ALSA_OUTPUT):
    """Synthesise text with Piper and play via aplay.

    Writes text to a temp file and runs Piper with `-i <file>` rather than piping
    via stdin — Piper silently truncates stdin input after a few words, but reads
    file input completely. Output is captured to a WAV temp file (so aplay reads
    from disk, avoiding any streaming buffer underruns on the USB speaker).
    """
    import tempfile
    clean = strip_markdown(text)
    if not clean:
        return
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(clean)
        text_path = tf.name
    wav_path = tempfile.mktemp(suffix=".wav")
    try:
        subprocess.run(
            [PIPER_CMD, "--model", PIPER_VOICE,
             "-i", text_path, "-f", wav_path, "--quiet"],
            check=True, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["aplay", "-D", alsa_output, "-q", wav_path],
        )
    finally:
        for p in (text_path, wav_path):
            try: os.unlink(p)
            except FileNotFoundError: pass

# ── OpenClaw gateway client ───────────────────────────────────────────────────

class GatewayClient:
    """
    Persistent WebSocket operator connection to the local OpenClaw gateway.

    Uses the trusted backend-client path (client.id="gateway-client",
    client.mode="backend") which bypasses device-pairing scope upgrades for
    loopback connections authenticated with the shared gateway token.
    """

    def __init__(self, token: str):
        self.token = token
        self._ws = None
        # Maps request-id → Future for chat.send acks
        self._send_acks: dict[str, asyncio.Future] = {}
        # Maps runId → Future[str] for final chat replies
        self._reply_futs: dict[str, asyncio.Future] = {}

    async def connect(self):
        self._ws = await websockets.connect(OPENCLAW_GW_URL)
        await self._ws.recv()  # connect.challenge — backend clients skip signing
        await self._ws.send(json.dumps({
            "type": "req", "id": "gw-connect", "method": "connect",
            "params": {
                "minProtocol": 3, "maxProtocol": 3,
                "client": {
                    "id": "gateway-client", "version": "1.2.0",
                    "platform": "linux", "mode": "backend",
                },
                "role": "operator",
                "scopes": ["operator.read", "operator.write"],
                "caps": [], "commands": [], "permissions": {},
                "auth": {"token": self.token},
                "locale": "en-US",
                "userAgent": "realtimetalk/1.2",
            },
        }))
        hello = json.loads(await self._ws.recv())
        if not hello.get("ok"):
            raise RuntimeError(f"Gateway connect failed: {hello.get('error')}")
        scopes = hello.get("payload", {}).get("auth", {}).get("scopes", [])
        log.info("OpenClaw gateway connected (scopes: %s)", scopes)

    async def listen(self, stop_event: asyncio.Event):
        """Route incoming gateway events to waiting futures. Run as a task."""
        try:
            async for raw in self._ws:
                if stop_event.is_set():
                    break
                msg = json.loads(raw)
                mtype = msg.get("type", "")
                event = msg.get("event", "")
                payload = msg.get("payload") or {}
                msg_id = msg.get("id", "")

                # Resolve chat.send acks
                if mtype == "res" and msg_id in self._send_acks:
                    fut = self._send_acks.pop(msg_id)
                    if not fut.done():
                        fut.set_result(msg)

                # Resolve agent replies on final chat event
                elif event == "chat" and payload.get("state") == "final":
                    run_id = payload.get("runId")
                    content = payload.get("message", {}).get("content", [])
                    text = " ".join(
                        c.get("text", "") for c in content if c.get("type") == "text"
                    ).strip()
                    fut = self._reply_futs.pop(run_id, None)
                    if fut and not fut.done():
                        fut.set_result(text)

        except websockets.ConnectionClosed:
            pass

    async def ask(self, message: str, session_key: str = OPENCLAW_SESSION) -> str:
        """Send a message to the agent and return its complete reply text."""
        loop = asyncio.get_running_loop()
        idem = str(uuid.uuid4())
        req_id = f"send:{idem}"

        ack_fut: asyncio.Future = loop.create_future()
        self._send_acks[req_id] = ack_fut

        await self._ws.send(json.dumps({
            "type": "req", "id": req_id, "method": "chat.send",
            "params": {
                "sessionKey": session_key,
                "message": message,
                "idempotencyKey": idem,
            },
        }))

        ack = await asyncio.wait_for(ack_fut, timeout=10)
        if not ack.get("ok"):
            raise RuntimeError(f"chat.send failed: {ack.get('error')}")

        run_id = ack.get("payload", {}).get("runId")
        if not run_id:
            raise RuntimeError("chat.send returned no runId")

        reply_fut: asyncio.Future = loop.create_future()
        self._reply_futs[run_id] = reply_fut

        # Register with agent.wait so the gateway tracks this run
        await self._ws.send(json.dumps({
            "type": "req", "id": f"wait:{run_id}", "method": "agent.wait",
            "params": {"runId": run_id, "timeoutMs": AGENT_TIMEOUT_S * 1000},
        }))

        return await asyncio.wait_for(reply_fut, timeout=AGENT_TIMEOUT_S + 5)

    async def close(self):
        if self._ws:
            await self._ws.close()

# ── OpenAI Realtime session (VAD + STT only) ──────────────────────────────────

class RealtimeSession:
    """
    Connects to OpenAI Realtime API solely for voice activity detection and
    speech-to-text. Does not generate AI responses (create_response: false).
    """

    def __init__(self, api_key: str, loop: asyncio.AbstractEventLoop,
                 gw: GatewayClient, stop_event: asyncio.Event,
                 input_device=None, alsa_output: str = ALSA_OUTPUT,
                 session_key: str = OPENCLAW_SESSION):
        self.api_key      = api_key
        self.loop         = loop
        self.gw           = gw
        self.stop_event   = stop_event
        self.input_device = input_device
        self.alsa_output  = alsa_output
        self.session_key  = session_key
        self._mic_q       = asyncio.Queue(maxsize=200)
        self._busy        = asyncio.Event()   # set while Five is speaking

    def _mic_cb(self, indata, frames, time_info, status):
        if self._busy.is_set():
            return  # discard mic input while Five is speaking to prevent feedback
        # Decimate DEVICE_RATE → SAMPLE_RATE
        raw = indata[::RESAMPLE_RATIO, 0]
        if int(np.max(np.abs(raw))) < MIC_GATE_PEAK:
            # noise floor — pass true silence so VAD can detect speech end
            out_arr = np.zeros_like(raw)
        else:
            boosted = raw.astype(np.float32) * MIC_GAIN
            out_arr = np.clip(boosted, -32768, 32767).astype(np.int16)
        try:
            self.loop.call_soon_threadsafe(self._mic_q.put_nowait, out_arr.tobytes())
        except asyncio.QueueFull:
            pass

    async def _send_mic(self, ws):
        while not self.stop_event.is_set():
            try:
                chunk = await asyncio.wait_for(self._mic_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if self._busy.is_set():
                continue
            await ws.send(json.dumps({
                "type":  "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode(),
            }))

    async def _handle_transcript(self, transcript: str):
        self._busy.set()
        try:
            log.info("Routing to Five: %s", transcript)
            reply = await self.gw.ask(transcript, session_key=self.session_key)
            log.info("Five: %s", reply)
            await asyncio.get_running_loop().run_in_executor(
                None, speak, reply, self.alsa_output
            )
        except asyncio.TimeoutError:
            log.error("OpenClaw agent timed out")
            await asyncio.get_running_loop().run_in_executor(
                None, speak, "Sorry, I timed out on that.", self.alsa_output
            )
        except Exception as e:
            log.error("Error routing transcript: %s", e)
        finally:
            self._busy.clear()

    async def _recv_ws(self, ws):
        async for raw in ws:
            if self.stop_event.is_set():
                break
            msg = json.loads(raw)
            t   = msg.get("type", "")

            if t in ("conversation.item.done", "conversation.item.input_audio_transcription.completed"):
                # transcription endpoint: transcript in item.content[].transcript
                # old realtime endpoint: transcript in top-level .transcript
                transcript = msg.get("transcript", "")
                if not transcript:
                    for chunk in msg.get("item", {}).get("content", []):
                        if chunk.get("type") == "input_audio" and chunk.get("transcript"):
                            transcript = chunk["transcript"]
                            break
                transcript = transcript.strip()
                if transcript and not self._busy.is_set():
                    log.info("You: %s", transcript)
                    asyncio.create_task(self._handle_transcript(transcript))

            elif t == "error":
                log.error("OpenAI error: %s", msg.get("error", msg))

            elif t not in (
                "input_audio_buffer.speech_started",
                "input_audio_buffer.speech_stopped",
                "input_audio_buffer.committed",
                "conversation.item.created",
                "conversation.item.added",
                "conversation.item.done",
                "conversation.item.input_audio_transcription.delta",
                "transcription_session.updated",
                "session.updated",
                "session.created",
            ):
                log.debug("OpenAI event: %s", t)

    async def run(self):
        log.info("Connecting to OpenAI Realtime API (STT mode)…")
        async with websockets.connect(
            OPENAI_WS_URL,
            additional_headers={
                "Authorization": f"Bearer {self.api_key}",
            },
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            # GA transcription session: nested audio.input config
            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "type": "transcription",
                    "audio": {
                        "input": {
                            "transcription": {"model": OPENAI_TRANSCRIBE_MODEL},
                            "turn_detection": {
                                "type":                "server_vad",
                                "threshold":           0.5,
                                "prefix_padding_ms":   300,
                                "silence_duration_ms": 800,
                            },
                        },
                    },
                },
            }))
            log.info("Session active — speak now (routed through Five / OpenClaw)")

            in_stream = sd.InputStream(
                samplerate=DEVICE_RATE, channels=CHANNELS, dtype="int16",
                blocksize=DEVICE_BLOCKSIZE, callback=self._mic_cb,
                device=self.input_device,
            )

            with in_stream:
                tasks = [
                    asyncio.create_task(self._send_mic(ws)),
                    asyncio.create_task(self._recv_ws(ws)),
                    asyncio.create_task(self.stop_event.wait()),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()

# ── HTTP toggle server ────────────────────────────────────────────────────────

def start_http_server(port: int, on_stop):
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

async def main(http_port: int, input_device=None, alsa_output: str = ALSA_OUTPUT,
               session_key: str = OPENCLAW_SESSION):
    loop       = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        openai_key = load_openai_key()
        gw_token   = load_gateway_token()
    except Exception as e:
        log.error(str(e))
        sys.exit(1)

    gw = GatewayClient(gw_token)
    while not stop_event.is_set():
        try:
            await gw.connect()
            break
        except (ConnectionRefusedError, OSError) as e:
            log.warning("Gateway not ready (%s) — retrying in 5s…", e)
            await asyncio.sleep(5)
    if stop_event.is_set():
        return
    gw_task = asyncio.create_task(gw.listen(stop_event))

    start_http_server(http_port, lambda: loop.call_soon_threadsafe(stop_event.set))
    log.info("OpenClaw RealTimeTalk daemon starting (gateway-integrated)")

    while not stop_event.is_set():
        session = RealtimeSession(
            api_key=openai_key, loop=loop, gw=gw,
            stop_event=stop_event,
            input_device=input_device, alsa_output=alsa_output,
            session_key=session_key,
        )
        try:
            await session.run()
            log.info("Session ended.")
        except websockets.exceptions.ConnectionClosedError as e:
            log.warning("Realtime connection closed: %s", e)
        except Exception as e:
            log.error("Session error: %s", e)

        if not stop_event.is_set():
            log.info("Reconnecting in %ds…", RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)

    gw_task.cancel()
    await gw.close()
    log.info("Daemon stopped.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="OpenClaw RealTimeTalk daemon")
    p.add_argument("--http-port",      type=int, default=DEFAULT_HTTP_PORT,
                   help=f"HTTP toggle port (default {DEFAULT_HTTP_PORT})")
    p.add_argument("--input-device",   type=int, default=None,
                   help="sounddevice input device index (see --list-devices)")
    p.add_argument("--alsa-output",    type=str, default=ALSA_OUTPUT,
                   help=f"ALSA output device for TTS playback (default: {ALSA_OUTPUT})")
    p.add_argument("--session-key",    type=str, default=OPENCLAW_SESSION,
                   help=f"OpenClaw session key (default: {OPENCLAW_SESSION})")
    p.add_argument("--list-devices",   action="store_true",
                   help="Print available audio devices and exit")
    args = p.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        sys.exit(0)

    asyncio.run(main(
        args.http_port,
        args.input_device,
        args.alsa_output,
        args.session_key,
    ))
