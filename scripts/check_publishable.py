"""Publication gate: fail if anything in the *entire* git history must not be public.

The repository is public, so every push is a publication. This complements gitleaks (secrets)
with project rules gitleaks cannot know about:

- forbidden paths (env files, private notes, raw/cached third-party content) in any commit;
- author/committer identities that are not GitHub noreply addresses;
- email addresses other than allow-listed placeholder/noreply domains;
- North American phone numbers other than the fictional 555-01xx range.

Run: `uv run python scripts/check_publishable.py` (pre-push hook and CI step), or with
`--staged` for just the staged diff (pre-commit hook), which catches problems before they
become part of a commit.
"""

from __future__ import annotations

import re
import subprocess
import sys

FORBIDDEN_PATH = re.compile(
    r"(^|/)\.env($|\.(?!example$))"  # .env and .env.* except .env.example
    r"|(^|/)BRIEF\.md$|(^|/)PRIVATE_NOTES\.md$"
    r"|^data/(raw|cache|local)/"
    r"|^eval/recordings/real/|^eval/gold/extraction/real/pages/"
)
ALLOWED_EMAIL_DOMAINS = ("users.noreply.github.com", "anthropic.com", "example.com", "example.org")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
# NANP number with separators; the fictional range is 555-0100..555-0199.
PHONE = re.compile(r"(?<![\d.])\(?([2-9]\d{2})\)?[-. /]([2-9]\d{2})[-. /](\d{4})(?![\d.])")
# Paths whose content is generated or third-party tooling, not our data.
SKIP_CONTENT = re.compile(r"^uv\.lock$")


def git(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def added_lines(staged: bool = False) -> list[tuple[str, str]]:
    """(path, line) for every line ever added in any commit, or only in the staged diff."""
    out: list[tuple[str, str]] = []
    path = ""
    diff = (
        git("diff", "--cached", "--no-color")
        if staged
        else git("log", "--all", "-p", "--no-color", "--format=")
    )
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("+") and path and not SKIP_CONTENT.search(path):
            out.append((path, line[1:]))
    return out


def line_problems(path: str, line: str) -> list[str]:
    """Emails outside the allow-list and phone numbers outside the fictional 555-01xx range."""
    problems = [
        f"email in {path}: {m.group(0)}"
        for m in EMAIL.finditer(line)
        if not m.group(0).lower().startswith("noreply@")
        and not m.group(1).lower().endswith(ALLOWED_EMAIL_DOMAINS)
    ]
    problems += [
        f"phone number in {path}: {m.group(0)}"
        for m in PHONE.finditer(line)
        if not (m.group(2) == "555" and m.group(3).startswith("01"))
    ]
    return problems


def main() -> int:
    """Check the whole history, or with `--staged` only what is about to be committed."""
    staged = "--staged" in sys.argv[1:]
    problems: list[str] = []

    if staged:
        paths = set(git("diff", "--cached", "--name-only").split())
    else:
        paths = set(git("log", "--all", "--name-only", "--format=").split())
    problems += [
        f"forbidden path in history: {p}" for p in sorted(paths) if FORBIDDEN_PATH.search(p)
    ]

    identities = set() if staged else set(git("log", "--all", "--format=%ae%n%ce").split())
    for ident in identities:
        if not ident.endswith(("@users.noreply.github.com", "noreply@github.com")):
            problems.append(f"non-noreply author/committer email: {ident}")

    for path, line in added_lines(staged):
        problems += line_problems(path, line)

    if problems:
        print("NOT publishable:", *sorted(set(problems)), sep="\n  ")
        return 1
    scope = "staged" if staged else "in history"
    print(f"publishable: {len(paths)} paths {scope} checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
