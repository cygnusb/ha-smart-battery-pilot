"""The pilot's ledger: only energy the pilot put in or held back earns savings."""

from __future__ import annotations

import pytest

from smart_battery_pilot.savings_ledger import PilotLedger

ETA = 0.9  # one-way efficiency, to keep the arithmetic readable


def test_grid_charge_discharged_later_earns_the_spread_after_losses():
    ledger = PilotLedger()
    ledger.book_charge(1.0, grid_price=0.10, eta_one_way=ETA)  # cost 0.10/0.9 per stored kWh
    gain = ledger.book_discharge(1.0, value_price=0.40, eta_one_way=ETA)
    assert gain == pytest.approx(0.9 * 0.40 - 0.10 / 0.9)
    assert ledger.savings_eur == pytest.approx(gain)
    assert ledger.total_kwh == 0.0


def test_a_bad_trade_books_a_loss():
    ledger = PilotLedger()
    ledger.book_charge(1.0, grid_price=0.30, eta_one_way=ETA)
    assert ledger.book_discharge(1.0, value_price=0.30, eta_one_way=ETA) < 0


def test_discharge_without_lots_earns_nothing():
    """Plain self-consumption is what the inverter does anyway."""
    ledger = PilotLedger()
    assert ledger.book_discharge(5.0, value_price=0.40, eta_one_way=ETA) == 0.0
    assert ledger.savings_eur == 0.0


def test_lots_are_consumed_oldest_first():
    ledger = PilotLedger()
    ledger.book_charge(1.0, grid_price=0.10, eta_one_way=1.0)
    ledger.book_charge(1.0, grid_price=0.20, eta_one_way=1.0)
    assert ledger.book_discharge(1.0, value_price=0.40, eta_one_way=1.0) == pytest.approx(0.30)
    assert ledger.book_discharge(1.0, value_price=0.40, eta_one_way=1.0) == pytest.approx(0.20)


def test_a_partial_discharge_splits_a_lot():
    ledger = PilotLedger()
    ledger.book_charge(2.0, grid_price=0.10, eta_one_way=1.0)
    ledger.book_discharge(0.5, value_price=0.40, eta_one_way=1.0)
    assert ledger.total_kwh == pytest.approx(1.5)


def test_held_back_energy_earns_the_price_difference_when_used_later():
    """Blocking at 0.20 and covering the same demand later at 0.40 saves 0.20/kWh."""
    ledger = PilotLedger()
    ledger.book_hold(1.0 * ETA, price=0.20, eta_one_way=ETA, room_kwh=10.0)  # 0.9 AC kWh held
    assert ledger.total_kwh == pytest.approx(1.0)
    gain = ledger.book_discharge(1.0, value_price=0.40, eta_one_way=ETA)
    assert gain == pytest.approx(0.9 * (0.40 - 0.20))


def test_holding_is_capped_by_what_the_battery_holds():
    ledger = PilotLedger()
    ledger.book_charge(1.0, grid_price=0.10, eta_one_way=1.0)
    ledger.book_hold(5.0, price=0.20, eta_one_way=1.0, room_kwh=3.0)
    assert ledger.total_kwh == pytest.approx(3.0)


def test_capping_drops_the_oldest_lots():
    ledger = PilotLedger()
    ledger.book_charge(1.0, grid_price=0.10, eta_one_way=1.0)
    ledger.book_charge(1.0, grid_price=0.20, eta_one_way=1.0)
    ledger.cap(1.5)
    assert ledger.total_kwh == pytest.approx(1.5)
    # 0.5 of the 0.10 lot is gone; the next discharge starts with its rest.
    assert ledger.book_discharge(0.5, value_price=0.40, eta_one_way=1.0) == pytest.approx(0.15)


def test_round_trip():
    ledger = PilotLedger()
    ledger.book_charge(1.0, grid_price=0.10, eta_one_way=ETA)
    ledger.book_discharge(0.3, value_price=0.40, eta_one_way=ETA)
    restored = PilotLedger.from_dict(ledger.to_dict())
    assert restored.savings_eur == pytest.approx(ledger.savings_eur)
    assert restored.total_kwh == pytest.approx(ledger.total_kwh)
    assert restored.to_dict() == ledger.to_dict()


def test_a_mangled_store_entry_starts_empty():
    assert PilotLedger.from_dict({"lots": "nonsense"}).total_kwh == 0.0
