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


# A third harness: the same plan drawn into cards of very different sizes.
# The card measures the box the dashboard gives it, so the shim has to be able
# to report a width - the two harnesses above deliberately report none, which
# is the "nothing to measure yet" path.
SIZE_HARNESS = """
class FakeEl {
  constructor() { this.innerHTML = ""; this.style = {}; this.clientWidth = 0; this.clientHeight = 0; }
  querySelector() { return null; }
  addEventListener() {}
  setAttribute() {}
  getBoundingClientRect() { return { left: 0, top: 0, width: this.clientWidth, height: 0 }; }
}
globalThis.HTMLElement = FakeEl;
globalThis.customElements = { get: () => undefined, define: () => {} };
globalThis.window = globalThis;

const Card = new Function(CARD_SOURCE + "\\nreturn SmartBatteryPilotCard;")();

const T0 = Date.parse("2026-01-15T00:00:00Z");
const HOUR = 3600000;
const slots = Array.from({ length: 24 }, (_, i) => ({
  start: new Date(T0 + i * HOUR).toISOString(),
  end: new Date(T0 + (i + 1) * HOUR).toISOString(),
  action: i === 3 ? "charge" : "auto",
  price: 0.1 + i * 0.01,
  net_demand_kwh: 0.4,
  pv_kwh: i >= 8 && i <= 16 ? 2.0 : 0.0,
  power_w: 3000,
  discharge_kwh: 0,
  soc_forecast: 40 + i,
}));
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

const draw = (config, width, height) => {
  const card = new Card();
  card.setConfig(Object.assign({ entity: "sensor.plan" }, config));
  card.clientWidth = width;
  card.clientHeight = height || 0;
  card.hass = hass;
  const box = card.innerHTML.match(/viewBox="0 0 ([\\d.]+) ([\\d.]+)"/);
  return {
    html: card.innerHTML,
    width: Number(box[1]),
    height: Number(box[2]),
    panels: card._geo.panels,
    hours: (card.innerHTML.match(/class="ax tx">/g) || []).length,
    size: card.getCardSize(),
  };
};

const out = { widths: {}, fixed: draw({ height: 200 }, 900), unmeasured: draw({}, 0) };
for (const w of [240, 320, 480, 900, 1400]) out.widths[w] = draw({}, w);

// A four-day plan on a phone-width card: the only case where two day
// separators come close enough for their dates to run into each other.
for (let d = 1; d < 4; d++) {
  for (let i = 0; i < 24; i++) {
    const s = slots[i];
    slots.push(Object.assign({}, s, {
      start: new Date(Date.parse(s.start) + d * 24 * HOUR).toISOString(),
      end: new Date(Date.parse(s.end) + d * 24 * HOUR).toISOString(),
    }));
  }
}
out.long_narrow = draw({}, 240);
out.long_wide = draw({}, 1400);
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def sizes() -> dict:
    """The card's geometry across a range of card widths."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = f"const CARD_SOURCE = {json.dumps(CARD.read_text(encoding='utf-8'))};\n" + SIZE_HARNESS
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"card size harness failed:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_chart_is_drawn_at_the_size_it_is_given(sizes):
    """One SVG unit is one CSS pixel, whatever width the dashboard hands over.

    This is what keeps 9px axis labels 9px: the card used to draw into a fixed
    480-unit viewBox stretched to 100% width, so a 1400px card magnified every
    label, stroke and dot by 2.9x.
    """
    for width, drawn in sizes["widths"].items():
        assert drawn["width"] == int(width), f"{width}px card drew a {drawn['width']}-unit box"


def test_height_grows_with_width_but_stops(sizes):
    """Wider means somewhat taller, never proportionally taller."""
    by_width = sizes["widths"]
    assert by_width["240"]["height"] < by_width["480"]["height"] < by_width["900"]["height"]
    # The old fixed aspect ratio would have made the 1400px card 817 units tall.
    assert by_width["1400"]["height"] <= 440
    assert by_width["240"]["height"] >= 150


def test_the_chart_fits_inside_its_own_viewbox(sizes):
    """Every panel is drawable and the stack never runs off the bottom edge."""
    for width, drawn in [*sizes["widths"].items(), ("fixed", sizes["fixed"])]:
        for panel in drawn["panels"]:
            assert panel["h"] >= 30, f"{width}: {panel['key']} panel is {panel['h']} tall"
        last = drawn["panels"][-1]
        assert last["y"] + last["h"] <= drawn["height"], f"{width}: panels overflow the viewBox"


def test_the_hour_grid_thins_out_when_there_is_no_room(sizes):
    """A fixed three-hour grid collided at phone widths and looked sparse at 1400px."""
    counts = [sizes["widths"][w]["hours"] for w in ("240", "320", "480", "900", "1400")]
    assert counts == sorted(counts), f"label count is not monotonic in width: {counts}"
    assert counts[0] < counts[-1]
    # Every label keeps at least 30px to itself, on the narrowest card too.
    for width in sizes["widths"]:
        drawn = sizes["widths"][width]
        assert drawn["width"] / max(1, drawn["hours"]) >= 30


def test_the_weekday_is_dropped_when_the_day_separators_crowd(sizes):
    """A weekday plus a date is wider than four day separators leave at 240px."""
    assert "Thu" in sizes["widths"]["240"]["html"], "a one-day plan has room to spare"
    assert "Thu" in sizes["long_wide"]["html"]
    assert "Thu" not in sizes["long_narrow"]["html"]


def test_a_configured_height_overrides_the_automatic_one(sizes):
    """`height: 200` wins over what the width would have chosen."""
    assert sizes["fixed"]["height"] == 200
    assert sizes["widths"]["900"]["height"] != 200
    assert sizes["fixed"]["size"] < sizes["widths"]["900"]["size"]


def test_an_unmeasurable_card_still_draws(sizes):
    """Off-DOM, or under a shim with no layout: fall back, do not divide by zero."""
    assert sizes["unmeasured"]["width"] == 480
    assert sizes["unmeasured"]["height"] == 280


# A fourth harness, for the one thing the string-rendering shims above cannot
# see: which listeners actually get attached. The shim reports no layout at
# all - every box is zero, as it is for a card first drawn on a dashboard tab
# that is not in front - and the card still has to end up interactive.
EVENT_HARNESS = """
const listeners = [];
class FakeEl {
  constructor() { this.innerHTML = ""; this.style = {}; this.dataset = {}; }
  querySelector(sel) {
    // Everything the card looks for exists; nothing has been laid out.
    const el = new FakeEl();
    el.selector = sel;
    return el;
  }
  addEventListener(type) { listeners.push(`${this.selector || "host"}:${type}`); }
  setAttribute() {}
  getBoundingClientRect() { return { left: 0, top: 0, width: 0, height: 0 }; }
}
globalThis.HTMLElement = FakeEl;
globalThis.customElements = { get: () => undefined, define: () => {} };
globalThis.window = globalThis;

const Card = new Function(CARD_SOURCE + "\\nreturn SmartBatteryPilotCard;")();

const T0 = Date.parse("2026-01-15T00:00:00Z");
const HOUR = 3600000;
const slots = Array.from({ length: 24 }, (_, i) => ({
  start: new Date(T0 + i * HOUR).toISOString(),
  end: new Date(T0 + (i + 1) * HOUR).toISOString(),
  action: "auto", price: 0.1 + i * 0.01, net_demand_kwh: 0.4, pv_kwh: 0,
  power_w: 0, discharge_kwh: 0, soc_forecast: 40 + i,
}));
const hass = {
  states: { "sensor.plan": { state: "4", attributes: {
    slots, price_adapter: "nordpool", error: null, warnings: [],
    min_soc: 10, max_soc: 95,
  } } },
  config: { time_zone: "UTC" },
  locale: { language: "en" },
  language: "en",
};
Date.now = () => T0 + 30 * 60000;

const card = new Card();
card.setConfig({ entity: "sensor.plan" });
card.hass = hass;
console.log(JSON.stringify(listeners));
"""


@pytest.fixture(scope="module")
def listeners() -> list[str]:
    """Which listeners the card attaches when nothing has a layout yet."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = (
        f"const CARD_SOURCE = {json.dumps(CARD.read_text(encoding='utf-8'))};\n" + EVENT_HARNESS
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"card event harness failed:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_tooltip_is_wired_up_even_with_nothing_to_measure(listeners):
    """A card first drawn off-screen must still be interactive once it is shown.

    The chrome measurement that a percentage height needs used to sit *above*
    the listener registration and return early when the card had no layout —
    so a card rendered on a background tab never got a pointermove handler,
    and the tooltip stayed dead for the rest of its life.
    """
    assert "svg:pointermove" in listeners
    assert "svg:pointerleave" in listeners
    assert ".viewtog:click" in listeners
