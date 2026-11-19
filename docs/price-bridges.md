# Bridges for price sources without forecast attributes

The pilot reads the price forecast from the attributes of one sensor (see
[price source](configuration.md#price-sources-without-forecast-attributes)).
Integrations that only offer the curve through an action need a small bridge
that publishes it as such a sensor. This page holds worked examples.

The recipe below was contributed by a user and has been running in a live
installation. Adapt the placeholders to your setup.

## Official Nord Pool integration

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
  still applies once, exactly as with the Nordpool HACS integration.
* The `raw_*` timestamps may be UTC, and the local day then starts the evening
  before. The pilot matches slots by timestamp, so this needs no handling.
* The state is the day's average only so that the sensor has a value; the pilot
  reads the attributes.
