/* ═══════════════════════════════════════════
   Dusky Sites Background — Central State v3.1
   ═══════════════════════════════════════════ */

'use strict';

// ─── Constants ───
const NATIVE_NAME = 'dusky_sites';
const RECONNECT_BASE = 2000;
const RECONNECT_MAX = 300000;

// ─── Default Config ───
const BUILTIN_DEFAULT_CONFIG = {
    colorsPath: '~/.config/matugen/generated/dusky_sites.css',
    websitesDir: '~/.config/dusky_sites',
    ecoMode: true,
    browserThemeEnabled: true,
    webThemeEnabled: false,
    forceUnthemedWebsites: false,
    userChromeEnabled: true,
    userContentEnabled: true,
    fontSize: 13,
    paletteTemplate: {
        background: '--background',
        backgroundLight: '--surface',
        backgroundExtra: '--surface_container',
        accentPrimary: '--primary',
        accentSecondary: '--secondary',
        text: '--on_background',
        textFocus: '--on_surface',
    },
    browserTemplate: {
        frame: 'background',
        frame_inactive: 'background',
        tab_text: 'textFocus',
        tab_background_text: 'text',
        tab_selected: 'backgroundLight',
        tab_line: 'accentPrimary',
        tab_loading: 'accentPrimary',
        toolbar: 'backgroundLight',
        toolbar_text: 'textFocus',
        toolbar_field: 'backgroundExtra',
        toolbar_field_text: 'textFocus',
        toolbar_field_border: 'backgroundExtra',
        toolbar_field_focus: 'backgroundLight',
        toolbar_field_text_focus: 'textFocus',
        toolbar_field_border_focus: 'accentPrimary',
        toolbar_field_highlight: 'accentPrimary',
        toolbar_field_highlight_text: 'background',
        icons: 'text',
        icons_attention: 'accentPrimary',
        sidebar: 'backgroundLight',
        sidebar_text: 'textFocus',
        sidebar_border: 'backgroundExtra',
        sidebar_highlight: 'accentPrimary',
        sidebar_highlight_text: 'background',
        popup: 'backgroundLight',
        popup_text: 'textFocus',
        popup_border: 'backgroundExtra',
        popup_highlight: 'accentPrimary',
        popup_highlight_text: 'background',
        ntp_background: 'background',
        ntp_card_background: 'backgroundLight',
        ntp_text: 'text',
        bookmark_text: 'textFocus',
        toolbar_top_separator: 'backgroundExtra',
        toolbar_bottom_separator: 'backgroundExtra',
        button_background_hover: 'backgroundExtra',
        button_background_active: 'backgroundExtra',
    }
};

function mergeConfig(updates) {
    const base = (typeof USER_CONFIG !== 'undefined') 
        ? { ...BUILTIN_DEFAULT_CONFIG, ...USER_CONFIG } 
        : BUILTIN_DEFAULT_CONFIG;
    const m = { ...base, ...(updates || {}) };
    if (updates && updates.paletteTemplate) m.paletteTemplate = { ...base.paletteTemplate, ...updates.paletteTemplate };
    if (updates && updates.browserTemplate) m.browserTemplate = { ...base.browserTemplate, ...updates.browserTemplate };
    return m;
}

const DEFAULT_CONFIG = mergeConfig();

// ─── State ───
const state = {
    port: null,
    shouldConnect: true,
    isConnecting: false,
    reconnectTimer: null,
    reconnectDelay: RECONNECT_BASE,
    lastThemeData: null,
    isApplied: false,
    config: { ...DEFAULT_CONFIG },
    hasPromptedPaths: false,
    configWritePromise: Promise.resolve(),
};

const broadcastQueue = new Map();

// ─── Utilities ───
function notifyUI(msg) {
    browser.runtime.sendMessage(msg).catch(e => console.warn('Dusky Sites:', e));
}

function isInternalProtocol(url) {
    if (!url) return true;
    try {
        const u = new URL(url);
        return ['about:', 'chrome:', 'moz-extension:', 'view-source:', 'blob:', 'data:'].includes(u.protocol);
    } catch {
        return true;
    }
}

// ─── Native Host ───
function connectNative() {
    if (!state.shouldConnect || state.isConnecting || state.port) return;
    state.isConnecting = true;
    try {
        const port = browser.runtime.connectNative(NATIVE_NAME);
        state.port = port;

        port.onMessage.addListener(handleHostMessage);
        port.onDisconnect.addListener(handleHostDisconnect);

        safePostMessage({ type: 'SET_CONFIG', config: state.config });
        safePostMessage({ type: 'FETCH_NOW' });

        notifyUI({ type: 'HOST_STATUS', connected: true });
    } catch (err) {
        console.error('Dusky Sites: connectNative error:', err);
        scheduleReconnect();
    } finally {
        state.isConnecting = false;
    }
}

function safePostMessage(msg) {
    if (!state.port) return false;
    try {
        state.port.postMessage(msg);
        return true;
    } catch (e) {
        console.warn('Dusky Sites: postMessage failed:', e);
        state.port = null;
        scheduleReconnect();
        return false;
    }
}

function handleHostDisconnect(p) {
    const err = p.error?.message || 'unknown';
    console.error('Dusky Sites: host disconnected:', err);
    state.port = null;
    notifyUI({ type: 'HOST_STATUS', connected: false, error: err, manuallyStopped: !state.shouldConnect });
    if (state.shouldConnect) scheduleReconnect();
}

function scheduleReconnect() {
    if (state.reconnectTimer) clearTimeout(state.reconnectTimer);
    state.reconnectTimer = setTimeout(() => {
        state.reconnectTimer = null;
        connectNative();
    }, state.reconnectDelay);
    state.reconnectDelay = Math.min(state.reconnectDelay * 2, RECONNECT_MAX);
}

function disconnectNative() {
    state.shouldConnect = false;
    if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
    if (state.port) { try { state.port.disconnect(); } catch { } state.port = null; }
    broadcastRollback();
    resetBrowserTheme();
    state.isApplied = false;
}

// ─── Theme Resolution ───
function resolveThemeData() {
    if (!state.lastThemeData) return null;
    return {
        ...state.lastThemeData,
        colors: { ...state.lastThemeData.colors }
    };
}

// ─── Palette & Browser Theme ───
function buildPalette(colors) {
    const tmpl = state.config.paletteTemplate || DEFAULT_CONFIG.paletteTemplate;
    const palette = {};
    for (const [role, varName] of Object.entries(tmpl)) {
        palette[role] = colors[varName] || null;
    }
    return palette;
}

function buildBrowserThemeColors(colors) {
    const palette = buildPalette(colors);
    const tmpl = state.config.browserTemplate || DEFAULT_CONFIG.browserTemplate;
    const out = {};
    for (const [element, role] of Object.entries(tmpl)) {
        const c = palette[role];
        if (c) out[element] = c;
    }
    return out;
}

function isColorLight(hex) {
    if (!hex) return false;
    let c = hex.replace('#', '');
    if (c.length === 3) c = c.split('').map(x => x + x).join('');
    if (c.length !== 6 && c.length !== 8) return false;
    const r = parseInt(c.slice(0, 2), 16);
    const g = parseInt(c.slice(2, 4), 16);
    const b = parseInt(c.slice(4, 6), 16);
    return ((0.299 * r + 0.587 * g + 0.114 * b) / 255) > 0.5;
}

function applyBrowserTheme(colors) {
    if (!colors || !state.config.browserThemeEnabled) return;
    const themeColors = buildBrowserThemeColors(colors);
    if (!Object.keys(themeColors).length) return;
    const scheme = isColorLight(themeColors.frame) ? 'light' : 'dark';
    browser.theme.update({
        colors: themeColors,
        properties: { color_scheme: scheme, content_color_scheme: scheme },
    }).then(() => {
        browser.theme.getCurrent().then(cur => {
            safePostMessage({ type: 'LIVE_THEME_RESPONSE', theme: cur });
        }).catch(() => {});
    }).catch(e => console.warn('Dusky Sites:', e));
    state.isApplied = true;
}

function resetBrowserTheme() {
    browser.theme.reset().catch(e => console.warn('Dusky Sites:', e));
    state.isApplied = false;
}

// ─── Domain Matching Engine ───
function hostMatchesDomain(hostname, domain, allowSingleLabel = false) {
    const h = (hostname || '').toLowerCase();
    let d = (domain || '').toLowerCase();
    if (!h || !d) return false;
    if (d.startsWith('.')) d = d.slice(1);

    // Strict domain suffix match (e.g. example.com, www.example.com)
    if (h === d || h.endsWith('.' + d)) return true;

    // Single-label template fallback (e.g. template "youtube" matching youtube.com)
    if (allowSingleLabel && !d.includes('.')) {
        const parts = h.split('.').filter(Boolean);
        if (parts.length >= 2 && parts.slice(0, -1).includes(d)) return true;
    }
    return false;
}

function filterWebsiteCss(url, websites) {
    if (!url || !websites) return '';
    try {
        const hostname = new URL(url).hostname.toLowerCase();
        let bestKey = '';
        let bestCss = '';
        for (const [key, siteCss] of Object.entries(websites)) {
            const domain = String(key).toLowerCase();
            if (!hostMatchesDomain(hostname, domain, true)) continue;
            if (domain.length >= bestKey.length) {
                bestKey = domain;
                bestCss = siteCss;
            }
        }
        return bestCss ? `/* ${bestKey} */\n${bestCss}\n` : '';
    } catch {
        return '';
    }
}

function isSiteDisabled(url, disabledSites) {
    if (!url || !disabledSites?.length) return false;
    try {
        const hostname = new URL(url).hostname.toLowerCase();
        return disabledSites.some((d) => hostMatchesDomain(hostname, String(d), true));
    } catch {
        return false;
    }
}

function broadcastToTabs(force = false) {
    const data = resolveThemeData();
    if (!data?.colors || !Object.keys(data.colors).length) return;
    const isEco = state.config.ecoMode;

    browser.tabs.query({}).then(tabs => {
        if (isEco) {
            const activeByWindow = {};
            for (const t of tabs) {
                if (t.active && !t.discarded) activeByWindow[t.windowId] = t;
            }
            for (const t of Object.values(activeByWindow)) {
                sendToTab(t.id, data, t.url, force);
            }
        } else {
            const targets = tabs.filter(t => t.status === 'complete' && !t.discarded);
            targets.forEach(tab => sendToTab(tab.id, data, tab.url, force));
        }
    }).catch(e => console.warn('Dusky Sites:', e));
}

const DEFAULT_UNTHEMED_FALLBACK_CSS = `@media screen {
    /* Matugen Generic Structural Engine for Unthemed Websites */
    html, body,
    header, nav, main, footer, aside, section, article,
    form, table, thead, tbody, tfoot, tr, td, th, ul, ol, li, dl, dt, dd,
    details, summary, figure, fieldset, legend,
    [class*="card"], [class*="header"], [class*="footer"],
    [class*="sidebar"], [class*="panel"], [class*="box"] {
        background-color: var(--background, var(--surface, #181a1b)) !important;
        color: var(--on_background, var(--on_surface, #e0e0e0)) !important;
        border-color: var(--outline_variant, rgba(255, 255, 255, 0.08)) !important;
    }

    /* Keep generic div, span, and overlay containers transparent so layout wrappers never block page content */
    [class*="overlay"], [class*="backdrop"], [class*="off-canvas"], [class*="dialog-off-canvas"], [class*="canvas"], [class*="wrapper"], [id*="wrapper"], [class*="screenshot"] {
        background-color: transparent !important;
    }

    h1, h2, h3, h4, h5, h6, p, li, dt, dd, label, b, strong, i, em, small, mark, blockquote {
        color: var(--on_background, var(--on_surface, inherit)) !important;
    }

    a:link, a:link *, [role="link"], h3 a, h3 a * {
        color: #8ab4f8 !important;
    }

    a:visited, a:visited * {
        color: #c58af9 !important;
    }

    a:hover, a:hover * {
        color: #8ab4f8 !important;
        text-decoration: underline;
    }

    pre, code, kbd, samp {
        background-color: var(--surface_container_high, var(--surface, #2b2a33)) !important;
        color: var(--on_surface, inherit) !important;
        border-radius: 4px;
    }

    button, select, textarea, option, optgroup, [role="button"], [role="combobox"], [role="option"], [role="listbox"] {
        background-color: var(--surface_container, var(--surface, #2b2a33)) !important;
        color: var(--on_surface, #fbfbfe) !important;
        border-color: var(--outline, rgba(255, 255, 255, 0.12)) !important;
    }

    [class*="search"] input, form input, [role="combobox"] input, input[type="text"], input[type="search"], .gLFyf {
        background-color: transparent !important;
        color: #e0e0e0 !important;
        box-shadow: none !important;
    }

    input[type="checkbox"], input[type="radio"], input[type="range"], progress {
        accent-color: var(--primary_container, #8ab4f8) !important;
    }

    input::placeholder, textarea::placeholder {
        color: var(--on_surface_variant, rgba(255, 255, 255, 0.5)) !important;
    }

    table th {
        background-color: var(--surface_container_high, var(--surface, #2b2a33)) !important;
        color: var(--on_surface, #fbfbfe) !important;
    }

    tbody tr:nth-child(even) {
        background-color: var(--surface_container_low, var(--surface, #1e1d27)) !important;
    }

    img, video, canvas, iframe, embed, object, svg {
        background-color: transparent !important;
    }

    ::backdrop {
        background-color: rgba(0, 0, 0, 0.7) !important;
    }

    hr {
        border-color: var(--outline_variant, rgba(255, 255, 255, 0.12)) !important;
    }

    ::selection {
        background-color: var(--primary_container, var(--primary, #364765)) !important;
        color: var(--on_primary_container, var(--on_primary, #ffffff)) !important;
    }

    * {
        scrollbar-color: var(--outline, #42414d) var(--surface, #1c1b22);
    }
}
`;

const domainFixCache = new Map();

function getHostname(url) {
    if (!url) return '';
    try {
        return new URL(url).hostname.toLowerCase();
    } catch {
        return '';
    }
}

function resolveSiteCss(url, themeSource, allowUnthemed) {
    if (!url || !themeSource) return { siteCss: '', isUnthemedFallback: false };
    // Priority 1: Explicit custom template from ~/.config/dusky_sites/
    let siteCss = filterWebsiteCss(url, themeSource.websites || {});
    if (siteCss) return { siteCss, isUnthemedFallback: false };

    // If unthemed website forcing is OFF, return empty (unthemed)
    if (!allowUnthemed) return { siteCss: '', isUnthemedFallback: false };

    const hostname = getHostname(url);
    if (hostname) {
        let domainFix = domainFixCache.get(hostname);
        if (!domainFix) {
            safePostMessage({ type: 'GET_DOMAIN_FIX', domain: hostname });
        } else {
            if (domainFix.isDarkSite) {
                return { siteCss: '', isUnthemedFallback: false };
            }
            if (domainFix.css) {
                return {
                    siteCss: DEFAULT_UNTHEMED_FALLBACK_CSS + '\n\n' + domainFix.css,
                    isUnthemedFallback: true
                };
            }
        }
    }

    return { siteCss: DEFAULT_UNTHEMED_FALLBACK_CSS, isUnthemedFallback: true };
}

function sendToTab(tabId, data, url, force = false) {
    if (!url || isInternalProtocol(url)) return;
    if (!state.config.webThemeEnabled || isSiteDisabled(url, data?.disabledSites)) {
        browser.tabs.sendMessage(tabId, { type: 'MATUGEN_ROLLBACK' }).catch(() => {});
        return;
    }
    const allowUnthemed = !!state.config.forceUnthemedWebsites;
    const { siteCss, isUnthemedFallback } = resolveSiteCss(url, data, allowUnthemed);

    if (!siteCss) {
        browser.tabs.sendMessage(tabId, { type: 'MATUGEN_ROLLBACK' }).catch(() => {});
        return;
    }

    if (broadcastQueue.has(tabId)) clearTimeout(broadcastQueue.get(tabId));
    broadcastQueue.set(tabId, setTimeout(() => {
        broadcastQueue.delete(tabId);
        browser.tabs.sendMessage(tabId, {
            type: 'MATUGEN_UPDATE',
            data: {
                colors: data.colors,
                websiteCss: siteCss,
                isUnthemedFallback,
                timestamp: data.timestamp,
                force,
            },
        }).catch(e => console.warn('Dusky Sites:', e));
    }, 16));
}

function broadcastRollback() {
    browser.tabs.query({}).then(tabs => {
        for (const t of tabs) {
            if (!isInternalProtocol(t.url)) {
                browser.tabs.sendMessage(t.id, { type: 'MATUGEN_ROLLBACK' }).catch(e => console.warn('Dusky Sites:', e));
            }
        }
    }).catch(e => console.warn('Dusky Sites:', e));
}

// ─── Config Management ───
let configInitPromise = null;

function loadConfig() {
    configInitPromise = browser.storage.local.get(['config', 'themeData']).then(res => {
        if (res.config) state.config = mergeConfig(res.config);
        if (res.themeData) state.lastThemeData = res.themeData;
        connectNative();
    }).catch(err => console.error('Dusky Sites: loadConfig error:', err));
    return configInitPromise;
}

function saveConfig(partial = null) {
    if (partial) Object.assign(state.config, partial);
    state.configWritePromise = state.configWritePromise
        .then(() => browser.storage.local.set({ config: state.config }))
        .then(() => {
            safePostMessage({ type: 'SET_CONFIG', config: state.config });
            safePostMessage({ type: 'FETCH_NOW' });
        })
        .catch(err => console.error('Dusky Sites: saveConfig error:', err));
    return state.configWritePromise;
}

// ─── Host Message Handler ───
function handleHostMessage(msg) {
    state.reconnectDelay = RECONNECT_BASE;
    switch (msg.type) {
        case 'MATUGEN_UPDATE': {
            if (!msg.data?.colors) return;
            if (typeof msg.data.webThemeEnabled === 'boolean') {
                state.config.webThemeEnabled = msg.data.webThemeEnabled;
            }
            if (typeof msg.data.forceUnthemedWebsites === 'boolean') {
                state.config.forceUnthemedWebsites = msg.data.forceUnthemedWebsites;
            }
            state.lastThemeData = msg.data;
            browser.storage.local.set({ themeData: msg.data, config: state.config }).catch(e => console.warn('Dusky Sites: storage error:', e));

            if (state.config.webThemeEnabled) {
                broadcastToTabs(true);
            } else {
                broadcastRollback();
            }
            if (state.config.browserThemeEnabled) applyBrowserTheme(msg.data.colors);
            notifyUI({ type: 'THEME_APPLIED', colors: msg.data.colors });
            break;
        }
        case 'DOMAIN_FIX_RESPONSE': {
            if (msg.domain) {
                const dom = msg.domain.toLowerCase();
                domainFixCache.set(dom, {
                    css: msg.css || '',
                    isDarkSite: !!msg.isDarkSite
                });
                browser.tabs.query({}).then(tabs => {
                    for (const t of tabs) {
                        if (t.url && hostMatchesDomain(getHostname(t.url), dom)) {
                            const data = resolveThemeData();
                            if (data) sendToTab(t.id, data, t.url, true);
                        }
                    }
                }).catch(() => {});
            }
            break;
        }
        case 'STORED_CONFIG': {
            if (msg.config) {
                const prev = JSON.stringify(state.config);
                state.config = mergeConfig({ ...state.config, ...msg.config });
                if (prev !== JSON.stringify(state.config)) {
                    browser.storage.local.set({ config: state.config });
                    notifyUI({ type: 'CONFIG_RECOVERED', config: state.config });
                }
            }
            break;
        }
        case 'QUERY_LIVE_THEME': {
            browser.theme.getCurrent().then(cur => {
                safePostMessage({ type: 'LIVE_THEME_RESPONSE', theme: cur });
            }).catch(e => console.warn('Dusky Sites theme query error:', e));
            break;
        }
        case 'SAVE_CONFIG_SUCCESS':
            break;
        default:
            notifyUI({ type: 'HOST_RESPONSE', data: msg });
    }
}

// ─── Message Router ───
browser.runtime.onMessage.addListener((req, sender) => {
    switch (req.type) {
        case 'UPDATE_CONFIG': {
            const oldBrowser = state.config.browserThemeEnabled;
            const oldWeb = state.config.webThemeEnabled;
            const oldForceUnthemed = state.config.forceUnthemedWebsites;
            state.config = mergeConfig({ ...state.config, ...req.partialUpdate });
            return saveConfig().then(() => {
                const data = resolveThemeData();
                if ('browserThemeEnabled' in req.partialUpdate && oldBrowser !== state.config.browserThemeEnabled) {
                    state.config.browserThemeEnabled ? applyBrowserTheme(data?.colors) : resetBrowserTheme();
                }
                if (('webThemeEnabled' in req.partialUpdate && oldWeb !== state.config.webThemeEnabled) ||
                    ('forceUnthemedWebsites' in req.partialUpdate && oldForceUnthemed !== state.config.forceUnthemedWebsites)) {
                    if (state.config.webThemeEnabled) {
                        broadcastToTabs(true);
                    } else {
                        broadcastRollback();
                    }
                }
                if ('paletteTemplate' in req.partialUpdate || 'browserTemplate' in req.partialUpdate) {
                    if (state.config.browserThemeEnabled) applyBrowserTheme(data?.colors);
                }
                return { ok: true };
            });
        }
        case 'GET_THEME_DATA': {
            return (configInitPromise || Promise.resolve()).then(() => {
                const status = {
                    connected: !!state.port,
                    manuallyStopped: !state.shouldConnect,
                    lastSyncTime: state.lastThemeData?.timestamp || null,
                    isApplied: state.isApplied,
                };
                if (!state.config.webThemeEnabled) return { data: null, status };
                const url = sender.url || sender.tab?.url;
                if (isInternalProtocol(url)) return { data: null, status };

                const data = resolveThemeData();
                if (data && isSiteDisabled(url, data.disabledSites)) return { data: null, status };

                const resolveData = (themeSource) => {
                    if (!themeSource || !themeSource.colors) return { data: null, status };
                    if (isSiteDisabled(url, themeSource.disabledSites)) return { data: null, status };
                    const allowUnthemed = !!state.config.forceUnthemedWebsites;
                    const { siteCss, isUnthemedFallback } = resolveSiteCss(url, themeSource, allowUnthemed);
                    if (!siteCss) return { data: null, status };

                    return {
                        data: {
                            colors: themeSource.colors,
                            websiteCss: siteCss,
                            isUnthemedFallback,
                            timestamp: themeSource.timestamp,
                            status: themeSource.status,
                        },
                        status,
                    };
                };

                if (!data) {
                    return browser.storage.local.get('themeData').then(res => resolveData(res.themeData));
                }
                return resolveData(data);
            });
        }
        case 'GET_STATUS':
            return Promise.resolve({
                connected: !!state.port,
                manuallyStopped: !state.shouldConnect,
                lastSyncTime: state.lastThemeData?.timestamp || null,
                isApplied: state.isApplied,
            });
        case 'GET_PALETTE': {
            const colors = resolveThemeData()?.colors;
            return Promise.resolve({ palette: buildPalette(colors), colors });
        }

        case 'GET_PROFILE_PATHS':
        case 'WRITE_USER_CHROME':
        case 'WRITE_USER_CONTENT':
        case 'SET_FONT_SIZE': {
            if (!sender.url || !sender.url.includes(browser.runtime.id)) {
                console.warn('Dusky Sites: Rejected native host command from untrusted sender:', sender);
                return Promise.resolve({ ok: false, error: 'Unauthorized' });
            }
            safePostMessage(req);
            return Promise.resolve({ ok: !!state.port });
        }
        default:
            return false;
    }
});

// ─── Tab & Window Events ───
browser.tabs.onActivated.addListener((activeInfo) => {
    if (state.config.ecoMode && state.lastThemeData) {
        browser.tabs.get(activeInfo.tabId).then(tab => {
            sendToTab(tab.id, resolveThemeData(), tab.url);
        }).catch(e => console.warn('Dusky Sites:', e));
    }
});

browser.windows.onFocusChanged.addListener((windowId) => {
    if (windowId === browser.windows.WINDOW_ID_NONE) return;
    if (state.config.ecoMode && state.lastThemeData) {
        browser.tabs.query({ active: true, windowId }).then(tabs => {
            if (tabs[0] && tabs[0].url) {
                sendToTab(tabs[0].id, resolveThemeData(), tabs[0].url);
            }
        }).catch(e => console.warn('Dusky Sites:', e));
    }
});

browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if ((changeInfo.status === 'complete' || changeInfo.url) && tab.url && state.lastThemeData) {
        sendToTab(tabId, resolveThemeData(), tab.url, true);
    }
});

// ─── Tab Cleanup ───
browser.tabs.onRemoved.addListener(tabId => {
    if (broadcastQueue.has(tabId)) {
        clearTimeout(broadcastQueue.get(tabId));
        broadcastQueue.delete(tabId);
    }
});

// ─── Site Access ───
browser.action.onClicked.addListener(() => {
    connectNative();
    broadcastToTabs(true);
});

browser.permissions.onAdded.addListener(() => {
    broadcastToTabs(true);
});

// ─── Init ───
loadConfig();