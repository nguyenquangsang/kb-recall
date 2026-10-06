"""Guard the shipped slash commands against the Slash Command Standard.

Reads the REAL kb_recall/commands/*.md rather than fixtures, for the same reason
tests/test_skills.py reads the real skills: a command that breaks the standard raises
no error anywhere. The first-line marker is the clearest case — it is where Claude Code
injects the user's argument, so a file that references `$ARGUMENTS` without the marker
silently receives nothing and the user's argument is dropped without a signal.

Deliberately NOT asserted, and why:

- **The `Report` step.** The standard requires one for write commands, but `init.md`'s
  "Step 3 — Reload KB and suggest next steps" *is* the report, and `link-feature.md`
  reports inside Step 3A. A heading-based assertion would fail those two correct files,
  and no honest content-based one exists, so the rule stays prose in CLAUDE.md.
- **The slug-detection boilerplate verbatim.** `load.md` deliberately diverges — it calls
  `load_feature_context` directly instead of `list_features` first, saving a round trip.
  Forcing it back to the template would make it worse, so only the *presence* and
  *position* of a slug step are asserted.
"""

import re
from pathlib import Path

import pytest

from kb_recall import cli

COMMANDS_DIR = cli.SCRIPT_DIR / "commands"

EXPECTED_COMMANDS = {
    "compact",
    "init",
    "link-feature",
    "list",
    "load",
    "miss",
    "save",
    "tidy",
}

# Commands whose Step 1 resolves a slug out of $ARGUMENTS. The other three correctly
# skip it: `init` creates a KB (it collects a name and slug instead), `link-feature`
# derives its target from the git branch, and `list` targets no KB at all.
SLUG_DETECTING = {"compact", "load", "miss", "save", "tidy"}

SLUG_STEP = "## Step 1 — Determine slug"
ARGUMENTS_MARKER = "Argument (optional): **$ARGUMENTS**"


def _command_paths():
    return sorted(COMMANDS_DIR.glob("*.md"))


def test_ships_exactly_the_eight_commands():
    assert {p.stem for p in _command_paths()} == EXPECTED_COMMANDS


def test_every_command_has_a_skill_sibling():
    """The reverse of test_skills.py::test_skill_names_have_a_command_sibling.

    That test catches a skill whose command went missing; this one catches a command
    added without its skill. Until both directions existed, adding commands/foo.md
    alone passed the entire suite — and a Copilot-only user would silently never get
    that workflow, since skills are the only surface Copilot reads.
    """
    skills = {p.parent.name for p in (cli.SCRIPT_DIR / "skills").glob("*/SKILL.md")}
    for path in _command_paths():
        expected = f"recall-{path.stem}"
        assert expected in skills, (
            f"commands/{path.name} has no skills/{expected}/SKILL.md"
        )


@pytest.mark.parametrize("path", _command_paths(), ids=lambda p: p.stem)
def test_first_line_carries_the_arguments_marker(path: Path):
    first = path.read_text().splitlines()[0].rstrip()
    assert first.endswith(ARGUMENTS_MARKER), (
        f"{path.name}: first line must end with {ARGUMENTS_MARKER!r} — got ...{first[-50:]!r}"
    )


@pytest.mark.parametrize("path", _command_paths(), ids=lambda p: p.stem)
def test_has_when_to_use_with_a_never_boundary(path: Path):
    text = path.read_text()
    assert re.search(r"^## When to use$", text, re.MULTILINE), (
        f"{path.name}: missing a `## When to use` heading"
    )
    assert re.search(r"Never\s", text), f"{path.name}: missing the `Never...` boundary"


@pytest.mark.parametrize("name", sorted(SLUG_DETECTING))
def test_slug_detecting_commands_have_a_slug_step(name: str):
    text = (COMMANDS_DIR / f"{name}.md").read_text()
    assert SLUG_STEP in text, f"{name}.md: missing `{SLUG_STEP}`"
    assert text.index(SLUG_STEP) < text.index("## Step 2"), (
        f"{name}.md: the slug step must come before Step 2"
    )
