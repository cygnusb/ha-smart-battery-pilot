#!/usr/bin/env python3
"""Render the Lovelace card for the README, using the real optimizer.

The scenarios below describe an *input* day - prices, household consumption,
PV production - and the plan is then computed by `build_plan()`, exactly as
the coordinator does it. Hand-written slot lists drift away from what the
integration actually plans; these cannot.

Every shot is rendered twice, in a light and a dark Home Assistant-like theme,
so the README can serve the one matching the reader's GitHub theme. The card
sits on a dashboard background with a margin around it, and the screenshot is
taken of that frame element, so nothing is ever cropped by the viewport.
Captions belong in the README, not in the images.

Usage:
    npm install --prefix /tmp playwright   # browsers are found in the cache
    # If it asks for a browser revision the cache does not have, pin the
    # playwright release that matches one it has (e.g. playwright@1.63.0).
    python3 tools/gen_card_screenshots.py [--lang en|de] [--theme light|dark|both]
    python3 tools/gen_card_screenshots.py --sizes   # layout check at several widths

Output: assets/screenshots/card_<shot>_<theme>.png for English. Any other
language, and --sizes, go to a work directory instead, so a translation check
never overwrites the images the README shows.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

REPO = Path(__file__).resolve().parent.parent
# The optimizer itself has no Home Assistant dependencies, but importing it
# goes through the package __init__, which does. The test stubs cover that.
sys.path.insert(0, str(REPO / "tests/stubs"))
sys.path.insert(0, str(REPO / "custom_components"))

from smart_battery_pilot.ev_sessions import limit_on_price_basis  # noqa: E402
from smart_battery_pilot.optimizer import (  # noqa: E402
    BatteryState,
    InputSlot,
    OptimizerConfig,
    build_plan,
)
from smart_battery_pilot.price_adapters.base import PriceSlot  # noqa: E402

CARD_JS = REPO / "custom_components/smart_battery_pilot/frontend/smart-battery-pilot-card.js"
OUT_DIR = REPO / "assets/screenshots"

# Where `npm install playwright` put the package. The browser binaries come
# from the shared ms-playwright cache, so only the package itself is needed.
PLAYWRIGHT_DIR = os.environ.get("PLAYWRIGHT_DIR", "/tmp/node_modules/playwright")


# ---------------------------------------------------------------------------
# Scenarios: describe the day, let the optimizer decide the actions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvSession:
    """A charging session evcc has planned, as the coordinator receives it.

    `kwh` holds the energy per hourly slot from `start` on. `evcc_offset` is
    how much more the planner's prices are than evcc's (fees evcc does not
    know about), so the limit goes through the same conversion the
    coordinator applies.
    """

    start: datetime
    kwh: list[float]
    source: str = "smart_cost"
    limit_evcc: float | None = None
    evcc_offset: float = 0.0

    @property
    def end(self) -> datetime:
        return self.start + timedelta(hours=len(self.kwh))

    def kwh_at(self, slot_start: datetime) -> float:
        index = int((slot_start - self.start) / timedelta(hours=1))
        return self.kwh[index] if 0 <= index < len(self.kwh) else 0.0


class Scenario:
    """One day of inputs plus the battery and optimizer settings to plan it."""

    def __init__(
        self,
        name: str,
        prices: list[float],
        consumption: list[float],
        pv: list[float],
        battery: BatteryState,
        config: OptimizerConfig,
        now: datetime,
        pv_power_w: float | None = None,
        horizon: int = 36,
        ev: EvSession | None = None,
    ) -> None:
        self.name = name
        self.prices = prices
        self.consumption = consumption
        self.pv = pv
        self.battery = battery
        self.config = config
        # Pinned wall clock: the plan starts with the slot containing it, the
        # way `merge_future_slots` leaves it for the coordinator.
        self.now = now
        self.start = now.replace(minute=0, second=0, microsecond=0)
        self.pv_power_w = pv_power_w
        # 36 hourly slots from noon is what a user sees once tomorrow's prices
        # are published; a plan made later in the day ends at the same midnight.
        self.horizon = horizon
        self.ev = ev

    def build(self, start: datetime):
        """Run the real optimizer over the horizon starting at `start`.

        The profiles are indexed by hour of day, so a plan that begins at noon
        gets the noon price, not the midnight one.
        """
        slots = []
        for i in range(self.horizon):
            slot_start = start + timedelta(hours=i)
            h = slot_start.hour
            ev_kwh = self.ev.kwh_at(slot_start) if self.ev else 0.0
            slots.append(
                InputSlot(
                    price_slot=PriceSlot(
                        start=slot_start,
                        end=slot_start + timedelta(hours=1),
                        price=self.prices[h],
                    ),
                    net_demand_kwh=round(self.consumption[h] - self.pv[h], 3),
                    pv_kwh=self.pv[h],
                    ev_kwh=ev_kwh,
                    # EV policy "external": evcc holds the house battery for
                    # the whole session, as the coordinator would mark it.
                    battery_blocked=ev_kwh > 0 and self.config.ev_no_grid_charge,
                )
            )
        return build_plan(slots, self.battery, self.config)


def _local(*args: int) -> datetime:
    """A fixed local instant, so the rendered clock does not drift."""
    return datetime(*args).astimezone()


def winter_arbitrage() -> Scenario:
    """Cold winter day: no PV worth the name, cheap night, two demand peaks."""
    prices = [
        0.09,
        0.08,
        0.07,
        0.07,
        0.08,
        0.12,
        0.24,
        0.31,
        0.29,
        0.24,
        0.21,
        0.19,
        0.18,
        0.17,
        0.18,
        0.21,
        0.27,
        0.34,
        0.38,
        0.35,
        0.30,
        0.22,
        0.15,
        0.11,
    ]
    consumption = [
        0.5,
        0.45,
        0.45,
        0.45,
        0.5,
        0.7,
        1.3,
        1.5,
        1.1,
        0.8,
        0.7,
        0.7,
        0.8,
        0.7,
        0.7,
        0.8,
        1.0,
        1.4,
        1.6,
        1.5,
        1.2,
        0.9,
        0.7,
        0.5,
    ]
    return Scenario(
        name="card_winter_arbitrage",
        prices=prices,
        consumption=consumption,
        pv=[0.0] * 24,
        battery=BatteryState(
            capacity_kwh=12.8,
            soc=22.0,
            min_soc=10.0,
            max_soc=95.0,
            max_charge_power_w=5000,
            max_discharge_power_w=5000,
            efficiency=90,
        ),
        config=OptimizerConfig(
            spread_threshold=0.10,
            discharge_mode="self_consumption",
            feed_in_tariff=0.08,
        ),
        now=_local(2026, 1, 13, 12, 20),
    )


def summer_export() -> Scenario:
    """Sunny day with a strong evening peak and market-price export."""
    prices = [
        0.16,
        0.14,
        0.13,
        0.13,
        0.14,
        0.17,
        0.21,
        0.24,
        0.20,
        0.13,
        0.07,
        0.03,
        0.01,
        0.01,
        0.03,
        0.08,
        0.15,
        0.23,
        0.34,
        0.44,
        0.41,
        0.33,
        0.25,
        0.19,
    ]
    consumption = [
        0.4,
        0.35,
        0.35,
        0.35,
        0.4,
        0.5,
        0.8,
        0.9,
        0.8,
        0.7,
        0.7,
        0.7,
        0.8,
        0.7,
        0.7,
        0.7,
        0.8,
        1.0,
        1.3,
        1.5,
        1.3,
        1.0,
        0.8,
        0.5,
    ]
    # A 6 kWp array on a hazy day: enough surplus to fill part of the battery,
    # not enough to saturate it - so the near-free midday hours are still worth
    # buying from the grid.
    pv = [
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.1,
        0.3,
        0.8,
        1.4,
        1.9,
        2.3,
        2.5,
        2.6,
        2.4,
        2.0,
        1.5,
        0.9,
        0.4,
        0.1,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    ]
    return Scenario(
        name="card_summer_export",
        prices=prices,
        consumption=consumption,
        pv=pv,
        battery=BatteryState(
            capacity_kwh=12.8,
            soc=35.0,
            min_soc=10.0,
            max_soc=95.0,
            max_charge_power_w=5000,
            max_discharge_power_w=5000,
            efficiency=90,
        ),
        config=OptimizerConfig(
            spread_threshold=0.05,
            discharge_mode="export",
            feed_in_tariff=0.0,  # sell at the market price
            price_offset=0.0,
        ),
        now=_local(2026, 6, 16, 12, 20),
        pv_power_w=2380.0,
    )


def dunkelflaute() -> Scenario:
    """No wind, no sun, extreme spread - the case the integration exists for."""
    prices = [
        0.14,
        0.11,
        0.09,
        0.09,
        0.12,
        0.22,
        0.41,
        0.52,
        0.47,
        0.38,
        0.33,
        0.30,
        0.29,
        0.28,
        0.30,
        0.35,
        0.46,
        0.61,
        0.68,
        0.59,
        0.48,
        0.33,
        0.22,
        0.16,
    ]
    consumption = [
        0.6,
        0.55,
        0.55,
        0.55,
        0.6,
        0.9,
        1.6,
        1.8,
        1.3,
        1.0,
        0.9,
        0.9,
        1.0,
        0.9,
        0.9,
        1.0,
        1.3,
        1.7,
        2.0,
        1.8,
        1.4,
        1.1,
        0.8,
        0.6,
    ]
    return Scenario(
        name="card_dunkelflaute",
        prices=prices,
        consumption=consumption,
        pv=[0.0] * 24,
        battery=BatteryState(
            capacity_kwh=12.8,
            soc=15.0,
            min_soc=10.0,
            max_soc=95.0,
            max_charge_power_w=6000,
            max_discharge_power_w=6000,
            efficiency=90,
        ),
        config=OptimizerConfig(
            spread_threshold=0.15,
            discharge_mode="self_consumption",
            feed_in_tariff=0.08,
        ),
        now=_local(2026, 1, 13, 12, 20),
    )


def ev_overnight() -> Scenario:
    """Winter evening, car plugged in: evcc charges it in the cheapest hours.

    evcc has planned 25 kWh at 11 kW below its price limit (`smart_cost`),
    and under EV policy "external" it holds the house battery for the whole
    session. The optimizer can neither grid-charge the battery in those slots
    nor let the car drain it, so it fills the battery in the cheap hours
    around the session instead.
    """
    winter = winter_arbitrage()
    # A late evening that stays expensive until midnight: the drop comes with
    # the new day, and the charger's limit label at the right edge of the price
    # panel stays clear of the curve.
    prices = [*winter.prices[:21], 0.30, 0.29, 0.28]
    return Scenario(
        name="card_ev_overnight",
        prices=prices,
        consumption=winter.consumption,
        pv=[0.0] * 24,
        battery=BatteryState(
            capacity_kwh=12.8,
            soc=45.0,
            min_soc=10.0,
            max_soc=95.0,
            max_charge_power_w=5000,
            max_discharge_power_w=5000,
            efficiency=90,
        ),
        config=OptimizerConfig(
            spread_threshold=0.10,
            discharge_mode="self_consumption",
            feed_in_tariff=0.08,
            ev_no_grid_charge=True,
        ),
        now=_local(2026, 1, 13, 18, 20),
        horizon=30,  # until tomorrow midnight, like a plan made at 18:00
        ev=EvSession(
            start=_local(2026, 1, 14, 2, 0),
            kwh=[11.0, 11.0, 3.0],
            source="smart_cost",
            limit_evcc=0.13,
            evcc_offset=0.02,
        ),
    )


SCENARIOS = [winter_arbitrage, summer_export, dunkelflaute, ev_overnight]


# ---------------------------------------------------------------------------
# Serialize a plan the way ChargePlanSensor does
# ---------------------------------------------------------------------------

PLAN_ENTITY = "sensor.smart_battery_pilot_charge_plan"
PV_ENTITY = "sensor.pv_power"


def plan_state(scenario: Scenario, plan, now: datetime) -> dict:
    slots = [
        {
            "start": s.start.isoformat(),
            "end": s.end.isoformat(),
            "action": s.action,
            "price": round(s.price, 4),
            "net_demand_kwh": round(s.net_demand_kwh, 3),
            "pv_kwh": round(s.pv_kwh, 3),
            "power_w": s.power_w,
            "charge_power_w": s.power_w,
            "discharge_kwh": s.discharge_kwh,
            "soc_forecast": s.soc_forecast,
        }
        for s in plan.slots
    ]
    ev = {}
    if scenario.ev is not None:
        session = scenario.ev
        # Per-slot EV keys, as the sensor adds them once an EV source is set.
        for slot, s in zip(slots, plan.slots, strict=True):
            slot["ev_kwh"] = s.ev_kwh
            slot["ev_source"] = session.source if s.ev_kwh > 0 else None
            slot["battery_blocked"] = s.battery_blocked
        # evcc's tariff, a constant offset below the planner's prices.
        rates = [(s.start, s.end, round(s.price - session.evcc_offset, 4)) for s in plan.slots]
        ev = {
            "ev_source": "evcc",
            "ev_kwh_total": round(sum(s.ev_kwh for s in plan.slots), 2),
            "ev_sessions": [
                {
                    "start": session.start.isoformat(),
                    "end": session.end.isoformat(),
                    "energy_kwh": round(sum(session.kwh), 2),
                    "source": session.source,
                }
            ],
            "ev_price_limit": limit_on_price_basis(
                session.limit_evcc, rates, [(s.start, s.end, s.price) for s in plan.slots]
            ),
            "ev_price_limit_evcc": session.limit_evcc,
        }
    return {
        "entity_id": PLAN_ENTITY,
        "state": str(sum(1 for s in plan.slots if s.action != "auto")),
        "attributes": {
            "slots": slots,
            **ev,
            "total_slots": len(slots),
            "grid_charge_kwh": round(plan.grid_charge_kwh, 2),
            "battery_discharge_kwh": round(plan.battery_discharge_kwh, 2),
            "min_soc": scenario.battery.min_soc,
            "max_soc": scenario.battery.max_soc,
            "price_adapter": "nordpool",
            "updated_at": now.isoformat(),
            "error": None,
            "warnings": list(plan.warnings),
            "pv_power_entity": PV_ENTITY if scenario.pv_power_w is not None else None,
            "pv_power_w": scenario.pv_power_w,
        },
    }


# ---------------------------------------------------------------------------
# HTML harness
# ---------------------------------------------------------------------------

HTML_TEMPLATE = textwrap.dedent("""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{
{theme_vars}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--dashboard-background);
          font-family: Roboto, "Helvetica Neue", Arial, sans-serif;
          -webkit-font-smoothing: antialiased; }}
  /* The screenshot is taken of this element: the card plus a margin of
     dashboard around it, whatever height the card turns out to need. */
  .frame {{ display: inline-block; padding: {margin}px; background: var(--dashboard-background); }}
  smart-battery-pilot-card {{ display: block; width: {width}px; }}
  ha-card {{
    display: block;
    background: var(--card-background-color);
    color: var(--primary-text-color);
    border: 1px solid var(--card-border-color);
    border-radius: 12px;
    box-shadow: var(--card-shadow);
    overflow: hidden;
  }}
</style>
</head>
<body>
<div class="frame"><div id="mount"></div></div>
<script>
  // The card reads the wall clock for the "now" marker and the status chip.
  // Pinning it keeps the screenshots reproducible instead of depending on the
  // hour they happen to be generated in.
  Date.now = () => {now_ms};

  // Just enough of Home Assistant's <ha-card> to show the card's title the
  // way a dashboard does. The card's content stays in the light DOM, where
  // the card itself measures and queries it.
  customElements.define("ha-card", class extends HTMLElement {{
    connectedCallback() {{
      if (this.shadowRoot) return;
      const root = this.attachShadow({{ mode: "open" }});
      root.innerHTML = `<style>
        .card-header {{ margin: 0; padding: 12px 16px 12px; font-size: 24px; font-weight: 400;
                        line-height: 40px; letter-spacing: -0.012em;
                        color: var(--ha-card-header-color, var(--primary-text-color)); }}
      </style><h1 class="card-header"></h1><slot></slot>`;
      const header = this.getAttribute("header");
      const h1 = root.querySelector(".card-header");
      if (header) h1.textContent = header;
      else h1.remove();
    }}
  }});
</script>
<script type="module">
import("/card.js").then(() => {{
  const card = document.createElement("smart-battery-pilot-card");
  card.setConfig({{ entity: {plan_entity}, title: {title_json}, view: {view_json} }});
  card.hass = {{
    states: {states_json},
    language: {lang_json},
    locale: {{ language: {lang_json} }},
    config: {{ time_zone: {tz_json} }},
  }};
  document.getElementById("mount").appendChild(card);
}});
</script>
</body>
</html>
""")


# The CSS variables the card reads, as Home Assistant's default light theme
# and a dark theme set them, plus the dashboard around the card.
THEMES = {
    "light": {
        "dashboard-background": "#f2f4f7",
        "card-background-color": "#ffffff",
        "card-border-color": "#e3e6ea",
        "card-shadow": "0 1px 2px rgba(16, 24, 40, 0.06), 0 6px 20px rgba(16, 24, 40, 0.08)",
        "primary-text-color": "#212121",
        "secondary-text-color": "#727272",
        "divider-color": "#e0e0e0",
        "primary-color": "#03a9f4",
        "error-color": "#db4437",
    },
    "dark": {
        "dashboard-background": "#111827",
        "card-background-color": "#1f2937",
        "card-border-color": "#273244",
        "card-shadow": "0 1px 2px rgba(0, 0, 0, 0.3), 0 8px 24px rgba(0, 0, 0, 0.35)",
        "primary-text-color": "#f3f4f6",
        "secondary-text-color": "#9ca3af",
        "divider-color": "#374151",
        "primary-color": "#38bdf8",
        "error-color": "#f87171",
    },
}

CARD_WIDTH = 560  # CSS px; rendered at deviceScaleFactor 2
MARGIN = 28  # dashboard visible around the card, CSS px


def write_html(
    path: Path,
    scenario: Scenario,
    states: dict,
    title: str,
    lang: str,
    tz: str,
    now: datetime,
    view: str,
    theme: str,
    width: int = CARD_WIDTH,
):
    theme_vars = "\n".join(f"    --{key}: {value};" for key, value in THEMES[theme].items())
    path.write_text(
        HTML_TEMPLATE.format(
            title=scenario.name,
            theme_vars=theme_vars,
            margin=MARGIN,
            width=width,
            view_json=json.dumps(view),
            title_json=json.dumps(title),
            plan_entity=json.dumps(PLAN_ENTITY),
            states_json=json.dumps(states, indent=2),
            lang_json=json.dumps(lang),
            tz_json=json.dumps(tz),
            now_ms=int(now.timestamp() * 1000),
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Playwright
# ---------------------------------------------------------------------------

PLAYWRIGHT_SCRIPT = textwrap.dedent("""\
const {{ chromium }} = require({playwright_json});
const http = require('http');
const fs = require('fs');
const path = require('path');

const serveDir = {serve_dir_json};
const jobs = {jobs_json};

const srv = http.createServer((req, res) => {{
  const file = path.join(serveDir, decodeURIComponent(req.url.split('?')[0]));
  try {{
    const data = fs.readFileSync(file);
    res.writeHead(200, {{
      'Content-Type': file.endsWith('.js') ? 'application/javascript' : 'text/html',
    }});
    res.end(data);
  }} catch (e) {{
    res.writeHead(404);
    res.end();
  }}
}});

(async () => {{
  await new Promise(r => srv.listen({port}, '127.0.0.1', r));
  const browser = await chromium.launch();
  const page = await browser.newPage({{ deviceScaleFactor: 2 }});
  for (const job of jobs) {{
    // Wider and taller than any card, so the frame is never cut off.
    await page.setViewportSize({{ width: job.width + 2 * {margin} + 40, height: 2000 }});
    await page.goto(`http://127.0.0.1:{port}/${{job.html}}`);
    await page.waitForSelector('.chartwrap svg', {{ timeout: 10000 }});
    await page.waitForTimeout(400);
    await page.locator('.frame').screenshot({{ path: job.png }});
    console.log('saved ' + job.png);
  }}
  await browser.close();
  srv.close();
}})().catch(e => {{ console.error(e); process.exit(1); }});
""")


def render(jobs: list[dict], serve_dir: Path) -> bool:
    if not Path(PLAYWRIGHT_DIR).exists():
        print(
            f"playwright not found at {PLAYWRIGHT_DIR}\n"
            f"  npm install --prefix {Path(PLAYWRIGHT_DIR).parent.parent} playwright\n"
            f"  (or set PLAYWRIGHT_DIR)",
            file=sys.stderr,
        )
        return False
    link = serve_dir / "card.js"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(CARD_JS)

    script = serve_dir / "_shoot.js"
    script.write_text(
        PLAYWRIGHT_SCRIPT.format(
            playwright_json=json.dumps(PLAYWRIGHT_DIR),
            serve_dir_json=json.dumps(str(serve_dir)),
            jobs_json=json.dumps(jobs, indent=2),
            port=18411,
            margin=MARGIN,
        )
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=180)
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr[:1500], file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

TITLES = {"en": "Smart Battery Pilot", "de": "Smart Battery Pilot"}

# (scenario factory, view, output name) for every screenshot the README shows.
# The scenarios in the tracks view document what the optimizer does; the other
# views are shown on a day that has something to show in them (PV against
# consumption, a plan worth summarising, a car session for the EV tile).
SHOTS = [
    (dunkelflaute, "tracks", "card_dunkelflaute"),
    (winter_arbitrage, "tracks", "card_winter_arbitrage"),
    (summer_export, "tracks", "card_summer_export"),
    (ev_overnight, "tracks", "card_ev_overnight"),
    (ev_overnight, "compact", "card_ev_compact"),
    (summer_export, "balance", "card_view_balance"),
    (summer_export, "compact", "card_view_compact"),
]

# Widths the card is expected to survive: a phone column, the size it was
# designed at, a `column_span: 2` section and a full-width panel view. These
# are a verification aid rather than documentation, so they are written to the
# work directory instead of into assets/.
SIZE_WIDTHS = [300, 380, 520, 900, 1400]
SIZE_SHOTS = [
    (winter_arbitrage, "tracks", "size_tracks"),
    (winter_arbitrage, "compact", "size_compact"),
    (ev_overnight, "compact", "size_ev_compact"),
]


def _states(scenario: Scenario) -> dict:
    """The hass states the card reads: the plan sensor, and live PV power."""
    plan = scenario.build(scenario.start)
    actions = sorted({s.action for s in plan.slots})
    print(
        f"{scenario.name}: {len(plan.slots)} slots, actions={actions}, "
        f"grid charge {plan.grid_charge_kwh:.1f} kWh, "
        f"savings {plan.estimated_savings_eur:.2f} EUR"
    )
    if plan.warnings:
        print(f"  warnings: {plan.warnings}")
    states = {PLAN_ENTITY: plan_state(scenario, plan, scenario.now)}
    if scenario.pv_power_w is not None:
        states[PV_ENTITY] = {
            "entity_id": PV_ENTITY,
            "state": str(scenario.pv_power_w),
            "attributes": {"unit_of_measurement": "W"},
        }
    return states


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--lang",
        default="en",
        choices=["en", "de"],
        help="card language; anything but en is written to the work directory",
    )
    parser.add_argument("--tz", default="Europe/Berlin")
    parser.add_argument("--theme", default="both", choices=["light", "dark", "both"])
    parser.add_argument(
        "--sizes",
        action="store_true",
        help="render the card at several widths into the work directory, for eyeballing",
    )
    args = parser.parse_args()

    work_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "sbp_card_shots"
    work_dir.mkdir(parents=True, exist_ok=True)
    themes = ["light", "dark"] if args.theme == "both" else [args.theme]

    if args.sizes:
        out_dir = work_dir
        targets = [
            (factory, view, f"{name}_{width}", width)
            for factory, view, name in SIZE_SHOTS
            for width in SIZE_WIDTHS
        ]
    else:
        out_dir = OUT_DIR if args.lang == "en" else work_dir
        targets = [(factory, view, name, CARD_WIDTH) for factory, view, name in SHOTS]
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs = []
    states_by_scenario: dict = {}
    for factory, view, name, width in targets:
        if factory not in states_by_scenario:
            scenario = factory()
            states_by_scenario[factory] = (scenario, _states(scenario))
        scenario, states = states_by_scenario[factory]
        for theme in themes:
            stem = f"{name}_{theme}" if args.lang == "en" else f"{name}_{args.lang}_{theme}"
            html = work_dir / f"{stem}.html"
            write_html(
                html,
                scenario,
                states,
                TITLES[args.lang],
                args.lang,
                args.tz,
                scenario.now,
                view,
                theme,
                width,
            )
            jobs.append({"html": html.name, "png": str(out_dir / f"{stem}.png"), "width": width})

    if out_dir != OUT_DIR:
        print(f"writing to {out_dir}")
    return 0 if render(jobs, work_dir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
