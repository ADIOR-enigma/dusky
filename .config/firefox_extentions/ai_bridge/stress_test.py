#!/usr/bin/env python3
"""
Local AI Bridge - Stress Test Suite
Executes a battery of high-load tests against the web AI extension bridge:
1. Complex formatting (Markdown + LaTeX formulas)
2. Code generation (Newlines + Indentation + Quotes)
3. Special characters, Emojis, and HTML escaping resilience
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

TEST_SUITE = [
    {
        "name": "Test 1: Complex Formatting & LaTeX",
        "prompt": "Explain Quantum Entanglement in 3 bullet points using markdown bolding and the LaTeX formula: \\(|\\psi\\rangle = \\frac{1}{\\sqrt{2}}(|00\\rangle + |11\\rangle)\\)."
    },
    {
        "name": "Test 2: Code Generation & Special Syntax",
        "prompt": "Write a short Python function `def quicksort(arr):` with docstrings, inline comments, and nested quotes like 'hello \"world\"'. Output pure Python code."
    },
    {
        "name": "Test 3: Emojis, Unicode & Escaping Resilience",
        "prompt": "List 3 fun facts about space with emojis 🚀 🌌 🌟, nested quotes \"'test'\", and HTML tags `<div id=\"test\">`."
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

current_deferred = None

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

async def run_single_test(client, test_info, test_num, total_tests):
    global current_deferred
    print(f"\n==========================================")
    print(f" [{test_num}/{total_tests}] Executing: {test_info['name']}")
    print(f" Prompt: \"{test_info['prompt']}\"")
    print(f"==========================================\n")

    import uuid
    rid = str(uuid.uuid4())[:8]
    payload = {"type": "RUN_QUERY", "query": test_info["prompt"], "requestId": rid}
    
    current_deferred = asyncio.get_event_loop().create_future()
    start_time = time.time()
    await client.send(json.dumps(payload))

    try:
        res = await asyncio.wait_for(current_deferred, timeout=45.0)
        elapsed = time.time() - start_time
        if res.get("type") == "FINAL":
            full_text = res.get("full", "")
            print(f"\n\n[PASSED] Test {test_num} completed in {elapsed:.2f}s | Received {len(full_text)} characters")
            return True
        else:
            print(f"\n\n[FAILED] Test {test_num} error: {res.get('error')}")
            return False
    except asyncio.TimeoutError:
        print(f"\n\n[TIMEOUT] Test {test_num} timed out after 45 seconds!")
        return False

async def main():
    # Kill previous task if running on port 8765
    server = await asyncio.start_server(handle_client, HOST, PORT)
    print("==========================================")
    print("      Local AI Bridge - Stress Tester")
    print("==========================================")
    print(f"Listening on ws://{HOST}:{PORT}")
    print("Waiting for Firefox extension client...")

    async with server:
        while not clients:
            await asyncio.sleep(0.5)
        
        client = list(clients)[0]
        print(f"\n[+] Firefox extension connected! Starting stress test battery ({len(TEST_SUITE)} tests)...\n")
        await asyncio.sleep(1.0)

        results = []
        for i, test in enumerate(TEST_SUITE, 1):
            passed = await run_single_test(client, test, i, len(TEST_SUITE))
            results.append((test['name'], passed))
            await asyncio.sleep(2.0)

        print("\n==========================================")
        print("          STRESS TEST SUMMARY")
        print("==========================================")
        all_ok = True
        for name, passed in results:
            status = "PASSED ✅" if passed else "FAILED ❌"
            print(f" - {name}: {status}")
            if not passed: all_ok = False
        print("==========================================\n")
        if all_ok:
            print("🎉 ALL STRESS TESTS PASSED PERFECTLY!")
        else:
            print("⚠️ SOME TESTS FAILED.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye")
