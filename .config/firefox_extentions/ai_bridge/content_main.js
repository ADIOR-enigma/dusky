// content_main.js - MAIN world - runs in page realm
(() => {
  const BRIDGE = "__LOCAL_AI_BRIDGE__";
  let currentRequestId = null;
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

  const isGenerating = () => {
    const stopBtn = document.querySelector('button[aria-label*="Stop"]') ||
                    document.querySelector('button[aria-label*="stop"]') ||
                    document.querySelector('button.stop-button') ||
                    document.querySelector('[data-is-streaming="true"]') ||
                    document.querySelector('.is-streaming');
    
    const loading = document.querySelector('mat-progress-spinner') ||
                    document.querySelector('mat-spinner') ||
                    document.querySelector('.loading-dots') ||
                    document.querySelector('.dot-flashing') ||
                    document.querySelector('[data-test-id="thinking-indicator"]');

    return !!(stopBtn || loading);
  };

  const hasUnclosedCodeBlock = (txt) => {
    if(!txt) return false;
    const ticks = (txt.match(/```/g) || []).length;
    return ticks % 2 !== 0;
  };

  const getSendButton = () => {
    if(isGenerating()) return null;

    const rich = document.querySelector('rich-textarea');
    if(rich && rich.shadowRoot){
      const sBtn = rich.shadowRoot.querySelector('button.send-button') ||
                   rich.shadowRoot.querySelector('button[aria-label*="Send"]') ||
                   rich.shadowRoot.querySelector('button');
      if(sBtn && !sBtn.disabled) return sBtn;
    }

    // Direct Gemini & Universal Send Button Selectors
    const geminiBtn = document.querySelector('button.send-button') ||
                      document.querySelector('button[aria-label*="Send prompt"]') ||
                      document.querySelector('button[aria-label*="Send message"]') ||
                      document.querySelector('button.send-button-container') ||
                      document.querySelector('.send-button-container button') ||
                      document.querySelector('rich-textarea ~ * button:last-of-type') ||
                      document.querySelector('button[mat-icon-button][aria-label*="Send"]');
    if(geminiBtn && !geminiBtn.disabled) return geminiBtn;

    const candidates = Array.from(document.querySelectorAll('button'));
    for(const btn of candidates){
      const label = (btn.getAttribute('aria-label') || '').toLowerCase();
      const text = (btn.textContent || '').toLowerCase();
      if(label.includes('stop') || text.includes('stop')) continue;
      if((label.includes('send') || label.includes('submit') || btn.classList.contains('send-button')) && !btn.disabled){
        return btn;
      }
    }

    return document.querySelector('button[aria-label*="Send prompt"]') ||
           document.querySelector('button[aria-label*="Send message"]') ||
           document.querySelector('button[aria-label*="Send"]') ||
           document.querySelector('button.send-button') ||
           document.querySelector('rich-textarea ~ button');
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

    selectAndFocus(targetEl);
    if(rich && rich !== targetEl) selectAndFocus(rich);

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
    if(isGenerating()) return false;

    // PRIORITY: Ensure the editor element has DOM focus so that the
    // hardware wtype -k Return (sent by bridge.py 1.5s after query)
    // hits the correct element. Synthetic JS events are isTrusted:false
    // and Gemini ignores them, so the real submission happens via wtype.
    const editor = getEditor();
    if(editor){
      let el = editor;
      if(el.tagName === "RICH-TEXTAREA"){
        el = el.querySelector('div[contenteditable="true"], p, div.ql-editor') || el;
      }
      selectAndFocus(el);
    }

    // Best-effort synthetic submission for non-Gemini sites
    // (On Gemini, these all fail silently because isTrusted === false)

    // 1. Try Form RequestSubmit
    const form = document.querySelector('form') || document.querySelector('rich-textarea')?.closest('form');
    if(form){
      try{ form.requestSubmit(); }catch{ try{ form.submit(); }catch{} }
    }

    // 2. Poll for enabled Send button
    for(let i = 0; i < 15; i++){
      const btn = getSendButton();
      if(btn && !btn.disabled){
        try{ btn.focus(); }catch{}
        try{ HTMLElement.prototype.click.call(btn); }catch{ btn.click(); }

        btn.dispatchEvent(new PointerEvent("pointerdown", {bubbles:true, cancelable:true, composed:true, view:window}));
        btn.dispatchEvent(new MouseEvent("mousedown", {bubbles:true, cancelable:true, composed:true, view:window}));
        btn.dispatchEvent(new PointerEvent("pointerup", {bubbles:true, cancelable:true, composed:true, view:window}));
        btn.dispatchEvent(new MouseEvent("mouseup", {bubbles:true, cancelable:true, composed:true, view:window}));
        btn.dispatchEvent(new MouseEvent("click", {bubbles:true, cancelable:true, composed:true, view:window}));

        triggerAngularComponentSubmit();
        console.log("[LocalAI MAIN] Synthetic click attempted on send button");
        
        // Re-focus editor after clicking button so wtype Return hits the right element
        if(editor){
          let el = editor;
          if(el.tagName === "RICH-TEXTAREA"){
            el = el.querySelector('div[contenteditable="true"], p, div.ql-editor') || el;
          }
          selectAndFocus(el);
        }
        return true;
      }
      await new Promise(r => setTimeout(r, 200));
    }

    // 3. Synthetic Enter Keypress on Editor (last resort)
    const ed = getEditor();
    if(ed){
      let pEl = ed.tagName === "RICH-TEXTAREA" ? (ed.querySelector('div[contenteditable="true"], p, div.ql-editor') || ed) : ed;
      selectAndFocus(pEl);
      pEl.dispatchEvent(new KeyboardEvent("keydown", {key:"Enter", code:"Enter", keyCode:13, which:13, bubbles:true, cancelable:true, composed:true}));
      pEl.dispatchEvent(new KeyboardEvent("keypress", {key:"Enter", code:"Enter", keyCode:13, which:13, bubbles:true, cancelable:true, composed:true}));
      pEl.dispatchEvent(new KeyboardEvent("keyup", {key:"Enter", code:"Enter", keyCode:13, which:13, bubbles:true, cancelable:true, composed:true}));
      triggerAngularComponentSubmit();
    }

    return true;
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

  function startObserver(){
    stopObserver();
    const containers = getModelResponseContainers();
    baselineCount = containers.length;
    targetElement = null;
    lastObservedText = "";
    lastFullText = "";
    hasStartedStreaming = false;

    const target = document.body;
    observer = new MutationObserver(() => {
      const currentContainers = getModelResponseContainers();

      if(!targetElement){
        if(currentContainers.length > baselineCount){
          const newContainer = currentContainers[baselineCount];
          targetElement = newContainer.querySelector('message-content') || newContainer;
          lastObservedText = "";
        } else {
          return;
        }
      }

      const full = (targetElement.innerText || targetElement.textContent || "").trim();
      if(!full || full === lastObservedText) return;

      const chunk = full.slice(lastObservedText.length);
      lastObservedText = full;
      lastFullText = full;

      if(chunk){
        hasStartedStreaming = true;
        emit({type:"STREAM_CHUNK", text: chunk, full: full, requestId: currentRequestId});
      }

      clearTimeout(finalTimer);
      const checkCompletion = () => {
        if(!hasStartedStreaming || isGenerating() || hasUnclosedCodeBlock(lastFullText)){
          finalTimer = setTimeout(checkCompletion, 1500);
          return;
        }
        if(currentRequestId && lastFullText){
          emit({type:"FINAL", full: lastFullText, requestId: currentRequestId});
          currentRequestId = null;
          stopObserver();
        }
      };
      finalTimer = setTimeout(checkCompletion, 3000);
    });
    observer.observe(target, {childList:true, subtree:true, characterData:true, characterDataOldValue:true});
  }

  function stopObserver(){
    if(observer){ observer.disconnect(); observer=null; }
    clearTimeout(finalTimer);
  }

  // --- Query Handling ---
  const handleQuery = async (msg) => {
    if(!msg || msg.type !== "RUN_QUERY") return;

    currentRequestId = msg.requestId || crypto.randomUUID();
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
    }, 250);
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
