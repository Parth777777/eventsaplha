/* tools.js — shared helpers for /app/tools/* calculator pages.
   Pure vanilla JS, no dependencies. Exposes window.TKTools with:
     formatINR(n)           — "₹1,23,45,678" (Indian numbering)
     formatINRShort(n)      — "₹1.23 Cr", "₹45.20 L", "₹12,345"
     formatPct(n, digits)   — "12.50%"
     parseNumber(str)       — "1,23,456" → 123456
     bindSlider({slider, input, min, max, step, onChange})
                            — keeps a <input type=range> and a <input type=number>
                              in sync; fires onChange(value) on every update
     animateNumber(el, to, opts) — smoothly count from current to `to`
     drawDonut(svg, segments)    — render SVG donut: [{value, color, label}]
     drawStackedArea(svg, series, opts) — render area chart of invested vs returns
     drawBars(svg, data, opts)   — render simple bar chart
     mountFAQ(faqs)              — injects JSON-LD <script> for FAQPage rich snippet
*/
(function () {
  "use strict";
  if (window.TKTools) return;

  // ── number formatting (Indian numbering: lakhs / crores) ──────────────
  function _toFixed(n, d) {
    if (typeof d !== "number") d = 0;
    return Number(n).toFixed(d);
  }
  function formatINR(n, digits) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
    const fixed = _toFixed(n, digits || 0);
    // Split sign / integer / decimal
    const negative = fixed.startsWith("-");
    const abs = negative ? fixed.slice(1) : fixed;
    const parts = abs.split(".");
    let intPart = parts[0];
    const decPart = parts[1] ? "." + parts[1] : "";
    // Indian numbering: last 3 digits, then groups of 2
    if (intPart.length > 3) {
      const last3 = intPart.slice(-3);
      let rest = intPart.slice(0, -3);
      rest = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",");
      intPart = rest + "," + last3;
    }
    return (negative ? "-" : "") + "₹" + intPart + decPart;
  }
  function formatINRShort(n) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
    const v = Number(n);
    const negative = v < 0;
    const abs = Math.abs(v);
    let body;
    if (abs >= 1e7)      body = (abs / 1e7).toFixed(2) + " Cr";
    else if (abs >= 1e5) body = (abs / 1e5).toFixed(2) + " L";
    else                 body = formatINR(abs, 0).replace("₹", "");
    return (negative ? "-" : "") + "₹" + body;
  }
  function formatPct(n, digits) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
    return Number(n).toFixed(digits == null ? 2 : digits) + "%";
  }
  function parseNumber(str) {
    if (typeof str === "number") return str;
    if (str === null || str === undefined) return NaN;
    const cleaned = String(str).replace(/[₹,\s]/g, "");
    return parseFloat(cleaned);
  }

  // ── slider ↔ number-input sync ───────────────────────────────────────
  function bindSlider(opts) {
    const slider = opts.slider;
    const input = opts.input;
    const min = Number(opts.min != null ? opts.min : slider.min || 0);
    const max = Number(opts.max != null ? opts.max : slider.max || 100);
    const step = Number(opts.step || slider.step || 1);
    const onChange = opts.onChange || function () {};
    function clamp(v) { return Math.max(min, Math.min(max, v)); }
    function setPct() {
      const v = Number(slider.value);
      const pct = ((v - min) / (max - min)) * 100;
      slider.style.setProperty("--pct", pct + "%");
    }
    function fromSlider() {
      const v = Number(slider.value);
      if (input) input.value = v;
      setPct();
      onChange(v);
    }
    function fromInput() {
      let v = parseNumber(input.value);
      if (Number.isNaN(v)) v = min;
      v = clamp(v);
      slider.value = String(v);
      input.value = v;
      setPct();
      onChange(v);
    }
    slider.addEventListener("input", fromSlider);
    if (input) {
      input.addEventListener("input", fromInput);
      input.addEventListener("blur", fromInput);
    }
    setPct();
    // Initial value broadcast so consumers can compute on load
    onChange(Number(slider.value));
    return {
      setValue: function (v) {
        v = clamp(Number(v));
        slider.value = String(v);
        if (input) input.value = v;
        setPct();
        onChange(v);
      },
      getValue: function () { return Number(slider.value); },
    };
  }

  // ── animated number counter ──────────────────────────────────────────
  const _ANIM_STATE = new WeakMap();
  function animateNumber(el, to, opts) {
    if (!el) return;
    opts = opts || {};
    const prev = _ANIM_STATE.get(el) || { value: 0 };
    if (prev.raf) cancelAnimationFrame(prev.raf);
    const from = Number(prev.value) || 0;
    const target = Number(to) || 0;
    if (Math.abs(target - from) < 0.01) {
      el.textContent = (opts.format || formatINRShort)(target);
      _ANIM_STATE.set(el, { value: target });
      return;
    }
    const duration = opts.duration || 700;
    const start = performance.now();
    function ease(t) { return 1 - Math.pow(1 - t, 3); }
    function step(now) {
      const t = Math.min(1, (now - start) / duration);
      const v = from + (target - from) * ease(t);
      el.textContent = (opts.format || formatINRShort)(v);
      const state = { value: v };
      if (t < 1) state.raf = requestAnimationFrame(step);
      else state.value = target;
      _ANIM_STATE.set(el, state);
    }
    _ANIM_STATE.set(el, { value: from, raf: requestAnimationFrame(step) });
  }

  // ── SVG donut chart ──────────────────────────────────────────────────
  // segments = [{ value, color, label }]
  function drawDonut(svg, segments, opts) {
    opts = opts || {};
    const size = opts.size || 220;
    const stroke = opts.stroke || 30;
    const r = (size - stroke) / 2;
    const cx = size / 2, cy = size / 2;
    const total = segments.reduce((s, x) => s + Math.max(0, Number(x.value) || 0), 0) || 1;
    svg.setAttribute("viewBox", `0 0 ${size} ${size}`);
    svg.innerHTML = "";
    // Background ring
    const bg = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    bg.setAttribute("cx", cx); bg.setAttribute("cy", cy); bg.setAttribute("r", r);
    bg.setAttribute("fill", "none");
    bg.setAttribute("stroke", "#1c2435");
    bg.setAttribute("stroke-width", stroke);
    svg.appendChild(bg);
    // Segments — stroke-dasharray on a circle
    let offset = 0;
    const C = 2 * Math.PI * r;
    segments.forEach((seg) => {
      const v = Math.max(0, Number(seg.value) || 0);
      const len = (v / total) * C;
      if (len <= 0) return;
      const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      c.setAttribute("cx", cx); c.setAttribute("cy", cy); c.setAttribute("r", r);
      c.setAttribute("fill", "none");
      c.setAttribute("stroke", seg.color);
      c.setAttribute("stroke-width", stroke);
      c.setAttribute("stroke-dasharray", `${len} ${C - len}`);
      c.setAttribute("stroke-dashoffset", `${-offset}`);
      c.setAttribute("transform", `rotate(-90 ${cx} ${cy})`);
      c.setAttribute("stroke-linecap", "butt");
      svg.appendChild(c);
      offset += len;
    });
    // Centre label
    if (opts.centerLabel) {
      const t1 = document.createElementNS("http://www.w3.org/2000/svg", "text");
      t1.setAttribute("x", cx); t1.setAttribute("y", cy - 6);
      t1.setAttribute("text-anchor", "middle");
      t1.setAttribute("font-family", "Geist Mono, monospace");
      t1.setAttribute("font-size", "11");
      t1.setAttribute("fill", "#8a94a8");
      t1.setAttribute("letter-spacing", "0.1em");
      t1.textContent = (opts.centerLabel || "").toUpperCase();
      svg.appendChild(t1);
    }
    if (opts.centerValue) {
      const t2 = document.createElementNS("http://www.w3.org/2000/svg", "text");
      t2.setAttribute("x", cx); t2.setAttribute("y", cy + 14);
      t2.setAttribute("text-anchor", "middle");
      t2.setAttribute("font-family", "Plus Jakarta Sans, sans-serif");
      t2.setAttribute("font-size", "20");
      t2.setAttribute("font-weight", "700");
      t2.setAttribute("fill", "#dde3ef");
      t2.textContent = opts.centerValue;
      svg.appendChild(t2);
    }
  }

  // ── stacked area chart (invested vs total over time) ────────────────
  // series = [{ label, color, values: [n0, n1, ...] }]  (same length)
  function drawStackedArea(svg, series, opts) {
    opts = opts || {};
    const W = opts.width || 520;
    const H = opts.height || 220;
    const padL = 36, padR = 12, padT = 14, padB = 22;
    const innerW = W - padL - padR, innerH = H - padT - padB;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.innerHTML = "";
    if (!series.length || !series[0].values.length) return;
    const n = series[0].values.length;
    const stacked = [];
    for (let i = 0; i < n; i++) {
      let sum = 0; const cell = [];
      for (let s = 0; s < series.length; s++) {
        sum += Number(series[s].values[i]) || 0;
        cell.push(sum);
      }
      stacked.push(cell);
    }
    const maxV = stacked.reduce((m, row) => Math.max(m, row[row.length - 1]), 1);
    function xAt(i) { return padL + (n === 1 ? 0 : (i / (n - 1)) * innerW); }
    function yAt(v) { return padT + innerH * (1 - v / maxV); }
    // Grid
    for (let k = 0; k < 4; k++) {
      const y = padT + (innerH * k / 3);
      const g = document.createElementNS("http://www.w3.org/2000/svg", "line");
      g.setAttribute("x1", padL); g.setAttribute("x2", W - padR);
      g.setAttribute("y1", y); g.setAttribute("y2", y);
      g.setAttribute("stroke", "#253040"); g.setAttribute("stroke-dasharray", "2 4");
      svg.appendChild(g);
    }
    // Stacked layers — bottom up
    for (let s = 0; s < series.length; s++) {
      let d = `M ${xAt(0)} ${yAt(s === 0 ? 0 : stacked[0][s - 1])}`;
      for (let i = 0; i < n; i++) d += ` L ${xAt(i)} ${yAt(stacked[i][s])}`;
      for (let i = n - 1; i >= 0; i--) {
        d += ` L ${xAt(i)} ${yAt(s === 0 ? 0 : stacked[i][s - 1])}`;
      }
      d += " Z";
      const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
      p.setAttribute("d", d);
      p.setAttribute("fill", series[s].color);
      p.setAttribute("fill-opacity", "0.85");
      svg.appendChild(p);
    }
    // X-axis tick labels (year markers)
    const labels = opts.xLabels || [];
    if (labels.length) {
      const step = Math.max(1, Math.floor(n / Math.min(labels.length, 6)));
      for (let i = 0; i < n; i += step) {
        const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
        t.setAttribute("x", xAt(i));
        t.setAttribute("y", H - 6);
        t.setAttribute("text-anchor", "middle");
        t.setAttribute("font-family", "Geist Mono, monospace");
        t.setAttribute("font-size", "10");
        t.setAttribute("fill", "#8a94a8");
        t.textContent = String(labels[i] != null ? labels[i] : i);
        svg.appendChild(t);
      }
    }
  }

  // ── simple vertical bars ─────────────────────────────────────────────
  function drawBars(svg, data, opts) {
    opts = opts || {};
    const W = opts.width || 520;
    const H = opts.height || 220;
    const padL = 36, padR = 12, padT = 14, padB = 22;
    const innerW = W - padL - padR, innerH = H - padT - padB;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.innerHTML = "";
    const n = data.length || 1;
    const maxV = Math.max(1, ...data.map(d => Number(d.value) || 0));
    const gap = 4;
    const bw = Math.max(2, (innerW - gap * (n - 1)) / n);
    data.forEach((d, i) => {
      const v = Number(d.value) || 0;
      const h = innerH * (v / maxV);
      const x = padL + i * (bw + gap);
      const y = padT + innerH - h;
      const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      r.setAttribute("x", x); r.setAttribute("y", y);
      r.setAttribute("width", bw); r.setAttribute("height", h);
      r.setAttribute("rx", Math.min(3, bw / 3));
      r.setAttribute("fill", d.color || "#2dd4aa");
      svg.appendChild(r);
    });
  }

  // ── FAQ rich snippet injection ──────────────────────────────────────
  function mountFAQ(faqs) {
    if (!faqs || !faqs.length) return;
    const ld = {
      "@context": "https://schema.org",
      "@type": "FAQPage",
      mainEntity: faqs.map(f => ({
        "@type": "Question",
        name: f.q,
        acceptedAnswer: { "@type": "Answer", text: f.a },
      })),
    };
    const s = document.createElement("script");
    s.type = "application/ld+json";
    s.textContent = JSON.stringify(ld);
    document.head.appendChild(s);
  }

  // ── SoftwareApplication schema for the calc itself ─────────────────
  function mountSoftwareSchema(meta) {
    if (!meta) return;
    const ld = {
      "@context": "https://schema.org",
      "@type": "WebApplication",
      name: meta.name,
      description: meta.description,
      applicationCategory: "FinanceApplication",
      operatingSystem: "Any",
      offers: { "@type": "Offer", price: "0", priceCurrency: "INR" },
    };
    const s = document.createElement("script");
    s.type = "application/ld+json";
    s.textContent = JSON.stringify(ld);
    document.head.appendChild(s);
  }

  window.TKTools = {
    formatINR, formatINRShort, formatPct, parseNumber,
    bindSlider, animateNumber,
    drawDonut, drawStackedArea, drawBars,
    mountFAQ, mountSoftwareSchema,
  };
})();
