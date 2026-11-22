# Bridges for price sources without forecast attributes

The pilot reads the price forecast from the attributes of one sensor (see
[price source](configuration.md#price-sources-without-forecast-attributes)).
Integrations that only offer the curve through an action, or carry it in a
format the pilot does not read, need a small bridge that publishes it as such a
sensor. This page holds worked examples. Each says where it comes from; adapt
the placeholders to your setup.

| Price source | Bridge |
|---|---|
| Official Home Assistant **Nord Pool** integration | [automation + MQTT sensor](#official-nord-pool-integration) |
| Official Home Assistant **Tibber** integration | [trigger template sensor](#official-tibber-integration) |
| **Belgian electricity prices** (`be_electricity_prices`, custom integration) | [template sensor](#belgian-electricity-prices-be_electricity_prices) |

## Bridges that deliver the final price

Some bridges publish the price you actually pay, fees and taxes included
(`all_in` of the Belgian integration, and Tibber's price where your contract
includes the grid fees). Two settings follow from that:

* **Price offset `0`**, so fees and taxes are not counted twice. Whatever you
  pay on top of the published price, such as grid fees billed separately, still
  belongs in the offset.
* **A fixed feed-in tariff.** A feed-in of `0` means "market price minus the
  price offset". With a final-price curve and an offset of `0`, that would value
  every exported kWh at the full consumer price. In self-consumption mode the plan
  does not change, but the battery benefit then charges every kWh of stored PV
  at that price, so the benefit comes out too low (see
  [Price source](configuration.md#1-price-source)). Enter your average feed-in
  price instead.

Then check the [minimum price spread](configuration.md#options-settings--integrations--configure)
against the new curve: a final-price curve usually swings by a different
amount than the spot price did.

## Official Nord Pool integration

Contributed by a user; running in a live installation.

The official Home Assistant **Nord Pool** integration exposes only the current
price as a sensor. The curve comes from the `nordpool.get_prices_for_date`
action, with prices in currency per MWh. The bridge below calls the action for
today and tomorrow, converts to EUR/kWh and publishes the result as the
attributes the pilot's Nordpool adapter already understands (`today`,
`tomorrow`, `raw_today`, `raw_tomorrow`, `tomorrow_valid`, `average`).

It uses an automation and a retained MQTT sensor, so it needs an MQTT broker.
The retained state survives restarts, and `configuration.yaml` stays untouched.

**1. Create the sensor** (once), with MQTT discovery. Publish this retained, for
example from Developer Tools → MQTT:

```yaml
topic: homeassistant/sensor/nordpool_curve/config
payload:
  name: Nord Pool price curve
  unique_id: nord_pool_price_curve_mqtt
  state_topic: homeassistant/sensor/nordpool_curve/state
  json_attributes_topic: homeassistant/sensor/nordpool_curve/attributes
  unit_of_measurement: EUR/kWh
  state_class: measurement
```

**2. Refresh automation**, every 10 minutes and at Home Assistant start.
Replace the config entry id and the area:

```yaml
triggers:
  - trigger: time_pattern
    minutes: /10
  - trigger: homeassistant
    event: start
actions:
  - action: nordpool.get_prices_for_date
    data:
      config_entry: "<your nordpool config entry id>"
      date: "{{ now().date() }}"
      areas: NL            # your area
      currency: EUR
    response_variable: curve_today
  - action: nordpool.get_prices_for_date
    data:
      config_entry: "<your nordpool config entry id>"
      date: "{{ (now() + timedelta(days=1)).date() }}"
      areas: NL
      currency: EUR
    response_variable: curve_tomorrow
  - variables:
      slots_today: "{{ curve_today['NL'] | default([], true) }}"
      slots_tomorrow: "{{ curve_tomorrow['NL'] | default([], true) }}"
      raw_today: >-
        {% set ns = namespace(items=[]) %}
        {% for p in slots_today %}
          {% set ns.items = ns.items + [{'start': p.start, 'end': p.end, 'value': (p.price / 1000) | round(5)}] %}
        {% endfor %}
        {{ ns.items }}
      raw_tomorrow: >-
        {% set ns = namespace(items=[]) %}
        {% for p in slots_tomorrow %}
          {% set ns.items = ns.items + [{'start': p.start, 'end': p.end, 'value': (p.price / 1000) | round(5)}] %}
        {% endfor %}
        {{ ns.items }}
      avg_price: >-
        {% if slots_today | count == 0 %}{{ none }}{% else %}
        {{ ((slots_today | map(attribute='price') | list | sum / slots_today | count) / 1000) | round(5) }}
        {% endif %}
  - action: mqtt.publish
    data:
      topic: homeassistant/sensor/nordpool_curve/state
      payload: "{{ avg_price if slots_today | count > 0 else 'unavailable' }}"
      retain: true
  - action: mqtt.publish
    data:
      topic: homeassistant/sensor/nordpool_curve/attributes
      payload: >-
        {{ {'today': raw_today | map(attribute='value') | list,
            'tomorrow': raw_tomorrow | map(attribute='value') | list,
            'raw_today': raw_today, 'raw_tomorrow': raw_tomorrow,
            'tomorrow_valid': raw_tomorrow | count > 0,
            'average': avg_price} | to_json }}
      retain: true
```

**3.** Select `sensor.nord_pool_price_curve` as the price entity.

* The prices are the raw spot price, so the [price offset](configuration.md#1-price-source)
  still applies once, exactly as with the Nordpool custom integration.
* The `raw_*` timestamps may be UTC, and the local day then starts the evening
  before. The pilot matches slots by timestamp, so this needs no handling.
* The state is the day's average only so that the sensor has a value; the pilot
  reads the attributes.

## Official Tibber integration

Contributed by a user; running in a live installation with 15-minute prices.
Only the sensor names and the midnight refresh time (see the notes) differ
from the contributed version.

The official Home Assistant **Tibber** integration's price sensor carries the
current price and a few statistics, but no price list. The curve comes from the
`tibber.get_prices` action, which returns the consumer price of your Tibber
contract. The bridge has two stages: a trigger template sensor that calls the
action, and a second sensor that turns its result into the plain `today` /
`tomorrow` lists the pilot reads.

The example uses [packages](https://www.home-assistant.io/docs/configuration/packages/);
the `template:` blocks can also go into an existing template configuration.

**1. Fetch the prices** (`packages/tibber_prices.yaml`):

```yaml
template:
  - triggers:
      - trigger: homeassistant
        event: start
      - trigger: time
        at: "00:00:30"
      - trigger: time
        at: "13:05:00"
      - trigger: time
        at: "14:05:00"
      - trigger: time
        at: "15:05:00"

    actions:
      - action: tibber.get_prices
        data:
          start: "{{ today_at('00:00').isoformat() }}"
          end: "{{ (today_at('00:00') + timedelta(days=2)).isoformat() }}"
        response_variable: tibber_price_response

      # Select the first Tibber home.
      - variables:
          price_rows: >-
            {{ tibber_price_response.prices.values()
               | first | default([], true) }}

      # Split the returned prices by local calendar day.
      - variables:
          today_raw: >-
            {% set ns = namespace(items=[]) %}
            {% for row in price_rows %}
              {% if (as_datetime(row.start_time) | as_local).date()
                    == now().date() %}
                {% set ns.items = ns.items +
                   [{'value': row.price | float}] %}
              {% endif %}
            {% endfor %}
            {{ ns.items }}

          tomorrow_raw: >-
            {% set ns = namespace(items=[]) %}
            {% for row in price_rows %}
              {% if (as_datetime(row.start_time) | as_local).date()
                    == (now() + timedelta(days=1)).date() %}
                {% set ns.items = ns.items +
                   [{'value': row.price | float}] %}
              {% endif %}
            {% endfor %}
            {{ ns.items }}

    sensor:
      - name: "Tibber price curve raw"
        unique_id: tibber_price_curve_raw
        unit_of_measurement: "EUR/kWh"

        state: >-
          {% set ns = namespace(price=none) %}
          {% for row in price_rows %}
            {% if (as_datetime(row.start_time) | as_local) <= now() %}
              {% set ns.price = row.price %}
            {% endif %}
          {% endfor %}
          {{ ns.price if ns.price is not none else 'unknown' }}

        attributes:
          raw_today: "{{ today_raw }}"
          raw_tomorrow: "{{ tomorrow_raw }}"
          slots_today: "{{ today_raw | count }}"
          slots_tomorrow: "{{ tomorrow_raw | count }}"
```

**2. Publish them as lists** (`packages/tibber_price_lists.yaml`):

```yaml
template:
  - sensor:
      - name: "Tibber price curve"
        unique_id: tibber_price_curve
        unit_of_measurement: "EUR/kWh"

        state: >-
          {{ states('sensor.tibber_price_curve_raw') }}

        attributes:
          today: >-
            {{ state_attr('sensor.tibber_price_curve_raw', 'raw_today')
               | default([], true)
               | map(attribute='value')
               | map('float', 0)
               | list }}

          tomorrow: >-
            {{ state_attr('sensor.tibber_price_curve_raw', 'raw_tomorrow')
               | default([], true)
               | map(attribute='value')
               | map('float', 0)
               | list }}
```

**3.** Select `sensor.tibber_price_curve` (the second sensor) as the price
entity. The pilot reads it as plain hourly arrays (`hourly_arrays` in the
diagnostics). The first sensor is not in a format the pilot reads. When the
Tibber price already contains everything you pay per kWh, set the price offset
to `0` and a fixed feed-in tariff, see
[bridges that deliver the final price](#bridges-that-deliver-the-final-price);
fees billed separately stay in the offset.

* With more than one Tibber home, pick the right entry of
  `tibber_price_response.prices` instead of the first.
* Plain lists carry no dates: the pilot takes `today` to start at the current
  local midnight. That is why the first refresh runs right after midnight
  (the contributed version ran it at 00:05); until it has run, `today` still
  holds the previous day. The number of values
  per day is not fixed, so the clock-change days work.
* `tomorrow` stays empty until Tibber publishes the next day's prices,
  usually early in the afternoon. The refreshes after 13:00 pick them up.

## Belgian electricity prices (`be_electricity_prices`)

Written by the maintainer against the integration's source and confirmed
working by a user.

The [Belgian electricity prices](https://github.com/renaudallard/homeassistant_be_electricity_prices)
integration computes the price Belgian contracts really charge, with network
costs and taxes, which a spot price plus a fixed offset can only approximate.
Its current price sensor carries the curve in `today` / `tomorrow`, but each row
is `{start, energy, network, taxes, all_in}`, a shape the pilot does not read.
`all_in` is the price it needs. This template sensor republishes it in the
format of the Nordpool adapter (`configuration.yaml`; replace
`sensor.YOUR_CURRENT_PRICE` with the integration's current price sensor):

```yaml
template:
  - sensor:
      - name: BE price curve
        unique_id: be_price_curve
        unit_of_measurement: EUR/kWh
        state: "{{ states('sensor.YOUR_CURRENT_PRICE') }}"
        attributes:
          raw_today: >-
            {%- set ns = namespace(rows=[]) -%}
            {%- for r in state_attr('sensor.YOUR_CURRENT_PRICE', 'today') or [] -%}
              {%- set ns.rows = ns.rows + [{'start': r.start, 'value': r.all_in}] -%}
            {%- endfor -%}
            {{ ns.rows }}
          raw_tomorrow: >-
            {%- set ns = namespace(rows=[]) -%}
            {%- for r in state_attr('sensor.YOUR_CURRENT_PRICE', 'tomorrow') or [] -%}
              {%- set ns.rows = ns.rows + [{'start': r.start, 'value': r.all_in}] -%}
            {%- endfor -%}
            {{ ns.rows }}
          tomorrow_valid: "{{ (state_attr('sensor.YOUR_CURRENT_PRICE', 'tomorrow') or []) | count > 0 }}"
```

Select `sensor.be_price_curve` as the price entity, with the price offset at
`0` and a fixed feed-in tariff, see
[bridges that deliver the final price](#bridges-that-deliver-the-final-price).
Before you switch, compare the bridge's `raw_today` values (Developer Tools →
States) with what the integration shows. Both 15-minute and hourly contracts
work, since the pilot takes the slot length from the timestamps.
