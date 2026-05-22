/* Event Terminal — live WebSocket-first (SSE fallback) feed with
   browser-native TTS squawk and event-window backtest stats.

   Self-initializes when it finds an element with id="tkFeed" on the page.
   Coexists with the rest of the app; all DOM is scoped under .tk-terminal. */

(function () {
    "use strict";

    function init() {
        const elFeed = document.getElementById("tkFeed");
        if (!elFeed) return;   // page does not embed the terminal
        if (elFeed.dataset.tkInit === "1") return;  // already wired
        elFeed.dataset.tkInit = "1";

        const elEmpty    = document.getElementById("tkEmpty");
        const elAudioBtn = document.getElementById("tkAudioBtn");
        const elAudioLbl = document.getElementById("tkAudioLabel");
        const elConn     = document.getElementById("tkConn");
        const elConnLbl  = document.getElementById("tkConnLabel");
        const elScored   = document.getElementById("tkScoredOnly");

        // ---- STATE -------------------------------------------------------
        const MAX_CARDS = 60;
        const LS_AUDIO = "tk:terminal:audioOn";
        const LS_SCORED = "tk:terminal:scoredOnly";
        const cardsById = new Map();
        let audioOn = false;
        let scoredOnly = false;
        const origTitle = document.title;
        let unseenCount = 0;

        function lsGet(k, fallback) {
            try { return localStorage.getItem(k) || fallback; } catch (e) { return fallback; }
        }
        function lsSet(k, v) {
            try { localStorage.setItem(k, v); } catch (e) {}
        }

        // ---- AUDIO (browser-native TTS) ----------------------------------
        function speak(text) {
            if (!audioOn || !text || !("speechSynthesis" in window)) return;
            try {
                const u = new SpeechSynthesisUtterance(String(text));
                u.rate = 1.05; u.pitch = 1.0; u.volume = 0.95;
                const v = speechSynthesis.getVoices().find(x => /en-IN|en-GB|en-US/i.test(x.lang));
                if (v) u.voice = v;
                speechSynthesis.speak(u);
            } catch (e) { /* swallow */ }
        }

        function unlockAudio() {
            try {
                const u = new SpeechSynthesisUtterance(" ");
                u.volume = 0; speechSynthesis.speak(u);
            } catch (e) {}
            audioOn = true;
            if (elAudioBtn) elAudioBtn.classList.add("is-live");
            if (elAudioLbl) elAudioLbl.textContent = "Audio Live — click to mute";
        }

        function muteAudio() {
            audioOn = false;
            try { speechSynthesis.cancel(); } catch (e) {}
            if (elAudioBtn) elAudioBtn.classList.remove("is-live");
            if (elAudioLbl) elAudioLbl.textContent = "Launch Audio Station";
            lsSet(LS_AUDIO, "0");
        }

        if (elAudioBtn) {
            elAudioBtn.addEventListener("click", () => {
                if (audioOn) muteAudio(); else { unlockAudio(); lsSet(LS_AUDIO, "1"); }
            });
        }
        if (elScored) {
            // Restore saved preference before wiring the change handler
            if (lsGet(LS_SCORED, "0") === "1") {
                elScored.checked = true;
                scoredOnly = true;
            }
            elScored.addEventListener("change", () => {
                scoredOnly = elScored.checked;
                lsSet(LS_SCORED, scoredOnly ? "1" : "0");
                cardsById.forEach((entry) => {
                    const visible = !scoredOnly || (entry.payload.alpha_score || 0) >= 50;
                    entry.el.style.display = visible ? "" : "none";
                });
            });
        }

        // Tab-title badge for new alerts while the tab is backgrounded.
        // speechSynthesis still works when hidden, but a visual ping helps
        // users who muted the squawk.
        function bumpBadge() {
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

        // ---- HELPERS -----------------------------------------------------
        function fmtTime(ts) {
            const d = ts ? new Date(ts * 1000) : new Date();
            return d.toLocaleTimeString("en-IN", { hour12: false });
        }
        function fmtPct(v) {
            if (v === null || v === undefined || Number.isNaN(v)) return "—";
            const n = Number(v); return (n >= 0 ? "+" : "") + n.toFixed(2) + "%";
        }
        function pctClass(v) {
            if (v === null || v === undefined || Number.isNaN(v)) return "muted";
            return Number(v) >= 0 ? "bull" : "bear";
        }
        function escapeHtml(s) {
            return String(s == null ? "" : s)
                .replace(/&/g, "&amp;").replace(/</g, "&lt;")
                .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
        }
        function tickersText(p) {
            if (p.ticker) return p.ticker;
            if (Array.isArray(p.tickers) && p.tickers.length) return p.tickers.join(", ");
            return p.company || "";
        }
        function sentimentPill(s) {
            if (s == null) return "";
            const n = Number(s);
            if (Number.isNaN(n)) return "";
            const cls = n > 0.15 ? "is-bull" : (n < -0.15 ? "is-bear" : "is-caution");
            const label = n > 0.15 ? "BULLISH" : (n < -0.15 ? "BEARISH" : "NEUTRAL");
            return '<span class="tk-pill ' + cls + '">' + label + ' ' + n.toFixed(2) + '</span>';
        }
        function alphaPill(a) {
            if (a == null) return "";
            const n = Number(a);
            if (Number.isNaN(n)) return "";
            const cls = n >= 70 ? "is-bull" : (n >= 50 ? "is-caution" : "");
            return '<span class="tk-pill ' + cls + '">&alpha; ' + Math.round(n) + '</span>';
        }

        // ---- RENDER ------------------------------------------------------
        function renderCard(p) {
            const headline = p.headline || p.title || p.summary || "(no headline)";
            const tail = headline.length > 240 ? headline.slice(0, 237) + "…" : headline;
            const ticker = tickersText(p);
            const cat = p.event_type ? p.event_type.replace(/_/g, " ") : "";
            return [
                '<div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">',
                    '<span class="tk-time">', fmtTime(p.ts), '</span>',
                    ticker ? '<span class="tk-ticker">' + escapeHtml(ticker) + '</span>' : "",
                    cat ? '<span class="tk-pill">' + escapeHtml(cat).toUpperCase() + '</span>' : "",
                    alphaPill(p.alpha_score),
                    sentimentPill(p.sentiment != null ? p.sentiment : p.sentiment_score),
                '</div>',
                '<div class="tk-tail">', escapeHtml(tail), '</div>',
                p.link ? '<div style="margin-top:6px;"><a class="tk-link" href="' + encodeURI(p.link) + '" target="_blank" rel="noopener">Source &#8599;</a></div>' : "",
                '<div class="tk-grid" data-bt-slot>',
                    '<div class="tk-stat"><div class="lbl">Gap</div><div class="val muted">—</div></div>',
                    '<div class="tk-stat"><div class="lbl">Fade Prob</div><div class="val muted">—</div></div>',
                    '<div class="tk-stat"><div class="lbl">5d Drift</div><div class="val muted">—</div></div>',
                '</div>',
            ].join("");
        }

        function upsertCard(p) {
            if (!p || !p.event_id) return;
            if (scoredOnly && p.channel !== "scored" && p.channel !== "backtest" &&
                (!p.alpha_score || p.alpha_score < 50)) {
                if (!cardsById.has(p.event_id)) return;
            }

            let entry = cardsById.get(p.event_id);
            if (!entry) {
                const el = document.createElement("div");
                el.className = "tk-card";
                el.dataset.eventId = p.event_id;
                el.dataset.new = "1";
                el.innerHTML = renderCard(p);
                if (elEmpty) elEmpty.style.display = "none";
                elFeed.insertBefore(el, elFeed.firstChild === elEmpty ? null : elFeed.firstChild);
                entry = { el, payload: Object.assign({}, p) };
                cardsById.set(p.event_id, entry);

                if (p.cleaned_voice_phrase) speak(p.cleaned_voice_phrase);
                bumpBadge();

                while (cardsById.size > MAX_CARDS) {
                    const oldestId = cardsById.keys().next().value;
                    const old = cardsById.get(oldestId);
                    if (old && old.el && old.el.parentNode) old.el.parentNode.removeChild(old.el);
                    cardsById.delete(oldestId);
                }
                setTimeout(() => { if (el.parentNode) el.dataset.new = "0"; }, 4000);
            } else {
                entry.payload = Object.assign({}, entry.payload, p);
                entry.el.innerHTML = renderCard(entry.payload);
                entry.el.classList.add("tk-flash");
                setTimeout(() => entry.el.classList.remove("tk-flash"), 1200);
            }

            if (scoredOnly) {
                const a = entry.payload.alpha_score || 0;
                entry.el.style.display = a >= 50 ? "" : "none";
            }
        }

        function verdictFromBacktest(bs) {
            // One-line plain-English summary of the historical pattern.
            // Returns null when sample too small or data missing.
            if (!bs || bs.stale || (bs.sample_events_found || 0) < 2) return null;
            const gap = Number(bs.avg_gap_up_pct);
            const fade = Number(bs.fade_probability_pct);
            const drift = Number(bs.avg_5day_drift_pct);
            const n = bs.sample_events_found;
            let label, cls;
            if (gap > 0.5 && fade < 40 && drift > 0) {
                label = "GAP-AND-GO"; cls = "is-bull";
            } else if (gap > 0.5 && fade >= 60) {
                label = "SPIKE-AND-FADE"; cls = "is-bear";
            } else if (gap < -0.5 && drift < 0) {
                label = "BREAKDOWN"; cls = "is-bear";
            } else if (gap < -0.5 && drift > 0.5) {
                label = "OVERREACTION REBOUND"; cls = "is-bull";
            } else if (Math.abs(drift) < 0.5) {
                label = "MUTED"; cls = "";
            } else {
                label = drift > 0 ? "DRIFT UP" : "DRIFT DOWN";
                cls = drift > 0 ? "is-bull" : "is-bear";
            }
            return '<span class="tk-pill ' + cls + '">' + label +
                   ' · ' + n + ' similar</span>';
        }

        function applyBacktest(p) {
            const entry = cardsById.get(p.event_id);
            if (!entry || !p.backtest_summary) return;
            const bs = p.backtest_summary;
            const slot = entry.el.querySelector("[data-bt-slot]");
            if (!slot) return;
            const gap = bs.avg_gap_up_pct, fade = bs.fade_probability_pct,
                  drift = bs.avg_5day_drift_pct, n = bs.sample_events_found || 0,
                  stale = !!bs.stale;
            slot.innerHTML = [
                '<div class="tk-stat"><div class="lbl">Gap (n=', n, ')</div>',
                    '<div class="val ', stale ? "muted" : pctClass(gap), '">',
                    stale ? "—" : fmtPct(gap), '</div></div>',
                '<div class="tk-stat"><div class="lbl">Fade Prob</div>',
                    '<div class="val ', stale ? "muted" : (fade >= 50 ? "bear" : "bull"), '">',
                    stale ? "—" : (Number(fade).toFixed(0) + "%"), '</div></div>',
                '<div class="tk-stat"><div class="lbl">5d Drift</div>',
                    '<div class="val ', stale ? "muted" : pctClass(drift), '">',
                    stale ? "—" : fmtPct(drift), '</div></div>',
            ].join("");

            // Verdict line — appears between the headline and the stats grid.
            // Replaces any prior verdict from earlier-arriving raw/scored frames.
            const verdict = verdictFromBacktest(bs);
            let vEl = entry.el.querySelector("[data-bt-verdict]");
            if (verdict) {
                if (!vEl) {
                    vEl = document.createElement("div");
                    vEl.dataset.btVerdict = "1";
                    vEl.style.marginTop = "8px";
                    // Place verdict just before the stats grid
                    slot.parentNode.insertBefore(vEl, slot);
                }
                vEl.innerHTML = verdict;
            } else if (vEl) {
                vEl.remove();
            }
        }

        // ---- LIVE TRANSPORT (WS primary, SSE fallback) -------------------
        let ws = null, es = null, transport = null;
        let retryDelay = 1000;
        let nextWsFallbackAt = 0;
        const MAX_RETRY = 30000;

        function setConn(state, label) {
            if (!elConn) return;
            elConn.className = "tk-conn " + (state || "");
            if (elConnLbl) elConnLbl.textContent = label;
        }
        function teardown() {
            try { if (ws) ws.close(); } catch (e) {}
            try { if (es) es.close(); } catch (e) {}
            ws = es = null;
        }
        function handleFrame(data) {
            if (!data) return;
            if (data.channel === "hello" || data.channel === "heartbeat") return;
            if (data.channel === "backtest") return applyBacktest(data);
            if (data._replay) {
                const before = audioOn; audioOn = false;
                upsertCard(data); audioOn = before;
            } else { upsertCard(data); }
        }
        function wsUrl() {
            const proto = location.protocol === "https:" ? "wss:" : "ws:";
            return proto + "//" + location.host +
                "/ws/stream?channels=news,scored,alert,backtest&replay=10";
        }
        function connectWS() {
            teardown();
            setConn("", "Connecting (WS)…");
            let opened = false;
            try { ws = new WebSocket(wsUrl()); }
            catch (e) { return connectSSE(); }
            ws.onopen = () => {
                opened = true; transport = "ws"; retryDelay = 1000;
                setConn("live", "Live · WS");
            };
            ws.onmessage = (e) => {
                let data; try { data = JSON.parse(e.data); } catch (err) { return; }
                handleFrame(data);
            };
            ws.onerror = () => { /* handled by onclose */ };
            ws.onclose = () => {
                if (!opened) {
                    nextWsFallbackAt = Date.now() + 5 * 60 * 1000;
                    setConn("err", "WS unavailable, falling back…");
                    setTimeout(connectSSE, 500);
                } else {
                    setConn("err", "Reconnecting (WS)…");
                    setTimeout(connect, retryDelay);
                    retryDelay = Math.min(retryDelay * 2, MAX_RETRY);
                }
            };
        }
        function connectSSE() {
            teardown();
            setConn("", "Connecting (SSE)…");
            try { es = new EventSource("/api/stream?channels=news,scored,alert,backtest&replay=10"); }
            catch (e) {
                setTimeout(connect, retryDelay);
                retryDelay = Math.min(retryDelay * 2, MAX_RETRY);
                return;
            }
            es.onopen = () => {
                transport = "sse"; retryDelay = 1000;
                setConn("live", "Live · SSE");
            };
            es.onerror = () => {
                setConn("err", "Reconnecting (SSE)…");
                try { es.close(); } catch (e) {}
                setTimeout(connect, retryDelay);
                retryDelay = Math.min(retryDelay * 2, MAX_RETRY);
            };
            const onMessage = (e) => {
                let data; try { data = JSON.parse(e.data); } catch (err) { return; }
                handleFrame(data);
            };
            ["news", "scored", "alert", "backtest", "hello", "message"].forEach(t =>
                es.addEventListener(t, onMessage));
        }
        function connect() {
            if (typeof WebSocket !== "undefined" && Date.now() >= nextWsFallbackAt) {
                connectWS();
            } else {
                connectSSE();
            }
        }

        if ("speechSynthesis" in window) {
            speechSynthesis.onvoiceschanged = () => {};
            try { speechSynthesis.getVoices(); } catch (e) {}
        }

        // Pre-populate from the REST replay buffer so the feed isn't empty
        // for the first 30s after a new visitor lands. We tag every fetched
        // event with `_replay:true` so the speech path stays muted.
        function preloadRecent() {
            fetch("/api/stream/recent?limit=15", { credentials: "same-origin" })
                .then(r => r.ok ? r.json() : null)
                .then(j => {
                    if (!j || !j.data || !Array.isArray(j.data)) return;
                    // Render oldest first so the newest ends up on top
                    j.data.forEach(ev => {
                        if (!ev) return;
                        if (ev.channel === "hello" || ev.channel === "heartbeat") return;
                        if (ev.channel === "backtest") return applyBacktest(ev);
                        const before = audioOn; audioOn = false;
                        upsertCard(Object.assign({}, ev, { _replay: true }));
                        audioOn = before;
                    });
                })
                .catch(() => { /* fail silent — live stream still works */ });
        }
        preloadRecent();

        // Restore audio preference (NOTE: even with autoOn=true in storage,
        // the browser blocks speechSynthesis until the user clicks the page.
        // Surfacing the saved preference via the button state is the best we
        // can do without a gesture — they tap and it resumes immediately).
        if (lsGet(LS_AUDIO, "0") === "1" && elAudioBtn) {
            elAudioBtn.classList.add("is-live");
            if (elAudioLbl) elAudioLbl.textContent = "Tap to resume audio";
        }

        connect();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init, { once: true });
    } else {
        init();
    }
})();
