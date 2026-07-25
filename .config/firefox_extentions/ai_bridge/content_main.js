// content_main.js - MAIN world, document_start - runs in page realm, before React
(() => {
  if (globalThis.__LOCAL_AI_MAIN_INJECTED__) return;
  globalThis.__LOCAL_AI_MAIN_INJECTED__ = true;

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
    return document.querySelector('rich-textarea div[contenteditable="true"]') ||
           document.querySelector('div.ql-editor[contenteditable="true"]') ||
           document.querySelector('#prompt-textarea') ||
           document.querySelector('div#prompt-textarea[contenteditable="true"]') ||
           document.querySelector('div[data-lexical-editor="true"][contenteditable="true"]') ||
           document.querySelector('div.ProseMirror[contenteditable="true"]') ||
           document.querySelector('div[contenteditable="true"][role="textbox"]') ||
           document.querySelector('div[contenteditable="true"]') ||
           document.querySelector('textarea');
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

    const candidates = Array.from(document.querySelectorAll('button'));
    for(const btn of candidates){
      const label = (btn.getAttribute('aria-label') || '').toLowerCase();
      const text = (btn.textContent || '').toLowerCase();
      if(label.includes('stop') || text.includes('stop')) continue;
      if(label.includes('send') || label.includes('submit') || btn.classList.contains('send-button')){
        if(!btn.disabled) return btn;
      }
    }

    return document.querySelector('button[aria-label*="Send message"]') ||
           document.querySelector('button[aria-label*="Send"]') ||
           document.querySelector('button.send-button') ||
           document.querySelector('rich-textarea ~ button');
  };

  // --- Typing ---
  function setInput(el, text){
    if(!el) return false;
    el.focus({preventScroll:true});

    if(el.getAttribute && el.getAttribute("contenteditable") === "true"){
      const sel = window.getSelection();
      const range = document.createRange();
      try{
        range.selectNodeContents(el);
        sel.removeAllRanges();
        sel.addRange(range);
      }catch{}

      let ok = false;
      try{ ok = document.execCommand("selectAll", false, null); }catch{}
      try{ ok = document.execCommand("insertText", false, text) || ok; }catch{}

      if(!ok || !el.textContent.trim()){
        try{
          const htmlContent = text.split('\n').map(line => `<p>${line || '<br>'}</p>`).join('');
          el.innerHTML = htmlContent;
        } catch{
          el.textContent = text;
        }
        el.dispatchEvent(new InputEvent("beforeinput", {bubbles:true, inputType:"insertText", data:text}));
      }
      el.dispatchEvent(new InputEvent("input", {bubbles:true, inputType:"insertText", data:text}));
      el.dispatchEvent(new Event("change", {bubbles:true}));
      return true;
    } else {
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      if(setter) setter.call(el, text);
      else el.value = text;
      el.dispatchEvent(new Event("input", {bubbles:true}));
      el.dispatchEvent(new Event("change", {bubbles:true}));
      return true;
    }
  }

  function clickSend(){
    if(isGenerating()) return false;
    const btn = getSendButton();
    if(btn && !btn.disabled){
      btn.click();
      return true;
    }
    const ed = getEditor();
    if(ed){
      ed.dispatchEvent(new KeyboardEvent("keydown", {key:"Enter", code:"Enter", keyCode:13, which:13, bubbles:true}));
      ed.dispatchEvent(new KeyboardEvent("keyup", {key:"Enter", code:"Enter", keyCode:13, which:13, bubbles:true}));
      return true;
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

      // Index baselineCount is strictly the NEW response block for this query!
      if(!targetElement){
        if(currentContainers.length > baselineCount){
          const newContainer = currentContainers[baselineCount];
          targetElement = newContainer.querySelector('message-content') || newContainer;
          lastObservedText = "";
        } else {
          return; // Wait for the new model-response container to be added to DOM
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

      // Final check: MUST have started streaming, NOT currently generating, NO unclosed code blocks, and text stable for 3s
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

  // --- Listen for queries ---
  globalThis.addEventListener("message", async (e)=>{
    if(e.source !== globalThis) return;
    const data = e.data;
    if(!data || data.__bridge !== BRIDGE) return;
    if(data.direction !== "BG_TO_MAIN") return;
    const msg = data.payload;
    if(msg?.type !== "RUN_QUERY") return;

    currentRequestId = msg.requestId || crypto.randomUUID();
    lastFullText = "";
    lastObservedText = "";
    hasStartedStreaming = false;

    // Wait if Gemini is currently generating or thinking
    for(let i = 0; i < 40; i++){
      if(!isGenerating()) break;
      await new Promise(r => setTimeout(r, 500));
    }

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

    setTimeout(()=>{
      clickSend();
    }, 250);
  });

  console.log("[LocalAI MAIN] Injected - ready for queries - Firefox 153");
})();
