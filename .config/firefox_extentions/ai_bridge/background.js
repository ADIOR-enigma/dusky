// background.js - Firefox 153 MV3 - ESM - Event Page
// Bare bones flawless bridge: WS <-> Tabs

const WS_URL = "ws://127.0.0.1:8765";
const AI_URL_PATTERNS = ["chatgpt.com", "chat.openai.com", "claude.ai", "meta.ai"];

let ws = null;
let retry = 0;
let isConnecting = false;
let activeTabId = null;

const BRIDGE_KEY = "__LOCAL_AI_BRIDGE__";

function log(...args){ console.log("[LocalAI BG]", ...args); }

// --- Badge / Title (minimal UI) ---
async function setStatus(connected){
  try{
    const title = connected ? "Local AI Bridge - Connected" : "Local AI Bridge - Disconnected";
    const text = connected ? "ON" : "OFF";
    const color = connected ? "#2ecc71" : "#e74c3c";
    if(browser.action){
      await browser.action.setTitle({title});
      await browser.action.setBadgeText({text});
      await browser.action.setBadgeBackgroundColor({color});
    }
  }catch(e){}
}

// --- WebSocket ---
function connectWS(){
  if(isConnecting || (ws && ws.readyState === WebSocket.OPEN)) return;
  isConnecting = true;
  log(`Connecting to ${WS_URL} attempt ${retry}`);
  try{
    ws = new WebSocket(WS_URL);
  }catch(e){
    scheduleReconnect();
    return;
  }

  ws.addEventListener("open", async ()=>{
    log("WS open");
    isConnecting = false;
    retry = 0;
    await setStatus(true);
  });

  ws.addEventListener("message", async (event)=>{
    let msg;
    try{ msg = JSON.parse(event.data); }catch{ return; }
    // Expected from Python: {type:"RUN_QUERY", query:"...", requestId:"...", tabId?:number}
    if(msg.type === "RUN_QUERY" && msg.query){
      const targetId = await findTargetTab(msg.tabId);
      if(!targetId){
        ws?.send(JSON.stringify({type:"ERROR", error:"No AI tab found. Open chatgpt.com/claude.ai", requestId: msg.requestId}));
        return;
      }
      try{
        await browser.tabs.sendMessage(targetId, {type:"RUN_QUERY", query: msg.query, requestId: msg.requestId});
      }catch(e){
        ws?.send(JSON.stringify({type:"ERROR", error: String(e), requestId: msg.requestId}));
      }
    }
  });

  ws.addEventListener("close", ()=>{
    log("WS closed");
    isConnecting = false;
    setStatus(false);
    scheduleReconnect();
  });

  ws.addEventListener("error", ()=>{
    log("WS error");
    isConnecting = false;
    try{ ws.close(); }catch{}
    setStatus(false);
    scheduleReconnect();
  });
}

function scheduleReconnect(){
  retry++;
  const delay = Math.min(1000 * Math.pow(1.5, retry), 30000);
  log(`Reconnect in ${delay}ms`);
  setTimeout(connectWS, delay);
  // Alarms for when event page dies
  try{
    browser.alarms.create("reconnect", {when: Date.now()+delay});
  }catch{}
}

// --- Tab targeting ---
async function findTargetTab(preferredId){
  // 1. preferred
  if(preferredId){
    try{
      const tab = await browser.tabs.get(preferredId);
      if(tab && AI_URL_PATTERNS.some(p=>tab.url?.includes(p))) return preferredId;
    }catch{}
  }
  // 2. last active
  if(activeTabId){
    try{
      const tab = await browser.tabs.get(activeTabId);
      if(tab && AI_URL_PATTERNS.some(p=>tab.url?.includes(p))) return activeTabId;
    }catch{}
  }
  // 3. query most recent AI tab
  const tabs = await browser.tabs.query({url: ["*://chatgpt.com/*","*://chat.openai.com/*","*://claude.ai/*","*://*.meta.ai/*","*://meta.ai/*"]});
  if(!tabs.length) return null;
  tabs.sort((a,b)=> (b.lastAccessed||0)-(a.lastAccessed||0));
  activeTabId = tabs[0].id;
  try{ await browser.storage.session.set({activeTabId}); }catch{}
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

// --- Messages from content script (MAIN -> BG) ---
browser.runtime.onMessage.addListener((msg, sender)=>{
  // msg = {type:"STREAM_CHUNK"|"FINAL"|"ERROR", text?, full?, requestId?}
  if(!msg?.type) return;
  // keep activeTabId fresh
  if(sender?.tab?.id) activeTabId = sender.tab.id;
  // Forward to WS server (terminal AI)
  if(ws && ws.readyState === WebSocket.OPEN){
    try{
      ws.send(JSON.stringify({...msg, tabId: sender?.tab?.id}));
    }catch(e){ log("WS send fail", e); }
  }
});

// --- Lifecycle ---
browser.alarms.onAlarm.addListener((alarm)=>{
  if(alarm.name === "reconnect") connectWS();
});

browser.runtime.onStartup.addListener(()=> connectWS());
browser.runtime.onInstalled.addListener(()=> connectWS());

// Top-level immediate connect (ESM top-level await allowed in Fx 128+)
try{
  const stored = await browser.storage.session.get("activeTabId");
  if(stored?.activeTabId) activeTabId = stored.activeTabId;
}catch{}
connectWS();

if(browser.action?.onClicked){
  browser.action.onClicked.addListener(()=> connectWS());
}
