"""The publication gate must catch what it claims to catch."""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "check_publishable", Path(__file__).parents[2] / "scripts" / "check_publishable.py"
)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        "BRIEF.md",
        "PRIVATE_NOTES.md",
        "data/raw/x.gz",
        "data/cache/y",
        "eval/recordings/real/a.json",
    ],
)
def test_forbidden_paths(path: str) -> None:
    assert gate.FORBIDDEN_PATH.search(path)


@pytest.mark.parametrize("path", [".env.example", "data/demo/records.jsonl", "docs/PLAN.md"])
def test_allowed_paths(path: str) -> None:
    assert not gate.FORBIDDEN_PATH.search(path)


def test_flags_real_contact_data() -> None:
    # Assembled at runtime so this file itself never contains a matching literal.
    phone = "(209) " + "-".join(["234", "5678"])
    email = "someone" + "@" + "agency.gov"
    assert len(gate.line_problems("f", f"call {phone} or mail {email}")) == 2


def test_allows_placeholders_and_code() -> None:
    assert gate.line_problems("f", "phone 760-555-0142, info@example.com") == []
    assert gate.line_problems("f", "@app.command()  # 2026-11-24") == []
    assert gate.line_problems("f", "committer " + "noreply" + "@github.com") == []
