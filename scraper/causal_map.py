"""TickerWave causal-impact map.

Static lookup tables that connect commodity moves and macro/policy events
to Indian-equity sector beneficiaries and victims. The result is a small
analytical takeaway returned alongside raw data, so the UI can show
"crude up → aviation pressure" rather than only "Brent +3%".

This is intentionally NOT an ML model. The mappings here are well-known
sector linkages an institutional desk would recite from memory:
they encode WHY a move matters, not WHETHER it happens.

Two public entry points:

    commodity_implication(name, direction) -> dict | None
        Lookup for a named commodity (gold / crude / copper / ...) and
        direction ('up' | 'down' | 'flat'). Returns:
            {summary, helps[], hurts[], driver}

    event_implication(text) -> dict | None
        Scans event text for known macro/policy triggers (rate cut,
        PLI push, weak rupee, infra spend, etc.) and returns the same
        shape.

Both are pure functions, no IO.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Commodity → equity impact
# ---------------------------------------------------------------------------
# Keyed by a normalized commodity id (matches scraper/api_v2.py catalogue).

_COMMODITY_MAP: Dict[str, Dict[str, dict]] = {
    "crude": {
        "up": {
            "summary": "Crude oil spike pressures aviation, paints and chemical margins; supports upstream energy producers and oilfield services.",
            "helps":   ["Upstream Oil & Gas", "ONGC", "Oil India", "Selan Exploration"],
            "hurts":   ["Aviation", "Paints", "Specialty Chemicals", "Tyres", "OMCs", "Logistics"],
            "driver":  "input-cost shock for downstream consumers, realisation gain for upstream",
        },
        "down": {
            "summary": "Crude weakness eases input pressure on aviation, paints and chemicals; oilfield/upstream realisations compress.",
            "helps":   ["Aviation", "Paints", "Tyres", "OMCs", "Logistics", "FMCG"],
            "hurts":   ["ONGC", "Oil India", "Reliance E&P"],
            "driver":  "lower feedstock cost flows to downstream margin",
        },
    },
    "natural gas": {
        "up": {
            "summary": "Gas price spike squeezes CGD margins and fertilizer feedstock costs; LNG importers face under-recovery.",
            "helps":   ["GAIL", "Petronet LNG (volume tailwind)"],
            "hurts":   ["IGL", "MGL", "GSPL", "Fertilizers (gas-based)", "Specialty Chemicals"],
            "driver":  "gas as feedstock; passthrough lags",
        },
        "down": {
            "summary": "Cheaper gas improves CGD margins and fertilizer subsidy math; LNG importers regain pricing flex.",
            "helps":   ["IGL", "MGL", "Fertilizers", "Glass / Ceramics"],
            "hurts":   ["GAIL (transmission spread compression)"],
            "driver":  "feedstock relief",
        },
    },
    "gold": {
        "up": {
            "summary": "Gold rally aids jewellers' inventory gains but signals risk-off — defensives and import dependents to watch.",
            "helps":   ["Titan", "Kalyan Jewellers", "Senco", "Manappuram", "Muthoot Finance"],
            "hurts":   ["Discretionary retail (gold-substitution)"],
            "driver":  "inventory revaluation + loan-against-gold LTV uplift",
        },
        "down": {
            "summary": "Gold weakness compresses jeweller inventory and gold-loan LTV; broader risk-on supportive of cyclicals.",
            "helps":   ["Cyclicals broadly"],
            "hurts":   ["Titan", "Kalyan Jewellers", "Manappuram", "Muthoot Finance"],
            "driver":  "LTV reset, inventory loss",
        },
    },
    "silver": {
        "up": {
            "summary": "Silver strength helps producers and solar-cell consumers face cost pressure; industrial demand read-through.",
            "helps":   ["Hindustan Zinc", "Vedanta"],
            "hurts":   ["Solar Module Makers", "Electronics PCB"],
            "driver":  "by-product silver in zinc/lead mining; PV-cell input cost",
        },
        "down": {
            "summary": "Silver weakness pressures by-product margins for zinc/lead miners but eases solar-PV cell costs.",
            "helps":   ["Solar Module Makers"],
            "hurts":   ["Hindustan Zinc", "Vedanta"],
            "driver":  "by-product realisation drop",
        },
    },
    "copper": {
        "up": {
            "summary": "Copper rally lifts integrated miner realisations; EV/grid/cable consumers absorb cost.",
            "helps":   ["Hindalco", "Vedanta", "Hindustan Copper"],
            "hurts":   ["Cables & Wires", "Capital Goods (transformers, motors)", "EV Components"],
            "driver":  "realisation gain offset by downstream margin pressure",
        },
        "down": {
            "summary": "Copper weakness pressures miner P&L; cables, EV components, capital goods see input relief.",
            "helps":   ["Polycab", "KEI", "Havells", "Capital Goods", "EV Components"],
            "hurts":   ["Hindalco", "Vedanta", "Hindustan Copper"],
            "driver":  "feedstock cost relief downstream",
        },
    },
    "aluminium": {
        "up": {
            "summary": "Aluminium spike lifts integrated producers; auto/packaging/cables face metal cost inflation.",
            "helps":   ["Hindalco", "NALCO", "Vedanta"],
            "hurts":   ["Auto OEMs (aluminium body)", "Packaging", "Cables"],
            "driver":  "LME-linked realisation expansion",
        },
        "down": {
            "summary": "Aluminium weakness compresses producer EBITDA; packaging and auto component margins improve.",
            "helps":   ["Packaging", "Auto Components", "Cables"],
            "hurts":   ["Hindalco", "NALCO", "Vedanta"],
            "driver":  "spread compression",
        },
    },
    "zinc": {
        "up": {
            "summary": "Zinc rally lifts Hindustan Zinc; galvanized steel and battery consumers see input cost rise.",
            "helps":   ["Hindustan Zinc", "Vedanta"],
            "hurts":   ["Galvanized Steel processors", "Battery (zinc-carbon)"],
            "driver":  "LME zinc realisation",
        },
        "down": {
            "summary": "Zinc weakness pressures HZ margins; galvanized steel and battery makers gain cost flexibility.",
            "helps":   ["Galvanized Steel", "Battery"],
            "hurts":   ["Hindustan Zinc", "Vedanta"],
            "driver":  "realisation compression",
        },
    },
    "cotton": {
        "up": {
            "summary": "Cotton price spike inflates spinner/weaver raw material costs; upstream ginners benefit briefly.",
            "helps":   ["Cotton Ginners"],
            "hurts":   ["Cotton Spinners (Vardhman, Trident)", "Textiles (Welspun, KPR Mill)", "Garment Exporters"],
            "driver":  "raw material > finished goods price pass-through lag",
        },
        "down": {
            "summary": "Cotton softness expands spinner and integrated-textile margins; ginners absorb realisation drop.",
            "helps":   ["Vardhman Textiles", "Trident", "Welspun India", "KPR Mill"],
            "hurts":   ["Cotton Ginners"],
            "driver":  "margin expansion in mid-stream",
        },
    },
    "soybean": {
        "up": {
            "summary": "Soybean / soy-oil rally lifts solvent extractors but pressures FMCG / edible oil refiners.",
            "helps":   ["Solvent Extractors", "Ruchi Soya / Patanjali Foods (integrated)"],
            "hurts":   ["Edible Oil Refiners", "FMCG (oil-input dependent)"],
            "driver":  "seed-vs-oil price spread",
        },
        "down": {
            "summary": "Soybean weakness compresses extractor realisations; edible-oil refiners and FMCG gain.",
            "helps":   ["Edible Oil Refiners", "FMCG"],
            "hurts":   ["Solvent Extractors"],
            "driver":  "feedstock cost ease",
        },
    },
    "sugar": {
        "up": {
            "summary": "Sugar rally lifts integrated sugar mills with ethanol blending optionality; FMCG/beverage cost up.",
            "helps":   ["Balrampur Chini", "Dalmia Bharat Sugar", "Dwarikesh Sugar", "EID Parry"],
            "hurts":   ["Beverages", "Confectionery"],
            "driver":  "realisation + ethanol blend math",
        },
        "down": {
            "summary": "Sugar weakness compresses mill realisations and ethanol economics; beverage / confectionery input ease.",
            "helps":   ["Beverages", "Confectionery"],
            "hurts":   ["Balrampur Chini", "Dalmia Sugar", "EID Parry"],
            "driver":  "realisation compression",
        },
    },
    "wheat": {
        "up": {
            "summary": "Wheat price spike inflates flour mill, biscuit and packaged food input cost; agri inputs benefit indirectly.",
            "helps":   ["Agri Inputs"],
            "hurts":   ["Britannia", "Nestle", "ITC Foods", "Flour Mills"],
            "driver":  "input-cost passthrough lag",
        },
        "down": {
            "summary": "Wheat softness eases input cost for biscuit / packaged food makers; flour mills see margin expansion.",
            "helps":   ["Britannia", "Nestle", "ITC Foods", "Flour Mills"],
            "hurts":   ["Agri Input Dealers"],
            "driver":  "feedstock relief",
        },
    },
    "corn": {
        "up": {
            "summary": "Corn / maize rally lifts poultry feed cost; protein producers face margin compression.",
            "helps":   ["Maize Procurement Cos"],
            "hurts":   ["Poultry (Venky's, Suguna)", "Animal Feed", "Starch Producers"],
            "driver":  "feed cost inflation",
        },
        "down": {
            "summary": "Corn weakness eases poultry feed cost; protein producers, starch makers expand margin.",
            "helps":   ["Poultry", "Animal Feed", "Starch Producers"],
            "hurts":   ["Maize Procurement Cos"],
            "driver":  "feed cost relief",
        },
    },
    "coffee": {
        "up": {
            "summary": "Coffee rally aids exporters and integrated growers; QSR / specialty cafés face input cost rise.",
            "helps":   ["CCL Products", "Tata Coffee (now part of Tata Cons.)"],
            "hurts":   ["QSR (Jubilant)", "Cafe chains"],
            "driver":  "bean realisation expansion",
        },
        "down": {
            "summary": "Coffee weakness compresses grower / exporter margin; QSR and packaged-coffee margins expand.",
            "helps":   ["QSR", "Packaged Coffee FMCG"],
            "hurts":   ["CCL Products"],
            "driver":  "realisation compression",
        },
    },
    "dxy": {
        "up": {
            "summary": "Dollar strength typically risk-off for EM; weighs on rupee, supports IT/pharma exporters.",
            "helps":   ["IT Exporters", "Pharma Exporters"],
            "hurts":   ["Importers", "FII-heavy financials", "Gold-loan NBFCs (sentiment)"],
            "driver":  "EM risk premium, FX translation",
        },
        "down": {
            "summary": "Dollar weakness aids EM equities and rupee; importers and FII-favoured names benefit, exporters see FX headwind.",
            "helps":   ["Importers (oil, electronics)", "FII-heavy financials"],
            "hurts":   ["IT Exporters", "Pharma Exporters"],
            "driver":  "EM risk-on, FX translation reverse",
        },
    },
    "usdinr": {
        "up": {  # rupee weakening
            "summary": "Rupee weakness supports IT and pharma export realisations; oil importers and foreign-debt borrowers absorb cost.",
            "helps":   ["IT Services", "Pharma Exporters", "Specialty Chem Exporters"],
            "hurts":   ["OMCs", "Airlines", "Electronics Importers", "ForEx-Debt corporates"],
            "driver":  "translation gain for USD revenue / loss on USD costs",
        },
        "down": {  # rupee strengthening
            "summary": "Rupee strength aids oil importers and ForEx-debt holders; IT/pharma exporters face translation drag.",
            "helps":   ["OMCs", "Airlines", "Electronics Importers"],
            "hurts":   ["IT Services", "Pharma Exporters"],
            "driver":  "translation drag for USD revenue",
        },
    },
}


# Aliases — map common spellings to the canonical key.
_COMMODITY_ALIASES = {
    "brent": "crude",
    "wti": "crude",
    "crudeoil": "crude",
    "crude oil": "crude",
    "natural_gas": "natural gas",
    "naturalgas": "natural gas",
    "lng": "natural gas",
    "aluminum": "aluminium",
    "rupee": "usdinr",
    "usd_inr": "usdinr",
    "dollar_index": "dxy",
}


def _normalize_commodity(name: str) -> str:
    if not name:
        return ""
    key = name.lower().strip()
    return _COMMODITY_ALIASES.get(key, key)


def commodity_implication(name: str, direction: str = "up") -> Optional[dict]:
    """Return analytical takeaway for a commodity move.

    Args:
        name: commodity name (case-insensitive, accepts aliases).
        direction: 'up' / 'down' / 'flat'. 'flat' returns None — no narrative
                   for sub-noise moves.
    Returns:
        dict with summary / helps / hurts / driver — or None when no entry.
    """
    if not direction or direction == "flat":
        return None
    key = _normalize_commodity(name)
    entry = _COMMODITY_MAP.get(key)
    if not entry:
        return None
    direction = "down" if direction.lower() in ("down", "fall", "drop", "-") else "up"
    return entry.get(direction)


def commodity_implication_from_change(name: str, change_pct: Optional[float],
                                      flat_threshold: float = 0.6) -> Optional[dict]:
    """Convenience: resolve direction from percentage change."""
    if change_pct is None:
        return None
    if abs(change_pct) < flat_threshold:
        return None
    return commodity_implication(name, "up" if change_pct > 0 else "down")


# ---------------------------------------------------------------------------
# Macro / policy event → sector beneficiaries
# ---------------------------------------------------------------------------

_EVENT_RULES: List[tuple] = [
    # (keyword regex, summary, helps, hurts, driver)
    (re.compile(r"\b(?:repo\s+rate\s+cut|rbi\s+cuts?\s+(?:rate|repo)|rate\s+cut)\b", re.IGNORECASE),
     "Rate cut — lower discount rate and EMI math supports rate-sensitive cyclicals.",
     ["Banks (PSU + private)", "NBFCs", "Auto OEMs", "Realty", "Consumer Durables"],
     ["Insurance (yield reset)"],
     "lower repo flows to lending rates and asset prices"),

    (re.compile(r"\b(?:repo\s+rate\s+hike|rbi\s+hikes?\s+(?:rate|repo)|rate\s+hike)\b", re.IGNORECASE),
     "Rate hike — higher discount rate pressures duration assets; banks gain on NIMs.",
     ["Private Banks (NIM expansion)", "Cash-rich NBFCs"],
     ["Real Estate", "Auto OEMs", "Consumer Durables", "Long-duration debt"],
     "cost of funds reset"),

    (re.compile(r"\b(?:pli\s+scheme|production[-\s]linked\s+incentive)\b", re.IGNORECASE),
     "PLI scheme — capex incentive for domestic manufacturing; sector winners absorb subsidy.",
     ["Specialty Chem", "Electronics Manufacturing", "Solar Modules", "Pharma APIs", "Auto components"],
     ["Imports (intent to displace)"],
     "fiscal subsidy, import substitution"),

    (re.compile(r"\b(?:infrastructure\s+push|infra\s+capex|infra\s+spend|capex\s+budget|"
                r"national\s+infrastructure\s+pipeline|nip\b)", re.IGNORECASE),
     "Infrastructure capex push — EPC, cement, capital goods see orderbook tailwind.",
     ["EPC", "Cement", "Capital Goods", "Construction", "Steel"],
     [],
     "government order pipeline"),

    (re.compile(r"\b(?:defence\s+(?:order|contract|procurement|spend)|mod\s+(?:order|contract)|"
                r"indigenous\s+defence)\b", re.IGNORECASE),
     "Defence order flow — domestic defence PSUs / private vendors benefit from indigenisation push.",
     ["HAL", "BEL", "BDL", "Mazagon Dock", "Cochin Shipyard", "Solar Industries"],
     [],
     "Aatmanirbhar Bharat defence procurement"),

    (re.compile(r"\b(?:railway(?:s)?\s+(?:order|contract|capex|tender|vande\s+bharat)|"
                r"mission\s+raftaar)\b", re.IGNORECASE),
     "Railway capex flow — rolling stock, signalling and track vendors see orderbook visibility.",
     ["Titagarh Rail", "Texmaco Rail", "BEML", "Jupiter Wagons", "RVNL", "IRCON", "RailTel"],
     [],
     "Indian Railways capex push"),

    (re.compile(r"\b(?:transmission\s+capex|power\s+transmission|grid\s+expansion|"
                r"national\s+monetisation\s+pipeline)\b", re.IGNORECASE),
     "Power transmission capex — grid EPC and equipment vendors see multi-year tailwind.",
     ["Power Grid", "KEC International", "Kalpataru Projects", "Siemens", "ABB India", "Apar Industries"],
     [],
     "grid expansion order flow"),

    (re.compile(r"\b(?:solar\s+(?:tender|capacity|capex)|renewable\s+(?:capacity|target|push)|"
                r"green\s+hydrogen|electrolyser)\b", re.IGNORECASE),
     "Renewable / green energy push — module, EPC, and green-hydrogen value-chain beneficiaries.",
     ["Waaree", "Premier Energies", "Adani Green", "JSW Energy", "Tata Power", "Inox Wind"],
     ["Thermal-only producers"],
     "renewables capacity build-out"),

    (re.compile(r"\b(?:export\s+ban|export\s+duty|export\s+restriction)\b", re.IGNORECASE),
     "Export restriction — domestic pricing softens for the curbed commodity; downstream consumers benefit.",
     ["Downstream consumers (case-specific)"],
     ["Upstream exporters (case-specific)"],
     "supply gets channeled domestically"),

    (re.compile(r"\b(?:gst\s+(?:cut|reduced|rationalisation)|customs?\s+duty\s+cut|import\s+duty\s+cut)\b",
                re.IGNORECASE),
     "Tax / duty cut — demand stimulus and margin tailwind for the targeted sector.",
     ["Demand-elastic consumer goods", "Auto", "FMCG"],
     [],
     "lower indirect tax expands demand"),

    (re.compile(r"\b(?:gst\s+(?:hike|increase|rate\s+raised)|customs?\s+duty\s+hike|"
                r"import\s+duty\s+(?:hike|raised))\b", re.IGNORECASE),
     "Tax / duty hike — protection for domestic producer, demand drag for end-consumer.",
     ["Domestic producers (case-specific)"],
     ["End-consumer (case-specific)"],
     "tax wall reduces import competition"),

    (re.compile(r"\b(?:weak\s+rupee|rupee\s+(?:falls?|depreciat|hits?\s+low|tumbles?)|"
                r"depreciat(?:ing|ion)\s+rupee)\b", re.IGNORECASE),
     "Rupee weakness — IT, pharma exporters gain on translation; oil importers, ForEx-debt holders hit.",
     ["IT Services", "Pharma Exporters", "Specialty Chem Exporters"],
     ["OMCs", "Airlines", "Electronics Importers", "ForEx-Debt corporates"],
     "USD translation differential"),

    (re.compile(r"\b(?:strong\s+rupee|rupee\s+(?:rises?|strengthens?|appreciates?))\b", re.IGNORECASE),
     "Rupee strength — oil importers and import-heavy names gain; IT/pharma exporters see headwind.",
     ["OMCs", "Airlines", "Electronics Importers"],
     ["IT Services", "Pharma Exporters"],
     "USD translation reversal"),

    (re.compile(r"\b(?:fed\s+(?:cuts?|hikes?|holds?)|fomc\s+(?:decision|meeting))\b", re.IGNORECASE),
     "Fed action — global risk premium reset; EM flows and INR carry-trade response are the proximate channel.",
     ["EM Equities (on dovish)"],
     ["EM Equities (on hawkish)"],
     "global liquidity / dollar dynamics"),

    (re.compile(r"\b(?:opec\s*\+?\s+(?:cuts?|extends?\s+cut|production\s+cut))\b", re.IGNORECASE),
     "OPEC supply cut — bid for crude; downstream consumers face pressure, upstream gains.",
     ["ONGC", "Oil India"],
     ["Aviation", "Paints", "Specialty Chem", "OMCs"],
     "production curb tightens crude balance"),
]


def event_implication(text: str) -> Optional[dict]:
    """Match the event text against the macro-event rules. First hit wins."""
    if not text:
        return None
    for pattern, summary, helps, hurts, driver in _EVENT_RULES:
        if pattern.search(text):
            return {
                "summary": summary,
                "helps": list(helps),
                "hurts": list(hurts),
                "driver": driver,
            }
    return None


__all__ = [
    "commodity_implication",
    "commodity_implication_from_change",
    "event_implication",
]
