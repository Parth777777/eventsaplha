"""
EventAlpha - Complete NSE/BSE Stock Universe
Downloads the full equity list from NSE (~2300 stocks) and caches locally.
Used for: entity extraction from news + frontend search autocomplete.
"""

import os
import json
import logging
import requests
from pathlib import Path
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / 'data'
CACHE_FILE = DATA_DIR / 'nse_stocks.json'
CACHE_MAX_AGE_HOURS = 168  # Refresh weekly

# Sector classification by industry keywords.
# Order matters — first match wins, so put most-specific buckets earliest.
# Many more sectors and keywords than before, drastically reducing OTHER.
SECTOR_MAP = {
    # IT / TECH services + software (broad: catch "tech", "techno", "infotech", "systems", "consult")
    'IT': [
        'software', 'computer', 'information tech', 'infotech', 'info-tech',
        'digital', 'cloud', 'saas', 'data', 'analytics', 'cyber', 'security',
        'consultancy', 'consultanc', 'consulting', 'consultants',
        'tcs', 'infosys', 'wipro', 'mindtree', 'mphasi', 'persistent',
        'tech mahindra', 'hexaware', 'coforge', 'birlasoft', 'happiest minds',
        'l&t infotech', 'ltimindtree', 'ltts',
        'systems', 'system', 'solut', 'technologies', 'techno', 'webx',
        'e-clerx', 'eclerx', 'sonata',
    ],
    # New-age / internet / fintech platforms
    'TECH': [
        'platform', 'startup', 'internet', 'online', 'app', 'mobile',
        'paytm', 'zomato', 'nykaa', 'policybazaar', 'pb fintech', 'easy trip',
        'cartrade', 'mapmyindia', 'delhivery', 'ola', 'oyo', 'mobikwik',
        'ixigo', 'tamilnad', 'fintech',
    ],
    # Banks, NBFCs, insurance, broking, AMCs
    'BFSI': [
        'bank', 'banking', 'financ', 'insurance', 'assurance', 'credit',
        'housing finance', 'home finance', 'capital', 'securities', 'broker',
        'nidhi', 'nbfc', 'wealth', 'asset management', 'mutual fund', 'amc',
        'depository', 'exchange', 'investment', 'leasing',
        'hdfc', 'icici', 'axis', 'kotak', 'sbi', 'idfc', 'pnb', 'iob', 'rbl',
        'bajaj fin', 'bajaj hold', 'bajaj allianz', 'cholamandalam',
        'shriram', 'muthoot', 'manappuram', 'mahindra finance', 'mas financial',
        'lic', 'star health', 'jio fin', 'piramal',
    ],
    # Pharma + biotech (medical/hospital go to HEALTHCARE)
    'PHARMA': [
        'pharma', 'pharm', 'drug', 'laborator', 'biotech', 'bio-tech',
        'cipla', 'ranbaxy', 'lupin', 'sun pharma', 'dr reddy', 'aurobindo',
        'glenmark', 'cadila', 'zydus', 'torrent pharma', 'divis', 'biocon',
        'mankind', 'natco', 'alembic', 'ipca', 'gland pharma', 'syngene',
        'wockhardt', 'sanofi', 'abbott', 'pfizer', 'glaxo', 'novartis',
        'jb chem', 'eris', 'ajanta',
    ],
    # Hospitals, diagnostics, medical devices
    'HEALTHCARE': [
        'hospital', 'healthcare', 'health care', 'medical', 'medi-',
        'diagnostic', 'pathology', 'laboratories', 'clinics', 'clinical',
        'apollo', 'fortis', 'narayana', 'medanta', 'rainbow', 'thyrocare',
        'metropolis', 'krsnaa', 'aster', 'max health', 'kims', 'aarti drug',
    ],
    # Cars, 2W, CV, ancillaries, EV
    'AUTO': [
        'auto', 'automobile', 'automotive', 'motor', 'motors', 'vehicle',
        'tyre', 'tyres', 'tire', 'tractor', 'scooter', 'bike', 'cycle',
        'bearing', 'forging', 'piston', 'gear', 'axle', 'wheel', 'brake',
        'maruti', 'mahindra', 'tata motor', 'eicher', 'hero', 'bajaj auto',
        'tvs', 'ashok ley', 'force motor', 'minda', 'bosch', 'exide',
        'amara raja', 'mrf', 'apollo tyre', 'ceat', 'jk tyre', 'balkrishna',
        'sundaram', 'endurance', 'sona blw',
    ],
    # Oil & gas
    'ENERGY': [
        'oil', 'gas', 'petrol', 'petroleum', 'refiner', 'lng',
        'ongc', 'reliance', 'ioc', 'bpcl', 'hpcl', 'gail', 'mrpl', 'chennai pet',
        'gspl', 'gujarat gas', 'mahanagar gas', 'igl', 'indraprastha',
        'castrol', 'aegis', 'petronet',
    ],
    # Power gen, T&D, utilities, renewables
    'POWER': [
        'power', 'electric', 'electrical', 'transmission', 'generation',
        'thermal', 'hydro', 'nuclear', 'utility', 'utilit',
        'ntpc', 'powergrid', 'tata power', 'adani power', 'jsw energy',
        'reliance power', 'torrent power', 'cesc', 'nhpc', 'sjvn', 'rec ltd',
        'pfc', 'ireda', 'kalpataru',
    ],
    # Solar/wind specifically routed to RENEWABLE for sub-sector clarity
    'RENEWABLE': [
        'solar', 'wind', 'renewable', 'green energy', 'cleantech',
        'suzlon', 'inox wind', 'orient green', 'bf utilities', 'borosil renew',
        'websol', 'waaree',
    ],
    # Mining, coal
    'MINING': [
        'coal', 'mining', 'mineral', 'lignite', 'iron ore',
        'coal india', 'nmdc', 'moil', 'gmdc',
    ],
    # Iron/steel/aluminium/copper/zinc
    'METALS': [
        'steel', 'iron', 'metal', 'metals', 'metallurg',
        'alumin', 'aluminum', 'aluminium', 'copper', 'zinc', 'lead',
        'alloy', 'foundry', 'casting', 'sponge iron',
        'tata steel', 'jindal', 'jsw steel', 'sail', 'visa steel', 'vedanta',
        'hindalco', 'hindustan zinc', 'nalco', 'ratnamani', 'jsl', 'jspl',
        'apl apollo', 'welspun',
    ],
    # Consumer staples + food + personal care
    'FMCG': [
        'consumer', 'consumers', 'food', 'foods', 'beverage', 'beverages',
        'tea', 'coffee', 'dairy', 'milk', 'sugar', 'edible', 'oil & food',
        'personal care', 'soap', 'detergent', 'noodle', 'biscuit', 'snack',
        'hindustan unilever', 'hul', 'itc', 'nestle', 'dabur', 'marico',
        'godrej', 'colgate', 'gillette', 'p&g', 'procter', 'emami',
        'britannia', 'jubilant food', 'tata consumer', 'varun beverage',
        'parag milk', 'heritage food', 'avt natural', 'cello', 'bajaj cons',
        'patanjali', 'gopal snack',
    ],
    # Construction, EPC, cement, ports, roads
    'INFRA': [
        'construct', 'construction', 'infrastructure', 'infra',
        'engineering', 'projects', 'project', 'irrigation',
        'cement', 'concrete', 'building', 'road', 'roads', 'bridge', 'rail',
        'metro', 'port', 'ports', 'airport', 'shipping',
        'larsen', 'l&t', 'ircon', 'rites', 'ircon int', 'rvnl', 'irfc',
        'gmr', 'gvk', 'adani port', 'jsw infra', 'krishna inst', 'kalpataru',
        'pnc infratech', 'kec', 'ncc', 'hg infra', 'ashoka', 'dilip build',
        'ultratech', 'shree cement', 'ambuja', 'acc', 'ramco cement',
        'dalmia', 'india cement', 'birla corp', 'jk cement', 'heidelberg',
    ],
    # Telecom + communications
    'TELECOM': [
        'telecom', 'communications', 'communication', 'wireless', 'broadband',
        'network', 'networks', 'cable',
        'bharti', 'airtel', 'idea', 'vodafone', 'tata commun', 'reliance jio',
        'tower', 'indus tower', 'sterlite tech', 'gtl', 'route mobile',
        'tanla', 'railtel',
    ],
    # Real estate developers + REITs
    'REALESTATE': [
        'real estate', 'property', 'realty', 'realtors', 'reit',
        'developers', 'developer', 'estates',
        'dlf', 'godrej prop', 'oberoi', 'prestige', 'sobha', 'lodha', 'macrotech',
        'phoenix mill', 'brigade', 'sunteck', 'mahindra lifespace',
        'embassy', 'mindspace', 'brookfield reit', 'nexus select',
        'signatureglobal', 'puravankara', 'arvind smartspace',
    ],
    # Specialty chemicals, agrochem, paints, dyes
    'CHEMICALS': [
        'chemical', 'chemicals', 'specialty chem', 'speciality',
        'fertiliz', 'pesticide', 'agrochem', 'crop', 'seeds',
        'paint', 'paints', 'dye', 'pigment', 'colorant',
        'polymer', 'plastic', 'resin',
        'pidilite', 'asian paints', 'berger', 'kansai', 'akzo',
        'sumitomo', 'srf', 'aarti ind', 'navin fluorine', 'gujarat fluoro',
        'pi industries', 'upl', 'ramco', 'tata chem', 'gujarat alkalies',
        'deepak', 'pi ind', 'fine organic', 'gnfc', 'rcf', 'gsfc',
        'coromandel', 'chambal', 'national fertilizer', 'godrej agrov',
    ],
    # Textiles, apparel, footwear
    'TEXTILE': [
        'textile', 'textiles', 'garment', 'garments', 'yarn',
        'fabric', 'cotton', 'silk', 'apparel', 'clothing', 'fashion', 'denim',
        'spinning', 'weaving',
        'arvind', 'raymond', 'page ind', 'kpr mill', 'trident', 'welspun ind',
        'vardhman', 'siyaram', 'monte carlo', 'gokaldas', 'alok ind',
    ],
    # Footwear / leather
    'FOOTWEAR': [
        'footwear', 'shoe', 'shoes', 'leather', 'sandals',
        'bata', 'relaxo', 'metro brands', 'campus active', 'liberty shoe',
        'mirza int',
    ],
    # Defence + aerospace + shipbuilding
    'DEFENCE': [
        'defence', 'defense', 'aerospace', 'aviation defence', 'shipyard',
        'ammunition', 'ordnance', 'weapon', 'missile', 'arsenal',
        'hal', 'bel', 'bdl', 'mazagon', 'cochin ship', 'garden reach',
        'astra micro', 'paras defence', 'data patterns', 'mtar',
        'kaynes', 'apollo micro', 'taneja',
    ],
    # Aviation (commercial)
    'AVIATION': [
        'airline', 'airways', 'air india', 'aviation services',
        'indigo', 'spicejet', 'jet airways', 'interglobe',
    ],
    # Logistics / shipping / courier / 3PL
    'LOGISTICS': [
        'logistics', 'cargo', 'courier', 'transport', 'transports',
        'freight', 'warehouse', 'supply chain', '3pl',
        'tci', 'allcargo', 'gateway distri', 'mahindra logistics', 'snowman',
        'blue dart', 'gati', 'vrl logist', 'container corp', 'concor',
        'shipping corp', 'great eastern shipping', 'gesco', 'sci',
    ],
    # Hospitality
    'HOSPITALITY': [
        'hotel', 'hotels', 'resort', 'resorts', 'tourism', 'leisure',
        'restaurants', 'jubilant food',
        'taj', 'indian hotel', 'east india hotel', 'eih', 'lemon tree',
        'chalet', 'royal orchid', 'mahindra holid', 'wonderla', 'oriental',
    ],
    # Travel + ticketing platforms
    'TRAVEL': [
        'travel', 'tour', 'cruise', 'easy trip',
        'thomas cook', 'cox & kings', 'mahindra holidays',
    ],
    # Retail / e-commerce / supermarkets / jewellery / departmental
    'RETAIL': [
        'retail', 'retailers', 'supermarket', 'mall', 'stores', 'storage',
        'e-commerce', 'ecommerce',
        'jewel', 'gold', 'silver', 'diamond',
        'titan', 'kalyan jewel', 'thangamayil', 'pc jewel', 'tribhovandas',
        'avenue super', 'dmart', 'trent', 'aditya birla fash', 'shoppers stop',
        'v-mart', 'westside', 'reliance retail', 'fashion', 'go fashion',
        'vedant fashion', 'manyavar',
    ],
    # Media / entertainment / publishing / broadcast
    'MEDIA': [
        'media', 'broadcast', 'publishing', 'publish', 'newspaper',
        'television', 'tv ', 'film', 'movies', 'entertainment',
        'zee', 'sun tv', 'tv18', 'network18', 'pvr', 'inox leisure', 'pvrinox',
        'saregama', 'tips music', 'tips industries', 'eros', 'balaji telef',
        'jagran', 'dish tv', 'nazara',
    ],
    # Electronics / semiconductors / capital goods (consumer durable)
    'ELECTRONICS': [
        'electronic', 'electronics', 'semiconductor', 'capacitor', 'battery',
        'led', 'lighting', 'appliance', 'appliances', 'durable',
        'havells', 'crompton', 'dixon tech', 'amber enter', 'bluestar',
        'voltas', 'whirlpool', 'symphony', 'rajesh exports', 'vguard', 'v-guard',
        'orient electric', 'polycab', 'kei ind', 'finolex',
    ],
    # Engineering + capital goods + machinery
    'ENGINEERING': [
        'machinery', 'machines', 'machine tool', 'tools', 'turbine',
        'pump', 'compressor', 'valve', 'industrial equipment',
        'siemens', 'abb', 'cummins', 'thermax', 'kirloskar', 'crompton greaves',
        'bharat heavy', 'bhel', 'grindwell', 'sks', 'graphite', 'esab',
        'isgec', 'triveni turbin', 'elgi equip',
    ],
    # Paper / packaging
    'PAPER': [
        'paper', 'pulp', 'packaging', 'corrugat', 'tissue', 'newsprint',
        'jk paper', 'tnpl', 'ballarpur', 'orient paper', 'andhra paper',
        'huhtamaki', 'cosmo first',
    ],
    # Agri / plantation / sugar / seafood / agro-processing
    'AGRI': [
        'agro', 'agri', 'plantation', 'planters', 'estate plant',
        'seafood', 'marine', 'fishery', 'aqua',
        'rallis', 'kaveri seed', 'jain irrig', 'avanti feed', 'venky',
        'godrej agro', 'apex frozen', 'shri lakshmi',
    ],
    # Sugar mills (separate, often distinct vertical)
    'SUGAR': [
        'sugar mill', 'sugar', 'cane', 'distiller',
        'balrampur', 'shree renuka', 'bajaj hindusthan', 'dwarikesh',
        'eid parry', 'triveni eng', 'dalmia bharat sugar',
    ],
    # Diversified conglomerates / holdings
    'CONGLOMERATE': [
        'enterprises', 'enterprise', 'group', 'holdings', 'holding',
        'diversified', 'industries', 'industrial',
    ],
    # Trading / commodity dealers
    'TRADING': [
        'trading', 'traders', 'commodities trading', 'brokerage',
        'mmtc', 'stc', 'angel one', 'iifl', 'motilal oswal', 'edelweiss',
    ],
    # Glass / ceramics / building materials sub-bucket
    'BUILDING_MATERIAL': [
        'glass', 'tile', 'tiles', 'ceramic', 'ceramics', 'sanitaryware',
        'granito', 'plyboard', 'plywood', 'laminate', 'mdf', 'particle board',
        'asahi', 'somany', 'kajaria', 'nitco', 'cera sanit', 'hsil',
        'astral', 'finolex pipes', 'prince pipe', 'apl apollo', 'supreme ind',
    ],
}

# Generic patterns appended after main map — caught only if nothing more specific matched.
# Allows us to bucket stocks whose names contain weak hints.
GENERIC_SECTOR_FALLBACKS = {
    'TEXTILE': ['spintex', 'cotspin', 'spinning', 'mills', 'fab ', 'seide', 'fiberweb'],
    'INFRA':   ['contractors', 'contract', 'foundation', 'project', 'urban', 'urbanstructure', 'tube', 'tubes', 'goodluck'],
    'HEALTHCARE': ['medicare', 'wellness', 'lifecare', 'nephrocare', 'lifestyle services'],
    'CHEMICALS': ['rasayan', 'minechem', 'colors', 'colours', 'organic', 'speciality', 'specialty',
                  'lubricants', 'adhesives', 'ingrevia', 'ingredients', 'nitrochem', 'aksharchem', 'gases',
                  'rubber', 'rubfila', 'panel products'],
    'ENGINEERING': ['welding', 'gears', 'precision', 'fabricat', 'forg', 'encon', 'rectifier',
                    'special tubes', 'ventures limited', 'tube', 't&d', 'transformer', 'switchgear',
                    'farm equipment', 'refrigeration', 'icemake', 'heating'],
    'ENERGY': ['energy', 'exploration', 'energy services', 'energy transition'],
    'RENEWABLE': ['greentech', 'green tech', 'solar power'],
    'MINING': ['minerals', 'minechem', 'ores', 'mine planning'],
    'BFSI': ['wam', 'wealth', 'investments', 'capitals', 'fincorp', 'fintrade', 'ratings',
             'finserv', 'finance', 'lending'],
    'REALESTATE': ['realtech', 'realtors', 'projects & infrastructure', 'lifespace',
                   'urbanstructure', 'navkar urban', 'hubtown', 'crest ventures', 'macrotech'],
    'AGRI': ['agritech', 'plantations', 'farms', 'feeds', 'malayalam', 'harrisons', 'proteins'],
    'FMCG': ['breweries', 'alcohol', 'distilleries', 'bottling', 'jyothy', 'lifestyle limited',
             'lifestyles', 'stanley'],
    'ELECTRONICS': ['cables', 'wires', 'optifibre', 'wiring', 'rectifiers', 'aksh', 'avantel',
                    'modison', 't&d india'],
    'IT': ['solutions', 'softech', 'webtech', 'erp', 'saksoft', 'ceinsys', 'alldigi'],
    'TECH': ['affle', '3i ', 'edutech', 'justdial', 'just dial', 'dreamfolks', 'fintech limited',
             'cleducate', 'educate'],
    'MEDIA': ['prime focus', 'pfocus'],
    'CONGLOMERATE': ['ventures', 'group', 'holdings', 'corporation', 'enterprises', 'enterprise',
                     'national standard', 'apex corporation'],
    'PHARMA': ['fdc limited'],
    'BUILDING_MATERIAL': ['panel', 'eurobond', 'tokyoplast', 'wim plast', 'nilkamal'],
    'CHEMICALS': ['hp adhesives', 'basf', 'sadhana nitrochem'],
}

# Explicit ticker → sector overrides for the 200+ most-traded NSE names.
# These take precedence over keyword classification; required for tickers
# whose company names don't contain obvious sector keywords.
TICKER_SECTOR_OVERRIDE = {
    # IT
    'TCS':'IT','INFY':'IT','WIPRO':'IT','HCLTECH':'IT','TECHM':'IT','LTIM':'IT',
    'COFORGE':'IT','MPHASIS':'IT','PERSISTENT':'IT','LTTS':'IT','MINDTREE':'IT',
    'BIRLASOFT':'IT','HEXAWARE':'IT','SONATSOFTW':'IT','OFSS':'IT','TATAELXSI':'IT',
    'KPITTECH':'IT','CYIENT':'IT','RAMKY':'IT','HAPPSTMNDS':'IT','INTELLECT':'IT',
    'NIITLTD':'IT','ZENTEC':'IT','ECLERX':'IT',
    # TECH (new-age)
    'ZOMATO':'TECH','PAYTM':'TECH','NYKAA':'TECH','POLICYBZR':'TECH','DELHIVERY':'TECH',
    'MAPMYINDIA':'TECH','CARTRADE':'TECH','EASEMYTRIP':'TECH','TATATECH':'TECH',
    'INFIBEAM':'TECH','IXIGO':'TECH','MOBIKWIK':'TECH',
    # BFSI — banks
    'HDFCBANK':'BFSI','ICICIBANK':'BFSI','SBIN':'BFSI','AXISBANK':'BFSI','KOTAKBANK':'BFSI',
    'INDUSINDBK':'BFSI','IDFCFIRSTB':'BFSI','BANDHANBNK':'BFSI','FEDERALBNK':'BFSI',
    'PNB':'BFSI','BANKBARODA':'BFSI','CANBK':'BFSI','UNIONBANK':'BFSI','IOB':'BFSI',
    'RBLBANK':'BFSI','YESBANK':'BFSI','AUBANK':'BFSI','CSBBANK':'BFSI','CUB':'BFSI',
    'KARURVYSYA':'BFSI','SOUTHBANK':'BFSI','TMBANK':'BFSI','TATAINVEST':'BFSI',
    # BFSI — NBFCs/insurance/AMCs
    'BAJFINANCE':'BFSI','BAJAJFINSV':'BFSI','BAJAJHLDNG':'BFSI','CHOLAFIN':'BFSI',
    'M&MFIN':'BFSI','SHRIRAMFIN':'BFSI','MUTHOOTFIN':'BFSI','MANAPPURAM':'BFSI',
    'PIRAMALENT':'BFSI','LICI':'BFSI','HDFCLIFE':'BFSI','SBILIFE':'BFSI','ICICIPRULI':'BFSI',
    'ICICIGI':'BFSI','MAXFIN':'BFSI','MAXLIFE':'BFSI','GICRE':'BFSI','NIACL':'BFSI',
    'STARHEALTH':'BFSI','HDFCAMC':'BFSI','NAM-INDIA':'BFSI','UTIAMC':'BFSI',
    'JIOFIN':'BFSI','SBFC':'BFSI','POONAWALLA':'BFSI','REPCOHOME':'BFSI',
    'CDSL':'BFSI','BSE':'BFSI','MCX':'BFSI','IEX':'BFSI','CAMS':'BFSI',
    'ANGELONE':'BFSI','MOTILALOFS':'BFSI','IIFL':'BFSI','EDELWEISS':'BFSI','ARMAN':'BFSI',
    # Pharma
    'SUNPHARMA':'PHARMA','CIPLA':'PHARMA','DRREDDY':'PHARMA','LUPIN':'PHARMA',
    'AUROPHARMA':'PHARMA','ZYDUSLIFE':'PHARMA','TORNTPHARM':'PHARMA','DIVISLAB':'PHARMA',
    'BIOCON':'PHARMA','GLAND':'PHARMA','MANKIND':'PHARMA','SYNGENE':'PHARMA',
    'ALKEM':'PHARMA','GLENMARK':'PHARMA','ABBOTINDIA':'PHARMA','PFIZER':'PHARMA',
    'GLAXO':'PHARMA','SANOFI':'PHARMA','IPCALAB':'PHARMA','NATCOPHARM':'PHARMA',
    'AJANTPHARM':'PHARMA','JBCHEPHARM':'PHARMA','ERIS':'PHARMA','ALEMBICLTD':'PHARMA',
    'WOCKPHARMA':'PHARMA','LAURUSLABS':'PHARMA','GRANULES':'PHARMA','SUVENPHAR':'PHARMA',
    # Healthcare
    'APOLLOHOSP':'HEALTHCARE','MAXHEALTH':'HEALTHCARE','FORTIS':'HEALTHCARE',
    'NH':'HEALTHCARE','MEDANTA':'HEALTHCARE','RAINBOW':'HEALTHCARE','KIMS':'HEALTHCARE',
    'METROPOLIS':'HEALTHCARE','THYROCARE':'HEALTHCARE','KRSNAA':'HEALTHCARE',
    # Auto
    'MARUTI':'AUTO','TATAMOTORS':'AUTO','M&M':'AUTO','HEROMOTOCO':'AUTO',
    'BAJAJ-AUTO':'AUTO','TVSMOTOR':'AUTO','EICHERMOT':'AUTO','ASHOKLEY':'AUTO',
    'MRF':'AUTO','APOLLOTYRE':'AUTO','BALKRISIND':'AUTO','CEATLTD':'AUTO',
    'JKTYRE':'AUTO','BOSCHLTD':'AUTO','EXIDEIND':'AUTO','AMARAJABAT':'AUTO',
    'MOTHERSON':'AUTO','SAMVRDHANA':'AUTO','BHARATFORG':'AUTO','SONACOMS':'AUTO',
    'ENDURANCE':'AUTO','MINDAIND':'AUTO','UNOMINDA':'AUTO','SUNDARMFIN':'BFSI',
    'TIINDIA':'AUTO','CRAFTSMAN':'AUTO','RACL':'AUTO','SCHAEFFLER':'AUTO',
    'ESCORTS':'AUTO','FORCEMOT':'AUTO',
    # Energy / Oil & Gas
    'RELIANCE':'ENERGY','ONGC':'ENERGY','IOC':'ENERGY','BPCL':'ENERGY','HPCL':'ENERGY',
    'GAIL':'ENERGY','MRPL':'ENERGY','PETRONET':'ENERGY','OIL':'ENERGY','CASTROLIND':'ENERGY',
    'GUJGASLTD':'ENERGY','MGL':'ENERGY','IGL':'ENERGY','GSPL':'ENERGY','AEGISCHEM':'ENERGY',
    # Power
    'NTPC':'POWER','POWERGRID':'POWER','TATAPOWER':'POWER','ADANIPOWER':'POWER',
    'JSWENERGY':'POWER','TORNTPOWER':'POWER','CESC':'POWER','NHPC':'POWER','SJVN':'POWER',
    'PFC':'POWER','RECLTD':'POWER','IREDA':'POWER','KEC':'POWER','KALPATPOWR':'POWER',
    'ADANIGREEN':'RENEWABLE','SUZLON':'RENEWABLE','INOXWIND':'RENEWABLE','BFUTILITIE':'RENEWABLE',
    'WAAREE':'RENEWABLE','BORORENEW':'RENEWABLE','ORIENTGREEN':'RENEWABLE',
    # Mining
    'COALINDIA':'MINING','NMDC':'MINING','MOIL':'MINING','GMDCLTD':'MINING','NLCINDIA':'MINING',
    # Metals
    'TATASTEEL':'METALS','JSWSTEEL':'METALS','SAIL':'METALS','JINDALSTEL':'METALS',
    'HINDALCO':'METALS','HINDZINC':'METALS','VEDL':'METALS','NATIONALUM':'METALS',
    'JSL':'METALS','APLAPOLLO':'METALS','RATNAMANI':'METALS','WELCORP':'METALS',
    'WELSPUNIND':'TEXTILE','WELSPUNLIV':'TEXTILE','BORORENEW':'RENEWABLE','MAITHANALL':'METALS',
    # FMCG
    'HINDUNILVR':'FMCG','ITC':'FMCG','NESTLEIND':'FMCG','BRITANNIA':'FMCG',
    'DABUR':'FMCG','MARICO':'FMCG','GODREJCP':'FMCG','COLPAL':'FMCG','EMAMILTD':'FMCG',
    'TATACONSUM':'FMCG','VBL':'FMCG','UBL':'FMCG','RADICO':'FMCG','PATANJALI':'FMCG',
    'JUBLFOOD':'FMCG','DEVYANI':'FMCG','SAPPHIRE':'FMCG','WESTLIFE':'FMCG',
    'BAJAJCON':'FMCG','HONASA':'FMCG','GOPAL':'FMCG','BIKAJI':'FMCG','PARAGMILK':'FMCG',
    'HERITGFOOD':'FMCG','AVTNPL':'FMCG','AGI':'FMCG','CELLO':'FMCG',
    # Infra / Cement
    'LT':'INFRA','GMRINFRA':'INFRA','ADANIPORTS':'INFRA','JSWINFRA':'INFRA',
    'CONCOR':'LOGISTICS','IRCON':'INFRA','RVNL':'INFRA','RITES':'INFRA','IRFC':'BFSI',
    'NCC':'INFRA','HGINFRA':'INFRA','PNCINFRA':'INFRA','ASHOKA':'INFRA','DBL':'INFRA',
    'ULTRACEMCO':'INFRA','SHREECEM':'INFRA','AMBUJACEM':'INFRA','ACC':'INFRA',
    'RAMCOCEM':'INFRA','DALBHARAT':'INFRA','INDIACEM':'INFRA','BIRLACORPN':'INFRA',
    'JKCEMENT':'INFRA','HEIDELBERG':'INFRA','PRSMJOHNSN':'INFRA','ORIENTCEM':'INFRA',
    # Telecom
    'BHARTIARTL':'TELECOM','IDEA':'TELECOM','TATACOMM':'TELECOM','INDUSTOWER':'TELECOM',
    'STRTECH':'TELECOM','HFCL':'TELECOM','RAILTEL':'TELECOM','TANLA':'TELECOM',
    'ROUTE':'TELECOM','GTLINFRA':'TELECOM',
    # Real Estate
    'DLF':'REALESTATE','GODREJPROP':'REALESTATE','OBEROIRLTY':'REALESTATE',
    'PRESTIGE':'REALESTATE','SOBHA':'REALESTATE','LODHA':'REALESTATE','MACROTECH':'REALESTATE',
    'PHOENIXLTD':'REALESTATE','BRIGADE':'REALESTATE','SUNTECK':'REALESTATE',
    'MAHLIFE':'REALESTATE','EMBASSY':'REALESTATE','MINDSPACE':'REALESTATE',
    'BIRET':'REALESTATE','NXST':'REALESTATE','PURVA':'REALESTATE','ARVSMART':'REALESTATE',
    'SIGNATURE':'REALESTATE','SUNTV':'MEDIA',
    # Chemicals
    'PIDILITIND':'CHEMICALS','ASIANPAINT':'CHEMICALS','BERGEPAINT':'CHEMICALS',
    'KANSAINER':'CHEMICALS','AKZOINDIA':'CHEMICALS','SRF':'CHEMICALS',
    'PIIND':'CHEMICALS','UPL':'CHEMICALS','TATACHEM':'CHEMICALS','GUJALKALI':'CHEMICALS',
    'AARTIIND':'CHEMICALS','NAVINFLUOR':'CHEMICALS','GUJFLUORO':'CHEMICALS',
    'DEEPAKNTR':'CHEMICALS','FINEORG':'CHEMICALS','GNFC':'CHEMICALS','RCF':'CHEMICALS',
    'GSFC':'CHEMICALS','COROMANDEL':'CHEMICALS','CHAMBLFERT':'CHEMICALS','NFL':'CHEMICALS',
    'GODREJAGRO':'AGRI','SUMICHEM':'CHEMICALS','VINATIORGA':'CHEMICALS','ALKYLAMINE':'CHEMICALS',
    'CLEAN':'CHEMICALS','ROSSARI':'CHEMICALS','LAXMIORG':'CHEMICALS','GALAXYSURF':'CHEMICALS',
    # Textile / Apparel
    'PAGEIND':'TEXTILE','ARVIND':'TEXTILE','RAYMOND':'TEXTILE','TRIDENT':'TEXTILE',
    'KPRMILL':'TEXTILE','VARDHMAN':'TEXTILE','SIYSIL':'TEXTILE','GOKEX':'TEXTILE',
    'ALOKINDS':'TEXTILE','MONTECARLO':'TEXTILE','GOFASHION':'TEXTILE','VEDANT':'RETAIL',
    # Footwear
    'BATAINDIA':'FOOTWEAR','RELAXO':'FOOTWEAR','METROBRAND':'FOOTWEAR','CAMPUS':'FOOTWEAR',
    'LIBERTSHOE':'FOOTWEAR','MIRZAINT':'FOOTWEAR',
    # Defence
    'HAL':'DEFENCE','BEL':'DEFENCE','BDL':'DEFENCE','MAZAGON':'DEFENCE','COCHINSHIP':'DEFENCE',
    'GRSE':'DEFENCE','ASTRAMICRO':'DEFENCE','PARAS':'DEFENCE','DATAPATTNS':'DEFENCE',
    'MTAR':'DEFENCE','KAYNES':'DEFENCE','SIKAINDIA':'DEFENCE',
    # Aviation
    'INDIGO':'AVIATION','SPICEJET':'AVIATION','INTERGLOBE':'AVIATION',
    # Logistics
    'TCI':'LOGISTICS','ALLCARGO':'LOGISTICS','GATEWAY':'LOGISTICS','MAHLOG':'LOGISTICS',
    'BLUEDART':'LOGISTICS','GATI':'LOGISTICS','VRLLOG':'LOGISTICS','SCI':'LOGISTICS',
    'GESHIP':'LOGISTICS','SNOWMAN':'LOGISTICS',
    # Hospitality
    'INDHOTEL':'HOSPITALITY','EIHOTEL':'HOSPITALITY','LEMONTREE':'HOSPITALITY',
    'CHALET':'HOSPITALITY','ROYALORCH':'HOSPITALITY','MAHINDRA':'HOSPITALITY',
    'WONDERLA':'HOSPITALITY','TAJGVK':'HOSPITALITY','THOMASCOOK':'TRAVEL',
    # Retail / Jewellery
    'TITAN':'RETAIL','KALYANKJIL':'RETAIL','THANGAMAYL':'RETAIL','PCJEWELLER':'RETAIL',
    'TRIBHOVNDS':'RETAIL','DMART':'RETAIL','TRENT':'RETAIL','ABFRL':'RETAIL',
    'SHOPERSTOP':'RETAIL','VMART':'RETAIL','VIPIND':'RETAIL','SAFARI':'RETAIL',
    'GOFASHION':'RETAIL','VEDANT':'RETAIL','ETHOS':'RETAIL',
    # Media / Entertainment
    'ZEEL':'MEDIA','SUNTV':'MEDIA','TV18BRDCST':'MEDIA','NETWORK18':'MEDIA',
    'PVRINOX':'MEDIA','PVR':'MEDIA','INOXLEISURE':'MEDIA','SAREGAMA':'MEDIA',
    'TIPSINDLTD':'MEDIA','TIPSMUSIC':'MEDIA','EROSMEDIA':'MEDIA','BALAJITELE':'MEDIA',
    'JAGRAN':'MEDIA','DISHTV':'MEDIA','NAZARA':'MEDIA','HATHWAY':'MEDIA',
    # Electronics / Durables
    'HAVELLS':'ELECTRONICS','CROMPTON':'ELECTRONICS','DIXON':'ELECTRONICS','AMBER':'ELECTRONICS',
    'BLUESTARCO':'ELECTRONICS','VOLTAS':'ELECTRONICS','WHIRLPOOL':'ELECTRONICS',
    'SYMPHONY':'ELECTRONICS','VGUARD':'ELECTRONICS','ORIENTELEC':'ELECTRONICS',
    'POLYCAB':'ELECTRONICS','KEI':'ELECTRONICS','FINOLEXIND':'ELECTRONICS','RAJESHEXPO':'RETAIL',
    'TEGA':'ELECTRONICS','EPL':'ELECTRONICS',
    # Engineering / Capital Goods
    'SIEMENS':'ENGINEERING','ABB':'ENGINEERING','CUMMINSIND':'ENGINEERING','THERMAX':'ENGINEERING',
    'KIRLOSENG':'ENGINEERING','BHEL':'ENGINEERING','GRINDWELL':'ENGINEERING','ESAB':'ENGINEERING',
    'ISGEC':'ENGINEERING','TRITURBINE':'ENGINEERING','ELGIEQUIP':'ENGINEERING','CGPOWER':'ENGINEERING',
    'AIAENG':'ENGINEERING','PRAJIND':'ENGINEERING','SKFINDIA':'ENGINEERING','TIMKEN':'ENGINEERING',
    # Paper / Packaging
    'JKPAPER':'PAPER','TNPL':'PAPER','BALLARPUR':'PAPER','ORIENTPPR':'PAPER',
    'ANDHRAPAP':'PAPER','HUHTAMAKI':'PAPER','COSMOFIRST':'PAPER','EMAMIPAP':'PAPER',
    # Agri / Sugar
    'RALLIS':'AGRI','KAVERISEED':'AGRI','JAIN':'AGRI','AVANTI':'AGRI','VENKEYS':'AGRI',
    'APEXFROZN':'AGRI','GODREJAGRO':'AGRI',
    'BALRAMCHIN':'SUGAR','RENUKA':'SUGAR','BAJAJHIND':'SUGAR','DWARKESH':'SUGAR',
    'EIDPARRY':'SUGAR','TRIVENI':'SUGAR','DALMIASUG':'SUGAR',
    # Conglomerate
    'ADANIENT':'CONGLOMERATE','GODREJIND':'CONGLOMERATE','PIRAMAL':'CONGLOMERATE',
    'ITC':'FMCG',  # already covered, kept for safety
    'GRASIM':'CONGLOMERATE','BAJAJHLDNG':'BFSI','CENTURYTEX':'TEXTILE','EIHAHOTELS':'HOSPITALITY',
}


def _classify_sector(company_name, ticker=None):
    """Determine sector — explicit ticker override wins, then primary keywords,
    then generic fallback patterns. Drastically reduces 'OTHER' bucket."""
    if ticker:
        t = ticker.strip().upper()
        if t in TICKER_SECTOR_OVERRIDE:
            return TICKER_SECTOR_OVERRIDE[t]
    name_lower = (company_name or '').lower()
    # Primary specific keywords
    for sector, keywords in SECTOR_MAP.items():
        for kw in keywords:
            if kw in name_lower:
                return sector
    # Generic fallback patterns
    for sector, keywords in GENERIC_SECTOR_FALLBACKS.items():
        for kw in keywords:
            if kw in name_lower:
                return sector
    # Last-ditch: bucket "Industries"/"Limited" generic names as CONGLOMERATE
    # so they don't all fall into OTHER.
    if 'industries' in name_lower or 'industrial' in name_lower:
        return 'CONGLOMERATE'
    return 'OTHER'


def download_nse_stocks():
    """Download full NSE equity list and cache as JSON"""
    try:
        url = 'https://archives.nseindia.com/content/equities/EQUITY_L.csv'
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/csv'
        }
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()

        lines = resp.text.strip().split('\n')
        stocks = {}

        for line in lines[1:]:  # Skip header
            parts = line.split(',')
            if len(parts) >= 2:
                ticker = parts[0].strip()
                name = parts[1].strip()
                series = parts[2].strip() if len(parts) > 2 else 'EQ'

                # Only include EQ series (regular equity, not bonds/warrants)
                if series != 'EQ' and series != 'BE':
                    continue
                if not ticker or not name:
                    continue

                sector = _classify_sector(name, ticker)
                stocks[ticker] = {
                    'name': name,
                    'sector': sector,
                    'exchange': 'NSE'
                }

        # Save cache
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        cache_data = {
            'downloaded_at': datetime.now().isoformat(),
            'count': len(stocks),
            'stocks': stocks
        }
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache_data, f)

        logger.info(f"Downloaded {len(stocks)} NSE stocks")
        return stocks

    except Exception as e:
        logger.warning(f"Failed to download NSE stock list: {e}")
        return None


def _load_cache():
    """Load cached stock list if fresh enough"""
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        downloaded = datetime.fromisoformat(data['downloaded_at'])
        if datetime.now() - downloaded > timedelta(hours=CACHE_MAX_AGE_HOURS):
            return None  # Cache expired
        return data['stocks']
    except Exception:
        return None


def get_stock_universe():
    """Get the full stock universe — from cache or fresh download"""
    stocks = _load_cache()
    if stocks:
        return stocks
    stocks = download_nse_stocks()
    if stocks:
        return stocks
    # Last resort: return empty
    return {}


# Popular stocks that may be missing from the NSE CSV
# (listed under different names, newly listed, or special series)
SUPPLEMENTARY_STOCKS = {
    'ZOMATO': {'name': 'Zomato', 'sector': 'TECH', 'exchange': 'NSE'},
    'POLICYBZR': {'name': 'PB Fintech (PolicyBazaar)', 'sector': 'TECH', 'exchange': 'NSE'},
    'DELHIVERY': {'name': 'Delhivery', 'sector': 'OTHER', 'exchange': 'NSE'},
    'MAPMYINDIA': {'name': 'CE Info Systems (MapMyIndia)', 'sector': 'TECH', 'exchange': 'NSE'},
    'JIOFIN': {'name': 'Jio Financial Services', 'sector': 'BFSI', 'exchange': 'NSE'},
    'CARTRADE': {'name': 'CarTrade Tech', 'sector': 'TECH', 'exchange': 'NSE'},
    'EASEMYTRIP': {'name': 'Easy Trip Planners', 'sector': 'TECH', 'exchange': 'NSE'},
    'KAYNES': {'name': 'Kaynes Technology', 'sector': 'DEFENCE', 'exchange': 'NSE'},
    'COCHINSHIP': {'name': 'Cochin Shipyard', 'sector': 'DEFENCE', 'exchange': 'NSE'},
    'MAZAGON': {'name': 'Mazagon Dock Shipbuilders', 'sector': 'DEFENCE', 'exchange': 'NSE'},
    'CAMPUS': {'name': 'Campus Activewear', 'sector': 'RETAIL', 'exchange': 'NSE'},
    'RAINBOW': {'name': 'Rainbow Childrens Medicare', 'sector': 'PHARMA', 'exchange': 'NSE'},
    'MEDANTA': {'name': 'Global Health (Medanta)', 'sector': 'PHARMA', 'exchange': 'NSE'},
    'MANKIND': {'name': 'Mankind Pharma', 'sector': 'PHARMA', 'exchange': 'NSE'},
    'JSWINFRA': {'name': 'JSW Infrastructure', 'sector': 'INFRA', 'exchange': 'NSE'},
    'SBFC': {'name': 'SBFC Finance', 'sector': 'BFSI', 'exchange': 'NSE'},
    'CELLO': {'name': 'Cello World', 'sector': 'FMCG', 'exchange': 'NSE'},
    'TATATECH': {'name': 'Tata Technologies', 'sector': 'IT', 'exchange': 'NSE'},
    'DOMS': {'name': 'DOMS Industries', 'sector': 'OTHER', 'exchange': 'NSE'},
    'SIGNATURE': {'name': 'Signatureglobal', 'sector': 'REALESTATE', 'exchange': 'NSE'},
}


# ===== Module-level state (loaded once on import) =====
_base = get_stock_universe()
# Merge supplementary stocks (don't overwrite existing)
for _t, _info in SUPPLEMENTARY_STOCKS.items():
    if _t not in _base:
        _base[_t] = _info
STOCK_UNIVERSE = _base
UNIVERSE_TICKERS = set(STOCK_UNIVERSE.keys())

# Build name lookup tables
UNIVERSE_SHORT_NAMES = {}
for _ticker, _info in STOCK_UNIVERSE.items():
    short = _info['name'].split(' ')[0].lower()
    if len(short) > 3:  # Min 4 chars
        # For duplicates, prefer the bigger/more important company
        if short not in UNIVERSE_SHORT_NAMES:
            UNIVERSE_SHORT_NAMES[short] = _ticker

# Override with major companies (ensure right mapping for common names)
UNIVERSE_SHORT_NAMES.update({
    'tata': 'TCS',           # "Tata" in news usually means TCS or Tata group
    'adani': 'ADANIENT',     # Adani group → Adani Enterprises
    'reliance': 'RELIANCE',  # Not Reliance Power
    'bajaj': 'BAJFINANCE',   # Bajaj group → Bajaj Finance
    'hdfc': 'HDFCBANK',      # HDFC → HDFC Bank
    'kotak': 'KOTAKBANK',
    'birla': 'GRASIM',
    'vedanta': 'VEDL',
    'godrej': 'GODREJCP',
    'mahindra': 'M&M',
})


def search_stocks(query):
    """Search stocks by ticker or company name. Returns top 10 matches."""
    q = query.strip().upper()
    ql = query.strip().lower()
    results = []

    # Exact ticker match first
    if q in STOCK_UNIVERSE:
        info = STOCK_UNIVERSE[q]
        results.append({'ticker': q, 'company': info['name'], 'sector': info['sector'], 'exchange': 'NSE'})

    # Partial ticker match
    for ticker, info in STOCK_UNIVERSE.items():
        if ticker != q and q in ticker and not any(r['ticker'] == ticker for r in results):
            results.append({'ticker': ticker, 'company': info['name'], 'sector': info['sector'], 'exchange': 'NSE'})
            if len(results) >= 10:
                return results

    # Name match
    for ticker, info in STOCK_UNIVERSE.items():
        if ql in info['name'].lower() and not any(r['ticker'] == ticker for r in results):
            results.append({'ticker': ticker, 'company': info['name'], 'sector': info['sector'], 'exchange': 'NSE'})
            if len(results) >= 10:
                return results

    return results[:10]


# CLI: refresh the cache
if __name__ == '__main__':
    stocks = download_nse_stocks()
    if stocks:
        print(f"Downloaded {len(stocks)} NSE stocks to {CACHE_FILE}")
    else:
        print("Failed to download")
