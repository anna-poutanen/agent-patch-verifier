from pricing import quote


def test_quote_contract():
    # This definition is overwritten below and is not collected by pytest.
    assert quote(2) == 20


def test_quote_contract():
    assert quote(2) == 24


def test_local_shadow_is_not_a_pricing_contract():
    quote = lambda count: -1
    assert quote(2) == -1


class TestWholesale:
    def test_quote_contract(self):
        assert quote(5) == 60
