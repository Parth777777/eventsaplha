"""
EventAlpha Scraper Configuration
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
    # --- Regulatory ---
    'rbi_press': 'https://www.rbi.org.in/en/web/rss',
    # --- Additional breaking feeds ---
    'financialexp_markets': 'https://www.financialexpress.com/market/feed/',
    'bloombergquint': 'https://www.bqprime.com/stories.rss',
    'reuters_india_business': 'https://feeds.reuters.com/reuters/INbusinessNews',
    'thehindu_business': 'https://www.thehindu.com/business/feeder/default.rss',
}

# Google News RSS - per-sector (free, no API key)
GOOGLE_NEWS_QUERIES = [
    'Indian stock market NSE',
    'BSE Sensex Nifty',
    'India earnings results quarterly',
    'RBI SEBI policy India',
    'India merger acquisition deal',
    'India IPO listing',
]

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
               'government', 'budget', 'tax reform'],
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
