// content_main.js - MAIN world - runs in page realm
(() => {
  const BRIDGE = "__LOCAL_AI_BRIDGE__";
  let currentRequestId = null;
  let hasSubmittedThisQuery = false;
  let lastFullText = "";
  let observer = null;
  let finalTimer = null;

  function emit(payload){
    globalThis.postMessage({__bridge: BRIDGE, direction: "MAIN_TO_BG", payload}, "*");
  }

  // --- Selectors ---
  const getEditor = () => {
    const rich = document.querySelector('rich-textarea');
    if(rich){
      if(rich.shadowRoot){
        const sEdit = rich.shadowRoot.querySelector('div[contenteditable="true"], [contenteditable="true"], div.ql-editor');
        if(sEdit) return sEdit;
      }
      const lEdit = rich.querySelector('div[contenteditable="true"], [contenteditable="true"], div.ql-editor');
      if(lEdit) return lEdit;
    }

    const candidates = [
      'rich-textarea div[contenteditable="true"]',
      'rich-textarea div.ql-editor',
      'rich-textarea [contenteditable="true"]',
      'div.ql-editor[contenteditable="true"]',
      '#prompt-textarea',
      'div#prompt-textarea[contenteditable="true"]',
      'div[data-lexical-editor="true"][contenteditable="true"]',
      'div.ProseMirror[contenteditable="true"]',
      'div[contenteditable="true"][role="textbox"]',
      'div[contenteditable="true"]',
      'textarea'
    ];
    for(const sel of candidates){
      const el = document.querySelector(sel);
      if(el && (el.isContentEditable || el.tagName === "TEXTAREA" || el.getAttribute("contenteditable") === "true")) return el;
    }

    const allEditable = Array.from(document.querySelectorAll('*')).filter(el => el.isContentEditable || el.getAttribute("contenteditable") === "true");
    if(allEditable.length > 0) return allEditable[0];

    return document.querySelector('rich-textarea');
  };

  const isVisible = (el) => {
    if(!el) return false;
    try{
      const style = window.getComputedStyle(el);
      if(style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
      if(el.getAttribute("aria-hidden") === "true" || el.hasAttribute("hidden")) return false;
      const rect = el.getBoundingClientRect();
      if(rect.width < 1 && rect.height < 1) return false;
      // offsetParent null can mean fixed/sticky; only treat as hidden when also no rect
      return true;
    }catch(e){
      return !!el;
    }
  };

  const getSendButtonRaw = () => {
    // 0. Direct query for ChatGPT Send button
    const chatgptBtn = document.querySelector('button[data-testid="send-button"]');
    if(chatgptBtn && isVisible(chatgptBtn)) return chatgptBtn;

    const rich = document.querySelector('rich-textarea');
    if(rich && rich.shadowRoot){
      const sBtn = rich.shadowRoot.querySelector('button[aria-label*="Send"], button.send-button, button');
      if(sBtn && isVisible(sBtn)) return sBtn;
    }

    // 1. Direct query for Gemini Send button
    const geminiBtn = document.querySelector('button.send-button') ||
                      document.querySelector('button[aria-label*="Send prompt"]') ||
                      document.querySelector('button[aria-label*="Send message"]') ||
                      document.querySelector('button[aria-label*="Send"]') ||
                      document.querySelector('button.send-button-container') ||
                      document.querySelector('.send-button-container button') ||
                      document.querySelector('rich-textarea ~ * button:last-of-type') ||
                      document.querySelector('button[mat-icon-button][aria-label*="Send"]');
    if(geminiBtn && isVisible(geminiBtn)) return geminiBtn;

    // 2. Query prompt bar container buttons (excluding mic, add, upload, stop)
    const promptParent = rich?.parentElement || document.querySelector('form') || document.querySelector('.input-area-container');
    if(promptParent){
      const pBtns = Array.from(promptParent.querySelectorAll('button'));
      for(const b of pBtns){
        if(!isVisible(b)) continue;
        const label = (b.getAttribute('aria-label') || b.textContent || '').toLowerCase();
        if(label.includes('mic') || label.includes('voice') || label.includes('upload') || label.includes('add') || label.includes('stop') || label.includes('cancel')) continue;
        return b;
      }
    }

    // 3. Fallback scan all buttons
    const candidates = Array.from(document.querySelectorAll('button'));
    for(const btn of candidates){
      if(!isVisible(btn)) continue;
      const label = (btn.getAttribute('aria-label') || '').toLowerCase();
      if(label.includes('send') || label.includes('submit') || btn.classList.contains('send-button') || btn.getAttribute('data-testid') === 'send-button'){
        return btn;
      }
    }
    return null;
  };

  const isGenerating = () => {
    const stopBtn =
      document.querySelector('button[data-testid="stop-button"]') ||
      document.querySelector('button[aria-label*="Stop generating"]') ||
      document.querySelector('button[aria-label*="Stop"]') ||
      document.querySelector('button.stop-button') ||
      document.querySelector('.send-button-container button[aria-label*="Stop"]') ||
      document.querySelector('rich-textarea ~ * button[aria-label*="Stop"]');
    if(stopBtn && isVisible(stopBtn)) return true;

    const streaming = document.querySelector('[data-is-streaming="true"], .is-streaming');
    if(streaming && isVisible(streaming)) return true;

    const sendBtn = getSendButtonRaw();
    if(sendBtn && (sendBtn.disabled || sendBtn.getAttribute("aria-disabled") === "true")){
      if(hasSubmittedThisQuery) return true;
    }
    return false;
  };

  const hasUnclosedCodeBlock = (txt) => {
    if(!txt) return false;
    const ticks = (txt.match(/```/g) || []).length;
    return ticks % 2 !== 0;
  };

  // --- Select & Focus Text Box Before Typing ---
  function selectAndFocus(el){
    if(!el) return;

    try{ el.dispatchEvent(new PointerEvent("pointerdown", {bubbles:true, cancelable:true, composed:true, view:window})); }catch(e){}
    try{ el.dispatchEvent(new MouseEvent("mousedown", {bubbles:true, cancelable:true, composed:true, view:window})); }catch(e){}
    try{ el.focus({preventScroll:true}); }catch(e){}
    try{ el.dispatchEvent(new PointerEvent("pointerup", {bubbles:true, cancelable:true, composed:true, view:window})); }catch(e){}
    try{ el.dispatchEvent(new MouseEvent("mouseup", {bubbles:true, cancelable:true, composed:true, view:window})); }catch(e){}
    try{ el.dispatchEvent(new MouseEvent("click", {bubbles:true, cancelable:true, composed:true, view:window})); }catch(e){}

    try{
      const sel = window.getSelection();
      if(sel){
        const range = document.createRange();
        range.selectNodeContents(el);
        range.collapse(false);
        sel.removeAllRanges();
        sel.addRange(range);
      }
    }catch(e){}
  }

  // --- Typing ---
  function setInput(targetEl, text){
    if(!targetEl) return false;

    const rich = document.querySelector('rich-textarea');
    if(rich && rich !== targetEl) selectAndFocus(rich);
    selectAndFocus(targetEl);

    let ok = false;
    try{ ok = document.execCommand("selectAll", false, null); }catch(e){}
    try{ ok = document.execCommand("insertText", false, text); }catch(e){}

    const targets = [targetEl];
    if(rich && !targets.includes(rich)) targets.push(rich);

    for(const el of targets){
      if(!ok || !el.textContent || !el.textContent.includes(text)){
        try{
          if(el.tagName === "P"){
            el.textContent = text;
          } else if(el.tagName === "RICH-TEXTAREA"){
            try{ if(el.value !== undefined) el.value = text; }catch(e){}
          } else {
            el.innerHTML = '<p>' + text + '</p>';
          }
        }catch(e){
          try{ el.textContent = text; }catch(err){}
        }
        try{ if(el.value !== undefined) el.value = text; }catch(e){}
      }

      try{ el.dispatchEvent(new InputEvent("beforeinput", {bubbles:true, cancelable:true, composed:true, inputType:"insertText", data:text})); }catch(e){}
      try{ el.dispatchEvent(new InputEvent("input", {bubbles:true, cancelable:true, composed:true, inputType:"insertText", data:text})); }catch(e){}
      try{ el.dispatchEvent(new Event("input", {bubbles:true, composed:true})); }catch(e){}
      try{ el.dispatchEvent(new Event("change", {bubbles:true, composed:true})); }catch(e){}
      try{ el.dispatchEvent(new KeyboardEvent("keydown", {key:"a", code:"KeyA", bubbles:true, cancelable:true, composed:true})); }catch(e){}
      try{ el.dispatchEvent(new KeyboardEvent("keyup", {key:"a", code:"KeyA", bubbles:true, cancelable:true, composed:true})); }catch(e){}
    }

    // Explicitly enable Send button if disabled by Angular template
    const btns = Array.from(document.querySelectorAll('button'));
    for(const btn of btns){
      const label = (btn.getAttribute('aria-label') || '').toLowerCase();
      if(label.includes('send') || btn.classList.contains('send-button')){
        btn.removeAttribute('disabled');
        btn.disabled = false;
        btn.removeAttribute('aria-disabled');
      }
    }

    return true;
  }

  function triggerAngularComponentSubmit(){
    const targets = [
      document.querySelector('rich-textarea'),
      document.querySelector('button.send-button'),
      document.querySelector('button[aria-label*="Send"]'),
      document.querySelector('chat-window'),
      document.querySelector('model-response'),
      document.body
    ];

    for(const el of targets){
      if(!el) continue;

      if(window.ng && window.ng.getComponent){
        try{
          const comp = window.ng.getComponent(el);
          if(comp){
            const sendFn = comp.sendMessage || comp.onSubmit || comp.send || comp.onSendClick || comp.submit;
            if(typeof sendFn === "function"){
              sendFn.call(comp);
              return true;
            }
          }
        }catch(e){}
      }

      try{
        for(const k in el){
          if(k.startsWith('__ng') || k.includes('Angular')){
            const ctx = el[k];
            if(ctx){
              const fn = ctx.sendMessage || ctx.onSubmit || ctx.send || ctx.onSendClick;
              if(typeof fn === "function"){
                fn.call(ctx);
                return true;
              }
            }
          }
        }
      }catch(e){}
    }
    return false;
  }

  async function clickSend(){
    if(hasSubmittedThisQuery || isGenerating()) return false;

    const editor = getEditor();
    if(editor){
      let el = editor;
      if(el.tagName === "RICH-TEXTAREA"){
        el = el.querySelector('div[contenteditable="true"], p, div.ql-editor') || el;
      }
      selectAndFocus(el);
    }

    // Poll for Send button directly across all AI sites (ChatGPT, Gemini, Claude)
    for(let i = 0; i < 30; i++){
      if(hasSubmittedThisQuery) return true;
      const btn = getSendButtonRaw();
      if(btn){
        btn.removeAttribute('disabled');
        btn.disabled = false;
        btn.removeAttribute('aria-disabled');
        hasSubmittedThisQuery = true;

        try{ btn.focus(); }catch(e){}
        try{ btn.click(); }catch(e){}

        const host = window.location.hostname;
        if(host.includes('gemini.google.com')){
          triggerAngularComponentSubmit();
        }

        console.log("[LocalAI MAIN] Single click executed on send button");
        return true;
      }
      await new Promise(r => setTimeout(r, 150));
    }

    return false;
  }

  function getModelResponseContainers(){
    const host = window.location.hostname;
    if(host.includes('gemini.google.com')){
      return Array.from(document.querySelectorAll('model-response'));
    } else if(host.includes('claude.ai')){
      return Array.from(document.querySelectorAll('.font-claude-message, [data-is-streaming="true"]'));
    } else {
      return Array.from(document.querySelectorAll('div[data-message-author-role="assistant"]'));
    }
  }

  let baselineCount = 0;
  let targetElement = null;
  let lastObservedText = "";
  let hasStartedStreaming = false;
  let lastChangeAt = 0;
  let pollTimer = null;

  function emitFinal(reason){
    if(!currentRequestId || !lastFullText) return;
    emit({type:"FINAL", full: lastFullText, requestId: currentRequestId, reason: reason || "complete"});
    currentRequestId = null;
    hasSubmittedThisQuery = false;
    stopObserver();
  }

  function scheduleCompletionCheck(){
    clearTimeout(finalTimer);
    const checkCompletion = () => {
      if(!currentRequestId) return;
      if(!hasStartedStreaming || !lastFullText){
        finalTimer = setTimeout(checkCompletion, 500);
        return;
      }

      const idleFor = Date.now() - lastChangeAt;
      const openCode = hasUnclosedCodeBlock(lastFullText);
      const stopBtn = document.querySelector('button[data-testid="stop-button"]') ||
                      document.querySelector('button[aria-label*="Stop generating"]') ||
                      document.querySelector('button[aria-label*="Stop"]') ||
                      document.querySelector('button.stop-button') ||
                      document.querySelector('.send-button-container button[aria-label*="Stop"]');
      const isStillGenerating = !!(stopBtn && isVisible(stopBtn));

      // Primary Completion: Text hasn't grown for 1.0s AND generation stop button disappeared AND code fences closed
      if(idleFor >= 1000 && !isStillGenerating && !openCode){
        emitFinal("idle");
        return;
      }

      // Fallback Completion: Text hasn't grown for 2.0s AND code fences closed
      if(idleFor >= 2000 && !openCode){
        emitFinal("silence-fallback");
        return;
      }

      // Absolute Safety Fallback: Text hasn't grown for 3.0s (unconditional completion)
      if(idleFor >= 3000){
        emitFinal("absolute-safety");
        return;
      }

      finalTimer = setTimeout(checkCompletion, 200);
    };
    finalTimer = setTimeout(checkCompletion, 500);
  }

  function readResponseText(el){
    if(!el) return "";
    // Prefer message-content body text; strip common chrome if present
    const body = el.querySelector?.('message-content') || el;
    return (body.innerText || body.textContent || "").trim();
  }

  function startObserver(){
    stopObserver();
    const containers = getModelResponseContainers();
    baselineCount = containers.length;
    targetElement = null;
    lastObservedText = "";
    lastFullText = "";
    hasStartedStreaming = false;
    lastChangeAt = Date.now();

    const target = document.body;
    const onDomTick = () => {
      if(!currentRequestId) return;
      const currentContainers = getModelResponseContainers();

      if(currentContainers.length > baselineCount){
        const latest = currentContainers[currentContainers.length - 1];
        const latestContent = latest.querySelector('message-content') || latest;
        if(targetElement !== latestContent){
          targetElement = latestContent;
          lastObservedText = "";
        }
      } else if(!targetElement || !document.contains(targetElement)){
        if(currentContainers.length > 0){
          const last = currentContainers[currentContainers.length - 1];
          targetElement = last.querySelector('message-content') || last;
          lastObservedText = readResponseText(targetElement);
        } else {
          return;
        }
      }

      const full = readResponseText(targetElement);
      if(!full) return;

      if(full === lastObservedText){
        // Still poll completion while idle
        return;
      }

      let chunk = "";
      if(full.startsWith(lastObservedText)){
        // Append-only growth (ideal streaming)
        chunk = full.slice(lastObservedText.length);
      } else if(lastObservedText && lastObservedText.startsWith(full)){
        // Transient shrink / rewrite mid-stream — wait for next tick
        lastObservedText = full;
        lastFullText = full;
        lastChangeAt = Date.now();
        scheduleCompletionCheck();
        return;
      } else {
        // Full re-render: compute longest common prefix and emit the new suffix only
        let i = 0;
        const max = Math.min(lastObservedText.length, full.length);
        while(i < max && lastObservedText.charCodeAt(i) === full.charCodeAt(i)) i++;
        chunk = full.slice(i);
        // If common prefix is tiny relative to previous text, treat as full replace
        if(i < Math.min(12, lastObservedText.length) && lastObservedText.length > 0){
          chunk = full;
        }
      }

      lastObservedText = full;
      lastFullText = full;
      lastChangeAt = Date.now();

      if(chunk){
        hasStartedStreaming = true;
        emit({type:"STREAM_CHUNK", text: chunk, full: full, requestId: currentRequestId});
      }

      scheduleCompletionCheck();
    };

    observer = new MutationObserver(onDomTick);
    observer.observe(target, {childList:true, subtree:true, characterData:true, characterDataOldValue:true});

    // Polling backup: Gemini sometimes mutates in ways that miss MutationObserver edges
    pollTimer = setInterval(onDomTick, 500);
    scheduleCompletionCheck();
  }

  function stopObserver(){
    if(observer){ observer.disconnect(); observer=null; }
    if(pollTimer){ clearInterval(pollTimer); pollTimer=null; }
    clearTimeout(finalTimer);
  }

  // --- Query Handling ---
  const handleQuery = async (msg) => {
    if(!msg || msg.type !== "RUN_QUERY") return;

    currentRequestId = msg.requestId || crypto.randomUUID();
    hasSubmittedThisQuery = false;
    lastFullText = "";
    lastObservedText = "";
    hasStartedStreaming = false;

    const query = msg.query || "";
    const editor = getEditor();
    if(!editor){
      emit({type:"ERROR", error:"Editor not found - is AI page loaded?", requestId: currentRequestId});
      return;
    }

    startObserver();

    const ok = setInput(editor, query);
    if(!ok){
      emit({type:"ERROR", error:"Failed to set input", requestId: currentRequestId});
      stopObserver();
      return;
    }

    setTimeout(async ()=>{
      await clickSend();
    }, 350);

    setTimeout(async ()=>{
      if(!hasSubmittedThisQuery && !hasStartedStreaming){
        console.log("[LocalAI MAIN] Retrying clickSend at 800ms...");
        await clickSend();
      }
    }, 800);

    // Focus keep-alive: re-focus editor every 200ms for 3s so that when
    // wtype sends hardware Return at ~T=1.9s, the editor has keyboard focus.
    // Without this, focus drifts to the response area after Prompt 1 completes.
    let focusKeepaliveCount = 0;
    const focusInterval = setInterval(() => {
      focusKeepaliveCount++;
      if(focusKeepaliveCount > 15 || !currentRequestId || hasStartedStreaming){
        clearInterval(focusInterval);
        return;
      }
      const ed = getEditor();
      if(ed){
        let el = ed;
        if(el.tagName === "RICH-TEXTAREA"){
          el = el.querySelector('div[contenteditable="true"], p, div.ql-editor') || el;
        }
        try{ el.focus({preventScroll:true}); }catch{}
      }
    }, 200);
  };

  window.__LocalAI_HandleQuery = handleQuery;

  globalThis.addEventListener("message", async (e)=>{
    const data = e.data;
    if(!data || data.__bridge !== BRIDGE) return;
    if(data.direction !== "BG_TO_MAIN") return;
    const msg = data.payload;
    if(msg?.type === "RUN_QUERY") handleQuery(msg);
  });

  console.log("[LocalAI MAIN] Injected - ready for queries - Firefox 153");
})();
