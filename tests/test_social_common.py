from social.common import normalize_headline, cluster_hash, extract_tickers


def test_normalize_strips_stopwords_and_case():
    a = normalize_headline("Infosys beats Q4 estimates on the back of strong deal wins")
    b = normalize_headline("Infosys BEATS q4 estimates on the BACK OF strong deal wins!")
    assert a == b


def test_cluster_hash_stable():
    h1 = cluster_hash("INFY", normalize_headline("Infosys beats Q4"))
    h2 = cluster_hash("INFY", normalize_headline("infosys Beats Q4"))
    assert h1 == h2


def test_extract_tickers_word_boundary():
    tickers = ["INFY", "TCS"]
    assert extract_tickers("INFY surprised on the upside", tickers) == ["INFY"]
    assert extract_tickers("INFYNITELY WRONG", tickers) == []
    assert extract_tickers("TCS and INFY posted strong numbers", tickers) == sorted(["INFY", "TCS"])
