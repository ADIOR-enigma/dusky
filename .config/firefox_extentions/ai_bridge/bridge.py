#!/usr/bin/env python3
"""
Local AI Web Bridge Daemon & CLI Client - Zero External Dependencies (Standard Library Only)
Bridges terminal commands to open web AI tabs (ChatGPT, Claude, Gemini, Meta AI) in Firefox.
"""

import sys
import os
import json
import asyncio
import socket
import base64
import hashlib
import uuid
import struct
import subprocess
import threading

HOST = "127.0.0.1"
PORT = 8765

CONNECTED_CLIENTS = set()

def make_ws_response_key(sec_key):
    magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    sha1 = hashlib.sha1((sec_key + magic).encode('utf-8')).digest()
    return base64.b64encode(sha1).decode('utf-8')

class WSConnection:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    async def send(self, message):
        data = message.encode('utf-8') if isinstance(message, str) else message
        length = len(data)
        frame = bytearray([0x81])
        if length <= 125:
            frame.append(length)
        elif length <= 65535:
            frame.append(126)
            frame.extend(struct.pack("!H", length))
        else:
            frame.append(127)
            frame.extend(struct.pack("!Q", length))
        frame.extend(data)
        self.writer.write(frame)
        await self.writer.drain()

    async def recv(self):
        try:
            head = await self.reader.readexactly(2)
        except Exception:
            return None

        b1, b2 = head[0], head[1]
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        payload_len = b2 & 0x7F

        if payload_len == 126:
            data = await self.reader.readexactly(2)
            payload_len = struct.unpack("!H", data)[0]
        elif payload_len == 127:
            data = await self.reader.readexactly(8)
            payload_len = struct.unpack("!Q", data)[0]

        masks = await self.reader.readexactly(4) if masked else None
        raw = await self.reader.readexactly(payload_len)

        if masked:
            raw = bytes(b ^ masks[i % 4] for i, b in enumerate(raw))

        # Close frame (0x8)
        if opcode == 0x8:
            return None

        # Ping frame (0x9) -> Send Pong (0x8A)
        if opcode == 0x9:
            frame = bytearray([0x8A])
            n = len(raw)
            if n <= 125:
                frame.append(n)
            elif n <= 65535:
                frame.append(126)
                frame.extend(struct.pack("!H", n))
            else:
                frame.append(127)
                frame.extend(struct.pack("!Q", n))
            frame.extend(raw)
            self.writer.write(frame)
            await self.writer.drain()
            return await self.recv()

        # Pong frame (0xA)
        if opcode == 0xA:
            return await self.recv()

        # Only process text/continuation frames
        if opcode not in (0x1, 0x0):
            return await self.recv()

        return raw.decode("utf-8", errors="replace")

    def close(self):
        try: self.writer.close()
        except: pass

# --- Hardware Keypress Submission (daemon-level) ---
# Gemini ignores synthetic JS clicks (isTrusted:false). The ONLY reliable
# way to submit a typed prompt is via an OS-level hardware keypress.
# This function is called by the daemon for EVERY RUN_QUERY, regardless
# of whether the caller is bridge.py CLI, a benchmark script, or the SDK.
import time as _time

def _do_os_return():
    """Focus Firefox AI tab and send hardware Ctrl+Enter + Return for Gemini."""
    try:
        res = subprocess.run(
            ["hyprctl", "clients", "-j"],
            capture_output=True, text=True, timeout=2,
        )
        if res.returncode != 0:
            return

        clients = json.loads(res.stdout)
        ai_keywords = (
            "google gemini", "gemini.google", "chatgpt", "claude",
            "meta ai", "chatgpt.com", "claude.ai",
        )
        ff = [c for c in clients if "firefox" in (c.get("class") or "").lower()]
        preferred = None
        for c in ff:
            title = (c.get("title") or "").lower()
            if any(k in title for k in ai_keywords):
                preferred = c
                break
        target = preferred or (ff[0] if ff else None)
        if not target:
            return

        addr = target["address"]
        ws_info = target.get("workspace", {}) or {}
        ws_id = ws_info.get("id")
        multi_mon_ws = os.environ.get(
            "AI_BRIDGE_MULTI_MON_WS",
            "/home/dusk/user_scripts/hypr/multi_monitor_workspace.sh",
        )

        if ws_id is not None and ws_id > 0:
            rel_ws = ((ws_id - 1) % 10) + 1
            try:
                subprocess.run(
                    [multi_mon_ws, "workspace", str(rel_ws)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                )
            except Exception:
                pass
            subprocess.run(
                ["hyprctl", "dispatch", f'hl.dsp.focus({{ workspace = "{ws_id}" }})'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
            )

        subprocess.run(
            ["hyprctl", "dispatch", f'hl.dsp.focus({{ window = "address:{addr}" }})'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )

        title = (target.get("title") or "").lower()
        if "gemini" in title:
            _time.sleep(0.4)
            subprocess.run(
                ["wtype", "-M", "ctrl", "-k", "Return", "-m", "ctrl"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
            )
            _time.sleep(0.2)
            subprocess.run(
                ["wtype", "-k", "Return"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
            )
    except Exception:
        pass

def _schedule_os_return():
    """Schedule hardware Return keypress 1.5s after query dispatch."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        t = threading.Timer(1.5, _do_os_return)
        t.daemon = True
        t.start()
        return

    async def _run():
        await asyncio.sleep(1.5)
        await asyncio.to_thread(_do_os_return)

    loop.create_task(_run())

async def handle_client(reader, writer):
    # Perform HTTP WebSocket Handshake
    headers = ""
    while "\r\n\r\n" not in headers:
        line = await reader.readline()
        if not line: break
        headers += line.decode('utf-8', errors='replace')

    sec_key = None
    for l in headers.split("\r\n"):
        if l.lower().startswith("sec-websocket-key:"):
            sec_key = l.split(":", 1)[1].strip()
            break

    if not sec_key:
        resp = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{\"status\":\"ok\"}\n"
        writer.write(resp.encode('utf-8'))
        await writer.drain()
        try: writer.close()
        except: pass
        return

    resp_key = make_ws_response_key(sec_key)
    handshake = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {resp_key}\r\n\r\n"
    )
    writer.write(handshake.encode('utf-8'))
    await writer.drain()

    ws = WSConnection(reader, writer)
    CONNECTED_CLIENTS.add(ws)
    print(f"[Daemon] Client connected. Active clients: {len(CONNECTED_CLIENTS)}")

    try:
        while True:
            msg = await ws.recv()
            if msg is None: break
            
            try: data = json.loads(msg)
            except Exception: continue

            msg_type = data.get("type")
            req_id = data.get("requestId")

            if msg_type in ("PING", "HEARTBEAT"):
                await ws.send(json.dumps({"type": "PONG"}))
                continue

            if msg_type == "DIAGNOSE_TABS":
                # Forward to background.js and relay response
                dead = set()
                for c in CONNECTED_CLIENTS:
                    if c != ws:
                        try: await c.send(msg)
                        except Exception: dead.add(c)
                CONNECTED_CLIENTS.difference_update(dead)
                continue

            # Broadcast all messages to all other connected clients
            dead = set()
            broadcast_count = 0
            for c in CONNECTED_CLIENTS:
                if c != ws:
                    try:
                        await c.send(msg)
                        broadcast_count += 1
                    except Exception: dead.add(c)
            CONNECTED_CLIENTS.difference_update(dead)

            if msg_type == "RUN_QUERY":
                query_preview = data.get("query", "")[:60]
                print(f"[Daemon] RUN_QUERY broadcast to {broadcast_count} client(s): '{query_preview}...'", flush=True)

            # For every RUN_QUERY, schedule a hardware Return keypress
            # This is essential because synthetic JS clicks are isTrusted:false
            # and Gemini ignores them. The ONLY reliable submission is wtype.
            if msg_type == "RUN_QUERY":
                print(f"[Daemon] Scheduling _do_os_return in 1.5s", flush=True)
                _schedule_os_return()

    except Exception:
        pass
    finally:
        CONNECTED_CLIENTS.discard(ws)
        ws.close()
        print(f"[Daemon] Client disconnected. Active clients: {len(CONNECTED_CLIENTS)}")

async def start_daemon():
    print(f"[Daemon] Starting Local AI WebSocket Daemon on {HOST}:{PORT} (Standard Library)")
    server = await asyncio.start_server(handle_client, HOST, PORT)
    async with server:
        await server.serve_forever()

def safe_write(text):
    if not text: return
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except UnicodeEncodeError:
        try:
            clean = text.encode('utf-16', 'surrogatepass').decode('utf-16', 'replace')
            sys.stdout.write(clean)
            sys.stdout.flush()
        except Exception:
            pass

def send_query_cli(query_text, stream=True, idle_timeout=10.0, hard_timeout=180.0):
    """Send one prompt and wait for FINAL (or idle/hard timeout fallback).

    idle_timeout: seconds without STREAM_CHUNK after first token before treating
                  the last full text as complete (guards against missing FINAL).
    hard_timeout: absolute max wait from query send.
    """
    import time as _time

    # Standard library socket client
    s = socket.socket()
    s.settimeout(1.0)  # allow periodic idle checks
    s.connect((HOST, PORT))
    sec_key = base64.b64encode(os.urandom(16)).decode('utf-8')
    handshake = (
        f"GET / HTTP/1.1\r\n"
        f"Host: {HOST}:{PORT}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {sec_key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    s.sendall(handshake.encode('utf-8'))
    resp = s.recv(1024)

    if b"101" not in resp.split(b"\r\n", 1)[0]:
        print("[Error] WebSocket handshake failed:", resp[:200], file=sys.stderr)
        try: s.close()
        except Exception: pass
        sys.exit(1)

    req_id = str(uuid.uuid4())
    payload = json.dumps({"type": "RUN_QUERY", "query": query_text, "requestId": req_id}).encode('utf-8')

    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    header = bytearray([0x81])
    n = len(payload)
    if n <= 125:
        header.append(0x80 | n)
    elif n <= 65535:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", n))
    s.sendall(header + mask + masked)

    # NOTE: Hardware Return keypress is now handled by the daemon (_schedule_os_return)
    # for ALL RUN_QUERY messages, so no need to schedule it here in CLI.

    full_text = ""
    started = _time.time()
    last_chunk_at = None
    printed_len = 0

    def finish(text, note=None):
        if stream:
            # If we only got full snapshots, print any remaining tail once
            if text and printed_len == 0:
                safe_write(text)
            sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            print(text or "")
        if note:
            print(f"[bridge] {note}", file=sys.stderr)
        try: s.close()
        except Exception: pass
        return text or ""

    while True:
        now = _time.time()
        if now - started > hard_timeout:
            if full_text:
                return finish(full_text, f"hard timeout after {hard_timeout:.0f}s — returning partial")
            print(f"\n[Error] Timed out after {hard_timeout:.0f}s with no response", file=sys.stderr)
            try: s.close()
            except Exception: pass
            sys.exit(1)

        if last_chunk_at is not None and full_text and (now - last_chunk_at) >= idle_timeout:
            return finish(full_text, f"idle timeout ({idle_timeout:.0f}s) — treating as FINAL")

        try:
            head = s.recv(2)
        except socket.timeout:
            continue
        except Exception:
            break
        if not head: break
        # If partial header, keep reading
        while len(head) < 2:
            try:
                more = s.recv(2 - len(head))
            except socket.timeout:
                continue
            if not more:
                break
            head += more
        if len(head) < 2:
            break

        l = head[1] & 0x7F
        if l == 126:
            raw = b""
            while len(raw) < 2:
                try:
                    c = s.recv(2 - len(raw))
                except socket.timeout:
                    continue
                if not c: break
                raw += c
            l = struct.unpack("!H", raw)[0]
        elif l == 127:
            raw = b""
            while len(raw) < 8:
                try:
                    c = s.recv(8 - len(raw))
                except socket.timeout:
                    continue
                if not c: break
                raw += c
            l = struct.unpack("!Q", raw)[0]

        p = bytearray()
        while len(p) < l:
            try:
                c = s.recv(l - len(p))
            except socket.timeout:
                continue
            if not c: break
            p.extend(c)

        try: data = json.loads(p.decode('utf-8'))
        except Exception: continue

        if data.get("requestId") != req_id: continue

        mtype = data.get("type")
        if mtype == "STREAM_CHUNK":
            chunk = data.get("text", "")
            new_full = data.get("full")
            if new_full:
                full_text = new_full
            elif chunk:
                full_text = full_text + chunk

            if stream and chunk:
                # Avoid re-printing when chunk is a full re-render of known text
                if chunk == full_text and printed_len > 0:
                    # re-render snapshot — only print suffix beyond what we already showed
                    if full_text.startswith(full_text[:printed_len]) and len(full_text) > printed_len:
                        safe_write(full_text[printed_len:])
                        printed_len = len(full_text)
                    # else identical re-send; ignore
                else:
                    safe_write(chunk)
                    printed_len += len(chunk)
            last_chunk_at = _time.time()
        elif mtype == "FINAL":
            full = data.get("full", full_text) or full_text
            # Print any unstreamed tail once (append-only case)
            if stream and full and printed_len < len(full):
                if printed_len == 0 or full[:printed_len] == full_text[:printed_len]:
                    safe_write(full[printed_len:])
                    printed_len = len(full)
            return finish(full)
        elif mtype == "ERROR":
            print(f"\n[Error] {data.get('error')}", file=sys.stderr)
            try: s.close()
            except Exception: pass
            sys.exit(1)

    if full_text:
        return finish(full_text, "connection closed — returning partial")
    print("\n[Error] Connection closed with no response", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--daemon":
            asyncio.run(start_daemon())
        else:
            query = " ".join(sys.argv[1:])
            send_query_cli(query)
    else:
        print("Usage:")
        print("  python3 bridge.py --daemon            # Run server daemon")
        print("  python3 bridge.py \"<your prompt>\"    # Send prompt CLI query")
