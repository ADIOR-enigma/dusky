// background.js - Firefox 153 MV3 - ESM - Event Page
// Robust bridge with Heartbeat, Tab Communication Retry, and Remote Reload

const WS_URL = "ws://127.0.0.1:8765";
const AI_URL_PATTERNS = ["chatgpt.com", "chat.openai.com", "claude.ai", "meta.ai", "gemini.google.com"];

let ws = null;
let retry = 0;
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
  if(force && ws){
    try{ ws.close(); }catch{}
    ws = null;
  }
  if(ws && ws.readyState === WebSocket.OPEN){
    setStatus(true);
    return;
  }
  if(isConnecting) return;
  isConnecting = true;
  log(`Connecting to ${WS_URL} (attempt ${retry})`);

  try{
    if(ws){ try{ ws.close(); }catch{} }
    ws = new WebSocket(WS_URL);
  }catch(e){
    log("WS init error", e);
    isConnecting = false;
    setStatus(false, "Init Error");
    scheduleReconnect();
    return;
  }

  ws.addEventListener("open", async ()=>{
    log("WS connected successfully!");
    isConnecting = false;
    retry = 0;
    await setStatus(true);
    startHeartbeat();
  });

  ws.addEventListener("message", async (event)=>{
    let msg;
    try{ msg = JSON.parse(event.data); }catch{ return; }

    if(msg.type === "RELOAD_EXTENSION"){
      log("Reloading extension...");
      try{ ws.send(JSON.stringify({type: "RELOAD_ACK"})); }catch{}
      browser.runtime.reload();
      return;
    }

    if(msg.type === "PING"){
      try{ ws.send(JSON.stringify({type: "PONG"})); }catch{}
      return;
    }

    if(msg.type === "RUN_QUERY" && msg.query){
      const targetId = await findTargetTab(msg.tabId);
      if(!targetId){
        ws?.send(JSON.stringify({type:"ERROR", error:"No AI tab found. Open chatgpt.com/claude.ai/gemini.google.com", requestId: msg.requestId}));
        return;
      }
      
      let sent = false;
      let lastErr = "";
      for(let attempt = 0; attempt < 6; attempt++){
        try{
          await browser.tabs.sendMessage(targetId, {type:"RUN_QUERY", query: msg.query, requestId: msg.requestId});
          sent = true;
          break;
        }catch(e){
          lastErr = String(e);
          await new Promise(r => setTimeout(r, 600));
        }
      }
      if(!sent){
        ws?.send(JSON.stringify({type:"ERROR", error: lastErr || "Tab connection failed", requestId: msg.requestId}));
      }
    }
  });

  ws.addEventListener("close", (e)=>{
    log("WS closed", e);
    isConnecting = false;
    stopHeartbeat();
    setStatus(false);
    scheduleReconnect();
  });

  ws.addEventListener("error", (e)=>{
    log("WS error", e);
    isConnecting = false;
    stopHeartbeat();
    try{ ws.close(); }catch{}
    setStatus(false, "Err");
  });
}

function startHeartbeat(){
  stopHeartbeat();
  heartbeatInterval = setInterval(()=>{
    if(ws && ws.readyState === WebSocket.OPEN){
      try{
        ws.send(JSON.stringify({type: "HEARTBEAT"}));
      }catch(e){
        log("Heartbeat failed, reconnecting...");
        connectWS(true);
      }
    } else {
      connectWS(true);
    }
  }, 5000);
}

function stopHeartbeat(){
  if(heartbeatInterval){
    clearInterval(heartbeatInterval);
    heartbeatInterval = null;
  }
}

function scheduleReconnect(){
  retry++;
  const delay = Math.min(1000 * Math.pow(1.5, retry), 15000);
  log(`Reconnect scheduled in ${delay}ms`);
  setTimeout(() => connectWS(false), delay);
  try{
    browser.alarms.create("reconnect", {when: Date.now()+delay});
  }catch{}
}

// --- Tab targeting ---
async function findTargetTab(preferredId){
  if(preferredId){
    try{
      const tab = await browser.tabs.get(preferredId);
      if(tab && AI_URL_PATTERNS.some(p=>tab.url?.includes(p))) return preferredId;
    }catch{}
  }
  if(activeTabId){
    try{
      const tab = await browser.tabs.get(activeTabId);
      if(tab && AI_URL_PATTERNS.some(p=>tab.url?.includes(p))) return activeTabId;
    }catch{}
  }
  const tabs = await browser.tabs.query({url: ["*://chatgpt.com/*","*://chat.openai.com/*","*://claude.ai/*","*://*.meta.ai/*","*://meta.ai/*","*://gemini.google.com/*"]});
  if(!tabs.length) return null;
  tabs.sort((a,b)=> (b.lastAccessed||0)-(a.lastAccessed||0));
  activeTabId = tabs[0].id;
  try{ await browser.storagesession.set({activeTabId}); }catch{}
  return activeTabId;
}

// Track active tab
browser.tabs.onActivated?.addListener(async ({tabId})=>{
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

// Forward content script messages -> WS
browser.runtime.onMessage.addListener((msg, sender)=>{
  if(!msg?.type) return;
  if(sender?.tab?.id) activeTabId = sender.tab.id;
  if(ws && ws.readyState === WebSocket.OPEN){
    try{
      ws.send(JSON.stringify({...msg, tabId: sender?.tab?.id}));
    }catch(e){ log("WS send fail", e); }
  }
});

browser.alarms.onAlarm.addListener((alarm)=>{
  if(alarm.name === "reconnect") connectWS();
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
