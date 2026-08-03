/* ═══════════════════════════════════════════
   Dusky Sites Content Script v3.1
   ═══════════════════════════════════════════ */

'use strict';

// ─── State ───
let constructedSheet = null;
let styleEl = null;
let lastHash = null;
let observer = null;

const UNSAFE_CSS_VALUE = /url\s*\(|expression\s*\(|@import|-moz-binding/i;
const supportsConstructed = typeof CSSStyleSheet !== 'undefined' && 'adoptedStyleSheets' in Document.prototype;

// ─── Theme Application ───
function applyTheme(data, force = false) {
    if (!data?.colors || !Object.keys(data.colors).length) {
        removeTheme();
        return;
    }

    if (!force && data.timestamp === lastHash) return;
    lastHash = data.timestamp;

    let css = ':root {\n';
    for (const [k, v] of Object.entries(data.colors)) {
        if (/^--[\w-]+$/.test(k) && typeof v === 'string' && !/[;{}]/.test(v) && !UNSAFE_CSS_VALUE.test(v)) {
            css += `  ${k}: ${v} !important;\n`;
        }
    }
    css += '}\n';
    if (data.websiteCss) css += data.websiteCss;

    if (supportsConstructed) {
        try {
            if (!constructedSheet) {
                constructedSheet = new CSSStyleSheet();
            }
            constructedSheet.replaceSync(css);
            const docs = [document];
            for (const doc of docs) {
                if (doc.adoptedStyleSheets && !doc.adoptedStyleSheets.includes(constructedSheet)) {
                    doc.adoptedStyleSheets = [...doc.adoptedStyleSheets, constructedSheet];
                }
            }
            return;
        } catch {
            // Fallback to DOM style tag on any constructed stylesheet error
        }
    }

    if (!styleEl) {
        styleEl = document.createElement('style');
        styleEl.id = 'mf-theme';
    }
    styleEl.textContent = css;

    const apply = () => {
        if (!styleEl.parentNode) {
            if (document.head) document.head.appendChild(styleEl);
            else if (document.documentElement) document.documentElement.appendChild(styleEl);
        }
    };

    if (document.documentElement) apply();
    else requestAnimationFrame(apply);

    startObserver();
}

function removeTheme() {
    stopObserver();
    lastHash = null;

    if (constructedSheet && document.adoptedStyleSheets) {
        document.adoptedStyleSheets = document.adoptedStyleSheets.filter(s => s !== constructedSheet);
    }
    constructedSheet = null;

    const targetStyle = styleEl;
    styleEl = null;

    if (targetStyle) {
        targetStyle.remove();
    }
    const elements = document.querySelectorAll('#mf-theme');
    elements.forEach(el => el.remove());
}

// ─── Persistence Observer (Fallback only) ───
function startObserver() {
    if (observer || supportsConstructed) return;
    observer = new MutationObserver(() => {
        if (styleEl && !styleEl.parentNode) {
            const target = document.head || document.documentElement;
            if (target) target.appendChild(styleEl);
        }
    });
    const target = document.head || document.documentElement;
    if (target) observer.observe(target, { childList: true });
}

function stopObserver() {
    if (observer) {
        observer.disconnect();
        observer = null;
    }
}

// ─── Init ───
function initTheme(retries = 3) {
    browser.runtime.sendMessage({ type: 'GET_THEME_DATA' }).then(res => {
        if (res?.status?.manuallyStopped || !res?.data) {
            removeTheme();
        } else {
            applyTheme(res.data, true);
        }
    }).catch(() => {
        if (retries > 0) setTimeout(() => initTheme(retries - 1), 800);
    });
}
initTheme();

// ─── Message Listener ───
browser.runtime.onMessage.addListener((msg, sender) => {
    if (sender.id !== browser.runtime.id) return;
    if (msg.type === 'MATUGEN_UPDATE') {
        applyTheme(msg.data, msg.data?.force);
    } else if (msg.type === 'MATUGEN_ROLLBACK') {
        removeTheme();
    }
});