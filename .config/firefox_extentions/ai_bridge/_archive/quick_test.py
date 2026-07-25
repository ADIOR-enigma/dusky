#!/usr/bin/env python3
"""
Quick Test Script - 1-Turn Verification against Live Daemon
"""

import socket
import json
import base64
import os
import time

HOST = "127.0.0.1"
PORT = 8765

def main():
    print("==================================================")
    print("        Quick 1-Turn Live Verification")
    print("==================================================")
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((HOST, PORT))
    except Exception as e:
        print(f"[!] Could not connect to daemon on {HOST}:{PORT}: {e}")
        return

    key = base64.b64encode(os.urandom(16)).decode('utf-8')
    req = (
        f"GET / HTTP/1.1\r\n"
        f"Host: {HOST}:{PORT}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode('utf-8'))
    resp = sock.recv(4096).decode('utf-8', errors='ignore')
    if "101" not in resp:
        print(f"[!] Handshake failed: {resp}")
        return

    print("[+] Connected to live bridge daemon!")
    prompt = "Tell me a 1-sentence funny joke about programming."
    print(f"[>] Sending Prompt: \"{prompt}\"\n")

    payload = json.dumps({"type": "RUN_QUERY", "query": prompt, "requestId": "quick-1"}).encode('utf-8')
    length = len(payload)
    mask = os.urandom(4)
    masked_payload = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))
    header = bytes([0x81, 0x80 | length])
    sock.sendall(header + mask + masked_payload)

    start = time.time()
    buf = ""
    while True:
        try:
            head = sock.recv(2)
            if not head or len(head) < 2: break
            l = head[1] & 0x7F
            if l == 126: l = int.from_bytes(sock.recv(2), 'big')
            elif l == 127: l = int.from_bytes(sock.recv(8), 'big')
            
            p = bytearray()
            while len(p) < l:
                c = sock.recv(l - len(p))
                if not c: break
                p.extend(c)
                
            msg = json.loads(p.decode('utf-8', errors='replace'))
            mtype = msg.get("type")
            if mtype == "STREAM_CHUNK":
                chunk = msg.get("text", "")
                buf += chunk
                print(chunk, end="", flush=True)
            elif mtype in ("FINAL", "ERROR"):
                dur = time.time() - start
                if mtype == "FINAL":
                    print(f"\n\n[TEST PASSED in {dur:.2f}s | Received {len(buf)} chars]")
                else:
                    print(f"\n\n[TEST ERROR]: {msg.get('error')}")
                break
        except Exception as e:
            print(f"\n[Socket error]: {e}")
            break

    print("\n==================================================")
    print(" 🎉 QUICK VERIFICATION TEST COMPLETE!")
    print("==================================================")

if __name__ == "__main__":
    main()
