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
for (const view of ["tracks", "balance", "bars", "flow", "compact", "nonsense", null]) {
  const card = new Card();
  const config = { entity: "sensor.plan" };
  if (view !== null) config.view = view;
  card.setConfig(config);
  card.hass = hass;
  out[String(view)] = { html: card.innerHTML, size: card.getCardSize() };
}
// The same plan from an installation counting in kronor.
const sek = new Card();
sek.setConfig({ entity: "sensor.plan" });
sek.hass = {
  ...hass,
  states: {
    "sensor.plan": { ...planState, attributes: { ...planState.attributes, currency: "SEK" } },
  },
};
out.sek = { html: sek.innerHTML, tip: sek._tipRows(sek._slots[0]) };
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


def test_prices_carry_the_plans_currency(views):
    """Nothing is converted, so kronor must not be drawn as euros."""
    assert 'class="ax pl">Price SEK/kWh<' in views["sek"]["html"]
    assert "€" not in views["sek"]["html"]
    assert "0.1000 SEK/kWh" in views["sek"]["tip"][1]


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


def test_bars_view_colours_each_price_by_its_action(views):
    """One bar per slot, in the colour of the planned action; no ribbon."""
    html = views["bars"]["html"]
    bars = re.findall(r'class="pbar (\w+)"', html)
    assert len(bars) == 24
    assert bars[3] == "charge" and bars[18:21] == ["idle"] * 3 and bars[0] == "auto"
    assert 'class="band ' not in html
    assert '<i class="pbar-i"></i>Auto (Price)</span>' in html
    assert 'class="pvarea"' in html and 'class="socline"' in html
    # Only the two views reading the same panels share the toggle.
    assert 'class="viewtog"' not in html


def test_flow_view_draws_the_price_as_a_band(views):
    html = views["flow"]["html"]
    shades = [float(v) for v in re.findall(r'class="pcell" fill-opacity="([\d.]+)"', html)]
    assert len(shades) == 24
    # Cheapest palest, dearest darkest.
    assert min(shades) == shades[0] and max(shades) == shades[-1]
    # The ribbon still runs under the band, but is too thin for words.
    assert 'class="band charge"' in html and 'class="bandtx' not in html
    assert 'class="consarea"' in html and 'class="pvarea"' in html
    assert 'class="ax pl">PV ↑  Consumption ↓  kWh<' in html


def test_flow_view_names_the_operating_window(views):
    html = views["flow"]["html"]
    assert html.count('class="socref"') == 2
    assert ">Max 95%<" in html
    # 0-100 % on the axis, the window as lines rather than as the scale.
    assert 'class="ax pr">100%<' in html and 'class="ax pr">0%<' in html


def test_an_unknown_view_draws_the_default(views):
    """A typo in a dashboard must not cost the user their plan."""
    assert unnumbered(views["nonsense"]["html"]) == unnumbered(views["tracks"]["html"])
    assert unnumbered(views["null"]["html"]) == unnumbered(views["tracks"]["html"])


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


# A fifth harness: the plan of the view harness with an EV source switched on.
# The ev_* keys only exist when one is configured, so the same plan is drawn
# three ways - without the keys, with the keys but nothing for the car, and
# with a charging window - to pin down both the EV marks and that nothing
# changes for everyone else.
EV_HARNESS = """
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
const base = Array.from({ length: 24 }, (_, i) => {
  const pv = i >= 8 && i <= 16 ? 4.0 : 0.0;
  return {
    start: new Date(T0 + i * HOUR).toISOString(),
    end: new Date(T0 + (i + 1) * HOUR).toISOString(),
    action: i === 3 ? "charge" : i >= 18 && i <= 20 ? "idle" : "auto",
    price: 0.1 + (i >= 18 ? 0.5 : 0),
    net_demand_kwh: 0.4 - pv,
    pv_kwh: pv,
    power_w: 3000,
    discharge_kwh: 0,
    soc_forecast: 40 + i,
  };
});
const baseAttrs = {
  price_adapter: "nordpool", error: null, warnings: [],
  min_soc: 10, max_soc: 95,
  grid_charge_kwh: 3.2, battery_discharge_kwh: 5.5,
  updated_at: "2026-01-15T00:00:00+00:00",
};
// 01:00-05:00: four hours of charging with the house battery held (wide
// enough for labels at 480px), 10:00-12:00: two hours, too narrow for one.
const evSlots = base.map((s, i) => {
  const held = i >= 1 && i <= 4;
  const late = i === 10 || i === 11;
  return Object.assign({}, s, {
    ev_kwh: held ? 2.75 : late ? 1.0 : 0,
    ev_source: held ? "smart_cost" : late ? (i === 10 ? "<b>later</b>" : null) : null,
    battery_blocked: held,
    soc_forecast: held ? 31 : s.soc_forecast,
  });
});
const idleSlots = base.map((s) =>
  Object.assign({}, s, { ev_kwh: 0, ev_source: null, battery_blocked: false })
);
const iso = (h) => new Date(T0 + h * HOUR).toISOString();
const plans = {
  plain: Object.assign({ slots: base }, baseAttrs),
  idle: Object.assign({ slots: idleSlots }, baseAttrs, {
    ev_source: "evcc", ev_kwh_total: 0, ev_sessions: [], ev_price_limit: 0.12,
  }),
  ev: Object.assign({ slots: evSlots }, baseAttrs, {
    ev_source: "evcc", ev_kwh_total: 13.0, ev_price_limit: 0.12, ev_status: "ok",
    ev_loadpoints: [{ connected: true, charging: false, mode: "smart", vehicle_soc: 40, limit_soc: 80 }],
    ev_sessions: [
      { start: iso(-3), end: iso(-2), energy_kwh: 5.0, source: "plan" },
      { start: iso(1), end: iso(5), energy_kwh: 12.3, source: "smart_cost" },
    ],
  }),
  ev_nosessions: Object.assign({ slots: evSlots }, baseAttrs, {
    ev_source: "evcc", ev_kwh_total: 13.0, ev_price_limit: null, ev_sessions: [],
  }),
  // Nothing to plan, with the charger's state from a 1.2.3 backend.
  ev_full: Object.assign({ slots: idleSlots }, baseAttrs, {
    ev_source: "evcc", ev_kwh_total: 0, ev_sessions: [], ev_price_limit: null, ev_status: "ok",
    ev_loadpoints: [
      { connected: true, charging: false, mode: "smart", vehicle_soc: 100, limit_soc: 100 },
      { connected: false, charging: false, mode: "off", vehicle_soc: null, limit_soc: null },
    ],
  }),
  ev_unplugged: Object.assign({ slots: idleSlots }, baseAttrs, {
    ev_source: "evcc", ev_kwh_total: 0, ev_sessions: [], ev_price_limit: null, ev_status: "ok",
    ev_loadpoints: [{ connected: false, charging: false, mode: "pv", vehicle_soc: null, limit_soc: null }],
  }),
  ev_offline: Object.assign({ slots: idleSlots }, baseAttrs, {
    ev_source: "evcc", ev_kwh_total: 0, ev_sessions: [], ev_price_limit: null, ev_status: "offline",
    ev_loadpoints: [],
  }),
  // A 1.2.1 backend with both figures: the line on the plan's axis, the
  // label quoting evcc's own limit.
  ev_both: Object.assign({ slots: evSlots }, baseAttrs, {
    ev_source: "evcc", ev_kwh_total: 13.0, ev_price_limit: 0.12, ev_price_limit_evcc: 0.22,
    ev_sessions: [],
  }),
  // A 1.2.1 backend whose two price series are not a constant offset apart:
  // no limit on the plan's axis, but evcc's own figure, and one slot that evcc
  // charges for its plan rather than for the price.
  ev_offset: Object.assign(
    { slots: evSlots.map((s, i) => (i === 11 ? Object.assign({}, s, { ev_source: "plan" }) : s)) },
    baseAttrs,
    {
      ev_source: "evcc", ev_kwh_total: 13.0, ev_price_limit: null, ev_price_limit_evcc: 0.22,
      ev_sessions: [],
    },
  ),
};

Date.now = () => T0 + 30 * 60000;

const draw = (plan, view, width) => {
  const card = new Card();
  card.setConfig({ entity: "sensor.plan", view });
  if (width) card.clientWidth = width;
  card.hass = {
    states: { "sensor.plan": { state: "4", attributes: plan } },
    config: { time_zone: "UTC" },
    locale: { language: "en" },
    language: "en",
  };
  return card;
};

const out = {};
for (const [name, plan] of Object.entries(plans)) {
  out[name] = {};
  for (const view of ["tracks", "balance", "bars", "flow", "compact"]) {
    const card = draw(plan, view);
    out[name][view] = { html: card.innerHTML, panels: card._geo.panels };
  }
}
const ev = draw(plans.ev, "tracks");
const plain = draw(plans.plain, "tracks");
out.tips = {
  held: ev._tipRows(ev._slots[2]),
  late: ev._tipRows(ev._slots[10]),
  late_no_source: ev._tipRows(ev._slots[11]),
  expensive: ev._tipRows(ev._slots[19]),
  plain: plain._tipRows(plain._slots[2]),
};
const offset = draw(plans.ev_offset, "tracks");
out.tips.offset_smart_cost = offset._tipRows(offset._slots[2]);
out.tips.offset_plan = offset._tipRows(offset._slots[11]);
// The compact view's tile strip on a regular and on a narrow card.
out.compact = {};
for (const [name, plan] of [["ev", plans.ev], ["plain", plans.plain], ["idle", plans.idle]]) {
  for (const width of [480, 320]) {
    const card = draw(plan, "compact", width);
    out.compact[`${name}_${width}`] = { html: card.innerHTML, size: card.getCardSize() };
  }
}
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def ev_views() -> dict:
    """The card's markup for one plan without, with idle and with active EV keys."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = f"const CARD_SOURCE = {json.dumps(CARD.read_text(encoding='utf-8'))};\n" + EV_HARNESS
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"card EV harness failed:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def unnumbered(html: str) -> str:
    """clipPath and gradient ids are numbered across every card on the page.

    Two cards therefore never share one, and that is the only difference two
    renders of the same plan may have.
    """
    return re.sub(r"sbp-(clip|g)-\d+", r"sbp-\1-N", html)


EV_MARKERS = ("evwin", "evedge", "evlimit", "evheld", "dirlbl ev", "next ev", "ev-i", "evlim-i")


def test_a_plan_without_ev_charging_renders_exactly_as_before(ev_views):
    """No EV source, or one with nothing planned for the car, changes nothing.

    The keys arrive with every plan once an EV source is configured, also on
    days the car is not charging at all; the card must not tell those apart
    from a plan that never had them, down to the style block.
    """
    for view in ("tracks", "balance", "compact"):
        plain = ev_views["plain"][view]["html"]
        idle = ev_views["idle"][view]["html"]
        assert unnumbered(idle) == unnumbered(plain), f"{view}: idle EV keys changed the markup"
        for marker in EV_MARKERS:
            assert marker not in plain, f"{view}: {marker!r} without EV charging"


def test_ev_windows_run_through_every_panel(ev_views):
    """One column per panel and window, behind the curves, never in the gaps."""
    for view in ("tracks", "balance", "compact"):
        drawn = ev_views["ev"][view]
        rects = re.findall(
            r'<rect x="[\d.]+" y="([\d.]+)" width="[\d.]+" height="([\d.]+)" class="evwin"/>',
            drawn["html"],
        )
        expected = sorted((p["y"], p["h"]) for p in drawn["panels"]) * 2
        assert sorted((float(y), float(h)) for y, h in rects) == sorted(expected), view
        # Two dashed edges per column.
        assert drawn["html"].count('class="evedge"') == 2 * len(rects)
        # Behind every curve: the columns come before the first mark is drawn.
        assert drawn["html"].index('class="evwin"') < drawn["html"].index('class="socline"')


def test_the_price_panel_shows_the_limit_and_the_window_energy(ev_views):
    html = ev_views["ev"]["tracks"]["html"]
    assert html.count('class="evlimit"') == 1
    assert ">evcc limit 0.12</text>" in html
    # 4 x 2.75 kWh in the wide window; the two-hour one has no room for a label.
    assert ">EV 11.0 kWh</text>" in html
    assert ">EV 2.0 kWh</text>" not in html
    # No limit reported: no line, and no legend entry for one.
    nolimit = ev_views["ev_nosessions"]["tracks"]["html"]
    assert 'class="evlimit"' not in nolimit
    assert 'class="li evlim-i"' not in nolimit
    assert '<i class="ev-i"></i>' in nolimit


def test_the_limit_line_quotes_evccs_own_figure(ev_views):
    """The line sits on the card's price axis; its label and the tooltip both
    name the limit as set in evcc, so the two never disagree."""
    html = ev_views["ev_both"]["tracks"]["html"]
    assert html.count('class="evlimit"') == 1
    assert ">evcc limit 0.22</text>" in html
    assert ">evcc limit 0.12</text>" not in html


@pytest.mark.parametrize(
    ("plan", "text"),
    [
        ("ev_full", "EV: 100 % · no charging planned"),
        ("ev_unplugged", "EV: not plugged in"),
        ("ev_offline", "EV: evcc not reachable"),
    ],
)
def test_a_quiet_charger_still_says_how_it_stands(ev_views, plan, text):
    """Nothing planned for the car: every view names the reason, and draws
    nothing else for the car."""
    for view in ("tracks", "balance", "compact"):
        html = ev_views[plan][view]["html"]
        assert f'<span class="next">{text}</span>' in html, view
        for marker in ("evwin", "evlimit", "evheld", "ev-i"):
            assert marker not in html, f"{view}: {marker}"


def test_a_planned_session_replaces_the_idle_text(ev_views):
    for view in ("tracks", "balance", "compact"):
        assert "no charging planned" not in ev_views["ev"][view]["html"], view


def test_bars_and_flow_give_the_car_a_lane(ev_views):
    """The car's kWh per slot on a scale of its own, only while it charges."""
    for view in ("bars", "flow"):
        drawn = ev_views["ev"][view]
        assert drawn["html"].count('class="evbar"') == 6, view
        assert 'class="ax pl ev">EV kWh<' in drawn["html"], view
        lane = next(p for p in drawn["panels"] if p["key"] == "evlane")
        assert lane["h"] == 30, view
        assert not any(p["key"] == "evlane" for p in ev_views["plain"][view]["panels"]), view
        assert 'class="evheld"' in drawn["html"], view
    # The bars carry the limit as a line, the band as dots on cheap slots.
    assert ev_views["ev"]["bars"]["html"].count('class="evlimit"') == 1
    flow = ev_views["ev"]["flow"]["html"]
    assert 'class="evlimit"' not in flow
    assert flow.count('class="evbelow"') == 24 - 6  # every slot below 0.12 €/kWh but 18-23
    assert '<i class="evbelow-i"></i>below evcc limit</span>' in flow


def test_bars_and_flow_without_ev_charging_render_as_before(ev_views):
    for view in ("bars", "flow"):
        plain = ev_views["plain"][view]["html"]
        assert unnumbered(ev_views["idle"][view]["html"]) == unnumbered(plain), view
        for marker in (*EV_MARKERS, "evbar", "evbelow"):
            assert marker not in plain, f"{view}: {marker!r} without EV charging"


def test_the_soc_panel_marks_the_held_battery(ev_views):
    html = ev_views["ev"]["tracks"]["html"]
    assert html.count('class="evheld"') == 1
    assert ">held 31%</text>" in html


def test_the_status_line_names_the_next_session(ev_views):
    """The charger's own session list first; the past session is skipped."""
    for view in ("tracks", "balance"):
        html = ev_views["ev"][view]["html"]
        assert '<span class="next ev">EV: 12.3 kWh · 01:00 AM\u201305:00 AM</span>' in html, view
    # Without a session list the first window on the plan stands in.
    html = ev_views["ev_nosessions"]["tracks"]["html"]
    assert '<span class="next ev">EV: 11.0 kWh · 01:00 AM\u201305:00 AM</span>' in html


def test_the_legend_explains_the_ev_marks(ev_views):
    html = ev_views["ev"]["tracks"]["html"]
    assert '<i class="ev-i"></i>EV charging</span>' in html
    assert '<i class="li evlim-i"></i>evcc limit</span>' in html


def test_the_energy_panel_is_left_to_the_household(ev_views):
    """The car's energy is not drawn there and does not stretch its scale."""
    ticks = re.compile(r'class="ax pr">([^<]+)<')
    assert ticks.findall(ev_views["ev"]["tracks"]["html"]) == ticks.findall(
        ev_views["plain"]["tracks"]["html"]
    )
    energy = next(p for p in ev_views["ev"]["tracks"]["panels"] if p["key"] == "energy")
    for y in re.findall(r'y="([\d.]+)" class="dirlbl ev"', ev_views["ev"]["tracks"]["html"]):
        assert not energy["y"] <= float(y) <= energy["y"] + energy["h"]


def test_the_tooltip_explains_ev_slots(ev_views):
    tips = ev_views["tips"]
    held = tips["held"]
    assert "EV: <b>2.75 kWh</b> · cheap price" in held
    assert "Battery held for EV" in held
    assert held[1].endswith(" ≤ evcc limit 0.12")
    # A reason the card does not know yet is shown, escaped.
    assert "EV: <b>1.00 kWh</b> · &lt;b&gt;later&lt;/b&gt;" in tips["late"]
    assert "EV: <b>1.00 kWh</b>" in tips["late_no_source"]
    assert "Battery held for EV" not in tips["late"]
    # Above the limit and without EV energy: nothing EV about the slot.
    for rows in (tips["expensive"], tips["plain"]):
        assert not any("EV" in row or "limit" in row for row in rows)


def test_the_tooltip_quotes_evccs_own_limit_for_cheap_price_slots(ev_views):
    """evcc chose a smart_cost slot by its own price against its own limit.

    That holds whether or not the limit could be moved onto the plan's price
    axis, so the figure shows even with `ev_price_limit` null - and it is
    evcc's figure, not a comparison with the plan's price on the same row.
    """
    tips = ev_views["tips"]
    assert tips["offset_smart_cost"][1].endswith(" ≤ evcc limit 0.22")
    # Charged for evcc's plan, not for the price: no limit to quote.
    assert "EV: <b>1.00 kWh</b> · plan" in tips["offset_plan"]
    assert "limit" not in tips["offset_plan"][1]
    # With no limit on the plan's axis there is still no line to draw.
    assert 'class="evlimit"' not in ev_views["ev_offset"]["tracks"]["html"]


def tiles(html: str) -> list[str]:
    """The class attribute of every tile in the compact view's strip."""
    return re.findall(r'<div class="(tile(?: [^"]*)?)">', html)


def test_the_compact_view_gives_the_session_a_tile(ev_views):
    """A fourth tile carries the session, so the status line leaves it out."""
    html = ev_views["compact"]["ev_480"]["html"]
    assert '<div class="tiles four">' in html
    assert tiles(html) == ["tile", "tile", "tile", "tile ev"]
    assert (
        '<div class="tile ev"><div class="tl">EV</div><div class="tv">12.3 kWh</div>'
        '<div class="ts">01:00 AM\u201305:00 AM · cheap price</div></div>'
    ) in html
    # Said once: the compact status line has no EV summary, the others keep it.
    assert 'class="next ev"' not in html
    assert 'class="next ev"' not in ev_views["ev"]["compact"]["html"]
    assert 'class="next ev"' in ev_views["ev"]["tracks"]["html"]


def test_four_tiles_go_two_by_two_on_a_narrow_card(ev_views):
    compact = ev_views["compact"]
    assert '<div class="tiles four narrow">' in compact["ev_320"]["html"]
    assert (
        ".tiles.four.narrow { grid-template-columns: repeat(2, 1fr); }" in compact["ev_320"]["html"]
    )
    # The second row is one more row of card.
    assert compact["ev_320"]["size"] > compact["plain_320"]["size"]
    # On one row the strip costs what three tiles did.
    assert compact["ev_480"]["size"] == compact["plain_480"]["size"]


def test_no_ev_tile_without_a_session(ev_views):
    compact = ev_views["compact"]
    for name in ("plain_480", "plain_320", "idle_480", "idle_320"):
        html = compact[name]["html"]
        assert tiles(html) == ["tile", "tile", "tile"], name
        assert "tiles four" not in html, name
        assert 'class="next ev"' not in html, name
    assert unnumbered(compact["idle_320"]["html"]) == unnumbered(compact["plain_320"]["html"])
    assert compact["idle_320"]["size"] == compact["plain_320"]["size"]
