/**
 * Vibry AI Admin Panel — i18n Engine
 * Zero-dependency vanilla JS. Supports: zh, en, ko, ja
 *
 * Usage:
 *   Static HTML:  <span data-i18n="key.path">Fallback</span>
 *   Placeholders: <input data-i18n-placeholder="key.path" placeholder="Fallback">
 *   Titles:       <div data-i18n-title="key.path" title="Fallback">
 *   Dynamic JS:   I18N.t('key.path', arg0, arg1)
 */

const I18N = (() => {
  const STORAGE_KEY = 'vibry_lang';
  const SUPPORTED = ['zh', 'en', 'ko', 'ja'];

  let currentLang = 'en';
  let translations = {};
  let loadedLangs = {};
  let initPromise = null;
  let observer = null;

  /** Detect best language: URL param → localStorage → browser → default 'en' */
  function detectLang() {
    // 1. URL query param ?lang=xx
    const urlParams = new URLSearchParams(window.location.search);
    const urlLang = urlParams.get('lang');
    if (urlLang && SUPPORTED.includes(urlLang)) return urlLang;

    // 2. localStorage
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && SUPPORTED.includes(stored)) return stored;

    // 3. Browser preference
    const browserLang = (navigator.language || '').split('-')[0];
    if (SUPPORTED.includes(browserLang)) return browserLang;

    // 4. Default
    return 'en';
  }

  /** Load a translation JSON file with timeout */
  async function loadLang(lang) {
    if (loadedLangs[lang]) return loadedLangs[lang];
    try {
      const ctrl = new AbortController();
      const timeout = setTimeout(() => ctrl.abort(), 5000);
      const resp = await fetch(`/static/i18n/${lang}.json`, { signal: ctrl.signal });
      clearTimeout(timeout);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      loadedLangs[lang] = data;
      return data;
    } catch (e) {
      console.warn(`[i18n] Failed to load "${lang}.json":`, e.message);
      // Fallback: use built-in data-i18n content as-is
      if (lang !== 'en') {
        try {
          const ctrl2 = new AbortController();
          const t2 = setTimeout(() => ctrl2.abort(), 3000);
          const resp2 = await fetch(`/static/i18n/en.json`, { signal: ctrl2.signal });
          clearTimeout(t2);
          if (resp2.ok) { loadedLangs['en'] = await resp2.json(); return loadedLangs['en']; }
        } catch (_) {}
      }
      return {};
    }
  }

  /**
   * Get translation for a dot-notation key.
   * Supports placeholders: I18N.t('key', 'arg0', 'arg1') → replaces {0}, {1}, etc.
   */
  function t(key, ...args) {
    const keys = key.split('.');
    let val = translations;
    for (const k of keys) {
      if (val == null) break;
      val = val[k];
    }
    if (typeof val !== 'string') {
      // Try English fallback
      const enData = loadedLangs['en'];
      if (enData && enData !== translations) {
        let enVal = enData;
        for (const k of keys) {
          if (enVal == null) break;
          enVal = enVal[k];
        }
        if (typeof enVal === 'string') val = enVal;
      }
    }
    if (typeof val !== 'string') return key;

    // Replace placeholders {0}, {1}, ...
    return val.replace(/\{(\d+)\}/g, (match, idx) => {
      return args[parseInt(idx)] !== undefined ? args[parseInt(idx)] : match;
    });
  }

  /** Apply translations to all elements with data-i18n attributes */
  function applyToDOM(root) {
    root = root || document;

    // textContent
    root.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.getAttribute('data-i18n');
      el.textContent = t(key);
    });

    // placeholder
    root.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      const key = el.getAttribute('data-i18n-placeholder');
      el.setAttribute('placeholder', t(key));
    });

    // title
    root.querySelectorAll('[data-i18n-title]').forEach(el => {
      const key = el.getAttribute('data-i18n-title');
      el.setAttribute('title', t(key));
    });

    // value (for input[type=button], etc.)
    root.querySelectorAll('[data-i18n-value]').forEach(el => {
      const key = el.getAttribute('data-i18n-value');
      el.setAttribute('value', t(key));
    });
  }

  /** Switch language and re-apply all translations */
  async function setLanguage(lang) {
    if (!SUPPORTED.includes(lang)) return;
    const data = await loadLang(lang);
    translations = data;
    currentLang = lang;
    localStorage.setItem(STORAGE_KEY, lang);
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : lang === 'ko' ? 'ko-KR' : lang === 'ja' ? 'ja-JP' : 'en-US';

    // Update language switcher select
    const sel = document.getElementById('lang-switcher');
    if (sel) sel.value = lang;

    // Re-apply to entire document
    applyToDOM(document);

    // Emit event so page-specific refresh can happen
    window.dispatchEvent(new CustomEvent('i18n:langChanged', { detail: { lang } }));
  }

  /** Initialize: load detected language and set up MutationObserver */
  async function init() {
    const lang = detectLang();
    translations = await loadLang(lang);
    currentLang = lang;
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : lang === 'ko' ? 'ko-KR' : lang === 'ja' ? 'ja-JP' : 'en-US';

    // Apply on DOM ready
    if (document.readyState === 'loading') {
      await new Promise(resolve => document.addEventListener('DOMContentLoaded', resolve));
    }
    applyToDOM(document);

    // Sync language switcher
    const sel = document.getElementById('lang-switcher');
    if (sel) sel.value = lang;

    // Observe dynamic DOM changes (chat messages, etc.)
    observer = new MutationObserver(mutations => {
      for (const m of mutations) {
        for (const node of m.addedNodes) {
          if (node.nodeType === 1) {
            // Check if the added element or its children have i18n attributes
            if (node.hasAttribute && (node.hasAttribute('data-i18n') ||
                node.hasAttribute('data-i18n-placeholder') ||
                node.hasAttribute('data-i18n-title') ||
                node.querySelectorAll)) {
              applyToDOM(node);
            }
          }
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });

    // Mark ready
    document.documentElement.classList.add('i18n-ready');
    window.dispatchEvent(new CustomEvent('i18n:ready', { detail: { lang: currentLang } }));
  }

  // Auto-init
  initPromise = init();

  return {
    get currentLang() { return currentLang; },
    t,
    setLanguage,
    applyToDOM,
    init,
    get ready() { return initPromise; },
  };
})();
