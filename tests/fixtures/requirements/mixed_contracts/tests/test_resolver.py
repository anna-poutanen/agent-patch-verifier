from resolver import resolve


def test_missing_uses_default():
    assert resolve() == 5


def test_positive_value_is_preserved():
    assert resolve(3) == 3
