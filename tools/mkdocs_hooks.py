"""MkDocs hooks for the documentation pages of the project site.

The icon lives in assets/, shared by the README and the project page. MkDocs
only publishes files under docs/, so this hands it the icon from there instead
of keeping a second copy.
"""

from pathlib import Path

from mkdocs.structure.files import File

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def on_files(files, config):
    files.append(File.generated(config, "assets/icon.svg", abs_src_path=str(ASSETS / "icon.svg")))
    return files
