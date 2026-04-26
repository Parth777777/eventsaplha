from forensics.bse_filing_fetcher import extract_numbers_from_text


def test_extract_revenue_in_crore():
    text = "Revenue from operations: ₹1,250.5 crore, net profit of 180 crore"
    rows = extract_numbers_from_text(text)
    fields = {r["field_name"]: r for r in rows}
    assert "revenue" in fields
    assert fields["revenue"]["value"] == 1250.5
    assert "net_profit" in fields


def test_extract_margin_pct():
    text = "EBITDA margin was 18.5%"
    rows = extract_numbers_from_text(text)
    assert any(r["field_name"] == "margin" and r["value"] == 18.5 for r in rows)


def test_extract_order_value():
    text = "Order value: ₹450 crore"
    rows = extract_numbers_from_text(text)
    assert any(r["field_name"] == "order_value" and r["value"] == 450 for r in rows)


def test_extract_no_match_on_noise():
    text = "Today the sky is blue and the grass is green."
    rows = extract_numbers_from_text(text)
    assert rows == []
