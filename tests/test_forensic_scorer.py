from forensics.forensic_scorer import score_event, classify_band


def test_clean_event():
    r = score_event(event_id="x1", discrepancy=None, intent=None, source_credibility=0.7)
    assert r["band"] == "clean"
    assert r["score"] < 40


def test_likely_manipulated_compound():
    intent = {
        "intent": "dump_setup",
        "fingerprint_flags": ["anonymous_insider", "unsourced_numbers", "contradicts_public_facts"],
    }
    r = score_event(
        event_id="x2",
        discrepancy={"flagged": True},
        intent=intent,
        source_credibility=0.25,
        coordinated_campaign=True,
        unexplained_volume_pre_news=True,
        promoter_selling_recent=True,
    )
    assert r["band"] == "likely_manipulated"
    assert r["score"] >= 70
    assert "intent_dump_setup" in r["reasons"]
    assert "filing_number_mismatch" in r["reasons"]
    assert "coordinated_campaign" in r["reasons"]


def test_unverified_middle_band():
    # Compose triggers totalling 40-69 without tipping into "likely_manipulated"
    r = score_event(
        event_id="x3",
        intent={"intent": "hype", "fingerprint_flags": ["recycled_phrasing"]},
        source_credibility=0.25,
        unexplained_volume_pre_news=True,
    )
    # hype=12 + recycled=6 + low_credibility=12 + pre_news_vol=14 = 44
    assert 40 <= r["score"] < 70
    assert r["band"] == "unverified"


def test_classify_band():
    assert classify_band(85) == "likely_manipulated"
    assert classify_band(55) == "unverified"
    assert classify_band(10) == "clean"
