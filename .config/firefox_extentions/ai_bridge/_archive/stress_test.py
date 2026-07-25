#!/usr/bin/env python3
import sys
import os
import json
import asyncio
import socket
import base64
import uuid
import struct
import time

HOST = "127.0.0.1"
PORT = 8765

async def run_single_prompt(prompt_text, prompt_idx):
    print(f"\n==========================================", flush=True)
    print(f"[TEST {prompt_idx}] Sending prompt: '{prompt_text}'", flush=True)
    print(f"==========================================", flush=True)
    
    s = socket.socket()
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
    payload = json.dumps({"type": "RUN_QUERY", "query": prompt_text, "requestId": req_id}).encode('utf-8')
    mask = os.urandom(4)
    masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))
    frame = bytearray([0x81, 0x80 | len(payload)]) + mask + masked
    s.sendall(frame)

    full_text = ""
    start_time = time.time()
    while True:
        try:
            head = s.recv(2)
            if not head: break
            l = head[1] & 0x7F
            if l == 126: l = struct.unpack("!H", s.recv(2))[0]
            elif l == 127: l = struct.unpack("!Q", s.recv(8))[0]

            p = bytearray()
            while len(p) < l:
                c = s.recv(l - len(p))
                if not c: break
                p.extend(c)

            data = json.loads(p.decode('utf-8'))
            if data.get("requestId") != req_id: continue

            mtype = data.get("type")
            if mtype == "STREAM_CHUNK":
                chunk = data.get("text", "")
                sys.stdout.write(chunk)
                sys.stdout.flush()
                full_text = data.get("full", full_text + chunk)
            elif mtype == "FINAL":
                print("\n" + "-"*40, flush=True)
                final_res = data.get("full", full_text)
                print(f"[TEST {prompt_idx} SUCCESS] Final response received ({len(final_res)} chars in {time.time()-start_time:.1f}s):", flush=True)
                print(f"--> {final_res}", flush=True)
                s.close()
                return True, final_res
            elif mtype == "ERROR":
                print(f"\n[TEST {prompt_idx} FAILED] Error: {data.get('error')}", flush=True)
                s.close()
                return False, data.get('error')
        except Exception as e:
            print(f"\n[TEST {prompt_idx} EXCEPTION] {e}", flush=True)
            s.close()
            return False, str(e)

async def main():
    prompts = [
        "Write a 1-sentence joke about programming.",
        "What is 35 * 12?",
        "List 3 primary colors in lowercase separated by commas."
    ]
    
    results = []
    for idx, p in enumerate(prompts, 1):
        ok, res = await run_single_prompt(p, idx)
        results.append((idx, p, ok, res))
        await asyncio.sleep(2.0)
        
    print("\n==========================================", flush=True)
    print("STRESS TEST SUMMARY REPORT", flush=True)
    print("==========================================", flush=True)
    all_passed = True
    for idx, p, ok, res in results:
        status = "PASSED" if ok else "FAILED"
        if not ok: all_passed = False
        print(f"Test {idx}: [{status}] Prompt: '{p}'", flush=True)
        if ok:
            print(f"  Response: {res[:120]}...", flush=True)
    
    if all_passed:
        print("\nALL STRESS TESTS PASSED 100% SUCCESSFULLY!", flush=True)
    else:
        print("\nSOME TESTS FAILED", flush=True)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
