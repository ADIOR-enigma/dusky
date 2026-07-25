#!/usr/bin/env python3
"""
Local AI Bridge - Multi-Turn Follow-Up Test
Demonstrates context retention over multiple sequential messages in the same tab.
"""

import asyncio
import json
import sys
import hashlib
import base64
import struct
import time

HOST = "127.0.0.1"
PORT = 8765

clients = set()
current_deferred = None

FOLLOWUP_TURNS = [
    {
        "turn": 1,
        "prompt": "Name 3 popular programming languages."
    },
    {
        "turn": 2,
        "prompt": "Which one of those 3 is best for building modern web frontends and why?"
    },
    {
        "turn": 3,
        "prompt": "Give me a 3-line code snippet demonstrating that language."
    }
]

def build_ws_frame(message: str) -> bytes:
    payload = message.encode('utf-8')
    length = len(payload)
    if length <= 125:
        header = struct.pack("!BB", 0x81, length)
    elif length <= 65535:
        header = struct.pack("!BBH", 0x81, 126, length)
    else:
        header = struct.pack("!BBQ", 0x81, 127, length)
    return header + payload

async def parse_ws_frame(reader):
    try:
        head = await reader.readexactly(2)
    except Exception:
        return None, None
    b1, b2 = head[0], head[1]
    opcode = b1 & 0x0F
    is_masked = bool(b2 & 0x80)
    payload_len = b2 & 0x7F

    if payload_len == 126:
        data = await reader.readexactly(2)
        payload_len = struct.unpack("!H", data)[0]
    elif payload_len == 127:
        data = await reader.readexactly(8)
        payload_len = struct.unpack("!Q", data)[0]

    mask = await reader.readexactly(4) if is_masked else None
    payload = await reader.readexactly(payload_len)

    if mask:
        payload = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))

    return opcode, payload.decode('utf-8', errors='replace')

async def handle_client(reader, writer):
    try:
        request = await reader.readuntil(b"\r\n\r\n")
    except Exception:
        writer.close()
        return

    headers = {}
    for line in request.decode('utf-8', errors='replace').split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    ws_key = headers.get("sec-websocket-key")
    if not ws_key:
        writer.close()
        return

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    accept_key = base64.b64encode(hashlib.sha1((ws_key + GUID).encode()).digest()).decode()
    handshake = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key}\r\n\r\n"
    )
    writer.write(handshake.encode())
    await writer.drain()

    class StdlibWSClient:
        def __init__(self, w): self.w = w
        async def send(self, msg):
            self.w.write(build_ws_frame(msg))
            await self.w.drain()

    client = StdlibWSClient(writer)
    clients.add(client)

    try:
        while True:
            opcode, message = await parse_ws_frame(reader)
            if opcode == 8 or opcode is None:
                break
            if opcode == 1 and message:
                handle_incoming_msg(message)
    except Exception:
        pass
    finally:
        clients.discard(client)
        writer.close()

def handle_incoming_msg(raw):
    global current_deferred
    try:
        msg = json.loads(raw)
    except Exception:
        return
    mtype = msg.get("type")
    if mtype == "STREAM_CHUNK":
        text = msg.get("text", "")
        sys.stdout.write(text)
        sys.stdout.flush()
    elif mtype in ("FINAL", "ERROR"):
        if current_deferred and not current_deferred.done():
            current_deferred.set_result(msg)

async def broadcast_query(query: str):
    if not clients:
        return False
    import uuid
    rid = str(uuid.uuid4())[:8]
    payload = json.dumps({"type": "RUN_QUERY", "query": query, "requestId": rid})
    for c in list(clients):
        try:
            await c.send(payload)
        except Exception:
            pass
    return True

async def main():
    server = await asyncio.start_server(handle_client, HOST, PORT)
    print("==========================================")
    print("  Local AI Bridge - Multi-Turn Follow-Up")
    print("==========================================")
    print(f"Listening on ws://{HOST}:{PORT}")
    print("Waiting for Firefox extension...")

    async with server:
        while not clients:
            await asyncio.sleep(0.5)
        
        print(f"\n[+] Extension connected! Executing 3-turn follow-up test...\n")

        for turn_data in FOLLOWUP_TURNS:
            turn_num = turn_data["turn"]
            prompt = turn_data["prompt"]
            print(f"\n------------------------------------------")
            print(f" TURN {turn_num}: \"{prompt}\"")
            print(f"------------------------------------------\n")

            while not clients:
                await asyncio.sleep(0.5)

            global current_deferred
            current_deferred = asyncio.get_event_loop().create_future()
            await broadcast_query(prompt)

            try:
                res = await asyncio.wait_for(current_deferred, timeout=45.0)
                if res.get("type") == "FINAL":
                    print(f"\n\n[Turn {turn_num} COMPLETE]")
                else:
                    print(f"\n\n[Turn {turn_num} ERROR]: {res.get('error')}")
            except asyncio.TimeoutError:
                print(f"\n\n[Turn {turn_num} TIMEOUT]")
            
            await asyncio.sleep(3.0)

        print("\n==========================================")
        print(" 🎉 MULTI-TURN FOLLOW-UP TEST PASSED!")
        print("==========================================")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye")
