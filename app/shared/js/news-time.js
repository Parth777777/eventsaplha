/* news-time.js — classify when a news/event was published relative to
 * Indian market hours (NSE/BSE: 9:15 - 15:30 IST, Mon-Fri).
 *
 * window.NewsTime.classify(publishedAt) → {status, label, color, time, ageText, fullText}
 * window.NewsTime.renderPill(publishedAt, opts?) → HTML string
 *
 * Statuses (color = the visual cue traders should associate):
 *   pre_market  — published before today's 9:15 IST open (will hit at the bell)
 *   intra_day   — published during today's 9:15-15:30 session (already reacting)
 *   post_close  — published after today's 15:30 close (will hit tomorrow's open)
 *   yesterday   — published the previous trading day after close
 *   weekend     — Saturday/Sunday (will hit Monday's open)
 *   older       — more than 1 day ago
 *   unknown     — unparseable / missing timestamp
 */
(function () {
    const PRE_OPEN_MIN = 9 * 60 + 15;   // 09:15 IST
    const CLOSE_MIN    = 15 * 60 + 30;  // 15:30 IST

    function _parseDate(v) {
        if (!v) return null;
        if (v instanceof Date) return isNaN(v.getTime()) ? null : v;
        let s = String(v).trim();
        // SQLite "YYYY-MM-DD HH:MM:SS" with no zone — treat as UTC
        if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(s)) s = s.replace(' ', 'T') + 'Z';
        const d = new Date(s);
        return isNaN(d.getTime()) ? null : d;
    }

    // Convert a UTC Date to a pseudo-UTC Date offset by IST (+5:30)
    // so we can read getUTCHours/Day on it as if they were IST values.
    function _toIST(d) { return new Date(d.getTime() + 5.5 * 3600 * 1000); }

    function _relAge(ms) {
        if (ms < 0) return 'just now';
        const m = Math.floor(ms / 60000);
        if (m < 1)  return 'just now';
        if (m < 60) return m + 'm ago';
        const h = Math.floor(m / 60);
        if (h < 24) return h + 'h ago';
        const d = Math.floor(h / 24);
        if (d < 7)  return d + 'd ago';
        if (d < 30) return Math.floor(d / 7) + 'w ago';
        return Math.floor(d / 30) + 'mo ago';
    }

    function _isWeekendIST(istD) {
        const wd = istD.getUTCDay();
        return wd === 0 || wd === 6;
    }

    function classify(publishedAt) {
        const d = _parseDate(publishedAt);
        if (!d) return { status: 'unknown', label: '—', color: '#5a6373',
                         time: '', ageText: '', fullText: '' };

        const now = new Date();
        const istPub = _toIST(d);
        const istNow = _toIST(now);

        const pubMin = istPub.getUTCHours() * 60 + istPub.getUTCMinutes();
        const timeStr = String(istPub.getUTCHours()).padStart(2, '0') + ':' +
                        String(istPub.getUTCMinutes()).padStart(2, '0') + ' IST';
        const ageText = _relAge(now - d);

        const sameDay = (istPub.getUTCFullYear() === istNow.getUTCFullYear()
                      && istPub.getUTCMonth()    === istNow.getUTCMonth()
                      && istPub.getUTCDate()     === istNow.getUTCDate());
        const pubIsWeekend = _isWeekendIST(istPub);

        const mk = (status, label, color) => ({
            status, label, color, time: timeStr, ageText,
            fullText: `${label} · ${timeStr} · ${ageText}`,
        });

        if (sameDay) {
            if (pubIsWeekend)              return mk('weekend',    'Weekend',     '#8a94a8');
            if (pubMin < PRE_OPEN_MIN)     return mk('pre_market', 'Pre-market',  '#a78bfa');
            if (pubMin <= CLOSE_MIN)       return mk('intra_day',  'Intra-day',   '#e6b84a');
            return                              mk('post_close',  'Post-close',  '#6ba3d6');
        }

        const istYesterday = new Date(istNow.getTime() - 86400000);
        const isYesterday = (istPub.getUTCFullYear() === istYesterday.getUTCFullYear()
                          && istPub.getUTCMonth()    === istYesterday.getUTCMonth()
                          && istPub.getUTCDate()     === istYesterday.getUTCDate());
        if (isYesterday) {
            if (pubIsWeekend) return mk('weekend',   'Weekend',   '#8a94a8');
            return mk('yesterday', 'Yesterday', '#5a6373');
        }
        if (pubIsWeekend) return mk('weekend', 'Weekend', '#8a94a8');
        return mk('older', ageText, '#3d4a5c');
    }

    function _esc(s) {
        return String(s || '').replace(/[&<>"']/g,
            c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function renderPill(publishedAt, opts) {
        opts = opts || {};
        const c = classify(publishedAt);
        const showTime = opts.compact ? false : (opts.showTime !== false);
        const showAge  = !!opts.showAge;
        return `<span class="news-time-pill" title="${_esc(c.fullText)}"
            style="background:${c.color}1f;color:${c.color};border:1px solid ${c.color}55;">
            <span class="ntp-dot" style="background:${c.color};"></span>
            ${_esc(c.label)}${showTime && c.time ? ` <span style="opacity:0.7;font-weight:600;">${_esc(c.time)}</span>` : ''}${showAge && c.ageText ? ` <span style="opacity:0.55;">· ${_esc(c.ageText)}</span>` : ''}
        </span>`;
    }

    // Inject minimal CSS once
    if (typeof document !== 'undefined' && !document.getElementById('news-time-styles')) {
        const s = document.createElement('style');
        s.id = 'news-time-styles';
        s.textContent = `
            .news-time-pill {
                display: inline-flex; align-items: center; gap: 4px;
                padding: 2px 7px; border-radius: 999px;
                font-size: 8.5px; font-weight: 800; letter-spacing: 0.05em;
                text-transform: uppercase; line-height: 1.4;
                white-space: nowrap;
                font-family: 'Geist Mono', monospace;
            }
            .news-time-pill .ntp-dot {
                display: inline-block; width: 5px; height: 5px; border-radius: 50%;
                box-shadow: 0 0 4px currentColor;
            }
        `;
        document.head.appendChild(s);
    }

    window.NewsTime = { classify, renderPill };
})();
