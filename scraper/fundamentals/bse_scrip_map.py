"""
Ticker → BSE scrip code map for the 32 monitored stocks.

BSE endpoints take numeric scrip codes, not NSE tickers. This map keeps the
lookup centralised. Expand as the universe grows.
"""

BSE_SCRIP_CODES = {
    "INFY": "500209",
    "TCS": "532540",
    "WIPRO": "507685",
    "LT": "500510",
    "RELIANCE": "500325",
    "HDFCBANK": "500180",
    "ICICIBANK": "532174",
    "SBIN": "500112",
    "BAJAJFINSV": "532978",
    "MARUTI": "532500",
    "TATASTEEL": "500470",
    "JSWSTEEL": "500228",
    "ADANIGREEN": "541450",
    "ADANIPORTS": "532921",
    "SUNPHARMA": "524715",
    "DIVISLAB": "532488",
    "HINDUNILVR": "500696",
    "ITC": "500875",
    "NESTLEIND": "500790",
    "AXISBANK": "532215",
    "KNRCON": "532942",
    "TECHM": "532755",
    "HCLTECH": "532281",
    "BAJAJ-AUTO": "532977",
    "BHARTIARTL": "532454",
    "CIPLA": "500087",
    "LUPIN": "500257",
    "POWERGRID": "532898",
    "NTPC": "532555",
    "COALINDIA": "533278",
    "IOC": "530965",
    "TATAPOWER": "500400",
}


def get_scrip(ticker: str) -> str:
    return BSE_SCRIP_CODES.get(ticker.upper(), "")
