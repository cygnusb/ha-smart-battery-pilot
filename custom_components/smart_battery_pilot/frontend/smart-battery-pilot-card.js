/* Smart Battery Pilot Lovelace card.
 *
 * Price curve with action bands, PV forecast, SOC projection, grid with
 * nice price ticks and a hover tooltip (price / SOC / action / PV).
 *
 * type: custom:smart-battery-pilot-card
 * entity: sensor.<...>_charge_plan   (auto-discovered if omitted/wrong)
 */

const ACTION_COLORS = {
  charge: "rgba(67, 160, 71, 0.40)",
  idle: "rgba(120, 130, 140, 0.28)",
  export: "rgba(255, 152, 0, 0.40)",
  auto: "rgba(3, 169, 244, 0.07)",
};

const ACTION_LABELS = {
  charge: "Laden",
  idle: "Gesperrt",
  export: "Einspeisen",
  auto: "Auto",
};

const W = 480;
const H = 230;
const PAD_L = 46;
const PAD_R = 38;
const PAD_T = 14;
const PAD_B = 30;
const PLOT_W = W - PAD_L - PAD_R;
const PLOT_H = H - PAD_T - PAD_B;

function niceTickStep(range, maxTicks) {
  const raw = range / maxTicks;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 2.5, 5, 10]) {
    if (raw <= m * mag) return m * mag;
  }
  return 10 * mag;
}

class SmartBatteryPilotCard extends HTMLElement {
  setConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("smart-battery-pilot-card: invalid config");
    }
    this._config = config;
    this._renderedState = null;
  }

  set hass(hass) {
    this._hass = hass;
    const state = this._resolveState();
    if (state === this._renderedState) return; // plan unchanged - keep DOM (and tooltip)
    this._renderedState = state;
    this._render(state);
  }

  getCardSize() {
    return 5;
  }

  static _findPlanEntity(hass) {
    return Object.keys(hass.states).find(
      (id) =>
        id.startsWith("sensor.") &&
        hass.states[id].attributes &&
        hass.states[id].attributes.price_adapter !== undefined &&
        Array.isArray(hass.states[id].attributes.slots)
    );
  }

  static getStubConfig(hass) {
    const entity =
      (hass && SmartBatteryPilotCard._findPlanEntity(hass)) ||
      "sensor.smart_battery_pilot_charge_plan";
    return { entity };
  }

  _resolveState() {
    if (!this._hass || !this._config) return null;
    let state = this._config.entity ? this._hass.states[this._config.entity] : null;
    if (!state) {
      const found = SmartBatteryPilotCard._findPlanEntity(this._hass);
      if (found) state = this._hass.states[found];
    }
    return state || null;
  }

  _render(state) {
    const title = this._config.title || "Smart Battery Pilot";

    if (!state) {
      this._html(
        title,
        `<div class="empty">Entity <code>${
          this._config.entity || "?"
        }</code> nicht gefunden und keine Ladeplan-Entity erkannt.</div>`
      );
      return;
    }
    if (!state.attributes.slots || state.attributes.slots.length === 0) {
      this._html(
        title,
        `<div class="empty">Kein gültiger Ladeplan (Fehler: ${
          state.attributes.error || "unbekannt"
        })</div>`
      );
      return;
    }

    const slots = state.attributes.slots.map((s) => ({
      ...s,
      startMs: Date.parse(s.start),
      endMs: Date.parse(s.end),
    }));
    this._slots = slots;

    const t0 = slots[0].startMs;
    const t1 = slots[slots.length - 1].endMs;
    this._t0 = t0;
    this._t1 = t1;
    const x = (ms) => PAD_L + ((ms - t0) / (t1 - t0)) * PLOT_W;
    this._x = x;

    // --- price scale with nice ticks ---
    const prices = slots.map((s) => s.price);
    const step = niceTickStep(Math.max(...prices) - Math.min(0, ...prices) || 0.1, 5);
    let pMin = Math.floor(Math.min(0, ...prices) / step) * step;
    let pMax = Math.ceil((Math.max(...prices) + step * 0.15) / step) * step;
    const yPrice = (p) => PAD_T + (1 - (p - pMin) / (pMax - pMin)) * PLOT_H;
    const ySoc = (soc) => PAD_T + (1 - soc / 100) * PLOT_H;
    this._yPrice = yPrice;

    // --- grid: horizontal price ticks ---
    let grid = "";
    for (let p = pMin; p <= pMax + 1e-9; p += step) {
      const y = yPrice(p).toFixed(1);
      grid += `<line x1="${PAD_L}" y1="${y}" x2="${PAD_L + PLOT_W}" y2="${y}" class="grid${
        Math.abs(p) < 1e-9 ? " zero" : ""
      }"/>`;
      grid += `<text x="${PAD_L - 5}" y="${+y + 3}" class="ax pr">${p.toFixed(2)}</text>`;
    }
    // vertical: every 3h anchored to LOCAL midnight, stronger + date label at midnight
    const anchor = new Date(t0);
    anchor.setHours(0, 0, 0, 0);
    let firstTick = anchor.getTime();
    while (firstTick < t0) firstTick += 10800000;
    for (let ms = firstTick; ms <= t1; ms += 10800000) {
      const d = new Date(ms);
      const midnight = d.getHours() === 0;
      grid += `<line x1="${x(ms).toFixed(1)}" y1="${PAD_T}" x2="${x(ms).toFixed(1)}" y2="${
        PAD_T + PLOT_H
      }" class="grid${midnight ? " day" : ""}"/>`;
      grid += `<text x="${x(ms).toFixed(1)}" y="${H - 16}" class="ax tx">${String(
        d.getHours()
      ).padStart(2, "0")}</text>`;
      if (midnight) {
        grid += `<text x="${x(ms).toFixed(1)}" y="${H - 4}" class="ax tx day">${d.toLocaleDateString(
          [],
          { weekday: "short", day: "numeric", month: "numeric" }
        )}</text>`;
      }
    }
    grid += `<text x="${W - 4}" y="${ySoc(100) + 4}" class="ax soc" text-anchor="end">100%</text>`;
    grid += `<text x="${W - 4}" y="${ySoc(50) + 4}" class="ax soc" text-anchor="end">50%</text>`;
    grid += `<text x="${W - 4}" y="${ySoc(0) + 4}" class="ax soc" text-anchor="end">0%</text>`;

    // --- action bands ---
    let bands = "";
    for (const s of slots) {
      const color = ACTION_COLORS[s.action] || "rgba(3, 169, 244, 0.07)";
      bands += `<rect x="${x(s.startMs).toFixed(1)}" y="${PAD_T}" width="${(
        x(s.endMs) - x(s.startMs)
      ).toFixed(1)}" height="${PLOT_H}" fill="${color}"/>`;
    }

    // --- PV forecast area + consumption forecast line ---
    // Shared kWh scale (lower 45% of the plot) so both are comparable.
    const consumption = (s) => Math.max(0, (s.net_demand_kwh || 0) + (s.pv_kwh || 0));
    const pvMax = Math.max(...slots.map((s) => s.pv_kwh || 0));
    const kwhMax = Math.max(pvMax, ...slots.map(consumption));
    let pvArea = "";
    let consPath = "";
    if (kwhMax > 0) {
      const yKwh = (kwh) => PAD_T + PLOT_H - (kwh / kwhMax) * PLOT_H * 0.45;
      if (pvMax > 0) {
        let d = `M${x(slots[0].startMs).toFixed(1)},${(PAD_T + PLOT_H).toFixed(1)} `;
        for (const s of slots) {
          const y = yKwh(s.pv_kwh || 0).toFixed(1);
          d += `L${x(s.startMs).toFixed(1)},${y} L${x(s.endMs).toFixed(1)},${y} `;
        }
        d += `L${x(slots[slots.length - 1].endMs).toFixed(1)},${(PAD_T + PLOT_H).toFixed(1)} Z`;
        pvArea = `<path d="${d}" class="pvarea"/>`;
      }
      for (const s of slots) {
        const y = yKwh(consumption(s)).toFixed(1);
        consPath += `${consPath ? "L" : "M"}${x(s.startMs).toFixed(1)},${y} L${x(
          s.endMs
        ).toFixed(1)},${y} `;
      }
    }

    // --- price step line + SOC line ---
    let pricePath = "";
    for (const s of slots) {
      const y = yPrice(s.price).toFixed(1);
      pricePath += `${pricePath ? "L" : "M"}${x(s.startMs).toFixed(1)},${y} L${x(
        s.endMs
      ).toFixed(1)},${y} `;
    }
    let socPath = `M${x(slots[0].startMs).toFixed(1)},${ySoc(slots[0].soc_forecast).toFixed(1)} `;
    for (const s of slots) {
      socPath += `L${x(s.endMs).toFixed(1)},${ySoc(s.soc_forecast).toFixed(1)} `;
    }

    // --- now marker + status line ---
    const now = Date.now();
    let nowLine = "";
    if (now >= t0 && now <= t1) {
      nowLine = `<line x1="${x(now).toFixed(1)}" y1="${PAD_T}" x2="${x(now).toFixed(1)}" y2="${
        PAD_T + PLOT_H
      }" class="now"/>`;
    }
    const current = slots.find((s) => now >= s.startMs && now < s.endMs);
    const next = slots.find((s) => s.startMs > now && current && s.action !== current.action);
    const fmtTime = (ms) =>
      new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const statusBits = [];
    if (current) {
      const cc = ACTION_COLORS[current.action] || "rgba(3,169,244,0.25)";
      statusBits.push(
        `<span class="chip" style="background:${cc}">${
          ACTION_LABELS[current.action] || current.action
        }</span>`
      );
    }
    if (next) {
      statusBits.push(
        `<span class="next">→ ${ACTION_LABELS[next.action] || next.action} um ${fmtTime(next.startMs)}</span>`
      );
    }

    const legend =
      Object.entries(ACTION_LABELS)
        .filter(([k]) => k !== "auto")
        .map(
          ([key, label]) =>
            `<span class="lg"><i style="background:${ACTION_COLORS[key]}"></i>${label}</span>`
        )
        .join("") +
      `<span class="lg"><i class="li price-i"></i>Preis</span>` +
      `<span class="lg"><i class="li soc-i"></i>SOC</span>` +
      `<span class="lg"><i class="li cons-i"></i>Verbrauch</span>` +
      (pvMax > 0 ? `<span class="lg"><i class="pv-i"></i>PV</span>` : "");

    this._html(
      title,
      `
      <div class="status">${statusBits.join(" ")}</div>
      <div class="chartwrap">
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
          ${bands}
          ${pvArea}
          ${grid}
          ${consPath ? `<path d="${consPath}" class="consline"/>` : ""}
          <path d="${pricePath}" class="price"/>
          <path d="${socPath}" class="socline"/>
          ${nowLine}
          <line id="sbp-cursor" x1="0" y1="${PAD_T}" x2="0" y2="${PAD_T + PLOT_H}" class="cursor" style="display:none"/>
          <circle id="sbp-dot" r="3.5" class="dot" style="display:none"/>
          <rect id="sbp-hit" x="${PAD_L}" y="${PAD_T}" width="${PLOT_W}" height="${PLOT_H}" fill="transparent"/>
        </svg>
        <div id="sbp-tip" class="tip" style="display:none"></div>
      </div>
      <div class="legend">${legend}</div>`
    );
    this._attachHover();
  }

  _attachHover() {
    const svg = this.querySelector("svg");
    const tip = this.querySelector("#sbp-tip");
    const cursor = this.querySelector("#sbp-cursor");
    const dot = this.querySelector("#sbp-dot");
    const wrap = this.querySelector(".chartwrap");
    if (!svg || !tip) return;

    const onMove = (ev) => {
      const rect = svg.getBoundingClientRect();
      const xSvg = ((ev.clientX - rect.left) / rect.width) * W;
      if (xSvg < PAD_L || xSvg > PAD_L + PLOT_W) {
        onLeave();
        return;
      }
      const ms = this._t0 + ((xSvg - PAD_L) / PLOT_W) * (this._t1 - this._t0);
      const slot = this._slots.find((s) => ms >= s.startMs && ms < s.endMs);
      if (!slot) {
        onLeave();
        return;
      }
      const fmt = (msv) =>
        new Date(msv).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      const rows = [
        `<b>${fmt(slot.startMs)}–${fmt(slot.endMs)}</b> · ${ACTION_LABELS[slot.action] || slot.action}`,
        `Preis: <b>${slot.price.toFixed(4)} €/kWh</b>`,
        `SOC-Prognose: <b>${slot.soc_forecast}%</b>`,
      ];
      const cons = Math.max(0, (slot.net_demand_kwh || 0) + (slot.pv_kwh || 0));
      rows.push(`Verbrauch: ${cons.toFixed(2)} kWh`);
      if (slot.pv_kwh) rows.push(`PV: ${slot.pv_kwh.toFixed(2)} kWh`);
      if (slot.net_demand_kwh !== undefined)
        rows.push(`Netto-Bedarf: ${slot.net_demand_kwh.toFixed(2)} kWh`);
      if (slot.charge_power_w) rows.push(`Ladeleistung: ${Math.round(slot.charge_power_w)} W`);
      tip.innerHTML = rows.join("<br>");
      tip.style.display = "block";

      const slotMidX = this._x((slot.startMs + slot.endMs) / 2);
      cursor.setAttribute("x1", slotMidX);
      cursor.setAttribute("x2", slotMidX);
      cursor.style.display = "";
      dot.setAttribute("cx", slotMidX);
      dot.setAttribute("cy", this._yPrice(slot.price));
      dot.style.display = "";

      // position tooltip near pointer, keep inside the card
      const wrapRect = wrap.getBoundingClientRect();
      let left = ev.clientX - wrapRect.left + 14;
      if (left + tip.offsetWidth > wrapRect.width - 4) {
        left = ev.clientX - wrapRect.left - tip.offsetWidth - 14;
      }
      let top = ev.clientY - wrapRect.top - tip.offsetHeight - 8;
      if (top < 0) top = ev.clientY - wrapRect.top + 16;
      tip.style.left = `${Math.max(2, left)}px`;
      tip.style.top = `${top}px`;
    };
    const onLeave = () => {
      tip.style.display = "none";
      cursor.style.display = "none";
      dot.style.display = "none";
    };
    svg.addEventListener("pointermove", onMove);
    svg.addEventListener("pointerleave", onLeave);
  }

  _html(title, body) {
    this.innerHTML = `
      <ha-card header="${title}">
        <style>
          ha-card { padding-bottom: 8px; }
          .chartwrap { position: relative; }
          svg { width: 100%; display: block; touch-action: pan-y; }
          .empty { padding: 16px; color: var(--secondary-text-color); }
          .status { padding: 0 16px 4px; font-size: 14px; }
          .chip { padding: 2px 10px; border-radius: 10px; font-weight: 500; }
          .next { color: var(--secondary-text-color); margin-left: 6px; }
          .ax { font-size: 9px; fill: var(--secondary-text-color); }
          .ax.pr { text-anchor: end; }
          .ax.tx { text-anchor: middle; }
          .ax.tx.day { font-weight: 600; }
          .ax.soc { fill: #7e57c2; }
          .grid { stroke: var(--divider-color, #e0e0e0); stroke-width: 0.5; }
          .grid.day { stroke: var(--secondary-text-color, #999); stroke-width: 1; opacity: 0.55; }
          .grid.zero { stroke: var(--primary-text-color, #444); stroke-width: 0.8; opacity: 0.5; }
          .price { fill: none; stroke: #ffb300; stroke-width: 2; }
          .socline { fill: none; stroke: #7e57c2; stroke-width: 1.5; stroke-dasharray: 4 3; }
          .consline { fill: none; stroke: #26a69a; stroke-width: 1.3; opacity: 0.85; }
          .cons-i { background: #26a69a; }
          .pvarea { fill: rgba(255, 213, 79, 0.25); stroke: rgba(251, 192, 45, 0.6); stroke-width: 1; }
          .now { stroke: var(--error-color, #f44336); stroke-width: 1.5; }
          .cursor { stroke: var(--primary-text-color, #555); stroke-width: 0.8; stroke-dasharray: 2 2; }
          .dot { fill: #ffb300; stroke: var(--card-background-color, #fff); stroke-width: 1.5; }
          .tip { position: absolute; z-index: 5; pointer-events: none;
                 background: var(--card-background-color, #fff);
                 color: var(--primary-text-color, #222);
                 border: 1px solid var(--divider-color, #ddd); border-radius: 6px;
                 box-shadow: 0 2px 8px rgba(0,0,0,0.25);
                 padding: 6px 9px; font-size: 12px; line-height: 1.5; white-space: nowrap; }
          .legend { padding: 4px 16px 0; font-size: 11px; color: var(--secondary-text-color);
                    display: flex; gap: 10px; flex-wrap: wrap; }
          .lg { display: inline-flex; align-items: center; gap: 4px; }
          .lg i { width: 12px; height: 12px; border-radius: 2px; display: inline-block; }
          .lg .li { height: 3px; border-radius: 1px; }
          .price-i { background: #ffb300; }
          .soc-i { background: #7e57c2; }
          .pv-i { width: 12px; height: 12px; border-radius: 2px; display: inline-block;
                  background: rgba(255, 213, 79, 0.45); border: 1px solid rgba(251, 192, 45, 0.9); }
        </style>
        ${body}
      </ha-card>`;
  }
}

// Guard against double-loading (extra_module_url + lovelace resource)
if (!customElements.get("smart-battery-pilot-card")) {
  customElements.define("smart-battery-pilot-card", SmartBatteryPilotCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "smart-battery-pilot-card",
    name: "Smart Battery Pilot Card",
    description: "Price curve, planned battery actions, PV and SOC forecast",
    preview: false,
  });
}
