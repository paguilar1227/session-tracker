'use strict';
/* Session Tracker UI — plain DOM, no build step. */
(function () {
  var STATES = ['not_started', 'in_progress', 'review', 'blocked', 'waiting', 'done'];
  var STATE_LABEL = { not_started: 'Not started', in_progress: 'In progress', review: 'Review', blocked: 'Blocked', waiting: 'Waiting', done: 'Done' };
  var PRIOS = ['P1', 'P2', 'P3'];
  var LEDGER_STATUS = { decision: ['proposed', 'decided', 'superseded'], mistake: ['recorded'], issue: ['open', 'resolved'] };
  var KIND_LABEL = { decision: 'Decisions', mistake: 'Mistakes', issue: 'Issues' };
  var KIND_ONE = { decision: 'decision', mistake: 'mistake', issue: 'issue' };
  var STATUS_LABEL = { running: 'Running', stalled: 'Stalled', waiting: 'Waiting on you', done: 'Done', failed: 'Failed', interrupted: 'Interrupted', archived: 'Archived', 'new': 'New' };
  var SOURCE = { extractor: ['pen-line', 'scribe'], scribe: ['pen-line', 'scribe'], agent: ['bot', 'agent'], user: ['user-round', 'you'], rule: ['cog', 'rule'] };
  var KIND_ICON = { html: 'file-code', markdown: 'file-text', image: 'file-image', pdf: 'book-open-text', canvas: 'workflow', excalidraw: 'pen-tool', other: 'file' };
  var SEARCH_ICON = { session: 'message-square-text', todo: 'square-check-big', decision: 'scale', mistake: 'triangle-alert', issue: 'bug', artifact: 'file' };
  var SORTS = [['activity', 'Last activity'], ['created', 'Created'], ['project', 'Project'], ['title', 'Title']];
  var ACTIVITY = [['any', 'Any time'], ['1', 'Today'], ['7', 'Last 7 days'], ['30', 'Last 30 days']];
  var ACTIVITY_CHIP = { '1': 'Active today', '7': 'Active in the last 7 days', '30': 'Active in the last 30 days' };
  var STATUS_ORDER = ['running', 'stalled', 'waiting', 'interrupted', 'failed', 'done', 'new', 'archived'];
  var HAS = {
    todos: ['Open todos', 'square-check-big', function (n) { return (n.counts || {}).todos_open > 0; }],
    issues: ['Open issues', 'bug', function (n) { return (n.counts || {}).issues_open > 0; }],
    mistakes: ['Mistakes', 'triangle-alert', function (n) { return (n.counts || {}).mistakes > 0; }],
    children: ['Has subagents', 'git-fork', function (n) { return n.children.length > 0; }]
  };
  var EMOJI_CATS = [['Smileys & Emotion', '😀'], ['People & Body', '👋'], ['Animals & Nature', '🐶'], ['Food & Drink', '🍕'], ['Travel & Places', '✈️'], ['Activities', '⚽'], ['Objects', '💡'], ['Symbols', '✅'], ['Flags', '🏁']];

  var state = {
    tree: [], treeKey: '', currentId: null, archived: false, filter: '', treeOpen: new Set(), expanded: new Set(),
    hideDone: false, ledgerTab: {}, openGroups: new Set(), health: null, version: -1, viewKey: {}, highlight: null, scrollTree: false,
    filterCollapsed: new Set(), view: null
  };
  var EMOJI = null, EMOJI_MAP = new Map();
  var PREFS_KEY = 'st-tree-view';
  (function loadPrefs() {
    var p = {};
    try { p = JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; } catch (e) { p = {}; }
    function list(x) { return Array.isArray(x) ? x.filter(function (v) { return typeof v === 'string'; }) : []; }
    state.view = {
      sort: SORTS.some(function (s) { return s[0] === p.sort; }) ? p.sort : 'activity',
      dir: p.dir === 'asc' ? 'asc' : 'desc',
      status: list(p.status), projects: list(p.projects), has: list(p.has).filter(function (k) { return HAS[k]; }),
      activity: ACTIVITY.some(function (a) { return a[0] === p.activity; }) ? p.activity : 'any'
    };
    state.archived = !!p.archived;
  })();
  function savePrefs() {
    var v = state.view;
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({ sort: v.sort, dir: v.dir, status: v.status, projects: v.projects, has: v.has, activity: v.activity, archived: state.archived }));
    } catch (e) { /* storage unavailable: settings last for this page only */ }
  }

  function $(sel, el) { return (el || document).querySelector(sel); }
  function h(tag, attrs) {
    var el = document.createElement(tag);
    var a = attrs || {};
    Object.keys(a).forEach(function (k) {
      var v = a[k];
      if (v == null || v === false) return;
      if (k === 'class') el.className = v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k.slice(0, 2) === 'on' && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === 'value') el.value = v;
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, v);
    });
    appendKids(el, Array.prototype.slice.call(arguments, 2));
    return el;
  }
  /* Like Element.append, but skips null/false and flattens arrays (native append would stringify them). */
  function appendKids(el, list) {
    list.forEach(function (kid) {
      if (kid == null || kid === false) return;
      if (Array.isArray(kid)) return appendKids(el, kid);
      el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    });
    return el;
  }
  var SVGNS = 'http://www.w3.org/2000/svg';
  /* Lucide icon (vendor/lucide-icons.js). Decorative: the control's text, title or aria-label names it. */
  function icon(name, cls) {
    var svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('class', 'i' + (cls ? ' ' + cls : ''));
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    ((window.LUCIDE || {})[name] || []).forEach(function (node) {
      var el = document.createElementNS(SVGNS, node[0]);
      Object.keys(node[1]).forEach(function (k) { el.setAttribute(k, node[1][k]); });
      svg.append(el);
    });
    return svg;
  }
  function iconBtn(name, label, cls) {
    return h('button', { type: 'button', class: 'btn small ghost icon-only' + (cls ? ' ' + cls : ''), title: label, 'aria-label': label }, icon(name));
  }
  function pinBtn(pinned, what, onToggle) {
    var label = (pinned ? 'Unpin ' : 'Pin ') + what;
    var b = h('button', { type: 'button', class: 'pin-btn' + (pinned ? ' on' : ''), title: label, 'aria-label': label, 'aria-pressed': pinned ? 'true' : 'false' }, icon('pin'));
    b.onclick = function (e) { e.preventDefault(); e.stopPropagation(); onToggle(!pinned); };
    return b;
  }
  function setPin(kind, id, on) {
    return act(function () { return api('POST', '/api/' + kind + '/' + encodeURIComponent(id) + '/pin', { pinned: on }); }, on ? 'Pinned' : 'Unpinned');
  }
  function sourceLabel(src) { var x = SOURCE[src]; return x ? [icon(x[0]), x[1]] : src; }
  function relShort(ms) {
    if (!ms) return '';
    var d = (Date.now() - ms) / 1000;
    if (d < 60) return 'now';
    if (d < 3600) return Math.floor(d / 60) + 'm';
    if (d < 86400) return Math.floor(d / 3600) + 'h';
    if (d < 86400 * 7) return Math.floor(d / 86400) + 'd';
    if (d < 86400 * 330) return new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    return new Date(ms).toLocaleDateString(undefined, { year: '2-digit', month: 'short' });
  }
  function trunc(s, n) { s = s || ''; return s.length > n ? s.slice(0, n - 1) + '…' : s; }
  function rel(ms) {
    if (!ms) return '—';
    var d = (Date.now() - ms) / 1000;
    if (d < 45) return 'just now';
    if (d < 3600) return Math.max(1, Math.round(d / 60)) + 'm ago';
    if (d < 86400) return Math.round(d / 3600) + 'h ago';
    if (d < 86400 * 30) return Math.round(d / 86400) + 'd ago';
    return new Date(ms).toLocaleDateString();
  }
  function abs(ms) { return ms ? new Date(ms).toLocaleString() : ''; }
  function when(label, ms) { return h('span', { title: abs(ms) }, label + ' ' + rel(ms)); }
  function bytes(n) {
    if (n == null) return '';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
  }
  function toast(msg, err) {
    var t = $('#toast');
    t.textContent = msg;
    t.className = 'toast' + (err ? ' err' : '');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(function () { t.className = 'toast hidden'; }, err ? 6000 : 2200);
  }

  async function api(method, path, body, headers) {
    var opts = { method: method, headers: Object.assign({ 'X-Actor': 'user' }, headers || {}) };
    if (body instanceof Blob) opts.body = body;
    else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers['Content-Type'] = 'application/json'; }
    var r = await fetch(path, opts);
    var data = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(data.error || (r.status + ' ' + r.statusText));
    return data;
  }
  var INTERACTIVE = '.editing, .popover, .emoji-pop';
  function openedSince(before) {
    return Array.prototype.some.call(document.querySelectorAll(INTERACTIVE), function (el) { return !before.has(el); });
  }
  async function act(fn, okMsg) {
    var before = new Set(document.querySelectorAll(INTERACTIVE));
    try {
      var out = await fn();
      if (okMsg) toast(okMsg);
      // Re-render to show the change, but never wipe a menu or editor the user opened after starting this action;
      // renderMain re-checks right before replacing the DOM, and the deferred refresh runs once it closes.
      await refresh(true, before);
      return out;
    } catch (e) { toast(e.message || String(e), true); }
  }

  /* ---------- emoji + markdown ---------- */
  async function loadEmoji() {
    try {
      EMOJI = await (await fetch('/vendor/emoji.json')).json();
      EMOJI.forEach(function (e) { e.a.forEach(function (a) { EMOJI_MAP.set(a, e.e); }); });
    } catch (e) { EMOJI = []; }
  }
  function emojify(text) {
    return (text || '').split(/(\x60\x60\x60[\s\S]*?\x60\x60\x60|\x60[^\x60\n]*\x60)/g).map(function (part, i) {
      return i % 2 ? part : part.replace(/:([a-z0-9_+\-]+):/g, function (m, n) { return EMOJI_MAP.get(n) || m; });
    }).join('');
  }
  if (window.marked) window.marked.setOptions({ gfm: true, breaks: true });
  function md(text, base) {
    var div = h('div', { class: 'md' });
    var html = window.marked ? window.marked.parse(emojify(text)) : emojify(text);
    var frag = window.DOMPurify.sanitize(html, { RETURN_DOM_FRAGMENT: true });
    frag.querySelectorAll('a[href]').forEach(function (a) { a.target = '_blank'; a.rel = 'noopener noreferrer'; });
    if (base) {
      frag.querySelectorAll('img[src],a[href]').forEach(function (el) {
        var attr = el.tagName === 'IMG' ? 'src' : 'href';
        var v = el.getAttribute(attr);
        if (v && !/^([a-z][a-z0-9+.\-]*:|\/|#)/i.test(v)) el.setAttribute(attr, base + v);
      });
    }
    div.append(frag);
    return div;
  }

  function mdInline(text) {
    var span = h('span', { class: 'md' });
    var html = window.marked ? window.marked.parseInline(emojify(text)) : emojify(text);
    var frag = window.DOMPurify.sanitize(html, { RETURN_DOM_FRAGMENT: true });
    frag.querySelectorAll('a[href]').forEach(function (a) { a.target = '_blank'; a.rel = 'noopener noreferrer'; });
    span.append(frag);
    return span;
  }
  /* One-line text box with an emoji button (shortcodes like :rocket: render too). */
  function withEmoji(input) {
    var wrap = h('span', { class: 'with-emoji' }, input);
    var b = h('button', { type: 'button', class: 'emoji-inline', title: 'Insert emoji', 'aria-label': 'Insert emoji' }, icon('smile-plus'));
    b.onclick = function (e) {
      e.stopPropagation();
      emojiPicker(wrap, function (em) { input.setRangeText(em, input.selectionStart || 0, input.selectionEnd || 0, 'end'); input.focus(); });
    };
    wrap.append(b);
    return wrap;
  }

  function closePopovers(except) {
    document.querySelectorAll('.emoji-pop, .popover').forEach(function (p) { if (p !== except) p.remove(); });
    var fb = $('#filter-btn');
    if (fb && !document.querySelector('.filter-pop')) fb.setAttribute('aria-expanded', 'false');
  }
  document.addEventListener('click', function () { closePopovers(); });

  function emojiPicker(anchor, onPick) {
    closePopovers();
    var pop = h('div', { class: 'emoji-pop' });
    var search = h('input', { type: 'search', placeholder: 'Search emoji — rocket, check, bug, fire…' });
    var grid = h('div', { class: 'emoji-grid' });
    var cat = EMOJI_CATS[0][0];
    var cats = h('div', { class: 'emoji-cats' });
    EMOJI_CATS.forEach(function (c) {
      var b = h('button', { type: 'button', title: c[0], class: c[0] === cat ? 'on' : '' }, c[1]);
      b.onclick = function () {
        cat = c[0]; search.value = '';
        cats.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); });
        fill();
      };
      cats.append(b);
    });
    function fill() {
      var q = search.value.trim().toLowerCase();
      var list = (EMOJI || []).filter(function (e) {
        if (!q) return e.c === cat;
        return e.a.some(function (a) { return a.indexOf(q) >= 0; }) || e.d.indexOf(q) >= 0 || e.t.some(function (t) { return t.indexOf(q) >= 0; });
      });
      grid.replaceChildren.apply(grid, list.map(function (e) {
        var b = h('button', { type: 'button', title: ':' + e.a[0] + ': ' + e.d }, e.e);
        b.onclick = function () { onPick(e.e); pop.remove(); };
        return b;
      }));
    }
    search.oninput = fill;
    pop.addEventListener('click', function (e) { e.stopPropagation(); });
    pop.append(search, cats, grid);
    anchor.append(pop);
    fill();
    search.focus();
  }

  function editor(initial, opts) {
    var ta = h('textarea', { placeholder: opts.placeholder || 'Markdown: **bold**, _italic_, - lists, - [ ] tasks, [links](https://…), :rocket: emoji' });
    ta.value = initial || '';
    var preview = h('div', { class: 'ed-preview hidden' });
    var wrap;
    function wrapSel(before, after, ph) {
      var s = ta.selectionStart, e = ta.selectionEnd;
      var sel = ta.value.slice(s, e) || ph || '';
      ta.setRangeText(before + sel + after, s, e, 'end');
      ta.focus();
    }
    function linePrefix(prefix) {
      var s = ta.selectionStart;
      var ls = ta.value.lastIndexOf('\n', s - 1) + 1;
      ta.setRangeText(prefix, ls, ls, 'end');
      ta.focus();
    }
    function insert(text) { ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, 'end'); ta.focus(); }
    var prevBtn = h('button', { type: 'button', title: 'Preview' }, icon('eye'), 'Preview');
    prevBtn.onclick = function () {
      var showing = preview.classList.contains('hidden');
      preview.classList.toggle('hidden', !showing);
      ta.classList.toggle('hidden', showing);
      prevBtn.classList.toggle('on', showing);
      if (showing) preview.replaceChildren(md(ta.value));
    };
    var emojiBtn = h('button', { type: 'button', title: 'Insert emoji', 'aria-label': 'Insert emoji', class: 'emoji-btn' }, icon('smile-plus'));
    emojiBtn.onclick = function (e) { e.stopPropagation(); emojiPicker(wrap, insert); };
    var bar = h('div', { class: 'ed-bar' },
      h('button', { type: 'button', title: 'Bold (⌘B)', onclick: function () { wrapSel('**', '**', 'bold'); } }, h('b', {}, 'B')),
      h('button', { type: 'button', title: 'Italic (⌘I)', onclick: function () { wrapSel('_', '_', 'italic'); } }, h('i', {}, 'I')),
      h('button', { type: 'button', title: 'Link', 'aria-label': 'Link', onclick: function () { wrapSel('[', '](https://)', 'text'); } }, icon('link')),
      h('button', { type: 'button', title: 'Bulleted list', 'aria-label': 'Bulleted list', onclick: function () { linePrefix('- '); } }, icon('list')),
      h('button', { type: 'button', title: 'Checklist item', 'aria-label': 'Checklist item', onclick: function () { linePrefix('- [ ] '); } }, icon('list-todo')),
      h('button', { type: 'button', title: 'Inline code', 'aria-label': 'Inline code', onclick: function () { wrapSel('\x60', '\x60', 'code'); } }, icon('code')),
      emojiBtn, h('span', { class: 'sep' }), prevBtn);
    var save = h('button', { type: 'button', class: 'btn primary small ed-save' }, opts.saveLabel || 'Save');
    var cancel = h('button', { type: 'button', class: 'btn small' }, 'Cancel');
    wrap = h('div', { class: 'editor editing' }, bar, ta, preview,
      h('div', { class: 'ed-foot' }, h('span', { class: 'ed-hint' }, '⌘↩ save · Esc cancel · :shortcode: becomes an emoji'), cancel, save));
    save.onclick = function () { opts.onSave(ta.value); };
    cancel.onclick = function () { wrap.remove(); if (opts.onCancel) opts.onCancel(); };
    ta.addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); save.click(); }
      else if (e.key === 'Escape') { e.preventDefault(); cancel.click(); }
      else if ((e.metaKey || e.ctrlKey) && e.key === 'b') { e.preventDefault(); wrapSel('**', '**', 'bold'); }
      else if ((e.metaKey || e.ctrlKey) && e.key === 'i') { e.preventDefault(); wrapSel('_', '_', 'italic'); }
    });
    wrap.addEventListener('click', function (e) { e.stopPropagation(); });
    setTimeout(function () { ta.focus(); }, 0);
    wrap.textarea = ta;
    return wrap;
  }

  /* ---------- navigation, tree, search, health ---------- */
  function idFromPath() { var m = location.pathname.match(/^\/s\/([\w\-]+)/); return m ? m[1] : null; }
  function navLink(id) { return function (e) { if (e) { e.preventDefault(); e.stopPropagation(); } navigate(id); }; }
  function navigate(id, push) {
    if (push !== false) history.pushState({}, '', id ? '/s/' + id : '/');
    state.currentId = id;
    state.scrollTree = true;
    if (id) openAncestors(id);
    renderTree();
    renderMain(true);
  }
  window.addEventListener('popstate', function () { navigate(idFromPath(), false); });
  function findPath(nodes, id, path) {
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.id === id) return path.concat([n]);
      var p = findPath(n.children, id, path.concat([n]));
      if (p) return p;
    }
    return null;
  }
  function openAncestors(id) {
    var p = findPath(state.tree, id, []);
    if (p) p.slice(0, -1).forEach(function (n) { state.treeOpen.add(n.id); });
  }
  function flatten(nodes, out) {
    out = out || [];
    nodes.forEach(function (n) { out.push(n); flatten(n.children, out); });
    return out;
  }
  /* ----- sidebar: sort + filter ----- */
  function filtering() {
    var v = state.view;
    return !!(state.filter.trim() || v.status.length || v.projects.length || v.has.length || v.activity !== 'any');
  }
  function activitySince(key) {
    if (key === '1') { var d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime(); }
    return Date.now() - Number(key) * 86400000;
  }
  function selfMatch(n) {
    var v = state.view, f = state.filter.trim().toLowerCase();
    if (f && (n.title || '').toLowerCase().indexOf(f) < 0 && n.id.indexOf(f) !== 0 && (n.project || '').toLowerCase().indexOf(f) < 0) return false;
    if (v.status.length && v.status.indexOf(n.status) < 0) return false;
    if (v.projects.length && v.projects.indexOf(n.project) < 0) return false;
    if (v.activity !== 'any' && (n.updated_at || 0) < activitySince(v.activity)) return false;
    return v.has.every(function (k) { return HAS[k][2](n); });
  }
  /* Sessions that match, plus the ancestors needed to reach them (drawn dimmed). Subagents keep their spawn order. */
  function visibleTree(nodes) {
    var out = [];
    nodes.forEach(function (n) {
      var kids = visibleTree(n.children);
      var self = selfMatch(n);
      if (self || kids.length) out.push({ n: n, self: self, kids: kids });
    });
    return out;
  }
  function sortTime(n) { return state.view.sort === 'created' ? n.created_at : (n.activity_at || n.updated_at); }
  function sortRoots(list) {
    var v = state.view, sign = v.dir === 'asc' ? 1 : -1;
    function text(a, b, key) { return (a.n[key] || '').localeCompare(b.n[key] || '', undefined, { sensitivity: 'base', numeric: true }); }
    function recent(a, b) { return (b.n.activity_at || 0) - (a.n.activity_at || 0); }
    return list.slice().sort(function (a, b) {
      if (v.sort === 'project' || v.sort === 'title') return text(a, b, v.sort) * sign || recent(a, b);
      return ((sortTime(a.n) || 0) - (sortTime(b.n) || 0)) * sign;
    });
  }
  function renderTree() {
    var on = filtering();
    var vis = sortRoots(visibleTree(state.tree));
    var matched = 0;
    function countMatches(x) { if (x.self) matched++; x.kids.forEach(countMatches); }
    function li(x) {
      var n = x.n, kids = x.kids.length > 0;
      if (x.self) matched++;
      var open = kids && (on ? !state.filterCollapsed.has(n.id) : state.treeOpen.has(n.id));
      var c = n.counts || {};
      var when = sortTime(n);
      var tip = n.title + '\n' + (STATUS_LABEL[n.status] || n.status) + ' · ' + (n.project || '') +
        '\nLast activity ' + abs(n.activity_at || n.updated_at) + '\nCreated ' + abs(n.created_at);
      var caret = h('span', { class: 'caret' }, kids ? icon(open ? 'chevron-down' : 'chevron-right') : null);
      var row = h('div', { class: 'tnode' + (n.id === state.currentId ? ' active' : '') + (x.self ? '' : ' ctx'), dataset: { id: n.id }, title: tip },
        caret, h('span', { class: 'dot ' + n.status }), h('span', { class: 'tt' }, n.title),
        pinBtn(n.pinned_at, 'session', function (on) { setPin('sessions', n.id, on); }),
        h('span', { class: 'badges' },
          c.todos_open ? h('span', { class: 'mini m-todo', title: c.todos_open + ' open todos' }, icon('square-check-big'), c.todos_open) : null,
          c.issues_open ? h('span', { class: 'mini m-issue', title: c.issues_open + ' open issues' }, icon('bug'), c.issues_open) : null,
          n.children.length ? h('span', { class: 'mini', title: n.children.length + ' subagents' }, icon('git-fork'), n.children.length) : null),
        h('span', { class: 'when' }, relShort(when)));
      caret.onclick = function (e) {
        e.stopPropagation();
        if (!kids) return;
        var set = on ? state.filterCollapsed : state.treeOpen;
        if (set.has(n.id)) set.delete(n.id); else set.add(n.id);
        renderTree();
      };
      row.onclick = function () { navigate(n.id); };
      var item = h('li', {}, row);
      if (open) item.append(h('ul', {}, x.kids.map(function (k) { return li(k); })));
      else x.kids.forEach(countMatches);
      return item;
    }
    var ul = h('ul');
    function header(ic, name, count) { return h('li', { class: 'group-h' }, icon(ic), h('h3', { class: 'gname' }, name), h('span', { class: 'count' }, count)); }
    var pinned = vis.filter(function (x) { return x.n.pinned_at; });
    var rest = vis.filter(function (x) { return !x.n.pinned_at; });
    if (pinned.length) {
      ul.append(header('pin', 'Pinned', pinned.length));
      pinned.forEach(function (x) { ul.append(li(x)); });
      if (rest.length && state.view.sort !== 'project') ul.append(header('list', 'Sessions', rest.length));
    }
    var groupSize = {};
    rest.forEach(function (x) { groupSize[x.n.project] = (groupSize[x.n.project] || 0) + 1; });
    var lastProject = null;
    rest.forEach(function (x) {
      if (state.view.sort === 'project' && x.n.project !== lastProject) {
        lastProject = x.n.project;
        ul.append(header('folder', lastProject, groupSize[lastProject]));
      }
      ul.append(li(x));
    });
    var nav = $('#tree');
    nav.replaceChildren(ul.childNodes.length ? ul : h('div', { class: 'empty' }, on || state.archived
      ? ['No sessions match these filters. ', h('button', { type: 'button', class: 'linkish', onclick: clearFilters }, 'Clear filters')]
      : 'No sessions yet.'));
    renderFilterBar(on, matched, flatten(state.tree).length);
    if (state.scrollTree) {
      state.scrollTree = false;
      var active = nav.querySelector('.tnode.active');
      if (active) active.scrollIntoView({ block: 'nearest' });
    }
  }
  function filtersChanged() {
    state.filterCollapsed = new Set();
    savePrefs();
    renderTree();
  }
  function setArchived(on) {
    state.archived = on;
    state.treeKey = '';
    savePrefs();
    loadTree().then(function () {
      var pop = document.querySelector('.filter-pop');
      if (pop && pop.refreshCounts) pop.refreshCounts();
    }, function () {});
  }
  function clearFilters() {
    var v = state.view;
    state.filter = '';
    $('#tree-filter').value = '';
    v.status = []; v.projects = []; v.has = []; v.activity = 'any';
    if (state.archived) setArchived(false);
    filtersChanged();
  }
  function renderFilterBar(on, matched, total) {
    var v = state.view;
    var n = v.status.length + v.projects.length + v.has.length + (v.activity !== 'any' ? 1 : 0) + (state.archived ? 1 : 0);
    var btn = $('#filter-btn');
    btn.querySelector('.n').textContent = n || '';
    btn.querySelector('.n').classList.toggle('hidden', !n);
    btn.classList.toggle('on', n > 0);
    function chip(label, remove) {
      return h('span', { class: 'achip' }, h('span', { class: 'lbl', title: label }, label),
        h('button', { type: 'button', title: 'Remove: ' + label, 'aria-label': 'Remove filter: ' + label, onclick: function () { remove(); filtersChanged(); } }, icon('x')));
    }
    var chips = [];
    if (v.status.length) chips.push(chip(v.status.map(function (s) { return STATUS_LABEL[s] || s; }).join(' or '), function () { v.status = []; }));
    v.projects.forEach(function (p) { chips.push(chip(p, function () { v.projects = v.projects.filter(function (x) { return x !== p; }); })); });
    if (v.activity !== 'any') chips.push(chip(ACTIVITY_CHIP[v.activity], function () { v.activity = 'any'; }));
    v.has.forEach(function (k) { chips.push(chip(HAS[k][0], function () { v.has = v.has.filter(function (x) { return x !== k; }); })); });
    if (state.archived) chips.push(chip('Including archived', function () { setArchived(false); }));
    var bar = $('#filter-bar');
    bar.replaceChildren();
    appendKids(bar, [
      on ? h('span', { class: 'fcount' }, matched + ' of ' + total + ' match') : null,
      chips,
      chips.length > 1 || (chips.length && state.filter.trim()) ? h('button', { type: 'button', class: 'linkish', onclick: clearFilters }, 'Clear all') : null]);
    bar.classList.toggle('hidden', !on && !chips.length);
  }
  function openFilterPanel(e) {
    e.stopPropagation();
    var btn = $('#filter-btn');
    if (document.querySelector('.filter-pop')) { closeFilterPanel(); return; }
    closePopovers();
    var v = state.view;
    function toggleIn(key, val, on) {
      v[key] = v[key].filter(function (x) { return x !== val; });
      if (on) v[key].push(val);
    }
    function toggleChip(lead, label, count, pressed, onToggle) {
      var b = h('button', { type: 'button', class: 'fchip', 'aria-pressed': String(pressed) }, lead, label, count != null ? h('span', { class: 'c' }, count) : null);
      b.onclick = function () {
        var on = b.getAttribute('aria-pressed') !== 'true';
        b.setAttribute('aria-pressed', String(on));
        onToggle(on);
        filtersChanged();
      };
      return b;
    }
    var statusGroup = h('div', { class: 'fchips' });
    var actGroup = h('div', { class: 'fchips', role: 'radiogroup', 'aria-label': 'Last active' });
    ACTIVITY.forEach(function (a) {
      var rb = h('input', { type: 'radio', name: 'st-activity', value: a[0], checked: v.activity === a[0] });
      rb.onchange = function () { if (rb.checked) { v.activity = a[0]; filtersChanged(); } };
      actGroup.append(h('label', { class: 'fchip radio' }, rb, a[1]));
    });
    var hasGroup = h('div', { class: 'fchips' });
    var perProject = {}, names = [];
    var pSearch = h('input', { type: 'search', placeholder: 'Find a project…', 'aria-label': 'Find a project', autocomplete: 'off' });
    var plist = h('div', { class: 'plist', role: 'group', 'aria-label': 'Projects' });
    function fillProjects() {
      var q = pSearch.value.trim().toLowerCase();
      var rows = names.filter(function (p) { return !q || p.toLowerCase().indexOf(q) >= 0; }).map(function (p) {
        var cb = h('input', { type: 'checkbox', checked: v.projects.indexOf(p) >= 0 });
        cb.onchange = function () { toggleIn('projects', p, cb.checked); filtersChanged(); };
        return h('label', { class: 'fopt' }, cb, h('span', { class: 'pn', title: p }, p), h('span', { class: 'c', title: 'top-level sessions' }, perProject[p]));
      });
      plist.replaceChildren.apply(plist, rows.length ? rows : [h('div', { class: 'empty' }, 'No projects match.')]);
    }
    /* Counts come from the loaded tree, so they are refreshed when "Include archived" reloads it. */
    function fillCounts() {
      var all = flatten(state.tree), statusCount = {};
      all.forEach(function (n) { statusCount[n.status] = (statusCount[n.status] || 0) + 1; });
      statusGroup.replaceChildren.apply(statusGroup, STATUS_ORDER.filter(function (s) { return statusCount[s] || v.status.indexOf(s) >= 0; }).map(function (s) {
        return toggleChip(h('span', { class: 'dot ' + s }), STATUS_LABEL[s] || s, statusCount[s] || 0, v.status.indexOf(s) >= 0, function (on) { toggleIn('status', s, on); });
      }));
      hasGroup.replaceChildren.apply(hasGroup, Object.keys(HAS).map(function (k) {
        return toggleChip(icon(HAS[k][1]), HAS[k][0], all.filter(HAS[k][2]).length, v.has.indexOf(k) >= 0, function (on) { toggleIn('has', k, on); });
      }));
      perProject = {};
      state.tree.forEach(function (r) { perProject[r.project] = (perProject[r.project] || 0) + 1; });
      v.projects.forEach(function (p) { if (!(p in perProject)) perProject[p] = 0; });
      names = Object.keys(perProject).sort(function (a, b) {
        return (v.projects.indexOf(b) >= 0) - (v.projects.indexOf(a) >= 0) || perProject[b] - perProject[a] || a.localeCompare(b);
      });
      fillProjects();
    }
    pSearch.oninput = fillProjects;
    var arch = h('input', { type: 'checkbox', checked: state.archived });
    arch.onchange = function () { setArchived(arch.checked); };
    var pop = h('div', { class: 'popover filter-pop', role: 'dialog', 'aria-label': 'Filter sessions' },
      h('div', { class: 'fgroup' }, h('div', { class: 'fgroup-h' }, 'Status'), statusGroup),
      h('div', { class: 'fgroup' }, h('div', { class: 'fgroup-h' }, 'Last active'), actGroup),
      h('div', { class: 'fgroup' }, h('div', { class: 'fgroup-h' }, 'Has'), hasGroup),
      h('div', { class: 'fgroup' }, h('div', { class: 'fgroup-h' }, 'Project'), pSearch, plist),
      h('label', { class: 'fopt arch' }, arch, 'Include archived sessions'),
      h('div', { class: 'fpop-foot' },
        h('button', { type: 'button', class: 'linkish', onclick: function () { clearFilters(); closeFilterPanel(); } }, 'Clear all'),
        h('button', { type: 'button', class: 'btn small primary', onclick: closeFilterPanel }, 'Done')));
    pop.addEventListener('click', function (ev) { ev.stopPropagation(); });
    pop.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') { ev.stopPropagation(); closeFilterPanel(); } });
    $('.side-tools').append(pop);
    pop.addEventListener('focusout', function (ev) {
      if (ev.relatedTarget && !pop.contains(ev.relatedTarget) && ev.relatedTarget !== btn) closeFilterPanel(false);
    });
    pop.refreshCounts = fillCounts;
    btn.setAttribute('aria-expanded', 'true');
    fillCounts();
    var first = pop.querySelector('button.fchip, input');
    if (first) first.focus();
  }
  function closeFilterPanel(refocus) {
    var pop = document.querySelector('.filter-pop');
    if (pop) pop.remove();
    var btn = $('#filter-btn');
    btn.setAttribute('aria-expanded', 'false');
    if (refocus !== false) btn.focus();
  }
  function renderSortDir() {
    var v = state.view, alpha = v.sort === 'project' || v.sort === 'title';
    var label = alpha ? (v.dir === 'asc' ? 'A–Z' : 'Z–A') : (v.dir === 'desc' ? 'Newest first' : 'Oldest first');
    var b = $('#sort-dir');
    b.replaceChildren(icon(v.dir === 'desc' ? 'arrow-down-wide-narrow' : 'arrow-up-narrow-wide'), label);
    b.title = 'Reverse the order (now ' + label + ')';
  }
  function initTreeControls() {
    var sel = $('#sort-key');
    sel.replaceChildren.apply(sel, SORTS.map(function (s) { return h('option', { value: s[0], selected: s[0] === state.view.sort }, s[1]); }));
    sel.onchange = function () {
      state.view.sort = sel.value;
      state.view.dir = sel.value === 'project' || sel.value === 'title' ? 'asc' : 'desc';
      savePrefs(); renderSortDir(); renderTree();
    };
    $('#sort-dir').onclick = function () {
      state.view.dir = state.view.dir === 'asc' ? 'desc' : 'asc';
      savePrefs(); renderSortDir(); renderTree();
    };
    renderSortDir();
    $('#filter-btn').prepend(icon('list-filter'));
    $('#filter-btn').onclick = openFilterPanel;
    $('.tree-search').prepend(icon('search', 'field-i'));
    $('#tree-filter').addEventListener('input', function (e) { state.filter = e.target.value; state.filterCollapsed = new Set(); renderTree(); });
  }
  async function loadTree() {
    var d = await api('GET', '/api/tree' + (state.archived ? '?archived=1' : ''));
    var key = JSON.stringify(d.roots);
    if (key === state.treeKey) return;
    state.treeKey = key;
    state.tree = d.roots;
    if (state.currentId) openAncestors(state.currentId);
    renderTree();
    if (!state.currentId) renderMain();
  }

  async function loadHealth() {
    var b = $('#health');
    try {
      var hl = await api('GET', '/api/health');
      state.health = hl;
      var ex = hl.extractor;
      var bad = !hl.codex_ok || !!hl.loop_error;
      var hk = hl.hooks || {};
      var untrusted = hk.installed && hk.trusted < hk.installed;
      var unconfirmed = hk.installed && !untrusted && hl.hooks_changed_at && (hl.last_hook_at || 0) < hl.hooks_changed_at;
      var label = (hl.codex_ok ? 'Codex linked' : 'Codex link broken') + ' · ' + hl.sessions + ' sessions · ledger ' +
        (!ex.enabled ? 'off' : (ex.pending ? ex.pending + ' queued' : 'idle')) + (ex.failed ? ' · ' + ex.failed + ' failed' : '') +
        (untrusted ? ' · hooks need trust (/hooks)' : unconfirmed ? ' · hooks not seen since change' : '');
      bad = bad || untrusted;
      b.replaceChildren(bad || unconfirmed ? icon('triangle-alert') : h('span', { class: 'dot done' }), label);
      b.className = 'health ' + (bad ? 'bad' : unconfirmed ? 'warn' : 'ok');
      b.title = (hl.codex_problems || []).concat(hl.loop_error ? [hl.loop_error] : [])
        .concat(untrusted ? ['Codex trusts ' + hk.trusted + ' of ' + hk.installed + ' session-tracker hooks. Open Codex, run /hooks and trust them so digests are injected after compaction and at subagent start.'] : [])
        .concat(unconfirmed ? ['The session-tracker hooks were installed or changed ' + rel(hl.hooks_changed_at) + ' and none has run since. Codex skips changed hooks until you approve them: open Codex, run /hooks (or Settings → Hooks) and approve the ones named "Session tracker: …". This clears once any session starts with them approved.'] : []).join('\n') ||
        ('Extraction: ' + ex.model + ' (effort ' + ex.effort + ') for turns finished after ' + abs(ex.auto_since) +
          (ex.last_failure ? '\nLast failure: ' + ex.last_failure.error : ''));
    } catch (e) {
      b.replaceChildren(icon('triangle-alert'), 'service unreachable');
      b.className = 'health bad';
    }
  }

  var searchTimer;
  async function doSearch() {
    var q = $('#search').value.trim();
    var box = $('#search-results');
    if (!q) { box.classList.add('hidden'); return; }
    var d;
    try { d = await api('GET', '/api/search?q=' + encodeURIComponent(q)); } catch (e) { return; }
    if (q !== $('#search').value.trim()) return;
    box.replaceChildren.apply(box, d.results.length ? d.results.map(function (r) {
      var b = h('button', { class: 'search-hit', type: 'button' },
        h('div', { class: 't' }, icon(SEARCH_ICON[r.kind] || 'search'), h('span', {}, trunc(r.title, 120))),
        h('div', { class: 's' }, r.kind + (r.session_title ? ' · ' + trunc(r.session_title, 80) : '') + (r.snippet ? ' · ' + trunc(r.snippet, 120) : '')));
      b.onclick = function () {
        box.classList.add('hidden');
        state.highlight = r.kind === 'session' ? null : r.kind + ':' + r.ref_id;
        navigate(r.session_id);
      };
      return b;
    }) : [h('div', { class: 'empty' }, 'No matches.')]);
    box.classList.remove('hidden');
  }


  /* ---------- session view ---------- */
  function statusPill(s) {
    var label = STATUS_LABEL[s.status] || s.status;
    var t = s.status === 'running' && s.last_turn_at ? ' · since ' + rel(s.last_turn_at) : '';
    return h('span', { class: 'pill ' + s.status, title: s.status === 'running' ? 'Last turn started ' + abs(s.last_turn_at) : s.status === 'stalled' ? 'Codex still marks the last turn in progress, but nothing has happened for longer than any pause seen in a turn that finished. It probably ended without Codex recording it.' : '' }, h('span', { class: 'dot ' + s.status }), label + t);
  }
  function section(title, count, actions, body, extraClass) {
    return h('section', { class: 'section ' + (extraClass || '') },
      h('div', { class: 'sec-head' }, h('h2', {}, title, count != null ? h('span', { class: 'count' }, count) : null), actions),
      body);
  }

  function sessionView(view, depth) {
    var s = view.session;
    var el = h('div', { class: 'session', dataset: { id: s.id } });
    if (!depth && view.breadcrumb.length) {
      el.append(h('div', { class: 'crumbs' }, view.breadcrumb.map(function (b) {
        return [h('a', { href: '/s/' + b.id, onclick: navLink(b.id) }, trunc(b.title, 70)), h('span', {}, '›')];
      }), h('span', {}, 'this session')));
    }
    var tv = view.turns || {};
    var extractBtn = h('button', { class: 'btn small', title: 'Queue finished turns that have not been through ledger extraction (Claude Sonnet 5.5)' }, icon('sparkles'), 'Extract ledger');
    extractBtn.onclick = function () {
      act(function () { return api('POST', '/api/sessions/' + s.id + '/extract', { only_missing: true }); }).then(function (r) {
        if (r) toast(r.queued ? r.queued + ' turn(s) queued for extraction' : 'Nothing new to extract');
      });
    };
    el.append(h('div', { class: 's-head' },
      h('div', { style: { minWidth: 0 } }, h(depth ? 'h3' : 'h1', { class: 's-title' }, s.display_title)),
      h('div', { class: 'actions' }, statusPill(s),
        h('button', { class: 'btn small' + (s.pinned_at ? ' pinned' : ''), type: 'button', 'aria-pressed': s.pinned_at ? 'true' : 'false',
          title: s.pinned_at ? 'Unpin this session' : 'Pin this session to the top of its list',
          onclick: function () { setPin('sessions', s.id, !s.pinned_at); } }, icon('pin'), s.pinned_at ? 'Pinned' : 'Pin'),
        h('button', { class: 'btn small', title: s.artifacts_dir || '', onclick: function () { act(function () { return api('POST', '/api/sessions/' + s.id + '/reveal-folder'); }, 'Opened the artifacts folder in Finder'); } }, icon('folder-open'), 'Folder'),
        extractBtn,
        depth ? h('button', { class: 'btn small', onclick: navLink(s.id) }, 'Open', icon('arrow-right')) : null)));
    var copyId = h('span', { class: 'mono', title: 'Copy session id', style: { cursor: 'pointer' } }, s.id);
    copyId.onclick = function () { navigator.clipboard.writeText(s.id).then(function () { toast('Session id copied'); }); };
    el.append(h('div', { class: 's-meta' },
      s.agent_path ? h('span', {}, h('b', {}, s.nickname || 'subagent'), ' ', h('span', { class: 'mono' }, s.agent_path)) : null,
      s.model ? h('span', {}, h('b', {}, s.model), s.effort ? ' · ' + s.effort : '') : null,
      when('started', s.created_at), when('updated', s.updated_at),
      h('span', {}, (s.turn_count || 0) + ' turns'),
      tv.pending_extraction ? h('span', { class: 'meta-i' }, icon('sparkles'), tv.pending_extraction + ' turn(s) being extracted') : null,
      (tv.failed_extraction || []).length ? h('span', { class: 'meta-i', style: { color: 'var(--bad)' }, title: tv.failed_extraction.map(function (t) { return t.extract_error; }).join('\n') }, icon('triangle-alert'), 'extraction failed for ' + tv.failed_extraction.length + ' turn(s)') : null,
      s.project ? h('span', { class: 'meta-i', title: 'Project' }, icon('folder'), h('b', {}, s.project)) : null,
      s.cwd ? h('span', { class: 'mono', title: 'Working directory' }, s.cwd) : null,
      copyId));
    el.append(h('div', { class: 'grid2' }, todoSection(view), ledgerSection(view)));
    el.append(artifactSection(view));
    el.append(childrenSection(view, depth));
    return el;
  }

  /* ----- todos ----- */
  function todoSection(view) {
    var sid = view.session.id;
    var title = h('input', { type: 'text', placeholder: 'Add a todo…', 'aria-label': 'New todo title' });
    var prio = h('select', { 'aria-label': 'Priority', class: 'prio p-P2' }, PRIOS.map(function (p) { return h('option', { value: p, selected: p === 'P2' }, p); }));
    prio.onchange = function () { prio.className = 'prio p-' + prio.value; };
    var st = h('select', { 'aria-label': 'State' }, STATES.map(function (x) { return h('option', { value: x }, STATE_LABEL[x]); }));
    function add() {
      if (!title.value.trim()) { title.focus(); return; }
      act(function () { return api('POST', '/api/sessions/' + sid + '/todos', { title: title.value, priority: prio.value, state: st.value }); }, 'Todo added');
    }
    title.addEventListener('keydown', function (e) { if (e.key === 'Enter') add(); });
    var addRow = h('div', { class: 'add-row' }, withEmoji(title), prio, st, h('button', { class: 'btn primary small', onclick: add }, 'Add'));
    var todos = view.todos.filter(function (t) { return !(state.hideDone && t.state === 'done'); });
    var done = view.todos.filter(function (t) { return t.state === 'done'; }).length;
    var toggle = h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: state.hideDone, onchange: function (e) { state.hideDone = e.target.checked; renderMain(true); } }), 'hide done');
    var open = view.todos.length - done;
    var openTodos = todos.filter(function (t) { return t.state !== 'done'; });
    var prios = new Set(openTodos.map(function (t) { return t.priority; }));
    var list = h('div', { class: 'todo-list' });
    var lastPrio = null;
    openTodos.forEach(function (t) {
      if (prios.size > 1 && t.priority !== lastPrio) { lastPrio = t.priority; list.append(h('div', { class: 'prio-div' }, t.priority)); }
      list.append(todoCard(t, view, openTodos));
    });
    todos.forEach(function (t) { if (t.state === 'done') list.append(todoCard(t, view)); });
    return section([icon('list-checks'), 'Todos'], open + ' open / ' + view.todos.length, toggle,
      h('div', { class: 'sec-body' }, addRow, todos.length ? list : h('div', { class: 'empty' }, view.todos.length ? 'All todos are done.' : 'No todos yet.')));
  }

  /* Open todos are ordered by priority, then by hand. Dropping a todo into another priority's group gives it that
     priority. The grip also works from the keyboard: arrow up/down moves one place. */
  var drag = null, moving = false;
  function clearDropMarks() {
    document.querySelectorAll('.todo.drop-before, .todo.drop-after').forEach(function (el) { el.classList.remove('drop-before', 'drop-after'); });
  }
  function moveTodo(t, anchor, where, priority, keepFocus) {
    var body = { priority: priority };
    body[where] = anchor.id;
    if (moving) return Promise.resolve();
    moving = true;
    if (keepFocus) state.focusTodo = t.id;
    return act(function () { return api('POST', '/api/todos/' + t.id + '/move', body); }, priority !== t.priority ? 'Moved to ' + priority : null)
      .finally(function () { moving = false; });
  }
  function wireTodoDrag(card, handle, t, list) {
    handle.addEventListener('pointerdown', function () { card.draggable = true; });
    handle.addEventListener('pointerup', function () { if (!drag) card.draggable = false; });
    handle.addEventListener('keydown', function (e) {
      if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
      e.preventDefault();
      var up = e.key === 'ArrowUp', nb = list[list.indexOf(t) + (up ? -1 : 1)];
      if (!nb) return;
      var same = nb.priority === t.priority;
      moveTodo(t, nb, up === same ? 'before' : 'after', nb.priority, true);
    });
    card.addEventListener('dragstart', function (e) {
      if (e.target !== card) return;
      drag = { t: t };
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', t.title);
      card.classList.add('dragging');
    });
    card.addEventListener('dragend', function () {
      drag = null;
      card.draggable = false;
      card.classList.remove('dragging');
      clearDropMarks();
    });
    card.addEventListener('dragover', function (e) {
      if (!drag || drag.t.id === t.id) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      var r = card.getBoundingClientRect(), cls = e.clientY > r.top + r.height / 2 ? 'drop-after' : 'drop-before';
      if (!card.classList.contains(cls)) { clearDropMarks(); card.classList.add(cls); }
    });
    card.addEventListener('dragleave', function (e) { if (!card.contains(e.relatedTarget)) card.classList.remove('drop-before', 'drop-after'); });
    card.addEventListener('drop', function (e) {
      if (!drag || drag.t.id === t.id) return;
      e.preventDefault();
      var moved = drag.t, where = card.classList.contains('drop-after') ? 'after' : 'before';
      clearDropMarks();
      var i = list.indexOf(moved), j = list.indexOf(t);
      if (moved.priority === t.priority && ((where === 'before' && j === i + 1) || (where === 'after' && j === i - 1))) return;
      moveTodo(moved, t, where, t.priority);
    });
  }

  function todoCard(t, view, openList) {
    var card = h('div', { class: 'todo' + (t.state === 'done' ? ' done' : ''), dataset: { ref: 'todo:' + t.id } });
    var handle = null;
    if (openList) {
      handle = h('button', { type: 'button', class: 'drag-handle', title: 'Drag to reorder, or focus and press ↑/↓',
        'aria-label': 'Reorder: ' + t.title + '. Press the up or down arrow to move it.' }, icon('grip-vertical'));
      wireTodoDrag(card, handle, t, openList);
    }
    function patch(data, msg) { return act(function () { return api('PATCH', '/api/todos/' + t.id, data); }, msg); }
    var stateSel = h('select', { class: 'state s-' + t.state, 'aria-label': 'Todo state' }, STATES.map(function (x) { return h('option', { value: x, selected: x === t.state }, STATE_LABEL[x]); }));
    stateSel.onchange = function () { patch({ state: stateSel.value }); };
    var prioSel = h('select', { class: 'prio p-' + t.priority, 'aria-label': 'Priority' }, PRIOS.map(function (p) { return h('option', { value: p, selected: p === t.priority }, p); }));
    prioSel.onchange = function () { patch({ priority: prioSel.value }); };
    var titleEl = h('div', { class: 'todo-title', title: 'Double-click to rename' }, md(t.title));
    titleEl.ondblclick = function () {
      var inp = h('input', { type: 'text', value: t.title, class: 'editing' });
      titleEl.replaceChildren(inp);
      inp.focus();
      inp.onkeydown = function (e) {
        if (e.key === 'Enter') patch({ title: inp.value });
        if (e.key === 'Escape') refresh(true);
      };
    };
    var notesBox = h('div', { class: 'notes' }, t.notes ? md(t.notes) : null);
    var notesBtn = h('button', { class: 'btn small ghost', title: 'Edit notes (Markdown + emoji)' }, icon(t.notes ? 'notebook-pen' : 'plus'), 'Notes');
    notesBtn.onclick = function () {
      notesBox.replaceChildren(editor(t.notes, {
        onSave: function (v) { patch({ notes: v }, 'Notes saved'); },
        onCancel: function () { notesBox.replaceChildren(t.notes ? md(t.notes) : ''); }
      }));
    };
    var del = iconBtn('trash-2', 'Delete todo', 'danger');
    del.onclick = function () { if (confirm('Delete this todo?\n\n' + t.title)) act(function () { return api('DELETE', '/api/todos/' + t.id); }, 'Todo deleted'); };
    var attachRow = h('div', { class: 'attach' }, t.attachments.map(function (a) { return attachmentChip(a); }), attachMenu(t, view));
    appendKids(card, [
      h('div', { class: 'todo-top' }, handle, stateSel, prioSel, titleEl, notesBtn, del),
      h('div', { class: 'times' }, when('created', t.created_at), ' · ', when('updated', t.updated_at), t.created_by && t.created_by !== 'user' ? [' · ', h('span', { class: 'src-i' }, sourceLabel(t.created_by))] : ''),
      notesBox, attachRow]);
    return card;
  }

  function attachmentChip(a) {
    var lbl;
    if (a.kind === 'url') {
      lbl = h('a', { class: 'lbl', href: a.url, target: '_blank', rel: 'noopener noreferrer', title: a.url }, icon('link'), a.title || a.url);
    } else {
      var art = { id: a.artifact_id, kind: a.artifact_kind, title: a.artifact_title || a.title, path: a.artifact_path, canvas_id: a.artifact_canvas_id };
      lbl = h('button', { class: 'lbl', type: 'button', title: a.artifact_path || '' }, icon(a.kind === 'reference' ? 'paperclip' : KIND_ICON[a.artifact_kind] || 'file'), a.title || a.artifact_title || 'artifact');
      lbl.onclick = function () { openViewer(art); };
    }
    var x = h('button', { class: 'x', type: 'button', title: 'Remove attachment', 'aria-label': 'Remove attachment' }, icon('x'));
    x.onclick = function () { act(function () { return api('DELETE', '/api/attachments/' + a.id); }); };
    return h('span', { class: 'chip' }, lbl, x);
  }

  function attachMenu(t, view) {
    var wrap = h('span', { style: { position: 'relative' } });
    var btn = h('button', { class: 'btn small', type: 'button' }, icon('paperclip'), 'Attach');
    var file = h('input', { type: 'file', class: 'hidden', multiple: true });
    file.onchange = function () {
      var files = Array.prototype.slice.call(file.files);
      act(async function () {
        for (var i = 0; i < files.length; i++) {
          await api('POST', '/api/sessions/' + t.session_id + '/references?todo=' + t.id, files[i],
            { 'X-Filename': encodeURIComponent(files[i].name), 'Content-Type': files[i].type || 'application/octet-stream' });
        }
      }, files.length + ' reference(s) uploaded to references/');
    };
    btn.onclick = function (e) {
      e.stopPropagation();
      closePopovers();
      var urlIn = h('input', { type: 'url', placeholder: 'https://…' });
      var artSel = h('select', {}, h('option', { value: '' }, 'Choose an artifact…'), view.artifacts.map(function (a) {
        return h('option', { value: a.id }, trunc(a.title || a.path || a.canvas_id, 70) + ' (' + a.kind + ')');
      }));
      var pop = h('div', { class: 'popover', style: { top: '30px', left: '0' } },
        h('div', { class: 'agroup-h' }, 'Link'),
        h('div', { class: 'inline-form' }, urlIn, h('button', { class: 'btn small primary', type: 'button', onclick: function () {
          if (urlIn.value.trim()) act(function () { return api('POST', '/api/todos/' + t.id + '/attachments', { kind: 'url', url: urlIn.value.trim() }); }, 'Link attached');
        } }, 'Add')),
        h('div', { class: 'agroup-h', style: { marginTop: '10px' } }, 'Artifact from this session'),
        h('div', { class: 'inline-form' }, artSel, h('button', { class: 'btn small primary', type: 'button', onclick: function () {
          if (artSel.value) act(function () { return api('POST', '/api/todos/' + t.id + '/attachments', { kind: 'artifact', artifact_id: Number(artSel.value) }); }, 'Artifact attached');
        } }, 'Attach')),
        h('div', { class: 'agroup-h', style: { marginTop: '10px' } }, 'Reference document'),
        h('button', { class: 'btn small', type: 'button', onclick: function () { file.click(); } }, icon('upload'), 'Upload to references/…'));
      pop.addEventListener('click', function (ev) { ev.stopPropagation(); });
      urlIn.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') pop.querySelector('.btn.primary').click(); });
      wrap.append(pop);
      urlIn.focus();
    };
    wrap.append(btn, file);
    return wrap;
  }

  /* ----- ledger ----- */
  function ledgerSection(view) {
    var sid = view.session.id;
    var tab = state.ledgerTab[sid] || 'decision';
    var L = view.ledger;
    var openIssues = L.issue.filter(function (i) { return i.status === 'open'; }).length;
    var tabs = h('div', { class: 'tabs', role: 'tablist' }, ['decision', 'mistake', 'issue'].map(function (k) {
      var n = k === 'issue' ? openIssues + ' open / ' + L.issue.length : L[k].length;
      var b = h('button', { class: 'tab' + (k === tab ? ' on' : ''), role: 'tab', type: 'button' }, KIND_LABEL[k] + ' ', h('span', { class: 'count' }, n));
      b.onclick = function () { state.ledgerTab[sid] = k; renderMain(true); };
      return b;
    }));
    var formBox = h('div');
    var addBtn = h('button', { class: 'btn small', type: 'button' }, icon('plus'), 'Record ' + KIND_ONE[tab]);
    addBtn.onclick = function () {
      var title = h('input', { type: 'text', placeholder: { decision: 'What was decided', mistake: 'Mistake (short title)', issue: 'Issue title' }[tab] });
      var status = h('select', {}, LEDGER_STATUS[tab].map(function (x) { return h('option', { value: x }, x); }));
      var why = h('input', { type: 'text', placeholder: tab === 'decision' ? 'Why (rationale)' : 'Why it was a mistake (root cause)' });
      var alts = h('input', { type: 'text', placeholder: 'Rejected alternatives (separate with ·)' });
      var lesson = h('input', { type: 'text', placeholder: 'Lesson: what a future session should do differently' });
      var ed = editor('', {
        placeholder: tab === 'mistake' ? 'What happened (Markdown)' : 'Details (Markdown)', saveLabel: 'Record',
        onSave: function (body) {
          if (!title.value.trim()) { title.focus(); return; }
          var data = { kind: tab, title: title.value, status: status.value, body: body };
          if (tab === 'decision') { data.rationale = why.value; data.alternatives = alts.value; }
          if (tab === 'mistake') { data.rationale = why.value; data.lesson = lesson.value; }
          act(function () { return api('POST', '/api/sessions/' + sid + '/ledger', data); }, 'Recorded');
        },
        onCancel: function () { formBox.replaceChildren(); }
      });
      var rows = [h('div', { class: 'inline-form' }, withEmoji(title), LEDGER_STATUS[tab].length > 1 ? status : null)];
      if (tab === 'decision') rows.push(h('div', { class: 'inline-form' }, withEmoji(why)), h('div', { class: 'inline-form' }, withEmoji(alts)));
      if (tab === 'mistake') rows.push(h('div', { class: 'inline-form' }, withEmoji(why)), h('div', { class: 'inline-form' }, withEmoji(lesson)),
        h('div', { class: 'muted', style: { fontSize: '12px', margin: '6px 2px 0' } }, icon('lock'), ' Mistakes are permanent once recorded.'));
      formBox.replaceChildren(h('div', { class: 'entry editing' }, rows, ed));
      title.focus();
    };
    var missedBtn = h('button', { class: 'btn small ghost', type: 'button', title: 'Tell the scribe it missed a mistake in one of this session\'s turns. Your reports become test cases for improving it.' }, icon('circle-plus'), 'Missed a mistake?');
    missedBtn.onclick = function () { missedForm(sid, formBox); };
    var entries = L[tab];
    return section([icon('notebook-text'), 'Ledger'], null, h('span', { class: 'sec-actions' }, missedBtn, addBtn),
      h('div', {}, tabs, h('div', { class: 'sec-body' }, formBox, entries.length ? entries.map(function (e) { return entryCard(e); }) :
        h('div', { class: 'empty' }, tab === 'mistake' ? 'No mistakes recorded. The scribe writes a lesson here whenever the session gets something wrong, so it is not repeated.' : 'Nothing recorded yet.'))));
  }

  function entryCard(e) {
    var card = h('div', { class: 'entry entry-' + e.kind, dataset: { ref: e.kind + ':' + e.id } });
    var opts = LEDGER_STATUS[e.kind];
    var statusEl = opts.length > 1 ? h('select', { class: 'st-' + e.status, 'aria-label': 'Status' }, opts.map(function (x) { return h('option', { value: x, selected: x === e.status }, x); })) : null;
    if (statusEl) statusEl.onchange = function () { act(function () { return api('PATCH', '/api/ledger/' + e.id, { status: statusEl.value }); }); };
    var bodyBox = h('div', { class: 'notes' }, e.body ? md(e.body) : null);
    var edit = null;
    if (e.kind === 'decision') {
      edit = iconBtn('pencil', 'Edit decision');
      edit.onclick = function () { card.replaceChildren(decisionForm(e)); };
    } else if (e.kind === 'issue') {
      edit = iconBtn('pencil', 'Edit issue');
      edit.onclick = function () { card.replaceChildren(issueForm(e)); };
    }
    var lock = e.kind === 'mistake' ? h('span', { class: 'src', title: 'Mistakes are permanent records; they cannot be edited' }, icon('lock'), 'permanent') : null;
    var del = iconBtn('trash-2', 'Delete entry', 'danger');
    del.onclick = function () {
      var msg = e.kind === 'mistake' ? 'Mistakes are permanent lessons. Delete this one only if it was recorded in error.\n\n' : 'Delete this ledger entry?\n\n';
      if (confirm(msg + e.title)) act(function () { return api('DELETE', '/api/ledger/' + e.id); }, 'Deleted');
    };
    var ev = (e.evidence || []).map(function (x) { return x.item; }).filter(Boolean);
    var isMistake = e.kind === 'mistake';
    var corrected = isMistake && (e.corrections || []).length > 0;
    var flag = null;
    if (e.source === 'extractor') {
      flag = h('button', { class: 'btn small ghost' + (e.flagged_wrong ? ' flagged' : ' icon-only'), title: e.flagged_wrong ? 'You flagged this scribe entry as wrong' : 'Flag this scribe entry as wrong (it becomes a test case for improving the scribe)', 'aria-label': e.flagged_wrong ? 'Flagged as wrong' : 'Flag as wrong' }, icon('thumbs-down'), e.flagged_wrong ? 'flagged' : null);
      flag.onclick = function () {
        var note = prompt('What is wrong with this entry? (optional)', '');
        if (note === null) return;
        act(function () { return api('POST', '/api/ledger/' + e.id + '/feedback', { note: note }); }, 'Flagged — thanks, this becomes a test case');
      };
    }
    var fixBox = h('div');
    var mistakeActions = null;
    if (isMistake) {
      var correctBtn = h('button', { class: 'btn small ghost', title: 'Append a correction. The original stays as written; the corrected lesson is what sessions see.' }, icon('pencil-line'), 'Correct');
      correctBtn.onclick = function () { fixBox.replaceChildren(correctionForm(e, fixBox)); };
      var recheckBtn = h('button', { class: 'btn small ghost', title: 'Ask the scribe to re-check this mistake against its turn and add a correction if the cause or lesson is off' }, icon('refresh-cw'), 'Re-check');
      recheckBtn.onclick = function () {
        recheckBtn.disabled = true; recheckBtn.replaceChildren(icon('loader-circle', 'spin'), 'Re-checking…');
        act(function () { return api('POST', '/api/ledger/' + e.id + '/recheck'); }).then(function (r) {
          if (r) toast(r.verdict === 'corrected' ? 'The scribe added a correction' : 'The scribe confirmed this lesson');
        }).finally(function () { recheckBtn.disabled = false; recheckBtn.replaceChildren(icon('refresh-cw'), 'Re-check'); });
      };
      mistakeActions = h('span', { class: 'entry-actions' }, correctBtn, recheckBtn);
    }
    var lessonText = isMistake ? (e.effective_lesson || e.lesson) : null;
    var causeText = isMistake ? (e.effective_cause || e.rationale) : e.rationale;
    appendKids(card, [
      h('div', { class: 'entry-top' }, h('div', { class: 'entry-title' }, md(e.title)), statusEl, lock, mistakeActions, flag, edit, del),
      h('div', { class: 'times' }, h('span', { class: 'src' }, sourceLabel(e.source)), e.made_by ? ' · by ' + e.made_by : '', ' · ', when('', e.created_at),
        e.updated_at && e.updated_at - e.created_at > 1000 ? [' · ', when('edited', e.updated_at)] : null),
      lessonText ? h('div', { class: 'lesson' }, h('b', {}, icon('lightbulb'), corrected ? 'Lesson (corrected): ' : 'Lesson: '), mdInline(lessonText)) : null,
      isMistake && !lessonText ? h('div', { class: 'lesson missing' }, icon('lightbulb'), 'No lesson yet — use Re-check or Correct to add one.') : null,
      isMistake && e.body ? h('div', { class: 'kv' }, h('b', {}, 'What happened: ')) : null,
      bodyBox,
      causeText ? h('div', { class: 'kv' }, h('b', {}, isMistake ? 'Why it was a mistake: ' : 'Why: '), mdInline(causeText)) : null,
      corrected ? correctionHistory(e) : null,
      fixBox,
      (e.alternatives || []).length ? h('div', { class: 'kv' }, h('b', {}, 'Rejected: '), mdInline(e.alternatives.join(' · '))) : null,
      e.what_worked ? h('div', { class: 'kv kv-worked' }, h('b', {}, 'What worked instead: '), mdInline(e.what_worked)) : null,
      (e.turn_id || ev.length) ? h('div', { class: 'evidence' }, 'evidence: ' + (e.turn_id ? 'turn ' + e.turn_id.slice(-8) : '') + (ev.length ? ' · items ' + ev.join(', ') : '')) : null]);
    return card;
  }

  function correctionHistory(e) {
    var rows = [h('div', { class: 'corr' }, h('div', { class: 'times' }, icon('history'), 'Original · ', sourceLabel(e.source), ' · ', when('', e.created_at)),
      e.rationale ? h('div', { class: 'kv' }, h('b', {}, 'Cause: '), mdInline(e.rationale)) : null,
      e.lesson ? h('div', { class: 'kv' }, h('b', {}, 'Lesson: '), mdInline(e.lesson)) : h('div', { class: 'kv muted' }, 'No lesson was recorded.'))];
    e.corrections.forEach(function (x) {
      rows.push(h('div', { class: 'corr' }, h('div', { class: 'times' }, icon('pencil-line'), 'Correction · ', sourceLabel(x.source), ' · ', when('', x.created_at)),
        x.cause ? h('div', { class: 'kv' }, h('b', {}, 'Cause: '), mdInline(x.cause)) : null,
        x.lesson ? h('div', { class: 'kv' }, h('b', {}, 'Lesson: '), mdInline(x.lesson)) : null,
        x.note ? h('div', { class: 'kv' }, h('b', {}, 'Why: '), mdInline(x.note)) : null));
    });
    return h('details', { class: 'corr-history' }, h('summary', {}, 'Correction history (' + e.corrections.length + ')'), rows);
  }

  function correctionForm(e, box) {
    var cause = h('input', { type: 'text', value: e.effective_cause || e.rationale || '', placeholder: 'Corrected root cause', 'aria-label': 'Corrected cause' });
    var lesson = h('input', { type: 'text', value: e.effective_lesson || e.lesson || '', placeholder: 'Corrected lesson for future sessions', 'aria-label': 'Corrected lesson' });
    var ed = editor('', {
      placeholder: 'Why this correction (optional, Markdown)', saveLabel: 'Add correction',
      onSave: function (note) {
        if (!cause.value.trim() && !lesson.value.trim()) { lesson.focus(); return; }
        act(function () { return api('POST', '/api/ledger/' + e.id + '/corrections', { cause: cause.value, lesson: lesson.value, note: note }); }, 'Correction added');
      },
      onCancel: function () { box.replaceChildren(); }
    });
    return h('div', { class: 'editing' },
      h('div', { class: 'muted', style: { fontSize: '12px', margin: '2px 2px 6px' } }, 'The original stays as written; your correction is what sessions will follow.'),
      h('div', { class: 'inline-form' }, withEmoji(cause)), h('div', { class: 'inline-form' }, withEmoji(lesson)), ed);
  }

  function missedForm(sid, box) {
    var sel = h('select', { 'aria-label': 'Turn' }, h('option', { value: '' }, 'Loading turns…'));
    api('GET', '/api/sessions/' + sid + '/turns').then(function (d) {
      sel.replaceChildren.apply(sel, [h('option', { value: '' }, 'Which turn? (newest first)')].concat(d.turns.map(function (t) {
        return h('option', { value: t.turn_id }, '#' + (t.ordinal == null ? '?' : t.ordinal) + ' · ' + rel(t.completed_at) + ' · ' + trunc(t.user_message || '(no message)', 70));
      })));
    }).catch(function () { sel.replaceChildren(h('option', { value: '' }, 'Could not load turns')); });
    var ed = editor('', {
      placeholder: 'What mistake did the session make that the scribe missed? (Markdown)', saveLabel: 'Report',
      onSave: function (note) {
        if (!sel.value) { sel.focus(); return; }
        if (!note.trim()) return;
        act(function () { return api('POST', '/api/sessions/' + sid + '/feedback', { turn_id: sel.value, note: note }); }, 'Reported — this becomes a test case for the scribe');
      },
      onCancel: function () { box.replaceChildren(); }
    });
    box.replaceChildren(h('div', { class: 'entry editing' }, h('div', { class: 'inline-form' }, sel), ed));
  }

  function issueForm(e) {
    var title = h('input', { type: 'text', value: e.title, 'aria-label': 'Issue title' });
    var status = h('select', { 'aria-label': 'Status' }, LEDGER_STATUS.issue.map(function (x) { return h('option', { value: x, selected: x === e.status }, x); }));
    var ed = editor(e.body, {
      placeholder: 'Details (Markdown)', saveLabel: 'Save issue',
      onSave: function (body) {
        if (!title.value.trim()) { title.focus(); return; }
        act(function () { return api('PATCH', '/api/ledger/' + e.id, { title: title.value, status: status.value, body: body }); }, 'Issue updated');
      },
      onCancel: function () { refresh(true); }
    });
    return h('div', { class: 'editing issue-form' }, h('div', { class: 'inline-form' }, withEmoji(title), status), ed);
  }

  function decisionForm(e) {
    var title = h('input', { type: 'text', value: e.title, 'aria-label': 'Decision title' });
    var status = h('select', { 'aria-label': 'Status' }, LEDGER_STATUS.decision.map(function (x) { return h('option', { value: x, selected: x === e.status }, x); }));
    var why = h('input', { type: 'text', value: e.rationale || '', placeholder: 'Why (rationale)', 'aria-label': 'Rationale' });
    var alts = h('input', { type: 'text', value: (e.alternatives || []).join(' · '), placeholder: 'Rejected alternatives (separate with ·)', 'aria-label': 'Rejected alternatives' });
    var ed = editor(e.body, {
      placeholder: 'Details (Markdown)', saveLabel: 'Save decision',
      onSave: function (body) {
        if (!title.value.trim()) { title.focus(); return; }
        act(function () { return api('PATCH', '/api/ledger/' + e.id, { title: title.value, status: status.value, rationale: why.value, alternatives: alts.value, body: body }); }, 'Decision updated');
      },
      onCancel: function () { refresh(true); }
    });
    return h('div', { class: 'editing decision-form' },
      h('div', { class: 'inline-form' }, withEmoji(title), status),
      h('div', { class: 'inline-form' }, withEmoji(why)),
      h('div', { class: 'inline-form' }, withEmoji(alts)), ed);
  }

  /* ----- artifacts ----- */
  var RENDERABLE = { html: true, markdown: true, image: true, pdf: true, canvas: true };
  function artifactSection(view) {
    var sid = view.session.id;
    var arts = view.artifacts;
    var rest = arts.filter(function (a) { return !a.pinned_at; });
    var groups = [
      ['pinned', 'pin', 'Pinned', arts.filter(function (a) { return a.pinned_at; })],
      ['canvas', 'workflow', 'Workflow canvases', rest.filter(function (a) { return a.kind === 'canvas'; })],
      ['html', 'file-code', 'HTML', rest.filter(function (a) { return a.kind === 'html' && a.origin !== 'reference'; })],
      ['markdown', 'file-text', 'Markdown', rest.filter(function (a) { return a.kind === 'markdown' && a.origin !== 'reference'; })],
      ['media', 'file-image', 'Images & PDFs', rest.filter(function (a) { return (a.kind === 'image' || a.kind === 'pdf') && a.origin !== 'reference'; })],
      ['references', 'paperclip', 'References', rest.filter(function (a) { return a.origin === 'reference'; })],
      ['other', 'file', 'Other files', rest.filter(function (a) { return (a.kind === 'other' || a.kind === 'excalidraw') && a.origin !== 'reference'; })]
    ];
    var canvasBtn = h('button', { class: 'btn small', type: 'button' }, icon('workflow'), 'Canvas', icon('chevron-down'));
    var wrap = h('span', { style: { position: 'relative' } }, canvasBtn);
    canvasBtn.onclick = function (e) { e.stopPropagation(); canvasMenu(wrap, sid); };
    var body = h('div', { class: 'sec-body' });
    var any = false;
    groups.forEach(function (g) {
      if (!g[3].length) return;
      any = true;
      var key = sid + ':' + g[0];
      var collapsed = g[0] === 'other' && !state.openGroups.has(key);
      var det = h('details', { class: 'agroup', open: !collapsed });
      det.addEventListener('toggle', function () { if (det.open) state.openGroups.add(key); else state.openGroups.delete(key); });
      appendKids(det, [h('summary', {}, icon(g[1]), g[2] + ' (' + g[3].length + ')'), g[3].map(function (a) { return artifactRow(a); })]);
      body.append(det);
    });
    if (!any) body.append(h('div', { class: 'empty' }, 'No artifacts yet. Files the session saves in its folder appear here automatically: ', h('span', { class: 'mono' }, view.session.artifacts_dir || '')));
    return section([icon('files'), 'Artifacts'], arts.length, h('div', { class: 'actions' },
      h('button', { class: 'btn small', onclick: function () { act(function () { return api('POST', '/api/sessions/' + sid + '/reveal-folder'); }, 'Opened in Finder'); } }, icon('folder-open'), 'Open folder'), wrap), body);
  }

  function artifactRow(a) {
    var renderable = RENDERABLE[a.kind];
    var name = h('button', { class: 'an' + (renderable ? '' : ' plain'), type: 'button', title: a.path || a.canvas_id || '' }, a.title || a.path || a.canvas_id);
    if (renderable) name.onclick = function () { openViewer(a); };
    var meta = a.kind === 'canvas' ? 'canvas ' + a.canvas_id : bytes(a.size) + ' · ' + rel(a.mtime);
    return h('div', { class: 'arow', dataset: { ref: 'artifact:' + a.id } },
      h('span', { class: 'ico' }, icon(KIND_ICON[a.kind] || 'file')),
      pinBtn(a.pinned_at, 'artifact', function (on) { setPin('artifacts', a.id, on); }), name,
      a.origin === 'linked' ? h('span', { class: 'tag link' }, 'linked') : null,
      h('span', { class: 'ameta' }, meta),
      renderable ? h('button', { class: 'btn small', type: 'button', onclick: function () { openViewer(a); } }, 'View') : null,
      a.path ? h('button', { class: 'btn small', type: 'button', title: 'Show in Finder — open it with any app', onclick: function () { reveal(a); } }, 'Show in Finder') : null,
      a.kind === 'canvas' ? h('a', { class: 'btn small', href: canvasUrl(a.canvas_id), target: '_blank', rel: 'noopener' }, 'Open', icon('external-link')) : null,
      (a.origin === 'linked' || a.origin === 'canvas') ? h('button', { class: 'btn small ghost danger icon-only', type: 'button', title: 'Unlink from this session', 'aria-label': 'Unlink from this session', onclick: function () {
        if (confirm('Unlink this from the session? (The file or canvas itself is not deleted.)')) act(function () { return api('DELETE', '/api/artifacts/' + a.id); }, 'Unlinked');
      } }, icon('unlink')) : null);
  }
  function canvasUrl(id) { return ((state.health && state.health.workflow_canvas_url) || 'http://localhost:8790') + '/?doc=' + encodeURIComponent(id); }
  function reveal(a) { act(function () { return api('POST', '/api/artifacts/' + a.id + '/reveal'); }, 'Shown in Finder'); }

  async function canvasMenu(wrap, sid) {
    closePopovers();
    var list = h('div', {}, h('div', { class: 'empty' }, 'Loading canvases…'));
    var newTitle = h('input', { type: 'text', placeholder: 'New canvas title' });
    var pop = h('div', { class: 'popover', style: { top: '30px', right: '0', width: '340px' } },
      h('div', { class: 'agroup-h' }, 'New canvas for this session'),
      h('div', { class: 'inline-form' }, newTitle, h('button', { class: 'btn small primary', type: 'button', onclick: function () {
        if (newTitle.value.trim()) act(function () { return api('POST', '/api/sessions/' + sid + '/canvases', { new_title: newTitle.value.trim() }); }, 'Canvas created and linked');
      } }, 'Create')),
      h('div', { class: 'agroup-h', style: { marginTop: '10px' } }, 'Link an existing canvas'), list);
    pop.addEventListener('click', function (e) { e.stopPropagation(); });
    wrap.append(pop);
    newTitle.focus();
    try {
      var d = await api('GET', '/api/canvas/documents');
      if (d.error) { list.replaceChildren(h('div', { class: 'empty' }, d.error)); return; }
      list.replaceChildren.apply(list, d.documents.map(function (doc) {
        var b = h('div', { class: 'list-row' }, icon('workflow'), h('span', { style: { flex: '1' } }, doc.title || doc.id), h('span', { class: 'muted', style: { fontSize: '11px' } }, doc.nodeCount + ' nodes'));
        b.onclick = function () { act(function () { return api('POST', '/api/sessions/' + sid + '/canvases', { document_id: doc.id, title: doc.title }); }, 'Canvas linked'); };
        return b;
      }));
    } catch (e) { list.replaceChildren(h('div', { class: 'empty' }, e.message)); }
  }

  /* ----- children (recursive) ----- */
  function childrenSection(view, depth) {
    var kids = view.children;
    var body = h('div', { class: 'sec-body' });
    if (!kids.length) body.append(h('div', { class: 'empty' }, 'No child sessions.'));
    kids.forEach(function (ch) {
      var c = ch.counts || {};
      var expanded = state.expanded.has(ch.id);
      var nested = h('div', { class: 'nested' + (expanded ? '' : ' hidden') });
      var toggle = h('button', { class: 'btn small', type: 'button', 'aria-expanded': String(expanded) }, icon(expanded ? 'chevron-down' : 'chevron-right'), expanded ? 'Collapse' : 'Expand');
      function load() {
        nested.replaceChildren(h('div', { class: 'empty' }, 'Loading…'));
        api('GET', '/api/sessions/' + ch.id).then(function (v) { nested.replaceChildren(sessionView(v, depth + 1)); applyHighlight(); })
          .catch(function (e) { nested.replaceChildren(h('div', { class: 'empty' }, e.message)); });
      }
      toggle.onclick = function () {
        var now = !state.expanded.has(ch.id);
        if (now) state.expanded.add(ch.id); else state.expanded.delete(ch.id);
        toggle.replaceChildren(icon(now ? 'chevron-down' : 'chevron-right'), now ? 'Collapse' : 'Expand');
        toggle.setAttribute('aria-expanded', String(now));
        nested.classList.toggle('hidden', !now);
        if (now) load();
      };
      if (expanded) load();
      body.append(h('div', { class: 'child' },
        h('div', { class: 'child-top' }, pinBtn(ch.pinned_at, 'child session', function (on) { setPin('sessions', ch.id, on); }),
          h('span', { class: 'dot ' + ch.status }),
          h('span', { class: 'child-title', title: ch.display_title }, ch.display_title),
          h('span', { class: 'badges' },
            c.todos_open ? h('span', { class: 'mini m-todo', title: 'open todos' }, icon('square-check-big'), c.todos_open) : null,
            c.decisions ? h('span', { class: 'mini', title: 'decisions' }, icon('scale'), c.decisions) : null,
            c.mistakes ? h('span', { class: 'mini', title: 'mistakes' }, icon('triangle-alert'), c.mistakes) : null,
            c.issues_open ? h('span', { class: 'mini m-issue', title: 'open issues' }, icon('bug'), c.issues_open) : null,
            c.artifacts ? h('span', { class: 'mini', title: 'artifacts' }, icon('files'), c.artifacts) : null,
            c.children ? h('span', { class: 'mini', title: 'child sessions' }, icon('git-fork'), c.children) : null),
          h('span', { class: 'pill ' + ch.status }, STATUS_LABEL[ch.status] || ch.status),
          h('span', { class: 'times', title: abs(ch.updated_at) }, rel(ch.updated_at)),
          toggle, h('button', { class: 'btn small', type: 'button', onclick: navLink(ch.id) }, 'Open', icon('arrow-right'))),
        nested));
    });
    return section([icon('git-fork'), 'Child sessions'], kids.length, null, body);
  }

  /* ---------- viewer ---------- */
  /* Each HTML artifact loads from its own origin (a<id>.localhost, same port) so its scripts can't reach the app or other artifacts. */
  function artifactBase(a) { var t = (state.health && state.health.artifact_base) || ''; return t.replace('{id}', a.id); }
  function closeViewer() { var v = $('#viewer'); v.classList.add('hidden'); v.replaceChildren(); }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !$('#viewer').classList.contains('hidden')) closeViewer();
    if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); $('#search').focus(); }
  });
  async function openViewer(a) {
    var v = $('#viewer');
    var body = h('div', { class: 'viewer-body' });
    var base = '/raw/' + a.id + '/';
    var name = (a.path || '').split('/').pop();
    var head = h('div', { class: 'viewer-head' },
      h('span', { class: 'ico' }, icon(KIND_ICON[a.kind] || 'file')),
      h('div', { class: 'vt' }, h('b', {}, a.title || name || a.canvas_id), h('div', { class: 'vp' }, a.path || canvasUrl(a.canvas_id))),
      a.path ? h('button', { class: 'btn small', type: 'button', onclick: function () { reveal(a); } }, 'Show in Finder') : null,
      a.kind === 'canvas' ? h('a', { class: 'btn small', href: canvasUrl(a.canvas_id), target: '_blank', rel: 'noopener' }, 'Open in Workflow Canvas', icon('external-link')) :
        (a.path ? h('a', { class: 'btn small', href: (a.kind === 'html' ? artifactBase(a) : '') + base + encodeURIComponent(name), target: '_blank', rel: 'noopener' }, 'Open in new tab', icon('external-link')) : null),
      h('button', { class: 'btn small', type: 'button', onclick: closeViewer }, 'Close', icon('x')));
    var box = h('div', { class: 'viewer-box' }, head, body);
    box.addEventListener('click', function (e) { e.stopPropagation(); });
    v.replaceChildren(box);
    v.onclick = closeViewer;
    v.classList.remove('hidden');
    if (a.kind === 'html') body.append(h('iframe', { src: artifactBase(a) + base + encodeURIComponent(name), sandbox: 'allow-scripts allow-same-origin allow-popups allow-forms allow-modals allow-downloads', title: a.title || name }));
    else if (a.kind === 'pdf') body.append(h('iframe', { src: base + encodeURIComponent(name), title: a.title || name }));
    else if (a.kind === 'image') body.append(h('img', { class: 'full', src: base + encodeURIComponent(name), alt: a.title || name }));
    else if (a.kind === 'canvas') body.append(h('iframe', { src: canvasUrl(a.canvas_id), title: 'Workflow Canvas ' + a.canvas_id }));
    else if (a.kind === 'markdown') {
      try {
        var r = await fetch(base + encodeURIComponent(name));
        if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
        body.append(md(await r.text(), base));
      } catch (e) { body.append(h('div', { class: 'cant' }, 'Could not load: ' + e.message)); }
    } else {
      body.append(h('div', { class: 'cant' }, h('p', {}, 'This file type can’t be previewed here.'),
        a.path ? h('button', { class: 'btn primary', type: 'button', onclick: function () { reveal(a); } }, 'Show in Finder') : null));
    }
  }

  /* ---------- home ---------- */
  function renderHome() {
    var all = flatten(state.tree);
    var by = {};
    all.forEach(function (n) { (by[n.status] = by[n.status] || []).push(n); });
    function list(title, nodes) {
      if (!nodes || !nodes.length) return null;
      nodes = nodes.slice().sort(function (a, b) { return (b.updated_at || 0) - (a.updated_at || 0); });
      return section(title, nodes.length, null, h('div', { class: 'sec-body' }, nodes.map(function (n) {
        return h('div', { class: 'list-row', onclick: navLink(n.id) }, h('span', { class: 'dot ' + n.status }), h('span', { style: { flex: '1', minWidth: '0', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, n.title),
          n.depth ? h('span', { class: 'tag' }, 'depth ' + n.depth) : null, h('span', { class: 'times', title: abs(n.updated_at) }, rel(n.updated_at)));
      })));
    }
    var openTodos = all.reduce(function (s, n) { return s + ((n.counts || {}).todos_open || 0); }, 0);
    var openIssues = all.reduce(function (s, n) { return s + ((n.counts || {}).issues_open || 0); }, 0);
    return h('div', { class: 'home' },
      h('h1', {}, 'Where everything stands'),
      h('div', { class: 'muted' }, 'Every Codex session and subagent, from Codex’s own records. Pick one on the left, or search with /.'),
      h('div', { class: 'stats' },
        [['Running', (by.running || []).length], ['Waiting on you', (by.waiting || []).length], ['Failed', (by.failed || []).length], ['Open todos', openTodos], ['Open issues', openIssues], ['Sessions shown', all.length]]
          .map(function (x) { return h('div', { class: 'stat' }, h('div', { class: 'n' }, x[1]), h('div', { class: 'l' }, x[0])); })),
      list([h('span', { class: 'dot running' }), 'Running now'], by.running), list([h('span', { class: 'dot waiting' }), 'Waiting on you'], by.waiting),
      list([h('span', { class: 'dot failed' }), 'Failed'], by.failed));
  }

  /* ---------- rendering + live refresh ---------- */
  function applyHighlight() {
    if (!state.highlight) return;
    var el = document.querySelector('[data-ref="' + state.highlight + '"]');
    if (!el) return;
    state.highlight = null;
    el.scrollIntoView({ block: 'center' });
    el.animate([{ boxShadow: '0 0 0 3px var(--accent)' }, { boxShadow: '0 0 0 0 transparent' }], { duration: 1800 });
  }
  async function renderMain(force, before) {
    var main = $('#main');
    var id = state.currentId;
    if (!id) { main.replaceChildren(renderHome()); return; }
    var view;
    try { view = await api('GET', '/api/sessions/' + encodeURIComponent(id)); } catch (e) {
      main.replaceChildren(h('div', { class: 'empty' }, 'Could not load session ' + id + ': ' + e.message));
      return;
    }
    if (id !== state.currentId) return;
    if (force ? (before && openedSince(before)) : busy()) { state.pending = true; return; }
    var key = JSON.stringify(view);
    if (!force && state.viewKey[id] === key) return;
    state.viewKey = {};
    state.viewKey[id] = key;
    var top = main.scrollTop;
    var same = main.firstChild && main.firstChild.dataset && main.firstChild.dataset.id === id;
    main.replaceChildren(sessionView(view, 0));
    moving = false;
    main.scrollTop = same ? top : 0;
    if (state.focusTodo) {
      var grip = main.querySelector('[data-ref="todo:' + state.focusTodo + '"] .drag-handle');
      state.focusTodo = null;
      if (grip) grip.focus();
    }
    document.title = view.session.display_title + ' — Session Tracker';
    applyHighlight();
  }
  function busy() { return !!drag || !!document.querySelector(INTERACTIVE) || !$('#viewer').classList.contains('hidden'); }
  async function refresh(force, before) {
    if (!force && busy()) { state.pending = true; return; }
    state.pending = false;
    await Promise.all([loadTree().catch(function () {}), loadHealth(), renderMain(!!force, before)]);
  }
  setInterval(function () { if (state.pending && !busy()) refresh(false); }, 1500);
  var refreshTimer;
  function connectEvents() {
    var es = new EventSource('/api/events');
    es.onmessage = function (e) {
      var v = JSON.parse(e.data).version;
      if (v === state.version) return;
      state.version = v;
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(function () { refresh(false); }, 500);
    };
  }

  /* ----- resizable sidebar ----- */
  /* The stylesheet's default sidebar is 330px and it switches to one column below 900px, so the smallest session pane
     it lays out side by side is 570px; dragging keeps at least that. The narrowest sidebar is measured: the width its
     own controls need at their natural size. */
  var SIDE_KEY = 'st-side-width', SIDE_DEFAULT = 330, LAYOUT_BREAK = 900, MAIN_MIN = LAYOUT_BREAK - SIDE_DEFAULT;
  function naturalWidth(el) {
    var flex = el.style.flex;
    el.style.flex = 'none';
    var w = el.getBoundingClientRect().width;
    el.style.flex = flex;
    return w;
  }
  function sideBounds() {
    var tools = $('.side-tools'), cs = getComputedStyle(tools);
    var pad = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) + parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth);
    var need = Array.prototype.map.call(tools.querySelectorAll(':scope > .side-row'), function (row) {
      var kids = Array.prototype.filter.call(row.children, function (k) { return k.offsetParent !== null; });
      var gap = parseFloat(getComputedStyle(row).columnGap) || 0;
      return kids.reduce(function (s, k) { return s + naturalWidth(k); }, 0) + gap * Math.max(0, kids.length - 1);
    });
    var min = Math.ceil(Math.max.apply(null, need.concat([0])) + pad);
    return { min: min, max: Math.max(min, window.innerWidth - MAIN_MIN) };
  }
  function setSideWidth(w, bounds) {
    var b = bounds || sideBounds();
    w = Math.round(Math.min(b.max, Math.max(b.min, w)));
    $('.layout').style.setProperty('--side-w', w + 'px');
    var hd = $('#side-resizer');
    hd.setAttribute('aria-valuenow', w);
    hd.setAttribute('aria-valuemin', b.min);
    hd.setAttribute('aria-valuemax', b.max);
    return w;
  }
  function saveSideWidth(w) {
    try { if (w == null) localStorage.removeItem(SIDE_KEY); else localStorage.setItem(SIDE_KEY, String(w)); } catch (e) { /* page-only */ }
  }
  function savedSideWidth() {
    var n = Number(localStorage.getItem(SIDE_KEY));
    return n > 0 ? n : SIDE_DEFAULT;
  }
  function initSideResizer() {
    var hd = $('#side-resizer'), current = setSideWidth(savedSideWidth());
    hd.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      e.preventDefault();
      hd.focus();
      hd.setPointerCapture(e.pointerId);
      var startX = e.clientX, startW = $('#sidebar').getBoundingClientRect().width, b = sideBounds(), frame = 0, lastX = startX;
      document.body.classList.add('resizing');
      hd.classList.add('dragging');
      function move(ev) {
        lastX = ev.clientX;
        if (!frame) frame = requestAnimationFrame(function () { frame = 0; current = setSideWidth(startW + lastX - startX, b); });
      }
      function up() {
        if (frame) { cancelAnimationFrame(frame); frame = 0; current = setSideWidth(startW + lastX - startX, b); }
        hd.removeEventListener('pointermove', move);
        hd.removeEventListener('pointerup', up);
        hd.removeEventListener('pointercancel', up);
        document.body.classList.remove('resizing');
        hd.classList.remove('dragging');
        saveSideWidth(current);
      }
      hd.addEventListener('pointermove', move);
      hd.addEventListener('pointerup', up);
      hd.addEventListener('pointercancel', up);
    });
    hd.addEventListener('dblclick', function () { saveSideWidth(null); current = setSideWidth(SIDE_DEFAULT); });
    hd.addEventListener('keydown', function (e) {
      var b = sideBounds(), step = e.shiftKey ? 50 : 10, w = null;
      if (e.key === 'ArrowLeft') w = current - step;
      else if (e.key === 'ArrowRight') w = current + step;
      else if (e.key === 'Home') w = b.min;
      else if (e.key === 'End') w = b.max;
      else if (e.key === 'Enter') { saveSideWidth(null); w = SIDE_DEFAULT; }
      if (w == null) return;
      e.preventDefault();
      current = setSideWidth(w, b);
      if (e.key !== 'Enter') saveSideWidth(current);
    });
    window.addEventListener('resize', function () { current = setSideWidth(savedSideWidth()); });
  }

  /* ---------- init ---------- */
  async function init() {
    function themeIcon() {
      var dark = document.documentElement.classList.contains('dark');
      var t = $('#theme');
      t.replaceChildren(icon(dark ? 'sun' : 'moon'));
      t.title = dark ? 'Switch to light mode' : 'Switch to dark mode';
      t.setAttribute('aria-label', t.title);
    }
    themeIcon();
    $('#theme').onclick = function () {
      var dark = document.documentElement.classList.toggle('dark');
      localStorage.setItem('st-theme', dark ? 'dark' : 'light');
      themeIcon();
    };
    $('.search-wrap').prepend(icon('search', 'field-i'));
    initTreeControls();
    initSideResizer();
    $('#search').addEventListener('input', function () { clearTimeout(searchTimer); searchTimer = setTimeout(doSearch, 200); });
    $('#search').addEventListener('keydown', function (e) { if (e.key === 'Escape') { e.target.value = ''; $('#search-results').classList.add('hidden'); } });
    document.addEventListener('click', function (e) { if (!e.target.closest('.search-wrap')) $('#search-results').classList.add('hidden'); });
    $('.brand').onclick = navLink(null);
    await loadEmoji();
    state.currentId = idFromPath();
    await Promise.all([loadTree(), loadHealth()]);
    if (state.currentId) { openAncestors(state.currentId); state.scrollTree = true; renderTree(); }
    await renderMain(true);
    connectEvents();
  }
  init();
})();

