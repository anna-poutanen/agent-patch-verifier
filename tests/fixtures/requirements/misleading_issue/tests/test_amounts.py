import pytest

from amounts import parse_amount


def test_empty_amount_is_rejected():
    with pytest.raises(ValueError):
        parse_amount("")
