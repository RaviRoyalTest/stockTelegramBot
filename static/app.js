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
})();
