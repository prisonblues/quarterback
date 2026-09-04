"""`QUARTERBACK_INSTANCE` as a name a human can type, not just a key (#156).

Since v2.12 the board *designates* the name half of an identity: a client sends an
opaque key in `X-Agent-Instance` and is allocated two words, and `zeus/cotton-indigo`
is what every peer sees. `X-Agent-Name` is the one way to ask for something typeable
instead — it has worked server-side since v2.12, and the lifecycle hook never sent it.
So `QUARTERBACK_INSTANCE=seat-3` named nothing at all: it keyed a two-word allocation
and survived only as an alias nobody is shown.

Allocation is first-contact-wins and the hook fires on `SessionStart`, so what the
hook sends is what the whole session is called. That makes these subprocess runs the
test that matters: asserting on the text of `qb-hook` cannot tell a header that is
present from one that is actually sent, which is the distinction the bug was made of.

Three properties, and the second is the one with teeth:

* an explicit label is *requested* as the name, by every client that talks to a board;
* an **unset** `QUARTERBACK_INSTANCE` requests nothing, because the fallback instance
  is a session-id hex fragment and asking for that as a name would put `zeus/a4f81c2e`
  back on every status bar in the fleet;
* the requested name is shaped by the board's *name* rule, which is stricter than its
  key rule — both clients swallow a 400 in silence, `qb-hook` by contract.

Run: pytest harness/tests
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_qb_hook_end import Hooked

BIN = Path(__file__).resolve().parents[1] / "bin"
QB_ENV = BIN / "qb-env"
QB = BIN / "qb"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="the board clients are bash and parse their payloads with jq",
)


# --------------------------------------------------------- the rule, on its own


def requested_name(label: str | None) -> str:
    """What the real `qb_requested_name` prints for `QUARTERBACK_INSTANCE=<label>`."""
    env = {k: v for k, v in os.environ.items() if k != "QUARTERBACK_INSTANCE"}
    if label is not None:
        env["QUARTERBACK_INSTANCE"] = label
    got = subprocess.run(
        ["bash", "-c", 'set -uo pipefail; . "$1"; qb_requested_name', "_", str(QB_ENV)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert got.returncode == 0, got.stderr
    return got.stdout


#: The board's own shape for a name, restated here because this suite runs in a nix
#: sandbox that holds `harness/` and no `app/`. `tests/test_designated_names.py` pins
#: the same corpus against `app.identity` itself, which is the half that owns the rule.
NAME_RE = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"

LABELS = {
    "seat-3": "seat-3",
    "deploy": "deploy",
    # A valid KEY and an invalid name: the key charset allows `_`, `.` and `~` and
    # upper case, and the name charset allows none of them.
    "Deploy_1": "deploy-1",
    "seat.lexray~9": "seat-lexray-9",
    "UPPER": "upper",
    "-lead-and-trail-": "lead-and-trail",
    "sea t 3": "sea-t-3",
    "café-3": "caf-3",
    # Truncation happens BEFORE the final trim, because cutting at the board's 40
    # can itself produce the trailing hyphen the shape forbids.
    "x" * 45: "x" * 40,
    ("y" * 39) + "-zz": "y" * 39,
    # Nothing usable in it: ask for no name rather than for `-`.
    "___": "",
    "": "",
}


@pytest.mark.parametrize("label,want", list(LABELS.items()), ids=range(len(LABELS)))
def test_a_label_is_shaped_into_something_the_board_will_take(label, want):
    assert requested_name(label) == want


def test_every_shaped_label_matches_the_board_s_name_rule():
    import re
    for label, want in LABELS.items():
        if want:
            assert re.match(NAME_RE, want) and len(want) <= 40, label


def test_no_variable_means_no_request():
    """Not the same as an empty one, and both must produce nothing."""
    assert requested_name(None) == ""


# ------------------------------------------------------------------ the hook


@pytest.fixture
def hook(tmp_path):
    """`qb-hook` over the REAL `qb-env`, with only the site config stubbed.

    `test_qb_hook_end` stubs `qb-env` wholesale, which is right for the events it
    drives and wrong here: the thing under test is the name that library derives.
    """
    h = Hooked(tmp_path)
    (h.bin / "qb-env").write_text(
        f'. "{QB_ENV}"\n'
        "qb_load_config() {\n"
        "  QUARTERBACK_BASE_URL=http://board.test\n"
        "  QUARTERBACK_AGENT=testbox\n"
        "}\n"
        "qb_resolve_token() { QUARTERBACK_TOKEN=tok-test; return 0; }\n"
    )
    return h


def headers_sent(hook) -> list[str]:
    calls = hook.to("/session/end")
    assert calls, hook.sent()
    return calls


def test_an_explicit_label_is_requested_as_the_name(hook):
    hook.fire("SessionEnd", env=hook.env(QUARTERBACK_INSTANCE="seat-3"), reason="other")
    sent = headers_sent(hook)[0]
    assert "X-Agent-Instance: seat-3" in sent
    assert "X-Agent-Name: seat-3" in sent


def test_an_unset_label_requests_nothing_at_all(hook):
    """The regression that would hurt most. With no label the instance is the
    session-id prefix, and requesting *that* as a name would replace every agent's
    two memorable words with a hex fragment — the thing v2.12 moved naming
    server-side to stop."""
    hook.fire("SessionEnd", reason="other")
    sent = headers_sent(hook)[0]
    assert "X-Agent-Instance: sid-1" in sent
    assert "X-Agent-Name" not in sent


def test_a_label_with_nothing_usable_in_it_requests_nothing(hook):
    """`___` cannot be a name. The header is omitted rather than sent malformed: the
    board answers 400, and this hook swallows a 400 in silence, so a bad request
    here would take the whole session's presence down with it.

    It sends no key either, and that is **not** this change — the key sanitiser has
    always stripped a label with no alphanumerics down to nothing, which collapses
    the session onto the bare machine name (the broadcast address). Recorded here
    because this is the test that found it; the MCP server falls back to the session
    prefix on the same input, so the two halves disagree. Out of scope for #156,
    which is about the name.
    """
    hook.fire("SessionEnd", env=hook.env(QUARTERBACK_INSTANCE="___"), reason="other")
    sent = headers_sent(hook)[0]
    assert "X-Agent-Name" not in sent


def test_a_label_the_name_rule_refuses_is_reshaped_not_dropped(hook):
    hook.fire("SessionEnd", env=hook.env(QUARTERBACK_INSTANCE="Deploy_1"), reason="other")
    sent = headers_sent(hook)[0]
    assert "X-Agent-Instance: Deploy_1" in sent   # the key keeps the operator's spelling
    assert "X-Agent-Name: deploy-1" in sent       # the name is what the board can take


def test_an_older_qb_env_beside_the_hook_costs_the_name_and_nothing_else(hook):
    """`qb-hook` and `qb-env` are separately pinned store paths (#204), so a
    half-migrated install can pair this hook with a library that predates
    `qb_requested_name`. That must lose the requested name in silence — not the
    presence post, and not a stderr line on every event of every session."""
    (hook.bin / "qb-env").write_text(
        "qb_load_config() { QUARTERBACK_BASE_URL=http://board.test; QUARTERBACK_AGENT=testbox; }\n"
        "qb_resolve_token() { QUARTERBACK_TOKEN=tok-test; return 0; }\n"
    )
    got = hook.fire("SessionEnd", env=hook.env(QUARTERBACK_INSTANCE="seat-3"), reason="other")
    sent = headers_sent(hook)[0]
    assert "X-Agent-Instance: seat-3" in sent
    assert "X-Agent-Name" not in sent
    assert got.stderr == "", got.stderr


# -------------------------------------------------------------------- the CLI


class Recorder:
    """`qb record-review` with a stub board beside it and a stub curl in front."""

    def __init__(self, tmp_path: Path) -> None:
        self.bin = tmp_path / "qbbin"
        self.bin.mkdir()
        (self.bin / "qb").write_bytes(QB.read_bytes())
        (self.bin / "qb").chmod(0o755)
        (self.bin / "qb-env").write_text(
            f'. "{QB_ENV}"\n'
            "qb_load_config() { QUARTERBACK_BASE_URL=http://board.test; QUARTERBACK_AGENT=testbox; }\n"
            "qb_resolve_token() { QUARTERBACK_TOKEN=tok-test; return 0; }\n"
        )
        self.stub = tmp_path / "stub"
        self.stub.mkdir()
        self.calls = tmp_path / "curl.log"
        (self.stub / "curl").write_text(
            "#!/bin/sh\n" f'printf "%s\\n" "$*" >> {self.calls}\n' "exit 0\n"
        )
        (self.stub / "curl").chmod(0o755)

    def run(self, **over) -> str:
        env = {k: v for k, v in os.environ.items()
               if k not in ("QUARTERBACK_INSTANCE", "CLAUDE_CODE_SESSION_ID")}
        env["PATH"] = f"{self.stub}:{os.environ['PATH']}"
        env.update(over)
        got = subprocess.run([str(self.bin / "qb"), "record-review"],
                             input='{"pr":1}', capture_output=True, text=True,
                             env=env, timeout=60)
        assert got.returncode == 0, got.stderr
        return self.calls.read_text() if self.calls.exists() else ""


@pytest.fixture
def recorder(tmp_path):
    return Recorder(tmp_path)


def test_a_recorded_run_asks_for_the_same_name_the_hook_does(recorder):
    sent = recorder.run(QUARTERBACK_INSTANCE="seat-quarterback-1")
    assert "X-Agent-Name: seat-quarterback-1" in sent


def test_an_explicit_label_is_the_key_verbatim_not_its_first_eight_characters(recorder):
    """The 8 belongs to the session id and to nothing else. Applied to a label as
    well, this filed `seat-quarterback-1`'s panel runs under the key `seat-qua` — a
    different row on the board from the one its hook and its MCP server register, so
    the agent that ran the review was not the agent the review was recorded against.
    Every seat `qb-seats` builds has a label longer than eight characters."""
    sent = recorder.run(QUARTERBACK_INSTANCE="seat-quarterback-1")
    assert "X-Agent-Instance: seat-quarterback-1" in sent
    assert "X-Agent-Instance: seat-qua " not in sent


def test_with_no_label_the_key_is_still_the_session_prefix_and_no_name_is_asked(recorder):
    sent = recorder.run(CLAUDE_CODE_SESSION_ID="c9f3ff06-8af9-4ead-83a2-25aeb800ce88")
    assert "X-Agent-Instance: c9f3ff06" in sent
    assert "X-Agent-Name" not in sent


# ----------------------------------------------------- the two copies of one rule

#: The board's shape for a KEY, restated for the same reason `NAME_RE` is above.
#: Wider than a name — upper case, `.`, `_` and `~` are all legal — which is exactly
#: why the two need separate sanitisers.
KEY_RE = r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,39}$"

#: Labels chosen to hurt: over the 40-character cap, whitespace in the middle, a
#: leading character that may not start a key, multibyte bytes, and a newline.
HOSTILE = ["seat-3", "seat-quarterback-1", "Deploy_1", "sea t 3", "x" * 45,
           "-lead", "café-3", "one\ntwo", "  padded"]


def _instance_header(line: str) -> str | None:
    marker = "-H X-Agent-Instance: "
    if marker not in line:
        return None
    return line.split(marker, 1)[1].split(" -H ", 1)[0].split(" --", 1)[0].strip()


@pytest.mark.parametrize("label", HOSTILE)
def test_the_hook_and_the_cli_send_one_key_for_one_label(tmp_path, label):
    """`qb-hook` and `qb` each hold their own copy of the key rule, and this is what
    makes that safe. Sharing it through `qb-env` would hand the hook's whole identity
    to a library it is pinned separately from (#204) — the shim that costs a
    *requested name* nothing would cost the hook its key, and a keyless agent is the
    bare machine name, which is also the broadcast address.

    Two copies that disagree are the #146 failure — one session showing up as two
    agents — so they are pinned equal here instead. `qb` sent the label raw until
    #156, which made `sea t 3` and anything over 40 characters a 400 that
    `record-review` swallows by design.
    """
    import re

    (tmp_path / "h").mkdir()
    (tmp_path / "c").mkdir()
    h = Hooked(tmp_path / "h")
    (h.bin / "qb-env").write_text(
        f'. "{QB_ENV}"\n'
        "qb_load_config() { QUARTERBACK_BASE_URL=http://board.test; QUARTERBACK_AGENT=testbox; }\n"
        "qb_resolve_token() { QUARTERBACK_TOKEN=tok-test; return 0; }\n"
    )
    h.fire("SessionEnd", env=h.env(QUARTERBACK_INSTANCE=label), reason="other")
    hook_key = _instance_header(h.to("/session/end")[0])

    cli_key = _instance_header(Recorder(tmp_path / "c").run(QUARTERBACK_INSTANCE=label))

    assert hook_key == cli_key, (label, hook_key, cli_key)
    assert hook_key is not None, label
    assert re.match(KEY_RE, hook_key), (label, hook_key)
