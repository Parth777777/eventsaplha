from forensics.forensic_scorer import fuse


def test_fuse_clean():
    r = fuse(
        event_id="x1", ticker="INFY",
        base_score_result={"score": 10, "band": "clean", "reasons": []},
    )
    assert r["score"] <= 15
    assert r["band"] == "clean"


def test_fuse_pump_dominant():
    r = fuse(
        event_id="x2", ticker="SMALLCAP",
        base_score_result={"score": 20, "reasons": []},
        pump_dump_result={"pump_score": 75, "band": "likely_pump"},
    )
    # base=20*0.4 + pump=75*0.25 = 8 + 18.75 = 26.75 → below 40
    # So adding more fine_print should push into unverified
    r2 = fuse(
        event_id="x2", ticker="SMALLCAP",
        base_score_result={"score": 60, "reasons": ["discrepancy"]},
        pump_dump_result={"pump_score": 75, "band": "likely_pump"},
        fine_print_result={"score": 50, "flags": []},
        article_forensics_result={"rule_score": 60, "rule_flags": ["promotional_tone"]},
    )
    # 60*0.4 + 75*0.25 + 60*0.2 + 50*0.15 = 24 + 18.75 + 12 + 7.5 = 62.25
    assert r2["score"] >= 60
    assert r2["band"] in ("unverified", "likely_manipulated")
    assert "likely_pump_pattern" in r2["reasons"]


def test_fuse_fake_neutral_bumps_article():
    r = fuse(
        event_id="x3", ticker="ANY",
        base_score_result={"score": 30, "reasons": []},
        article_forensics_result={
            "rule_score": 30,
            "rule_flags": ["promotional_tone"],
            "llm_verdict": {"tone_bias": "fake_neutral", "favouring_party": "company"},
        },
    )
    assert "fake_neutral_article" in r["reasons"]
    assert "favours_company" in r["reasons"]


def test_fuse_reason_dedup():
    r = fuse(
        event_id="x4", ticker="ANY",
        base_score_result={"score": 10, "reasons": ["promotional_tone"]},
        article_forensics_result={"rule_score": 20, "rule_flags": ["promotional_tone"]},
    )
    assert r["reasons"].count("promotional_tone") == 1
