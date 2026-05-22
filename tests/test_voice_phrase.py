from voice_phrase import compact, compact_from_event


def test_prd_canonical_example():
    out = compact(
        "Tata Power Company Limited bags order worth Rs 500,00,00,000 from Government",
        company="Tata Power Company Limited",
        category="Order Win",
    )
    assert out.startswith("Alert. Tata Power. Order Win.")
    assert "500 Crore" in out
    # The cleaned tail must not echo the original company prefix
    assert "Tata Power Company" not in out
    assert "Limited" not in out
    assert out.endswith(".")


def test_strips_stacked_corporate_suffixes():
    out = compact("Some news", company="Reliance Industries Limited")
    assert "Reliance" in out
    assert "Limited" not in out
    assert "Industries" not in out


def test_money_word_form_crore_and_lakh():
    s1 = compact("Bags order worth Rs 5 crore", company="Foo")
    s2 = compact("Bags order worth INR 50 lakh", company="Foo")
    s3 = compact("Bags order worth ₹2.5 Cr", company="Foo")
    assert "5 Crore" in s1
    assert "50 Lakh" in s2
    assert "2.5 Crore" in s3


def test_money_digit_form_converts_to_indian_unit():
    out = compact("Order win Rs 500,00,00,000", company="Tata Power")
    assert "500 Crore" in out


def test_humanizes_event_type_via_from_event():
    out = compact_from_event({
        "headline": "Reliance Industries Limited announces strategic green hydrogen expansion",
        "company": "Reliance Industries Limited",
        "event_type": "capacity_expansion",
    })
    assert "Capacity Expansion" in out
    assert "Reliance" in out
    assert "Limited" not in out


def test_from_event_falls_back_to_ticker_when_no_company():
    out = compact_from_event({
        "title": "Strategic order announcement",
        "tickers": ["RELIANCE.NS"],
        "event_type": "order_win",
    })
    assert "Reliance" in out
    assert "Order Win" in out
    # Should not read out the exchange suffix
    assert ".NS" not in out


def test_empty_and_invalid_inputs():
    assert compact("") == ""
    assert compact(None) == ""  # type: ignore[arg-type]
    assert compact_from_event({}) == ""
    assert compact_from_event(None) == ""  # type: ignore[arg-type]
    assert compact_from_event({"event_type": "order_win"}) == ""


def test_max_len_truncation_preserves_sentence_boundary():
    long_tail = "very long tail " * 30
    out = compact(long_tail, company="Acme", category="Order Win", max_len=80)
    assert len(out) <= 80
    assert out.endswith(".")


def test_no_duplicate_category_in_tail():
    out = compact("order win", company="Acme", category="Order Win")
    # "order win" tail equals category — must not appear twice
    assert out.lower().count("order win") == 1


def test_drops_filler_pursuant_and_regulation_boilerplate():
    out = compact(
        "Pursuant to regulation 30 of SEBI (Listing Obligations and Disclosure "
        "Requirements) Regulations, 2015, we inform exchange about board meeting",
        company="Foo Ltd",
        category="Board Meeting",
    )
    assert "Pursuant to" not in out
    assert "Regulation 30" not in out
    assert "Board Meeting" in out


def test_compact_returns_safe_when_headline_only():
    out = compact("Quick news")
    assert out == "Alert. Quick news."
