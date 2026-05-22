/* chat-assistant.js — TickerWave Planning Assistant.
 *
 * Dual-mode design:
 *
 *  Mode A — inline card (preserved):
 *    If the page contains <section id="ae-chat-host" data-ticker="X"></section>,
 *    render the legacy inline Q&A card into that host. Used by stock.html.
 *
 *  Mode B — global floating launcher (new):
 *    Otherwise, mount a 44×44 floating button bottom-right. Click to open
 *    a slide-in side panel: thread switcher, multi-turn message stream,
 *    profile setup mini-card, mic, citation chips, suggested follow-ups.
 *
 * Auth: JWT from localStorage('tw_token' || 'auth_token').
 * SSE: POST /api/chat — events: meta, delta, title, suggestions, done.
 * Threads: GET/POST/DELETE /api/chat/threads — multi-turn persistence.
 * Profile: GET/PUT /api/user/profile — risk, horizon, sectors, goals.
 *
 * The launcher is auto-mounted by bootstrap.js but is user-triggered to open
 * (single click on the button). No auto-displayed overlay panel — honors the
 * project's no-overlay-popups rule. The launcher button is whitelisted in
 * bootstrap.js's popup-killer KEEP_IDS.
 */
(function () {
  'use strict';
  if (window.__aeChatMounted) return;
  window.__aeChatMounted = true;

  // ── Skip-list — landing / auth flows don't get the launcher ───────────
  function shouldSkipLauncher() {
    var p = (location.pathname || '').toLowerCase();
    return /\/(login|signup|onboarding|disclosures)\.html?$/.test(p)
        || /\/index\.html?$/.test(p)
        || p === '/' || p === '/app/' || p === '/app';
  }

  // ── Utilities ─────────────────────────────────────────────────────────
  function getToken() {
    return localStorage.getItem('tw_token')
        || localStorage.getItem('auth_token') || '';
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;',
               '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function pageTicker() {
    var el = document.querySelector('section[data-ticker]');
    if (el && el.dataset && el.dataset.ticker) return el.dataset.ticker;
    var p = new URLSearchParams(location.search);
    return (p.get('ticker') || '').toUpperCase() || '';
  }

  // ── Minimal markdown renderer (escape-first, no script execution) ─────
  //   **bold**, *italic*, `code`, - list items, blank line → paragraph,
  //   [source:id] → clickable chip
  function renderMarkdown(raw) {
    if (!raw) return '';
    var html = esc(raw);
    // citation chips: [source:id]
    html = html.replace(/\[([a-z_]{2,16}):(\d+)\]/gi,
      function (_m, src, id) {
        var href = '#';
        if (src.toLowerCase() === 'event') {
          href = 'stock.html';  // event:N — no direct link, fallback
        }
        return '<a class="ae-chat-chip" target="_blank" data-src="'
             + esc(src) + '" data-id="' + esc(id) + '" href="' + href
             + '">[' + esc(src) + ':' + esc(id) + ']</a>';
      });
    // code: `x`
    html = html.replace(/`([^`]+)`/g,
      function (_m, c) { return '<code>' + c + '</code>'; });
    // bold: **x**
    html = html.replace(/\*\*([^*]+)\*\*/g,
      function (_m, c) { return '<strong>' + c + '</strong>'; });
    // italic: *x* (avoid clashing with bold by running after)
    html = html.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g,
      function (_m, pre, c) { return pre + '<em>' + c + '</em>'; });
    // bulleted lists — collapse contiguous "- " lines into <ul>
    html = html.replace(/(?:^|\n)((?:- [^\n]+\n?)+)/g, function (_m, blk) {
      var items = blk.split(/\n/).filter(function (l) { return l.indexOf('- ') === 0; })
        .map(function (l) { return '<li>' + l.slice(2) + '</li>'; }).join('');
      return '\n<ul>' + items + '</ul>';
    });
    // Paragraphs from blank-line separation
    html = html.split(/\n{2,}/).map(function (chunk) {
      if (/^\s*<(ul|ol|pre|h\d)/.test(chunk)) return chunk;
      return '<p>' + chunk.replace(/\n/g, '<br>') + '</p>';
    }).join('');
    return html;
  }

  // ── SSE reader — yields parsed event objects from a streaming response ─
  function readSSE(resp, onEvent) {
    var reader = resp.body.getReader();
    var dec = new TextDecoder();
    var buf = '';
    return (function pump() {
      return reader.read().then(function (r) {
        if (r.done) return;
        buf += dec.decode(r.value, { stream: true });
        var parts = buf.split(/\n\n/);
        buf = parts.pop();
        for (var i = 0; i < parts.length; i++) {
          var m = /^data:\s*(.+)$/m.exec(parts[i]);
          if (!m) continue;
          try {
            var ev = JSON.parse(m[1]);
            onEvent(ev);
          } catch (_) {}
        }
        return pump();
      });
    })();
  }

  // ──────────────────────────────────────────────────────────────────────
  //  Mode A — legacy inline card (preserved for stock.html)
  // ──────────────────────────────────────────────────────────────────────
  function renderInline(host) {
    var ticker = host.dataset.ticker || '';
    var threadKey = ticker ? ('tw_chat_thread_' + ticker) : 'tw_chat_thread_global';
    var threadId = localStorage.getItem(threadKey) || null;

    host.innerHTML =
      '<ae-card title="Ask AI about ' + esc(ticker || 'the market') + '" style="margin-top:20px;">' +
        '<div id="ae-chat-quota" style="font-size:11px;color:var(--text-tertiary);margin-bottom:10px;">Loading quota…</div>' +
        '<div style="font-size:11px;color:var(--text-tertiary);margin-bottom:8px;padding:6px 10px;background:var(--surface-2);border-radius:6px;">' +
          'Analysis only — not investment advice. NSE/BSE focus.' +
        '</div>' +
        '<textarea id="ae-chat-input" rows="2" maxlength="800" ' +
          'placeholder="' + esc(ticker ? ('Why is ' + ticker + ' moving today?') : 'Ask about any NSE/BSE stock…') + '" ' +
          'style="width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--border-strong);background:var(--surface-1);color:var(--text-primary);font-family:inherit;font-size:14px;resize:vertical;min-height:60px;"></textarea>' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:10px;">' +
          '<span style="font-size:11px;color:var(--text-tertiary);">Grounded in scraped events + filings. Cites sources.</span>' +
          '<button id="ae-chat-send" class="ae-btn" data-variant="primary" data-size="sm">Ask</button>' +
        '</div>' +
        '<div id="ae-chat-output" style="margin-top:16px;display:none;">' +
          '<div id="ae-chat-meta" style="font-size:11px;color:var(--text-tertiary);margin-bottom:8px;"></div>' +
          '<div id="ae-chat-answer" style="font-size:14px;line-height:1.6;color:var(--text-primary);padding:12px 14px;background:var(--surface-2);border-radius:10px;"></div>' +
        '</div>' +
      '</ae-card>';

    var input = host.querySelector('#ae-chat-input');
    var send = host.querySelector('#ae-chat-send');
    var out = host.querySelector('#ae-chat-output');
    var meta = host.querySelector('#ae-chat-meta');
    var answer = host.querySelector('#ae-chat-answer');
    var quotaEl = host.querySelector('#ae-chat-quota');

    fetch('/api/chat/quota', { headers: { Authorization: 'Bearer ' + getToken() }})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j || !j.success) {
          quotaEl.innerHTML = 'Sign in to ask. <a href="login.html" style="color:var(--link);">Sign in →</a>';
          send.setAttribute('disabled', '');
          return;
        }
        quotaEl.textContent = j.remaining + '/' + j.limit + ' questions remaining today (' + j.tier + ')';
        if (j.remaining <= 0) {
          send.setAttribute('disabled', '');
          if (j.tier !== 'pro') {
            quotaEl.innerHTML += ' · <a href="pricing.html" style="color:var(--link);">Upgrade →</a>';
          }
        }
      })
      .catch(function () { quotaEl.textContent = 'Quota lookup failed — try anyway.'; });

    function ask() {
      var q = input.value.trim();
      if (!q) return;
      var tok = getToken();
      if (!tok) {
        quotaEl.innerHTML = 'Sign in first. <a href="login.html" style="color:var(--link);">Sign in →</a>';
        return;
      }
      send.setAttribute('disabled', '');
      out.style.display = '';
      meta.textContent = 'Thinking…';
      answer.textContent = '';
      var streamed = '';
      var cites = [];

      fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + tok },
        body: JSON.stringify({ q: q, ticker: ticker || undefined, thread_id: threadId || undefined }),
      }).then(function (resp) {
        if (!resp.ok) {
          if (resp.status === 429) { meta.textContent = 'Daily limit reached.'; return; }
          if (resp.status === 401) { meta.textContent = 'Authentication failed.'; return; }
          meta.textContent = 'Error: HTTP ' + resp.status;
          return;
        }
        return readSSE(resp, function (ev) {
          if (ev.type === 'meta') {
            cites = ev.chunks || [];
            if (ev.thread_id) {
              threadId = ev.thread_id;
              try { localStorage.setItem(threadKey, String(threadId)); } catch (_) {}
            }
            meta.textContent = cites.length
              ? ('Grounded in ' + cites.length + ' sources · ' + ev.remaining_today + ' questions left')
              : 'No relevant context found — answer may be limited.';
          } else if (ev.type === 'delta') {
            streamed += ev.t;
            answer.innerHTML = renderMarkdown(streamed);
          } else if (ev.type === 'done') {
            if (cites.length) {
              var list = cites.map(function (c) { return '[' + c.source + ':' + c.id + ']'; }).join(' ');
              answer.innerHTML += '<div style="margin-top:8px;font-size:11px;color:var(--text-tertiary);">Sources: ' + esc(list) + '</div>';
            }
          }
        });
      }).catch(function (e) {
        meta.textContent = 'Network error: ' + e.message;
      }).finally(function () {
        send.removeAttribute('disabled');
      });
    }

    send.addEventListener('click', ask);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) ask();
    });
  }

  // ──────────────────────────────────────────────────────────────────────
  //  Mode B — global floating launcher + slide-in side panel
  // ──────────────────────────────────────────────────────────────────────
  function mountLauncher() {
    if (shouldSkipLauncher()) return;
    if (document.getElementById('ae-chat-launcher')) return;

    // Launcher button (single 44×44 pinned bottom-right)
    var launcher = document.createElement('button');
    launcher.id = 'ae-chat-launcher';
    launcher.type = 'button';
    launcher.setAttribute('aria-label', 'Open Planning Assistant');
    launcher.innerHTML =
      '<span class="material-symbols-outlined" aria-hidden="true">smart_toy</span>';
    document.body.appendChild(launcher);

    // Side panel (hidden until launcher clicked)
    var panel = document.createElement('aside');
    panel.id = 'ae-chat-panel';
    panel.setAttribute('aria-hidden', 'true');
    panel.innerHTML =
      '<header class="ae-chat-h">' +
        '<button id="ae-chat-threads-toggle" type="button" class="ae-chat-icon" aria-label="Threads"><span class="material-symbols-outlined">menu</span></button>' +
        '<h3>Planning Assistant</h3>' +
        '<button id="ae-chat-new" type="button" class="ae-chat-icon" aria-label="New chat"><span class="material-symbols-outlined">add</span></button>' +
        '<button id="ae-chat-close" type="button" class="ae-chat-icon" aria-label="Close"><span class="material-symbols-outlined">close</span></button>' +
      '</header>' +
      '<div id="ae-chat-threads-list" hidden></div>' +
      '<div id="ae-chat-disclaimer">Analysis only — not investment advice. NSE/BSE focus. Refuses buy/sell calls.</div>' +
      '<div id="ae-chat-profile-card" hidden></div>' +
      '<div id="ae-chat-quota-strip"></div>' +
      '<div id="ae-chat-messages" aria-live="polite"></div>' +
      '<div id="ae-chat-suggestions"></div>' +
      '<div id="ae-chat-input-row">' +
        '<textarea id="ae-chat-input" rows="2" maxlength="800" placeholder="Ask about NSE/BSE stocks, earnings, events…"></textarea>' +
        '<div class="ae-chat-input-actions">' +
          '<button id="ae-chat-mic" type="button" class="ae-chat-icon" aria-label="Voice input" hidden><span class="material-symbols-outlined">mic</span></button>' +
          '<button id="ae-chat-send" type="button" class="ae-chat-send-btn" aria-label="Send"><span class="material-symbols-outlined">arrow_upward</span></button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(panel);

    var $ = panel.querySelector.bind(panel);
    var threadsList = $('#ae-chat-threads-list');
    var threadsToggle = $('#ae-chat-threads-toggle');
    var newBtn = $('#ae-chat-new');
    var closeBtn = $('#ae-chat-close');
    var profileCard = $('#ae-chat-profile-card');
    var quotaStrip = $('#ae-chat-quota-strip');
    var messagesEl = $('#ae-chat-messages');
    var sugEl = $('#ae-chat-suggestions');
    var input = $('#ae-chat-input');
    var sendBtn = $('#ae-chat-send');
    var micBtn = $('#ae-chat-mic');

    // State
    var state = {
      activeThreadId: null,
      threads: [],
      quota: null,
      hasProfile: null,
      streaming: false,
    };

    // Show empty state
    function renderEmptyState() {
      var t = pageTicker();
      var greeting = t
        ? 'Ask about <strong>' + esc(t) + '</strong> — catalysts, earnings, filings, risks, regime fit.'
        : 'Ask about any NSE/BSE stock, sector, earnings event, or your watchlist.';
      messagesEl.innerHTML =
        '<div class="ae-chat-empty">' +
          '<div class="ae-chat-empty-icon"><span class="material-symbols-outlined">show_chart</span></div>' +
          '<p>' + greeting + '</p>' +
          '<p class="ae-chat-empty-sub">I cite every claim. Buy/sell recommendations are out of scope.</p>' +
        '</div>';
    }

    function appendMessage(role, text, opts) {
      opts = opts || {};
      var wrap = document.createElement('div');
      wrap.className = 'ae-chat-msg ae-chat-msg-' + role;
      wrap.innerHTML =
        '<div class="ae-chat-msg-role">' + (role === 'user' ? 'You' : 'Assistant') + '</div>' +
        '<div class="ae-chat-msg-body">' + (opts.raw ? text : renderMarkdown(text)) + '</div>';
      messagesEl.appendChild(wrap);
      messagesEl.scrollTop = messagesEl.scrollHeight;
      return wrap.querySelector('.ae-chat-msg-body');
    }

    function renderQuota() {
      if (!state.quota) { quotaStrip.textContent = ''; return; }
      var q = state.quota;
      if (!q.success) {
        quotaStrip.innerHTML = 'Sign in to chat — <a href="login.html">Sign in →</a>';
        sendBtn.setAttribute('disabled', '');
        return;
      }
      quotaStrip.textContent = q.remaining + '/' + q.limit + ' questions left today (' + q.tier + ')';
      if (q.remaining <= 0) {
        sendBtn.setAttribute('disabled', '');
        if (q.tier !== 'pro') {
          quotaStrip.innerHTML += ' · <a href="pricing.html">Upgrade →</a>';
        }
      } else {
        sendBtn.removeAttribute('disabled');
      }
    }

    function renderThreads() {
      if (!state.threads.length) {
        threadsList.innerHTML = '<div class="ae-chat-threads-empty">No previous chats.</div>';
        return;
      }
      threadsList.innerHTML = state.threads.map(function (t) {
        var active = state.activeThreadId === t.id ? ' active' : '';
        return '<div class="ae-chat-thread' + active + '" data-tid="' + t.id + '">' +
          '<button class="ae-chat-thread-pick" type="button">' + esc(t.title || 'New chat') + '</button>' +
          '<button class="ae-chat-thread-del" type="button" aria-label="Delete"><span class="material-symbols-outlined">delete</span></button>' +
        '</div>';
      }).join('');
    }

    threadsList.addEventListener('click', function (e) {
      var row = e.target.closest('.ae-chat-thread');
      if (!row) return;
      var tid = parseInt(row.dataset.tid, 10);
      if (!tid) return;
      if (e.target.closest('.ae-chat-thread-del')) {
        if (!confirm('Delete this thread?')) return;
        fetch('/api/chat/threads/' + tid, {
          method: 'DELETE',
          headers: { Authorization: 'Bearer ' + getToken() },
        }).then(function () {
          if (state.activeThreadId === tid) {
            state.activeThreadId = null;
            try { localStorage.removeItem('tw_chat_active_thread'); } catch (_) {}
            renderEmptyState();
          }
          loadThreads();
        });
        return;
      }
      loadThread(tid);
      threadsList.hidden = true;
    });

    threadsToggle.addEventListener('click', function () {
      threadsList.hidden = !threadsList.hidden;
    });

    newBtn.addEventListener('click', function () {
      state.activeThreadId = null;
      try { localStorage.removeItem('tw_chat_active_thread'); } catch (_) {}
      renderEmptyState();
      sugEl.innerHTML = '';
      input.value = '';
      input.focus();
    });

    closeBtn.addEventListener('click', function () {
      panel.classList.remove('open');
      panel.setAttribute('aria-hidden', 'true');
      launcher.style.display = '';
    });

    launcher.addEventListener('click', function () {
      panel.classList.add('open');
      panel.setAttribute('aria-hidden', 'false');
      launcher.style.display = 'none';
      ensureBootstrapped();
      setTimeout(function () { input.focus(); }, 250);
    });

    var bootstrapped = false;
    function ensureBootstrapped() {
      if (bootstrapped) return;
      bootstrapped = true;
      loadQuota();
      loadThreads();
      loadProfile();
      // Restore last active thread
      try {
        var saved = localStorage.getItem('tw_chat_active_thread');
        if (saved) loadThread(parseInt(saved, 10));
        else renderEmptyState();
      } catch (_) { renderEmptyState(); }
    }

    function loadQuota() {
      fetch('/api/chat/quota', { headers: { Authorization: 'Bearer ' + getToken() }})
        .then(function (r) { return r.ok ? r.json() : { success: false }; })
        .then(function (j) { state.quota = j; renderQuota(); })
        .catch(function () { state.quota = { success: false }; renderQuota(); });
    }

    function loadThreads() {
      var tok = getToken();
      if (!tok) { state.threads = []; renderThreads(); return; }
      fetch('/api/chat/threads', { headers: { Authorization: 'Bearer ' + tok }})
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) {
          state.threads = (j && j.threads) || [];
          renderThreads();
        })
        .catch(function () {});
    }

    function loadThread(tid) {
      var tok = getToken();
      if (!tok || !tid) return;
      fetch('/api/chat/threads/' + tid, { headers: { Authorization: 'Bearer ' + tok }})
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) {
          if (!j || !j.success) return;
          state.activeThreadId = tid;
          try { localStorage.setItem('tw_chat_active_thread', String(tid)); } catch (_) {}
          messagesEl.innerHTML = '';
          (j.messages || []).forEach(function (m) {
            appendMessage(m.role, m.content || '');
          });
          if (!j.messages || !j.messages.length) renderEmptyState();
          renderThreads();
          sugEl.innerHTML = '';
        });
    }

    function loadProfile() {
      var tok = getToken();
      if (!tok) { state.hasProfile = false; return; }
      fetch('/api/user/profile', { headers: { Authorization: 'Bearer ' + tok }})
        .then(function (r) {
          if (r.status === 404) { state.hasProfile = false; showProfileCard(); return null; }
          return r.ok ? r.json() : null;
        })
        .then(function (j) {
          if (j && j.success) state.hasProfile = true;
        })
        .catch(function () {});
    }

    function showProfileCard() {
      profileCard.hidden = false;
      profileCard.innerHTML =
        '<div class="ae-chat-profile-title">Tell me about yourself</div>' +
        '<div class="ae-chat-profile-sub">One-time setup. Helps me frame analysis for you. Skippable.</div>' +
        '<label>Risk tolerance' +
          '<select id="ae-pf-risk">' +
            '<option value="">—</option>' +
            '<option value="low">Low — capital preservation</option>' +
            '<option value="med">Medium — balanced</option>' +
            '<option value="high">High — aggressive</option>' +
          '</select>' +
        '</label>' +
        '<label>Horizon' +
          '<select id="ae-pf-horizon">' +
            '<option value="">—</option>' +
            '<option value="intraday">Intraday</option>' +
            '<option value="swing">Swing (days–weeks)</option>' +
            '<option value="short">Short (1–3 months)</option>' +
            '<option value="medium">Medium (3–12 months)</option>' +
            '<option value="long">Long (1y+)</option>' +
          '</select>' +
        '</label>' +
        '<label>Preferred sectors (comma-separated)' +
          '<input id="ae-pf-sectors" type="text" placeholder="banks, IT, FMCG, auto"></label>' +
        '<label>Goals (optional, ≤500 chars)' +
          '<textarea id="ae-pf-goals" rows="2" maxlength="500"></textarea>' +
        '</label>' +
        '<div class="ae-chat-profile-actions">' +
          '<button id="ae-pf-skip" type="button">Skip</button>' +
          '<button id="ae-pf-save" type="button">Save</button>' +
        '</div>';
      profileCard.querySelector('#ae-pf-skip').addEventListener('click', function () {
        profileCard.hidden = true;
      });
      profileCard.querySelector('#ae-pf-save').addEventListener('click', function () {
        var sectors = (profileCard.querySelector('#ae-pf-sectors').value || '')
          .split(',').map(function (s) { return s.trim(); }).filter(Boolean).slice(0, 10);
        var body = {
          risk_profile: profileCard.querySelector('#ae-pf-risk').value || null,
          horizon: profileCard.querySelector('#ae-pf-horizon').value || null,
          sectors: sectors,
          goals_text: profileCard.querySelector('#ae-pf-goals').value || '',
        };
        fetch('/api/user/profile', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + getToken() },
          body: JSON.stringify(body),
        }).then(function (r) { return r.json(); })
          .then(function (j) {
            if (j && j.success) {
              state.hasProfile = true;
              profileCard.hidden = true;
            } else {
              alert('Save failed: ' + (j && j.error || 'unknown'));
            }
          });
      });
    }

    // ── Send + SSE stream ───────────────────────────────────────────────
    function send() {
      if (state.streaming) return;
      var q = input.value.trim();
      if (!q) return;
      var tok = getToken();
      if (!tok) {
        quotaStrip.innerHTML = 'Sign in to chat — <a href="login.html">Sign in →</a>';
        return;
      }
      state.streaming = true;
      sendBtn.setAttribute('disabled', '');
      input.value = '';

      // Append user message
      appendMessage('user', q);

      // Add assistant placeholder
      var asstBody = appendMessage('assistant', '<span class="ae-chat-thinking">Thinking…</span>',
                                   { raw: true });
      sugEl.innerHTML = '';

      var streamed = '';
      var cites = [];
      var ticker = pageTicker();

      fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + tok },
        body: JSON.stringify({
          q: q,
          ticker: ticker || undefined,
          thread_id: state.activeThreadId || undefined,
        }),
      }).then(function (resp) {
        if (!resp.ok) {
          if (resp.status === 429) {
            asstBody.innerHTML = '<em>Daily limit reached. <a href="pricing.html">Upgrade →</a></em>';
            loadQuota();
            return;
          }
          if (resp.status === 401) {
            asstBody.innerHTML = '<em>Authentication failed. Please sign in again.</em>';
            return;
          }
          asstBody.innerHTML = '<em>Error: HTTP ' + resp.status + '</em>';
          return;
        }
        return readSSE(resp, function (ev) {
          if (ev.type === 'meta') {
            cites = ev.chunks || [];
            if (ev.thread_id) {
              state.activeThreadId = ev.thread_id;
              try { localStorage.setItem('tw_chat_active_thread', String(ev.thread_id)); } catch (_) {}
            }
            if (state.quota && state.quota.success) {
              state.quota.remaining = ev.remaining_today;
              renderQuota();
            }
            asstBody.innerHTML = '<span class="ae-chat-thinking">Searching context (' + cites.length + ' chunks)…</span>';
          } else if (ev.type === 'delta') {
            streamed += ev.t;
            asstBody.innerHTML = renderMarkdown(streamed);
            messagesEl.scrollTop = messagesEl.scrollHeight;
          } else if (ev.type === 'title') {
            // New thread title — refresh thread list
            loadThreads();
          } else if (ev.type === 'suggestions') {
            renderSuggestions(ev.items || []);
          } else if (ev.type === 'done') {
            if (cites.length) {
              asstBody.innerHTML += '<div class="ae-chat-sources">Sources: ' +
                cites.map(function (c) { return '<a class="ae-chat-chip" data-src="' + esc(c.source) + '" data-id="' + esc(c.id) + '">[' + esc(c.source) + ':' + esc(c.id) + ']</a>'; }).join(' ') +
                '</div>';
            }
          }
        });
      }).catch(function (e) {
        asstBody.innerHTML = '<em>Network error: ' + esc(e.message) + '</em>';
      }).finally(function () {
        state.streaming = false;
        sendBtn.removeAttribute('disabled');
      });
    }

    function renderSuggestions(items) {
      if (!items || !items.length) { sugEl.innerHTML = ''; return; }
      sugEl.innerHTML = '<div class="ae-chat-sug-label">Follow-ups</div>' +
        items.map(function (s) {
          return '<button class="ae-chat-sug" type="button">' + esc(s) + '</button>';
        }).join('');
      sugEl.querySelectorAll('.ae-chat-sug').forEach(function (btn) {
        btn.addEventListener('click', function () {
          input.value = btn.textContent;
          input.focus();
        });
      });
    }

    sendBtn.addEventListener('click', send);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });

    // ── Voice input (Web Speech API; graceful no-op if unavailable) ─────
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SR) {
      micBtn.hidden = false;
      var recog = new SR();
      recog.lang = 'en-IN';
      recog.continuous = false;
      recog.interimResults = false;
      var listening = false;
      recog.addEventListener('result', function (e) {
        var text = '';
        for (var i = 0; i < e.results.length; i++) {
          text += e.results[i][0].transcript;
        }
        input.value = (input.value ? input.value + ' ' : '') + text.trim();
        input.focus();
      });
      recog.addEventListener('end', function () {
        listening = false;
        micBtn.removeAttribute('data-listening');
      });
      micBtn.addEventListener('click', function () {
        if (listening) { try { recog.stop(); } catch (_) {} return; }
        try {
          listening = true;
          micBtn.setAttribute('data-listening', '');
          recog.start();
        } catch (_) { listening = false; micBtn.removeAttribute('data-listening'); }
      });
    }

    // ESC closes the panel
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && panel.classList.contains('open')) {
        closeBtn.click();
      }
    });
  }

  // ──────────────────────────────────────────────────────────────────────
  //  Init
  // ──────────────────────────────────────────────────────────────────────
  function init() {
    var hosts = document.querySelectorAll('#ae-chat-host, [data-ae-chat]');
    if (hosts.length) {
      hosts.forEach(renderInline);
      // Inline mode also gets the global launcher unless skipped
      mountLauncher();
    } else {
      mountLauncher();
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
