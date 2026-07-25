# Local AI Bridge - Firefox 153 - Bare Bones

Minimal flawless extension that lets your terminal local AI control web AI (ChatGPT/Claude/Meta AI) by typing into the page and streaming back results.

## Files

- manifest.json - MV3 with background.scripts (Firefox 153 does NOT support service_worker) + world:MAIN injection (since 128)
- background.js - ESM module, WebSocket client to localhost:8765, tab targeting, badge status
- content_isolated.js - ISOLATED world bridge background <-> MAIN
- content_main.js - MAIN world, document_start: fetch hook + Lexical typing + MutationObserver streaming
- bridge.py - Python WebSocket server that your local AI talks to

## Install - Firefox 153

### Temporary (all editions, survives until restart, best for dev)

1. Open about:debugging#/runtime/this-firefox
2. Load Temporary Add-on -> select manifest.json folder
3. Open chatgpt.com / claude.ai / meta.ai and keep logged in

### Permanent unsigned (Developer, Nightly, ESR only)

1. about:config -> xpinstall.signatures.required = false
2. about:addons -> Gear -> Install Add-on From File -> zip files as .xpi (zip contents, not folder)

Release/Beta 153 hard-locks signatures true - use Developer Edition.

## Run

pip install websockets
python bridge.py

- Extension auto-connects to ws://127.0.0.1:8765
- Badge shows ON/OFF
- Type query in terminal, it types into web AI tab and streams back

## Integrate with local AI

Replace input() loop in bridge.py:

```python
from ollama import AsyncClient

async def local_ai_task(user_prompt):
    # local LLM decides to use web AI
    rid = await send_query(f"Search web: {user_prompt}")
    # wait for pending[rid] to fill via FINAL event
```

## Why this design is flawless for 153

- Uses world:MAIN directly in manifest (no CSP bypass hacks needed, GA since 128)
- Uses background.scripts with type:module (service_worker not supported in Firefox)
- Uses execCommand insertText for Lexical editors (only way that updates internal state in 2026)
- Uses fetch tee() + MutationObserver dual extraction (covers all providers)
- Minimal UI: only action badge, no popup
- No backwards compat shims
