#!/usr/bin/env python3
import asyncio, json, sys, struct, uuid, base64, os

HOST, PORT = "127.0.0.1", 8765

FOLLOWUP_PROMPTS = [
    "Explain the difference between a stack and a queue in 2 sentences.",
    "Give me a simple Python example of each.",
    "Now show me how to reverse a stack using recursion.",
]

def build_ws_frame(message: str) -> bytes:
    payload = message.encode('utf-8')
    length = len(payload)
    mask = uuid.uuid4().bytes
    masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))
    if length <= 125:
        header = struct.pack("!BB", 0x81, 0x80 | length)
    elif length <= 65535:
        header = struct.pack("!BBH", 0x81, 0x80 | 126, length)
    else:
        header = struct.pack("!BBQ", 0x81, 0x80 | 127, length)
    return header + mask + bytes(masked)

async def parse_ws_frame(reader):
    try:
        head = await asyncio.wait_for(reader.readexactly(2), timeout=30)
    except asyncio.TimeoutError:
        return -1, "TIMEOUT"
    except Exception:
        return None, None
    b1, b2 = head[0], head[1]
    opcode = b1 & 0x0F
    payload_len = b2 & 0x7F
    if opcode == 8:
        return 8, None
    if payload_len == 126:
        data = await reader.readexactly(2)
        payload_len = struct.unpack("!H", data)[0]
    elif payload_len == 127:
        data = await reader.readexactly(8)
        payload_len = struct.unpack("!Q", data)[0]
    is_masked = bool(b2 & 0x80)
    mask = await reader.readexactly(4) if is_masked else None
    payload = await reader.readexactly(payload_len)
    if mask:
        payload = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload.decode('utf-8', errors='replace')

async def main():
    reader, writer = await asyncio.open_connection(HOST, PORT)
    sec_key = base64.b64encode(uuid.uuid4().bytes).decode()
    handshake = (
        f"GET / HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {sec_key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    writer.write(handshake.encode())
    await writer.drain()
    resp = await reader.readuntil(b"\r\n\r\n")
    if b"101" not in resp:
        print("Handshake failed"); return
    print("Connected to daemon.")

    for i, prompt in enumerate(FOLLOWUP_PROMPTS, 1):
        print(f"\n--- TURN {i}: {prompt} ---\n", flush=True)
        req_id = str(uuid.uuid4())[:8]
        payload = json.dumps({"type": "RUN_QUERY", "query": prompt, "requestId": req_id})
        writer.write(build_ws_frame(payload))
        await writer.drain()
        print(f"[DEBUG] Sent RUN_QUERY req_id={req_id}", flush=True)

        full_text = ""
        while True:
            opcode, message = await parse_ws_frame(reader)
            print(f"[DEBUG] opcode={opcode} msg_preview={str(message)[:120] if message else None}", flush=True)
            if opcode is None or opcode == 8:
                print("Connection closed"); return
            if opcode == -1:
                print("TIMEOUT waiting for response"); break
            if not message:
                continue
            try:
                data = json.loads(message)
            except:
                continue
            mtype = data.get("type")
            if data.get("requestId") != req_id:
                print(f"[DEBUG] Skipping msg type={mtype} (different requestId)", flush=True)
                continue
            print(f"[DEBUG] MATCH type={mtype}", flush=True)
            if mtype == "STREAM_CHUNK":
                chunk = data.get("text", "")
                sys.stdout.write(chunk)
                sys.stdout.flush()
                full_text += chunk
            elif mtype == "FINAL":
                print(f"\n[Turn {i} COMPLETE | {len(data.get('full', full_text))} chars]")
                break
            elif mtype == "ERROR":
                print(f"\n[ERROR] {data.get('error')}")
                break

        if i < len(FOLLOWUP_PROMPTS):
            await asyncio.sleep(3.0)

    print("\n DONE!")
    writer.close()

if __name__ == "__main__":
    asyncio.run(main())
