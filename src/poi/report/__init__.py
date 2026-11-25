"""Markdown reports generated from the database into reports/."""

from __future__ import annotations

from poi.settings import PROJECT_ROOT

REPORTS = PROJECT_ROOT / "reports"


def write(name: str, content: str) -> str:
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / name
    path.write_text(content)
    return str(path.relative_to(PROJECT_ROOT))
