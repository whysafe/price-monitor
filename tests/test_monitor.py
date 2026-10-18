import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor as m  # noqa: E402


@pytest.mark.parametrize("text, expected", [
    ("12 990 ₽", 12990), ("£51.77", 51.77), ("1.299,00 €", 1299.0), ("$1,299.99", 1299.99),
    ("Цена: 4 590,50 руб.", 4590.5), ("1.299 ₽", 1299), ("от 990", 990), ("нет в наличии", None),
])
def test_parse_price(text, expected):
    assert m.parse_price(text) == expected


def test_extract_by_selector_and_meta():
    html = '<div><span class="old">15 000 ₽</span><span class="price">12 490 ₽</span><meta itemprop="price" content="12490.00"></div>'
    assert m.extract_price(html, "span.price") == 12490
    assert m.extract_price(html, "meta[itemprop=price]") == 12490
    assert m.extract_price(html, ".missing") is None


def test_decide_drop_and_target():
    item = m.Item(1, "https://x", ".p", "Наушники", target=10000, last_price=12000)
    assert "достигла" in m.decide(item, 9900)
    assert "12000 → 11000" in m.decide(item, 11000)
    assert m.decide(item, 12500) is None
