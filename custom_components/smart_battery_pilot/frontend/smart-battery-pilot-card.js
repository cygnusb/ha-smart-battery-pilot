/* Smart Battery Pilot Lovelace card.
 *
 * Shows the price curve, the planned actions as colored bands and the
 * projected SOC from the charge plan sensor.
 *
 * type: custom:smart-battery-pilot-card
 * entity: sensor.smart_battery_pilot_charge_plan
 */

const ACTION_COLORS = {
  charge: "rgba(67, 160, 71, 0.45)",
  idle: "rgba(120, 130, 140, 0.30)",
  export: "rgba(255, 152, 0, 0.45)",
  auto: "rgba(3, 169, 244, 0.12)",
};

const ACTION_LABELS = {
  charge: "Laden",
  idle: "Gesperrt",
  export: "Einspeisen",
  auto: "Auto",
};

class SmartBatteryPilotCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity) {
      throw new Error("smart-battery-pilot-card: 'entity' is required");
    }
    this._config = config;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 5;
  }

  static getStubConfig() {
    return { entity: "sensor.smart_battery_pilot_charge_plan" };
  }

  _render() {
    if (!this._hass || !this._config) return;
    const state = this._hass.states[this._config.entity];
    const title = this._config.title || "Smart Battery Pilot";

    if (!state || !state.attributes.slots || state.attributes.slots.length === 0) {
      this._html(title, `<div class="empty">Kein gültiger Ladeplan</div>`);
      return;
    }

    const slots = state.attributes.slots.map((s) => ({
      ...s,
      startMs: Date.parse(s.start),
      endMs: Date.parse(s.end),
    }));

    const W = 480;
    const H = 200;
    const PAD_L = 44;
    const PAD_R = 40;
    const PAD_T = 12;
    const PAD_B = 26;
    const plotW = W - PAD_L - PAD_R;
    const plotH = H - PAD_T - PAD_B;

    const t0 = slots[0].startMs;
    const t1 = slots[slots.length - 1].endMs;
    const x = (ms) => PAD_L + ((ms - t0) / (t1 - t0)) * plotW;

    const prices = slots.map((s) => s.price);
    let pMin = Math.min(...prices, 0);
    let pMax = Math.max(...prices);
    const pPad = (pMax - pMin) * 0.1 || 0.05;
    pMax += pPad;
    pMin -= pPad;
    const yPrice = (p) => PAD_T + (1 - (p - pMin) / (pMax - pMin)) * plotH;
    const ySoc = (soc) => PAD_T + (1 - soc / 100) * plotH;

    // Action bands
    let bands = "";
    for (const s of slots) {
      if (s.action === "auto") continue;
      const color = ACTION_COLORS[s.action] || "transparent";
      bands += `<rect x="${x(s.startMs).toFixed(1)}" y="${PAD_T}" width="${(
        x(s.endMs) - x(s.startMs)
      ).toFixed(1)}" height="${plotH}" fill="${color}"/>`;
    }

    // Price step line
    let pricePath = "";
    for (const s of slots) {
      const y = yPrice(s.price).toFixed(1);
      pricePath += `${pricePath ? "L" : "M"}${x(s.startMs).toFixed(1)},${y} L${x(
        s.endMs
      ).toFixed(1)},${y} `;
    }

    // SOC forecast line (end-of-slot values)
    let socPath = `M${x(slots[0].startMs).toFixed(1)},${ySoc(
      slots[0].soc_forecast
    ).toFixed(1)} `;
    for (const s of slots) {
      socPath += `L${x(s.endMs).toFixed(1)},${ySoc(s.soc_forecast).toFixed(1)} `;
    }

    // Axis labels
    const fmtH = (ms) => {
      const d = new Date(ms);
      return `${String(d.getHours()).padStart(2, "0")}`;
    };
    let xLabels = "";
    for (let ms = Math.ceil(t0 / 21600000) * 21600000; ms <= t1; ms += 21600000) {
      xLabels += `<text x="${x(ms).toFixed(1)}" y="${H - 8}" class="ax">${fmtH(
        ms
      )}h</text>`;
      xLabels += `<line x1="${x(ms).toFixed(1)}" y1="${PAD_T}" x2="${x(
        ms
      ).toFixed(1)}" y2="${PAD_T + plotH}" class="grid"/>`;
    }
    const yLabels =
      `<text x="4" y="${yPrice(pMax - pPad) + 4}" class="ax">${(pMax - pPad).toFixed(2)}€</text>` +
      `<text x="4" y="${yPrice(pMin + pPad) + 4}" class="ax">${(pMin + pPad).toFixed(2)}€</text>` +
      `<text x="${W - 36}" y="${ySoc(100) + 10}" class="ax soc">100%</text>` +
      `<text x="${W - 36}" y="${ySoc(0)}" class="ax soc">0%</text>`;

    // Now marker
    const now = Date.now();
    let nowLine = "";
    if (now >= t0 && now <= t1) {
      nowLine = `<line x1="${x(now).toFixed(1)}" y1="${PAD_T}" x2="${x(now).toFixed(
        1
      )}" y2="${PAD_T + plotH}" class="now"/>`;
    }

    const current = slots.find((s) => now >= s.startMs && now < s.endMs);
    const next = slots.find(
      (s) => s.startMs > now && current && s.action !== current.action
    );
    const fmtTime = (ms) =>
      new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

    const statusBits = [];
    if (current) {
      statusBits.push(
        `<span class="chip" style="background:${
          ACTION_COLORS[current.action] || "#ccc"
        }">${ACTION_LABELS[current.action] || current.action}</span>`
      );
    }
    if (next) {
      statusBits.push(
        `<span class="next">→ ${ACTION_LABELS[next.action] || next.action} um ${fmtTime(
          next.startMs
        )}</span>`
      );
    }

    const legend = Object.entries(ACTION_LABELS)
      .map(
        ([key, label]) =>
          `<span class="lg"><i style="background:${ACTION_COLORS[key]}"></i>${label}</span>`
      )
      .join("");

    this._html(
      title,
      `
      <div class="status">${statusBits.join(" ")}</div>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
        ${bands}
        ${xLabels}
        ${yLabels}
        <path d="${pricePath}" class="price"/>
        <path d="${socPath}" class="socline"/>
        ${nowLine}
      </svg>
      <div class="legend">${legend}
        <span class="lg"><i class="li price-i"></i>Preis</span>
        <span class="lg"><i class="li soc-i"></i>SOC</span>
      </div>`
    );
  }

  _html(title, body) {
    this.innerHTML = `
      <ha-card header="${title}">
        <style>
          ha-card { padding-bottom: 8px; }
          svg { width: 100%; display: block; }
          .empty { padding: 16px; color: var(--secondary-text-color); }
          .status { padding: 0 16px 4px; font-size: 14px; }
          .chip { padding: 2px 10px; border-radius: 10px; font-weight: 500; }
          .next { color: var(--secondary-text-color); margin-left: 6px; }
          .ax { font-size: 9px; fill: var(--secondary-text-color); }
          .ax.soc { fill: #7e57c2; }
          .grid { stroke: var(--divider-color, #e0e0e0); stroke-width: 0.5; }
          .price { fill: none; stroke: #ffb300; stroke-width: 2; }
          .socline { fill: none; stroke: #7e57c2; stroke-width: 1.5; stroke-dasharray: 4 3; }
          .now { stroke: var(--error-color, #f44336); stroke-width: 1.5; }
          .legend { padding: 4px 16px 0; font-size: 11px; color: var(--secondary-text-color);
                    display: flex; gap: 10px; flex-wrap: wrap; }
          .lg { display: inline-flex; align-items: center; gap: 4px; }
          .lg i { width: 12px; height: 12px; border-radius: 2px; display: inline-block; }
          .lg .li { height: 3px; border-radius: 1px; }
          .price-i { background: #ffb300; }
          .soc-i { background: #7e57c2; }
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
    description: "Price curve, planned battery actions and SOC forecast",
    preview: false,
  });
}
