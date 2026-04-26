from jargon.aho_corasick import AhoCorasick


def test_simple_match():
    ac = AhoCorasick(["OBV", "RSI", "MACD"])
    out = ac.find("Price rose but OBV diverged and RSI fell.")
    matched = [m[2] for m in out]
    assert "OBV" in matched
    assert "RSI" in matched


def test_word_boundary():
    ac = AhoCorasick(["OBV"])
    out = ac.find("This is KNOBVENT valuable.")
    assert out == []


def test_overlapping_longest_wins():
    ac = AhoCorasick(["put call ratio", "call"])
    out = ac.find("The put call ratio indicates fear.")
    # longest match wins — "put call ratio" preferred over "call"
    surfaces = [m[2].lower() for m in out]
    assert "put call ratio" in surfaces


def test_empty():
    ac = AhoCorasick([])
    assert ac.find("anything") == []
