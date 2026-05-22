/* live-enrichment.js — voice squawk + inline backtest strip for the existing UI.
 *
 * The voice squawk is the moat for cross-tab trading: when a high-impact
 * event hits, the trader hears a tone + spoken summary without switching
 * tabs. The pipeline below is layered so each defect mode is contained:
 *
 *   1. Subscribes to /api/stream via window.Realtime (the existing SSE
 *      client). Falls back to a direct EventSource if Realtime isn't loaded.
 *   2. Filters to high-signal events only — `scored` and `alert` channels,
 *      never the raw `news` firehose. The trader hears curated information,
 *      not the entire scrape stream.
 *   3. Dedupes by event_id so a single event arriving as raw → scored
 *      doesn't speak twice.
 *   4. Plays a leading chime (SoundFX.pop or .alertHigh) BEFORE the spoken
 *      phrase. The chime grabs attention when the page is in a background
 *      tab; the voice gives the actionable summary.
 *   5. Keeps speechSynthesis alive in backgrounded tabs (Chrome auto-pauses
 *      after ~15s; we resume() every 4s while the queue is non-empty).
 *   6. Pings the document title with a "(N)" badge while the tab is hidden
 *      so the trader sees pending-alert count when glancing at the tab bar.
 *   7. Injects the third-stage `backtest_summary` strip into matching
 *      [data-event-id] rows (e.g. on #twHomeNews) so the trader can also
 *      visually inspect what happened historically in the same setup.
 *   8. Caches backtest summaries by event_id so a row rendered AFTER the
 *      broadcast still gets the strip (the home news widget polls every
 *      60s; the stream may have already delivered the summary).
 */
(function () {
    "use strict";

    // ---- AUDIO -----------------------------------------------------------
    const LS_VOICE = "tk:voice:on";
    const btn = document.getElementById("tkVoiceBtn");
    let voiceOn = false;
    // Cross-stage dedup: speak each event_id at most once per page session.
    // Raw → scored typically both carry the same event_id within 1-2s; without
    // this set the trader would hear every alert twice (and stale replay
    // events would replay aloud on reconnect).
    const spokenIds = new Set();
    const SPOKEN_MAX = 500;
    // Tab-title badge — count of new alerts received while document.hidden.
    const origTitle = document.title;
    let unseenCount = 0;

    function setBtnState() {
        if (!btn) return;
        btn.setAttribute("aria-pressed", voiceOn ? "true" : "false");
        btn.title = voiceOn ? "Voice squawk ON — click to mute"
                            : "Voice squawk OFF — click to read alerts aloud";
    }

    function lsGet(k, fb) { try { return localStorage.getItem(k) || fb; } catch (e) { return fb; } }
    function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

    function chimeFor(ev) {
        // Reuse the existing SoundFX class from app.js — same engine the
        // SoundFX bell button toggles. If SoundFX itself is muted by the
        // user, the calls below silently no-op (SoundFX._enabled flag).
        if (!window.SoundFX) return;
        const alpha = Number(ev && ev.alpha_score) || 0;
        const isAlert = ev && ev.channel === "alert";
        try {
            if (isAlert || alpha >= 70) {
                SoundFX.alertHigh();   // two-tone urgent (660 + 990 Hz)
            } else {
                SoundFX.pop();         // gentle single-tone descent
            }
        } catch (e) { /* audio is best-effort */ }
    }

    function voicePhraseFor(ev) {
        // Compose the spoken text — defaults to the server's cleaned phrase
        // but prepends "Breaking." for high-impact events so the trader's
        // ear catches the urgency without parsing every word.
        const base = ev && ev.cleaned_voice_phrase ? String(ev.cleaned_voice_phrase) : "";
        if (!base) return "";
        const alpha = Number(ev && ev.alpha_score) || 0;
        const isAlert = ev && ev.channel === "alert";
        if (isAlert || alpha >= 70) {
            // Don't re-prepend if the server already produced an "Alert."-led
            // phrase — voice_phrase.compact() does. Replace the lead-in word.
            return base.replace(/^Alert\b\.?\s*/i, "Breaking. ");
        }
        return base;
    }

    function speak(ev) {
        if (!voiceOn || !ev || !("speechSynthesis" in window)) return;
        const text = voicePhraseFor(ev);
        if (!text) return;
        try {
            const u = new SpeechSynthesisUtterance(text);
            u.rate = 1.05; u.pitch = 1.0; u.volume = 0.95;
            const v = speechSynthesis.getVoices().find(x => /en-IN|en-GB|en-US/i.test(x.lang));
            if (v) u.voice = v;
            speechSynthesis.speak(u);
        } catch (e) { /* swallow — audio is best-effort */ }
    }

    // Chrome / Edge pause speechSynthesis after ~15s in a backgrounded tab.
    // Periodically resume() while the queue is non-empty so a chain of
    // alerts arriving during a quick tab-switch still plays through.
    function startKeepAlive() {
        if (!("speechSynthesis" in window)) return;
        setInterval(() => {
            try {
                if (voiceOn && (speechSynthesis.speaking || speechSynthesis.pending)) {
                    speechSynthesis.resume();
                }
            } catch (e) {}
        }, 4000);
    }

    function bumpUnseenBadge() {
        // Increment the document.title prefix counter while the tab is
        // hidden so the trader sees "(3) Tickwave" in the tab bar even when
        // audio happens to be muted by the OS / hardware.
        if (!document.hidden) return;
        unseenCount += 1;
        document.title = "(" + unseenCount + ") " + origTitle;
    }

    document.addEventListener("visibilitychange", () => {
        if (!document.hidden) {
            unseenCount = 0;
            document.title = origTitle;
        }
    });

    function unlockAudio() {
        // Priming utterance to satisfy the browser's user-gesture requirement
        try { const u = new SpeechSynthesisUtterance(" "); u.volume = 0; speechSynthesis.speak(u); }
        catch (e) {}
        // Also wake the AudioContext used by SoundFX so the chime plays
        // immediately on the next alert (instead of being deferred to the
        // user's next manual click).
        try { if (window.SoundFX && SoundFX._getCtx) SoundFX._getCtx(); } catch (e) {}
        voiceOn = true;
        setBtnState();
        lsSet(LS_VOICE, "1");
    }
    function muteAudio() {
        voiceOn = false;
        try { speechSynthesis.cancel(); } catch (e) {}
        setBtnState();
        lsSet(LS_VOICE, "0");
    }

    if (btn) {
        btn.addEventListener("click", () => { voiceOn ? muteAudio() : unlockAudio(); });
        // Reflect the saved preference visually. Note: the browser still
        // requires a user gesture before speak() works, so we DON'T flip
        // voiceOn=true on its own — the saved state is just a hint for
        // the next click.
        if (lsGet(LS_VOICE, "0") === "1") {
            btn.title = "Voice squawk was on last session — click to resume";
        }
        setBtnState();
    }
    if ("speechSynthesis" in window) {
        speechSynthesis.onvoiceschanged = () => {};
        try { speechSynthesis.getVoices(); } catch (e) {}
        startKeepAlive();
    }

    // ---- BACKTEST INJECTION ---------------------------------------------
    // Cache of event_id -> backtest_summary for events seen on the stream.
    // Lets us re-apply the strip when the home news widget re-renders rows
    // after a poll cycle.
    const btCache = new Map();
    const BT_CACHE_MAX = 200;

    function fmtPct(v) {
        if (v === null || v === undefined || Number.isNaN(v)) return "—";
        const n = Number(v); return (n >= 0 ? "+" : "") + n.toFixed(2) + "%";
    }
    function pctCls(v) {
        if (v === null || v === undefined || Number.isNaN(v)) return "";
        return Number(v) >= 0 ? "bull" : "bear";
    }
    function verdict(bs) {
        if (!bs || bs.stale || (bs.sample_events_found || 0) < 2) return null;
        const gap = Number(bs.avg_gap_up_pct), fade = Number(bs.fade_probability_pct),
              drift = Number(bs.avg_5day_drift_pct), n = bs.sample_events_found;
        let label, cls = "";
        if (gap > 0.5 && fade < 40 && drift > 0)       { label = "GAP-AND-GO";          cls = "bull"; }
        else if (gap > 0.5 && fade >= 60)              { label = "SPIKE-AND-FADE";      cls = "bear"; }
        else if (gap < -0.5 && drift < 0)              { label = "BREAKDOWN";           cls = "bear"; }
        else if (gap < -0.5 && drift > 0.5)            { label = "OVERREACTION REBOUND"; cls = "bull"; }
        else if (Math.abs(drift) < 0.5)                { label = "MUTED"; }
        else                                            { label = drift > 0 ? "DRIFT UP" : "DRIFT DOWN";
                                                          cls = drift > 0 ? "bull" : "bear"; }
        return { label, cls, n };
    }
    function buildStripHTML(bs) {
        if (!bs) return "";
        const v = verdict(bs);
        const stale = !!bs.stale;
        const parts = [];
        if (v) {
            parts.push('<span class="bt-verdict ' + v.cls + '">' +
                       v.label + '</span> <span class="bt-pill">n=' + v.n + '</span> ');
        }
        if (!stale) {
            parts.push('<span class="bt-pill ' + pctCls(bs.avg_gap_up_pct) + '">gap ' +
                       fmtPct(bs.avg_gap_up_pct) + '</span>');
            const fadeCls = Number(bs.fade_probability_pct) >= 50 ? "bear" : "bull";
            parts.push('<span class="bt-pill ' + fadeCls + '">fade ' +
                       Number(bs.fade_probability_pct).toFixed(0) + '%</span>');
            parts.push('<span class="bt-pill ' + pctCls(bs.avg_5day_drift_pct) + '">5d ' +
                       fmtPct(bs.avg_5day_drift_pct) + '</span>');
        }
        return parts.join("");
    }

    function applyToRow(rowEl, bs) {
        if (!rowEl) return;
        const slot = rowEl.querySelector("[data-bt-slot]");
        if (!slot) return;
        const html = buildStripHTML(bs);
        if (html && slot.innerHTML !== html) slot.innerHTML = html;
    }

    function applyBacktest(eventId, bs) {
        if (!eventId || !bs) return;
        // Cache for re-render on next poll
        btCache.set(eventId, bs);
        while (btCache.size > BT_CACHE_MAX) {
            btCache.delete(btCache.keys().next().value);
        }
        // Try every matching row currently in DOM (selector matches both home news rows
        // and any other surface that opts in by setting data-event-id).
        const rows = document.querySelectorAll('[data-event-id="' + cssEscape(eventId) + '"]');
        rows.forEach(r => applyToRow(r, bs));
    }

    function cssEscape(s) {
        // Cheap CSS-attribute-value escape — enough for our event-id strings
        // (which are slug-like: alphanumeric, hyphens, underscores).
        return String(s).replace(/["\\]/g, "\\$&");
    }

    // When the home news widget re-renders (every 60s), apply cached
    // backtests to any newly-inserted rows that we already have data for.
    // We watch the news list for childList mutations rather than racing
    // setInterval with the widget's own polling cadence.
    function watchHomeNews() {
        const root = document.getElementById("twHomeNews");
        if (!root || typeof MutationObserver === "undefined") return;
        const reapply = () => {
            if (btCache.size === 0) return;
            root.querySelectorAll("[data-event-id]").forEach(row => {
                const id = row.getAttribute("data-event-id");
                const bs = id && btCache.get(id);
                if (bs) applyToRow(row, bs);
            });
        };
        reapply();
        const mo = new MutationObserver(() => { reapply(); });
        mo.observe(root, { childList: true, subtree: false });
    }

    // ---- STREAM SUBSCRIPTION --------------------------------------------
    // Squawk quality gate: speak only events the alpha pipeline has scored,
    // or smart-alert hits. The raw `news` channel is the firehose — speaking
    // every entry would drown the trader in noise. By the time an event
    // reaches `scored`, it has passed the alpha threshold + forensic + news-
    // quality filters; by the time it reaches `alert`, it crossed a smart-
    // alert rule the user explicitly set. Both are "good news" by definition.
    function shouldSquawk(ev) {
        if (!ev) return false;
        if (ev._replay) return false;                      // never auto-speak replays
        if (!ev.cleaned_voice_phrase) return false;
        if (ev.event_id && spokenIds.has(ev.event_id)) return false;
        if (ev.channel === "alert") return true;
        if (ev.channel === "scored") return true;
        // Raw news firehose: only speak if the row has a meaningful alpha
        // score attached (rare on the raw stage, but supported for callers
        // that pre-score before broadcasting).
        if (ev.channel === "news" && Number(ev.alpha_score) >= 50) return true;
        return false;
    }

    function rememberSpoken(eventId) {
        if (!eventId) return;
        spokenIds.add(eventId);
        if (spokenIds.size > SPOKEN_MAX) {
            // Evict the oldest entry (Set preserves insertion order)
            const first = spokenIds.values().next().value;
            if (first) spokenIds.delete(first);
        }
    }

    function onEvent(ev) {
        if (!ev) return;
        if (ev.channel === "hello" || ev.channel === "heartbeat") return;
        if (ev.channel === "backtest") {
            applyBacktest(ev.event_id, ev.backtest_summary);
            return;
        }
        if (!shouldSquawk(ev)) return;
        // The trader's attention path: chime first (grabs ear), then voice.
        // Both fire even if the page is in a background tab.
        chimeFor(ev);
        speak(ev);
        rememberSpoken(ev.event_id);
        bumpUnseenBadge();
    }

    function start() {
        watchHomeNews();
        if (window.Realtime && typeof window.Realtime.subscribe === "function") {
            // Reuse the existing client — same SSE connection as the status
            // banner uses; the server fan-out is one event → all subscribers.
            window.Realtime.subscribe({
                channels: ["news", "scored", "alert", "backtest"],
                replay: 10,
                onEvent,
            });
        } else {
            // Fallback if shared/js/realtime.js didn't load
            try {
                const es = new EventSource("/api/stream?channels=news,scored,alert,backtest&replay=10");
                const handle = (e) => { try { onEvent(JSON.parse(e.data)); } catch (err) {} };
                es.onmessage = handle;
                ["news", "scored", "alert", "backtest", "hello"].forEach(t =>
                    es.addEventListener(t, handle));
            } catch (e) { /* no live enrichment — page still works on polling */ }
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start, { once: true });
    } else {
        start();
    }
})();
