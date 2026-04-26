from forensics.article_forensics import analyze_rules, ArticleForensics


def test_anonymous_sources_counted():
    text = "According to sources familiar with the matter, a person told Reuters that sources close to the company confirmed..."
    f = analyze_rules(text)
    assert f.anonymous_source_count >= 2
    assert "heavy_anonymous_sourcing" in f.rule_flags


def test_unsourced_target_price():
    text = "The stock could hit Rs 1,500 in the next quarter, traders believe."
    f = analyze_rules(text)
    assert f.unsourced_target_claims >= 1
    assert not f.has_analyst_attribution
    assert "unsourced_target_claim" in f.rule_flags


def test_target_with_attribution():
    text = "Jefferies analysts set a target price of Rs 1,500 in a note to clients."
    f = analyze_rules(text)
    assert f.unsourced_target_claims >= 1
    assert f.has_analyst_attribution
    # Should NOT produce unsourced_target_claim flag
    assert "unsourced_target_claim" not in f.rule_flags


def test_promotional_density():
    text = ("A revolutionary multi-bagger with game-changing tech and blockbuster potential. "
            "Truly transformational.")
    f = analyze_rules(text)
    assert f.promotional_density >= 3
    assert "promotional_tone" in f.rule_flags


def test_attack_piece():
    text = "The company is overleveraged, unsustainable, and the bubble could collapse imminently."
    f = analyze_rules(text)
    assert f.bear_density >= 2
    # May or may not hit 3 depending on phrasing, but direction should be bearish


def test_suspicious_balance():
    body = (" This amazing company is set to rally. " * 40)
    tail = " However, bears argue risks include regulatory pressure."
    text = body + tail
    f = analyze_rules(text)
    assert f.suspicious_balance
    assert "balanced_in_form_only" in f.rule_flags


def test_rule_score_clean():
    text = "The company reported Q3 results."
    f = analyze_rules(text)
    assert f.rule_score == 0
    assert f.rule_flags == []


def test_rule_score_compound():
    text = ("Sources close to the company said the stock could double to Rs 2,000. "
            "Revolutionary technology with massive potential. According to insiders, "
            "it's a multi-bagger in the making.")
    f = analyze_rules(text)
    assert f.rule_score >= 25
    assert len(f.rule_flags) >= 2
