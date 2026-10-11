"""MkDocs hooks for the documentation pages of the project site.

The icon and the fonts live in assets/, shared by the README and the project
page. MkDocs only publishes files under docs/, so this hands them over from
there instead of keeping a second copy.

The changelog page is built from the GitHub releases, so the release notes
stay the one place a release is described. It needs network access; set
GITHUB_TOKEN to lift the API's rate limit for anonymous requests.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import urllib.request

from mkdocs.structure.files import File

ASSETS = Path(__file__).resolve().parent.parent / "assets"
REPO = "cygnusb/ha-smart-battery-pilot"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases?per_page=100&page={{}}"
RELEASES_URL = f"https://github.com/{REPO}/releases"

FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,5}) ")
LIST_ITEM = re.compile(r"^(?:[-*+]|\d+\.) ")
# Nested list items indented by 1-3 spaces, as GitHub accepts them.
SHALLOW_ITEM = re.compile(r"^ {1,3}(?=(?:[-*+]|\d+\.) )")


def on_files(files, config):
    files.append(File.generated(config, "assets/icon.svg", abs_src_path=str(ASSETS / "icon.svg")))
    for font in sorted((ASSETS / "fonts").iterdir()):
        files.append(File.generated(config, f"assets/fonts/{font.name}", abs_src_path=str(font)))
    files.append(File.generated(config, "changelog.md", content=_changelog(_fetch_releases())))
    return files


def _fetch_releases():
    headers = {"Accept": "application/vnd.github+json", "User-Agent": REPO}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    releases = []
    page = 1
    while True:
        request = urllib.request.Request(RELEASES_API.format(page), headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            batch = json.load(response)
        releases += batch
        if len(batch) < 100:
            return [r for r in releases if not r["draft"]]
        page += 1


def _changelog(releases):
    releases = sorted(releases, key=lambda r: r["published_at"], reverse=True)
    index = ["| Version | Released | Headline |", "|---|---|---|"]
    sections = []
    for release in releases:
        tag = release["tag_name"]
        anchor = re.sub(r"[^0-9a-z]+", "-", tag.lower()).strip("-")
        title = re.sub(rf"^{re.escape(tag)}\s*[\u2014\u2013-]?\s*", "", release["name"] or "")
        released = datetime.fromisoformat(release["published_at"]).date().isoformat()
        heading = f"{tag} — {title}" if title else tag
        index.append(f"| [{tag}](#{anchor}) | {released} | {title} |")
        sections.append(
            f"## {heading} {{ #{anchor} }}\n\n"
            f"*Released {released} · [on GitHub]({release['html_url']})*\n\n"
            f"{_body(release['body'] or '')}\n"
        )
    return (
        "---\nhide:\n  - toc\n---\n\n"
        "# Changelog\n\n"
        "Every release of Smart Battery Pilot, newest first. The same notes are on "
        f"[GitHub]({RELEASES_URL}), and Home Assistant shows them in the update "
        "dialog when HACS offers a new version.\n\n"
        + "\n".join(index)
        + "\n\n"
        + "\n".join(sections)
    )


def _body(text):
    """Fit release notes, written for GitHub, into one page of the site.

    Headings move one level down, under the release's own heading. Two things
    GitHub accepts and Python-Markdown does not are rewritten, or the lists
    would run into the paragraph before them: a list directly under a line of
    text gets a blank line before it, and nested items get a four-space indent.
    """
    lines = []
    in_fence = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            line = HEADING.sub(r"#\1 ", line)
            line = SHALLOW_ITEM.sub("    ", line)
            previous = lines[-1] if lines else ""
            if (
                LIST_ITEM.match(line)
                and previous[:1] not in ("", " ")
                and not LIST_ITEM.match(previous)
            ):
                lines.append("")
        lines.append(line)
    return "\n".join(lines).strip()
