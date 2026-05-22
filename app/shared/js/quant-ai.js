/* quant-ai.js — AI Coach drawer for the Quant Playground.
 * The drawer markup + open/close is already in playground.html. This module:
 *   - captures the current tab's state (tool name + visible inputs/outputs)
 *   - sends a templated, guardrailed prompt to /api/chat or /api/agent
 *   - streams the reply back into the drawer, rendering KaTeX for $…$
 *
 * Endpoint resolution: tries /api/chat then /api/agent. If neither responds,
 * shows a graceful "AI coach offline — endpoint not configured yet" message.
 */
(function () {
  'use strict';

  var ENDPOINTS = ['/api/chat', '/api/agent', '/api/ai/quant'];
  var resolvedEndpoint = null;
  var endpointChecked = false;

  // Cache: tool+inputsHash → reply (10 min TTL)
  var cache = new Map();
  var CACHE_TTL = 10 * 60 * 1000;

  function ready() {
    var fab = document.getElementById('qpAiFab');
    var drawer = document.getElementById('qpAiDrawer');
    var body = document.getElementById('qpAiBody');
    var input = document.getElementById('qpAiInput');
    if (!fab || !drawer || !body || !input) return;

    input.disabled = false;
    input.placeholder = 'Ask about this tool…';
    body.innerHTML = welcomeMsg();

    fab.addEventListener('click', function () {
      setTimeout(function () { input.focus(); }, 250);
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        var q = input.value.trim();
        if (!q) return;
        input.value = '';
        ask(q);
      }
    });
  }

  function welcomeMsg() {
    return '<div style="padding:14px 12px;background:rgba(45,212,170,0.06);border-left:2px solid rgba(45,212,170,0.30);border-radius:3px;">'
      + '<div style="font:700 10px/1.4 Plus Jakarta Sans;letter-spacing:0.12em;text-transform:uppercase;color:#6ddec0;margin-bottom:8px;">Quant Coach</div>'
      + '<div style="font:500 12.5px/1.5 DM Sans;color:#dde3ef;">'
      + 'Ask anything about the current tool — formulas, intuition, when to use, what kills the strategy. '
      + 'Grounded in standard quant theory + NSE/BSE context. <strong style="color:#ffc85a;">No buy/sell advice.</strong>'
      + '</div>'
      + '<div style="margin-top:10px;font:500 11px/1.4 DM Sans;color:#8a94a8;">Try: "Why is theta higher near expiry?" or "What kills an iron condor?"</div>'
      + '</div>';
  }

  function currentTabContext() {
    var params = new URLSearchParams(location.search);
    var tab = params.get('tool') || 'greeks';
    var root = document.querySelector('.qp-pane.is-active');
    if (!root) return { tab: tab, snapshot: {} };
    // Grab labeled inputs + outputs into a flat object
    var snap = {};
    root.querySelectorAll('[data-field]').forEach(function (i) { snap['input_' + i.dataset.field] = i.value; });
    root.querySelectorAll('[data-out]').forEach(function (o) { snap['out_' + o.dataset.out] = o.textContent.trim(); });
    return { tab: tab, snapshot: snap };
  }

  function templatedPrompt(userQ, ctx) {
    var lines = [
      'You are a quant tutor inside the Tickwave Quant Playground.',
      'Your role: explain quant concepts in plain English with reference to NSE/BSE context.',
      'STRICT RULES:',
      '- Do NOT give buy/sell recommendations.',
      '- Do NOT predict prices or returns.',
      '- Do NOT name specific stocks as picks.',
      '- DO explain math, intuition, when a tool is appropriate, what assumptions break.',
      '- Use $...$ for inline math, $$...$$ for display math (KaTeX renders them).',
      '',
      'Current tool: ' + ctx.tab,
      'Current state (inputs/outputs visible to user):',
      JSON.stringify(ctx.snapshot, null, 2),
      '',
      'User question: ' + userQ,
    ];
    return lines.join('\n');
  }

  function hashKey(tab, snap, q) {
    return tab + '|' + JSON.stringify(snap) + '|' + q;
  }

  function ask(q) {
    var body = document.getElementById('qpAiBody');
    var ctx = currentTabContext();
    var cacheKey = hashKey(ctx.tab, ctx.snapshot, q);
    var cached = cache.get(cacheKey);
    if (cached && (Date.now() - cached.at) < CACHE_TTL) {
      renderTurn(body, q, cached.reply, true);
      return;
    }
    renderTurn(body, q, null, false);

    var prompt = templatedPrompt(q, ctx);
    sendToAI(prompt).then(function (reply) {
      cache.set(cacheKey, { reply: reply, at: Date.now() });
      // Cap cache
      if (cache.size > 50) { var first = cache.keys().next().value; cache.delete(first); }
      updateLastReply(body, reply);
    }).catch(function () {
      updateLastReply(body, '_AI coach is offline. The RAG agent endpoint is not configured yet — once `/api/chat` or `/api/agent` is wired up on the backend, this drawer will work._');
    });
  }

  function renderTurn(body, q, reply, fromCache) {
    var bubble = document.createElement('div');
    bubble.style.cssText = 'margin-top:14px;';
    bubble.innerHTML = ''
      + '<div style="font:600 12px/1.4 DM Sans;color:#dde3ef;background:rgba(142,180,224,0.08);padding:10px 12px;border-radius:6px 6px 6px 0;">'
      +   escapeHtml(q) + (fromCache ? '<span style="font-size:9px;color:#6a7484;margin-left:6px;">(cached)</span>' : '')
      + '</div>'
      + '<div data-ai-reply style="margin-top:8px;font:500 13px/1.6 DM Sans;color:#b0bccf;padding:10px 12px;background:rgba(45,212,170,0.04);border-left:2px solid rgba(45,212,170,0.30);border-radius:3px;">'
      +   (reply ? renderMarkdown(reply) : '<span style="color:#6a7484;font-style:italic;">Thinking…</span>')
      + '</div>';
    body.appendChild(bubble);
    body.scrollTop = body.scrollHeight;
    if (reply && window.katex && window.Glossary) Glossary.renderAll(bubble);
  }
  function updateLastReply(body, reply) {
    var slots = body.querySelectorAll('[data-ai-reply]');
    var last = slots[slots.length - 1];
    if (!last) return;
    last.innerHTML = renderMarkdown(reply);
    body.scrollTop = body.scrollHeight;
    if (window.katex && window.Glossary) Glossary.renderAll(last);
  }

  // Cheap markdown: bold, italic, inline+display math, paragraphs
  function renderMarkdown(s) {
    var out = escapeHtml(s);
    // display math $$...$$
    out = out.replace(/\$\$([^$]+)\$\$/g, function (_, m) { return '<span data-katex-display data-katex="' + escapeAttr(m) + '"></span>'; });
    // inline math $...$
    out = out.replace(/\$([^$\n]+)\$/g, function (_, m) { return '<span data-katex data-katex="' + escapeAttr(m) + '"></span>'; });
    // bold
    out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // italic _x_
    out = out.replace(/_([^_]+)_/g, '<em>$1</em>');
    // line breaks
    out = out.replace(/\n\n/g, '</p><p>');
    out = '<p style="margin:0 0 8px 0;">' + out + '</p>';
    return out;
  }

  function sendToAI(prompt) {
    return resolveEndpoint().then(function (ep) {
      if (!ep) throw new Error('no endpoint');
      return fetch(ep, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: prompt, prompt: prompt, query: prompt }),
      }).then(function (r) {
        if (!r.ok) throw new Error('http ' + r.status);
        return r.json();
      }).then(function (j) {
        // Accept several shapes
        return j.reply || j.message || j.text || j.answer || (j.data && (j.data.reply || j.data.text)) || JSON.stringify(j);
      });
    });
  }

  function resolveEndpoint() {
    if (endpointChecked) return Promise.resolve(resolvedEndpoint);
    return Promise.all(ENDPOINTS.map(function (ep) {
      return fetch(ep, { method: 'OPTIONS', credentials: 'same-origin' })
        .then(function (r) { return { ep: ep, ok: r.ok || r.status === 405 /* method-not-allowed still means route exists */ }; })
        .catch(function () { return { ep: ep, ok: false }; });
    })).then(function (results) {
      endpointChecked = true;
      var hit = results.find(function (r) { return r.ok; });
      resolvedEndpoint = hit ? hit.ep : null;
      return resolvedEndpoint;
    });
  }

  function escapeHtml(s) { return String(s||'').replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function escapeAttr(s) { return String(s||'').replace(/"/g, '&quot;'); }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready);
  else ready();
})();
