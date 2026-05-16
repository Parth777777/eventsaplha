"""
Tickwave Scraper Configuration
All settings loaded from environment variables with sensible defaults
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / '.env')

# Paths
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR.parent / 'data'
LOG_DIR = BASE_DIR / 'logs'

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ============ DATABASE ============
DATABASE_URL = os.getenv('DATABASE_URL', '')

# ============ GROQ LLM ============
GROQ_API_KEY = os.getenv('GROQ_API_KEY', '')
GROQ_MODEL = os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')
GROQ_BATCH_SIZE = int(os.getenv('GROQ_BATCH_SIZE', '25'))
GROQ_MAX_DAILY_REQUESTS = 14000

# ============ NEWS SOURCES ============

# RSS Feed sources — financial/market feeds for breaking news.
# Keep diverse: NSE/BSE filings, mainstream outlets, and fast-moving feeds.
RSS_SOURCES = {
    # --- Fast-breaking exchange feeds ---
    'bse_announcements': 'https://www.bseindia.com/news/announcementnew.aspx?SubCategoryID=999',
    'nse_corp_announcements': 'https://www.nseindia.com/rss/corporate-announcements',
    'sebi_press': 'https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doRss=yes&rssType=pressreleases',
    # --- ET streams (hourly high-volume) ---
    'et_markets': 'https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms',
    'et_stocks': 'https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms',
    'et_breaking': 'https://economictimes.indiatimes.com/news/business/rssfeeds/1977021501.cms',
    # --- Mint / Moneycontrol / BS ---
    'mint_markets': 'https://www.livemint.com/rss/markets',
    'mint_companies': 'https://www.livemint.com/rss/companies',
    'mint_money': 'https://www.livemint.com/rss/money',
    'moneycontrol_markets': 'https://www.moneycontrol.com/rss/marketreports.xml',
    'moneycontrol_stocks': 'https://www.moneycontrol.com/rss/stocksnews.xml',
    'moneycontrol_results': 'https://www.moneycontrol.com/rss/results.xml',
    'moneycontrol_business': 'https://www.moneycontrol.com/rss/business.xml',
    'bs_markets': 'https://www.business-standard.com/rss/markets-106.rss',
    'bs_companies': 'https://www.business-standard.com/rss/companies-101.rss',
    # --- Regulatory / Government ---
    'rbi_press': 'https://www.rbi.org.in/en/web/rss',
    # PIB (Press Information Bureau) — official source for cabinet
    # decisions, PM speeches, ministry announcements. The .xml endpoint
    # is a public RSS proxy maintained by PIB.
    'pib_releases': 'https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3',
    'pib_features': 'https://pib.gov.in/RssMain.aspx?ModId=7&Lang=1&Regid=3',
    # Finance Ministry via Google News (no native RSS) — captured as a
    # Google News query channel; declared here so policy classification
    # in news_type / source_tiering treats it as official tier.
    'mof_press': 'https://news.google.com/rss/search?q=%22ministry+of+finance%22+india&hl=en-IN&gl=IN&ceid=IN:en',
    'pmo_press': 'https://news.google.com/rss/search?q=%22pmo+india%22+OR+%22prime+minister%22+india+market&hl=en-IN&gl=IN&ceid=IN:en',
    # --- Additional breaking feeds ---
    'financialexp_markets': 'https://www.financialexpress.com/market/feed/',
    'bloombergquint': 'https://www.bqprime.com/stories.rss',
    'reuters_india_business': 'https://feeds.reuters.com/reuters/INbusinessNews',
    'thehindu_business': 'https://www.thehindu.com/business/feeder/default.rss',
    # --- Small / mid cap focused feeds (these often surface obscure tickers) ---
    'mc_smallcap': 'https://www.moneycontrol.com/rss/smallcap.xml',
    'mc_midcap': 'https://www.moneycontrol.com/rss/midcap.xml',
    'mc_ipo': 'https://www.moneycontrol.com/rss/iponews.xml',
    'et_smallcap': 'https://economictimes.indiatimes.com/markets/stocks/news/rssfeeds/2146843.cms',
    'mc_buzzing_stocks': 'https://www.moneycontrol.com/rss/buzzingstocks.xml',
    'mc_recommendations': 'https://www.moneycontrol.com/rss/recommendations.xml',
    'fe_market_cap': 'https://www.financialexpress.com/market/cafeinvest/feed/',
    # --- Stock-specific aggregators ---
    'mc_latest_news': 'https://www.moneycontrol.com/rss/latestnews.xml',
    'mc_corp_action': 'https://www.moneycontrol.com/rss/results.xml',
}

# Google News RSS - per-sector (free, no API key)
# Generic broad queries (run every cycle)
GOOGLE_NEWS_QUERIES = [
    'Indian stock market NSE',
    'BSE Sensex Nifty',
    'India earnings results quarterly',
    'RBI SEBI policy India',
    'India merger acquisition deal',
    'India IPO listing',
    # National / government pulse — every cycle, not rotation. A PM speech
    # or finance-ministry move can crater the index in minutes; if we only
    # see these on a sector-rotation week we miss the catalyst entirely.
    'PM Modi speech market reaction',
    'Prime Minister India policy announcement',
    'Finance Minister Nirmala Sitharaman statement',
    'Union Budget India market impact',
    'RBI MPC repo rate decision',
    'India cabinet decision today',
    'Parliament India bill passed market',
    'GST council meeting decision',
    'India government policy stocks',
    'India market crash today reason',
    'Nifty Sensex fall today',
    # Small/mid cap focused — catches names that mainstream broad queries miss
    'BSE smallcap rally',
    'Nifty smallcap 250 stocks',
    'Nifty midcap 100 movers',
    'multibagger stocks India',
    'India smallcap earnings beat',
    'India microcap result',
    'NSE corporate announcement',
    'India SME IPO listing',
    'Indian stock 52 week high',
    'Indian stock upper circuit',
]

# Sector-rotation queries — broaden coverage to non-largecap-dominated sectors.
# Each cycle picks a few of these on rotation so we don't hit Google rate limits.
SECTOR_ROTATION_QUERIES = [
    'India auto ancillary stocks news',
    'India textile garment exporters NSE',
    'India chemical specialty stocks',
    'India agro chemical fertilizer NSE',
    'India defence aerospace stocks',
    'India hospitality hotels stocks',
    'India real estate developers NSE',
    'India logistics shipping stocks',
    'India paper packaging stocks',
    'India sugar mills NSE',
    'India footwear retail stocks',
    'India electronics EMS stocks',
    'India renewable energy solar wind stocks',
    'India healthcare diagnostics hospital NSE',
    'India media entertainment stocks',
    'India fintech NBFC stocks',
    'India microcap IPO listing recent',
    'India SME platform listing',
    'India sugar exporter quarterly result',
    'India railway PSU stocks',
    'India shipbuilder defence stocks',
    # ── Sector-specific policy / regulator queries ─────────────────
    # These catch policy-level events that move whole sectors but don't
    # name a specific ticker (NPPA pricing → pharma, spectrum auction →
    # telecom, anti-dumping → metals). Without these in the rotation
    # the macro sector signals stay sparse.
    'NPPA pharma drug price ceiling India',
    'TRAI telecom spectrum auction India',
    'India steel import duty anti dumping',
    'India electric vehicle subsidy FAME policy',
    'PLI scheme manufacturing semiconductor India',
    'India ethanol blending policy sugar',
    'India sugar export quota notification',
    'India crude oil import duty refining',
    'India gold import duty jewellery',
    'India real estate RERA notification',
    'India MSME credit guarantee scheme',
    'India agri MSP procurement notification',
    'India fertilizer subsidy notification urea',
    'SEBI margin trading peak margin circular',
    'SEBI mutual fund expense ratio circular',
    'RBI repo rate MPC decision India',
    'RBI banking license NBFC notification',
    'India Union Budget capital gains tax',
    'India infrastructure spending capex announcement',
    'India semiconductor chip incentive announcement',
    # ── State-level industrial policy ─────────────────────────────
    # State governments increasingly drive sector winners (Maharashtra
    # data centres, Karnataka EV, Gujarat semiconductors, TN auto).
    # These were missing from coverage entirely — adding explicit queries.
    'Maharashtra industrial policy investment',
    'Karnataka EV policy electric vehicle',
    'Gujarat semiconductor incentive Dholera',
    'Tamil Nadu electronics manufacturing policy',
    'Telangana data centre policy Hyderabad',
    'Uttar Pradesh logistics warehouse policy',
    'Andhra Pradesh ports infrastructure policy',
    'India state MoU manufacturing investment',
]
GOOGLE_NEWS_SECTOR_PER_CYCLE = int(os.getenv('SECTOR_QUERIES_PER_CYCLE', '5'))

# Per-ticker rotation: each cycle, pull news for N rotating small/mid cap tickers
# This is the biggest fix for "small/mid cap focus" — we explicitly query for
# specific tickers in 60+ name rotation rather than relying on broad queries to
# bubble them up.
TICKER_NEWS_ROTATION = [
    # Mid-cap industrials/cap goods
    'POLYCAB','KEI','HAVELLS','CROMPTON','SUPRIYA','SUPREMEIND','ASTRAL','FINOLEXIND',
    'SCHAEFFLER','TIINDIA','BHARATFORG','KIRLOSENG','AIAENG','TIMKEN','SKFINDIA',
    # Mid-cap pharma/healthcare
    'MANKIND','ALKEM','TORNTPHARM','GLAND','SYNGENE','LAURUSLABS','GRANULES',
    'IPCALAB','AJANTPHARM','NATCOPHARM','METROPOLIS','THYROCARE','RAINBOW','KIMS',
    # Mid-cap chemicals/agri
    'PIIND','SRF','AARTIIND','NAVINFLUOR','DEEPAKNTR','VINATIORGA','FINEORG',
    'COROMANDEL','RALLIS','CHAMBLFERT','GNFC','KAVERISEED','GODREJAGRO',
    # Mid-cap banks/NBFC
    'AUBANK','BANDHANBNK','CSBBANK','RBLBANK','POONAWALLA','SBFC','HOMEFIRST',
    'CHOLAFIN','MASFIN','UJJIVANSFB','EQUITAS',
    # Mid-cap consumer/retail
    'HONASA','GOPAL','BIKAJI','JUBLFOOD','DEVYANI','SAPPHIRE','VBL','RADICO',
    'METROBRAND','CAMPUS','GOFASHION','ETHOS','MANYAVAR','VEDANT','TRENT',
    # Mid-cap IT/tech
    'PERSISTENT','COFORGE','LTTS','MPHASIS','BIRLASOFT','HAPPSTMNDS','TATAELXSI',
    'KPITTECH','CYIENT','NEWGEN','SAKSOFT','MASTEK','OFSS',
    # Small/mid cap auto ancil + EVs
    'ENDURANCE','MINDA','UNOMINDA','SONACOMS','MOTHERSON','CRAFTSMAN','RACL','EXIDE',
    'AMARARAJA','SAMVRDHANA','TVSSCS',
    # Small/mid cap defence/PSU
    'BEL','HAL','BDL','MAZAGON','COCHINSHIP','GRSE','PARAS','DATAPATTNS','ASTRAMICRO',
    'KAYNES','MTAR','TANEJA','PARASDEFEN',
    # Mid-cap power/renewable
    'TORNTPOWER','CESC','NHPC','SJVN','PFC','RECLTD','IREDA','SUZLON','INOXWIND',
    'WAAREE','BORORENEW','ADANIGREEN','JSWENERGY',
    # Mid-cap infra/real estate/cement
    'PRESTIGE','SOBHA','OBEROIRLTY','GODREJPROP','MACROTECH','BRIGADE','SUNTECK',
    'PHOENIXLTD','RVNL','IRCON','RITES','HGINFRA','PNCINFRA','NCC','ASHOKA',
    'ULTRACEMCO','SHREECEM','RAMCOCEM','DALBHARAT','JKCEMENT','HEIDELBERG',
    # Small caps frequently moving
    'CYIENTDLM','APARINDS','NETWEB','SYRMA','TARC','RAILTEL','ITDC','HUDCO',
    'GMDCLTD','MOIL','NLCINDIA','BSE','MCX','CDSL','IEX','ANGELONE','MOTILALOFS',
    # Recent listings
    'JIOFIN','TATATECH','IREDA','MAMAEARTH','HONASA','YATHARTH','CELLO','RKEC',
    'PROTEAN','UTIAMC','UPDATER','RAILTEL','CONCORDBIO','DOMS','NEXUSSELECT',
]
TICKERS_PER_CYCLE = int(os.getenv('TICKERS_PER_CYCLE', '12'))

# Per-feed entries cap — was 10, doubled for fresher catches
GOOGLE_NEWS_ENTRIES_PER_QUERY = int(os.getenv('GOOGLE_NEWS_ENTRIES', '20'))

# NewsAPI (optional, free tier: 100 req/day)
NEWSAPI_KEY = os.getenv('NEWSAPI_KEY', '')
NEWSAPI_QUERY = 'NSE OR BSE OR "Indian stock" OR Nifty OR Sensex'

# ============ STOCKS ============

# Indian stocks to monitor (NSE tickers) - ALL 32
MONITORED_STOCKS = [
    'INFY', 'TCS', 'WIPRO', 'LT', 'RELIANCE', 'HDFCBANK', 'ICICIBANK', 'SBIN',
    'BAJAJFINSV', 'MARUTI', 'TATASTEEL', 'JSWSTEEL', 'ADANIGREEN', 'ADANIPORTS',
    'SUNPHARMA', 'DIVISLAB', 'HINDUNILVR', 'ITC', 'NESTLEIND', 'AXISBANK',
    'KNRCON', 'TECHM', 'HCLTECH', 'BAJAJ-AUTO', 'BHARTIARTL',
    'CIPLA', 'LUPIN', 'POWERGRID', 'NTPC', 'COALINDIA', 'IOC', 'TATAPOWER'
]

# Stock → Company name mapping for display
STOCK_COMPANIES = {
    'INFY': 'Infosys Ltd.', 'TCS': 'Tata Consultancy Services',
    'WIPRO': 'Wipro Ltd.', 'LT': 'Larsen & Toubro',
    'RELIANCE': 'Reliance Industries', 'HDFCBANK': 'HDFC Bank',
    'ICICIBANK': 'ICICI Bank', 'SBIN': 'State Bank of India',
    'BAJAJFINSV': 'Bajaj Finserv', 'MARUTI': 'Maruti Suzuki',
    'TATASTEEL': 'Tata Steel', 'JSWSTEEL': 'JSW Steel',
    'ADANIGREEN': 'Adani Green Energy', 'ADANIPORTS': 'Adani Ports',
    'SUNPHARMA': 'Sun Pharma', 'DIVISLAB': 'Divis Laboratories',
    'HINDUNILVR': 'Hindustan Unilever', 'ITC': 'ITC Ltd.',
    'NESTLEIND': 'Nestle India', 'AXISBANK': 'Axis Bank',
    'KNRCON': 'KNR Constructions', 'TECHM': 'Tech Mahindra',
    'HCLTECH': 'HCL Technologies', 'BAJAJ-AUTO': 'Bajaj Auto',
    'BHARTIARTL': 'Bharti Airtel', 'CIPLA': 'Cipla Ltd.',
    'LUPIN': 'Lupin Ltd.', 'POWERGRID': 'Power Grid Corp',
    'NTPC': 'NTPC Ltd.', 'COALINDIA': 'Coal India',
    'IOC': 'Indian Oil Corp', 'TATAPOWER': 'Tata Power',
}

# Market cap classification
LARGE_CAP = ['RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN', 'HINDUNILVR',
             'ITC', 'BHARTIARTL', 'LT', 'AXISBANK', 'MARUTI', 'BAJAJFINSV',
             'SUNPHARMA', 'NESTLEIND', 'NTPC', 'POWERGRID', 'TATAPOWER']
MID_CAP = ['WIPRO', 'TECHM', 'HCLTECH', 'BAJAJ-AUTO', 'CIPLA', 'LUPIN', 'DIVISLAB',
           'TATASTEEL', 'JSWSTEEL', 'IOC', 'COALINDIA']
SMALL_CAP = ['KNRCON', 'ADANIGREEN', 'ADANIPORTS']

# ============ SECTOR CLASSIFICATION ============
# Stock → Sector mapping (for per-sector momentum and relative strength)
STOCK_SECTORS = {
    # IT Services
    'INFY': 'IT', 'TCS': 'IT', 'WIPRO': 'IT', 'TECHM': 'IT', 'HCLTECH': 'IT',
    # Banking & Finance
    'HDFCBANK': 'BFSI', 'ICICIBANK': 'BFSI', 'SBIN': 'BFSI', 'AXISBANK': 'BFSI', 'BAJAJFINSV': 'BFSI',
    # Energy & Oil
    'RELIANCE': 'ENERGY', 'IOC': 'ENERGY', 'NTPC': 'ENERGY', 'POWERGRID': 'ENERGY',
    'COALINDIA': 'ENERGY', 'ADANIGREEN': 'ENERGY', 'TATAPOWER': 'ENERGY',
    # Metals & Mining
    'TATASTEEL': 'METALS', 'JSWSTEEL': 'METALS',
    # Pharma & Healthcare
    'SUNPHARMA': 'PHARMA', 'CIPLA': 'PHARMA', 'LUPIN': 'PHARMA', 'DIVISLAB': 'PHARMA',
    # Auto
    'MARUTI': 'AUTO', 'BAJAJ-AUTO': 'AUTO',
    # FMCG & Consumer
    'HINDUNILVR': 'FMCG', 'ITC': 'FMCG', 'NESTLEIND': 'FMCG',
    # Infrastructure & Capital Goods
    'LT': 'INFRA', 'KNRCON': 'INFRA', 'ADANIPORTS': 'INFRA',
    # Telecom
    'BHARTIARTL': 'TELECOM',
}

# Reverse: Sector → list of stocks
SECTOR_STOCKS = {}
for _ticker, _sector in STOCK_SECTORS.items():
    SECTOR_STOCKS.setdefault(_sector, []).append(_ticker)

# ============ EVENT CLASSIFICATION ============

EVENT_KEYWORDS = {
    'earnings': ['earnings', 'quarterly results', 'q1', 'q2', 'q3', 'q4',
                 'net profit', 'revenue', 'eps', 'profit after tax', 'PAT',
                 'operating profit', 'EBITDA', 'topline', 'bottomline'],
    'merger': ['merger', 'acquisition', 'acquired', 'merger of equals', 'm&a',
               'takeover', 'buyout', 'demerger', 'amalgamation'],
    'policy': ['rbi', 'sebi', 'regulatory', 'policy', 'amendment', 'circular',
               'notification', 'repo rate', 'monetary policy', 'fiscal policy',
               'government', 'budget', 'tax reform',
               # PM / Cabinet / Parliament — these move the whole tape but
               # the previous keyword set missed them entirely (a PM speech
               # crashing the market would be filed as "news" instead of
               # "policy"). Adding them ensures the macro filter catches them.
               'pm modi', 'prime minister', 'modi speech', 'pmo', 'parliament',
               'lok sabha', 'rajya sabha', 'cabinet decision', 'cabinet approval',
               'finance minister', 'sitharaman', 'nirmala sitharaman', 'fm speech',
               'union budget', 'interim budget', 'gst council', 'niti aayog',
               'mpc decision', 'mpc meeting', 'address to nation', 'pib',
               'press information bureau', 'ministry of finance', 'mof india',
               'ministry of commerce', 'pli scheme', 'gazette notification',
               'ordinance', 'bill passed', 'bill cleared', 'supreme court ruling',
               'cci approval', 'tariff', 'duty hike', 'duty cut', 'subsidy'],
    'order_win': ['order', 'contract', 'bid', 'awarded', 'secured', 'won',
                  'order book', 'order inflow', 'mandate'],
    'dividend': ['dividend', 'bonus', 'stock split', 'interim dividend',
                 'final dividend', 'record date', 'ex-date', 'buyback'],
    'supply': ['supply', 'disruption', 'shortage', 'logistics', 'export',
               'import', 'production', 'capacity expansion', 'plant shutdown'],
    'insider': ['insider', 'promoter', 'founder', 'director', 'shareholding',
                'promoter holding', 'pledge', 'stake sale', 'block deal',
                'bulk deal'],
}

POSITIVE_KEYWORDS = [
    'growth', 'surge', 'rally', 'bullish', 'outperform', 'beat', 'exceed',
    'positive', 'strong', 'upgrade', 'breakout', 'record high', 'expansion',
    'robust', 'impressive', 'above estimate', 'upside', 'momentum'
]

NEGATIVE_KEYWORDS = [
    'decline', 'fall', 'bearish', 'underperform', 'miss', 'weak', 'loss',
    'negative', 'crash', 'downgrade', 'selloff', 'slump', 'below estimate',
    'downside', 'contraction', 'warning', 'default', 'fraud'
]

# ============ MACRO → SECTOR MAPPING ============
# When a news article mentions macro themes but no specific company,
# map it to affected sectors so we can generate sector-wide signals.
# Each macro theme has: keywords to detect, affected sectors, and whether
# the theme is typically bullish or bearish for those sectors.
MACRO_SECTOR_MAP = {
    'oil_surge': {
        'keywords': ['oil price', 'crude oil', 'brent crude', 'oil surge', 'oil rally',
                     'opec cut', 'production cut', 'oil shortage', 'petroleum', 'oil rises'],
        'sectors': {'ENERGY': 'bullish'},  # Oil up → energy stocks up
        'magnitude_boost': 1.2,
    },
    'oil_crash': {
        'keywords': ['oil crash', 'oil slump', 'oil falls', 'crude falls', 'oil price drop',
                     'opec increase', 'production increase', 'oil glut', 'oil oversupply'],
        'sectors': {'ENERGY': 'bearish'},
        'magnitude_boost': 1.2,
    },
    'war_conflict': {
        'keywords': ['war', 'conflict', 'military', 'missile', 'invasion', 'sanctions',
                     'geopolitical tension', 'armed forces', 'defense', 'troops deployed',
                     'ceasefire', 'airstrike', 'nuclear threat', 'escalation'],
        'sectors': {'ENERGY': 'bullish', 'METALS': 'bullish', 'IT': 'bearish', 'AUTO': 'bearish'},
        'magnitude_boost': 1.5,  # Wars are high-impact
    },
    'rate_hike': {
        'keywords': ['rate hike', 'interest rate increase', 'repo rate hike', 'hawkish',
                     'tightening', 'rate increase', 'fed hike', 'rbi hike'],
        'sectors': {'BFSI': 'bullish', 'AUTO': 'bearish', 'INFRA': 'bearish'},
        'magnitude_boost': 1.1,
    },
    'rate_cut': {
        'keywords': ['rate cut', 'interest rate cut', 'repo rate cut', 'dovish',
                     'easing', 'rate reduction', 'fed cut', 'rbi cut', 'accommodative'],
        'sectors': {'BFSI': 'bearish', 'AUTO': 'bullish', 'INFRA': 'bullish'},
        'magnitude_boost': 1.1,
    },
    'recession_fear': {
        'keywords': ['recession', 'economic slowdown', 'gdp contraction', 'stagflation',
                     'unemployment rise', 'layoffs', 'economic crisis', 'downturn'],
        'sectors': {'FMCG': 'bullish', 'IT': 'bearish', 'AUTO': 'bearish', 'METALS': 'bearish'},
        'magnitude_boost': 1.3,
    },
    'inflation': {
        'keywords': ['inflation spike', 'cpi rise', 'wpi surge', 'cost of living',
                     'food inflation', 'fuel price hike', 'commodity inflation'],
        'sectors': {'FMCG': 'bearish', 'ENERGY': 'bullish', 'METALS': 'bullish'},
        'magnitude_boost': 1.0,
    },
    'rupee_weakness': {
        'keywords': ['rupee falls', 'rupee depreciation', 'inr weak', 'dollar surge',
                     'forex reserves decline', 'capital outflow'],
        'sectors': {'IT': 'bullish', 'PHARMA': 'bullish', 'AUTO': 'bearish'},
        'magnitude_boost': 1.0,
    },
    'china_risk': {
        'keywords': ['china slowdown', 'china covid', 'china lockdown', 'taiwan strait',
                     'china tariff', 'trade war', 'china ban', 'china dumping'],
        'sectors': {'METALS': 'bearish', 'PHARMA': 'bullish', 'IT': 'neutral'},
        'magnitude_boost': 1.2,
    },
    'infra_push': {
        'keywords': ['infrastructure spending', 'highway project', 'smart city',
                     'railway expansion', 'metro project', 'government capex',
                     'public infrastructure', 'roads', 'bridges', 'construction boom'],
        'sectors': {'INFRA': 'bullish', 'METALS': 'bullish'},
        'magnitude_boost': 1.1,
    },
    'tech_boom': {
        'keywords': ['ai boom', 'artificial intelligence', 'cloud computing',
                     'digital transformation', 'tech spending', 'semiconductor demand',
                     'it outsourcing', 'saas growth'],
        'sectors': {'IT': 'bullish'},
        'magnitude_boost': 1.1,
    },
}

# ============ GEO EVENT KEYWORDS ============
# Geo keywords require 2+ matches to tag (enforced in classify_geo_event)
# Use specific geopolitical terms, not generic words
GEO_KEYWORDS = {
    'India': {'lat': 20.5937, 'lng': 78.9629, 'keywords': [
        'india', 'indian economy', 'rbi policy', 'sebi regulation', 'nifty crash',
        'sensex crash', 'indian market', 'mumbai exchange', 'indian rupee',
        'modi government', 'india gdp', 'indian exports']},
    'United States': {'lat': 37.0902, 'lng': -95.7129, 'keywords': [
        'federal reserve', 'us economy', 'wall street', 'nasdaq', 'dow jones',
        'us inflation', 'us recession', 'washington', 'white house', 'us congress',
        'us treasury', 'us stocks', 'american economy', 'us tariff']},
    'China': {'lat': 35.8617, 'lng': 104.1954, 'keywords': [
        'china economy', 'chinese market', 'shanghai index', 'pboc',
        'china trade', 'beijing', 'chinese exports', 'china gdp',
        'hong kong', 'china slowdown', 'china stimulus']},
    'Europe': {'lat': 50.1109, 'lng': 8.6821, 'keywords': [
        'european central bank', 'ecb rate', 'eurozone', 'ftse',
        'european economy', 'eu regulation', 'brexit', 'germany economy',
        'european market', 'eu trade']},
    'Japan': {'lat': 36.2048, 'lng': 138.2529, 'keywords': [
        'bank of japan', 'nikkei', 'japanese yen', 'japan economy',
        'tokyo stock', 'japan trade', 'yen carry trade']},
    'Middle East': {'lat': 25.2048, 'lng': 55.2708, 'keywords': [
        'opec', 'crude oil', 'oil price', 'saudi arabia', 'iran nuclear',
        'middle east tension', 'gulf crisis', 'iraq conflict', 'yemen',
        'houthi', 'oil supply', 'brent crude', 'oil embargo']},
    'Russia': {'lat': 61.524, 'lng': 105.3188, 'keywords': [
        'russia ukraine', 'russian sanctions', 'moscow', 'putin',
        'russian economy', 'kremlin', 'nord stream', 'russian oil']},
    'Taiwan': {'lat': 23.6978, 'lng': 120.9605, 'keywords': [
        'taiwan strait', 'tsmc', 'taiwan tension', 'taipei',
        'taiwan semiconductor', 'china taiwan']},
    'South Korea': {'lat': 35.9078, 'lng': 127.7669, 'keywords': [
        'south korea economy', 'kospi', 'korean market', 'samsung',
        'north korea', 'korean semiconductor']},
    'Brazil': {'lat': -14.235, 'lng': -51.9253, 'keywords': [
        'brazil economy', 'bovespa', 'brazilian real', 'brazil commodity']},
    'Australia': {'lat': -25.2744, 'lng': 133.7751, 'keywords': [
        'australia economy', 'rba rate', 'australian dollar', 'iron ore australia']},
}

# ============ SCRAPER SETTINGS ============
YFINANCE_TIMEOUT = 10
REQUEST_TIMEOUT = 15
# Default lowered to 3 min to break news faster. Override via SCRAPER_INTERVAL env.
UPDATE_INTERVAL = int(os.getenv('SCRAPER_INTERVAL', '3')) * 60  # Convert to seconds
MAX_ARTICLES_PER_SOURCE = 25
BATCH_SIZE = 10  # Stocks to fetch at once with yfinance

# ============ NOTIFICATION SETTINGS ============
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# ============ OUTPUT ============
OUTPUT_FILE = DATA_DIR / 'market_data.json'
BACKUP_FILE = DATA_DIR / 'market_data_backup.json'

# ============ LOGGING ============
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
LOG_FILE = LOG_DIR / 'scraper.log'
