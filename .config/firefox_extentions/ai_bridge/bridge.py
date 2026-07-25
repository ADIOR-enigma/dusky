#!/usr/bin/env python3
"""
Local AI Bridge - Python side - Firefox 153 minimal flawless
- WebSocket server on ws://127.0.0.1:8765
- Extension connects as client
- Terminal AI / you can send queries via stdin or import

pip install websockets

Usage:
  python bridge.py
  > your query here -> sends to web AI
  Streams back chunks in terminal.

Integrate with your local LLM:
  from bridge import send_query
  await send_query("Summarize...")
"""

import asyncio
import json
import sys
from collections import deque

try:
    import websockets
except ImportError:
    print("Missing websockets: pip install websockets")
    sys.exit(1)

HOST = "127.0.0.1"
PORT = 8765

clients = set()  # extension clients
pending = {}  # requestId -> full response buffer

async def handle_extension(websocket):
    clients.add(websocket)
    print(f"[+] Extension connected: {websocket.remote_address} - total {len(clients)}")
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except:
                continue
            mtype = msg.get("type")
            rid = msg.get("requestId", "unknown")
            if mtype == "STREAM_CHUNK":
                text = msg.get("text","")
                full = msg.get("full","")
                sys.stdout.write(text)
                sys.stdout.flush()
                pending[rid] = full
            elif mtype == "FINAL":
                full = msg.get("full") or pending.get(rid,"")
                print(f"\n\n[FINAL rid={rid}] length={len(full)}")
                if rid in pending:
                    del pending[rid]
                print("\n--- Ready for next query ---\n> ", end="", flush=True)
            elif mtype == "ERROR":
                print(f"\n[ERROR rid={rid}] {msg.get('error')}")
                print("\n> ", end="", flush=True)
            else:
                print(f"\n[MSG] {msg}")
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        clients.discard(websocket)
        print(f"[-] Extension disconnected - total {len(clients)}")

async def send_query(query: str, tab_id=None):
    if not clients:
        print("[!] No extension connected. Open Firefox with extension and visit chatgpt.com")
        return None
    import uuid
    rid = str(uuid.uuid4())[:8]
    payload = {"type":"RUN_QUERY", "query": query, "requestId": rid}
    if tab_id:
        payload["tabId"] = tab_id
    # Send to all extension clients (usually 1)
    for ws in list(clients):
        try:
            await ws.send(json.dumps(payload))
        except:
            pass
    pending[rid] = ""
    return rid

async def input_loop():
    print("Local AI Bridge - Firefox 153")
    print(f"Listening on ws://{HOST}:{PORT}")
    print("Open Firefox, load extension, open chatgpt.com / claude.ai, keep tab focused")
    print("Type query and press Enter to send to web AI\n")
    loop = asyncio.get_event_loop()
    while True:
        # Use thread executor for blocking input()
        query = await loop.run_in_executor(None, lambda: input("> ").strip())
        if not query:
            continue
        if query in ("exit","quit"):
            break
        await send_query(query)

async def main():
    async with websockets.serve(handle_extension, HOST, PORT, ping_interval=10, ping_timeout=10):
        await input_loop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye")
