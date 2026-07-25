// background.js - Firefox 153 MV3 - ESM - Event Page
// Robust bridge with activeTabId Lock, Auto-Healing, & Universal Support

const WS_URL = "ws://127.0.0.1:8765";
const AI_URL_PATTERNS = ["chatgpt.com", "chat.openai.com", "claude.ai", "meta.ai", "gemini.google.com"];

let ws = null;
let isConnecting = false;
let activeTabId = null;
let heartbeatInterval = null;

function log(...args){ console.log("[LocalAI BG]", ...args); }

// --- Badge / Title ---
async function setStatus(connected, extra=""){
  try{
    const title = connected ? "Local AI Bridge - Connected" : `Local AI Bridge - Disconnected ${extra}`.trim();
    const text = connected ? "ON" : "OFF";
    const color = connected ? "#2ecc71" : "#e74c3c";
    if(browser.action){
      await browser.action.setTitle({title});
      await browser.action.setBadgeText({text});
      await browser.action.setBadgeBackgroundColor({color});
    }
  }catch(e){}
}

// --- WebSocket Connection ---
function connectWS(force = false){
  if(!force && ws && ws.readyState === WebSocket.OPEN){
    setStatus(true);
    return;
  }

  isConnecting = false;
  if(ws){
    try{ ws.close(); }catch(e){}
    ws = null;
  }

  try{
    ws = new WebSocket(WS_URL);
  }catch(e){
    setStatus(false, "Init Error");
    scheduleReconnect();
    return;
  }

  ws.addEventListener("open", async ()=>{
    log("WS connected successfully!");
    await setStatus(true);
    startHeartbeat();
  });

  ws.addEventListener("message", async (event)=>{
    let msg;
    try{ msg = JSON.parse(event.data); }catch{ return; }

    if(msg.type === "RELOAD_EXTENSION"){
      log("Reconnecting extension bridge...");
      try{ ws.send(JSON.stringify({type: "RELOAD_ACK"})); }catch{}
      connectWS(true);
      return;
    }

    if(msg.type === "PING"){
      try{ ws.send(JSON.stringify({type: "PONG"})); }catch{}
      return;
    }

    if(msg.type === "DIAGNOSE_TABS"){
      try{
        const all = await browser.tabs.query({});
        const tabList = all.map(t => ({id: t.id, url: t.url, active: t.active, title: t.title}));
        ws?.send(JSON.stringify({type: "DIAGNOSE_REPLY", tabs: tabList}));
      }catch(e){
        ws?.send(JSON.stringify({type: "DIAGNOSE_REPLY", error: String(e)}));
      }
      return;
    }

    if(msg.type === "RUN_QUERY" && msg.query){
      const targetId = await findTargetTab(msg.tabId);
      if(!targetId){
        ws?.send(JSON.stringify({type:"ERROR", error:"No AI tab found. Open chatgpt.com/claude.ai/gemini.google.com", requestId: msg.requestId}));
        return;
      }

      try{
        const tab = await browser.tabs.get(targetId);
        if(tab.windowId) await browser.windows.update(tab.windowId, {focused: true});
        await browser.tabs.update(targetId, {active: true});
      }catch(e){ log("Focus error (non-fatal):", e); }

      try{
        browser.tabs.sendMessage(targetId, msg).catch((e)=>{
          ws?.send(JSON.stringify({type:"ERROR", error:"Tab message failed: " + e.message, requestId: msg.requestId}));
        });
      }catch(e){
        ws?.send(JSON.stringify({type:"ERROR", error:"Tab message error: " + e.message, requestId: msg.requestId}));
      }
    }
  });

  ws.addEventListener("close", ()=>{
    stopHeartbeat();
    ws = null;
    setStatus(false);
    scheduleReconnect();
  });

  ws.addEventListener("error", ()=>{
    stopHeartbeat();
    if(ws){ try{ ws.close(); }catch{} }
    ws = null;
    setStatus(false, "Err");
    scheduleReconnect();
  });
}

function startHeartbeat(){
  stopHeartbeat();
  heartbeatInterval = setInterval(()=>{
    if(ws && ws.readyState === WebSocket.OPEN){
      try{
        ws.send(JSON.stringify({type: "HEARTBEAT"}));
      }catch(e){
        connectWS(true);
      }
    } else {
      connectWS(true);
    }
  }, 3000);
}

function stopHeartbeat(){
  if(heartbeatInterval){
    clearInterval(heartbeatInterval);
    heartbeatInterval = null;
  }
}

function scheduleReconnect(){
  setTimeout(() => connectWS(false), 2000);
}

// --- Firefox MV3 Alarms Keep-Alive & Auto-Heal ---
try {
  browser.alarms?.create("keepAlive", {periodInMinutes: 0.1});
  browser.alarms?.onAlarm.addListener((alarm) => {
    if(alarm.name === "keepAlive"){
      if(!ws || ws.readyState !== WebSocket.OPEN){
        connectWS(true);
      }
    }
  });
} catch(e) {}

// --- activeTabId Lock Priority Tab Targeting ---
async function findTargetTab(preferredId){
  if(preferredId){
    try{
      const tab = await browser.tabs.get(preferredId);
      if(tab && AI_URL_PATTERNS.some(p=>tab.url?.includes(p))) return preferredId;
    }catch{}
  }

  // 1. ALWAYS try active tab in currently focused window first
  try{
    const activeTabs = await browser.tabs.query({active: true, lastFocusedWindow: true});
    if(activeTabs.length > 0 && activeTabs[0].url && AI_URL_PATTERNS.some(p => activeTabs[0].url.includes(p))){
      log("Found via active focused tab:", activeTabs[0].id);
      activeTabId = activeTabs[0].id;
      return activeTabs[0].id;
    }
  }catch(e){}

  // 2. Try last active/communicated tab ID
  if(activeTabId){
    try{
      const tab = await browser.tabs.get(activeTabId);
      if(tab && AI_URL_PATTERNS.some(p=>tab.url?.includes(p))){
        log("Found via last activeTabId:", activeTabId);
        return activeTabId;
      }
    }catch{}
  }

  // 3. Query open AI tabs
  try{
    const allTabs = await browser.tabs.query({});
    const aiTabs = allTabs.filter(tab => tab.url && AI_URL_PATTERNS.some(p => tab.url.includes(p)));
    if(aiTabs.length > 0){
      aiTabs.sort((a,b) => {
        if(a.active && !b.active) return -1;
        if(!a.active && b.active) return 1;
        return (b.lastAccessed || 0) - (a.lastAccessed || 0);
      });
      activeTabId = aiTabs[0].id;
      return aiTabs[0].id;
    }
  }catch(e){}

  return null;
}

// Track active tab
browser.tabs.onActivated?.addListener(async ({tabId})=>{
  connectWS();
  try{
    const tab = await browser.tabs.get(tabId);
    if(tab.url && AI_URL_PATTERNS.some(p=>tab.url.includes(p))){
      activeTabId = tabId;
      try{ await browser.storage.session.set({activeTabId}); }catch{}
    }
  }catch{}
});

browser.tabs.onUpdated?.addListener(async (tabId, change, tab)=>{
  if(change.url || change.status === "complete"){
    if(tab.url && AI_URL_PATTERNS.some(p=>tab.url.includes(p))){
      activeTabId = tabId;
      try{ await browser.storage.session.set({activeTabId}); }catch{}
    }
  }
});

// Forward content script messages -> WS & Lock activeTabId
browser.runtime.onMessage.addListener((msg, sender)=>{
  if(!msg?.type) return;
  if(sender?.tab?.id) activeTabId = sender.tab.id;
  if(ws && ws.readyState === WebSocket.OPEN){
    try{
      ws.send(JSON.stringify({...msg, tabId: sender?.tab?.id}));
    }catch(e){}
  }
});

browser.runtime.onStartup.addListener(()=> connectWS(true));
browser.runtime.onInstalled.addListener(()=> connectWS(true));

try{
  const stored = await browser.storage.session.get("activeTabId");
  if(stored?.activeTabId) activeTabId = stored.activeTabId;
}catch{}
connectWS(true);

if(browser.action?.onClicked){
  browser.action.onClicked.addListener(()=> connectWS(true));
}
