"""The local report: index.html, which loads the profile from profile.js next to it.

A <script src> works when the report is opened straight from disk (file://), unlike fetching profile.json, and
the browser parses the data as it loads instead of a second copy of it embedded in the page.
"""

from importlib import resources
from pathlib import Path

DATA_FILE = "profile.js"
DATA_PREFIX = "window.FLAKYTEST_PROFILE = "


def write_report(profile_json: str, run_dir: Path) -> Path:
    """profile_json: the profile document, serialised once for profile.json and reused here."""
    template = resources.files("flakytest.report").joinpath("template.html").read_text(encoding="utf-8")
    with (run_dir / DATA_FILE).open("w", encoding="utf-8") as f:
        f.write(DATA_PREFIX)
        f.write(profile_json)
        f.write(";\n")
    path = run_dir / "index.html"
    path.write_text(template, encoding="utf-8")
    return path
