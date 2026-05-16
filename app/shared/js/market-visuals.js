/* market-visuals.js — premium icons, flags, and accents for commodities &
 * global markets. Pure CSS / SVG so we never use emoji (per design rules).
 *
 * Exposes:
 *   window.MarketVisuals.flag(regionCode, opts)   → HTML chip with stripe-flag
 *   window.MarketVisuals.commodityIcon(id)        → {icon, color, gradient}
 *   window.MarketVisuals.regionAccent(code)       → CSS color
 *
 * regionCode: ISO-3 (USA, GBR, JPN, IND, ...) or legacy (US, UK, IN, GL, EU)
 * commodity id: GOLD, SILVER, COPPER, CRUDEOIL, NATURALGAS, etc.
 */
(function () {
    if (window.MarketVisuals) return;

    // ── Flag definitions (CSS gradient stripes — no emoji, no images) ──
    // Each entry: {label, gradient, accent}.
    // gradient = the small flag chip's background
    // accent  = single brand color used elsewhere (border tint, region tag)
    const FLAGS = {
        USA: {
            label: 'US',
            gradient: 'linear-gradient(180deg,' +
                ' #b22234 0 14.3%, #fff 14.3% 28.6%, #b22234 28.6% 42.9%, #fff 42.9% 57.2%,' +
                ' #b22234 57.2% 71.5%, #fff 71.5% 85.8%, #b22234 85.8% 100%)',
            accent: '#3a6ea8',
        },
        GBR: {
            label: 'UK',
            // Union Jack approximation: blue field + red+white cross
            gradient: 'linear-gradient(45deg, transparent 47%, #fff 47% 53%, transparent 53%),' +
                ' linear-gradient(-45deg, transparent 47%, #fff 47% 53%, transparent 53%),' +
                ' linear-gradient(0deg, transparent 44%, #cf142b 44% 56%, transparent 56%),' +
                ' linear-gradient(90deg, transparent 44%, #cf142b 44% 56%, transparent 56%),' +
                ' #00247d',
            accent: '#00247d',
        },
        JPN: {
            label: 'JP',
            // Red disc on white
            gradient: 'radial-gradient(circle at 50% 50%, #bc002d 0 32%, #fff 33% 100%)',
            accent: '#bc002d',
        },
        CHN: {
            label: 'CN',
            // Red field
            gradient: 'linear-gradient(135deg, #de2910, #b21e0a)',
            accent: '#de2910',
        },
        HKG: {
            label: 'HK',
            // Red field with stylized white center
            gradient: 'radial-gradient(ellipse at 50% 50%, #ffffff 0 18%, #de2910 19% 100%)',
            accent: '#de2910',
        },
        DEU: {
            label: 'DE',
            // Black/red/yellow horizontal
            gradient: 'linear-gradient(180deg, #000 0 33.3%, #dd0000 33.3% 66.6%, #ffce00 66.6% 100%)',
            accent: '#dd0000',
        },
        FRA: {
            label: 'FR',
            // Blue/white/red vertical
            gradient: 'linear-gradient(90deg, #0055a4 0 33.3%, #fff 33.3% 66.6%, #ef4135 66.6% 100%)',
            accent: '#0055a4',
        },
        IND: {
            label: 'IN',
            // Saffron/white/green horizontal
            gradient: 'linear-gradient(180deg, #ff9933 0 33.3%, #ffffff 33.3% 66.6%, #138808 66.6% 100%)',
            accent: '#ff9933',
        },
        RUS: {
            label: 'RU',
            gradient: 'linear-gradient(180deg, #fff 0 33.3%, #0033a0 33.3% 66.6%, #d52b1e 66.6% 100%)',
            accent: '#0033a0',
        },
        SAU: {
            label: 'SA',
            gradient: 'linear-gradient(135deg, #006c35, #004d22)',
            accent: '#006c35',
        },
        IRN: {
            label: 'IR',
            gradient: 'linear-gradient(180deg, #239f40 0 33.3%, #fff 33.3% 66.6%, #da0000 66.6% 100%)',
            accent: '#da0000',
        },
        AUS: {
            label: 'AU',
            gradient: 'linear-gradient(135deg, #00247d 0 60%, #cf142b 60% 100%)',
            accent: '#00247d',
        },
        BRA: {
            label: 'BR',
            gradient: 'radial-gradient(ellipse at 50% 50%, #ffd700 0 35%, #009b3a 36% 100%)',
            accent: '#009b3a',
        },
        CAN: {
            label: 'CA',
            gradient: 'linear-gradient(90deg, #d52b1e 0 25%, #fff 25% 75%, #d52b1e 75% 100%)',
            accent: '#d52b1e',
        },
        KOR: {
            label: 'KR',
            gradient: 'radial-gradient(circle at 50% 50%, #cd2e3a 0 18%, #0047a0 18% 28%, #fff 28% 100%)',
            accent: '#0047a0',
        },
        TWN: {
            label: 'TW',
            gradient: 'linear-gradient(135deg, #fe0000, #c20000)',
            accent: '#fe0000',
        },
        SGP: {
            label: 'SG',
            gradient: 'linear-gradient(180deg, #ed2939 0 50%, #fff 50% 100%)',
            accent: '#ed2939',
        },
        ITA: {
            label: 'IT',
            gradient: 'linear-gradient(90deg, #009246 0 33.3%, #fff 33.3% 66.6%, #ce2b37 66.6% 100%)',
            accent: '#009246',
        },
        ESP: {
            label: 'ES',
            gradient: 'linear-gradient(180deg, #aa151b 0 25%, #f1bf00 25% 75%, #aa151b 75% 100%)',
            accent: '#aa151b',
        },
        CHE: {
            label: 'CH',
            gradient: 'linear-gradient(135deg, #ff0000, #d00000)',
            accent: '#ff0000',
        },
        ISR: {
            label: 'IL',
            gradient: 'linear-gradient(180deg, #fff 0 25%, #0038b8 25% 30%, #fff 30% 70%, #0038b8 70% 75%, #fff 75% 100%)',
            accent: '#0038b8',
        },
        UAE: {
            label: 'AE',
            gradient: 'linear-gradient(180deg, #00732f 0 33.3%, #fff 33.3% 66.6%, #000 66.6% 100%)',
            accent: '#00732f',
        },
        GLB: {
            label: 'GL',
            gradient: 'linear-gradient(135deg, #2c3a52, #1a2030)',
            accent: '#8eb4e0',
        },
        EU: {
            label: 'EU',
            gradient: 'radial-gradient(circle at 50% 50%, #ffd700 0 14%, #003399 15% 100%)',
            accent: '#003399',
        },
    };

    // Aliases (legacy 2-letter codes used in existing code → ISO-3)
    const ALIASES = {
        US: 'USA', UK: 'GBR', JP: 'JPN', CN: 'CHN', HK: 'HKG',
        DE: 'DEU', FR: 'FRA', IN: 'IND', RU: 'RUS', SA: 'SAU',
        AU: 'AUS', BR: 'BRA', CA: 'CAN', KR: 'KOR', TW: 'TWN',
        IT: 'ITA', SG: 'SGP', GL: 'GLB',
    };

    function _resolveCode(code) {
        if (!code) return 'GLB';
        const c = String(code).toUpperCase();
        if (FLAGS[c]) return c;
        if (ALIASES[c]) return ALIASES[c];
        return 'GLB';
    }

    /** Render a flag chip — small rounded rect with the country's stripe pattern.
     *  opts.size: 'sm' | 'md' | 'lg' (default 'md')
     *  opts.label: show 2-letter code beside flag (default true) */
    function flag(code, opts) {
        opts = opts || {};
        const c = _resolveCode(code);
        const f = FLAGS[c];
        const size = opts.size || 'md';
        const dims = size === 'sm' ? '14px' : size === 'lg' ? '24px' : '18px';
        const widthRatio = size === 'sm' ? '20px' : size === 'lg' ? '34px' : '26px';
        const showLabel = opts.label !== false;
        const flagSvg = `
            <span class="mv-flag" style="
                display:inline-block;
                width:${widthRatio};height:${dims};
                border-radius:3px;
                background: ${f.gradient};
                box-shadow:
                    inset 0 0 0 1px rgba(255,255,255,0.10),
                    inset 0 1px 0 rgba(255,255,255,0.10),
                    0 1px 2px rgba(0,0,0,0.4);
                vertical-align: middle;
                flex-shrink: 0;
            "></span>`;
        if (!showLabel) return flagSvg;
        return `<span class="mv-flag-chip" style="display:inline-flex;align-items:center;gap:6px;">
            ${flagSvg}
            <span style="font-family:'Geist Mono',monospace;font-weight:700;font-size:${size==='sm'?'9px':'10.5px'};color:${f.accent};letter-spacing:0.04em;">${f.label}</span>
        </span>`;
    }

    function regionAccent(code) {
        const f = FLAGS[_resolveCode(code)];
        return f ? f.accent : '#8eb4e0';
    }

    // ── Commodity visual identities (premium gradients, brand-color tints) ──
    const COMM = {
        GOLD:        { color:'#d4af37', dark:'#7a5e10', icon:'workspace_premium', tint:'rgba(212,175,55,0.10)' },
        SILVER:      { color:'#c0c8d4', dark:'#7a808a', icon:'auto_awesome',     tint:'rgba(192,200,212,0.10)' },
        COPPER:      { color:'#b87333', dark:'#5e3a18', icon:'electrical_services', tint:'rgba(184,115,51,0.10)' },
        ALUMINIUM:   { color:'#a8b3c2', dark:'#6a727f', icon:'construction',     tint:'rgba(168,179,194,0.10)' },
        ZINC:        { color:'#7c98ae', dark:'#445a6e', icon:'science',          tint:'rgba(124,152,174,0.10)' },
        NICKEL:      { color:'#7a8d96', dark:'#475660', icon:'bolt',             tint:'rgba(122,141,150,0.10)' },
        LEAD:        { color:'#5d6470', dark:'#363a44', icon:'battery_charging_full', tint:'rgba(93,100,112,0.10)' },
        CRUDEOIL:    { color:'#1f2933', dark:'#000',    icon:'local_gas_station', tint:'rgba(31,41,51,0.18)' },
        NATURALGAS:  { color:'#5b9dff', dark:'#1c4a99', icon:'local_fire_department', tint:'rgba(91,157,255,0.10)' },
        BRENT:       { color:'#1f2933', dark:'#000',    icon:'oil_barrel',       tint:'rgba(31,41,51,0.18)' },
        CRUDPALMOIL: { color:'#d97706', dark:'#7a3e02', icon:'eco',              tint:'rgba(217,119,6,0.10)' },
        COTTON:      { color:'#e8e2d4', dark:'#a39b89', icon:'cloud',            tint:'rgba(232,226,212,0.08)' },
        CHANA:       { color:'#a8923c', dark:'#5e5018', icon:'grain',            tint:'rgba(168,146,60,0.10)' },
        WHEAT:       { color:'#f4c430', dark:'#8a6e10', icon:'grass',            tint:'rgba(244,196,48,0.10)' },
        SOYBEAN:     { color:'#88a534', dark:'#4d5e1c', icon:'spa',              tint:'rgba(136,165,52,0.10)' },
        CORN:        { color:'#fbbf24', dark:'#8a6402', icon:'grain',            tint:'rgba(251,191,36,0.10)' },
        MENTHAOIL:   { color:'#10b981', dark:'#065f46', icon:'spa',              tint:'rgba(16,185,129,0.10)' },
        SUGAR:       { color:'#fef3c7', dark:'#92741a', icon:'cookie',           tint:'rgba(254,243,199,0.08)' },
        COFFEE:      { color:'#6f4e37', dark:'#3e2918', icon:'coffee',           tint:'rgba(111,78,55,0.12)' },
        DEFAULT:     { color:'#8a94a8', dark:'#3d4a5c', icon:'inventory_2',     tint:'rgba(138,148,168,0.08)' },
    };

    function commodityIcon(id) {
        const c = COMM[String(id || '').toUpperCase()] || COMM.DEFAULT;
        return c;
    }

    // Inject minimal CSS once (chips + flag aspect)
    if (typeof document !== 'undefined' && !document.getElementById('mv-styles')) {
        const s = document.createElement('style');
        s.id = 'mv-styles';
        s.textContent = `
            .mv-flag-chip { line-height: 1; }
            .mv-comm-badge {
                display: inline-flex; align-items: center; justify-content: center;
                width: 36px; height: 36px; border-radius: 10px;
                background: linear-gradient(135deg,
                    rgba(255,255,255,0.05),
                    rgba(255,255,255,0.01));
                border: 1px solid rgba(255,255,255,0.08);
                box-shadow:
                    inset 0 1px 0 rgba(255,255,255,0.10),
                    0 2px 6px rgba(0,0,0,0.3);
                position: relative; flex-shrink: 0;
            }
            .mv-comm-badge .material-symbols-outlined {
                font-size: 20px;
            }
        `;
        document.head.appendChild(s);
    }

    window.MarketVisuals = { flag, commodityIcon, regionAccent };
})();
