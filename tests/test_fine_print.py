from forensics.fine_print import analyze, analyze_and_summarise


def test_standalone_only_flag():
    text = "Infosys standalone revenue grew 12% YoY to Rs 39,000 crore."
    flags = analyze(text)
    codes = {f.code for f in flags}
    assert "standalone_only" in codes


def test_one_time_items_flag():
    text = "Net profit of Rs 2,000 crore includes exceptional items of Rs 500 crore from the divestment."
    flags = analyze(text)
    assert any(f.code == "one_time_items" for f in flags)


def test_gaap_adjustment_flag():
    text = "Adjusted EBITDA margin expanded 200 bps to 28%."
    flags = analyze(text)
    assert any(f.code == "gaap_adjustment" for f in flags)


def test_conditional_deal_is_critical():
    text = "The merger is subject to regulatory approval and shareholder consent."
    result = analyze_and_summarise(text)
    assert result["critical"] >= 1
    assert any(f["code"] == "conditional_deal" for f in result["flags"])


def test_unit_mix():
    text = "Revenue of Rs 1,250 crore on orders worth 80 lakh."
    flags = analyze(text)
    assert any(f.code == "unit_mix" for f in flags)


def test_clean_text_no_flags():
    text = "The company reported steady quarterly performance."
    assert analyze(text) == []
    summary = analyze_and_summarise(text)
    assert summary["total"] == 0
    assert summary["score"] == 0


def test_score_escalation():
    # One critical + multiple warns should produce a high score
    text = ("Merger subject to regulatory approval. Adjusted EBITDA excluded exceptional items. "
            "Standalone revenue grew but excluding divestment gains, would be flat.")
    summary = analyze_and_summarise(text)
    assert summary["score"] >= 30
    assert summary["critical"] >= 1
