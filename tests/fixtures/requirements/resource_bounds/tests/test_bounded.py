from bounded import bounded_value


def test_seed_contract():
    assert bounded_value(0) == 0
