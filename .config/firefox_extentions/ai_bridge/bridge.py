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
REPLY_FUTURES = {}

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
            unmasked = bytearray(b ^ masks[i % 4] for i, b in enumerate(raw))
            return unmasked.decode('utf-8', errors='replace')
        else:
            return raw.decode('utf-8', errors='replace')

    def close(self):
        try: self.writer.close()
        except: pass

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

            if msg_type == "GET_STATUS":
                await ws.send(json.dumps({
                    "type": "STATUS_REPLY",
                    "clientCount": len(CONNECTED_CLIENTS)
                }))
                continue

            # Broadcast all messages to all other connected clients
            dead = set()
            for c in CONNECTED_CLIENTS:
                if c != ws:
                    try: await c.send(msg)
                    except Exception: dead.add(c)
            CONNECTED_CLIENTS.difference_update(dead)

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

    req_id = str(uuid.uuid4())
    payload = json.dumps({"type": "RUN_QUERY", "query": query_text, "requestId": req_id}).encode('utf-8')
    
    # Masked frame
    mask = os.urandom(4)
    masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))
    frame = bytearray([0x81, 0x80 | len(payload)]) + mask + masked
    s.sendall(frame)

    # Hardware Return Keypress Fallback Trigger via wtype (1.5s post-typing)
    def send_os_return():
        # 1. Focus the Firefox window that is showing an AI chat (Gemini/ChatGPT/Claude)
        try:
            res = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=2)
            if res.returncode == 0:
                clients = json.loads(res.stdout)
                ai_keywords = (
                    "google gemini", "gemini.google", "chatgpt", "claude",
                    "meta ai", "chatgpt.com", "claude.ai",
                )
                ff = [c for c in clients if "firefox" in (c.get("class") or "").lower()]
                # Prefer a Firefox window whose title looks like an AI chat
                preferred = None
                for c in ff:
                    title = (c.get("title") or "").lower()
                    if any(k in title for k in ai_keywords):
                        preferred = c
                        break
                target = preferred or (ff[0] if ff else None)
                if target:
                    addr = target["address"]
                    ws_info = target.get("workspace", {})
                    ws_id = ws_info.get("id")
                    if ws_id is not None and ws_id > 0:
                        subprocess.run(
                            ["hyprctl", "dispatch", f"hl.dsp.focus({{ workspace = '{ws_id}' }})"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                        )
                    subprocess.run(
                        ["hyprctl", "dispatch", f"hl.dsp.focus({{ window = 'address:{addr}' }})"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                    )
        except Exception:
            pass

        # 2. Wait for window focus to settle, then fire authentic Return keypress
        _time.sleep(0.4)
        try:
            subprocess.run(["wtype", "-k", "Return"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            pass
        # Second Return after a beat helps if the first hit before focus settled
        _time.sleep(0.35)
        try:
            subprocess.run(["wtype", "-k", "Return"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            pass

    t = threading.Timer(0.7, send_os_return)
    t.daemon = True
    t.start()

    full_text = ""
    started = _time.time()
    last_chunk_at = None
    printed_len = 0

    def finish(text, note=None):
        if stream:
            # If we only got full snapshots, print any remaining tail once
            if text and printed_len == 0:
                sys.stdout.write(text)
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
                        sys.stdout.write(full_text[printed_len:])
                        sys.stdout.flush()
                        printed_len = len(full_text)
                    # else identical re-send; ignore
                else:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    printed_len += len(chunk)
            last_chunk_at = _time.time()
        elif mtype == "FINAL":
            full = data.get("full", full_text) or full_text
            # Print any unstreamed tail once (append-only case)
            if stream and full and printed_len < len(full):
                if printed_len == 0 or full[:printed_len] == full_text[:printed_len]:
                    sys.stdout.write(full[printed_len:])
                    sys.stdout.flush()
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
