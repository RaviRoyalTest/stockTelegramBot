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

    var navToggle = document.getElementById('navToggle');
    if (navToggle) {
      navToggle.addEventListener('click', function () {
        var nav = document.querySelector('.nav');
        if (!nav) return;
        var open = nav.classList.toggle('open');
        navToggle.setAttribute('aria-expanded', String(open));
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
        });
        actives.slice(1).forEach(function (link) { link.classList.remove('active'); });
      }
    })();

    // grouped dropdown menus: click toggles, Esc / outside-click closes,
    // desktop hover opens them too, arrow keys walk the menu items
    function closeGroups(except) {
      document.querySelectorAll('.nav-group.open').forEach(function (group) {
        if (group !== except) {
          group.classList.remove('open');
          var btn = group.querySelector('.nav-drop');
          if (btn) btn.setAttribute('aria-expanded', 'false');
        }
      });
    }
    document.querySelectorAll('.nav-group').forEach(function (group) {
      var button = group.querySelector('.nav-drop');
      if (!button) return;
      var menu = group.querySelector('.nav-menu');
      var hoverTimer = null;
      var canHover = window.matchMedia('(hover: hover) and (min-width: 1181px)');

      button.addEventListener('click', function (event) {
        event.stopPropagation();
        var willOpen = !group.classList.contains('open');
        closeGroups(group);
        group.classList.toggle('open', willOpen);
        button.setAttribute('aria-expanded', String(willOpen));
      });

      // keyboard: open with Enter/Space/ArrowDown, walk items, Esc closes
      button.addEventListener('keydown', function (event) {
        if (event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          if (!group.classList.contains('open')) {
            closeGroups(group);
            group.classList.add('open');
            button.setAttribute('aria-expanded', 'true');
          }
          var first = menu && menu.querySelector('a');
          if (first) first.focus();
        }
      });
      if (menu) {
        menu.addEventListener('keydown', function (event) {
          var items = Array.prototype.slice.call(menu.querySelectorAll('a'));
          var idx = items.indexOf(document.activeElement);
          if (event.key === 'ArrowDown') {
            event.preventDefault();
            (items[idx + 1] || items[0]).focus();
          } else if (event.key === 'ArrowUp') {
            event.preventDefault();
            (items[idx - 1] || items[items.length - 1]).focus();
          } else if (event.key === 'Escape') {
            closeGroups(null);
            button.focus();
          }
        });
      }

      // hover-to-open on pointer devices only (touch + small screens keep click)
      group.addEventListener('mouseenter', function () {
        if (!canHover.matches) return;
        clearTimeout(hoverTimer);
        closeGroups(group);
        group.classList.add('open');
        button.setAttribute('aria-expanded', 'true');
      });
      group.addEventListener('mouseleave', function () {
        if (!canHover.matches) return;
        hoverTimer = setTimeout(function () {
          group.classList.remove('open');
          button.setAttribute('aria-expanded', 'false');
        }, 180);
      });
    });
    document.addEventListener('click', function () { closeGroups(null); });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') closeGroups(null);
    });

    // close any open dropdown when a menu item is chosen (mobile flows through here)
    document.querySelectorAll('.nav-menu a').forEach(function (link) {
      link.addEventListener('click', function () { closeGroups(null); });
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
      var strong = active.querySelector('strong');
      var page = (strong ? strong.textContent : active.textContent).trim();
      var groupEl = active.closest('.nav-group')?.querySelector('.nav-drop');
      var crumb = groupEl ? groupEl.textContent.replace(/▾/g, '').trim() + '  ·  ' + page : page;
      var header = document.querySelector('.page-header');
      var p = document.createElement('p');
      p.className = 'crumbs';
      p.textContent = crumb;
      if (header) header.insertBefore(p, header.firstChild);
    })();

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
    })();

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

  window.RS.recLabel = function (key) {
    // 'strong_buy' -> 'Strong Buy' (Yahoo recommendationKey snake_case)
    return String(key == null ? '' : key)
      .replace(/_/g, ' ')
      .replace(/\w\S*/g, function (w) { return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase(); });
  };

  // '2026-09-04' -> '04 Sep 2026' (returns the input unchanged if not ISO)
  window.RS.fmtDate = function (value) {
    var s = String(value || '');
    var m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return s;
    var months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return m[3] + ' ' + months[Number(m[2]) - 1] + ' ' + m[1];
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
   * Turn Telegram-formatted report lines into structured, readable HTML.
   *
   * The bot's formatters emit lines like:
   *   "FUNDAMENTAL SNAPSHOT"           -> section heading
   *   "Price: 1,234.5"                 -> definition row
   *   "• Value: 55.1"                  -> bullet
   *   "VERDICT: Buy"                   -> callout (verdict-ish)
   *   anything else                    -> standalone note line
   *
   * Inline <b>/<code> markup from the server is preserved as-is.
   * @param {string[]} lines
   * @returns {string} html
   */
  window.RS.renderReportLines = function (lines) {
    if (!Array.isArray(lines) || !lines.length) {
      return '<p class="muted">No data available.</p>';
    }
    var esc = window.RS.escapeHtml;
    var out = [];
    var rows = [];
    var bullets = [];
    var notes = [];

    var statuses = [];

    function flushRows() {
      if (!rows.length) return;
      out.push('<div class="report-rows">' + rows.join('') + '</div>');
      rows = [];
    }
    function flushBullets() {
      if (!bullets.length) return;
      out.push('<ul class="report-list">' + bullets.join('') + '</ul>');
      bullets = [];
    }
    function flushNotes() {
      if (!notes.length) return;
      out.push('<p class="report-note">' + notes.join(' ') + '</p>');
      notes = [];
    }
    function flushStatuses() {
      if (!statuses.length) return;
      out.push('<ul class="report-status">' + statuses.join('') + '</ul>');
      statuses = [];
    }
    function flushAll() { flushRows(); flushBullets(); flushStatuses(); flushNotes(); }

    var HEADING_RE = /^[A-Z0-9][A-Z0-9 &+\-\u2013\u2014()\u20b9%\.,'\/]{2,60}$/;
    var EMOJI_LEAD = /^[^A-Za-z0-9\s<]/;

    lines.forEach(function (raw) {
      var line = String(raw == null ? '' : raw).trim();
      if (!line) return;
      var plain = line.replace(/<[^>]+>/g, '').trim();

      // status items: ✅ pass, ❌ fail, ⚪/🟡 manual-review lines (checklists)
      // (the u flag matters: without it, astral emoji share surrogate code units
      //  and e.g. 💡 would match [🟢])
      // verdict lines ("🔴 Weak - only 47% of checked items passed.") look like
      // status items but are the final takeaway — they must reach the callout
      // branch below, so detect and skip them here.
      var isVerdict = /(Strong candidate|Decent|Weak)\s*-\s*(only\s*)?\d+% of checked/i.test(plain);
      if (!isVerdict &&
          (/^[✅🟢]/u.test(line) || /^[❌🔴]/u.test(line) || /^[⚪🟡]/u.test(line))) {
        flushRows(); flushBullets(); flushNotes();
        var stCls = /^[✅🟢]/u.test(line) ? 'st-ok' : /^[❌🔴]/u.test(line) ? 'st-bad' : 'st-man';
        statuses.push('<li class="' + stCls + '">' + line + '</li>');
        return;
      }

      // callouts: verdict / summary / explicit tips (keep raw markup)
      if (/^(VERDICT|OVERALL VIEW|Main Question|RESULT|SUMMARY|⚠️ WARNING|TOTAL:)/.test(plain) ||
          /^💡 .*(Tip|Note)/i.test(plain) ||
          /(candidate|Decent|Weak)\s*<b>-|<b>(Strong candidate|Decent|Weak)<\/b>/i.test(line) ||
          /(Strong candidate|Decent|Weak)\s*-\s*\d+% of checked/i.test(plain)) {
        flushAll();
        var cls = 'report-callout';
        if (/^(VERDICT|OVERALL VIEW)/i.test(line)) cls += ' report-callout-verdict';
        else if (/⚠️|WARNING|🔴/.test(line)) cls += ' report-callout-warn';
        else cls += ' report-callout-tip';
        out.push('<div class="' + cls + '">' + line + '</div>');
        return;
      }

      // bullets
      if (/^[•\u2022\u25aa\u25cf]\s*/.test(line)) {
        flushRows(); flushStatuses(); flushNotes();
        bullets.push('<li>' + line.replace(/^[•\u2022\u25aa\u25cf]\s*/, '') + '</li>');
        return;
      }

      // definition row: "Label: value"
      var m = line.match(/^([^:<]{2,42}):\s*(.+)$/);
      if (m) {
        flushBullets(); flushStatuses(); flushNotes();
        rows.push('<div class="report-row"><span class="report-k">' + m[1] + '</span><span class="report-v">' + m[2] + '</span></div>');
        return;
      }

      // section heading: SHORT UPPERCASE line, or a short emoji-led line ("💡 What it means")
      var stripped = plain.replace(/^[^A-Za-z0-9]+/, '');
      var capsHeading = HEADING_RE.test(stripped) && !/:$/.test(stripped) && !/^END OF/i.test(stripped);
      var emojiHeading = EMOJI_LEAD.test(line) && plain.indexOf(':') < 0 && plain.length <= 52;
      if (capsHeading || emojiHeading) {
        flushAll();
        out.push('<h3 class="report-heading">' + line + '</h3>');
        return;
      }

      // subtitle: longer emoji-led line without a colon ("🔌 RSI (14) — … Neutral 🟡")
      if (EMOJI_LEAD.test(line) && plain.indexOf(':') < 0 && plain.length <= 95) {
        flushAll();
        out.push('<p class="report-subtitle">' + line + '</p>');
        return;
      }

      notes.push('<span>' + line + '</span>');
    });
    flushAll();
    return out.join('') || '<p class="muted">No data available.</p>';
  };

  /**
   * Symbol autocomplete: debounced /api/search -> <datalist> options.
   * @param {string} inputId id of the text input
   * @param {string} [listId] id of the <datalist>; created when omitted
   */
  /**
   * Sortable data tables: wraps each column header in a click-to-sort button
   * with an arrow indicator and re-orders the table body client-side.
   *
   * @param {string} tableSel selector for the <table> (or its container)
   * @param {Object} [opts]
   *   skip: array of column indexes (0-based) to leave unsortable (e.g. Actions)
   *   default: column index to pre-sort descending (default: first numeric col)
   *   onSort: optional callback(rows) to re-run filters after sorting
   */
  window.RS.makeSortable = function (tableSel, opts) {
    opts = opts || {};
    var el = document.querySelector(tableSel);
    if (!el) return null;
    var table = el.tagName === 'TABLE' ? el : el.closest('table');
    var head = table && table.querySelector('thead');
    var body = table.querySelector('tbody');
    if (!head || !body) return;
    var headers = Array.prototype.slice.call(head.querySelectorAll('tr:first-child > th'));
    var skip = opts.skip || [];
    var sortState = { idx: -1, dir: 'desc' };

    function parseCell(text) {
      var t = String(text == null ? '' : text).trim();
      if (!t || t === '−' || t === '-') return null;
      // dd-Mon-yyyy (NSE style) and similar human dates sort chronologically
      var dm = t.match(/^(\d{1,2})[- ]([A-Za-z]{3,})[- ,]+(\d{4})$/);
      if (dm) {
        var dv = Date.parse(dm[2].slice(0, 3) + ' ' + dm[1] + ', ' + dm[3]);
        if (!isNaN(dv)) return dv;
      }
      var m = t.replace(/[,%₹$]/g, '').replace(/−/g, '-').replace(/Cr$|L$|K$/i, '').trim();
      var n = Number(m);
      if (isFinite(n)) return n;
      var d2 = Date.parse(t);            // ISO dates like 2026-09-04
      return !isNaN(d2) ? d2 : t.toLowerCase();
    }

    function compare(a, b) {
      var va = parseCell(a.cells[sortState.idx] && a.cells[sortState.idx].textContent);
      var vb = parseCell(b.cells[sortState.idx] && b.cells[sortState.idx].textContent);
      if (va == null && vb == null) return 0;
      if (va == null) return 1;   // blanks always sink to the bottom
      if (vb == null) return -1;
      if (typeof va === 'number' && typeof vb === 'number') return va - vb;
      return String(va).localeCompare(String(vb));
    }

    function paintArrows() {
      headers.forEach(function (th, i) {
        var btn = th.querySelector('button.sort');
        if (!btn) return;
        if (i === sortState.idx) {
          th.setAttribute('aria-sort', sortState.dir === 'asc' ? 'ascending' : 'descending');
          btn.setAttribute('data-arrow', sortState.dir === 'asc' ? '↑' : '↓');
        } else {
          th.removeAttribute('aria-sort');
          btn.removeAttribute('data-arrow');
        }
      });
    }

    function firstClickDir(idx) {
      // text columns sort A→Z first; numbers/dates sort largest/newest first
      var rows = Array.prototype.slice.call(body.querySelectorAll('tr'));
      for (var r = 0; r < rows.length; r++) {
        var cell = rows[r].cells[idx];
        if (!cell) continue;
        var v = parseCell(cell.textContent);
        if (v == null) continue;
        return typeof v === 'string' ? 'asc' : 'desc';
      }
      return 'desc';
    }

    function sortBy(idx, dir) {
      var rows = Array.prototype.slice.call(body.querySelectorAll('tr')).filter(function (tr) {
        return !tr.querySelector('td.empty');
      });
      if (!rows.length) return;
      sortState.idx = idx;
      sortState.dir = dir;
      rows.sort(function (a, b) { return dir === 'asc' ? compare(a, b) : compare(b, a); });
      rows.forEach(function (tr) { body.appendChild(tr); });
      paintArrows();
      if (typeof opts.onSort === 'function') opts.onSort();
    }

    headers.forEach(function (th, i) {
      if (skip.indexOf(i) !== -1) return;
      var label = th.textContent.trim();
      if (!label) return;
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'sort';
      btn.setAttribute('aria-label', 'Sort by ' + label);
      btn.textContent = label;
      btn.addEventListener('click', function () {
        var dir = sortState.idx === i
          ? (sortState.dir === 'asc' ? 'desc' : 'asc')   // same column: flip
          : firstClickDir(i);                            // new column: smart default
        sortBy(i, dir);
      });
      th.textContent = '';
      th.appendChild(btn);
    });

    if (opts.default != null) sortBy(opts.default, 'desc');
    paintArrows();
    return {
      resort: function () {
        if (sortState.idx < 0 && opts.default != null) { sortBy(opts.default, 'desc'); return; }
        if (sortState.idx >= 0) sortBy(sortState.idx, sortState.dir);
      },
      clear: function () { sortState.idx = -1; paintArrows(); }
    };
  };

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
