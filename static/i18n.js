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

  // Core-workbench messages live beside the UI runtime so every supported
  // language receives a complete fallback while older language files remain
  // backward compatible during the transition.
  const EXTRA_MESSAGES = {
    en: {
      nav: { cognition: '🧠 Second Brain', integrations: '🔌 Integrations & MCP' },
      common: { refresh: 'Refresh', create: 'Create' },
      chat: { context_indicator: 'Cognitive context is compiled per request.' },
      cognition: {
        title: 'Second Brain', subtitle: 'Projects, evidence, and nightly insights',
        new_project: 'New project', project_name: 'Project name', goal: 'Goal', goal_placeholder: 'What outcome matters?',
        active_projects: 'Active projects', name: 'Name', stage: 'Stage', tags: 'Tags', latest_insights: 'Latest insights',
        capture_title: 'Capture source', source_title: 'Source title', source_content: 'Paste a note, meeting excerpt, or document text', capture: 'Capture',
        recent_sources: 'Recent sources', type: 'Type', status: 'Status', captured: 'Captured', untitled: 'Untitled source', empty_sources: 'No sources yet', source_required: 'Source content is required', run_insight: 'Run insight',
        empty_projects: 'No projects yet', project_labels: 'Project labels', save_labels: 'Save labels',
        delete_project: 'Delete project', delete_project_confirm: 'Delete this project? Its project-specific insights and memberships will be removed. Sources will be kept.', delete_project_done: 'Project deleted', delete_project_failed: 'Unable to delete project',
        empty_insights: 'Insights will appear after project evidence is processed.', load_failed: 'Unable to load Second Brain data',
        cards: { sources: 'Sources', processing: 'Processing', projects: 'Projects', claims: 'Claims', jobs: 'Nightly jobs' }
      },
      integrations: {
        title: 'Integrations', subtitle: 'Trusted plugins, background processing, and MCP access', plugins: 'Plugins',
        name: 'Name', kind: 'Kind', capabilities: 'Capabilities', trust: 'Trust', builtin: 'Built-in', external: 'External',
        empty_plugins: 'No trusted plugins found', jobs: 'Processing jobs', type: 'Type', status: 'Status', created: 'Created',
        retry: 'Retry', empty_jobs: 'No processing jobs', mcp: 'MCP server', entrypoint: 'Entrypoint', tools: 'Tools', scopes: 'Scopes', client_config: 'Client configuration', copy_config: 'Copy config', copied_config: 'MCP configuration copied', semantic: 'Semantic retrieval', mode: 'Mode', model: 'Model', rebuild_semantic: 'Rebuild semantic index', semantic_rebuilt: 'Semantic index rebuilt', semantic_rebuild_failed: 'Unable to rebuild semantic index',
        load_failed: 'Unable to load integration data'
      }
    },
    zh: {
      nav: { cognition: '🧠 第二大脑', integrations: '🔌 集成与MCP' }, common: { refresh: '刷新', create: '创建' },
      chat: { context_indicator: '每次请求都会编译认知上下文。' },
      cognition: {
        title: '第二大脑', subtitle: '项目、证据与夜间洞察', new_project: '新建项目', project_name: '项目名称',
        goal: '目标', goal_placeholder: '什么结果最重要？', capture_title: '录入素材', source_title: '素材标题', source_content: '粘贴笔记、会议片段或文档正文', capture: '录入',
        recent_sources: '最近素材', type: '类型', status: '状态', captured: '录入时间', untitled: '未命名素材', empty_sources: '暂无素材', source_required: '请输入素材内容', run_insight: '生成洞察',
        active_projects: '活跃项目', name: '名称', stage: '阶段', tags: '标签',
        latest_insights: '最新洞察',
        empty_projects: '暂无项目', empty_insights: '项目证据处理后将产生洞察。', load_failed: '无法加载第二大脑数据',
        cards: { sources: '素材', processing: '处理中', projects: '项目', claims: '主张', jobs: '夜间任务' }
      },
      integrations: {
        title: '集成与 MCP', subtitle: '受信任插件、后台处理与 MCP 访问', plugins: '插件', name: '名称', kind: '类型', capabilities: '能力', trust: '信任级别', builtin: '内置', external: '外部',
        empty_plugins: '没有可用的受信任插件', jobs: '处理任务', type: '任务类型', status: '状态', created: '创建时间', retry: '重试', empty_jobs: '暂无处理任务',
        mcp: 'MCP 服务', entrypoint: '启动入口', tools: '工具', scopes: '权限', load_failed: '无法加载集成状态'
      }
    },
    ko: {
      nav: { cognition: '🧠 세컨드 브레인', integrations: '🔌 통합과MCP' }, common: { refresh: '새로 고침', create: '생성' },
      chat: { context_indicator: '요청마다 인지 컨텍스트가 구성됩니다.' }
    },
    ja: {
      nav: { cognition: '🧠 第二の脳', integrations: '🔌 連携とMCP' }, common: { refresh: '更新', create: '作成' },
      chat: { context_indicator: 'リクエストごとに認知コンテキストを構成します。' }
    }
  };

  Object.assign(EXTRA_MESSAGES.zh.integrations, {
    semantic: '\u8bed\u4e49\u68c0\u7d22', mode: '\u6a21\u5f0f', model: '\u6a21\u578b',
    client_config: '\u5ba2\u6237\u7aef\u914d\u7f6e', copy_config: '\u590d\u5236\u914d\u7f6e', copied_config: 'MCP \u914d\u7f6e\u5df2\u590d\u5236',
    rebuild_semantic: '\u91cd\u5efa\u8bed\u4e49\u7d22\u5f15', semantic_rebuilt: '\u8bed\u4e49\u7d22\u5f15\u5df2\u91cd\u5efa', semantic_rebuild_failed: '\u65e0\u6cd5\u91cd\u5efa\u8bed\u4e49\u7d22\u5f15'
  });
  Object.assign(EXTRA_MESSAGES.zh.cognition, {
    project_labels: '\u9879\u76ee\u6807\u7b7e', save_labels: '\u4fdd\u5b58\u6807\u7b7e',
    delete_project: '\u5220\u9664\u9879\u76ee',
    delete_project_confirm: '\u5220\u9664\u8be5\u9879\u76ee\uff1f\u5176\u9879\u76ee\u5173\u7cfb\u548c\u6d1e\u5bdf\u5c06\u88ab\u6e05\u7406\uff0c\u539f\u59cb\u7d20\u6750\u4f1a\u4fdd\u7559\u3002',
    delete_project_done: '\u9879\u76ee\u5df2\u5220\u9664', delete_project_failed: '\u65e0\u6cd5\u5220\u9664\u9879\u76ee'
  });
  Object.assign(EXTRA_MESSAGES.en.cognition, {
    tab_overview: 'Statistics & insights', tab_projects: 'Project management', tab_sources: 'Source management',
    insight_hint: 'Open an insight to inspect it or correct it through conversation.',
    project_management: 'Project management', project_auto_hint: 'New sources are automatically matched to existing projects.', add_project: '+ Add project',
    source_management: 'Source management', source_auto_hint: 'Recordings, documents and conversations appear here automatically.', add_source: '+ Add source',
    description: 'Background', description_placeholder: 'Key context, boundaries and current state', tags_placeholder: 'Comma-separated tags',
    project: 'Project', confidence: 'Confidence', evidence: 'Evidence', created: 'Created', global_scope: 'Global', untitled_insight: 'Untitled insight',
    evidence_sources: 'Evidence sources', evidence_loading: 'Loading evidence sources...', evidence_empty: 'No linked evidence sources', evidence_load_failed: 'Unable to load evidence source', source_count: 'sources', open_minutes: 'View minutes', open_source: 'View source', no_quote: 'No evidence excerpt', minutes_empty: 'No minutes available', minutes_detail: 'View meeting minutes',
    correction_label: 'Question or correction', correction_placeholder: 'Explain what is incomplete or incorrect, or ask a follow-up question', send_correction: 'Send correction', correction_required: 'Enter a question or correction',
    correction_context: 'Please review this insight using my following question or correction:', correction_saved: 'Correction saved', correction_failed: 'Unable to process this correction',
    project_name_required: 'Enter a project name', project_create_failed: 'Unable to create project', source_create_failed: 'Unable to add source', insight_queued: 'Insight job queued',
    cards: {...EXTRA_MESSAGES.en.cognition.cards, insights: 'Insights'}
  });
  Object.assign(EXTRA_MESSAGES.zh.cognition, {
    tab_overview: '统计和洞察', tab_projects: '项目管理', tab_sources: '素材管理',
    insight_hint: '点击洞察可查看详情，并通过对话提问或修正。',
    project_management: '项目管理', project_auto_hint: '新素材会自动匹配到已有项目，也可以手动调整。', add_project: '+ 添加项目',
    source_management: '素材管理', source_auto_hint: '录音、文档和对话会自动汇集到这里。', add_source: '+ 添加素材',
    description: '项目背景', description_placeholder: '关键背景、边界与当前状态', tags_placeholder: '多个标签用逗号分隔',
    project: '项目', confidence: '置信度', evidence: '证据', created: '生成时间', global_scope: '全局', untitled_insight: '未命名洞察',
    evidence_sources: '证据来源', evidence_loading: '正在加载证据来源...', evidence_empty: '暂无关联证据来源', evidence_load_failed: '无法加载证据来源', source_count: '份纪要/素材', open_minutes: '查看纪要', open_source: '查看素材', no_quote: '暂无证据片段', minutes_empty: '暂无纪要内容', minutes_detail: '查看会议纪要',
    correction_label: '提问或修正', correction_placeholder: '指出不完整或不准确之处，或者继续追问', send_correction: '发送修正', correction_required: '请输入问题或修正内容',
    correction_context: '请结合我下面的问题或修正，重新审视这条洞察：', correction_saved: '修正已记录', correction_failed: '无法处理本次修正',
    project_name_required: '请输入项目名称', project_create_failed: '无法创建项目', source_create_failed: '无法添加素材', insight_queued: '洞察任务已加入队列',
    cards: {...EXTRA_MESSAGES.zh.cognition.cards, insights: '洞察'}
  });
  EXTRA_MESSAGES.ko.cognition = {
    ...EXTRA_MESSAGES.en.cognition,
    title: '세컨드 브레인', subtitle: '프로젝트, 근거 및 야간 인사이트',
    tab_overview: '통계 및 인사이트', tab_projects: '프로젝트 관리', tab_sources: '자료 관리',
    latest_insights: '최신 인사이트', insight_hint: '인사이트를 열어 확인하고 대화로 수정할 수 있습니다.',
    project_management: '프로젝트 관리', project_auto_hint: '새 자료는 기존 프로젝트에 자동으로 분류됩니다.', add_project: '+ 프로젝트 추가',
    source_management: '자료 관리', source_auto_hint: '녹음, 문서 및 대화가 자동으로 여기에 표시됩니다.', add_source: '+ 자료 추가',
    evidence_sources: '근거 출처', evidence_loading: '근거 출처를 불러오는 중...', evidence_empty: '연결된 근거가 없습니다.', evidence_load_failed: '근거 출처를 불러올 수 없습니다.', source_count: '개 출처', open_minutes: '회의록 보기', open_source: '자료 보기', no_quote: '근거 인용문 없음', minutes_empty: '회의록 내용 없음', minutes_detail: '회의록 보기',
    correction_label: '질문 또는 수정', correction_placeholder: '부정확하거나 불완전한 부분을 설명하거나 추가 질문을 입력하세요.', send_correction: '수정 보내기',
    cards: {...EXTRA_MESSAGES.en.cognition.cards, sources: '자료', processing: '처리 중', projects: '프로젝트', claims: '주장', insights: '인사이트', jobs: '야간 작업'}
  };
  EXTRA_MESSAGES.ja.cognition = {
    ...EXTRA_MESSAGES.en.cognition,
    title: '第二の脳', subtitle: 'プロジェクト、根拠、夜間インサイト',
    tab_overview: '統計とインサイト', tab_projects: 'プロジェクト管理', tab_sources: '素材管理',
    latest_insights: '最新インサイト', insight_hint: 'インサイトを開いて確認し、対話で修正できます。',
    project_management: 'プロジェクト管理', project_auto_hint: '新しい素材は既存プロジェクトへ自動分類されます。', add_project: '+ プロジェクト追加',
    source_management: '素材管理', source_auto_hint: '録音、文書、対話が自動的にここへ表示されます。', add_source: '+ 素材追加',
    evidence_sources: '根拠の出典', evidence_loading: '根拠の出典を読み込み中...', evidence_empty: '関連する根拠はありません。', evidence_load_failed: '根拠の出典を読み込めません。', source_count: '件の出典', open_minutes: '議事録を見る', open_source: '素材を見る', no_quote: '根拠の抜粋なし', minutes_empty: '議事録の内容がありません。', minutes_detail: '会議の議事録を見る',
    correction_label: '質問または修正', correction_placeholder: '不正確または不十分な点を説明するか、追加の質問を入力してください。', send_correction: '修正を送信',
    cards: {...EXTRA_MESSAGES.en.cognition.cards, sources: '素材', processing: '処理中', projects: 'プロジェクト', claims: '主張', insights: 'インサイト', jobs: '夜間ジョブ'}
  };

  function mergeMessages(...sources) {
    const target = {};
    for (const source of sources) {
      for (const [key, value] of Object.entries(source || {})) {
        target[key] = value && typeof value === 'object' && !Array.isArray(value)
          ? mergeMessages(target[key], value) : value;
      }
    }
    return target;
  }

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
      const resp = await fetch(`/static/i18n/${lang}.json?v=20260716-meeting-detail`, { signal: ctrl.signal });
      clearTimeout(timeout);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = mergeMessages(EXTRA_MESSAGES.en, await resp.json(), EXTRA_MESSAGES[lang]);
      loadedLangs[lang] = data;
      return data;
    } catch (e) {
      console.warn(`[i18n] Failed to load "${lang}.json":`, e.message);
      // Fallback: use built-in data-i18n content as-is
      if (lang !== 'en') {
        try {
          const ctrl2 = new AbortController();
          const t2 = setTimeout(() => ctrl2.abort(), 3000);
          const resp2 = await fetch(`/static/i18n/en.json?v=20260716-meeting-detail`, { signal: ctrl2.signal });
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
