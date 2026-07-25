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

  // --- Selectors 2026 ---
  const getEditor = () => {
    return document.querySelector('#prompt-textarea') ||
           document.querySelector('div#prompt-textarea[contenteditable="true"]') ||
           document.querySelector('div[data-lexical-editor="true"][contenteditable="true"]') ||
           document.querySelector('div.ProseMirror[contenteditable="true"]') ||
           document.querySelector('div[contenteditable="true"][role="textbox"]') ||
           document.querySelector('div[contenteditable="true"]') ||
           document.querySelector('textarea');
  };

  const getSendButton = () => {
    return document.querySelector('button[data-testid="send-button"]') ||
           document.querySelector('button[data-testid="send_button"]') ||
           document.querySelector('button[aria-label*="Send"]') ||
           document.querySelector('form button[type="submit"]') ||
           document.querySelector('button:has([data-icon="paper-plane"])');
  };

  // --- Typing for Lexical / React ---
  function setInput(el, text){
    if(!el) return false;
    el.focus({preventScroll:true});

    // ContentEditable path - Lexical
    if(el.getAttribute && el.getAttribute("contenteditable") === "true"){
      // Select all
      const sel = window.getSelection();
      const range = document.createRange();
      try{
        range.selectNodeContents(el);
        sel.removeAllRanges();
        sel.addRange(range);
      }catch{}
      // execCommand is deprecated but still the ONLY reliable way for Lexical in Firefox 153
      let ok = false;
      try{ ok = document.execCommand("selectAll", false, null); }catch{}
      try{ ok = document.execCommand("insertText", false, text) || ok; }catch{}

      if(!ok){
        // Fallback: beforeinput + insert
        el.dispatchEvent(new InputEvent("beforeinput", {bubbles:true, inputType:"insertText", data:text}));
        // direct text insertion as last resort
        document.execCommand("insertText", false, text);
      }
      el.dispatchEvent(new InputEvent("input", {bubbles:true, inputType:"insertText", data:text}));
      // Trigger React onChange via InputEvent
      return true;
    } else {
      // textarea
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      if(setter){
        setter.call(el, text);
      } else {
        el.value = text;
      }
      el.dispatchEvent(new Event("input", {bubbles:true}));
      el.dispatchEvent(new Event("change", {bubbles:true}));
      return true;
    }
  }

  function clickSend(){
    const btn = getSendButton();
    if(btn){
      btn.click();
      return true;
    }
    const ed = getEditor();
    if(ed){
      ed.dispatchEvent(new KeyboardEvent("keydown", {key:"Enter", code:"Enter", keyCode:13, bubbles:true}));
      ed.dispatchEvent(new KeyboardEvent("keyup", {key:"Enter", code:"Enter", keyCode:13, bubbles:true}));
      return true;
    }
    return false;
  }

  // --- Fetch hook for streaming capture ---
  const origFetch = globalThis.fetch;
  const STREAM_URL_RE = /\/backend-api\/(?:f\/)?conversation|\/api\/organizations\/.*\/chat_conversations\/.*\/completion|\/backend-api\/conversation\/gen_title|v1\/chat\/completions|\/api\/chat|\/graphql/;

  globalThis.fetch = async function(...args){
    const url = (typeof args[0] === "string" ? args[0] : args[0]?.url || "").toString();
    const isStreamUrl = STREAM_URL_RE.test(url);
    let response;
    try{
      response = await origFetch.apply(this, args);
    }catch(e){ throw e; }

    if(!isStreamUrl || !response?.body || !currentRequestId){
      return response;
    }

    // Tee the stream - one for page, one for us
    try{
      const [branch1, branch2] = response.body.tee();
      // Parse branch2 in background without blocking
      (async () => {
        const reader = branch2.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        try{
          while(true){
            const {done, value} = await reader.read();
            if(done){
              emit({type:"FINAL", requestId: currentRequestId, full: lastFullText});
              currentRequestId = null;
              break;
            }
            buffer += decoder.decode(value, {stream:true});
            // Try to extract text from SSE lines
            const lines = buffer.split("\n");
            buffer = lines.pop() || "";
            for(const line of lines){
              if(!line.trim()) continue;
              let data = line;
              if(data.startsWith("data: ")) data = data.slice(6);
              if(data === "[DONE]") continue;
              try{
                const json = JSON.parse(data);
                // ChatGPT: json.v or json.p or choices[0].delta.content
                let txt = json?.v || json?.o || json?.p || json?.choices?.[0]?.delta?.content || json?.delta?.text || "";
                if(typeof txt === "string" && txt){
                  // For ChatGPT, v can be JSON stringified
                  if(txt.startsWith("{")){
                    try{ const inner = JSON.parse(txt); txt = inner?.v || inner?.choices?.[0]?.delta?.content || ""; }catch{}
                  }
                  if(txt) handleNewText(txt);
                }
              }catch{
                // plain text chunk
                if(data.length > 0 && data.length < 5000){
                  // Heuristic: if not JSON, might be raw text
                }
              }
            }
          }
        }catch(e){
          // ignore
        }
      })();

      // Return original stream to page
      return new Response(branch1, {status: response.status, statusText: response.statusText, headers: response.headers});
    }catch{
      return response;
    }
  };

  // Fallback MutationObserver for DOM streaming
  function handleNewText(newChunkOrFull){
    // If fetch hook gives us incremental chunks, emit directly
    // If we use DOM, we compute diff
  }

  let lastObservedText = "";
  function startObserver(){
    stopObserver();
    lastObservedText = "";
    const target = document.body;
    observer = new MutationObserver(() => {
      // Find assistant container - try multiple selectors
      let el = document.querySelector('div[data-message-author-role="assistant"]:last-child') ||
               document.querySelector('[data-message-author-role="assistant"]:last-of-type') ||
               document.querySelector('div[data-is-streaming="true"]') ||
               document.querySelector('article:last-child [data-message-author-role="assistant"]') ||
               document.querySelector('main [data-testid="conversation-turn"]:last-child');

      // Fallback: last assistant-looking block
      if(!el){
        const candidates = Array.from(document.querySelectorAll('div[data-message-author-role="assistant"], div[data-message-author-role="assistant"] *'));
        el = candidates[candidates.length-1];
      }
      if(!el) return;
      const full = (el.innerText || el.textContent || "").trim();
      if(!full || full === lastObservedText) return;

      const chunk = full.slice(lastObservedText.length);
      lastObservedText = full;
      lastFullText = full;

      if(chunk){
        emit({type:"STREAM_CHUNK", text: chunk, full: full, requestId: currentRequestId});
      }

      // Detect final: no changes for 1.5s
      clearTimeout(finalTimer);
      finalTimer = setTimeout(()=>{
        emit({type:"FINAL", full: full, requestId: currentRequestId});
      }, 1500);
    });
    observer.observe(target, {childList:true, subtree:true, characterData:true, characterDataOldValue:true});
  }

  function stopObserver(){
    if(observer){ observer.disconnect(); observer=null; }
    clearTimeout(finalTimer);
  }

  // --- Listen for queries from background ---
  globalThis.addEventListener("message", (e)=>{
    if(e.source !== globalThis) return;
    const data = e.data;
    if(!data || data.__bridge !== BRIDGE) return;
    if(data.direction !== "BG_TO_MAIN") return;
    const msg = data.payload;
    if(msg?.type !== "RUN_QUERY") return;

    currentRequestId = msg.requestId || crypto.randomUUID();
    lastFullText = "";
    lastObservedText = "";

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
      // In case fetch hook doesn't fire, observer will still capture
    }, 120);
  });

  // Auto-start observer once to be ready
  if(document.readyState === "complete" || document.readyState === "interactive"){
    // Don't start until first query
  } else {
    document.addEventListener("DOMContentLoaded", ()=>{}, {once:true});
  }

  console.log("[LocalAI MAIN] Injected - ready for queries - Firefox 153");
})();
