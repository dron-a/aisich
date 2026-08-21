from cooldown import CooldownGate


def test_first_call_allowed_repeat_blocked():
    g = CooldownGate(window_s=60)
    assert g.allow("a") is True
    assert g.allow("a") is False


def test_keys_are_independent():
    g = CooldownGate(window_s=60)
    assert g.allow("group1:alice") is True
    assert g.allow("group1:bob") is True      # the shared-cooldown bug, fixed
    assert g.allow("group2:alice") is True    # same person, different group


def test_window_expiry(monkeypatch):
    g = CooldownGate(window_s=60)
    t = [1000.0]
    monkeypatch.setattr("cooldown.time.monotonic", lambda: t[0])
    assert g.allow("a") is True
    t[0] += 59
    assert g.allow("a") is False
    t[0] += 2
    assert g.allow("a") is True


def test_overflow_clears_and_still_allows():
    g = CooldownGate(window_s=60, max_entries=3)
    for k in ("a", "b", "c"):
        g.allow(k)
    assert g.allow("d") is True               # overflow cleared, d admitted
    assert g.allow("a") is True               # a's history gone — acceptable trade