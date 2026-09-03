#!/usr/bin/env python3
"""Generate card screenshots with synthetic plan data for documentation.

Usage: python3 tools/gen_card_screenshots.py
Output: assets/screenshots/card_*.png
"""

from datetime import datetime, timedelta
import json
import os
import subprocess
import sys
import textwrap

# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

def make_slot(start: datetime, hours: float, action: str, price: float,
              net_demand: float = 0.3, pv_kwh: float = 0.0,
              charge_power_w: float = 0.0, discharge_kwh: float = 0.0,
              soc: float = 50.0) -> dict:
    end = start + timedelta(hours=hours)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "action": action,
        "price": round(price, 4),
        "net_demand_kwh": round(net_demand, 3),
        "pv_kwh": round(pv_kwh, 3),
        "charge_power_w": charge_power_w,
        "discharge_kwh": round(discharge_kwh, 3),
        "soc_forecast": round(soc, 1),
    }


def scenario_winter_arbitrage(now: datetime) -> tuple[str, list]:
    """Winter night: cheap valley → charge; morning/evening peak → idle/auto.
    All actions present: charge, idle, auto.
    """
    # Start at midnight yesterday so "now" is in the middle of the plan
    t = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=2)
    slots = []

    # Night valley 00-06: charge (cheap)
    prices_night = [0.08, 0.07, 0.06, 0.06, 0.07, 0.08]
    soc = 20.0
    for i, p in enumerate(prices_night):
        action = "charge" if p <= 0.07 else "auto"
        charge_w = 4000.0 if action == "charge" else 0.0
        delta_soc = (charge_w * 1.0 / 10000) * 100 * 0.9 if action == "charge" else -3.0
        soc = min(95, soc + delta_soc)
        slots.append(make_slot(t + timedelta(hours=i), 1, action, p,
                               net_demand=0.4, pv_kwh=0.0,
                               charge_power_w=charge_w, soc=soc))

    # Morning peak 06-09: idle (preserve charge for evening)
    prices_morning = [0.28, 0.32, 0.30]
    for i, p in enumerate(prices_morning):
        soc -= 0.5
        slots.append(make_slot(t + timedelta(hours=6+i), 1, "idle", p,
                               net_demand=0.8, soc=soc))

    # Day 09-16: auto (normal, no PV in winter)
    prices_day = [0.22, 0.20, 0.18, 0.17, 0.18, 0.20, 0.22]
    for i, p in enumerate(prices_day):
        soc -= 2.0
        slots.append(make_slot(t + timedelta(hours=9+i), 1, "auto", p,
                               net_demand=0.5, soc=max(10, soc)))

    # Evening peak 16-20: idle (high prices, hold remaining charge)
    prices_eve = [0.31, 0.35, 0.33, 0.29]
    for i, p in enumerate(prices_eve):
        soc -= 1.0
        slots.append(make_slot(t + timedelta(hours=16+i), 1, "idle", p,
                               net_demand=1.0, soc=max(10, soc)))

    # Night 20-24: auto
    prices_late = [0.18, 0.14, 0.11, 0.09]
    for i, p in enumerate(prices_late):
        soc -= 1.5
        slots.append(make_slot(t + timedelta(hours=20+i), 1, "auto", p,
                               net_demand=0.3, soc=max(10, soc)))

    # Next day 00-06: charge again
    for i, p in enumerate(prices_night):
        action = "charge" if p <= 0.07 else "auto"
        charge_w = 4000.0 if action == "charge" else 0.0
        delta_soc = 8.0 if action == "charge" else -1.0
        soc = min(95, soc + delta_soc)
        slots.append(make_slot(t + timedelta(hours=24+i), 1, action, p,
                               net_demand=0.4, charge_power_w=charge_w, soc=soc))

    return "Winter-Arbitrage: Nacht-Laden + Sperren bei Morgen-/Abend-Peak", slots


def scenario_summer_pv_export(now: datetime) -> tuple[str, list]:
    """Summer day: PV surplus + export at peak, charge at night valley.
    All 4 actions: charge, idle, auto, export.
    """
    t = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=4)
    slots = []
    soc = 15.0

    # Night valley 00-05: charge (0.07-0.09)
    night_prices = [0.07, 0.065, 0.06, 0.065, 0.08]
    for i, p in enumerate(night_prices):
        action = "charge" if p <= 0.07 else "auto"
        charge_w = 3500.0 if action == "charge" else 0.0
        soc = min(95, soc + (10.0 if action == "charge" else -1.0))
        slots.append(make_slot(t + timedelta(hours=i), 1, action, p,
                               net_demand=0.3, charge_power_w=charge_w, soc=soc))

    # Morning 05-09: auto, PV starts
    pv_ramp = [0.0, 0.3, 0.8, 1.5]
    prices_morn = [0.18, 0.22, 0.26, 0.24]
    for i, (p, pv) in enumerate(zip(prices_morn, pv_ramp)):
        net = max(-pv + 0.6, 0)
        soc = min(95, soc + (pv * 0.5))
        slots.append(make_slot(t + timedelta(hours=5+i), 1, "auto", p,
                               net_demand=net, pv_kwh=pv, soc=soc))

    # Midday 09-15: export at high prices, PV > consumption
    pv_peak = [2.2, 2.8, 3.1, 3.0, 2.7, 2.1]
    prices_mid = [0.29, 0.33, 0.36, 0.34, 0.31, 0.28]
    for i, (p, pv) in enumerate(zip(prices_mid, pv_peak)):
        net = max(-pv + 0.5, -2.0)
        action = "export" if p >= 0.30 else "auto"
        discharge = abs(min(net, 0)) if action == "export" else 0.0
        soc = max(10, soc - (discharge * 10))
        slots.append(make_slot(t + timedelta(hours=9+i), 1, action, p,
                               net_demand=net, pv_kwh=pv,
                               discharge_kwh=discharge, soc=soc))

    # Afternoon 15-18: auto, PV declining
    pv_down = [1.4, 0.7, 0.2]
    prices_aft = [0.25, 0.27, 0.30]
    for i, (p, pv) in enumerate(zip(prices_aft, pv_down)):
        net = max(-pv + 0.6, 0)
        soc -= 2.0
        slots.append(make_slot(t + timedelta(hours=15+i), 1, "auto", p,
                               net_demand=net, pv_kwh=pv, soc=max(10, soc)))

    # Evening peak 18-21: idle (hold for evening consumption)
    prices_eve = [0.32, 0.35, 0.30]
    for i, p in enumerate(prices_eve):
        soc -= 3.0
        slots.append(make_slot(t + timedelta(hours=18+i), 1, "idle", p,
                               net_demand=1.1, soc=max(10, soc)))

    # Night 21-00: auto / next charge
    for i, p in enumerate([0.18, 0.13, 0.09]):
        soc -= 1.5
        slots.append(make_slot(t + timedelta(hours=21+i), 1, "auto", p,
                               net_demand=0.4, soc=max(10, soc)))

    # Next night valley
    for i, p in enumerate([0.07, 0.065, 0.06]):
        soc = min(95, soc + 12.0)
        slots.append(make_slot(t + timedelta(hours=24+i), 1, "charge", p,
                               net_demand=0.3, charge_power_w=3500, soc=soc))

    return "Sommer: PV-Einspeisung bei Spitzenpreisen + Nacht-Laden", slots


def scenario_dunkelflaute(now: datetime) -> tuple[str, list]:
    """Dunkelflaute: no PV, very high prices, aggressive charge at valley.
    Shows charge + idle + auto clearly.
    """
    t = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=1)
    slots = []
    soc = 10.0

    price_profile = [
        # hour, price, action_hint
        (0, 0.09, "charge"), (1, 0.08, "charge"), (2, 0.075, "charge"),
        (3, 0.08, "charge"), (4, 0.09, "auto"), (5, 0.12, "auto"),
        (6, 0.22, "idle"), (7, 0.28, "idle"), (8, 0.31, "idle"),
        (9, 0.26, "idle"), (10, 0.22, "auto"), (11, 0.20, "auto"),
        (12, 0.19, "auto"), (13, 0.18, "auto"), (14, 0.19, "auto"),
        (15, 0.21, "auto"), (16, 0.28, "idle"), (17, 0.34, "idle"),
        (18, 0.36, "idle"), (19, 0.32, "idle"), (20, 0.24, "auto"),
        (21, 0.18, "auto"), (22, 0.12, "auto"), (23, 0.09, "charge"),
        (24, 0.08, "charge"), (25, 0.075, "charge"), (26, 0.08, "auto"),
    ]

    for hour, price, hint in price_profile:
        if hint == "charge":
            charge_w = 5000.0
            soc = min(95, soc + 14.0)
            slots.append(make_slot(t + timedelta(hours=hour), 1, "charge", price,
                                   net_demand=0.5, charge_power_w=charge_w, soc=soc))
        elif hint == "idle":
            soc = max(10, soc - 0.5)
            slots.append(make_slot(t + timedelta(hours=hour), 1, "idle", price,
                                   net_demand=0.9, soc=soc))
        else:
            soc = max(10, soc - 2.5)
            slots.append(make_slot(t + timedelta(hours=hour), 1, "auto", price,
                                   net_demand=0.5, soc=soc))

    return "Dunkelflaute: Aggressives Nacht-Laden, Entladesperre bei Peak", slots


# ---------------------------------------------------------------------------
# HTML harness
# ---------------------------------------------------------------------------

CARD_JS_PATH = os.path.abspath(
    "custom_components/smart_battery_pilot/frontend/smart-battery-pilot-card.js"
)

HTML_TEMPLATE = textwrap.dedent("""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #111827; display: flex; flex-direction: column;
          align-items: center; padding: 20px; font-family: Roboto, sans-serif; }}
  /* ha-card stub with correct CSS variables */
  ha-card {{
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
  smart-battery-pilot-card {{
    display: block;
    --card-background-color: #1f2937;
    --primary-text-color: #f3f4f6;
    --secondary-text-color: #9ca3af;
    --divider-color: #374151;
    --error-color: #f87171;
  }}
  .wrap {{ width: 520px; }}
  .caption {{ color: #6b7280; font-size: 11px; text-align: center;
               padding: 6px 0 0; }}
</style>
</head>
<body>
<div class="wrap">
  <div id="mount"></div>
  <div class="caption">{title}</div>
</div>
<script type="module">
import("/card.js").then(() => {{
  const card = document.createElement("smart-battery-pilot-card");
  // setConfig BEFORE hass to avoid _config undefined error
  card.setConfig({{ entity: "sensor.smart_battery_pilot_ladeplan", title: "{title_short}" }});

  const planState = {{
    entity_id: "sensor.smart_battery_pilot_ladeplan",
    state: "{active_slots}",
    attributes: {{
      slots: {slots_json},
      total_slots: {total_slots},
      grid_charge_kwh: {grid_charge_kwh},
      battery_discharge_kwh: {battery_discharge_kwh},
      price_adapter: "Nordpool",
      updated_at: new Date("{now_iso}").toISOString(),
      error: null,
    }},
  }};
  card.hass = {{
    states: {{ "sensor.smart_battery_pilot_ladeplan": planState }},
    language: "de",
  }};
  document.getElementById("mount").appendChild(card);
}});
</script>
</body>
</html>
""")


def make_html(title: str, slots: list, now: datetime, output_path: str) -> str:
    active = sum(1 for s in slots if s["action"] != "auto")
    grid_charge = round(sum(s["charge_power_w"] / 1000 for s in slots if s["action"] == "charge"), 2)
    discharge_kwh = round(sum(s["discharge_kwh"] for s in slots), 2)
    title_short = title.split(":")[0]
    html = HTML_TEMPLATE.format(
        title=title,
        title_short=title_short,
        now_iso=now.isoformat(),
        active_slots=active,
        slots_json=json.dumps(slots, indent=4),
        total_slots=len(slots),
        grid_charge_kwh=grid_charge,
        battery_discharge_kwh=discharge_kwh,
    )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)
    return output_path


# ---------------------------------------------------------------------------
# Playwright screenshot
# ---------------------------------------------------------------------------

PLAYWRIGHT_SCRIPT = textwrap.dedent("""\
const {{ chromium }} = require('/tmp/node_modules/playwright');
const http = require('http');
const fs = require('fs');
const path = require('path');
const url = require('url');

function serveDir(dir, port) {{
  return new Promise(resolve => {{
    const srv = http.createServer((req, res) => {{
      const filePath = path.join(dir, url.parse(req.url).pathname);
      try {{
        const data = fs.readFileSync(filePath);
        const ext = path.extname(filePath);
        const ct = ext === '.js' ? 'application/javascript' :
                   ext === '.html' ? 'text/html' : 'text/plain';
        res.writeHead(200, {{ 'Content-Type': ct }});
        res.end(data);
      }} catch(e) {{ res.writeHead(404); res.end(); }}
    }});
    srv.listen(port, '127.0.0.1', () => resolve(srv));
  }});
}}

(async () => {{
  const srv = await serveDir('{serve_dir}', {port});
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.setViewportSize({{ width: 580, height: 440 }});
  await page.goto('http://127.0.0.1:{port}/{html_name}');
  await page.waitForTimeout(1500);
  const el = await page.$('body > div');
  const clip = await el.boundingBox();
  await page.screenshot({{ path: '{png_path}', clip: clip }});
  await browser.close();
  srv.close();
  console.log('saved', '{png_path}');
}})();
""")


_port_counter = [18400]

def screenshot(html_path: str, png_path: str, hass_json: str) -> bool:
    serve_dir = os.path.dirname(os.path.abspath(html_path))
    html_name = os.path.basename(html_path)
    # symlink card.js into serve_dir
    card_link = os.path.join(serve_dir, "card.js")
    if not os.path.exists(card_link):
        os.symlink(CARD_JS_PATH, card_link)
    port = _port_counter[0]
    _port_counter[0] += 1
    script = PLAYWRIGHT_SCRIPT.format(
        serve_dir=serve_dir,
        html_name=html_name,
        port=port,
        png_path=os.path.abspath(png_path),
        card_js=CARD_JS_PATH,
        hass_json=hass_json,
    )
    script_path = html_path.replace(".html", "_pw.js")
    with open(script_path, "w") as f:
        f.write(script)
    result = subprocess.run(
        ["node", script_path], capture_output=True, text=True, timeout=30
    )
    os.unlink(script_path)
    if result.returncode != 0:
        print(f"  Playwright error: {result.stderr[:400]}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Use a fixed "now" so screenshots are reproducible (10:30 local)
    now = datetime.now().replace(hour=10, minute=30, second=0, microsecond=0)
    now = now.astimezone()

    scenarios = [
        ("card_winter_arbitrage", scenario_winter_arbitrage(now)),
        ("card_summer_export",    scenario_summer_pv_export(now)),
        ("card_dunkelflaute",     scenario_dunkelflaute(now)),
    ]

    out_dir = "assets/screenshots"
    html_dir = "/tmp/sbp_card_test"
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(html_dir, exist_ok=True)

    for name, (title, slots) in scenarios:
        html_path = os.path.join(html_dir, f"{name}.html")
        png_path = os.path.join(out_dir, f"{name}.png")
        make_html(title, slots, now, html_path)

        active_slots = sum(1 for s in slots if s["action"] != "auto")
        grid_charge = round(sum(s["charge_power_w"] / 1000 for s in slots if s["action"] == "charge"), 2)
        discharge = round(sum(s["discharge_kwh"] for s in slots), 2)
        plan_state = {
            "entity_id": "sensor.smart_battery_pilot_ladeplan",
            "state": str(active_slots),
            "attributes": {
                "slots": slots,
                "total_slots": len(slots),
                "grid_charge_kwh": round(grid_charge, 2),
                "battery_discharge_kwh": round(discharge, 2),
                "price_adapter": "Nordpool",
                "updated_at": now.isoformat(),
                "error": None,
            },
        }
        hass_json = json.dumps({
            "states": {"sensor.smart_battery_pilot_ladeplan": plan_state},
            "language": "de",
        })

        print(f"  Screenshot: {name} … ", end="", flush=True)
        if screenshot(html_path, png_path, hass_json):
            print(f"OK → {png_path}")
        else:
            print("FAILED")

    print("\nDone. PNGs in assets/screenshots/")


if __name__ == "__main__":
    main()
