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

function buildDynamicFallbackRules() {
    let dynamicRules = [];
    const colorTester = document.createElement('div');
    colorTester.style.display = 'none';
    (document.body || document.documentElement).appendChild(colorTester);

    function parseColor(val) {
        if (!val || val === 'transparent' || val === 'inherit' || val === 'initial') return null;
        colorTester.style.color = val;
        const comp = window.getComputedStyle(colorTester).color;
        const match = comp.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
        if (!match) return null;
        const r = parseInt(match[1]), g = parseInt(match[2]), b = parseInt(match[3]);
        return {
            r, g, b,
            lum: (0.299 * r + 0.587 * g + 0.114 * b) / 255
        };
    }

    try {
        for (const sheet of document.styleSheets) {
            try {
                if (!sheet.cssRules) continue;
                for (const rule of sheet.cssRules) {
                    if (rule.type !== CSSRule.STYLE_RULE || !rule.style) continue;
                    const st = rule.style;
                    let overrides = '';

                    const sel = rule.selectorText ? rule.selectorText.toLowerCase() : '';
                    if (sel.includes('input') || sel.includes('search') || sel.includes('glfyf')) {
                        // Skip input elements so search pills remain seamless
                        continue;
                    }

                    const bg = parseColor(st.backgroundColor);
                    if (bg && bg.lum > 0.45) {
                        overrides += '  background-color: var(--background, var(--surface, #181a1b)) !important;\n';
                    }

                    const fg = parseColor(st.color);
                    if (fg && fg.lum < 0.5) {
                        overrides += '  color: var(--on_background, var(--on_surface, #e0e0e0)) !important;\n';
                    }

                    if (overrides) {
                        dynamicRules.push(`${rule.selectorText} {\n${overrides}}`);
                    }
                }
            } catch (e) {
                // Ignore cross-origin CORS stylesheet errors
            }
        }
    } catch (e) {}

    colorTester.remove();
    return dynamicRules.join('\n');
}

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

    if (data.websiteCss) css += data.websiteCss + '\n';
    if (data.isUnthemedFallback) {
        const dynamicRules = buildDynamicFallbackRules();
        if (dynamicRules) css += dynamicRules + '\n';
    }

    if (supportsConstructed) {
        try {
            if (!constructedSheet) {
                constructedSheet = new CSSStyleSheet();
            }
            constructedSheet.replaceSync(css);
            if (document.adoptedStyleSheets && !document.adoptedStyleSheets.includes(constructedSheet)) {
                document.adoptedStyleSheets = [...document.adoptedStyleSheets, constructedSheet];
            }
        } catch {
            // Fallback to DOM style tag on constructed stylesheet error
        }
    }

    if (!styleEl) {
        styleEl = document.createElement('style');
        styleEl.id = 'mf-theme';
    }
    styleEl.textContent = css;

    const apply = () => {
        const target = document.head || document.documentElement;
        if (target && !styleEl.parentNode) {
            target.appendChild(styleEl);
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