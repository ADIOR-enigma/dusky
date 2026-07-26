(function () {
  const BRIDGE = "__LOCAL_AI_BRIDGE__";

  try {
    browser.runtime.sendMessage({type: "TAB_REGISTER", url: window.location.href}).catch(() => {});
  } catch(e){}

  // Forward BG -> MAIN
  try {
    browser.runtime.onMessage.addListener((msg) => {
      if (msg?.type === "RUN_QUERY") {
        window.postMessage(
          {__bridge: BRIDGE, direction: "BG_TO_MAIN", payload: msg},
          window.location.origin
        );
      }
    });
  } catch(e) {}

  // Forward MAIN -> BG
  window.addEventListener("message", (e) => {
    if (e.source !== window) return;
    const data = e.data;
    if (!data || data.__bridge !== BRIDGE) return;
    if (data.direction !== "MAIN_TO_BG") return;
    if (data.payload == null || typeof data.payload !== "object") return;
    if (typeof data.payload.type !== "string") return;
    try {
      browser.runtime.sendMessage(data.payload).catch(() => {});
    } catch (err) {}
  });
})();
