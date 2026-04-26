from news_type import classify, label


def test_social_sources():
    assert classify("reddit:r/IndianStockMarket") == "social_buzz"
    assert classify("twitter:CNBCTV18News") == "social_buzz"
    assert classify("telegram:StockMarketNSE") == "social_buzz"


def test_news_outlets():
    assert classify("et_markets") == "news_article"
    assert classify("mint_markets") == "news_article"
    assert classify("moneycontrol_stocks") == "news_article"
    assert classify("newsapi") == "news_article"
    assert classify("google_news") == "news_article"


def test_filing_sources():
    assert classify("bse_announcements") == "filing"
    assert classify("bse:corpfiling") == "filing"
    assert classify("ir:infosys") == "filing"
    assert classify("sebi:pit") == "filing"


def test_unknown():
    assert classify("") == "unknown"
    assert classify(None) == "unknown"


def test_labels():
    assert label("news_article") == "News article"
    assert label("social_buzz") == "Social buzz"
    assert label("filing") == "Official filing"
