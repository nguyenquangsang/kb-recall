"""Guard the shipped Agent Skills against the fail-silent traps of the format.

These read the REAL kb_recall/skills/*/SKILL.md (not fixtures) because a wrong
`name` or an invalid frontmatter is exactly the failure that loads nothing and
raises no error — a test is the only signal that catches it. This mirrors the
T2f matcher test: the thing that fails silently gets a compile-time assert.
"""

import re
from pathlib import Path

import pytest

from kb_recall import cli

SKILLS_DIR = cli.SCRIPT_DIR / "skills"

# Full parity: all 8 commands/*.md are ported to Agent Skills. A Copilot-only user
# gets every workflow, so a skill missing from this set means a port slipped.
EXPECTED_SKILLS = {
    "recall-load",
    "recall-save",
    "recall-list",
    "recall-init",
    "recall-link-feature",
    "recall-miss",
    "recall-compact",
    "recall-tidy",
}

_NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)
_DESC_RE = re.compile(r"^description:\s*(\S.*)$", re.MULTILINE)


def _skill_paths():
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def _frontmatter(text: str) -> str:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    return m.group(1) if m else ""


def test_ships_exactly_the_eight_copilot_skills():
    names = {p.parent.name for p in _skill_paths()}
    assert names == EXPECTED_SKILLS


@pytest.mark.parametrize("path", _skill_paths(), ids=lambda p: p.parent.name)
def test_name_matches_directory_and_is_valid(path: Path):
    """The fail-silent trap: name != directory, or an invalid char, loads
    nothing with no error. Also enforced: lowercase+digits+hyphen, <=64."""
    text = path.read_text(encoding="utf-8")
    m = _NAME_RE.search(_frontmatter(text))
    assert m, f"{path}: frontmatter has no `name:` field"
    name = m.group(1)
    assert name == path.parent.name, (
        f"{path}: name {name!r} != dir {path.parent.name!r}"
    )
    assert re.fullmatch(r"[a-z0-9-]{1,64}", name), f"{path}: name {name!r} invalid"


@pytest.mark.parametrize("path", _skill_paths(), ids=lambda p: p.parent.name)
def test_description_is_present_and_bounded(path: Path):
    m = _DESC_RE.search(_frontmatter(path.read_text(encoding="utf-8")))
    assert m, f"{path}: frontmatter has no `description:` field"
    assert 1 <= len(m.group(1)) <= 1024


@pytest.mark.parametrize("path", _skill_paths(), ids=lambda p: p.parent.name)
def test_when_to_use_has_a_never_boundary(path: Path):
    text = path.read_text(encoding="utf-8")
    assert "## When to use" in text, f"{path}: missing `## When to use`"
    assert re.search(r"Never\s", text), f"{path}: missing the `Never...` boundary"


def test_skill_names_have_a_command_sibling():
    """Each recall-<x> skill must port a commands/<x>.md of the same name."""
    cmds = {p.stem for p in (cli.SCRIPT_DIR / "commands").glob("*.md")}
    for path in _skill_paths():
        sibling = path.parent.name.removeprefix("recall-")
        assert sibling in cmds, f"{path.parent.name} has no commands/{sibling}.md"
