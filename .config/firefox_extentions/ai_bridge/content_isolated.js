(function () {
  const BRIDGE = "__LOCAL_AI_BRIDGE__";

  try {
    browser.runtime.sendMessage({type: "TAB_REGISTER", url: window.location.href});
  } catch(e){}

  // Forward BG -> MAIN
  try {
    browser.runtime.onMessage.addListener((msg) => {
      if (msg?.type === "RUN_QUERY") {
        window.postMessage({__bridge: BRIDGE, direction: "BG_TO_MAIN", payload: msg}, "*");
      }
    });
  } catch(e) {}

  // Forward MAIN -> BG
  window.addEventListener("message", (e) => {
    const data = e.data;
    if(!data || data.__bridge !== BRIDGE) return;
    if(data.direction !== "MAIN_TO_BG") return;
    try {
      browser.runtime.sendMessage(data.payload);
    } catch(err) {}
  });
})();
