"""Argument handling, the tokenless path, and the refusals that guard a resume."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess

import httpx
import pytest
from mcp_server.board import __main__ as cli
from mcp_server.board.__main__ import (
    RecipientUnresolved,
    _client,
    _no_token_source_remedy,
    _project_dir,
    _report_health,
    _strip_verb,
    _tail_lines,
    _transcript_path,
    build_parser,
    do_resume,
    main,
    resolve_recipient,
)


class Cfg:
    base_url = "https://board.example"
    token = "tok"
    agent = "daedalus"
    config_path = "/home/rich/.config/quarterback/config"
    authenticated = True
    token_problem = None
    token_cmd_configured = True


def parse(*argv):
    return build_parser().parse_args(_strip_verb(list(argv)))


def test_a_leading_board_verb_is_accepted_and_dropped():
    """All `qb` needs is `board) exec qb-board "$@"` — in either repo."""
    assert parse("board", "--follow").follow is True
    assert parse("--follow").follow is True
    # ...and only a *leading* one, so `--to board` still means what it says.
    assert parse("--to", "board").to == "board"


def test_defaults_match_the_documented_behaviour():
    args = parse()
    assert args.follow is False
    assert args.lines == 20
    assert args.presence is False  # hidden by default, as GET /board already decided
    assert args.colour is None  # decided by whether stdout is a tty
    assert args.repo == "."


def test_types_accumulate():
    assert parse("-t", "ask", "-t", "finding").types == ["ask", "finding"]


def test_colour_flags_are_mutually_exclusive():
    assert parse("--no-color").colour is False
    assert parse("--color").colour is True
    with pytest.raises(SystemExit):
        parse("--color", "--no-color")


def test_project_dir_matches_the_substitution_claude_code_makes():
    # Getting this wrong writes the transcript somewhere --resume never looks,
    # and it fails silently.
    assert _project_dir("/home/rich/source/quarterback") == "-home-rich-source-quarterback"
    assert _project_dir("/home/rich/a.b/c") == "-home-rich-a-b-c"


def test_project_dir_replaces_underscores_too():
    """The encoding Claude Code actually uses, taken from a real directory:
    `/tmp/panel-claude-3q6p345_/cwd` is stored with a doubled dash where the
    underscore was. Missing it is the silent no-history resume."""
    assert _project_dir("/tmp/panel-claude-3q6p345_/cwd") == "-tmp-panel-claude-3q6p345--cwd"
    assert _project_dir("/home/rich/my_repo") == "-home-rich-my-repo"


def test_a_backlog_size_above_the_servers_cap_is_clamped_rather_than_422d():
    """`limit` is `ge=1, le=1000` on GET /board, and the response is
    materialised into a list here — so an unbounded -n is a 422 at best."""
    assert _tail_lines(5000) == 1000
    assert _tail_lines(20) == 20
    assert _tail_lines(-3) == 0


# -- the tokenless path ------------------------------------------------


class HealthyClient:
    def health(self):
        return {"status": "ok"}


class DownClient:
    def health(self):
        raise httpx.ConnectError("no route to host")


def test_with_no_token_a_healthy_board_is_reported_as_up():
    err = io.StringIO()
    assert _report_health(HealthyClient(), Cfg(), err) == 1
    assert "is up" in err.getvalue() and "no token" in err.getvalue()


def test_with_no_token_a_dead_board_is_reported_as_down():
    err = io.StringIO()
    assert _report_health(DownClient(), Cfg(), err) == 1
    assert "DOWN" in err.getvalue()


def test_a_token_source_that_answered_nothing_is_not_reported_as_an_unset_one():
    """#201: the message sent operators to configure what was already configured.

    On the host this was found on the token file was present and valid and the
    config was correct per the documented contract; the client had failed to supply
    a variable it defines itself. "Set QUARTERBACK_TOKEN_CMD" is then advice to edit
    a generated file to work around a client bug — so when a credential source did
    run, the report names the event and the identity it ran under instead.
    """

    class Failed(Cfg):
        token = None
        authenticated = False
        token_problem = "the token command succeeded but produced no output"

    err = io.StringIO()
    assert _report_health(HealthyClient(), Failed(), err) == 1
    out = err.getvalue()
    assert "is up" in out
    assert "produced no output" in out
    assert "daedalus" in out  # the agent the command expanded to: the whole bug
    # And NOT the old instruction, which is the part that cost the operator the hour.
    #
    # The whole sentence, and case-insensitively, because the round-1 form of this guard
    # (`"Set QUARTERBACK_TOKEN" not in out`) had stopped biting: the branch offers
    # "Or export QUARTERBACK_TOKEN in the environment", and an earlier wording of it said
    # "Or set QUARTERBACK_TOKEN…" — missed on the capital S alone. Passing on
    # capitalisation is not passing, so the assertion names the remedy that must not
    # appear (configure a token source, i.e. `_no_token_source_remedy`) rather than a
    # prefix of it that the legitimate one-shot advice also starts with.
    assert "set quarterback_token or quarterback_token_cmd" not in out.lower()


def test_the_token_problem_report_says_what_to_do_next():
    """A named failure the operator cannot act on is a better diagnosis, same dead end.

    The message deliberately never prints the command or its output, so the only way
    to see the helper's own words is to re-run it — under the same agent name, or the
    selector picks a different line. That, and the one-shot QUARTERBACK_TOKEN
    override, are what turn the diagnosis into something to do.
    """

    class Failed(Cfg):
        token = None
        authenticated = False
        token_problem = "the token command exited 1"

    err = io.StringIO()
    assert _report_health(HealthyClient(), Failed(), err) == 1
    out = err.getvalue()
    assert "export QUARTERBACK_AGENT=daedalus" in out
    assert 'eval "$QUARTERBACK_TOKEN_CMD"' in out
    assert Cfg.config_path in out
    assert "QUARTERBACK_TOKEN in the environment" in out
    assert "one-shot override" in out


def test_a_problem_with_no_command_configured_gets_the_other_remedy():
    """The command remedy is false on a host that never configured a command.

    `token_problem` also covers the legacy token file, which is tried whether or not a
    command exists — so a host with no `QUARTERBACK_TOKEN_CMD` and an unreadable
    `/run/op-secrets/quarterback-token` has a named failure whose fix genuinely IS to
    configure a token source. Printing the command remedy there would be this bug
    again: a confident instruction that is false on the box reading it.
    """

    class LegacyFailed(Cfg):
        token = None
        authenticated = False
        token_problem = "/run/op-secrets/quarterback-token could not be read (Permission denied)"
        token_cmd_configured = False

    err = io.StringIO()
    assert _report_health(HealthyClient(), LegacyFailed(), err) == 1
    out = err.getvalue()
    assert "could not be read" in out  # still named, which is the point of #201
    assert "Set QUARTERBACK_TOKEN or QUARTERBACK_TOKEN_CMD" in out
    # And NOT the command remedy, which there is no command to re-run for. Matched on
    # "not the remedy" rather than a longer quotation of the sentence, so rewording the
    # command branch's prose cannot quietly turn this guard into a tautology.
    assert "eval" not in out
    assert "not the remedy" not in out.lower()


def test_an_awkward_agent_name_stays_copy_pasteable_in_the_remedy():
    """`QUARTERBACK_AGENT` is environment-overridable, so the suggested line is quoted.

    Unquoted, an agent name with a space turns `export QUARTERBACK_AGENT=two words`
    into an export plus a command called `words` — a remedy that fails differently
    from the thing it is meant to diagnose.
    """

    class Spaced(Cfg):
        agent = "two words"
        token = None
        authenticated = False
        token_problem = "the token command exited 1"

    err = io.StringIO()
    _report_health(HealthyClient(), Spaced(), err)
    assert "export QUARTERBACK_AGENT='two words'" in err.getvalue()


def test_the_set_one_message_survives_for_a_host_that_genuinely_has_none():
    """The old wording is correct in the case it was written for, and stays."""

    class Bare(Cfg):
        token = None
        authenticated = False
        token_problem = None

    err = io.StringIO()
    assert _report_health(HealthyClient(), Bare(), err) == 1
    assert "Set QUARTERBACK_TOKEN or QUARTERBACK_TOKEN_CMD" in err.getvalue()


def _cmd_failure_report(config_path, agent="daedalus"):
    """The `token_cmd_configured` diagnostic, as stderr text, for `config_path`.

    Instance attributes over another `Cfg` subclass per test: the recipe interpolates
    both of these, so every guard below wants its own path and name rather than the
    module-level default.
    """
    cfg = Cfg()
    cfg.token = None
    cfg.authenticated = False
    cfg.token_problem = "the token command succeeded but produced no output"
    cfg.config_path = str(config_path)
    cfg.agent = agent
    err = io.StringIO()
    assert _report_health(HealthyClient(), cfg, err) == 1
    return err.getvalue()


def _recipe_lines(out):
    """The shell lines offered for copy-pasting, in the order they are printed.

    The recipe is indented twelve spaces and the prose around it eight, which is what
    tells them apart — and is why a prose line that grew a deeper indent would show up
    here as a broken shell command rather than passing unnoticed.
    """
    return [line[12:] for line in out.splitlines() if line.startswith(" " * 12)]


def _run_recipe(recipe):
    """Run the printed lines the way an operator would paste them into a shell.

    Nothing of this process's environment goes in but `PATH`: the recipe's own export is
    what has to establish `QUARTERBACK_AGENT`, and inheriting one would hide the very
    ordering bug this checks for.
    """
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - no bash to source the config with
        pytest.skip("no bash to run the suggested reproduction with")
    proc = subprocess.run(
        [bash, "-c", "\n".join(recipe)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_the_suggested_reproduction_runs_under_the_identity_the_client_used(tmp_path):
    """Copy-paste the recipe and you get the client's identity, not the file's.

    `config.resolve` puts the resolved agent name into the environment *first* and then
    sources the config, and it deliberately ignores a `QUARTERBACK_AGENT=` line in the
    file — the divergence from `qb-env` that config.py's docstring argues for. So a
    recipe that exports the name and *then* sources the file hands that ignored line the
    chance to overwrite it, and the command runs as somebody else: a reproduction of a
    bug the client never had, offered by the message whose entire subject is which
    identity the command ran under (#201).

    Proved by running the printed lines through bash against a config that pins a
    different name, rather than by asserting on their order — the order only matters
    because of what bash does with it, so bash is what should answer.
    """
    # A plain assignment, which is the shape `qb-env` honours and this client does not,
    # and a single-quoted command, which is the documented form: the reference is expanded
    # by the shell that runs the command, so it sees whichever name is exported by then.
    # The pinned name is one byte long, so `wc -c` alone separates the two identities.
    config = tmp_path / "config"
    config.write_text(
        "QUARTERBACK_AGENT=a\nQUARTERBACK_TOKEN_CMD='printf %s \"tok-$QUARTERBACK_AGENT\"'\n"
    )
    out = _run_recipe(_recipe_lines(_cmd_failure_report(config)))
    # len("tok-daedalus") == 12; the file's pin would have made it len("tok-a") == 5.
    assert out.split() == ["12"]


def test_the_suggested_reproduction_keeps_the_credential_out_of_the_terminal(tmp_path):
    """It promises not to print the token, so it must not tell you to print it.

    Two lines above the recipe the message explains that the command's output is not
    repeated here *because it can be the token*; a bare `eval "$QUARTERBACK_TOKEN_CMD"`
    then puts it in scrollback, and in shell history if it was typed. The failure being
    diagnosed is "no output", which a byte count answers — and the operator can drop the
    pipe deliberately when they do want the value.
    """
    config = tmp_path / "config"
    config.write_text("QUARTERBACK_TOKEN_CMD='printf %s s3cret-bearer'\n")
    out = _cmd_failure_report(config)
    assert 'eval "$QUARTERBACK_TOKEN_CMD" | wc -c' in out
    # And no line is the bare eval — the piped form contains the unpiped one as a
    # substring, so only an anchored match can tell the two apart.
    assert not re.search(r'^\s*eval "\$QUARTERBACK_TOKEN_CMD"\s*$', out, re.M)
    pasted = _run_recipe(_recipe_lines(out))
    assert "s3cret" not in pasted
    assert pasted.split() == ["13"]  # len("s3cret-bearer"), and nothing else


def test_every_recipe_line_keeps_its_own_comment_on_its_own_line():
    """The missing-comma shape, pinned as output rather than as source.

    `f"… {path}"` and `"   # skip if yours…"` were adjacent literals in the `lines` list
    with no comma between them — implicitly concatenated, which happened to produce the
    intended line. Putting the comma back (or losing one from a neighbour) changes the
    printed message without failing anything, so what is asserted is the message: each
    shell line carries its own trailing comment, and no line of the output is a stray
    comment fragment standing on its own.
    """
    out = _cmd_failure_report("/home/rich/.config/quarterback/config")
    recipe = _recipe_lines(out)
    assert len(recipe) == 3
    assert all("   # " in line for line in recipe)
    assert recipe[0].endswith("# skip if yours is in the environment")
    assert not [line for line in out.splitlines() if line.strip().startswith("#")]


def test_the_no_token_source_remedy_is_one_text_in_both_places():
    """The two "configure a token source" arms print the same string, not two copies.

    A named failure whose source was not a token command, and a host that configured
    nothing at all, get the same advice — and it was written out twice, a dozen lines
    apart, which is the arrangement where one copy gets reworded and the other does not.
    """

    class NoCommand(Cfg):
        token = None
        authenticated = False
        token_problem = "/run/op-secrets/quarterback-token is empty"
        token_cmd_configured = False

    class Nothing(Cfg):
        token = None
        authenticated = False
        token_problem = None

    remedy = "\n".join(_no_token_source_remedy(Cfg()))
    assert "QUARTERBACK_TOKEN_CMD" in remedy  # a remedy, not an empty string to match
    for cfg in (NoCommand(), Nothing()):
        err = io.StringIO()
        assert _report_health(HealthyClient(), cfg, err) == 1
        assert remedy in err.getvalue()
    # And absent from the branch it would be wrong in: that host has a command already.
    assert remedy not in _cmd_failure_report(Cfg.config_path)


# -- resume ------------------------------------------------------------


class SessionClient:
    def __init__(self, state):
        self._state = state

    def session_state(self, session):
        return self._state

    def get_blob(self, sha):  # pragma: no cover - not reached in these tests
        return b""


def test_resuming_a_session_another_device_still_holds_is_refused():
    """Two machines resuming one session both write transcripts; the second wins."""
    err = io.StringIO()
    client = SessionClient(
        {"latest_blob": "abc", "cwd": "/tmp", "active_lease": {"holder": "atlas/x"}}
    )
    assert do_resume(client, "s1", err) == 1
    assert "atlas/x" in err.getvalue() and "refusing" in err.getvalue()


def test_a_session_with_no_transcript_cannot_be_resumed():
    err = io.StringIO()
    assert do_resume(SessionClient({"latest_blob": None, "cwd": "/tmp"}), "s1", err) == 1
    assert "no transcript" in err.getvalue()


def test_a_session_with_no_recorded_cwd_says_what_to_run_by_hand():
    err = io.StringIO()
    assert do_resume(SessionClient({"latest_blob": "abc", "cwd": None}), "s1", err) == 1
    assert "claude --resume s1" in err.getvalue()


def test_an_unknown_session_is_reported_rather_than_raised():
    class Broken:
        def session_state(self, session):
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", "https://b/x"), response=httpx.Response(404)
            )

    err = io.StringIO()
    assert do_resume(Broken(), "s1", err) == 1
    assert "could not read session" in err.getvalue()


def test_a_checkout_missing_on_this_machine_is_named_rather_than_execd(tmp_path, monkeypatch):
    class Blobby(SessionClient):
        def get_blob(self, sha):
            return b'{"ok": true}\n'

    # HOME redirected: the transcript really is written, and it must land in the
    # test's directory rather than in the developer's ~/.claude/projects.
    monkeypatch.setenv("HOME", str(tmp_path))
    err = io.StringIO()
    cwd = tmp_path / "absent"
    client = Blobby({"latest_blob": "abc", "cwd": str(cwd), "active_lease": None})
    assert do_resume(client, "s1", err) == 1
    assert "not on this machine" in err.getvalue()
    # The pull happened first and is reported: the transcript is here even though
    # the checkout is not, which is what the "clone it, then --resume" line means.
    written = tmp_path / ".claude" / "projects" / _project_dir(str(cwd)) / "s1.jsonl"
    assert written.read_bytes() == b'{"ok": true}\n'


# -- resume: where the bytes land --------------------------------------

BLOB = b'{"type":"user"}\n'


class BlobClient(SessionClient):
    """A session that is free to take, with a transcript to pull."""

    def __init__(self, cwd, blob=BLOB):
        super().__init__({"latest_blob": "abc", "cwd": str(cwd), "active_lease": None})
        self._blob = blob

    def get_blob(self, sha):
        return self._blob


class Execd(Exception):
    """os.execvp does not return; the tests model that rather than falling through."""


@pytest.fixture
def home(tmp_path, monkeypatch):
    """HOME redirected, so a real write lands in the test's directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def execs(monkeypatch):
    """Capture chdir/execvp instead of replacing the test process."""
    calls: dict = {}

    def chdir(path):
        calls["chdir"] = path

    def execvp(file, argv):
        calls["exec"] = (file, argv)
        raise Execd

    monkeypatch.setattr(os, "chdir", chdir)
    monkeypatch.setattr(os, "execvp", execvp)
    return calls


def transcript(home, cwd, session="s1"):
    return home / ".claude" / "projects" / _project_dir(str(cwd)) / f"{session}.jsonl"


def test_a_pulled_transcript_lands_where_claude_resume_will_look_and_is_then_exceld(home, execs):
    err = io.StringIO()
    cwd = home / "src" / "my_repo"
    cwd.mkdir(parents=True)
    with pytest.raises(Execd):
        do_resume(BlobClient(cwd), "s1", err)
    assert transcript(home, cwd).read_bytes() == BLOB
    assert execs["chdir"] == str(cwd)
    assert execs["exec"] == ("claude", ["claude", "--resume", "s1"])


def test_a_local_transcript_that_differs_is_kept_rather_than_overwritten(home):
    """The exit hook may never have pushed — a crash, a kill -9, no network — in
    which case the local file is *ahead* of the blob and is the only copy."""
    err = io.StringIO()
    cwd = home / "absent"  # stops after the write, before any exec
    target = transcript(home, cwd)
    target.parent.mkdir(parents=True)
    target.write_bytes(b'{"type":"unpushed"}\n')

    assert do_resume(BlobClient(cwd), "s1", err) == 1
    assert target.read_bytes() == BLOB
    kept = list(target.parent.glob("s1.jsonl.*.local"))
    assert len(kept) == 1 and kept[0].read_bytes() == b'{"type":"unpushed"}\n'
    assert "kept at" in err.getvalue()


def test_an_identical_local_transcript_is_not_needlessly_backed_up(home):
    err = io.StringIO()
    cwd = home / "absent"
    target = transcript(home, cwd)
    target.parent.mkdir(parents=True)
    target.write_bytes(BLOB)

    assert do_resume(BlobClient(cwd), "s1", err) == 1
    assert list(target.parent.glob("*.local")) == []


def test_a_session_id_with_a_path_separator_is_refused_before_any_write(home):
    """The id comes back from the board and is interpolated into a filename."""
    err = io.StringIO()
    cwd = home / "absent"
    assert do_resume(BlobClient(cwd), "../../../../etc/cron.d/evil", err) == 1
    assert "refusing session id" in err.getvalue()
    assert not (home / ".claude").exists()


@pytest.mark.parametrize("session", ["a/b", "..", ".hidden", "x" * 65, "", "a b"])
def test_transcript_path_rejects_anything_that_is_not_a_bare_id(session, home):
    with pytest.raises(ValueError, match="refusing session id"):
        _transcript_path(session, "/tmp")


def test_a_target_that_escapes_the_projects_directory_is_refused(home, monkeypatch):
    """Belt to the regex's braces: if the directory encoding is ever loosened,
    containment still has to hold."""
    monkeypatch.setattr(cli, "_project_dir", lambda cwd: "../../..")
    with pytest.raises(ValueError, match="outside"):
        _transcript_path("s1", "/tmp")


def test_a_relative_recorded_cwd_is_refused_rather_than_resolved_against_ours(home):
    err = io.StringIO()
    assert do_resume(BlobClient("relative/path"), "s1", err) == 1
    assert "absolute path" in err.getvalue()


# -- resume: the lease is advisory, so it is re-read late ---------------


class LeaseRace(BlobClient):
    """Free on the first read, claimed by the time of the nth."""

    def __init__(self, cwd, claimed_at):
        super().__init__(cwd)
        self._claimed_at, self.reads = claimed_at, 0

    def session_state(self, session):
        self.reads += 1
        if self.reads < self._claimed_at:
            return self._state
        return {**self._state, "active_lease": {"holder": "atlas/x"}}


def test_a_lease_taken_between_the_first_check_and_the_write_still_refuses(home):
    """Nothing here locks, so the check is re-read as late as it can be."""
    err = io.StringIO()
    cwd = home / "src"
    cwd.mkdir()
    client = LeaseRace(cwd, claimed_at=2)
    assert do_resume(client, "s1", err) == 1
    assert "atlas/x" in err.getvalue()
    assert not transcript(home, cwd).exists()


def test_a_lease_taken_between_the_write_and_the_exec_refuses_before_exec(home, execs):
    err = io.StringIO()
    cwd = home / "src"
    cwd.mkdir()
    assert do_resume(LeaseRace(cwd, claimed_at=3), "s1", err) == 1
    assert transcript(home, cwd).read_bytes() == BLOB  # the pull already happened
    assert execs == {}


def test_an_unreadable_re_check_continues_because_the_check_is_advisory(home, execs):
    """A dropped packet turning into a refusal would make resume flakier than the
    fork it guards against — and the local copy is backed up either way."""

    class Flaky(BlobClient):
        def __init__(self, cwd):
            super().__init__(cwd)
            self.reads = 0

        def session_state(self, session):
            self.reads += 1
            if self.reads > 1:
                raise httpx.ConnectError("no route")
            return self._state

    err = io.StringIO()
    cwd = home / "src"
    cwd.mkdir()
    with pytest.raises(Execd):
        do_resume(Flaky(cwd), "s1", err)
    assert "could not re-check" in err.getvalue()


def test_a_failed_exec_says_what_to_run_by_hand(home, monkeypatch):
    def boom(file, argv):
        raise OSError("no claude on PATH")

    monkeypatch.setattr(os, "chdir", lambda path: None)
    monkeypatch.setattr(os, "execvp", boom)
    err = io.StringIO()
    cwd = home / "src"
    cwd.mkdir()
    assert do_resume(BlobClient(cwd), "s1", err) == 1
    assert "claude --resume s1" in err.getvalue()


# -- identity ----------------------------------------------------------


def headers(client):
    return dict(client._http.headers)


def test_by_default_the_client_sends_no_agent_key():
    """No key ⇒ the bare machine name, which is also the broadcast address.

    Sending one would have the board designate a fresh two-word name per launch —
    a name that never finishes and so is never recycled — and make `?to=@me` the
    inbox of an identity a second old, rather than this machine's whole mail.
    """
    h = headers(_client(Cfg(), {}))
    assert "x-agent-key" not in h and "x-agent-name" not in h
    assert h["authorization"] == "Bearer tok"


def test_quarterback_instance_gives_the_client_a_stable_identity():
    h = headers(_client(Cfg(), {"QUARTERBACK_INSTANCE": "deploy"}))
    assert h["x-agent-key"] == "deploy" and h["x-agent-name"] == "deploy"


def test_an_instance_label_the_board_would_refuse_as_a_name_is_still_sent_as_a_key():
    """Better than 400ing the session's first request over a capital letter."""
    h = headers(_client(Cfg(), {"QUARTERBACK_INSTANCE": "Rich_Laptop"}))
    assert h["x-agent-key"] == "Rich_Laptop"
    assert "x-agent-name" not in h


def test_a_tokenless_client_sends_no_authorization_header():
    class NoToken(Cfg):
        token = None

    assert "authorization" not in headers(_client(NoToken(), {}))


# -- @me ---------------------------------------------------------------


class WhoClient:
    def __init__(self, agent=None, boom=False):
        self._agent, self._boom = agent, boom

    def whoami(self):
        if self._boom:
            raise httpx.ConnectError("no route")
        return {"agent": self._agent}


def test_at_me_is_resolved_once_so_both_halves_of_the_tail_agree():
    """`/stream` takes no recipient filter, so the live half has nothing to
    compare a server-side spelling against."""
    assert resolve_recipient(WhoClient("zeus/fern-nectar"), "@me") == "zeus/fern-nectar"


def test_an_ordinary_recipient_is_passed_through_untouched():
    assert resolve_recipient(WhoClient(), "zeus") == "zeus"
    assert resolve_recipient(WhoClient(), None) is None


def test_an_unresolvable_at_me_raises_rather_than_dropping_the_filter():
    with pytest.raises(RecipientUnresolved):
        resolve_recipient(WhoClient(boom=True), "@me")


# -- main() ------------------------------------------------------------


class FollowSpy:
    def __init__(self):
        self.kwargs = None

    def __call__(self, client, base_url, **kwargs):
        self.kwargs = kwargs
        return 0


class FakeApp:
    """Stands in for tui.BoardApp, which another module owns."""

    def __init__(self, client, cfg, repo_path=None):
        self.repo_path = repo_path

    def run(self):
        return None


class FakeResumeRequest:
    pass


def run_main(monkeypatch, *argv, client=None, cursor=0, load_tui=None):
    spy = FollowSpy()
    monkeypatch.setattr(cli, "resolve", lambda: Cfg())
    monkeypatch.setattr(cli, "_client", lambda cfg: client if client is not None else WhoClient())
    monkeypatch.setattr(cli, "follow", spy)
    monkeypatch.setattr(cli, "read_cursor", lambda base_url: cursor)
    monkeypatch.setattr(cli, "want_colour", lambda: False)
    monkeypatch.setattr(cli, "_load_tui", load_tui or (lambda: (FakeApp, FakeResumeRequest)))
    return main(list(argv)), spy


def test_at_me_that_cannot_be_resolved_exits_rather_than_tailing_the_whole_board(
    monkeypatch, capsys
):
    """Answering "my inbox" with "everyone's" is the wrong answer, silently."""
    code, spy = run_main(monkeypatch, "--follow", "--to", "@me", client=WhoClient(boom=True))
    assert code == 1
    assert spy.kwargs is None
    assert "could not resolve @me" in capsys.readouterr().err


def test_resume_with_no_recorded_cursor_takes_the_backlog_not_the_whole_history(
    monkeypatch, capsys
):
    """read_cursor returns 0 for a board this client has never run against, and
    /stream?since=0 replays everything ever posted."""
    code, spy = run_main(monkeypatch, "--follow", "--resume", cursor=0)
    assert code == 0
    assert spy.kwargs["since"] is None
    assert "no cursor recorded" in capsys.readouterr().err


def test_resume_uses_the_recorded_cursor_when_there_is_one(monkeypatch):
    _, spy = run_main(monkeypatch, "--follow", "--resume", cursor=4210)
    assert spy.kwargs["since"] == 4210


def test_an_explicit_since_wins_over_the_cursor(monkeypatch):
    _, spy = run_main(monkeypatch, "--follow", "--resume", "--since", "9", cursor=4210)
    assert spy.kwargs["since"] == 9


def test_a_huge_backlog_request_is_clamped_before_it_reaches_the_board(monkeypatch):
    _, spy = run_main(monkeypatch, "--follow", "-n", "50000")
    assert spy.kwargs["tail"] == 1000


def test_follow_only_flags_are_named_back_when_the_fullscreen_client_runs(monkeypatch, capsys):
    """They are accepted and documented, so silently dropping them reads as a bug
    in the filter rather than in the invocation."""
    code, _ = run_main(monkeypatch, "--presence", "--to", "zeus", "-n", "5")
    assert code == 0
    err = capsys.readouterr().err
    assert "--presence" in err and "--to" in err and "-n/--lines" in err
    assert "--since" not in err  # not passed, so not named


def test_the_fullscreen_client_with_no_tail_flags_says_nothing(monkeypatch, capsys):
    code, _ = run_main(monkeypatch, "-C", "/some/repo")
    assert code == 0
    assert capsys.readouterr().err == ""


def test_a_missing_textual_is_reported_as_something_to_install(monkeypatch, capsys):
    def boom():
        raise ImportError("No module named 'textual.app'", name="textual.app")

    code, _ = run_main(monkeypatch, load_tui=boom)
    assert code == 1
    assert "Textual, which is not installed" in capsys.readouterr().err


def test_an_import_error_from_our_own_package_is_not_blamed_on_textual(monkeypatch):
    """`pip install textual` is the wrong advice for a defect in tui.py, and it
    hides the defect."""

    def boom():
        raise ImportError("cannot import name 'BoardApp'", name="mcp_server.board.tui")

    with pytest.raises(ImportError, match="BoardApp"):
        run_main(monkeypatch, load_tui=boom)
