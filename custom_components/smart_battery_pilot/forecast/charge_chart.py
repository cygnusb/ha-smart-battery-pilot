"""The cold-charge curves as a small SVG for the options dialog.

One picture answers what a table of ranges cannot: where the measurements
lie, how far the learned curve departs from the sliders, and where the
battery is right now. Pure string building, no Home Assistant imports and no
plotting library - the dialog embeds the result as an image.

No words in the picture: the dialog's translated description names the marks,
so the chart itself is the same in every language.
"""

from __future__ import annotations

import base64
import math

from .charge_rate import ChargeRateModel, curve_factor

WIDTH, HEIGHT = 480, 230
LEFT, RIGHT, TOP, BOTTOM = 44, 24, 12, 30
T_MIN, T_MAX = -5.0, 25.0
# One file for light and dark dialogs: an image cannot read the theme, so the
# two series colours and the ink are picked to hold on both surfaces.
LEARNED = "#2a78d6"
SLIDERS = "#c2691a"
INK = "#8a8a8a"


def _y(factor: float) -> float:
    return TOP + (1.0 - factor) * (HEIGHT - TOP - BOTTOM)


def derating_svg(model: ChargeRateModel, temperature: float | None = None) -> str:
    """Sliders, learned curve, observations and the current temperature."""
    # The default window, widened to hold every mark.
    seen = [s.temperature for s in model.samples]
    if temperature is not None:
        seen.append(temperature)
    t_min, t_max = min([T_MIN, *seen]), max([T_MAX, *seen])

    def x(at: float) -> float:
        return LEFT + (at - t_min) / (t_max - t_min) * (WIDTH - LEFT - RIGHT)

    def path(points: list[tuple[float, float]]) -> str:
        return " ".join(
            f"{'M' if i == 0 else 'L'}{x(t):.1f},{_y(f):.1f}" for i, (t, f) in enumerate(points)
        )

    steps = [t_min + (t_max - t_min) * i / 240 for i in range(241)]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" font-family="sans-serif" font-size="11">'
    ]
    for percent in (0, 25, 50, 75, 100):
        y = _y(percent / 100)
        parts.append(
            f'<line x1="{LEFT}" x2="{WIDTH - RIGHT}" y1="{y:.1f}" y2="{y:.1f}" '
            f'stroke="{INK}" stroke-width="0.5" opacity="0.5"/>'
            f'<text x="{LEFT - 6}" y="{y + 4:.1f}" text-anchor="end" fill="{INK}">'
            f"{percent} %</text>"
        )
    for tick in range(math.ceil(t_min / 5) * 5, math.floor(t_max) + 1, 5):
        parts.append(
            f'<text x="{x(tick):.1f}" y="{HEIGHT - 10}" text-anchor="middle" fill="{INK}">'
            f"{tick} °C</text>"
        )
    if temperature is not None:
        parts.append(
            f'<line x1="{x(temperature):.1f}" x2="{x(temperature):.1f}" y1="{TOP}" '
            f'y2="{HEIGHT - BOTTOM}" stroke="{INK}" stroke-width="1.5"/>'
        )
    parts.append(
        f'<path d="{path([(t, curve_factor(model.curve, t)) for t in steps])}" fill="none" '
        f'stroke="{SLIDERS}" stroke-width="2" stroke-dasharray="6 4"/>'
    )
    ranges = model.ranges()
    if ranges:
        # Bold where it was measured, thin where the sliders only lend the
        # shape: the picture should not claim more than the data holds.
        low, high = ranges[0].low, ranges[-1].high
        # A hair outside the edge reads the value beyond it, so a step
        # between sliders and fit is drawn as the vertical it is.
        edge = 1e-6
        pieces = (
            ([t for t in steps if t < low] + [low - edge, low], 1.25),
            ([low] + [t for t in steps if low < t < high] + [high], 3.0),
            ([high, high + edge] + [t for t in steps if t > high], 1.25),
        )
        for at, width in pieces:
            parts.append(
                f'<path d="{path([(t, model.measured_factor(t)) for t in at])}" fill="none" '
                f'stroke="{LEARNED}" stroke-width="{width}" stroke-linejoin="round" '
                'stroke-linecap="round"/>'
            )
    for sample in model.samples:
        fill = INK if sample.saturated else "none"
        parts.append(
            f'<circle cx="{x(sample.temperature):.1f}" '
            f'cy="{_y(max(0.0, min(1.0, sample.ratio))):.1f}" r="4" fill="{fill}" '
            f'stroke="{INK}" stroke-width="1.5"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def derating_image_markdown(model: ChargeRateModel, temperature: float | None = None) -> str:
    """The chart as a Markdown image with the SVG inlined as a data URI."""
    encoded = base64.b64encode(derating_svg(model, temperature).encode()).decode()
    return f"![](data:image/svg+xml;base64,{encoded})"
