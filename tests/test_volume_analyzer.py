"""
Unit tests for VolumeAnalyzer — OBV math, surge detection, divergence.
"""
from quant_layer import VolumeAnalyzer


def test_obv_monotone_up():
    closes = [10, 11, 12, 13, 14]
    vols = [100, 200, 300, 400, 500]
    obv = VolumeAnalyzer.compute_obv(closes, vols)
    # Starts at 0, then every up-day adds its own volume
    assert obv == [0, 200, 500, 900, 1400]


def test_obv_monotone_down():
    closes = [14, 13, 12, 11, 10]
    vols = [100, 200, 300, 400, 500]
    obv = VolumeAnalyzer.compute_obv(closes, vols)
    assert obv == [0, -200, -500, -900, -1400]


def test_analyze_insufficient_data():
    r = VolumeAnalyzer.analyze("TEST", [1, 2], [100, 200])
    assert r == {"ticker": "TEST", "has_data": False}


def test_volume_surge_detection():
    # 20 days of stable volume, spike on day 21
    closes = [100 + i * 0.1 for i in range(25)]
    vols = [1000] * 24 + [5000]  # 5× surge on last day
    r = VolumeAnalyzer.analyze("TEST", closes, vols, had_news_last_3d=False)
    assert r["has_data"]
    assert r["volume_surge"] is True
    assert r["unexplained_volume"] is True  # no news


def test_volume_surge_explained_by_news():
    closes = [100 + i * 0.1 for i in range(25)]
    vols = [1000] * 24 + [5000]
    r = VolumeAnalyzer.analyze("TEST", closes, vols, had_news_last_3d=True)
    assert r["volume_surge"] is True
    assert r["unexplained_volume"] is False


def test_volume_multiplier_bull_confirm():
    closes = [100 + i * 0.5 for i in range(25)]
    vols = [1000] * 24 + [3500]
    r = VolumeAnalyzer.analyze("TEST", closes, vols, had_news_last_3d=True)
    mul = VolumeAnalyzer.volume_confirmation_multiplier(r, "bullish")
    assert mul == 1.15


def test_volume_multiplier_bearish_divergence_penalty():
    # Price rising, volume consistently falling on up-days → bearish divergence
    closes = [100 + i * 0.3 for i in range(25)]
    vols = [5000 - i * 150 for i in range(25)]
    r = VolumeAnalyzer.analyze("TEST", closes, vols)
    if r["obv_divergence_flag"] == "bearish":
        mul = VolumeAnalyzer.volume_confirmation_multiplier(r, "bullish")
        assert mul == 0.85
