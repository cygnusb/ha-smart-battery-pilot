/* Smart Battery Pilot Lovelace card.
 *
 * Three views over the same plan, each giving every unit its own panel and
 * its own scale:
 *
 *   tracks  (default) price / PV + consumption / SOC, stacked
 *   balance           price / PV minus consumption / SOC - what the optimizer
 *                     actually plans against
 *   compact           status, three tiles, SOC and a price sparkline
 *
 * type: custom:smart-battery-pilot-card
 * entity: sensor.<...>_charge_plan   (auto-discovered if omitted/wrong)
 * view: tracks | balance | compact   (optional, default tracks)
 */

// Action keys as they arrive in the charge_plan slots; ACTIONS drives the legend
// order, "auto" is the implicit default and therefore not listed in the legend.
const ACTIONS = ["charge", "idle", "export"];

const TRANSLATIONS = {
  en: {
    action_charge: "Charge",
    action_idle: "Blocked",
    action_export: "Export",
    action_auto: "Auto",
    price: "Price",
    soc: "SOC",
    soc_forecast: "SOC forecast",
    consumption: "Consumption",
    pv: "PV",
    pv_now: "PV now",
    net_demand: "Net demand",
    energy: "Energy",
    balance: "Balance",
    surplus: "Surplus",
    deficit: "Deficit",
    next_change: "Next change",
    from_battery: "From battery",
    charge_power: "Charge power",
    entity_missing: "Entity {entity} not found, and no charge plan entity was detected.",
    no_plan: "No valid charge plan (error: {error})",
    unknown: "unknown",
    next_at: "{action} at {time}",
  },
  de: {
    action_charge: "Laden",
    action_idle: "Gesperrt",
    action_export: "Einspeisen",
    action_auto: "Auto",
    price: "Preis",
    soc: "SOC",
    soc_forecast: "SOC-Prognose",
    consumption: "Verbrauch",
    pv: "PV",
    pv_now: "PV aktuell",
    net_demand: "Netto-Bedarf",
    energy: "Energie",
    balance: "Bilanz",
    surplus: "Überschuss",
    deficit: "Defizit",
    next_change: "Nächster Wechsel",
    from_battery: "Aus dem Speicher",
    charge_power: "Ladeleistung",
    entity_missing: "Entity {entity} nicht gefunden und keine Ladeplan-Entity erkannt.",
    no_plan: "Kein gültiger Ladeplan (Fehler: {error})",
    unknown: "unbekannt",
    next_at: "{action} um {time}",
  },
  da: {
    action_charge: "Lad",
    action_idle: "Blokeret",
    action_export: "Salg",
    action_auto: "Auto",
    price: "Pris",
    soc: "SOC",
    soc_forecast: "SOC-prognose",
    consumption: "Forbrug",
    pv: "PV",
    pv_now: "PV nu",
    net_demand: "Nettobehov",
    energy: "Energi",
    balance: "Balance",
    surplus: "Overskud",
    deficit: "Underskud",
    next_change: "Næste skift",
    from_battery: "Fra batteriet",
    charge_power: "Ladeeffekt",
    entity_missing: "Entiteten {entity} blev ikke fundet, og ingen ladeplan-entitet blev registreret.",
    no_plan: "Ingen gyldig ladeplan (fejl: {error})",
    unknown: "ukendt",
    next_at: "{action} kl. {time}",
  },
  et: {
    action_charge: "Laadimine",
    action_idle: "Blokeeritud",
    action_export: "Müük",
    action_auto: "Auto",
    price: "Hind",
    soc: "SOC",
    soc_forecast: "SOC-i prognoos",
    consumption: "Tarbimine",
    pv: "PV",
    pv_now: "PV praegu",
    net_demand: "Netovajadus",
    energy: "Energia",
    balance: "Bilanss",
    surplus: "Ülejääk",
    deficit: "Puudujääk",
    next_change: "Järgmine muutus",
    from_battery: "Akust",
    charge_power: "Laadimisvõimsus",
    entity_missing: "Olemit {entity} ei leitud ja laadimisplaani olemit ei tuvastatud.",
    no_plan: "Kehtiv laadimisplaan puudub (viga: {error})",
    unknown: "teadmata",
    next_at: "{action} kell {time}",
  },
  fi: {
    action_charge: "Lataa",
    action_idle: "Estetty",
    action_export: "Myynti",
    action_auto: "Auto",
    price: "Hinta",
    soc: "SOC",
    soc_forecast: "SOC-ennuste",
    consumption: "Kulutus",
    pv: "PV",
    pv_now: "PV nyt",
    net_demand: "Nettotarve",
    energy: "Energia",
    balance: "Tase",
    surplus: "Ylijäämä",
    deficit: "Vajaus",
    next_change: "Seuraava muutos",
    from_battery: "Akusta",
    charge_power: "Latausteho",
    entity_missing: "Entiteettiä {entity} ei löytynyt, eikä lataussuunnitelman entiteettiä havaittu.",
    no_plan: "Ei kelvollista lataussuunnitelmaa (virhe: {error})",
    unknown: "tuntematon",
    next_at: "{action} klo {time}",
  },
  lt: {
    action_charge: "Įkrauti",
    action_idle: "Blokuota",
    action_export: "Pardavimas",
    action_auto: "Auto",
    price: "Kaina",
    soc: "SOC",
    soc_forecast: "SOC prognozė",
    consumption: "Suvartojimas",
    pv: "PV",
    pv_now: "PV dabar",
    net_demand: "Grynasis poreikis",
    energy: "Energija",
    balance: "Balansas",
    surplus: "Perteklius",
    deficit: "Trūkumas",
    next_change: "Kitas pokytis",
    from_battery: "Iš baterijos",
    charge_power: "Įkrovimo galia",
    entity_missing: "Objektas {entity} nerastas, ir įkrovimo plano objektas neaptiktas.",
    no_plan: "Nėra galiojančio įkrovimo plano (klaida: {error})",
    unknown: "nežinoma",
    next_at: "{action} {time}",
  },
  lv: {
    action_charge: "Uzlāde",
    action_idle: "Bloķēts",
    action_export: "Pārdošana",
    action_auto: "Auto",
    price: "Cena",
    soc: "SOC",
    soc_forecast: "SOC prognoze",
    consumption: "Patēriņš",
    pv: "PV",
    pv_now: "PV tagad",
    net_demand: "Neto pieprasījums",
    energy: "Enerģija",
    balance: "Bilance",
    surplus: "Pārpalikums",
    deficit: "Iztrūkums",
    next_change: "Nākamā maiņa",
    from_battery: "No baterijas",
    charge_power: "Uzlādes jauda",
    entity_missing: "Entītija {entity} nav atrasta, un uzlādes plāna entītija netika atklāta.",
    no_plan: "Nav derīga uzlādes plāna (kļūda: {error})",
    unknown: "nezināms",
    next_at: "{action} plkst. {time}",
  },
  nb: {
    action_charge: "Lad",
    action_idle: "Blokkert",
    action_export: "Innmating",
    action_auto: "Auto",
    price: "Pris",
    soc: "SOC",
    soc_forecast: "SOC-prognose",
    consumption: "Forbruk",
    pv: "PV",
    pv_now: "PV nå",
    net_demand: "Nettobehov",
    energy: "Energi",
    balance: "Balanse",
    surplus: "Overskudd",
    deficit: "Underskudd",
    next_change: "Neste endring",
    from_battery: "Fra batteriet",
    charge_power: "Ladeeffekt",
    entity_missing: "Entiteten {entity} ble ikke funnet, og ingen ladeplan-entitet ble oppdaget.",
    no_plan: "Ingen gyldig ladeplan (feil: {error})",
    unknown: "ukjent",
    next_at: "{action} kl. {time}",
  },
  nl: {
    action_charge: "Laden",
    action_idle: "Geblokkeerd",
    action_export: "Terugleveren",
    action_auto: "Auto",
    price: "Prijs",
    soc: "SOC",
    soc_forecast: "SOC-prognose",
    consumption: "Verbruik",
    pv: "PV",
    pv_now: "PV nu",
    net_demand: "Nettovraag",
    energy: "Energie",
    balance: "Balans",
    surplus: "Overschot",
    deficit: "Tekort",
    next_change: "Volgende wissel",
    from_battery: "Uit de accu",
    charge_power: "Laadvermogen",
    entity_missing: "Entiteit {entity} niet gevonden en geen laadplan-entiteit gedetecteerd.",
    no_plan: "Geen geldig laadplan (fout: {error})",
    unknown: "onbekend",
    next_at: "{action} om {time}",
  },
  sv: {
    action_charge: "Ladda",
    action_idle: "Blockerad",
    action_export: "Sälj",
    action_auto: "Auto",
    price: "Pris",
    soc: "SOC",
    soc_forecast: "SOC-prognos",
    consumption: "Förbrukning",
    pv: "PV",
    pv_now: "PV nu",
    net_demand: "Nettobehov",
    energy: "Energi",
    balance: "Balans",
    surplus: "Överskott",
    deficit: "Underskott",
    next_change: "Nästa byte",
    from_battery: "Från batteriet",
    charge_power: "Laddeffekt",
    entity_missing: "Entiteten {entity} hittades inte, och ingen laddplansentitet upptäcktes.",
    no_plan: "Ingen giltig laddplan (fel: {error})",
    unknown: "okänd",
    next_at: "{action} kl. {time}",
  },
};

const DEFAULT_LANGUAGE = "en";

// Home Assistant labels Norwegian "nb", but a browser may report "no"/"nn".
// Without this they would fall back to English on a card whose Norwegian
// strings are right there.
const LANGUAGE_ALIASES = { no: "nb", nn: "nb" };

// Rescan interval when no plan entity has been found yet. `set hass` runs on
// every state change in the whole system, so scanning every call is wasteful.
const ENTITY_RESCAN_MS = 5000;

// Home Assistant's timezone, not the browser's: a phone roaming abroad would
// otherwise label the day separators and slot times an hour or more off.
function resolveTimeZone(hass) {
  const tz = hass && hass.config && hass.config.time_zone;
  if (!tz) return undefined;
  try {
    new Intl.DateTimeFormat("en", { timeZone: tz });
    return tz;
  } catch (err) {
    return undefined;
  }
}

// HA's own language wins; fall back to the browser and finally to English.
// "de-CH" and friends resolve via their base tag.
function resolveLanguage(hass) {
  const candidates = [
    hass && hass.locale && hass.locale.language,
    hass && hass.language,
    typeof navigator !== "undefined" ? navigator.language : null,
  ];
  for (const candidate of candidates) {
    if (!candidate) continue;
    if (TRANSLATIONS[candidate]) return candidate;
    const base = String(candidate).split("-")[0];
    if (TRANSLATIONS[base]) return base;
    if (LANGUAGE_ALIASES[base]) return LANGUAGE_ALIASES[base];
  }
  return DEFAULT_LANGUAGE;
}

// Entity ids and coordinator error codes end up in innerHTML.
function esc(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// One scale per drawing surface. The card used to stack a price axis, an SOC
// axis and an unlabeled kWh band on a single plot, which meant PV and
// consumption were normalized against the PV maximum - on a strong PV day that
// pressed a perfectly correct consumption curve onto the baseline. Every view
// below gives each unit its own panel and its own y scale.
const W = 480;
const PAD_L = 42;
const PAD_R = 16;
const PLOT_W = W - PAD_L - PAD_R;
const AXIS_H = 24; // hour labels, plus the date under a day separator

// clipPath ids have to be unique across every card on the dashboard.
let clipSeq = 0;

const VIEWS = ["tracks", "balance", "compact"];
const DEFAULT_VIEW = "tracks";

// y/h of every panel, top to bottom, per view. The action ribbon sits above
// the first panel; the x-axis labels go under the last one.
const LAYOUTS = {
  tracks: {
    ribbon: { y: 6, h: 13 },
    panels: [
      { key: "price", y: 32, h: 60 },
      { key: "energy", y: 116, h: 58 },
      { key: "soc", y: 198, h: 58 },
    ],
  },
  balance: {
    ribbon: { y: 6, h: 13 },
    panels: [
      { key: "price", y: 32, h: 60 },
      { key: "balance", y: 116, h: 58 },
      { key: "soc", y: 198, h: 58 },
    ],
  },
  compact: {
    ribbon: { y: 8, h: 17 },
    panels: [
      { key: "soc", y: 42, h: 76 },
      { key: "price", y: 138, h: 30 },
    ],
  },
};

function layoutHeight(layout) {
  const last = layout.panels[layout.panels.length - 1];
  return last.y + last.h + AXIS_H;
}

function niceTickStep(range, maxTicks) {
  const raw = range / maxTicks;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 2.5, 5, 10]) {
    if (raw <= m * mag) return m * mag;
  }
  return 10 * mag;
}

// Household consumption for a slot: the optimizer reports demand net of PV,
// which goes negative whenever the sun covers the house.
function slotConsumption(slot) {
  return Math.max(0, (slot.net_demand_kwh || 0) + (slot.pv_kwh || 0));
}

// PV minus consumption: what the balance view draws, and exactly the quantity
// the optimizer plans against.
function slotBalance(slot) {
  return (slot.pv_kwh || 0) - slotConsumption(slot);
}

class SmartBatteryPilotCard extends HTMLElement {
  setConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("smart-battery-pilot-card: invalid config");
    }
    this._config = config;
    this._renderedState = null;
    // A view chosen in the card editor resets whatever the in-card toggle had
    // switched to; the config is the authority whenever it is re-applied.
    this._viewOverride = null;
  }

  // "tracks" (default), "balance" or "compact". An unknown value in the YAML
  // draws the default rather than an empty card - a typo in a dashboard should
  // not cost the user their plan.
  _view() {
    if (this._viewOverride && VIEWS.includes(this._viewOverride)) return this._viewOverride;
    const wanted = this._config && this._config.view;
    return VIEWS.includes(wanted) ? wanted : DEFAULT_VIEW;
  }

  set hass(hass) {
    this._hass = hass;
    const lang = resolveLanguage(hass);
    const tz = resolveTimeZone(hass);
    const state = this._resolveState();
    const pvState = this._livePvState(state);
    // plan, live PV, language and timezone unchanged - keep DOM (and tooltip)
    if (
      state === this._renderedState &&
      pvState === this._renderedPv &&
      lang === this._lang &&
      tz === this._tz &&
      !this._renderedSlotIsOver()
    )
      return;
    this._lang = lang;
    this._tz = tz;
    this._renderedState = state;
    this._renderedPv = pvState;
    this._render(state);
  }

  // True once the slot the DOM was drawn for has ended.
  //
  // The plan sensor's state and attributes are the same on both sides of a
  // slot boundary - the plan itself did not change - so Home Assistant fires
  // no state_changed event for it and the comparison above would hold the
  // stale action chip and "now" marker until the next coordinator refresh,
  // up to 30 minutes into a 15-minute slot. Comparing one cached timestamp
  // keeps `set hass`, which runs on every state change in the system, O(1).
  _renderedSlotIsOver() {
    return this._renderedSlotEnd != null && Date.now() >= this._renderedSlotEnd;
  }

  // Calendar fields of `ms` as Home Assistant sees them.
  _zoned(ms) {
    if (!this._zonedFmt || this._zonedFmtTz !== this._tz) {
      this._zonedFmtTz = this._tz;
      this._zonedFmt = new Intl.DateTimeFormat("en-GB", {
        timeZone: this._tz,
        hour12: false,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
    }
    const out = {};
    for (const part of this._zonedFmt.formatToParts(new Date(ms))) {
      if (part.type !== "literal") out[part.type] = Number(part.value);
    }
    // "24:00" is how en-GB spells midnight in this configuration.
    if (out.hour === 24) out.hour = 0;
    return out;
  }

  _tr(key, vars) {
    const table = TRANSLATIONS[this._lang] || TRANSLATIONS[DEFAULT_LANGUAGE];
    let text = table[key];
    if (text === undefined) text = TRANSLATIONS[DEFAULT_LANGUAGE][key];
    if (text === undefined) return key;
    if (!vars) return text;
    return text.replace(/\{(\w+)\}/g, (match, name) =>
      vars[name] === undefined ? match : vars[name]
    );
  }

  _actionLabel(action) {
    const table = TRANSLATIONS[this._lang] || TRANSLATIONS[DEFAULT_LANGUAGE];
    return table[`action_${action}`] || action;
  }

  _fmtTime(ms) {
    return new Date(ms).toLocaleTimeString(this._lang, {
      timeZone: this._tz,
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  _fmtDate(ms) {
    return new Date(ms).toLocaleDateString(this._lang, {
      timeZone: this._tz,
      weekday: "short",
      day: "numeric",
      month: "numeric",
    });
  }

  getCardSize() {
    return this._view() === "compact" ? 4 : 6;
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
    const configured = this._config.entity || this._foundEntity;
    let state = configured ? this._hass.states[configured] : null;
    if (!state) {
      const now = Date.now();
      if (!this._lastScan || now - this._lastScan >= ENTITY_RESCAN_MS) {
        this._lastScan = now;
        this._foundEntity = SmartBatteryPilotCard._findPlanEntity(this._hass) || null;
      }
      if (this._foundEntity) state = this._hass.states[this._foundEntity];
    }
    return state || null;
  }

  _livePvState(planState) {
    const entity =
      planState && planState.attributes && planState.attributes.pv_power_entity;
    if (!entity || !this._hass) return null;
    return this._hass.states[entity] || null;
  }

  _render(state) {
    const title = this._config.title || "Smart Battery Pilot";

    this._renderedSlotEnd = null;
    if (!state) {
      this._html(
        title,
        `<div class="empty">${this._tr("entity_missing", {
          entity: `<code>${esc(this._config.entity || "?")}</code>`,
        })}</div>`
      );
      return;
    }
    if (!state.attributes.slots || state.attributes.slots.length === 0) {
      this._html(
        title,
        `<div class="empty">${this._tr("no_plan", {
          error: esc(state.attributes.error || this._tr("unknown")),
        })}</div>`
      );
      return;
    }

    const slots = state.attributes.slots.map((s) => ({
      ...s,
      startMs: Date.parse(s.start),
      endMs: Date.parse(s.end),
    }));
    this._slots = slots;

    const view = this._view();
    const layout = LAYOUTS[view];
    const first = layout.panels[0];
    const last = layout.panels[layout.panels.length - 1];
    const plotBottom = last.y + last.h;
    const H = layoutHeight(layout);

    const t0 = slots[0].startMs;
    const t1 = slots[slots.length - 1].endMs;
    this._t0 = t0;
    this._t1 = t1;
    const x = (ms) => PAD_L + ((ms - t0) / (t1 - t0)) * PLOT_W;
    this._x = x;

    const scales = this._buildScales(slots, state.attributes, layout);
    this._yPrice = scales.price ? scales.price.y : null;
    this._cursorTop = layout.ribbon.y;
    this._cursorBottom = plotBottom;
    // The hover dot rides the curve the view leads with.
    this._dotAt = scales.price
      ? (slot) => scales.price.y(slot.price)
      : (slot) => scales.soc.y(slot.soc_forecast);

    let chrome = "";
    for (const panel of layout.panels) chrome += this._panelChrome(panel, scales[panel.key]);
    chrome += this._timeGrid(layout, plotBottom + 11);

    const pvMax = Math.max(...slots.map((s) => s.pv_kwh || 0));
    let marks = "";
    for (const panel of layout.panels) {
      marks += this._panelMarks(panel, scales[panel.key], slots, pvMax);
    }

    const nowMs = Date.now();
    let nowLine = "";
    if (nowMs >= t0 && nowMs <= t1) {
      const nx = x(nowMs).toFixed(1);
      nowLine = `<line x1="${nx}" y1="${layout.ribbon.y}" x2="${nx}" y2="${plotBottom}" class="now"/>`;
    }

    const current = slots.find((s) => nowMs >= s.startMs && nowMs < s.endMs);
    this._renderedSlotEnd = current ? current.endMs : null;
    const next = slots.find((s) => s.startMs > nowMs && current && s.action !== current.action);

    const statusBits = [];
    if (current) {
      statusBits.push(
        `<span class="chip ${esc(current.action)}">${esc(this._actionLabel(current.action))}</span>`
      );
    }
    if (next && view !== "compact") {
      statusBits.push(
        `<span class="next">→ ${this._tr("next_at", {
          action: esc(this._actionLabel(next.action)),
          time: this._fmtTime(next.startMs),
        })}</span>`
      );
    }
    const pvState = this._livePvState(state);
    if (pvState && pvState.state !== "unavailable" && pvState.state !== "unknown") {
      const pvNum = Number(pvState.state);
      if (!Number.isNaN(pvNum)) {
        const unit = (pvState.attributes && pvState.attributes.unit_of_measurement) || "W";
        statusBits.push(
          `<span class="next">${this._tr("pv_now")}: ${pvNum.toFixed(0)} ${esc(unit)}</span>`
        );
      }
    }

    this._html(
      title,
      `
      <div class="status">${statusBits.join(" ")}</div>
      ${view === "compact" ? this._tiles(slots, state.attributes, current || slots[0]) : ""}
      <div class="chartwrap">
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
          ${chrome}
          ${this._ribbon(slots, layout.ribbon)}
          ${marks}
          ${nowLine}
          <line id="sbp-cursor" x1="0" y1="${layout.ribbon.y}" x2="0" y2="${plotBottom}" class="cursor" style="display:none"/>
          <circle id="sbp-dot" r="3.5" class="dot" style="display:none"/>
          <rect id="sbp-hit" x="${PAD_L}" y="${first.y}" width="${PLOT_W}" height="${plotBottom - first.y}" fill="transparent"/>
        </svg>
        <div id="sbp-tip" class="tip" style="display:none"></div>
      </div>
      <div class="legend">${this._legend(view, slots, pvMax)}</div>`
    );
    this._attachEvents();
  }

  // --- scales -------------------------------------------------------------

  _buildScales(slots, attrs, layout) {
    const out = {};
    for (const panel of layout.panels) {
      if (panel.key === "price") {
        const prices = slots.map((s) => s.price);
        const lo = Math.min(0, ...prices);
        const hi = Math.max(...prices);
        const step = niceTickStep(hi - lo || 0.1, panel.h >= 50 ? 3 : 2);
        const bot = Math.floor(lo / step) * step;
        const top = Math.ceil((hi + step * 0.12) / step) * step;
        const ticks = [];
        for (let v = bot; v <= top + 1e-9; v += step) ticks.push(v);
        out.price = {
          y: (v) => panel.y + (1 - (v - bot) / (top - bot)) * panel.h,
          ticks,
          fmt: (v) => v.toFixed(2),
          label: `${this._tr("price")} €/kWh`,
        };
      } else if (panel.key === "energy") {
        const hi = Math.max(...slots.map((s) => Math.max(s.pv_kwh || 0, slotConsumption(s))));
        const step = niceTickStep(hi || 0.2, 2);
        const top = Math.max(step, Math.ceil((hi * 1.06) / step) * step);
        out.energy = {
          y: (v) => panel.y + (1 - v / top) * panel.h,
          ticks: [0, top / 2, top],
          fmt: (v) => v.toFixed(top < 1 ? 2 : 1),
          label: `${this._tr("energy")} kWh`,
        };
      } else if (panel.key === "balance") {
        const hi = Math.max(...slots.map((s) => Math.abs(slotBalance(s))));
        const step = niceTickStep(hi || 0.2, 2);
        const top = Math.max(step, Math.ceil((hi * 1.08) / step) * step);
        out.balance = {
          y: (v) => panel.y + panel.h / 2 - (v / top) * (panel.h / 2),
          ticks: [top, 0, -top],
          fmt: (v) => (v > 0 ? "+" : "") + v.toFixed(top < 1 ? 2 : 1),
          label: `${this._tr("balance")} kWh`,
        };
      } else if (panel.key === "soc") {
        const socs = slots.map((s) => Number(s.soc_forecast)).filter((v) => Number.isFinite(v));
        // The configured operating window when the plan sensor reports it -
        // drawing 0-100 % spends most of the panel on a range the battery is
        // never allowed to enter, which is what flattened the discharge depth
        // into a line along the top edge.
        let lo = Number(attrs.min_soc);
        let hi = Number(attrs.max_soc);
        if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) {
          lo = socs.length ? Math.min(...socs) : 0;
          hi = socs.length ? Math.max(...socs) : 100;
        }
        if (socs.length) {
          lo = Math.min(lo, ...socs);
          hi = Math.max(hi, ...socs);
        }
        // A plan that never moves the battery would otherwise magnify rounding
        // noise into a mountain range.
        if (hi - lo < 20) {
          hi = Math.min(100, (hi + lo) / 2 + 10);
          lo = Math.max(0, hi - 20);
          hi = Math.min(100, lo + 20);
        }
        out.soc = {
          y: (v) => panel.y + (1 - (v - lo) / (hi - lo)) * panel.h,
          // A mid tick of 52.5 renders as "53%", which reads like a measured
          // value; snap it to a round number inside the window instead.
          ticks: panel.h >= 50 ? [lo, Math.round((lo + hi) / 10) * 5, hi] : [lo, hi],
          fmt: (v) => `${Math.round(v)}%`,
          label: this._tr("soc"),
        };
      }
    }
    return out;
  }

  // --- chrome -------------------------------------------------------------

  _panelChrome(panel, scale) {
    let out = `<rect x="${PAD_L}" y="${panel.y}" width="${PLOT_W}" height="${panel.h}" class="panel"/>`;
    for (const v of scale.ticks) {
      const y = scale.y(v).toFixed(1);
      const zero = panel.key === "balance" && Math.abs(v) < 1e-9;
      out += `<line x1="${PAD_L}" y1="${y}" x2="${PAD_L + PLOT_W}" y2="${y}" class="grid${
        zero ? " zero" : ""
      }"/>`;
      out += `<text x="${PAD_L - 5}" y="${(+y + 3).toFixed(1)}" class="ax pr">${esc(
        scale.fmt(v)
      )}</text>`;
    }
    return (
      out + `<text x="${PAD_L}" y="${panel.y - 6}" class="ax pl">${esc(scale.label)}</text>`
    );
  }

  // Vertical grid every three hours, anchored to midnight in Home Assistant's
  // timezone. Each tick is re-snapped to the full hour so a DST change does
  // not shear the grid.
  _timeGrid(layout, labelY) {
    const HOUR = 3600000;
    let out = "";
    const startParts = this._zoned(this._t0);
    let tick = this._t0 - ((startParts.hour % 3) * 60 + startParts.minute) * 60000;
    while (tick < this._t0) tick += 3 * HOUR;
    for (let k = 0; tick <= this._t1 && k < 64; k++) {
      const parts = this._zoned(tick);
      if (parts.minute !== 0) tick -= parts.minute * 60000;
      const p = this._zoned(tick);
      const midnight = p.hour === 0;
      const px = this._x(tick).toFixed(1);
      for (const panel of layout.panels) {
        out += `<line x1="${px}" y1="${panel.y}" x2="${px}" y2="${panel.y + panel.h}" class="grid${
          midnight ? " day" : ""
        }"/>`;
      }
      out += `<text x="${px}" y="${labelY}" class="ax tx">${String(p.hour).padStart(
        2,
        "0"
      )}</text>`;
      if (midnight) {
        out += `<text x="${px}" y="${labelY + 10}" class="ax tx day">${esc(
          this._fmtDate(tick)
        )}</text>`;
      }
      tick += 3 * HOUR;
    }
    return out;
  }

  // Planned actions as one labeled band above the panels, instead of the pale
  // washes behind the curves that "auto" and "blocked" were told apart by.
  _ribbon(slots, ribbon) {
    let out = "";
    let runStart = 0;
    for (let i = 0; i <= slots.length; i++) {
      if (i < slots.length && slots[i].action === slots[runStart].action) continue;
      const action = slots[runStart].action;
      const x0 = this._x(slots[runStart].startMs);
      const width = this._x(slots[i - 1].endMs) - x0;
      out += `<rect x="${x0.toFixed(1)}" y="${ribbon.y}" width="${Math.max(
        0,
        width - 1
      ).toFixed(1)}" height="${ribbon.h}" rx="2" class="band ${esc(action)}"/>`;
      if (width > 46) {
        out += `<text x="${(x0 + width / 2).toFixed(1)}" y="${(
          ribbon.y +
          ribbon.h / 2 +
          3.4
        ).toFixed(1)}" class="bandtx ${esc(action)}">${esc(this._actionLabel(action))}</text>`;
      }
      runStart = i;
    }
    return out;
  }

  // --- marks --------------------------------------------------------------

  // `first` is the command the very first point gets: "M" for a standalone
  // line, "L" when the caller has already moved the pen to the baseline for a
  // filled area. Starting an area's steps with "M" opens a second subpath, and
  // the closing "Z" then draws a diagonal across the whole panel.
  _stepPath(slots, value, y, first = "M") {
    let d = "";
    slots.forEach((s, i) => {
      const yy = y(value(s)).toFixed(1);
      d += `${i ? "L" : first}${this._x(s.startMs).toFixed(1)},${yy} L${this._x(s.endMs).toFixed(
        1
      )},${yy} `;
    });
    return d;
  }

  _stepArea(slots, value, y, floor) {
    return (
      `M${this._x(slots[0].startMs).toFixed(1)},${floor.toFixed(1)} ` +
      this._stepPath(slots, value, y, "L") +
      `L${this._x(slots[slots.length - 1].endMs).toFixed(1)},${floor.toFixed(1)} Z`
    );
  }

  _panelMarks(panel, scale, slots, pvMax) {
    const floor = panel.y + panel.h;
    if (panel.key === "price") {
      let out =
        `<path d="${this._stepArea(slots, (s) => s.price, scale.y, floor)}" class="pricearea"/>` +
        `<path d="${this._stepPath(slots, (s) => s.price, scale.y)}" class="price"/>`;
      if (panel.h >= 50) out += this._peakLabel(slots, scale);
      return out;
    }
    if (panel.key === "energy") {
      let out = "";
      if (pvMax > 0) {
        out +=
          `<path d="${this._stepArea(
            slots,
            (s) => s.pv_kwh || 0,
            scale.y,
            floor
          )}" class="pvarea"/>` +
          `<path d="${this._stepPath(slots, (s) => s.pv_kwh || 0, scale.y)}" class="pvline"/>`;
      }
      return (
        out + `<path d="${this._stepPath(slots, slotConsumption, scale.y)}" class="consline"/>`
      );
    }
    if (panel.key === "balance") {
      const zero = scale.y(0);
      const id = `sbp-clip-${(clipSeq += 1)}`;
      return (
        `<defs>` +
        `<clipPath id="${id}-p"><rect x="0" y="${panel.y}" width="${W}" height="${(
          zero - panel.y
        ).toFixed(1)}"/></clipPath>` +
        `<clipPath id="${id}-n"><rect x="0" y="${zero.toFixed(1)}" width="${W}" height="${(
          floor - zero
        ).toFixed(1)}"/></clipPath>` +
        `</defs>` +
        `<path d="${this._stepArea(
          slots,
          (s) => Math.max(0, slotBalance(s)),
          scale.y,
          zero
        )}" class="balarea pos"/>` +
        `<path d="${this._stepArea(
          slots,
          (s) => Math.min(0, slotBalance(s)),
          scale.y,
          zero
        )}" class="balarea neg"/>` +
        `<path d="${this._stepPath(slots, slotBalance, scale.y)}" class="balline pos" clip-path="url(#${id}-p)"/>` +
        `<path d="${this._stepPath(slots, slotBalance, scale.y)}" class="balline neg" clip-path="url(#${id}-n)"/>` +
        `<text x="${PAD_L + 5}" y="${(zero - 5).toFixed(1)}" class="dirlbl pos">${esc(
          this._tr("surplus")
        )}</text>` +
        `<text x="${PAD_L + 5}" y="${(zero + 12).toFixed(1)}" class="dirlbl neg">${esc(
          this._tr("deficit")
        )}</text>`
      );
    }
    // soc
    const end = slots[slots.length - 1];
    const ex = this._x(end.endMs);
    const ey = scale.y(end.soc_forecast);
    return (
      `<path d="${this._stepArea(
        slots,
        (s) => s.soc_forecast,
        scale.y,
        floor
      )}" class="socarea"/>` +
      `<path d="${this._stepPath(slots, (s) => s.soc_forecast, scale.y)}" class="socline"/>` +
      `<circle cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="2.6" class="socdot"/>` +
      `<text x="${(ex - 4).toFixed(1)}" y="${(ey - 6).toFixed(1)}" class="dirlbl soc" text-anchor="end">${Math.round(
        end.soc_forecast
      )}%</text>`
    );
  }

  // The most expensive slot is the one the whole plan is built around, so it
  // gets the only number printed on the price curve.
  _peakLabel(slots, scale) {
    const peak = slots.reduce((best, s) => (s.price > best.price ? s : best), slots[0]);
    const px = (this._x(peak.startMs) + this._x(peak.endMs)) / 2;
    const py = scale.y(peak.price);
    const toTheRight = px > PAD_L + PLOT_W * 0.72;
    return (
      `<circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="2.6" class="pricedot"/>` +
      `<text x="${(px + (toTheRight ? -5 : 5)).toFixed(1)}" y="${(py - 6).toFixed(
        1
      )}" class="dirlbl price" text-anchor="${toTheRight ? "end" : "start"}">${peak.price.toFixed(
        2
      )}</text>`
    );
  }

  // --- compact extras -----------------------------------------------------

  _tiles(slots, attrs, current) {
    const last = slots[slots.length - 1];
    const now = Date.now();
    const next = slots.find((s) => s.startMs > now && s.action !== current.action);
    const discharge = Number(attrs.battery_discharge_kwh);
    const gridCharge = Number(attrs.grid_charge_kwh);
    const cells = [
      {
        label: this._tr("soc"),
        value: `${Math.round(current.soc_forecast)}%`,
        sub: `→ ${Math.round(last.soc_forecast)}% ${this._fmtTime(last.endMs)}`,
      },
      {
        label: this._tr("next_change"),
        value: next ? this._fmtTime(next.startMs) : "—",
        sub: this._actionLabel(next ? next.action : current.action),
      },
      {
        label: this._tr("from_battery"),
        value: Number.isFinite(discharge) ? `${discharge.toFixed(1)} kWh` : "—",
        sub: Number.isFinite(gridCharge)
          ? `${this._actionLabel("charge")} ${gridCharge.toFixed(1)} kWh`
          : "",
      },
    ];
    return (
      `<div class="tiles">` +
      cells
        .map(
          (c) =>
            `<div class="tile"><div class="tl">${esc(c.label)}</div>` +
            `<div class="tv">${esc(c.value)}</div>` +
            `<div class="ts">${esc(c.sub)}</div></div>`
        )
        .join("") +
      `</div>`
    );
  }

  // --- legend -------------------------------------------------------------

  _legend(view, slots, pvMax) {
    const present = ACTIONS.filter((a) => slots.some((s) => s.action === a));
    let out = present
      .map((key) => `<span class="lg"><i class="band-i ${key}"></i>${esc(this._actionLabel(key))}</span>`)
      .join("");
    out += `<span class="lg"><i class="li price-i"></i>${this._tr("price")}</span>`;
    if (view === "tracks") {
      out += `<span class="lg"><i class="li cons-i"></i>${this._tr("consumption")}</span>`;
      if (pvMax > 0) out += `<span class="lg"><i class="pv-i"></i>${this._tr("pv")}</span>`;
    }
    out += `<span class="lg"><i class="li soc-i"></i>${this._tr("soc")}</span>`;
    if (view !== "compact") {
      const other = view === "tracks" ? "balance" : "tracks";
      out += `<button type="button" class="viewtog" data-view="${other}">${esc(
        this._tr(other === "balance" ? "balance" : "energy")
      )}</button>`;
    }
    return out;
  }

  _attachEvents() {
    // The energy panel and the balance panel are two readings of the same
    // slots, so switching between them is a click rather than a config edit.
    const toggle = this.querySelector(".viewtog");
    if (toggle) {
      toggle.addEventListener("click", () => {
        this._viewOverride = toggle.dataset.view;
        this._render(this._renderedState);
      });
    }

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
      const rows = [
        `<b>${this._fmtTime(slot.startMs)}–${this._fmtTime(
          slot.endMs
        )}</b> · ${esc(this._actionLabel(slot.action))}`,
        `${this._tr("price")}: <b>${slot.price.toFixed(4)} €/kWh</b>`,
        `${this._tr("soc_forecast")}: <b>${slot.soc_forecast}%</b>`,
      ];
      const cons = Math.max(0, (slot.net_demand_kwh || 0) + (slot.pv_kwh || 0));
      rows.push(`${this._tr("consumption")}: ${cons.toFixed(2)} kWh`);
      if (slot.pv_kwh) rows.push(`${this._tr("pv")}: ${slot.pv_kwh.toFixed(2)} kWh`);
      if (slot.net_demand_kwh !== undefined)
        rows.push(`${this._tr("net_demand")}: ${slot.net_demand_kwh.toFixed(2)} kWh`);
      // charge_power_w is the pre-0.6.4 name for the same value.
      const power = slot.power_w ?? slot.charge_power_w;
      if (power)
        rows.push(`${this._tr("charge_power")}: ${Math.round(power)} W`);
      tip.innerHTML = rows.join("<br>");
      tip.style.display = "block";

      const slotMidX = this._x((slot.startMs + slot.endMs) / 2);
      cursor.setAttribute("x1", slotMidX);
      cursor.setAttribute("x2", slotMidX);
      cursor.style.display = "";
      dot.setAttribute("cx", slotMidX);
      dot.setAttribute("cy", this._dotAt(slot));
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
      <ha-card header="${esc(title)}">
        <style>
          ha-card { padding-bottom: 8px; }
          .chartwrap { position: relative; }
          svg { width: 100%; display: block; touch-action: pan-y; }
          .empty { padding: 16px; color: var(--secondary-text-color); }
          .status { padding: 0 16px 6px; font-size: 14px; }
          .chip { padding: 2px 10px; border-radius: 10px; font-weight: 500; color: #fff; }
          .chip.auto { background: var(--divider-color, #e0e0e0); color: var(--primary-text-color); }
          .chip.charge { background: #22a04a; }
          .chip.idle { background: #7166d9; }
          .chip.export { background: #e05252; }
          .next { color: var(--secondary-text-color); margin-left: 6px; }

          /* Compact view: the answer before the evidence. */
          .tiles { display: grid; grid-template-columns: repeat(3, 1fr);
                   gap: 1px; background: var(--divider-color, #e0e0e0);
                   border-top: 1px solid var(--divider-color, #e0e0e0);
                   border-bottom: 1px solid var(--divider-color, #e0e0e0);
                   margin: 0 0 8px; }
          .tile { background: var(--card-background-color, #fff); padding: 6px 16px 7px; }
          .tl { font-size: 10px; letter-spacing: 0.05em; text-transform: uppercase;
                color: var(--secondary-text-color); }
          .tv { font-size: 19px; font-weight: 500; line-height: 1.3;
                color: var(--primary-text-color); font-variant-numeric: tabular-nums; }
          .ts { font-size: 11px; color: var(--secondary-text-color); }

          .panel { fill: var(--divider-color, #e0e0e0); opacity: 0.22; }
          .ax { font-size: 9px; fill: var(--secondary-text-color); }
          .ax.pr { text-anchor: end; }
          .ax.pl { font-size: 8.5px; letter-spacing: 0.04em; opacity: 0.9; }
          .ax.tx { text-anchor: middle; }
          .ax.tx.day { font-weight: 600; }
          .grid { stroke: var(--divider-color, #e0e0e0); stroke-width: 0.5; }
          .grid.day { stroke: var(--secondary-text-color, #999); stroke-width: 1; opacity: 0.55; }
          .grid.zero { stroke: var(--secondary-text-color, #666); stroke-width: 1; opacity: 0.8; }

          .band.auto { fill: var(--divider-color, #e0e0e0); }
          .band.charge { fill: #22a04a; }
          .band.idle { fill: #7166d9; }
          .band.export { fill: #e05252; }
          .bandtx { font-size: 10px; font-weight: 500; text-anchor: middle; fill: #fff; }
          .bandtx.auto { fill: var(--secondary-text-color); }

          .price { fill: none; stroke: #eb6834; stroke-width: 2; stroke-linejoin: round; }
          .pricearea { fill: rgba(235, 104, 52, 0.12); stroke: none; }
          .pricedot { fill: #eb6834; stroke: var(--card-background-color, #fff); stroke-width: 1.5; }
          .consline { fill: none; stroke: #1baf7a; stroke-width: 2; stroke-linejoin: round; }
          .pvarea { fill: rgba(237, 161, 0, 0.25); stroke: none; }
          .pvline { fill: none; stroke: rgba(237, 161, 0, 0.95); stroke-width: 1.3; stroke-linejoin: round; }
          .socline { fill: none; stroke: #2a78d6; stroke-width: 2; stroke-linejoin: round; }
          .socarea { fill: rgba(42, 120, 214, 0.16); stroke: none; }
          .socdot { fill: #2a78d6; stroke: var(--card-background-color, #fff); stroke-width: 1.5; }
          .balarea.pos { fill: rgba(42, 120, 214, 0.20); stroke: none; }
          .balarea.neg { fill: rgba(208, 59, 59, 0.20); stroke: none; }
          .balline { fill: none; stroke-width: 1.6; stroke-linejoin: round; }
          .balline.pos { stroke: #2a78d6; }
          .balline.neg { stroke: #d03b3b; }
          .dirlbl { font-size: 10px; font-weight: 500; }
          .dirlbl.pos { fill: #2a78d6; }
          .dirlbl.neg { fill: #d03b3b; }
          .dirlbl.soc { fill: #2a78d6; }
          .dirlbl.price { fill: #eb6834; }

          .now { stroke: var(--error-color, #f44336); stroke-width: 1.5; }
          .cursor { stroke: var(--primary-text-color, #555); stroke-width: 0.8; stroke-dasharray: 2 2; }
          .dot { fill: #eb6834; stroke: var(--card-background-color, #fff); stroke-width: 1.5; }
          .tip { position: absolute; z-index: 5; pointer-events: none;
                 background: var(--card-background-color, #fff);
                 color: var(--primary-text-color, #222);
                 border: 1px solid var(--divider-color, #ddd); border-radius: 6px;
                 box-shadow: 0 2px 8px rgba(0,0,0,0.25);
                 padding: 6px 9px; font-size: 12px; line-height: 1.5; white-space: nowrap; }

          .legend { padding: 6px 16px 0; font-size: 11px; color: var(--secondary-text-color);
                    display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
          .lg { display: inline-flex; align-items: center; gap: 4px; }
          .lg i { width: 12px; height: 12px; border-radius: 2px; display: inline-block; }
          .lg .li { height: 3px; border-radius: 1px; }
          .band-i.charge { background: #22a04a; }
          .band-i.idle { background: #7166d9; }
          .band-i.export { background: #e05252; }
          .price-i { background: #eb6834; }
          .soc-i { background: #2a78d6; }
          .cons-i { background: #1baf7a; }
          .pv-i { background: rgba(237, 161, 0, 0.35);
                  border: 1px solid rgba(237, 161, 0, 0.9); }
          .viewtog { margin-left: auto; font: inherit; font-size: 11px; cursor: pointer;
                     color: var(--primary-color, #03a9f4); background: none;
                     border: 1px solid var(--divider-color, #e0e0e0); border-radius: 10px;
                     padding: 1px 9px; }
          .viewtog:hover { border-color: var(--primary-color, #03a9f4); }
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
