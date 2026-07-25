#!/usr/bin/env python3
"""Send 3 follow-up prompts. Waits for FINAL message only to avoid stream duplication."""
import socket, json, os, sys, base64, struct, uuid, subprocess, time

HOST, PORT = "127.0.0.1", 8765

FOLLOWUP_PROMPTS = [
    "Explain the difference between a stack and a queue in 2 sentences.",
    "Give me a simple Python example of each.",
    "Now show me how to reverse a stack using recursion.",
]

def ws_connect():
    s = socket.socket()
    s.connect((HOST, PORT))
    sec_key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(
        f"GET / HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {sec_key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n".encode()
    )
    resp = s.recv(1024)
    if b"101" not in resp:
        raise Exception("Handshake failed")
    return s

def ws_send(s, message):
    payload = message.encode('utf-8')
    mask = os.urandom(4)
    masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))
    l = len(payload)
    if l <= 125:
        frame = bytearray([0x81, 0x80 | l]) + mask + masked
    elif l <= 65535:
        frame = bytearray([0x81, 0x80 | 126]) + mask + struct.pack("!H", l) + masked
    else:
        frame = bytearray([0x81, 0x80 | 127]) + mask + struct.pack("!Q", l) + masked
    s.sendall(frame)

def ws_recv(s, timeout=60):
    s.settimeout(timeout)
    try:
        head = s.recv(2)
    except socket.timeout:
        return None
    if not head:
        return None
    l = head[1] & 0x7F
    if l == 126: l = struct.unpack("!H", s.recv(2))[0]
    elif l == 127: l = struct.unpack("!Q", s.recv(8))[0]
    if head[1] & 0x80: s.recv(4)
    p = bytearray()
    while len(p) < l:
        c = s.recv(l - len(p))
        if not c: break
        p.extend(c)
    return p.decode('utf-8', errors='replace')

def focus_firefox():
    try:
        res = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            clients = json.loads(res.stdout)
            ff = [c for c in clients if 'firefox' in c.get('class','').lower()]
            if ff:
                addr = ff[0]['address']
                subprocess.run(["hyprctl", "dispatch", f'hl.dsp.window.bring_to_top({{ window = "address:{addr}" }})'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
    except Exception: pass

def press_return():
    import time as _time
    _time.sleep(0.3)
    try:
        subprocess.run(["wtype", "-k", "Return"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except Exception: pass

s = ws_connect()
print("Connected to daemon.\n", flush=True)

for i, prompt in enumerate(FOLLOWUP_PROMPTS, 1):
    print(f"{'='*50}", flush=True)
    print(f" TURN {i}: {prompt}", flush=True)
    print(f"{'='*50}\n", flush=True)

    req_id = str(uuid.uuid4())
    ws_send(s, json.dumps({"type": "RUN_QUERY", "query": prompt, "requestId": req_id}))

    focus_firefox()
    import threading
    t = threading.Timer(1.8, press_return)
    t.daemon = True
    t.start()

    while True:
        raw = ws_recv(s, timeout=60)
        if raw is None:
            print("[TIMEOUT - no response]", flush=True)
            break
        try:
            data = json.loads(raw)
        except:
            continue
        if data.get("requestId") != req_id:
            continue
        mtype = data.get("type")
        if mtype == "STREAM_CHUNK":
            chunk = data.get("text", "")
            sys.stdout.write(chunk)
            sys.stdout.flush()
        elif mtype == "FINAL":
            full = data.get("full", "")
            sys.stdout.write("\n")
            sys.stdout.flush()
            print(f"\n[Turn {i} COMPLETE | {len(full)} chars]", flush=True)
            break
        elif mtype == "ERROR":
            print(f"\n[ERROR] {data.get('error')}", flush=True)
            break

    if i < len(FOLLOWUP_PROMPTS):
        time.sleep(3.0)

print(f"\n{'='*50}", flush=True)
print(" ALL 3 FOLLOW-UP PROMPTS SENT!", flush=True)
print(f"{'='*50}", flush=True)
s.close()
