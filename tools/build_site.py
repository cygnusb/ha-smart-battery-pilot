#!/usr/bin/env python3
"""Build the project page in every language the integration speaks.

site/index.html.j2 is the page, site/i18n/<lang>.json its text. English goes
to the root of the output directory, every other language to <lang>/. Each
page links its translations (hreflang) and shows the card screenshots in its
own language, from assets/screenshots/<lang>/ (tools/gen_card_screenshots.py).
A sitemap index at the root lists these pages and the one MkDocs writes for
/docs/. Search-engine verification files (site/google*.html) are copied to
the root as they are.

The texts are trusted HTML from this repository, so they are not escaped. A
key missing from a translation fails the build, so a new sentence on the page
cannot go out in English only.

Usage:
    python3 tools/build_site.py _site
"""

from __future__ import annotations

import argparse
from datetime import date
import json
import math
from pathlib import Path
import shutil
import sys

from jinja2 import Environment, FileSystemLoader, StrictUndefined, pass_context
from markupsafe import Markup

REPO = Path(__file__).resolve().parent.parent
SITE = REPO / "site"
ASSETS = REPO / "assets"
BASE_URL = "https://cygnusb.github.io/ha-smart-battery-pilot/"
DEFAULT = "en"

# Code, name in its own language, og:locale. The order is the menu's.
LANGUAGES = [
    ("en", "English", "en_GB"),
    ("de", "Deutsch", "de_DE"),
    ("nl", "Nederlands", "nl_NL"),
    ("da", "Dansk", "da_DK"),
    ("sv", "Svenska", "sv_SE"),
    ("nb", "Norsk bokmål", "nb_NO"),
    ("fi", "Suomi", "fi_FI"),
    ("et", "Eesti", "et_EE"),
    ("lv", "Latviešu", "lv_LV"),
    ("lt", "Lietuvių", "lt_LT"),
]


# The example winter day on the price board: night around 0.10 EUR/kWh, a
# morning peak near 0.45, about 0.20 through the day, an evening peak near
# 0.50. Per quarter hour, the price and what the pilot does: c charge,
# o battery covers, h held, a inverter on its own.
def _board_slot(q: int) -> tuple[float, str]:
    h = q / 4
    wobble = 0.006 * math.sin(q * 1.7) + 0.004 * math.cos(q * 0.9)
    if h < 2:
        return 0.135 - 0.012 * h + wobble, "a"
    if h < 6:
        return 0.100 + 0.004 * math.sin(q) + (0.012 if h >= 5.5 else 0), "c"
    if h < 9:
        x = (h - 6) / 3
        return 0.30 + 0.15 * math.sin(math.pi * min(1, x * 1.25)) ** 0.6 + wobble, "o"
    if h < 17:
        return 0.205 - 0.022 * math.sin(math.pi * (h - 9) / 8) + wobble, "h"
    if h < 21:
        x = (h - 17) / 4
        return 0.33 + 0.175 * math.sin(math.pi * x) ** 0.7 + wobble, "o"
    return 0.29 - 0.12 * (h - 21) / 3 + wobble, "a"


BARS = [{"p": round(price, 3), "k": k} for price, k in map(_board_slot, range(96))]

# The three seasons as 24 hours each, with p for PV filling the battery and
# e for export besides the board's actions.
STRIPS = [
    ("winter", "aaccccooohhhhhhhhooooaaa"),
    ("summer", "oooooooaapppppppaaeeeooo"),
    ("dunkelflaute", "hccccchooohhccchhoooohhh"),
]
assert all(len(hours) == 24 for _, hours in STRIPS)

# The card screenshots in the viewer, with each image's height in pixels.
SCREENSHOTS = [
    {"name": "dunkelflaute", "height": 942},
    {"name": "winter_arbitrage", "height": 942},
    {"name": "summer_export", "height": 942},
    {"name": "ev_overnight", "height": 986},
    {"name": "ev_compact", "height": 918},
    {"name": "view_balance", "height": 942},
    {"name": "view_compact", "height": 894},
]


def page_path(lang: str) -> str:
    """The page's path below the site root: '' for English, 'de/' for German."""
    return "" if lang == DEFAULT else f"{lang}/"


def load_texts() -> dict[str, dict]:
    texts = {
        code: json.loads((SITE / "i18n" / f"{code}.json").read_text(encoding="utf-8"))
        for code, _, _ in LANGUAGES
    }
    reference = _keys(texts[DEFAULT])
    problems = []
    for code, table in texts.items():
        keys = _keys(table)
        if missing := sorted(reference - keys):
            problems.append(f"{code}.json lacks {', '.join(missing)}")
        if extra := sorted(keys - reference):
            problems.append(f"{code}.json has keys en.json does not: {', '.join(extra)}")
    if problems:
        raise SystemExit("\n".join(problems))
    return {code: _markup(table) for code, table in texts.items()}


def _keys(table, prefix=""):
    keys = set()
    for key, value in table.items():
        if isinstance(value, dict):
            keys |= _keys(value, f"{prefix}{key}.")
        else:
            keys.add(f"{prefix}{key}")
    return keys


def _markup(value):
    if isinstance(value, dict):
        return {key: _markup(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_markup(item) for item in value]
    return Markup(value) if isinstance(value, str) else value


def build(out: Path) -> None:
    texts = load_texts()
    env = Environment(
        loader=FileSystemLoader(SITE),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )
    env.filters["price"] = _price
    template = env.get_template("index.html.j2")
    languages = [
        {
            "code": code,
            "name": name,
            "locale": locale,
            "url": BASE_URL + page_path(code),
            "path": page_path(code),
        }
        for code, name, locale in LANGUAGES
    ]
    for lang in languages:
        # Relative to the page: the English page sits at the root.
        root = "" if lang["code"] == DEFAULT else "../"
        shots = "assets/screenshots/" + ("" if lang["code"] == DEFAULT else f"{lang['code']}/")
        t = texts[lang["code"]]
        html = template.render(
            t=t,
            lang=lang,
            languages=languages,
            default_url=BASE_URL,
            root=root,
            shots=root + shots,
            og_image=BASE_URL + shots + "card_dunkelflaute_light.png",
            json_ld=_json_ld(t, lang),
            bars=BARS,
            strips=STRIPS,
            screenshots=SCREENSHOTS,
        )
        target = out / lang["path"] / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")

    _copy_assets(out)
    (out / "sitemap-pages.xml").write_text(_page_sitemap(languages), encoding="utf-8")
    (out / "sitemap.xml").write_text(_sitemap_index(), encoding="utf-8")
    for verification in SITE.glob("google*.html"):
        shutil.copy2(verification, out / verification.name)


@pass_context
def _price(context, value: float, digits: int = 2) -> str:
    """A figure as the page's language writes it: 0.10 in English, 0,10 in all the others."""
    text = f"{value:.{digits}f}"
    return text if context["lang"]["code"] == DEFAULT else text.replace(".", ",")


def _json_ld(t, lang) -> Markup:
    data = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": "Smart Battery Pilot",
        "description": str(t["meta"]["description"]),
        "url": lang["url"],
        "inLanguage": lang["code"],
        "applicationCategory": "UtilitiesApplication",
        "applicationSubCategory": "Home Assistant integration",
        "operatingSystem": "Home Assistant",
        "license": "https://opensource.org/licenses/MIT",
        "isAccessibleForFree": True,
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "EUR"},
        "codeRepository": "https://github.com/cygnusb/ha-smart-battery-pilot",
        "screenshot": BASE_URL + "assets/screenshots/card_dunkelflaute_light.png",
    }
    # </ cannot end the script element early.
    return Markup(json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))


def _copy_assets(out: Path) -> None:
    target = out / "assets"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("icon.svg", "logo.svg", "logo.png"):
        shutil.copy2(ASSETS / name, target / name)
    for folder in ("screenshots", "fonts"):
        shutil.copytree(ASSETS / folder, target / folder, dirs_exist_ok=True)


def _page_sitemap(languages) -> str:
    today = date.today().isoformat()
    links = (
        "".join(
            f'    <xhtml:link rel="alternate" hreflang="{lang["code"]}" href="{lang["url"]}"/>\n'
            for lang in languages
        )
        + f'    <xhtml:link rel="alternate" hreflang="x-default" href="{BASE_URL}"/>\n'
    )
    entries = "".join(
        f"  <url>\n    <loc>{lang['url']}</loc>\n    <lastmod>{today}</lastmod>\n{links}  </url>\n"
        for lang in languages
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
        ' xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
        f"{entries}</urlset>\n"
    )


def _sitemap_index() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <sitemap><loc>{BASE_URL}sitemap-pages.xml</loc></sitemap>\n"
        f"  <sitemap><loc>{BASE_URL}docs/sitemap.xml</loc></sitemap>\n"
        "</sitemapindex>\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="output directory, e.g. _site")
    build(parser.parse_args().out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
