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

let reconnectTimer = null;

// --- WebSocket Connection ---
function connectWS(force = false){
  if(!force && ws && ws.readyState === WebSocket.OPEN){
    setStatus(true);
    return;
  }

  if(isConnecting && !force) return;
  isConnecting = true;

  if(ws){
    try{
      ws.onopen = null;
      ws.onmessage = null;
      ws.onerror = null;
      ws.onclose = null;
      ws.close();
    }catch(e){}
    ws = null;
  }

  try{
    ws = new WebSocket(WS_URL);
  }catch(e){
    isConnecting = false;
    setStatus(false, "Init Error");
    scheduleReconnect();
    return;
  }

  ws.onopen = async ()=>{
    isConnecting = false;
    log("WS connected successfully!");
    await setStatus(true);
  };

  ws.onmessage = async (event)=>{
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
        browser.tabs.sendMessage(targetId, msg).catch(async (e)=>{
          log("Tab message failed (orphaned content script), auto-healing tab:", targetId, e);
          try {
            await browser.tabs.reload(targetId);
            const onLoaded = (tId, changeInfo) => {
              if(tId === targetId && changeInfo.status === "complete"){
                browser.tabs.onUpdated.removeListener(onLoaded);
                setTimeout(() => {
                  browser.tabs.sendMessage(targetId, msg).catch((err) => {
                    ws?.send(JSON.stringify({type:"ERROR", error:"Tab message failed after auto-heal: " + err.message, requestId: msg.requestId}));
                  });
                }, 1200);
              }
            };
            browser.tabs.onUpdated.addListener(onLoaded);
          } catch(reloadErr) {
            ws?.send(JSON.stringify({type:"ERROR", error:"Tab message failed: " + e.message, requestId: msg.requestId}));
          }
        });
      }catch(e){
        ws?.send(JSON.stringify({type:"ERROR", error:"Tab message error: " + e.message, requestId: msg.requestId}));
      }
    }
  };

  ws.onclose = ()=>{
    isConnecting = false;
    ws = null;
    setStatus(false);
    scheduleReconnect();
  };

  ws.onerror = ()=>{
    isConnecting = false;
    if(ws){
      try{
        ws.onopen = null;
        ws.onmessage = null;
        ws.onerror = null;
        ws.onclose = null;
        ws.close();
      }catch(e){}
      ws = null;
    }
    setStatus(false, "Err");
    scheduleReconnect();
  };
}

function scheduleReconnect(){
  if(reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectWS(false);
  }, 2500);
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

const knownAiTabs = new Set();

// --- activeTabId Lock Priority Tab Targeting ---
async function findTargetTab(preferredId){
  if(preferredId){
    try{
      const tab = await browser.tabs.get(preferredId);
      if(tab) return preferredId;
    }catch{}
  }

  if(activeTabId){
    try{
      const tab = await browser.tabs.get(activeTabId);
      if(tab) return activeTabId;
    }catch{
      activeTabId = null;
    }
  }

  for(const tabId of knownAiTabs){
    try{
      const tab = await browser.tabs.get(tabId);
      if(tab){
        activeTabId = tabId;
        return tabId;
      }
    }catch{
      knownAiTabs.delete(tabId);
    }
  }

  try{
    const allTabs = await browser.tabs.query({});
    const aiTabs = allTabs.filter(tab => tab.url && AI_URL_PATTERNS.some(p => tab.url.includes(p)));
    if(aiTabs.length > 0){
      activeTabId = aiTabs[0].id;
      return aiTabs[0].id;
    }
  }catch(e){}

  return null;
}

// Track active tab
browser.tabs.onActivated?.addListener(async ({tabId})=>{
  activeTabId = tabId;
});

browser.tabs.onUpdated?.addListener(async (tabId, change, tab)=>{
  if(tab.url && AI_URL_PATTERNS.some(p=>tab.url.includes(p))){
    activeTabId = tabId;
    knownAiTabs.add(tabId);
  }
});

// Forward content script messages -> WS & Lock activeTabId
browser.runtime.onMessage.addListener((msg, sender)=>{
  if(!msg?.type) return;
  if(sender?.tab?.id){
    activeTabId = sender.tab.id;
    knownAiTabs.add(sender.tab.id);
  }
  if(msg.type === "TAB_REGISTER"){
    log("Tab registered via content script:", sender?.tab?.id);
    return;
  }
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
