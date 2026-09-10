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
import re
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


# A second harness: render the same plan in every view and hand the produced
# markup back for inspection. The DOM shim above is enough - the card builds
# its whole output as a string and assigns it to `innerHTML`.
VIEW_HARNESS = """
class FakeEl {
  constructor() { this.innerHTML = ""; this.style = {}; }
  querySelector() { return null; }
  addEventListener() {}
  setAttribute() {}
  getBoundingClientRect() { return { left: 0, top: 0, width: 480, height: 300 }; }
}
globalThis.HTMLElement = FakeEl;
globalThis.customElements = { get: () => undefined, define: () => {} };
globalThis.window = globalThis;

const Card = new Function(CARD_SOURCE + "\\nreturn SmartBatteryPilotCard;")();

const T0 = Date.parse("2026-01-15T00:00:00Z");
const HOUR = 3600000;
// A sunny day: PV an order of magnitude above the household load. Sharing one
// scale normalized to the PV maximum is what used to press the consumption
// curve flat onto the baseline.
const slots = Array.from({ length: 24 }, (_, i) => {
  const pv = i >= 8 && i <= 16 ? 4.0 : 0.0;
  const cons = 0.4;
  return {
    start: new Date(T0 + i * HOUR).toISOString(),
    end: new Date(T0 + (i + 1) * HOUR).toISOString(),
    action: i === 3 ? "charge" : i >= 18 && i <= 20 ? "idle" : "auto",
    price: 0.1 + (i >= 18 ? 0.5 : 0),
    net_demand_kwh: cons - pv,
    pv_kwh: pv,
    power_w: 3000,
    discharge_kwh: 0,
    soc_forecast: 40 + i,
  };
});
const planState = {
  state: "4",
  attributes: {
    slots, price_adapter: "nordpool", error: null, warnings: [],
    min_soc: 10, max_soc: 95,
    grid_charge_kwh: 3.2, battery_discharge_kwh: 5.5,
    updated_at: "2026-01-15T00:00:00+00:00",
  },
};
const hass = {
  states: { "sensor.plan": planState },
  config: { time_zone: "UTC" },
  locale: { language: "en" },
  language: "en",
};

Date.now = () => T0 + 30 * 60000;

const out = {};
for (const view of ["tracks", "balance", "compact", "nonsense", null]) {
  const card = new Card();
  const config = { entity: "sensor.plan" };
  if (view !== null) config.view = view;
  card.setConfig(config);
  card.hass = hass;
  out[String(view)] = { html: card.innerHTML, size: card.getCardSize() };
}
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def views() -> dict[str, dict]:
    """The card's markup in each view."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = f"const CARD_SOURCE = {json.dumps(CARD.read_text(encoding='utf-8'))};\n" + VIEW_HARNESS
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"card view harness failed:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_every_unit_gets_its_own_panel(views):
    """The default view is three stacked scales, not three units on one plot.

    PV and consumption used to share the lower 45 % of the price plot,
    normalized to the PV maximum - on the sunny day this fixture describes
    that is a tenfold squeeze, and a perfectly correct consumption forecast
    ends up drawn along the baseline.
    """
    html = views["tracks"]["html"]
    for label in ("Price €/kWh", "Energy kWh", "SOC"):
        assert f'class="ax pl">{label}<' in html
    assert 'class="pvarea"' in html
    assert 'class="consline"' in html
    assert 'class="socline"' in html


def test_the_soc_track_uses_the_configured_window(views):
    """0-100 % spends most of the panel on a range the battery cannot enter."""
    html = views["tracks"]["html"]
    assert 'class="ax pr">10%<' in html
    assert 'class="ax pr">95%<' in html
    assert 'class="ax pr">0%<' not in html
    assert 'class="ax pr">100%<' not in html


def test_filled_areas_are_a_single_subpath(views):
    """An area whose steps start with `M` opens a second subpath.

    The closing `Z` then runs from the last point back to that `M` instead of
    along the baseline, drawing a diagonal across the whole panel.
    """
    paths = re.findall(
        r'<path d="([^"]+)" class="(?:pvarea|socarea|pricearea|balarea[^"]*)"',
        views["tracks"]["html"] + views["balance"]["html"],
    )
    assert paths, "no filled areas rendered"
    for d in paths:
        assert d.count("M") == 1, f"area path has {d.count('M')} subpaths"


def test_balance_view_replaces_the_energy_panel(views):
    html = views["balance"]["html"]
    assert 'class="ax pl">Balance kWh<' in html
    assert 'class="balarea pos"' in html and 'class="balarea neg"' in html
    assert 'class="pvarea"' not in html
    assert 'class="consline"' not in html
    # Price and SOC are untouched - only the middle panel changes.
    assert 'class="ax pl">Price €/kWh<' in html
    assert 'class="socline"' in html


def test_compact_view_leads_with_figures(views):
    html = views["compact"]["html"]
    assert 'class="tiles"' in html
    assert "Next change" in html and "From battery" in html
    assert 'class="pvarea"' not in html
    assert views["compact"]["size"] < views["tracks"]["size"]


def test_an_unknown_view_draws_the_default(views):
    """A typo in a dashboard must not cost the user their plan."""
    assert views["nonsense"]["html"] == views["tracks"]["html"]
    assert views["null"]["html"] == views["tracks"]["html"]
