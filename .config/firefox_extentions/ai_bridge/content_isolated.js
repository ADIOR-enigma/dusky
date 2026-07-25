// content_isolated.js - ISOLATED world, document_start
// Bridge: background <-> MAIN world

const BRIDGE = "__LOCAL_AI_BRIDGE__";

(() => {
  if (window.__ISOLATED_BRIDGE_INJECTED__) return;
  window.__ISOLATED_BRIDGE_INJECTED__ = true;

  // Forward MAIN -> background
  window.addEventListener("message", (e) => {
    if (e.source !== window) return;
    const data = e.data;
    if (!data || data.__bridge !== BRIDGE) return;
    if (data.direction !== "MAIN_TO_BG") return;
    // data.payload = {type, ...}
    browser.runtime.sendMessage(data.payload).catch(()=>{});
  });

  // Forward background -> MAIN
  browser.runtime.onMessage.addListener((msg) => {
    if (msg?.type === "RUN_QUERY") {
      window.postMessage({__bridge: BRIDGE, direction: "BG_TO_MAIN", payload: msg}, "*");
    }
  });
})();
