"""The local report: one self-contained HTML file with the profile embedded."""

import json
from importlib import resources
from pathlib import Path
from typing import Any

PLACEHOLDER = "/*__FLAKYTEST_PROFILE__*/null"


def render(profile: dict[str, Any]) -> str:
    template = resources.files("flakytest.report").joinpath("template.html").read_text(encoding="utf-8")
    data = json.dumps(profile, separators=(",", ":")).replace("</", "<\\/").replace("<!--", "<\\!--")
    return template.replace(PLACEHOLDER, data)


def write_report(profile: dict[str, Any], run_dir: Path) -> Path:
    path = run_dir / "index.html"
    path.write_text(render(profile), encoding="utf-8")
    return path
