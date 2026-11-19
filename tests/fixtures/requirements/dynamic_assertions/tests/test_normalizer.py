from normalizer import normalize


def test_static_missing_contract():
    assert normalize(None) == "missing"


def test_dynamic_attribute_lookup_is_not_static_evidence():
    operation = getattr(__import__("normalizer"), "normalize")
    assert operation(" READY ") == "ready"


def _assert_case(operation, value, expected):
    assert operation(value) == expected


def test_helper_dispatched_assertion_is_not_static_evidence():
    _assert_case(normalize, " YES ", "yes")
