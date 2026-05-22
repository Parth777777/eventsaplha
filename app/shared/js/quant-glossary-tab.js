/* quant-glossary-tab.js — Glossary tab renderer.
 * Renders Glossary.all() with search, see-also links, used-in chips, and KaTeX formulas.
 */
(function () {
  'use strict';
  var initialised = false;
  document.addEventListener('qp:tab', function (e) {
    if (e.detail.tab !== 'glossary' || initialised) return;
    init(); initialised = true;
    setTimeout(function () { handleHash(); }, 50);
  });
  window.addEventListener('hashchange', handleHash);
  function handleHash() {
    if (!initialised) return;
    var h = decodeURIComponent((location.hash || '').replace('#', ''));
    if (!h) return;
    var el = document.getElementById('qpGloss-' + h);
    if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function init() {
    var root = document.getElementById('qpGlossaryRoot');
    if (!root || !window.Glossary) return;
    var terms = Glossary.all();
    root.innerHTML = ''
      + '<div class="qp-card">'
      + '  <div class="qp-card-title">Glossary &mdash; ' + terms.length + ' terms</div>'
      + '  <input class="qp-input" id="qpGlossSearch" placeholder="Search delta, sharpe, condor…" style="margin-bottom:14px;">'
      + '  <div id="qpGlossList"></div>'
      + '</div>';
    var list = root.querySelector('#qpGlossList');
    var search = root.querySelector('#qpGlossSearch');
    function render(query) {
      var matches = query ? Glossary.search(query) : terms;
      if (!matches.length) {
        list.innerHTML = '<div style="padding:30px;text-align:center;color:#6a7484;">No matches for "' + escapeHtml(query) + '"</div>';
        return;
      }
      list.innerHTML = matches.map(termCard).join('');
      // wire up "see also" chips
      list.querySelectorAll('[data-see]').forEach(function (a) {
        a.addEventListener('click', function (e) {
          e.preventDefault();
          var id = a.dataset.see;
          search.value = ''; render(''); // reset filter
          location.hash = '#' + id;
        });
      });
      // wire up "used in" chips
      list.querySelectorAll('[data-tool]').forEach(function (a) {
        a.addEventListener('click', function (e) {
          e.preventDefault();
          var url = new URL(location.href);
          url.searchParams.set('tool', a.dataset.tool);
          url.hash = '';
          location.href = url.toString();
        });
      });
      // render any KaTeX
      var iv = setInterval(function () {
        if (window.katex) { Glossary.renderAll(list); clearInterval(iv); }
      }, 80);
    }
    search.addEventListener('input', function () { render(search.value); });
    render('');
  }

  function termCard(t) {
    var formula = t.formula
      ? '<div class="qp-concept-formula" style="margin-top:10px;" data-katex-display data-katex="' + escapeAttr(t.formula) + '"></div>'
      : '';
    var see = (t.see_also || []).length
      ? '<div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;"><span style="font:700 9px Plus Jakarta Sans;letter-spacing:0.10em;text-transform:uppercase;color:#6a7484;">See also</span>'
        + t.see_also.map(function (id) {
            var ref = Glossary.get(id);
            if (!ref) return '';
            return '<a href="#' + id + '" data-see="' + id + '" style="font:700 10px Geist Mono;color:#adc6ff;background:rgba(142,180,224,0.10);padding:2px 7px;border-radius:3px;text-decoration:none;border:1px solid rgba(142,180,224,0.25);">' + escapeHtml(ref.term) + '</a>';
          }).join('')
        + '</div>'
      : '';
    var usedIn = (t.used_in || []).length
      ? '<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;"><span style="font:700 9px Plus Jakarta Sans;letter-spacing:0.10em;text-transform:uppercase;color:#6a7484;">Used in</span>'
        + t.used_in.map(function (tool) {
            return '<a href="?tool=' + tool + '" data-tool="' + tool + '" style="font:700 9px Plus Jakarta Sans;letter-spacing:0.08em;text-transform:uppercase;color:#6ddec0;background:rgba(45,212,170,0.08);padding:3px 7px;border-radius:3px;text-decoration:none;border:1px solid rgba(45,212,170,0.25);">' + tool + ' &rarr;</a>';
          }).join('')
        + '</div>'
      : '';
    return ''
      + '<div id="qpGloss-' + t.id + '" style="padding:16px 0;border-bottom:1px solid rgba(255,255,255,0.05);">'
      + '  <h3 style="font:800 14px/1 Plus Jakarta Sans;letter-spacing:-0.01em;color:#dde3ef;margin:0 0 6px 0;">' + escapeHtml(t.term) + '</h3>'
      + '  <div style="font:500 13px/1.5 DM Sans;color:#dde3ef;margin-bottom:8px;">' + escapeHtml(t.plain) + '</div>'
      + '  <div style="font:500 12.5px/1.6 DM Sans;color:#a4afc2;">' + escapeHtml(t.formal) + '</div>'
      + (t.units ? '<div style="font:600 11px Geist Mono;color:#adc6ff;margin-top:8px;">Units: ' + escapeHtml(t.units) + '</div>' : '')
      + formula
      + (t.example ? '<div style="margin-top:10px;padding:10px 12px;background:rgba(45,212,170,0.04);border-left:2px solid rgba(45,212,170,0.30);border-radius:3px;font:500 12px/1.5 DM Sans;color:#b0bccf;"><strong style="color:#6ddec0;font-weight:700;">Example:</strong> ' + escapeHtml(t.example) + '</div>' : '')
      + see + usedIn
      + '</div>';
  }
  function escapeHtml(s) { return String(s||'').replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function escapeAttr(s) { return String(s||'').replace(/"/g, '&quot;').replace(/&/g, '&amp;'); }
})();
