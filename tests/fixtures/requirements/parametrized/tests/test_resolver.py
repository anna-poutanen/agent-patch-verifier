import pytest

from resolver import resolve


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 5), (0, 0), (3, 3)],
)
def test_resolve_cases(value, expected):
    assert resolve(value) == expected
