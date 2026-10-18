"""Sizing euro-written numbers to another currency, and reading a sensor's."""

from __future__ import annotations

import pytest

from smart_battery_pilot.currency import (
    currency_from_attrs,
    price_field_max,
    price_scale,
    scaled,
)


def test_euro_numbers_stay_exactly_what_they_were():
    assert scaled(0.20, "EUR") == 0.20
    assert scaled(0.08, "EUR") == 0.08
    assert scaled(0.0, "EUR") == 0.0
    assert price_field_max("EUR") == 1


@pytest.mark.parametrize(
    ("value", "currency", "expected"),
    [
        (0.20, "SEK", 2.2),
        (0.08, "SEK", 0.88),
        (0.20, "HUF", 80),
        (0.08, "CZK", 2.0),
        (-0.10, "NOK", -1.2),
    ],
)
def test_other_currencies_get_a_round_number_of_the_same_size(value, currency, expected):
    assert scaled(value, currency) == pytest.approx(expected)


def test_an_unknown_currency_keeps_the_euro_number_and_a_wide_field():
    assert price_scale("JPY") is None
    assert scaled(0.20, "JPY") == 0.20
    assert price_field_max("JPY") >= 100


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        ({"currency": "SEK", "unit_of_measurement": "öre/kWh"}, "SEK"),
        ({"currency": "nok"}, "NOK"),
        ({"unit_of_measurement": "SEK/kWh"}, "SEK"),
        ({"unit_of_measurement": "EUR/kWh"}, "EUR"),
        ({"unit_of_measurement": "€/kWh"}, "EUR"),
        ({"unit_of_measurement": "ct/kWh"}, "EUR"),
        ({"unit": "c€/kWh"}, "EUR"),
        # öre and øre belong to three currencies - no guess.
        ({"unit_of_measurement": "öre/kWh"}, None),
        ({"unit_of_measurement": "kWh"}, None),
        ({}, None),
        ({"currency": "kronor"}, None),
    ],
)
def test_the_currency_a_price_entity_declares(attrs, expected):
    assert currency_from_attrs(attrs) == expected
