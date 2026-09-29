"""What the pilot itself saved, as opposed to what the battery is worth.

A battery in self-consumption saves money on its own: the inverter discharges
it every evening whether or not anything steers it. Counting that as the
pilot's doing credited an integration that had only ever sent `auto` with
euros a day. The pilot's contribution is the energy it moved differently:

* grid energy it charged in a `charge` slot, and
* energy it held back in an `idle` slot that the battery would otherwise have
  spent right then.

Both go into lots, each with the price that energy stood at. When the
battery later discharges, lots are used up oldest first and the pilot is
credited with what the energy was worth then minus what it cost - which is
negative for a trade that did not pay off. Discharge beyond the lots is plain
self-consumption and earns the pilot nothing.

Every quantity here is battery-side kWh (what the charge and discharge meters
count); `eta_one_way` converts to and from the grid/house side. No Home
Assistant imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Lot:
    """Battery-side energy the pilot is answerable for."""

    kwh: float
    basis: float  # EUR per battery-side kWh


class PilotLedger:
    """Lots plus the running total of what the pilot saved."""

    def __init__(self) -> None:
        self.lots: list[Lot] = []
        self.savings_eur: float = 0.0

    @property
    def total_kwh(self) -> float:
        return sum(lot.kwh for lot in self.lots)

    def book_charge(self, kwh: float, grid_price: float, eta_one_way: float) -> None:
        """Grid charge in a `charge` slot: each stored kWh cost price / eta."""
        if kwh > 0:
            self.lots.append(Lot(kwh, grid_price / eta_one_way))

    def book_hold(self, ac_kwh: float, price: float, eta_one_way: float, room_kwh: float) -> None:
        """Demand an `idle` slot kept the battery from covering.

        Covered then, each stored kWh would have saved price * eta; that is
        what holding it back costs. Only what the battery actually holds
        beyond the existing lots (`room_kwh`) can have been held back.
        """
        stored = min(ac_kwh / eta_one_way, max(0.0, room_kwh - self.total_kwh))
        if stored > 0:
            self.lots.append(Lot(stored, price * eta_one_way))

    def book_discharge(self, kwh: float, value_price: float, eta_one_way: float) -> float:
        """Spend lots oldest first; return (and add up) the pilot's gain."""
        gain = 0.0
        remaining = kwh
        while remaining > 1e-12 and self.lots:
            lot = self.lots[0]
            used = min(remaining, lot.kwh)
            gain += used * (value_price * eta_one_way - lot.basis)
            lot.kwh -= used
            remaining -= used
            if lot.kwh <= 1e-12:
                self.lots.pop(0)
        self.savings_eur += gain
        return gain

    def cap(self, stored_kwh: float) -> None:
        """Lots can never exceed what is in the battery; drop the oldest first.

        Meters and SOC drift, and energy can leave the battery unmetered by the
        pilot's bookkeeping (a restart gap, a mode the pilot did not record).
        """
        excess = self.total_kwh - max(0.0, stored_kwh)
        while excess > 1e-12 and self.lots:
            lot = self.lots[0]
            dropped = min(excess, lot.kwh)
            lot.kwh -= dropped
            excess -= dropped
            if lot.kwh <= 1e-12:
                self.lots.pop(0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "savings_eur": self.savings_eur,
            "lots": [{"kwh": lot.kwh, "basis": lot.basis} for lot in self.lots],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PilotLedger:
        ledger = cls()
        try:
            ledger.savings_eur = float(data.get("savings_eur", 0.0))
            ledger.lots = [Lot(float(row["kwh"]), float(row["basis"])) for row in data["lots"]]
        except (AttributeError, KeyError, TypeError, ValueError):
            ledger.lots = []
        return ledger
