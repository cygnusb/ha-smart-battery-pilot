"""Behavioural tests for the bundled Lovelace card.

The card is plain JavaScript with no build step and no test runner of its own,
so it used to be covered only by `test_translations.py` reading its source as
text. That cannot catch a rendering decision going wrong. Node is present on
every GitHub runner and on most developer machines, so the card is loaded into
a throwaway DOM shim and driven directly; where it is missing the tests skip
rather than fail.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from smart_battery_pilot import config_flow

CARD = Path(config_flow.__file__).parent / "frontend" / "smart-battery-pilot-card.js"

# Just enough of a browser for the card to construct, render into a string and
# read the clock. Anything it touches beyond this belongs in a real browser
# test (see tools/gen_card_screenshots.py).
HARNESS = """
class FakeEl {
  constructor() { this.innerHTML = ""; this.style = {}; }
  querySelector() { return null; }
  addEventListener() {}
  setAttribute() {}
  getBoundingClientRect() { return { left: 0, top: 0, width: 480, height: 230 }; }
}
globalThis.HTMLElement = FakeEl;
globalThis.customElements = { get: () => undefined, define: () => {} };
globalThis.window = globalThis;

const Card = new Function(CARD_SOURCE + "\\nreturn SmartBatteryPilotCard;")();

const T0 = Date.parse("2026-01-15T00:00:00Z");
const SLOT_MS = 15 * 60000;
const slots = Array.from({ length: 8 }, (_, i) => ({
  start: new Date(T0 + i * SLOT_MS).toISOString(),
  end: new Date(T0 + (i + 1) * SLOT_MS).toISOString(),
  action: i % 2 ? "charge" : "auto",
  price: 0.2, net_demand_kwh: 0.3, pv_kwh: 0,
  power_w: 3000, discharge_kwh: 0, soc_forecast: 50,
}));
// One stable object, exactly as Home Assistant hands it out while the plan
// itself has not changed.
const planState = {
  state: "4",
  attributes: {
    slots, price_adapter: "nordpool", error: null, warnings: [],
    updated_at: "2026-01-15T00:00:00+00:00",
  },
};
const hass = {
  states: { "sensor.plan": planState },
  config: { time_zone: "UTC" },
  locale: { language: "en" },
  language: "en",
};

const card = new Card();
card.setConfig({ entity: "sensor.plan" });

let renders = 0;
const render = card._render.bind(card);
card._render = (state) => { renders++; render(state); };

let clock = T0;
Date.now = () => clock;

const at = (ms, updates = 1) => {
  clock = T0 + ms;
  for (let i = 0; i < updates; i++) card.hass = hass;
  return renders;
};

console.log(JSON.stringify({
  first: at(60000),
  same_slot: at(2 * 60000, 3),
  after_boundary: at(SLOT_MS + 60000),
  same_slot_again: at(SLOT_MS + 2 * 60000, 3),
  past_plan_end: at(8 * SLOT_MS + 60000),
  no_current_slot: at(9 * SLOT_MS, 3),
}));
"""


@pytest.fixture(scope="module")
def renders() -> dict[str, int]:
    """Render counts of the card as the clock walks across a slot boundary."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = f"const CARD_SOURCE = {json.dumps(CARD.read_text(encoding='utf-8'))};\n" + HARNESS
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"card harness failed:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_card_redraws_when_the_slot_changes(renders):
    """A slot boundary is invisible in the plan sensor's state.

    Its state (the number of non-auto slots) and its attributes are identical
    on both sides of a boundary, so Home Assistant fires no state_changed
    event and the frontend hands out the very same state object. Comparing
    only that object left the action chip and the "now" marker showing the
    previous slot until the next coordinator refresh - up to 30 minutes into
    a 15-minute slot, which is most of it.
    """
    assert renders["first"] == 1
    assert renders["after_boundary"] == 2


def test_the_card_keeps_its_dom_within_a_slot(renders):
    """`set hass` runs on every state change in the whole system.

    Re-rendering there would throw away an open tooltip several times a
    second, which is why the identity comparison exists in the first place.
    """
    assert renders["same_slot"] == 1
    assert renders["same_slot_again"] == 2


def test_a_plan_that_has_run_out_does_not_spin(renders):
    """No current slot means nothing left to expire.

    Without clearing the cached boundary, a card whose plan has ended would
    consider itself stale forever and re-render on every state change in the
    system until the coordinator delivered a new plan.
    """
    assert renders["past_plan_end"] == 3
    assert renders["no_current_slot"] == 3
