"""The board wiring: what turns an installed harness into a working board client.

#230. `homeManagerModules.quarterback-harness` said it wired the harness into `~/.claude`
and installed three things: the package, `~/.claude/loops`, and the command files. Every
mechanism that makes the harness a board *client* — the seven Claude Code lifecycle hooks,
the MCP registration, and `qb-hook` itself — lived in whatever personal config the consumer
happened to keep. So the module's own description was false, and worse: the hook script was
pinned by a different repo than the board it posts to, which is version skew no check could
see, because the file was not in the tree being checked.

This suite covers the mechanism that replaced it, in the order a failure would bite:

1. **The coupling.** Every event `qb-hook` handles must be an event the fragment wires, and
   vice versa. This is the bug that shipped: the fleet's own `qb-claude-setup` wired three
   of seven, so a host that ran it got no ask courier, no publish-on-push, no sync advice
   and no sub-agent records — and nothing said so, because the other four entries happened
   to exist in a settings.json the consumer maintained by hand.
2. **The merge.** Additive by identity, idempotent, and it must not double an entry —
   `PostToolUse` matches `*`, so a doubled entry is the hot path run twice per tool call.
3. **The composition.** Two writers into one file that home-manager cannot own.
4. **The nix.** A mechanism that ships unwired is #169's failure, and the module is where
   this one would ship unwired.

Run: pytest harness/tests
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# `_flake_sandbox` is a sibling module, imported by bare name the way #264's own members
# import it — the sandboxes that run these suites put `harness/tests` on the path by running
# pytest from `harness/`, and a developer running `pytest harness/tests` from the repo root
# does not. One entry, so both work.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Imported hard, not through `importorskip`. `worktree-tests` allowlists no skips — it has
# legitimate ones — so a skip here would be indistinguishable from a pass, and the guards
# below would be inert in exactly the sandbox they exist to protect. If this module is
# missing, that is a broken sandbox and it should say so.
import _flake_sandbox  # noqa: E402

HARNESS = Path(__file__).resolve().parent.parent
REPO_ROOT = HARNESS.parent
BIN = HARNESS / "bin"
SETUP = BIN / "qb-claude-setup"
HOOK = BIN / "qb-hook"
FRAGMENT = HARNESS / "claude" / "settings-fragment.json"
HM_MODULE = HARNESS / "hm-module.nix"
PACKAGE_NIX = HARNESS / "package.nix"
FLAKE = REPO_ROOT / "flake.nix"

#: The placeholder the fragment carries instead of a path. A fragment inside the package
#: cannot name the package's own store path, so the substitution happens at wiring time.
PLACEHOLDER = "@QB_HOOK@"

jq_required = pytest.mark.skipif(
    subprocess.run(["sh", "-c", "command -v jq"], capture_output=True).returncode != 0,
    reason="jq is not on PATH — qb-claude-setup does every merge with it",
)


# --------------------------------------------------------------- the coupling


def _fragment_events() -> list[str]:
    return list(json.loads(FRAGMENT.read_text(encoding="utf-8"))["hooks"].keys())


def _hook_events() -> set[str]:
    """The event names `qb-hook`'s dispatch switch actually handles.

    Read out of the source rather than by running the script, because the question is what
    the file CAN handle, and the answer for six of the seven is "nothing observable without
    a board to post to". The dispatch `case` is the one at column zero with nothing after
    `in`; the file has other `case` statements (a symlink walk, and a three-event guard
    written inline) and matching those would answer a different question.
    """
    text = HOOK.read_text(encoding="utf-8")
    start = re.search(r'^case "\$EVENT" in\s*$', text, re.MULTILINE)
    assert start, "qb-hook no longer has a dispatch `case \"$EVENT\" in` at column zero"
    end = re.search(r"^esac", text[start.end():], re.MULTILINE)
    assert end, "qb-hook's dispatch case is not terminated — the parse below is meaningless"
    body = text[start.end():start.end() + end.start()]
    return set(re.findall(r"^  ([A-Za-z]+)\)", body, re.MULTILINE))


def test_the_two_event_lists_are_not_empty():
    """A guard on the guard: an empty set makes every assertion below pass vacuously, which
    is the shape of the bug this file exists to catch."""
    assert len(_fragment_events()) == 7, _fragment_events()
    assert len(_hook_events()) == 7, _hook_events()


@pytest.mark.parametrize("event", sorted(_hook_events()))
def test_every_event_the_hook_handles_is_wired_by_the_fragment(event: str):
    """The direction that shipped. An arm with no settings entry is dead code that reads
    like a feature: `PostToolUse` carries the ask courier and publish-on-push, and the
    fleet's own wiring script never wired it — those two mechanisms simply did not exist
    on a host that had only run the script."""
    assert event in _fragment_events(), (
        f"qb-hook handles {event} but settings-fragment.json does not wire it, so Claude "
        f"Code never calls it and every mechanism on that arm is silently absent")


@pytest.mark.parametrize("event", _fragment_events())
def test_every_wired_event_is_an_event_the_hook_handles(event: str):
    """The other direction, cheaper but not free: an entry for an event with no arm spawns
    a bash process per occurrence to fall through a `case` and exit 0. On `PostToolUse`
    that is once per tool call."""
    assert event in _hook_events(), (
        f"settings-fragment.json wires {event} but qb-hook has no arm for it")


def test_the_fragment_names_no_path_of_its_own():
    """It ships INSIDE the package whose path it has to name, so the path cannot be in the
    file. A fragment that hardcoded one would wire every consumer to whichever machine's
    layout the file was written on — which is what `/home/rich/.local/bin/qb-hook`,
    committed in a fleet settings.json, actually was."""
    text = FRAGMENT.read_text(encoding="utf-8")
    assert PLACEHOLDER in text
    assert "/nix/store" not in text
    assert "/home/" not in text


def test_the_fragment_wires_post_tool_use_for_every_tool():
    """The courier has to reach an agent BETWEEN prompts, which is only possible if the
    hook fires on tools that are not Task and not Bash. A narrowed matcher here would look
    like a performance win and would silently take the mid-turn channel away."""
    frag = json.loads(FRAGMENT.read_text(encoding="utf-8"))
    assert [e.get("matcher") for e in frag["hooks"]["PostToolUse"]] == ["*"]


# ------------------------------------------------------------------ the merge


@pytest.fixture
def home(tmp_path):
    """An isolated $HOME, plus a runner for qb-claude-setup against it."""
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)

    def run(*args, **env_extra):
        env = {
            **os.environ,
            "HOME": str(h),
            "XDG_CONFIG_HOME": str(h / ".config"),
            # No site config in these tests unless one is written: the MCP branch reads
            # QUARTERBACK_REPO from it, and inheriting the developer's would make the
            # `auto` cases depend on whether this machine happens to have a venv.
            "QUARTERBACK_CONFIG": str(h / ".config" / "quarterback" / "config"),
            **env_extra,
        }
        env.pop("QUARTERBACK_REPO", None)
        return subprocess.run(
            [str(SETUP), *args], env=env, capture_output=True, text=True)

    run.home = h
    run.settings = h / ".claude" / "settings.json"
    run.claude_json = h / ".claude.json"
    run.claude_md = h / ".claude" / "CLAUDE.md"
    return run


def commands(settings: Path, event: str) -> list[str]:
    """Every hook command wired for one event, in file order."""
    data = json.loads(settings.read_text(encoding="utf-8"))
    return [h.get("command", "")
            for entry in data.get("hooks", {}).get(event, [])
            # Entries this script does not understand are kept verbatim rather than
            # dropped, so the reader has to survive one.
            if isinstance(entry, dict)
            for h in entry.get("hooks", [])]


@jq_required
def test_print_fragment_resolves_the_placeholder(home):
    """The documented manual route (`--print-fragment` into your own settings.json) and the
    wired route come out of one expression, so they cannot disagree. A placeholder left in
    the output would be a settings.json wiring a command named `@QB_HOOK@`."""
    result = home("--print-fragment")
    assert result.returncode == 0, result.stderr
    assert PLACEHOLDER not in result.stdout
    frag = json.loads(result.stdout)
    for event, entries in frag["hooks"].items():
        for entry in entries:
            for hook in entry["hooks"]:
                assert hook["command"] == f"{HOOK} {event}"


@jq_required
def test_wiring_creates_a_settings_file_that_did_not_exist(home):
    """A fresh host. The fleet's version only ever edited an existing file, so the very
    first switch on a new machine wired nothing at all."""
    home.settings.unlink(missing_ok=True)
    assert home("--mcp", "never").returncode == 0
    assert sorted(json.loads(home.settings.read_text())["hooks"]) == sorted(_fragment_events())


@jq_required
def test_wiring_keeps_a_foreign_hook_in_the_same_event(home):
    """The composition requirement, at the level of one array. A consumer's Bash guards
    share `PreToolUse` with quarterback's Task entry; a merge that replaced the array would
    disable their command guard as a side effect of installing a coordination board."""
    home.settings.write_text(json.dumps({
        "model": "opus",
        "hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-guard.sh"}]}]},
    }))
    assert home("--mcp", "never").returncode == 0
    assert commands(home.settings, "PreToolUse") == ["my-guard.sh", f"{HOOK} PreToolUse"]
    assert json.loads(home.settings.read_text())["model"] == "opus", (
        "an unrelated setting was lost — the merge must be surgical, not a rewrite")


@jq_required
def test_a_legacy_hook_entry_is_replaced_and_not_doubled(home):
    """The migration case, and the one with a cost attached. Every host that carried the
    hand-rolled wiring has entries naming `~/.local/bin/qb-hook`; matching on the command
    naming qb-hook (rather than on an exact path) is what turns those into a replacement.
    Two `PostToolUse` entries would run the hot path twice per tool call and poll the ask
    courier twice per window."""
    home.settings.write_text(json.dumps({"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [
            {"type": "command", "command": "/home/someone/.local/bin/qb-hook PostToolUse"}]}]}}))
    assert home("--mcp", "never").returncode == 0
    assert commands(home.settings, "PostToolUse") == [f"{HOOK} PostToolUse"]


@jq_required
def test_a_legacy_entry_whose_path_is_quoted_is_still_replaced(home):
    """The other side of matching loosely. `"$HOME/.local/bin/qb-hook" Stop` is a legitimate
    spelling of the same entry, and a pattern anchored on a trailing space would leave it in
    place and append ours beside it — the doubling the loose match exists to prevent."""
    home.settings.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": '"/home/someone/.local/bin/qb-hook" Stop'}]}]}}))
    assert home("--mcp", "never").returncode == 0
    assert commands(home.settings, "Stop") == [f"{HOOK} Stop"]


@jq_required
def test_a_foreign_hook_that_merely_mentions_qb_hook_is_not_claimed(home):
    """Loose about the path, strict about the word. A bare `test("qb-hook")` claims
    `my-qb-hooks.sh` — someone else's guard, deleted as a side effect of installing a
    coordination board, which is a worse failure than either doubling or missing."""
    home.settings.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "my-qb-hooks.sh --all"}]}]}}))
    assert home("--mcp", "never").returncode == 0
    assert commands(home.settings, "Stop") == ["my-qb-hooks.sh --all", f"{HOOK} Stop"]


@jq_required
def test_the_wired_path_does_not_depend_on_how_the_script_was_invoked(tmp_path, home):
    """The activation invokes this by store path while a human types it off PATH, where the
    sibling walk lands in `~/.nix-profile/bin`. Two spellings of one file, written by
    whichever ran last — and `--check` calling the difference SKEW, on a host that is
    perfectly wired. Both invocations have to produce the same command string."""
    link_dir = tmp_path / "profile-bin"
    link_dir.mkdir()
    for name in ("qb-claude-setup", "qb-hook", "qb-mcp", "qb-env"):
        (link_dir / name).symlink_to(BIN / name)
    result = subprocess.run(
        [str(link_dir / "qb-claude-setup"), "--mcp", "never"],
        env={**os.environ, "HOME": str(home.home),
             "QUARTERBACK_CONFIG": str(home.home / "no-config")},
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert commands(home.settings, "Stop") == [f"{HOOK} Stop"]
    # ...and the invocation that did NOT write it agrees that it is wired.
    assert home("--check").returncode == 0


@jq_required
def test_wiring_twice_changes_nothing_and_says_nothing(home):
    """It runs on every home-manager switch. A line of output per rebuild is how activation
    output stops being read, and a file rewritten every time is a mtime nobody can trust."""
    assert home("--mcp", "never").returncode == 0
    first = home.settings.read_bytes()
    second = home("--mcp", "never")
    assert second.returncode == 0
    assert second.stdout == ""
    assert home.settings.read_bytes() == first


@jq_required
def test_settings_that_are_not_json_are_left_alone(home):
    """Hand-maintained file, several writers. Guessing at a half-written one could destroy
    a user's whole configuration; refusing costs them a board until they fix it."""
    home.settings.write_text("{ this is not json")
    result = home("--mcp", "never")
    assert result.returncode == 0, "wiring must never fail an activation"
    assert home.settings.read_text() == "{ this is not json"
    assert "not valid JSON" in result.stderr


# ------------------------------------------------------------------ --check


@jq_required
def test_check_reports_every_event_wired_to_this_install(home):
    assert home("--mcp", "never").returncode == 0
    result = home("--check")
    assert result.returncode == 0, result.stdout
    # Line-anchored: the paths in the output end in `qb-hook <Event>`, and a bare
    # substring count finds "ok " inside "hook " on every one of them.
    assert [ln.split()[0] for ln in result.stdout.splitlines()] == ["ok"] * 7


@jq_required
def test_check_names_the_event_that_is_missing(home):
    """Per event, because that is the resolution the failure has: the bug being guarded
    against is six of seven wired, which a single yes/no would report as 'wired'."""
    assert home("--mcp", "never").returncode == 0
    data = json.loads(home.settings.read_text())
    del data["hooks"]["PostToolUse"]
    home.settings.write_text(json.dumps(data))
    result = home("--check")
    assert result.returncode == 1
    assert "MISSING" in result.stdout
    assert "PostToolUse" in result.stdout


@jq_required
def test_check_reports_skew_distinctly_from_absence(home):
    """Exit 2, and it is not a lesser 1. 'Wired to another pin' is the state this whole
    change exists to end, and it is the state a host is in for the whole of its migration
    — a doctor that called it 'missing' would be wrong, and one that called it fine would
    be useless."""
    home.settings.write_text(json.dumps({"hooks": {
        e: [{"hooks": [{"type": "command", "command": f"/elsewhere/qb-hook {e}"}]}]
        for e in _fragment_events()}}))
    result = home("--check")
    assert result.returncode == 2
    assert "SKEW" in result.stdout
    assert "/elsewhere/qb-hook" in result.stdout


def test_check_on_a_host_with_no_settings_file_is_a_failure_not_a_pass(home):
    """An absent file is the maximum amount of not-wired there is. Reporting it as clean is
    how a doctor blesses a host with no board."""
    home.settings.unlink(missing_ok=True)
    assert home("--check").returncode == 1


# ----------------------------------------------------------- the composition


CONSUMER_MERGE = (
    # The usual spelling of "declare part of settings.json from nix": canonical wins,
    # live extras are preserved. `*` replaces ARRAYS wholesale, which is the hazard.
    'jq -s ".[0] * .[1]" "$1" "$2"'
)


def _consumer_switch(settings: Path, canonical: dict) -> None:
    canonical_path = settings.parent / "canonical.json"
    canonical_path.write_text(json.dumps(canonical))
    # A first switch on a fresh host has nothing to merge INTO. The real activation
    # installs the canonical file outright in that case; an empty object is the same
    # thing to `*` and keeps this helper to one code path.
    if not settings.exists():
        settings.write_text("{}")
    out = subprocess.run(
        ["sh", "-c", CONSUMER_MERGE, "sh", str(settings), str(canonical_path)],
        capture_output=True, text=True, check=True)
    settings.write_text(out.stdout)


@jq_required
def test_the_consumer_merging_first_converges(home):
    """The order the module documents, and the one that works: their canonical file lands,
    then this wiring adds quarterback's entries to the arrays it finds."""
    canonical = {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-guard.sh"}]}]}}
    _consumer_switch(home.settings, canonical)
    assert home("--mcp", "never").returncode == 0
    assert home("--check").returncode == 0
    assert commands(home.settings, "PreToolUse") == ["my-guard.sh", f"{HOOK} PreToolUse"]


@jq_required
def test_the_consumer_merging_last_drops_the_array_it_declares(home):
    """The hazard, pinned as a test rather than left as a warning in a docstring.

    `*` replaces arrays, so a canonical file that declares `hooks.PreToolUse` overwrites
    the entry this wiring just added to it — and ONLY that array; the six events the
    canonical file says nothing about survive. That is the failure worth naming: not "the
    board stopped working" but "one event went quiet", which is the shape nobody notices.

    Nothing in a jq expression can fix this from our side, which is exactly why
    `claude.activationAfter` exists and why `--check` reports per event. If this test ever
    goes green on its own, home-manager's ordering changed underneath a consumer who was
    relying on it, and the option is still the answer."""
    assert home("--mcp", "never").returncode == 0
    _consumer_switch(home.settings, {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-guard.sh"}]}]}})
    assert commands(home.settings, "PreToolUse") == ["my-guard.sh"]
    result = home("--check")
    assert result.returncode == 1
    assert "PreToolUse" in result.stdout
    # ...and the repair is one command, with no rebuild: that is what makes the check
    # actionable rather than merely accurate.
    assert home("--mcp", "never").returncode == 0
    assert home("--check").returncode == 0


# ------------------------------------------------------- CLAUDE.md and MCP


@jq_required
def test_the_workflow_doc_import_is_added_once(home):
    home.claude_md.write_text("# my notes\n")
    (home.home / ".claude" / "quarterback-workflow.md").write_text("board norms\n")
    assert home("--mcp", "never").returncode == 0
    assert home.claude_md.read_text().count("@quarterback-workflow.md") == 1
    assert home("--mcp", "never").returncode == 0
    assert home.claude_md.read_text().count("@quarterback-workflow.md") == 1


@jq_required
def test_the_import_is_added_on_a_host_with_no_claude_md_at_all(home):
    """The third instance of the bug Codex found twice. Wiring only ever EDITED
    `~/.claude/CLAUDE.md`, so on a fresh host — the acceptance criterion's own case — the
    module installed `quarterback-workflow.md` and nothing ever imported it. Nothing else
    creates that file, so the gate never opens on its own: the doc would sit unread for
    the life of the machine, silently, exactly as the settings.json and `~/.claude.json`
    branches used to."""
    assert not home.claude_md.exists()
    (home.home / ".claude" / "quarterback-workflow.md").write_text("board norms\n")
    assert home("--mcp", "never").returncode == 0
    assert home.claude_md.read_text() == "@quarterback-workflow.md\n", (
        "a file created from nothing must not open with a blank line")
    # ...and it is still added exactly once on the next switch.
    assert home("--mcp", "never").returncode == 0
    assert home.claude_md.read_text().count("@quarterback-workflow.md") == 1


@jq_required
def test_no_import_is_added_for_a_doc_that_is_not_installed(home):
    """The fleet's copy appended the @import unconditionally. An @import of a missing file
    is a line in every session's context that resolves to nothing, on every host that
    turned the doc off."""
    home.claude_md.write_text("# my notes\n")
    assert home("--mcp", "never").returncode == 0
    assert "quarterback-workflow" not in home.claude_md.read_text()


@jq_required
def test_mcp_never_leaves_claude_json_untouched(home):
    home.claude_json.write_text(json.dumps({"mcpServers": {}}))
    assert home("--mcp", "never").returncode == 0
    assert json.loads(home.claude_json.read_text()) == {"mcpServers": {}}


@jq_required
def test_mcp_always_registers_the_shim_by_absolute_path(home):
    """~/.claude.json is Claude Code's own state file: it is not on PATH and cannot be, so
    the command has to be the resolved path of the shim in this install."""
    home.claude_json.write_text(json.dumps({"mcpServers": {"other": {"type": "stdio"}}}))
    assert home("--mcp", "always").returncode == 0
    servers = json.loads(home.claude_json.read_text())["mcpServers"]
    assert servers["quarterback"]["command"] == str(BIN / "qb-mcp")
    assert "other" in servers, "an unrelated MCP server was dropped"


@jq_required
def test_mcp_auto_declines_a_shim_that_could_not_start_and_says_what_is_missing(home):
    """qb-mcp execs a python out of a CHECKOUT's venv. Registering it anyway means every
    session opens on a failed MCP connection, so `auto` declines — and then has to say
    which path it wanted, or the user is left with a board that has no tools and no
    explanation."""
    cfg = home.home / ".config" / "quarterback" / "config"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f'QUARTERBACK_REPO="{home.home}/checkout"\n')
    home.claude_json.write_text(json.dumps({"mcpServers": {}}))
    result = home("--mcp", "auto")
    assert result.returncode == 0
    assert json.loads(home.claude_json.read_text()) == {"mcpServers": {}}
    assert "mcp/.venv/bin/python" in result.stderr
    assert "--mcp always" in result.stderr


@jq_required
def test_mcp_auto_registers_once_the_venv_exists(home):
    """Self-healing on the next switch is the other half of declining: a consumer who
    builds the venv must not have to know that a second rebuild is required."""
    venv = home.home / "checkout" / "mcp" / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("#!/bin/sh\n")
    (venv / "python").chmod(0o755)
    cfg = home.home / ".config" / "quarterback" / "config"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f'QUARTERBACK_REPO="{home.home}/checkout"\n')
    home.claude_json.write_text(json.dumps({"mcpServers": {}}))
    assert home("--mcp", "auto").returncode == 0
    assert json.loads(home.claude_json.read_text())["mcpServers"]["quarterback"]["command"] \
        == str(BIN / "qb-mcp")


@jq_required
def test_mcp_registers_on_a_host_where_claude_json_does_not_exist_yet(home):
    """230-C01 (Codex). It was gated on the file existing, which meant a host got the MCP
    server only if Claude Code had already run there once and written its state file — so
    the first switch on a fresh machine registered nothing, `board_read` was unavailable,
    and the activation said nothing about it. The same bug the settings.json branch had, and
    it defeats the acceptance criterion outright: a fresh host is exactly the case."""
    assert not home.claude_json.exists()
    assert home("--mcp", "always").returncode == 0
    servers = json.loads(home.claude_json.read_text())["mcpServers"]
    assert servers["quarterback"]["command"] == str(BIN / "qb-mcp")
    assert home.claude_json.stat().st_mode & 0o777 == 0o600


@jq_required
def test_claude_json_that_is_not_json_is_left_alone(home):
    """The other half of creating it: a file that exists and cannot be parsed is somebody's
    Claude Code state, and guessing at it costs them more than a missing MCP server does."""
    home.claude_json.write_text("{ half written")
    assert home("--mcp", "always").returncode == 0
    assert home.claude_json.read_text() == "{ half written"


@jq_required
def test_check_refuses_to_bless_a_settings_file_whose_hooks_are_the_wrong_shape(home):
    """230-C02 (Codex). `"hooks": "bad"` is valid JSON, so the parse check passes it, and the
    jq that reads the entries out then errors. With the rows read straight into the loop that
    error was unrecoverable: no lines, every counter zero, exit 0 — a doctor reporting a
    clean bill on a host with no board, which is the one answer this mode must never give."""
    home.settings.write_text(json.dumps({"hooks": "bad"}))
    result = home("--check")
    assert result.returncode == 1
    assert "not the shape Claude Code writes" in result.stdout


@jq_required
def test_check_takes_the_expected_command_from_the_fragment_not_from_a_guess(tmp_path, home):
    """`--check` used to compare the live entry against a `"$HOOK $event"` it rebuilt
    itself, which hardcodes this script's belief about the fragment's shape. The day an
    entry carries anything else — a flag, a different argument — every correctly wired host
    in the fleet reports SKEW, and the report is a string the doctor made up rather than
    one either file contains. Both halves have to come from the fragment."""
    frag = json.loads(FRAGMENT.read_text(encoding="utf-8"))
    frag["hooks"]["Stop"][0]["hooks"][0]["command"] = f"{PLACEHOLDER} Stop --quiet"
    custom = tmp_path / "frag.json"
    custom.write_text(json.dumps(frag))
    assert home("--fragment", str(custom), "--mcp", "never").returncode == 0
    assert commands(home.settings, "Stop") == [f"{HOOK} Stop --quiet"]
    result = home("--fragment", str(custom), "--check")
    assert result.returncode == 0, result.stdout
    assert [ln.split()[0] for ln in result.stdout.splitlines()] == ["ok"] * 7


@jq_required
def test_the_count_it_reports_comes_from_the_fragment(tmp_path, home):
    """"wired 7 hook events" was a literal 7 beside a list held in a data file. The first
    time the two disagree the activation reports a number that is simply not what it did."""
    frag = json.loads(FRAGMENT.read_text(encoding="utf-8"))
    del frag["hooks"]["Notification"]
    custom = tmp_path / "frag.json"
    custom.write_text(json.dumps(frag))
    result = home("--fragment", str(custom), "--mcp", "never")
    assert result.returncode == 0, result.stderr
    assert "wired 6 hook events" in result.stdout


@jq_required
def test_one_malformed_entry_does_not_cost_the_whole_wiring(home):
    """jq's `?` binds to the iteration, not to the index, so `.hooks[]?` on an entry that
    is not an object still raises — and one piece of junk in one array used to abort the
    merge for all seven events. A host left with no board because somebody else's config
    had a stray string in it is a worse outcome than skipping the string."""
    home.settings.write_text(json.dumps({"hooks": {
        "Stop": ["junk", {"hooks": [{"type": "command", "command": "my-guard.sh"}]}]}}))
    result = home("--mcp", "never")
    assert result.returncode == 0, result.stderr
    assert commands(home.settings, "Stop") == ["my-guard.sh", f"{HOOK} Stop"]
    assert json.loads(home.settings.read_text())["hooks"]["Stop"][0] == "junk", (
        "the entry this script does not understand was dropped rather than left alone")
    assert home("--check").returncode == 0


@jq_required
def test_a_fragment_that_wires_nothing_is_refused_rather_than_blessed(tmp_path, home):
    """An empty fragment makes every counter in --check stay at its initial value, and the
    answer that falls out of that is "all wired" — on a host with no board. The same
    vacuous-pass shape as the short-rows guard, one level up."""
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"hooks": {}}))
    assert home("--fragment", str(empty), "--check").returncode == 1
    # ...and wiring mode still refuses to fail an activation over it.
    result = home("--fragment", str(empty), "--mcp", "never")
    assert result.returncode == 0
    assert "wires no hook events" in result.stderr


def test_an_unknown_argument_is_refused_rather_than_ignored(home):
    """It is invoked from an activation script with flags composed by nix. A typo'd flag
    that was ignored would wire something other than what the module asked for."""
    assert home("--mcp-mode", "never").returncode == 64
    assert home("--mcp", "sometimes").returncode == 64


@pytest.mark.parametrize("flag", ["--mcp", "--hook", "--fragment"])
def test_a_flag_with_no_value_is_refused_too(home, flag: str):
    """Same reason as the unknown flag above, and the same activation. `--mcp` swallowed by
    a broken nix interpolation used to fall back to `auto` — wiring something other than
    what the module asked for, which is precisely what exit 64 exists to prevent."""
    result = home(flag)
    assert result.returncode == 64, result.stdout
    assert flag in result.stderr


def test_help_prints_the_whole_header_including_the_exit_code_contract(home):
    """It was a line range (`sed -n '2,32p'`), and a line range goes stale by cutting the
    contract off mid-word — which is what it was doing: "…mid-migration; it is"."""
    result = home("--help")
    assert result.returncode == 0
    assert "exactly the state a doctor should name." in result.stdout
    assert "The default (wiring) mode always exits 0." in result.stdout
    assert not result.stdout.startswith("#"), "the comment markers were not stripped"
    assert "set -uo pipefail" not in result.stdout, "it ran past the end of the header"


# --------------------------------------------------- which qb-hook is this


def test_qb_hook_reports_its_own_path_and_the_qb_env_it_loaded():
    """#204's fourth layer. The two lines matter together: a qb-hook and a qb-env from
    different store paths is a half-migrated install, and it is invisible from either line
    alone. Paths and not a version string, because on a nix install the store hash IS the
    pin, while a `version` field is something somebody has to remember to bump."""
    result = subprocess.run([str(HOOK), "--version"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["qb-hook", str(HOOK)]
    assert lines[1].split() == ["qb-env", str(BIN / "qb-env")]


def test_qb_hook_version_fails_loudly_when_its_library_is_missing(tmp_path):
    """Every other path in qb-hook is fail-open by contract — a board outage must never
    block a session. `--version` is the one question where exiting 0 with a shrug is the
    wrong answer, because the caller is a doctor asking whether this install is intact."""
    lone = tmp_path / "qb-hook"
    lone.write_bytes(HOOK.read_bytes())
    lone.chmod(0o755)
    result = subprocess.run([str(lone), "--version"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "not found" in result.stdout


@pytest.fixture
def hook_against_a_stub_board(tmp_path):
    """Run one qb-hook event against a `curl` that answers with a status we choose.

    A stub rather than a real server because the assertion is about what qb-hook makes of
    an HTTP status, and a status is the one thing a stub can produce exactly.
    """
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    curl = stub_dir / "curl"
    # It has to honour `-w`, since that is how qb-hook asks for the status at all: a stub
    # that always printed the code would pass even if the flag were dropped.
    # An absolute interpreter, not `/usr/bin/env`: the nix sandbox this suite also runs in
    # has no /usr/bin/env, so an env shebang leaves the stub unexecutable, qb-hook records
    # the board as unreachable, and the 200 case fails while the 401 case passes for the
    # wrong reason. That is the shape `patchShebangs` exists for, and a file written at
    # test time never meets it.
    curl.write_text(
        f'#!{shutil.which("bash") or "/bin/sh"}\n'
        'w=""\n'
        'while [ $# -gt 0 ]; do case "$1" in -w) w="$2"; shift ;; esac; shift; done\n'
        'printf \'%s\' "${STUB_BODY:-}"\n'
        'case "$w" in\n'
        '  "") : ;;\n'
        '  *"%{http_code}"*) printf \'\\n%s\' "${STUB_CODE:-200}" ;;\n'
        'esac\n')
    curl.chmod(0o755)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def run(event: str, code: str, body: str = "{}"):
        env = {
            **os.environ,
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path / "home"),
            "XDG_RUNTIME_DIR": str(run_dir),
            "QUARTERBACK_CONFIG": str(tmp_path / "no-config"),
            "QUARTERBACK_BASE_URL": "https://board.invalid",
            "QUARTERBACK_TOKEN": "t",
            "QUARTERBACK_AGENT": "testbox",
            "STUB_CODE": code,
            "STUB_BODY": body,
        }
        env.pop("TMUX", None)
        subprocess.run([str(HOOK), event], input='{"session_id":"abcdef123456"}',
                       env=env, capture_output=True, text=True, check=False)
        return (run_dir / "qb-health").read_text().strip()

    run.run_dir = run_dir
    return run


@pytest.mark.skipif(
    subprocess.run(["sh", "-c", "command -v jq"], capture_output=True).returncode != 0,
    reason="qb-hook exits 0 without jq, so there would be no beacon to assert on")
@pytest.mark.parametrize("code,verdict", [("200", "ok"), ("409", "ok"),
                                          ("401", "down"), ("500", "down")])
def test_the_health_beacon_reads_the_http_status_and_not_curls_exit_code(
        hook_against_a_stub_board, code: str, verdict: str):
    """`curl -sS` exits 0 on a 401 exactly as it does on a 200, so the beacon used to write
    `ok` on every turn of a host whose token the board had stopped accepting: the status
    line said quarterback was fine while every post it made was dropped. That is the
    failure qb-mcp's own self-heal exists for, and a health beacon is the one thing that
    must not have it. 409 stays `ok` on purpose — a lease conflict is news about the lease,
    not about the board being reachable."""
    assert hook_against_a_stub_board("Stop", code) == verdict


@pytest.mark.skipif(
    subprocess.run(["sh", "-c", "command -v jq"], capture_output=True).returncode != 0,
    reason="qb-hook exits 0 without jq")
def test_the_name_beacon_is_not_written_from_a_rejected_response(hook_against_a_stub_board):
    """The name half of an identity is the board's to designate, so caching one out of a
    401 body would put a name in the status line that no lease backs."""
    hook_against_a_stub_board("Stop", "401", body='{"holder":"testbox/ghost"}')
    assert not (hook_against_a_stub_board.run_dir / "qb-name-abcdef123456").exists()
    hook_against_a_stub_board("Stop", "200", body='{"holder":"testbox/glacier"}')
    assert (hook_against_a_stub_board.run_dir / "qb-name-abcdef123456").read_text() == "glacier"


def test_a_hook_event_on_an_unconfigured_host_is_silent_success(tmp_path):
    """The contract that lets this ship enabled by default: a host with no board (or an
    unreachable one) no-ops. A hook that errored would put a coordination board in the
    critical path of every tool call on every machine that imports the module."""
    result = subprocess.run(
        [str(HOOK), "Stop"], input="{}", capture_output=True, text=True,
        env={**os.environ, "HOME": str(tmp_path),
             "QUARTERBACK_CONFIG": str(tmp_path / "nope"),
             "QUARTERBACK_BASE_URL": ""})
    assert result.returncode == 0
    assert result.stdout == ""


# ------------------------------------------------------------------- the nix


def module_text() -> str:
    return HM_MODULE.read_text(encoding="utf-8")


@pytest.mark.parametrize("option", [
    "claude.enable", "claude.activationAfter", "claude.workflowDoc", "claude.registerMcp",
    "board.url", "board.tokenCommand", "board.tokenRefreshCommand", "board.agent", "board.repo",
])
def test_the_module_declares_the_option(option: str):
    """Each of these is load-bearing for one acceptance criterion, and a renamed option is
    a consumer's config silently doing nothing (`programs.quarterback-harness.claude.enable`
    that no longer exists is an eval error, but a REPLACEMENT default of false is not)."""
    section, leaf = option.split(".")
    text = module_text()
    assert re.search(rf"^\s*{section} = \{{", text, re.MULTILINE), f"no `{section}` block"
    assert re.search(rf"^\s*{leaf} = lib\.mkOption \{{", text, re.MULTILINE), \
        f"no `{leaf}` option in hm-module.nix"


def test_the_activation_runs_the_wiring_script_from_the_package():
    """By store path out of `cfg.package`, which is the whole point of #230: the wiring and
    the hook it wires come off ONE pin. A `$HOME/.local/bin/qb-claude-setup` here would
    reintroduce exactly the skew this change removes."""
    text = module_text()
    assert "${cfg.package}/bin/qb-claude-setup" in text
    assert "$HOME/.local/bin/qb-claude-setup" not in text


def test_the_activation_entry_is_not_named_quarterbackClaude():
    """`home.activation` is a shared namespace and two definitions of one attribute is not
    a merge — it is an eval failure for everyone who pins this. The consumers most likely
    to import this module are precisely the ones already carrying an activation script
    called `quarterbackClaude`, because that is where this code came from."""
    text = module_text()
    assert re.search(r"^\s*home\.activation\.quarterbackClaudeWiring =", text, re.MULTILINE)
    assert not re.search(r"^\s*home\.activation\.quarterbackClaude =", text, re.MULTILINE)


def test_the_activation_can_be_ordered_after_a_consumers_own_merge():
    """AC: "the module composes with a consumer that also merges into settings.json". The
    jq half is tested above; this is the half that cannot be tested by running anything,
    since it is a claim about a DAG home-manager builds. `activationAfter` has to reach
    `entryAfter`, or it is an option that documents an intention and changes nothing."""
    text = module_text()
    assert re.search(
        r'lib\.hm\.dag\.entryAfter \(\[ "writeBoundary" \] \+\+ cfg\.claude\.activationAfter\)',
        text), "activationAfter is declared but never reaches the DAG entry"


def test_the_activation_supplies_every_tool_the_script_shells_out_to():
    """The failure this catches is a switch that prints `cmp: command not found` and wires
    the file on every rebuild — the idempotence above is real and would be lost to a
    missing PATH entry, in a place no test of the script itself can see."""
    text = module_text()
    path = re.search(r"lib\.makeBinPath \[(.*?)\]", text, re.DOTALL)
    assert path, "the activation no longer builds a PATH"
    supplied = path.group(1)
    for pkg in ["pkgs.jq", "pkgs.gnugrep", "pkgs.coreutils", "pkgs.diffutils"]:
        assert pkg in supplied, f"{pkg} is missing from the activation PATH"


def test_the_site_config_is_only_rendered_when_a_board_is_named():
    """`xdg.configFile` is nix-owned, so declaring it unconditionally would COLLIDE with
    every consumer who already renders that file — which is every consumer that has a
    working board today. Null means "I manage it", not "use a default board"."""
    text = module_text()
    assert 'wantConfig = board.url != null' in text
    assert re.search(r"lib\.mkIf wantConfig \{\s*\n\s*xdg\.configFile", text)


@pytest.mark.parametrize("option", ["board.tokenCommand", "board.tokenRefreshCommand",
                                    "board.url", "board.agent"])
def test_a_single_quoted_value_may_not_carry_a_single_quote(option: str):
    """Every value emitted single-quoted needs the guard, not just the token command. A
    quote inside one terminates the quoting and the rest of the line becomes shell in a
    file that is *sourced* — surfacing as "no token" or an unset URL on every board call,
    with nothing pointing back at the option that caused it. So: an assertion, at eval
    time, by name."""
    assert f'(noSingleQuote "{option}" {option})' in module_text()


@pytest.mark.parametrize("var,value", [
    ("QUARTERBACK_BASE_URL", "board.url"),
    ("QUARTERBACK_AGENT", "board.agent"),
])
def test_the_site_config_quotes_every_value_it_emits(var: str, value: str):
    """`~/.config/quarterback/config` is sourced, so an unquoted assignment ends at the
    first `&`, `;` or space and the remainder is executed. `https://board/x?a=1&b=2` is a
    perfectly ordinary URL and, unquoted, it backgrounds `b=2` on every board call — in
    every hook, on every tool call, with the only symptom being a host that never appears."""
    assert re.search(rf"^\s*{var}='\$\{{{re.escape(value)}\}}'", module_text(), re.MULTILINE) \
        or re.search(rf"^\s*{var}='\$\{{toString {re.escape(value)}\}}'", module_text(), re.MULTILINE), \
        f"{var} is not emitted single-quoted in hm-module.nix"


@pytest.mark.parametrize("name", ["qb-hook", "qb-env", "qb-mcp", "qb-claude-setup", "qb"])
def test_the_board_client_ships_in_the_package(name: str):
    """`install -m 0755 bin/*` copies whatever is there, so this asserts the FILE exists in
    the tree the package builds from. The four scripts and their library are the thing
    #230 moved; a rename that lost one would leave the module wiring a path that does not
    exist, and the hook failing open means nothing would say so."""
    assert (BIN / name).exists(), f"harness/bin/{name} is gone — the package cannot ship it"


def test_the_package_ships_the_claude_data_directory():
    """The fragment and the workflow doc. `bin/*` is copied by glob and `claude/` is not,
    so this one is a hand-maintained list — the exact shape that goes stale."""
    text = PACKAGE_NIX.read_text(encoding="utf-8")
    assert re.search(
        r"cp -r loops commands templates claude githooks \$out/share/quarterback-harness/",
        text), "package.nix no longer installs harness/claude into share/"


#: What this suite reads from outside its own directory — the declaration `_flake_sandbox`
#: compares against the sandbox that runs it. Everything else here is `harness/bin`, which
#: `worktree-tests` has always held.
#:
#: Declared rather than discovered, for the reason `_prose_sandbox` gives about its own
#: members: a parser cannot chase every way of naming a path. Unlike those members this suite
#: routes no read through an accessor — its paths are module-level constants, four of them —
#: so `test_every_declared_read_is_a_path_this_file_names` holds the list against them
#: instead, which is the same guard by the means available here.
READS = (
    "harness/claude",        # a tree: the fragment, and the workflow doc's presence
    "harness/hm-module.nix",
    "harness/package.nix",
    "flake.nix",
)

#: The check that runs this suite. Written out, not discovered: a renamed check has to be an
#: error here rather than an empty comparison reporting everything as fine.
CHECK_NAME = "worktree-tests"


def _sandbox_sources() -> set[str]:
    """Every path `worktree-tests` copies in, via #264's parser."""
    flake = _flake_sandbox
    region = flake.check_region(FLAKE.read_text(encoding="utf-8"), CHECK_NAME)
    pairs = flake.copies(region)
    # prefix="" and not the default "repo/": this check builds `harness/…` at the top level
    # and `cd harness`, where the prose and release-metadata sandboxes build a `repo/` tree.
    # Passing the wrong prefix here would report every copy in this check as misdirected.
    assert not flake.misdirected(pairs, prefix=""), flake.misdirected(pairs, prefix="")
    return set(pairs)


@pytest.mark.parametrize("path", READS)
def test_the_worktree_flake_check_supplies_what_this_suite_reads(path: str):
    """The coupling guard for this file's own sandbox, and it is not hypothetical.

    `worktree-tests` copies named paths into a store sandbox. Before #264 it copied
    `harness/bin` and `harness/tests` only, and `test_commands_wired.py` reads
    `harness/hm-module.nix` — so the check had been aborting at COLLECTION and no bash suite
    in this repo had ever run under `nix flake check`, while the GitHub job stayed green
    because it runs in a real checkout where every path resolves. #264 moved that suite to a
    check that holds what it reads; this one reads four paths of its own, and needs the same
    guard for the same reason.

    Through `_flake_sandbox` rather than a regex of this file's own, which is the whole point
    of that module being factored out of #264: parsing a check's copy lines is identical for
    every suite with this problem, and a second implementation would be a second thing to get
    subtly wrong — the `${ ./x }` spacing, a copy line inside a comment, a destination that
    does not mirror its source."""
    sources = _sandbox_sources()
    assert _flake_sandbox.supplied_by(path, sources), (
        f"flake.nix's {CHECK_NAME} sandbox does not supply {path}, which this suite reads. "
        f"Add a `cp -r`/`install -D` line for it beside the others, or every assertion about "
        f"it errors on a missing file instead of being evaluated (#163).")


@pytest.mark.parametrize("path", READS)
def test_every_declared_read_is_a_path_this_file_names(path: str):
    """The converse, and the gap `_prose_sandbox`'s docstring is careful to state: a
    declaration whose last reader was deleted still matches the install answering it. This
    file's reads are four module constants rather than accessor calls, so the check that is
    unavailable to those members is available here — hold the declaration against the paths
    the module actually builds."""
    named = {str(x.relative_to(REPO_ROOT)) for x in (FRAGMENT, HM_MODULE, PACKAGE_NIX, FLAKE)}
    assert any(p == path or p.startswith(path + "/") for p in named), (
        f"READS declares {path}, which no constant in this module resolves to — either the "
        f"read went away and the declaration should too, or it moved")


def test_every_declared_read_exists():
    """A declaration pointing at a file nobody has. Cheap, and it is what catches a rename
    that updated the flake and the constants but not this list."""
    for path in READS:
        assert (REPO_ROOT / path).exists(), f"READS declares {path}, which does not exist"


def test_the_flake_no_longer_promises_a_wiring_it_does_not_do():
    """AC: "the flake description at flake.nix:22 becomes true". It claimed ~/.local/bin,
    which the module never wrote to, and it claimed to wire the harness in, which it did
    for the commands and not for the board."""
    text = FLAKE.read_text(encoding="utf-8")
    block = re.search(r"# Wires the harness into(.*?)homeManagerModules", text, re.DOTALL)
    assert block, "the homeManagerModules comment block moved — check the claim by hand"
    claim = block.group(1)
    assert "~/.local/bin" not in claim, (
        "the description still promises ~/.local/bin, which this module does not write")
    assert "board" in claim, "the description does not mention the board it now wires"


def test_an_unwired_event_is_MISSING_even_though_a_field_after_it_is_set(tmp_path, monkeypatch):
    """The column shift that CI caught, pinned.

    `--check` builds its rows in jq and reads them in bash. Tab is an IFS
    *whitespace* character, so `read` folds runs of tabs into a single delimiter
    and drops empty fields entirely. That was harmless while the possibly-empty
    `cmd` was the LAST column, and became a one-column shift the moment the
    matcher was added after it: an event with no qb-hook entry read its own
    matcher as its command, so the branch that reports MISSING never fired and
    the host was told SKEW instead.

    MISSING and SKEW are different facts with different remedies — "this host is
    deaf on that event" against "it is wired to something else" — and the whole
    point of the mode is telling them apart.
    """
    import json
    import subprocess

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    hook = str(BIN / "qb-hook") if "BIN" in globals() else str(
        Path(__file__).resolve().parents[1] / "bin" / "qb-hook")
    frag = json.loads(
        (Path(__file__).resolve().parents[1] / "claude" / "settings-fragment.json").read_text()
        .replace("@QB_HOOK@", hook))
    # PreToolUse wired to somebody else's guard: no qb-hook entry at all, and the
    # entry that IS there carries a matcher — the exact shape that shifted.
    frag["hooks"]["PreToolUse"] = [
        {"matcher": "Task", "hooks": [{"type": "command", "command": "my-guard.sh"}]}
    ]
    (home / ".claude" / "settings.json").write_text(json.dumps(frag, indent=2))

    got = subprocess.run([str(Path(__file__).resolve().parents[1] / "bin" / "qb-claude-setup"),
                          "--check"],
                         capture_output=True, text=True,
                         env={**os.environ, "HOME": str(home)}, timeout=60)
    assert "MISSING  PreToolUse" in got.stdout, got.stdout
    assert got.returncode == 1, got.stdout


def test_qb_mcp_exports_the_agent_name_the_server_interpolates(tmp_path):
    """`QUARTERBACK_AGENT` must reach the MCP server's ENVIRONMENT, not just qb-mcp's shell.

    The server resolves one credential for itself: `client._resolve_elevated` runs
    `QUARTERBACK_ELEVATED_TOKEN_CMD` through `subprocess.run(..., shell=True)` with no
    `env=`, so that child sees `os.environ` and nothing else. The fleet writes that
    command as `op read "op://…/quarterback-$QUARTERBACK_AGENT/elevated"`, and
    `qb_load_config` sets the variable as a plain shell variable. Unexported, it expands
    to empty in the child and the ref becomes `quarterback-/elevated` — an item name no
    vault has.

    What made it cost an evening is the shape of the failure: the board answered "this
    host has no delegated credential", which reads as an unprovisioned machine, and the
    variable this script dropped is nowhere in that sentence.

    Asserted through the exec rather than by reading the source, because `export` is the
    one thing a static check of the assignment cannot see. The stub stands in for the
    server and resolves the command exactly as `client.py` does.
    """
    repo = tmp_path / "repo"
    (repo / "mcp" / ".venv" / "bin").mkdir(parents=True)
    py = repo / "mcp" / ".venv" / "bin" / "python"
    # The interpreter by absolute path, never `/usr/bin/env` (#177): there is no
    # `/usr/bin/env` inside a nix build sandbox, so an env-shebang stub cannot exec
    # and the suite fails for a reason unrelated to the code under test.
    py.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys\n"
        "cmd = os.environ.get('QUARTERBACK_ELEVATED_TOKEN_CMD', '')\n"
        "out = subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout\n"
        "sys.stdout.write('AGENT=%s\\nRESOLVED=%s\\n'\n"
        "                 % (os.environ.get('QUARTERBACK_AGENT', ''), out.strip()))\n",
        encoding="utf-8")
    py.chmod(0o755)

    config = tmp_path / "config"
    config.write_text(
        # Unroutable on purpose: the self-heal probe must fail fast and fail open.
        "QUARTERBACK_BASE_URL='http://127.0.0.1:1'\n"
        "QUARTERBACK_TOKEN='stub-bearer'\n"
        # The fleet's real shape, with `op read` swapped for `echo` so the test needs no
        # vault. The interpolation is the part under test.
        "QUARTERBACK_ELEVATED_TOKEN_CMD='echo \"quarterback-$QUARTERBACK_AGENT/elevated\"'\n",
        encoding="utf-8")

    env = {**os.environ, "QUARTERBACK_CONFIG": str(config), "QUARTERBACK_REPO": str(repo)}
    for stray in ("QUARTERBACK_AGENT", "QUARTERBACK_TOKEN", "QUARTERBACK_BASE_URL",
                  "QUARTERBACK_ELEVATED_TOKEN", "QUARTERBACK_ELEVATED_TOKEN_CMD"):
        env.pop(stray, None)

    done = subprocess.run([str(BIN / "qb-mcp")], capture_output=True, text=True,
                          env=env, timeout=60)
    assert done.returncode == 0, f"qb-mcp exited {done.returncode}: {done.stderr}"

    host = subprocess.run(["hostname", "-s"], capture_output=True, text=True).stdout.strip()
    assert f"AGENT={host}" in done.stdout, (
        "qb-mcp did not export QUARTERBACK_AGENT into the server's environment; "
        f"got: {done.stdout!r}")
    assert f"RESOLVED=quarterback-{host}/elevated" in done.stdout, (
        "the credential reference resolved with an empty agent name — this is the "
        f"`quarterback-/elevated` failure. Got: {done.stdout!r}")
