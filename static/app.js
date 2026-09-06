/* Royal Stock — shared client helpers */
(function () {
  'use strict';

  function getPreferredTheme() {
    var saved = null;
    try { saved = localStorage.getItem('theme'); } catch (e) { /* private mode */ }
    if (saved === 'light' || saved === 'dark') return saved;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function applyTheme(theme) {
    var next = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('theme', next); } catch (e) { /* ignore */ }
    var button = document.getElementById('themeToggle');
    if (button) {
      button.textContent = next === 'dark' ? '☀️ Light' : '🌙 Dark';
      button.setAttribute('aria-pressed', String(next === 'dark'));
      button.setAttribute('aria-label', 'Switch to ' + (next === 'dark' ? 'light' : 'dark') + ' theme');
    }
  }

  applyTheme(getPreferredTheme());

  document.addEventListener('DOMContentLoaded', function () {
    var themeToggle = document.getElementById('themeToggle');
    if (themeToggle) {
      themeToggle.addEventListener('click', function () {
        applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
      });
    }

    var navToggle2 = document.getElementById('navToggle');
    if (navToggle2) {
      navToggle2.addEventListener('click', function () {
        document.querySelector('.nav')?.classList.toggle('open');
        var open = document.querySelector('.nav')?.classList.contains('open');
        navToggle2.setAttribute('aria-expanded', String(!!open));
      });
    }

    // highlight current page in the nav
    var path = window.location.pathname;
    document.querySelectorAll('.nav a').forEach(function (link) {
      var href = link.getAttribute('href');
      var isHome = href === '/' && path === '/';
      var isSection = href !== '/' && (path === href || path.indexOf(href + '/') === 0);
      if (isHome || isSection) link.classList.add('active');
    });
    // when parent + child both match (e.g. /invest and /invest/stocks),
    // keep only the longest href so one page is highlighted
    (function () {
      var actives = Array.prototype.slice.call(document.querySelectorAll('.nav a.active'));
      if (actives.length > 1) {
        actives.sort(function (a, b) {
          return (b.getAttribute('href') || '').length - (a.getAttribute('href') || '').length;
    // global topbar search: autocomplete + Enter jumps to the stock report.
    // "/" focuses it from anywhere (unless already typing).
    (function () {
      var form = document.getElementById('topSearch');
      var input = document.getElementById('topSearchInput');
      if (!form || !input) return;
      if (window.RS.attachSymbolAutocomplete) {
        window.RS.attachSymbolAutocomplete('topSearchInput', null);
      }
      form.addEventListener('submit', function (event) {
        event.preventDefault();
        var symbol = input.value.trim().toUpperCase().replace(/\.NS$|\.BO$|\.US$/, '');
        if (symbol) window.location.href = '/fundamentals?symbol=' + encodeURIComponent(symbol);
      });
      document.addEventListener('keydown', function (event) {
        if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey) return;
        var tag = (document.activeElement && document.activeElement.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        event.preventDefault();
        input.focus();
      });
    });

    // recently viewed symbols (written by the fundamentals page, read anywhere)
    window.RS.pushRecent = function (symbol, market) {
      try {
        var key = 'recentSymbols';
        var list = JSON.parse(localStorage.getItem(key) || '[]');
        list = [{ s: symbol, m: market || 'in' }].concat(
          list.filter(function (e) { return e && e.s !== symbol; })
        ).slice(0, 8);
        localStorage.setItem(key, JSON.stringify(list));
      } catch (e) { /* private mode */ }
    };
    window.RS.getRecent = function () {
      try { return JSON.parse(localStorage.getItem('recentSymbols') || '[]'); }
      catch (e) { return []; }
    };
  });
        actives.slice(1).forEach(function (link) { link.classList.remove('active'); });
      }
    })();

    // grouped dropdown menus: click toggles, Esc / outside-click closes
    function closeGroups(except) {
      document.querySelectorAll('.nav-group.open').forEach(function (group) {
        if (group !== except) {
          group.classList.remove('open');
          var btn = group.querySelector('.nav-drop');
          if (btn) btn.setAttribute('aria-expanded', 'false');
        }
      });
    }
    document.querySelectorAll('.nav-drop').forEach(function (button) {
      button.addEventListener('click', function (event) {
        event.stopPropagation();
        var group = button.closest('.nav-group');
        var willOpen = !group.classList.contains('open');
        closeGroups(group);
        group.classList.toggle('open', willOpen);
        button.setAttribute('aria-expanded', String(willOpen));
      });
    });
    document.addEventListener('click', function () { closeGroups(null); });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') closeGroups(null);
    });

    // propagate the active page up to its group button
    document.querySelectorAll('.nav-menu a.active').forEach(function (link) {
      var button = link.closest('.nav-group')?.querySelector('.nav-drop');
      if (button) button.classList.add('active');
    });

    // breadcrumb eyebrow derived from the nav structure (no per-page edits)
    (function () {
      var active = document.querySelector('.nav-menu a.active') || document.querySelector('.nav > a.active');
      if (!active) return;
      var page = active.textContent.trim();
      var groupEl = active.closest('.nav-group')?.querySelector('.nav-drop');
      var crumb = groupEl ? groupEl.textContent.replace(/▾/g, '').trim() + '  ·  ' + page : page;
      var header = document.querySelector('.page-header');
      var p = document.createElement('p');
      p.className = 'crumbs';
      p.textContent = crumb;
      if (header) header.insertBefore(p, header.firstChild);
    })();
  });

  window.RS = window.RS || {};

  window.RS.escapeHtml = function (value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  };

  window.RS.fmtNum = function (value, digits) {
    if (value == null || isNaN(Number(value))) return '−';
    return Number(value).toLocaleString(undefined, {
      minimumFractionDigits: digits == null ? 2 : digits,
      maximumFractionDigits: digits == null ? 2 : digits,
    });
  };

  window.RS.fmtPct = function (value) {
    if (value == null || isNaN(Number(value))) return '−';
    var n = Number(value);
    return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
  };

  window.RS.toast = function (message, kind) {
    var stack = document.querySelector('.toast-stack');
    if (!stack) return;
    var toast = document.createElement('div');
    toast.className = 'toast' + (kind ? ' toast-' + kind : '');
    toast.textContent = message;
    toast.setAttribute('role', 'status');
    stack.appendChild(toast);
    requestAnimationFrame(function () { toast.classList.add('show'); });
    setTimeout(function () {
      toast.classList.remove('show');
      setTimeout(function () { toast.remove(); }, 300);
    }, 3200);
  };

  window.RS.applyTheme = applyTheme;
  window.RS.getPreferredTheme = getPreferredTheme;

  /**
   * Symbol autocomplete: debounced /api/search -> <datalist> options.
   * @param {string} inputId id of the text input
   * @param {string} [listId] id of the <datalist>; created when omitted
   */
  window.RS.attachSymbolAutocomplete = function (inputId, listId) {
    var input = document.getElementById(inputId);
    if (!input) return;
    var list = listId ? document.getElementById(listId) : null;
    if (!list) {
      list = document.createElement('datalist');
      list.id = inputId + 'List';
      input.setAttribute('list', list.id);
      input.parentNode.appendChild(list);
    }
    var timer = null;
    input.addEventListener('input', function () {
      var term = input.value.trim();
      clearTimeout(timer);
      if (term.length < 2) { list.innerHTML = ''; return; }
      timer = setTimeout(function () {
        fetch('/api/search?q=' + encodeURIComponent(term) + '&limit=8')
          .then(function (res) { return res.ok ? res.json() : { results: [] }; })
          .then(function (data) {
            list.innerHTML = (data.results || []).map(function (r) {
              var sym = String(r.symbol || '').toUpperCase().replace(/\.NS$|\.BO$/, '');
              var name = r.name || r.company || '';
              return '<option value="' + window.RS.escapeHtml(sym) + '">' + window.RS.escapeHtml(name) + '</option>';
            }).join('');
          })
          .catch(function () { /* suggestions are best-effort */ });
      }, 250);
    });
  };
})();
