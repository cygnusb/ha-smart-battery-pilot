"""Every form field must be labelled and explained in every language.

The German file used to carry 51 `data_description` help texts that the
English one simply did not have, so English users configured five steps of
inverter jargon with no guidance at all. This keeps the files in lockstep
with each other *and* with the schemas the config flow actually builds.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from smart_battery_pilot import config_flow, sensor

TRANSLATIONS = Path(config_flow.__file__).parent / "translations"
CARD = Path(config_flow.__file__).parent / "frontend" / "smart-battery-pilot-card.js"

# Discovered, not listed: a language file that nobody remembered to add here
# would otherwise ship completely unchecked, which is exactly the situation
# these tests exist to prevent. English is the reference every file is
# compared against.
REFERENCE_LANGUAGE = "en"
LANGUAGES = sorted(p.stem for p in TRANSLATIONS.glob("*.json"))

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


def _flat(node, prefix: str = "") -> dict[str, str]:
    """Every leaf string in the document, keyed by its dotted path."""
    flat = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat |= _flat(value, path)
        else:
            flat[path] = value
    return flat


def _keys(node, prefix: str = "") -> set[str]:
    found = set()
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        found.add(path)
        if isinstance(value, dict):
            found |= _keys(value, path)
    return found


def test_english_is_present():
    assert REFERENCE_LANGUAGE in LANGUAGES
    assert len(LANGUAGES) > 1, "translations directory looks empty"


@pytest.mark.parametrize("language", LANGUAGES)
def test_all_languages_have_the_same_keys(language):
    reference = _keys(_load(REFERENCE_LANGUAGE))
    missing = reference - _keys(_load(language))
    extra = _keys(_load(language)) - reference
    assert not missing, f"{language}.json is missing {sorted(missing)}"
    assert not extra, f"{language}.json has unknown keys {sorted(extra)}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_nothing_was_left_in_english(language):
    """A copied file with untranslated values is worse than no file at all.

    Home Assistant falls back to English on its own, so an English string in a
    Norwegian file is not a fallback - it is a string nobody will ever fix,
    because the file looks complete. Placeholders, units and identifiers are
    the same in every language and are exempt.
    """
    if language == REFERENCE_LANGUAGE:
        return
    shared = _flat(_load(REFERENCE_LANGUAGE)).items() & _flat(_load(language)).items()
    untranslated = {
        path: text
        for path, text in shared
        # Short values are names and units ("SOC", "Auto", "PV", "OK"), which
        # legitimately survive translation unchanged in most languages.
        if len(text) > 24
    }
    assert not untranslated, f"{language}.json still in English: {sorted(untranslated)}"


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(
    ("section", "steps"), [("config", CONFIG_STEPS), ("options", OPTIONS_STEPS)]
)
def test_every_schema_field_is_labelled_and_described(language, section, steps):
    doc = _load(language)[section]["step"]
    for step, builder in steps.items():
        expected = _fields(builder)
        assert set(doc[step]["data"]) == expected, f"{language} {section}.{step} labels"
        assert set(doc[step]["data_description"]) == expected, (
            f"{language} {section}.{step} descriptions"
        )


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


def _card_translations() -> dict[str, set[str]]:
    """The card ships its own string table; parse out {language: keys}.

    The card is plain JS served to the browser and cannot read the
    integration's translation files, so its table is a second place a language
    has to be added. Parsing beats hand-maintaining a copy of the list here.
    """
    source = CARD.read_text(encoding="utf-8")
    start = source.index("const TRANSLATIONS = {")
    body = source[source.index("{", start) :]

    depth = 0
    language = None
    tables: dict[str, set[str]] = {}
    for line in body.splitlines():
        stripped = line.strip()
        if depth == 1:
            match = re.match(r"^([A-Za-z-]+):\s*\{$", stripped)
            if match:
                language = match.group(1)
                tables[language] = set()
        elif depth == 2 and language:
            match = re.match(r"^([a-z_]+):", stripped)
            if match:
                tables[language].add(match.group(1))
        depth += line.count("{") - line.count("}")
        if depth == 0:
            break
    return tables


def test_card_covers_the_same_languages():
    """Otherwise a new language gets a translated config flow and an English card.

    Nothing else connects the two - the integration would set up perfectly and
    the dashboard would silently stay in English.
    """
    assert set(_card_translations()) == set(LANGUAGES)


def test_card_languages_have_the_same_keys():
    tables = _card_translations()
    reference = tables[REFERENCE_LANGUAGE]
    assert reference, "could not parse the card's English strings"
    for language, keys in tables.items():
        assert keys == reference, f"card {language} diverged: {keys ^ reference}"


def test_card_placeholders_survive_translation():
    """{entity}, {error}, {action} and {time} are substituted at render time.

    A translation that drops or renames one produces a message with a hole in
    it, and only in that language.
    """
    source = CARD.read_text(encoding="utf-8")
    placeholders = {
        "entity_missing": {"{entity}"},
        "no_plan": {"{error}"},
        "next_at": {"{action}", "{time}"},
    }
    for key, expected in placeholders.items():
        for value in re.findall(rf'^\s+{key}:\s*"(.*)",$', source, re.MULTILINE):
            missing = {p for p in expected if p not in value}
            assert not missing, f"card {key}: {value!r} lost {sorted(missing)}"
