from rescoring import calibrated_alpha, _bucket_for, should_surface


def test_bucket_mapping():
    assert _bucket_for(30) == "<50"
    assert _bucket_for(55) == "50-65"
    assert _bucket_for(70) == "65-80"
    assert _bucket_for(85) == "80+"


def test_calibrated_alpha_monotonic_in_hit_rate():
    # Higher expected hit rate should produce higher calibrated alpha
    a = calibrated_alpha(60, 0.40)
    b = calibrated_alpha(60, 0.50)
    c = calibrated_alpha(60, 0.60)
    assert a < b < c


def test_calibrated_alpha_anchor_at_fifty():
    # A raw alpha of 50 with a 0.5 expected hit rate should land near 50
    val = calibrated_alpha(50, 0.5)
    assert 45 <= val <= 55


def test_calibrated_alpha_cap():
    assert calibrated_alpha(999, 1.0) <= 100
    assert calibrated_alpha(-999, 0.0) >= 0


def test_should_surface_policy():
    ok = {"calibrated_alpha": 60, "expected_hit_rate": 0.55, "event_type_gate": "keep"}
    assert should_surface(ok)
    penalised = {"calibrated_alpha": 80, "expected_hit_rate": 0.6, "event_type_gate": "penalise"}
    assert not should_surface(penalised)
    low_alpha = {"calibrated_alpha": 30, "expected_hit_rate": 0.55, "event_type_gate": "keep"}
    assert not should_surface(low_alpha)
    low_hit = {"calibrated_alpha": 60, "expected_hit_rate": 0.30, "event_type_gate": "keep"}
    assert not should_surface(low_hit)
