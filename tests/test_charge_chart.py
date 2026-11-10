"""The picture of the cold-weather curve in the options dialog."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from xml.etree import ElementTree

from smart_battery_pilot.forecast.charge_chart import (
    INK,
    LEARNED,
    SLIDERS,
    derating_image_markdown,
    derating_svg,
)
from smart_battery_pilot.forecast.charge_rate import (
    ChargeRateModel,
    ChargeSample,
    curve_from_percentages,
)

AT = datetime(2026, 1, 10, 3, 0, tzinfo=UTC)
SVG = "{http://www.w3.org/2000/svg}"


def _model(samples=()):
    model = ChargeRateModel(curve_from_percentages([10, 20, 50, 80, 100]))
    for sample in samples:
        model.add_sample(sample)
    return model


def _learned():
    samples = [ChargeSample(t, 0.55, True, AT) for t in (4.0, 5.5, 7.0, 8.0, 8.5, 9.0)]
    return _model([*samples, ChargeSample(12.0, 1.0, False, AT)])


def _parse(svg):
    return ElementTree.fromstring(svg)


def _curves(root, colour):
    return [p for p in root.findall(f".//{SVG}path") if p.get("stroke") == colour]


def test_an_empty_model_draws_the_sliders_alone():
    root = _parse(derating_svg(_model()))
    assert len(_curves(root, SLIDERS)) == 1
    assert _curves(root, LEARNED) == []
    assert root.findall(f".//{SVG}circle") == []


def test_a_learned_model_adds_its_curve_and_one_dot_per_slot():
    root = _parse(derating_svg(_learned()))
    # Bold between 4 and 12 °C where it was measured, thin on either side.
    widths = [p.get("stroke-width") for p in _curves(root, LEARNED)]
    assert widths == ["1.25", "3.0", "1.25"]
    # One dot per slot the battery limited. The slot it took in full is a
    # triangle: it only says the limit lies at least there.
    assert len(root.findall(f".//{SVG}circle")) == 6
    assert len(_curves(root, INK)) == 1


def test_observations_without_a_fit_are_drawn_without_a_learned_curve():
    root = _parse(derating_svg(_model([ChargeSample(12.0, 0.7, True, AT)] * 3)))
    assert _curves(root, LEARNED) == []
    assert len(root.findall(f".//{SVG}circle")) == 3


def test_the_battery_temperature_is_marked_when_known():
    without = derating_svg(_learned())
    marked = derating_svg(_learned(), 7.0)
    assert len(_parse(marked).findall(f".//{SVG}line")) == (
        len(_parse(without).findall(f".//{SVG}line")) + 1
    )


def test_the_window_widens_to_cold_and_hot_observations():
    samples = [ChargeSample(-12.0, 0.1, True, AT), ChargeSample(38.0, 1.0, True, AT)]
    root = _parse(derating_svg(_model(samples), -15.0))
    width = float(root.get("width"))
    for dot in root.findall(f".//{SVG}circle"):
        assert 0 <= float(dot.get("cx")) <= width


def test_the_markdown_image_carries_the_svg():
    markdown = derating_image_markdown(_learned())
    prefix = "![](data:image/svg+xml;base64,"
    assert markdown.startswith(prefix) and markdown.endswith(")")
    svg = base64.b64decode(markdown[len(prefix) : -1]).decode()
    assert svg == derating_svg(_learned())
    # No whitespace or bracket that would end the Markdown link early.
    assert not set(markdown[len(prefix) : -1]) & set(" \n()")


def test_no_learned_curve_is_drawn_when_the_temperature_explains_nothing():
    limited = [ChargeSample(t, 0.70, True, AT) for t in (8.0, 10.0, 11.0, 12.0, 12.5, 13.0)]
    model = _model([*limited, ChargeSample(9.0, 0.90, False, AT)])
    assert model.ranges()
    assert _curves(_parse(derating_svg(model)), LEARNED) == []
