"""Every form field must be labelled and explained in every language.

The German file used to carry 51 `data_description` help texts that the
English one simply did not have, so English users configured five steps of
inverter jargon with no guidance at all. This keeps the files in lockstep
with each other *and* with the schemas the config flow actually builds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smart_battery_pilot import config_flow, sensor

TRANSLATIONS = Path(config_flow.__file__).parent / "translations"
LANGUAGES = ["en", "de"]

# Translation step id -> the schema builder the flow uses for that step.
CONFIG_STEPS = {
    "user": config_flow.schema_prices,
    "battery": config_flow.schema_battery,
    "control": config_flow.schema_control,
    "consumption": config_flow.schema_consumption,
    "pv": config_flow.schema_pv,
}
OPTIONS_STEPS = {
    "prices": config_flow.schema_prices,
    "battery": config_flow.schema_battery,
    "control": config_flow.schema_control,
    "consumption": config_flow.schema_consumption,
    "pv": config_flow.schema_pv,
    "tuning": config_flow.schema_tuning,
}


def _load(language: str) -> dict:
    return json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))


def _fields(builder) -> set[str]:
    return {str(marker) for marker in builder({}).schema}


def _keys(node, prefix: str = "") -> set[str]:
    found = set()
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        found.add(path)
        if isinstance(value, dict):
            found |= _keys(value, path)
    return found


def test_all_languages_have_the_same_keys():
    reference = _keys(_load(LANGUAGES[0]))
    for language in LANGUAGES[1:]:
        assert _keys(_load(language)) == reference, f"{language}.json diverged"


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(
    ("section", "steps"), [("config", CONFIG_STEPS), ("options", OPTIONS_STEPS)]
)
def test_every_schema_field_is_labelled_and_described(language, section, steps):
    doc = _load(language)[section]["step"]
    for step, builder in steps.items():
        expected = _fields(builder)
        assert set(doc[step]["data"]) == expected, f"{language} {section}.{step} labels"
        assert (
            set(doc[step]["data_description"]) == expected
        ), f"{language} {section}.{step} descriptions"


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_empty_help_texts(language):
    doc = _load(language)
    for section in ("config", "options"):
        for step, body in doc[section]["step"].items():
            for field, text in body.get("data_description", {}).items():
                assert text.strip(), f"{language} {section}.{step}.{field} is empty"


# Enum sensors whose declared options must all be translatable.
ENUM_SENSORS = {
    "current_action": sensor.CurrentActionSensor,
    "next_action": sensor.NextActionSensor,
    "plan_status": sensor.PlanStatusSensor,
    "configuration": sensor.ConfigSensor,
}


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_enum_option_has_a_state_translation(language):
    """An option without a translation shows up raw in the UI.

    Renaming an option and forgetting the translation files is otherwise
    invisible until someone looks at the dashboard.
    """
    states = _load(language)["entity"]["sensor"]
    for key, entity in ENUM_SENSORS.items():
        assert set(states[key]["state"]) == set(entity._attr_options), (
            f"{language} entity.sensor.{key}.state"
        )
