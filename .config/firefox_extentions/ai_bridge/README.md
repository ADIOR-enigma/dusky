# Local AI Web Bridge

A lightweight, zero-latency Firefox MV3 extension & CLI daemon that allows any local AI agent (Ollama, Antigravity, Llama.cpp, LangChain, or custom scripts) to interact directly with web-based AI platforms (Google Gemini, ChatGPT, Claude) with **100% event-driven, streamable prompt execution**.

---

## 🚀 Quick Start for Local AI Agents & Developers

Any local AI or script can send prompts to Web AI models with **zero technical setup** by executing the Python bridge CLI:

```bash
python3 /home/dusk/.config/firefox_extentions/ai_bridge/bridge.py "Your prompt text here"
```

### Chaining Follow-Up Prompts (Event-Driven, Zero Blind Waiting)

You **do not need to use sleep timers**. The `bridge.py` script waits for the browser's `FINAL` completion event and **exits immediately** as soon as the response finishes streaming.

You can chain multiple follow-up prompts sequentially in a simple Python loop or bash pipeline:

#### Python Example:
```python
import subprocess

prompts = [
    "What is 10 + 10?",
    "Now multiply that result by 3.",
    "Now add 5 to that."
]

for prompt in prompts:
    # Synchronously runs query, streams output, and returns as soon as complete
    result = subprocess.run(
        ["python3", "/home/dusk/.config/firefox_extentions/ai_bridge/bridge.py", prompt],
        capture_output=True, text=True
    )
    print("Web AI Answer:", result.stdout.strip())
```

#### Bash One-Liner:
```bash
python3 bridge.py "Explain quantum computing in 1 sentence" && python3 bridge.py "Simplify that for a 5-year-old"
```

---

## 🛠️ How It Works Under the Hood

```
+-------------------+      WebSocket      +----------------------+      WebExtension      +------------------------+
| Local AI / Script |  ================>  | Python Bridge Daemon |  ====================> | Firefox Extension      |
| (bridge.py CLI)   |  <================  | (127.0.0.1:8765)     |  <==================== | (Gemini/Claude/ChatGPT)|
+-------------------+     JSON Packets    +----------------------+      JSON Packets      +------------------------+
```

1. **Daemon Service**: `localai_bridge.service` runs a systemd user daemon hosting a WebSocket server on `127.0.0.1:8765`.
2. **Browser Extension**: Listens on the active AI web tab (`gemini.google.com`, `chatgpt.com`, `claude.ai`), auto-reconnecting via `browser.alarms` keep-alive.
3. **Execution**:
   - `RUN_QUERY` is received by the extension.
   - Extension focuses the active AI tab and sets text inside the rich editor.
   - OS-level keystroke fallback (`wtype -k Return` + Hyprland IPC window focus) fires to submit the prompt cleanly.
   - `MutationObserver` streams tokens (`STREAM_CHUNK`) in real-time back to stdout.
   - Once generation stops, `FINAL` is emitted and `bridge.py` exits cleanly with code `0`.

---

## 📂 System Architecture & Files

- **`manifest.json`**: Manifest V3 extension configuration with host permissions `<all_urls>` and permissions (`storage`, `tabs`, `activeTab`, `scripting`, `alarms`).
- **`background.js`**: Background event page that auto-heals WebSocket connections to `127.0.0.1:8765` and targets active AI browser tabs.
- **`content_isolated.js`**: Isolated content script bridging extension messages and main window DOM.
- **`content_main.js`**: Main world page script performing Shadow DOM traversal, text typing, mutation observing, and token streaming.
- **`bridge.py`**: Python CLI client & daemon server.

---

## ⚡ Extension Status Check

- Check daemon status: `systemctl --user status localai_bridge.service`
- Check active client connections:
  ```bash
  python3 -c "import socket, json; s=socket.socket(); s.connect(('127.0.0.1', 8765)); s.sendall(b'GET / HTTP/1.1\r\n\r\n'); print(s.recv(1024))"
  ```
- Firefox top-right toolbar icon shows **ON** (green badge) when connected and ready.
