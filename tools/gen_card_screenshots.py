#!/usr/bin/env python3
"""Render the Lovelace card for the README, using the real optimizer.

The scenarios below describe an *input* day - prices, household consumption,
PV production - and the plan is then computed by `build_plan()`, exactly as
the coordinator does it. Hand-written slot lists drift away from what the
integration actually plans; these cannot.

Usage:
    npm install --prefix /tmp playwright   # browsers are found in the cache
    python3 tools/gen_card_screenshots.py [--lang en|de]

Output: assets/screenshots/card_*.png
"""

from __future__ import annotations

import argparse
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


class Scenario:
    """One day of inputs plus the battery and optimizer settings to plan it."""

    def __init__(
        self,
        name: str,
        caption: str,
        prices: list[float],
        consumption: list[float],
        pv: list[float],
        battery: BatteryState,
        config: OptimizerConfig,
        now: datetime,
        pv_power_w: float | None = None,
    ) -> None:
        self.name = name
        self.caption = caption
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

    def build(self, start: datetime):
        """Run the real optimizer over the horizon starting at `start`.

        The profiles are indexed by hour of day, so a plan that begins at noon
        gets the noon price, not the midnight one.
        """
        slots = []
        for i in range(HORIZON):
            slot_start = start + timedelta(hours=i)
            h = slot_start.hour
            slots.append(
                InputSlot(
                    price_slot=PriceSlot(
                        start=slot_start,
                        end=slot_start + timedelta(hours=1),
                        price=self.prices[h],
                    ),
                    net_demand_kwh=round(self.consumption[h] - self.pv[h], 3),
                    pv_kwh=self.pv[h],
                )
            )
        return build_plan(slots, self.battery, self.config)


HORIZON = 36  # what a user sees once tomorrow's prices are published


def _local(*args: int) -> datetime:
    """A fixed local instant, so the rendered clock does not drift."""
    return datetime(*args).astimezone()


def winter_arbitrage() -> Scenario:
    """Cold winter day: no PV worth the name, cheap night, two demand peaks."""
    prices = [
        0.09, 0.08, 0.07, 0.07, 0.08, 0.12, 0.24, 0.31, 0.29, 0.24, 0.21, 0.19,
        0.18, 0.17, 0.18, 0.21, 0.27, 0.34, 0.38, 0.35, 0.30, 0.22, 0.15, 0.11,
    ]
    consumption = [
        0.5, 0.45, 0.45, 0.45, 0.5, 0.7, 1.3, 1.5, 1.1, 0.8, 0.7, 0.7,
        0.8, 0.7, 0.7, 0.8, 1.0, 1.4, 1.6, 1.5, 1.2, 0.9, 0.7, 0.5,
    ]
    return Scenario(
        name="card_winter_arbitrage",
        caption="Winter: charge through the cheap night, hold the battery for the evening peak",
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
        0.16, 0.14, 0.13, 0.13, 0.14, 0.17, 0.21, 0.24, 0.20, 0.13, 0.07, 0.03,
        0.01, 0.01, 0.03, 0.08, 0.15, 0.23, 0.34, 0.44, 0.41, 0.33, 0.25, 0.19,
    ]
    consumption = [
        0.4, 0.35, 0.35, 0.35, 0.4, 0.5, 0.8, 0.9, 0.8, 0.7, 0.7, 0.7,
        0.8, 0.7, 0.7, 0.7, 0.8, 1.0, 1.3, 1.5, 1.3, 1.0, 0.8, 0.5,
    ]
    # A 6 kWp array on a hazy day: enough surplus to fill part of the battery,
    # not enough to saturate it - so the near-free midday hours are still worth
    # buying from the grid.
    pv = [
        0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.3, 0.8, 1.4, 1.9, 2.3, 2.5,
        2.6, 2.4, 2.0, 1.5, 0.9, 0.4, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0,
    ]
    return Scenario(
        name="card_summer_export",
        caption="Summer: PV fills the battery, the evening peak is sold to the grid",
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
        0.14, 0.11, 0.09, 0.09, 0.12, 0.22, 0.41, 0.52, 0.47, 0.38, 0.33, 0.30,
        0.29, 0.28, 0.30, 0.35, 0.46, 0.61, 0.68, 0.59, 0.48, 0.33, 0.22, 0.16,
    ]
    consumption = [
        0.6, 0.55, 0.55, 0.55, 0.6, 0.9, 1.6, 1.8, 1.3, 1.0, 0.9, 0.9,
        1.0, 0.9, 0.9, 1.0, 1.3, 1.7, 2.0, 1.8, 1.4, 1.1, 0.8, 0.6,
    ]
    return Scenario(
        name="card_dunkelflaute",
        caption="Dunkelflaute: an eightfold spread makes grid charging pay for itself",
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


SCENARIOS = [winter_arbitrage, summer_export, dunkelflaute]


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
            "charge_power_w": s.charge_power_w,
            "discharge_kwh": s.discharge_kwh,
            "soc_forecast": s.soc_forecast,
        }
        for s in plan.slots
    ]
    return {
        "entity_id": PLAN_ENTITY,
        "state": str(sum(1 for s in plan.slots if s.action != "auto")),
        "attributes": {
            "slots": slots,
            "total_slots": len(slots),
            "grid_charge_kwh": round(plan.grid_charge_kwh, 2),
            "battery_discharge_kwh": round(plan.battery_discharge_kwh, 2),
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
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #111827; display: flex; flex-direction: column;
          align-items: center; padding: 20px; font-family: Roboto, Arial, sans-serif; }}
  ha-card, smart-battery-pilot-card {{
    display: block;
    background: #1f2937;
    border-radius: 12px;
    overflow: hidden;
    width: 520px;
    --card-background-color: #1f2937;
    --primary-text-color: #f3f4f6;
    --secondary-text-color: #9ca3af;
    --divider-color: #374151;
    --error-color: #f87171;
  }}
  .wrap {{ width: 520px; }}
  .caption {{ color: #6b7280; font-size: 11px; text-align: center; padding: 6px 0 0; }}
</style>
</head>
<body>
<div class="wrap">
  <div id="mount"></div>
  <div class="caption">{caption}</div>
</div>
<script>
  // The card reads the wall clock for the "now" marker and the status chip.
  // Pinning it keeps the screenshots reproducible instead of depending on the
  // hour they happen to be generated in.
  Date.now = () => {now_ms};
</script>
<script type="module">
import("/card.js").then(() => {{
  const card = document.createElement("smart-battery-pilot-card");
  card.setConfig({{ entity: {plan_entity}, title: {title_json} }});
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


def write_html(
    path: Path, scenario: Scenario, states: dict, title: str, lang: str, tz: str, now: datetime
):
    path.write_text(
        HTML_TEMPLATE.format(
            title=scenario.name,
            caption=scenario.caption,
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
  await page.setViewportSize({{ width: 600, height: 460 }});
  for (const job of jobs) {{
    await page.goto(`http://127.0.0.1:{port}/${{job.html}}`);
    await page.waitForSelector('.chartwrap svg', {{ timeout: 10000 }});
    await page.waitForTimeout(400);
    const box = await (await page.$('body > .wrap')).boundingBox();
    await page.screenshot({{ path: job.png, clip: box }});
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", default="en", choices=["en", "de"])
    parser.add_argument("--tz", default="Europe/Berlin")
    args = parser.parse_args()

    work_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "sbp_card_shots"
    work_dir.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    jobs = []
    for factory in SCENARIOS:
        scenario = factory()
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

        html = work_dir / f"{scenario.name}.html"
        write_html(
            html, scenario, states, TITLES[args.lang], args.lang, args.tz, scenario.now
        )
        jobs.append(
            {"html": html.name, "png": str(OUT_DIR / f"{scenario.name}.png")}
        )

    return 0 if render(jobs, work_dir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
