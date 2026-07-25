(function () {
  const BRIDGE = "__LOCAL_AI_BRIDGE__";

  try {
    browser.runtime.sendMessage({type: "TAB_REGISTER", url: window.location.href});
  } catch(e){}

  // 1. Robust Script Injection into Page Realm (MAIN world)
  let injected = false;
  function injectMain() {
    if (injected) return;
    try {
      const target = document.head || document.documentElement || document.firstElementChild;
      if (!target) {
        setTimeout(injectMain, 10);
        return;
      }
      injected = true;
      const script = document.createElement("script");
      script.src = browser.runtime.getURL("content_main.js");
      script.onload = () => script.remove();
      target.appendChild(script);
    } catch(e) {}
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectMain, {once: true});
    injectMain();
  } else {
    injectMain();
  }

  // 2. Forward BG -> MAIN
  try {
    browser.runtime.onMessage.addListener((msg) => {
      if (msg?.type === "RUN_QUERY") {
        window.postMessage({__bridge: BRIDGE, direction: "BG_TO_MAIN", payload: msg}, "*");
      }
    });
  } catch(e) {}

  // 3. Forward MAIN -> BG
  window.addEventListener("message", (e) => {
    const data = e.data;
    if(!data || data.__bridge !== BRIDGE) return;
    if(data.direction !== "MAIN_TO_BG") return;
    try {
      browser.runtime.sendMessage(data.payload);
    } catch(err) {}
  });
})();
