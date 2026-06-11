# Home Assistant – Energie-System Dokumentation

Stand: 2026-06-11

---

## 1. BYD Battery-Box Premium HV + Fronius Gen24 Ansteuerung

### Architektur

Die BYD HVM (High Voltage Module) Batterie wird **nicht direkt** angesteuert – die Steuerung erfolgt ausschließlich über den **Fronius Gen24 Wechselrichter** per **Modbus TCP** (HA-Hub-Name: `gen24`, Slave: 1).

```
Home Assistant
    └─ modbus.write_register (hub: gen24, slave: 1)
          └─ Fronius Gen24 Wechselrichter
                └─ BYD Battery-Box Premium HV (via interne BYD-Schnittstelle)
```

### Modbus-Register (Fronius Gen24)

| Register | Name                | Beschreibung                                                                                                  |
|----------|---------------------|---------------------------------------------------------------------------------------------------------------|
| 40348    | StorCTL_Mod         | Betriebsmodus: `0` = Auto, `1` = Erzwungenes Laden, `2` = Erzwungenes Entladen                               |
| 40350    | Mindest-Reserve     | Minimaler SOC-Puffer (×100), z.B. `500` = 5%, `1000` = 10%, `2000` = 20%, `9900` = 99% (= volle Pufferreserve beim Zwangsladen) |
| 40355    | Laderate (InWRte)   | Ladeleistung als Promille der Maximalkapazität (10000 = 100%). Bei **Entladen**: Leistungswert direkt. Bei **Laden**: `65536 - Wert` (Zweierkomplement-Logik) |
| 40356    | Entladerate (OutWRte)| Entladeleistung als Promille (10000 = 100%). Bei **Laden**: hier `0` setzen, um Entladen zu sperren           |
| 40232    | PV-Produktion Stop  | `0` = PV stoppt (Register 40232 auf 0, dann 40236 auf 1)                                                     |
| 40236    | PV-Produktion Start | `0` = PV läuft, `1` = PV gesperrt                                                                            |

**Leistungsberechnung:**  
`input_number.charging_power` (Standard: 6000 W) wird durch die Max-Kapazität aus `sensor.reading_battery_settings` (erstes Feld: `12800`) dividiert und mit 10000 multipliziert:
```
Wert = (charging_power / max_capacity) * 10000
     = (6000 / 12800) * 10000 ≈ 4687
```

### Scripts zur Batteriesteuerung

| Script                   | Funktion                                        | Modbus-Aktionen                                                                             |
|--------------------------|-------------------------------------------------|---------------------------------------------------------------------------------------------|
| `script.force_charging`  | Erzwungenes Laden mit konfigurierbarer Leistung | 40355=Ladeleistung (negativ codiert), 40356=0, 40350=9900, 40348=1                         |
| `script.force_discharge` | Erzwungenes Entladen                            | 40356=0, 40355=Entladeleistung, 40348=2                                                     |
| `script.charge_limit`    | Laden bis Limit (ohne Max-Reserve)              | 40356=Ladeleistung, 40348=1                                                                 |
| `script.reset_charging`  | Zurück auf Auto-Modus, Reserve 5%               | 40348=0, 40355=10000, 40350=500, 40356=10000                                                |
| `script.reset_charging_10` | Auto, Reserve 10%                             | 40348=0, 40355=10000, 40350=1000, 40356=10000                                               |
| `script.reset_charging_20` | Auto, Reserve 20%                             | 40348=0, 40355=10000, 40350=2000, 40356=10000                                               |
| `script.pv_stop`         | PV-Produktion stoppen (Gen24 + BKW Garage)      | 40232=0, 40236=1, `button.garage_turn_inverter_off`                                         |
| `script.pv_start`        | PV-Produktion starten                           | 40236=0, `button.garage_turn_inverter_on`                                                   |

### Status-Sensoren (BYD/Batterie)

| Entity                                           | Beschreibung              | Einheit |
|--------------------------------------------------|---------------------------|---------|
| `sensor.byd_battery_box_premium_hv_ladezustand`  | State of Charge (SOC)     | %       |
| `sensor.byd_battery_box_premium_hv_temperatur`   | Batterietemperatur        | °C      |
| `sensor.byd_battery_box_premium_hv_spannung_dc`  | DC-Spannung               | V       |
| `sensor.byd_battery_box_premium_hv_stromstarke_dc` | DC-Strom (negativ = laden) | A    |
| `sensor.byd_battery_box_premium_hv_maximale_kapazitat` | Maximale Kapazität  | Wh      |
| `sensor.byd_storctl_mod`                         | Aktueller Steuermodus     | auto/1/2 |
| `sensor.byd_minrsvpct`                           | Aktuelles Min-Reserve %   | %       |
| `sensor.byd_outwrte`                             | Entlade-Rate              | %       |
| `sensor.byd_inwrte`                              | Lade-Rate                 | %       |
| `sensor.reading_battery_settings`                | Rohe Modbus-Einstellungen (kommasepariert) | – |
| `sensor.solarnet_ladeleistung`                   | Aktuelle Ladeleistung     | W       |
| `sensor.solarnet_entladeleistung`                | Aktuelle Entladeleistung  | W       |

### Automatisierungen (Negativstrompreis)

```yaml
# PV stoppen wenn Börsenpreis < -0,10 EUR/kWh
trigger: sensor.strompreis_zanderweg5 below: -0.1
action: script.pv_stop + evcc auf PV-Modus

# PV wieder starten wenn Preis >= -0,10 EUR/kWh
trigger: sensor.strompreis_zanderweg5 above: -0.101
action: script.pv_start + evcc auf aus + Batterie reset
```

---

## 2. Dynamischer Stromtarif – Nordpool & Tibber

### Nordpool (aktiv, Hauptquelle)

**Integration:** `nordpool` HACS-Custom-Component  
**Hauptentity:** `sensor.nordpool_kwh_ger_eur_3_10_019`

- Konfiguration: Region=GER, Währung=EUR, 3% MwSt., 10% Aufschlag, 0,019 Grundpreis
- Wert: Aktueller Börsen-Stundenpreis in EUR/kWh (Netto-Marktpreis)
- Aktualisierung: Stündlich, morgen-Preise ab ~14:00 Uhr verfügbar

**Attributes mit Preislisten:**

```python
# Zugriff per REST API:
GET /api/states/sensor.nordpool_kwh_ger_eur_3_10_019

# Attributes:
{
  "today": [0.175, 0.168, ...],      # 96 Werte = 15-Min-Intervalle
  "tomorrow": [0.12, 0.13, ...],     # 96 Werte (ab ~14 Uhr verfügbar)
  "tomorrow_valid": true/false,       # ob morgen-Preise schon verfügbar
  "raw_today": [
    {"start": "2026-06-11T00:00:00+02:00", "end": "2026-06-11T00:15:00+02:00", "value": 0.175},
    ...
  ],
  "raw_tomorrow": [...],              # gleiche Struktur für morgen
  "current_price": 0.167,
  "average": 0.133,
  "min": 0.036,
  "max": 0.223
}
```

> **Hinweis:** `today`/`tomorrow` enthalten **96 Werte** (15-Minuten-Intervalle, nicht 24 Stunden-Werte). Die Nordpool-Preise sind eigentlich Stundenpreise – der gleiche Preis wird 4× wiederholt.

**Abgeleiteter Gesamtstrompreis:**  
`sensor.strompreis_zanderweg5` = Nordpool-Wert + 0,187 EUR/kWh (Netzentgelt + Steuern + Abgaben)

```yaml
# template.yaml
- name: "Strompreis Zanderweg5"
  state: >
    {{ (states('sensor.nordpool_kwh_ger_eur_3_10_019') | float(0) + 0.187) | round(4) }}
  unit_of_measurement: "EUR/kWh"
```

**Preisabruf für die nächsten 1-2 Tage (15-Min-Intervalle):**

```python
import requests

HASS_URL = "https://ha.valerius.email"
TOKEN = "<bearer_token>"
headers = {"Authorization": f"Bearer {TOKEN}"}

r = requests.get(f"{HASS_URL}/api/states/sensor.nordpool_kwh_ger_eur_3_10_019", headers=headers)
attrs = r.json()["attributes"]

# Heute (96 × 15-Min-Slots):
today_prices = attrs["raw_today"]   # Liste von {start, end, value}

# Morgen (ab ~14:00 Uhr verfügbar):
if attrs.get("tomorrow_valid"):
    tomorrow_prices = attrs["raw_tomorrow"]

# Gesamtstrompreis (inkl. Netzentgelt):
total_prices_today = [
    {"start": p["start"], "end": p["end"], "price_eur_kwh": round(p["value"] + 0.187, 4)}
    for p in today_prices
]
```

### Tibber (teilweise aktiv)

**Status:** Tibber Pulse-Hardware ist installiert (`update.tibber_pulse_local_update: on`), aber die Tibber-API-Sensoren für Preise sind überwiegend `unavailable`.

| Entity                                    | Status       | Beschreibung                     |
|-------------------------------------------|--------------|----------------------------------|
| `sensor.electricity_price_zander`        | aktiv (0,353 EUR/kWh) | Tibber Preis (ähnlich Nordpool+Aufschlag) |
| `sensor.monthly_cost_zander`             | aktiv (52,69 EUR)     | Monatliche Kosten                |
| `sensor.monthly_net_consumption_zander`  | aktiv (172,31 kWh)    | Monatlicher Nettoverbrauch       |
| `sensor.accumulated_consumption_zander`  | unavailable  | Kumulierter Tagesverbrauch       |
| `sensor.electricity_price_prognose_zanderweg5` | unavailable | Preisvorhersage                |
| `sensor.electricity_price_max/min/avg_zanderweg5` | unavailable | Tagesstatistiken            |

**Fazit:** Für die Preisprognose der nächsten 1-2 Tage ist **Nordpool die zuverlässige Quelle**. Die Tibber-API-Preissensoren sind aktuell nicht funktional.

---

## 3. Hausverbrauch ermitteln

### Realtime-Leistung (empfohlen)

**Primärquelle:** Fronius SolarNet (via `fronius`-Integration)

| Entity                             | Beschreibung                              | Einheit | Aktualisierung |
|------------------------------------|-------------------------------------------|---------|----------------|
| `sensor.solarnet_leistung_verbrauch` | Gesamter Hausverbrauch (Echtzeit)       | W       | ~30 Sek        |
| `sensor.solarnet_leistung_netzbezug` | Aktuell aus dem Netz bezogene Leistung | W       | ~30 Sek        |
| `sensor.solarnet_leistung_netzeinspeisung` | Aktuell ins Netz eingespeist     | W       | ~30 Sek        |
| `sensor.smart_meter_ts_65a_3_wirkleistung` | Grid-Leistung (Tibber Pulse)   | W       | Echtzeit       |

**Formel (Plausibilitätsprüfung):**
```
Hausverbrauch = PV-Leistung + Entladeleistung + Netzbezug - Netzeinspeisung - Ladeleistung
             = sensor.solarnet_leistung_verbrauch  ← wird direkt von Fronius berechnet
```

### Tages-/Stunden-Energie (kWh)

| Entity                             | Zeitraum   | Quelle                 | Beschreibung                        |
|------------------------------------|------------|------------------------|-------------------------------------|
| `sensor.verbrauch_tagesverbrauch`  | Tag        | Netze BW Portal        | Tagesverbrauch Netzbezug (kWh)      |
| `sensor.verbrauch_stundenverbrauch`| Stunde     | Netze BW Portal        | Stündlicher Verbrauch (kWh)         |
| `sensor.verbrauch_15_minuten_verbrauch` | 15 Min | Netze BW Portal       | 15-Minuten-Verbrauch (kWh)          |
| `sensor.solarnet_netzbezug_tag`    | Tag        | SolarNet               | Netzbezug heute (kWh)               |
| `sensor.solarnet_netzbezug_stunde` | Stunde     | SolarNet               | Netzbezug letzte Stunde (kWh)       |
| `sensor.solarnet_netzbezug_15_min` | 15 Min     | SolarNet               | Netzbezug letzte 15 Min (kWh)       |
| `sensor.solarnet_netzbezug_monat`  | Monat      | SolarNet               | Netzbezug diesen Monat (kWh)        |
| `sensor.solarnet_netzbezug_jahr`   | Jahr       | SolarNet               | Netzbezug dieses Jahr (kWh)         |
| `sensor.monthly_net_consumption_zander` | Monat | Tibber              | Monatlicher Nettoverbrauch (kWh)    |

> **Hinweis:** `sensor.verbrauch_tagesverbrauch` liefert nur den reinen **Netzbezug** (Zählerwert der Netze BW). Der tatsächliche Hausverbrauch = Netzbezug + PV-Eigenverbrauch. Für den Gesamthausverbrauch ist `sensor.solarnet_leistung_verbrauch` die bessere Quelle (integriert über Statistics API).

### Historische Daten per Statistics API

```python
# Stündliche Netzbezug-Werte (letzter Tag):
POST /api/recorder/statistics_during_period
{
  "start_time": "2026-06-10T00:00:00Z",
  "end_time": "2026-06-11T00:00:00Z",
  "statistic_ids": ["sensor.smart_meter_ts_65a_3_bezogene_wirkenergie"],
  "period": "hour"
}

# Tageswerte über Wochen:
{
  "period": "day",
  "statistic_ids": ["sensor.solarnet_netzbezug_energie"]
}
```

---

## 4. PV-Produktion ermitteln

### Realtime-Leistung

| Entity                               | Beschreibung                             | Einheit |
|--------------------------------------|------------------------------------------|---------|
| `sensor.solarnet_pv_leistung`        | PV-Leistung gesamt (SolarNet)            | W       |
| `sensor.solarproduktion_dach_ost_leistung` | Fronius Gen24 AC-Leistung (Dach Ost) | W    |
| `sensor.solarproduktion_leistung_gesamt` | Gesamt inkl. Balkonkraftwerk         | W       |
| `sensor.pv_valerius_stromstarke_ac`  | AC-Strom Wechselrichter                  | A       |
| `sensor.pv_valerius_spannung_dc`     | DC-Spannung String 1                     | V       |
| `sensor.pv_valerius_dc_spannung_2`   | DC-Spannung String 2                     | V       |
| `sensor.pv_valerius_wechselrichterstatus` | Wechselrichter-Status (Running/…)   | –       |

### Energie (kWh)

| Entity                             | Zeitraum   | Beschreibung                          |
|------------------------------------|------------|---------------------------------------|
| `sensor.solarenergie_gesamt`       | Gesamt     | Gesamte PV-Produktion seit Inbetrieb  |
| `sensor.solarenergie_jahr`         | Jahr       | PV-Produktion dieses Jahr (kWh)       |
| `sensor.solarenergie_monat`        | Monat      | PV-Produktion diesen Monat (kWh)      |
| `sensor.solarenergie_dach_ostseite`| Gesamt     | Dach Ostseite (kWh, lifetime)         |
| `sensor.solarenergie_dach_westseite`| Gesamt    | Dach Westseite (kWh, lifetime)        |
| `sensor.fronius_energie_gesamt`    | Gesamt     | Fronius Gen24 Gesamtenergie (Wh)      |
| `sensor.solarnet_netzeinspeisung_tag` | Tag     | Einspeisung heute (kWh)               |
| `sensor.solarnet_netzeinspeisung_monat` | Monat | Einspeisung diesen Monat (kWh)       |
| `sensor.solarnet_netzeinspeisung_jahr` | Jahr   | Einspeisung dieses Jahr (kWh)         |
| `sensor.einspeisung_stundenverbrauch` | Stunde  | Einspeisung letzte Stunde (kWh)       |
| `sensor.einspeisung_tagesverbrauch`| Tag        | Einspeisung heute gesamt (kWh)        |
| `sensor.solarnet_autarkiegrad`     | Echtzeit   | Autarkiegrad (%)                      |

### PV-Vorhersage (Open-Meteo Solar Forecast)

| Entity                                  | Beschreibung                         |
|-----------------------------------------|--------------------------------------|
| `sensor.vorhersage_pv_produktion_heute` | Prognostizierte PV heute (kWh)       |
| `sensor.vorhersage_pv_produktion_morgen`| Prognostizierte PV morgen (kWh)      |
| `sensor.vorhersage_pv_produktion_tage_2`| Übermorgen (kWh)                     |
| `sensor.vorhersage_pv_produktion_tage_3`| In 3 Tagen (kWh)                     |
| `sensor.energy_current_hour_west`       | Dach West – diese Stunde (kWh)       |
| `sensor.energy_next_hour_west`          | Dach West – nächste Stunde (kWh)     |
| `sensor.energy_current_hour_bkw`        | Balkonkraftwerk – diese Stunde (kWh) |
| `sensor.energy_production_today_west`   | Dach West gesamt heute (kWh)         |
| `sensor.energy_production_tomorrow_west`| Dach West morgen (kWh)               |
| `sensor.vorhersage_solarproduktion_gesamt_heute` | Gesamte Anlage heute (kWh) |
| `sensor.vorhersage_solarproduktion_gesamt_morgen`| Gesamte Anlage morgen (kWh)|

### Stündliche/Tägliche PV-Daten per Statistics API

```python
# Stündliche Produktionsdaten (via Long-Term Statistics):
POST /api/recorder/statistics_during_period
{
  "start_time": "2026-06-10T00:00:00Z",
  "end_time": "2026-06-11T00:00:00Z",
  "statistic_ids": ["sensor.solarenergie_gesamt"],
  "period": "hour",
  "types": ["change"]  # Delta pro Stunde
}

# Für Tageswerte:
{
  "period": "day",
  "types": ["change"]
}
```

---

## 5. Wettervorhersage

### Aktive Wetterquellen

| Entity                  | Integration        | Aktuell  | Vorhersage |
|-------------------------|--------------------|----------|------------|
| `weather.forecast_home` | Meteorologis/HA    | ✓        | Daily ✓    |
| `weather.eggstoi`       | Unbekannte Quelle  | ✓        | Nein       |

### Aktuelle Wetterwerte

```python
GET /api/states/weather.forecast_home
# Attributes: temperature, humidity, wind_speed, wind_bearing, pressure, dew_point, visibility

GET /api/states/weather.eggstoi
# temperature, wind_speed, wind_bearing (kein humidity)
```

### Tagesvorhersage abrufen

```python
# Daily Forecast (nächste Tage):
POST /api/services/weather/get_forecasts?return_response
{
  "entity_id": "weather.forecast_home",
  "type": "daily"
}

# Response:
{
  "weather.forecast_home": {
    "forecast": [
      {
        "datetime": "2026-06-11T12:00:00+00:00",
        "temperature": 18.4,    # Tageshöchsttemperatur
        "templow": 10.1,         # Tagestiefsttemperatur
        "precipitation": 0.6,   # Niederschlag mm
        "condition": "partlycloudy",
        "wind_speed": 12.0,
        "humidity": 70
      },
      ...
    ]
  }
}
```

> **Hinweis:** Stündliche Vorhersage (`type: "hourly"`) liefert aktuell keine Daten für `weather.forecast_home`. Für stündliche Temperaturen sind die Solar-Forecast-Sensoren der bessere Proxy.

### Template-Sensoren (abgeleitet, immer aktuell)

| Entity                          | Quelle                  | Beschreibung                             |
|---------------------------------|-------------------------|------------------------------------------|
| `sensor.aussen_temperatur`      | Lokale Sensoren + eggstoi | Beste verfügbare Außentemperatur (°C)  |
| `sensor.si_temperatur`          | weather.forecast_home   | Aktuelle Temperatur laut Wetterdienst   |
| `sensor.si_temperatur_max`      | Daily Forecast          | Heutige Max-Temperatur (°C)             |
| `sensor.si_temperatur_min`      | Daily Forecast          | Heutige Min-Temperatur (°C)             |
| `sensor.si_niederschlag_prognose` | Daily Forecast        | Heutiger Niederschlag (mm)              |
| `sensor.si_luftfeuchtigkeit`    | weather.forecast_home   | Luftfeuchtigkeit (%)                    |
| `sensor.si_luftdruck`           | weather.forecast_home   | Luftdruck (hPa)                         |
| `sensor.si_taupunkt`            | weather.forecast_home   | Taupunkt (°C)                           |
| `sensor.si_windgeschwindigkeit` | weather.forecast_home   | Windgeschwindigkeit (km/h)              |

> Die SI-Trigger-Sensoren (`si_temperatur_max`, `si_temperatur_min`, `si_niederschlag_prognose`) werden täglich um 01:00 Uhr und beim HA-Start per `weather.get_forecasts` aktualisiert.

---

## 6. Zusammenfassung – wichtigste Entities für Energieoptimierung

```python
# Aktueller Strompreis (Gesamtpreis inkl. Netzentgelt):
sensor.strompreis_zanderweg5           # EUR/kWh

# Preise nächste 1-2 Tage (15-Min-Slots in Attributen):
sensor.nordpool_kwh_ger_eur_3_10_019   # raw_today, raw_tomorrow

# Batteriezustand:
sensor.byd_battery_box_premium_hv_ladezustand  # SOC %
sensor.byd_storctl_mod                          # auto/1/2
sensor.solarnet_ladeleistung                    # Laden W
sensor.solarnet_entladeleistung                 # Entladen W

# PV aktuell:
sensor.solarproduktion_leistung_gesamt  # W gesamt
sensor.vorhersage_pv_produktion_morgen  # kWh Prognose morgen

# Hausverbrauch aktuell:
sensor.solarnet_leistung_verbrauch      # W (Echtzeit)
sensor.solarnet_leistung_netzbezug      # W (Netzbezug Echtzeit)

# Energie historisch:
sensor.solarnet_netzbezug_tag           # kWh heute
sensor.solarnet_netzeinspeisung_tag     # kWh heute eingespeist
sensor.solarenergie_monat               # kWh PV dieser Monat

# Wetter:
sensor.si_temperatur_max                # Heutige Höchsttemperatur
sensor.si_temperatur_min                # Heutige Tiefsttemperatur
sensor.aussen_temperatur                # Aktuelle Außentemperatur
```
