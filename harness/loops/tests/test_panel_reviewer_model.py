"""Pinning a codex model/effort, and failing legibly when the pin doesn't work.

The panel's Claude reviewer is pinned to the floating alias `opus`; Codex is
pinned (if at all) to a versioned build name that gets retired. So the pin's
failure path is the part that has to be right: a model the installed CLI is too
old for is refused by the API, and the panel must say THAT rather than blame
auth and drop a whole vendor into a footnote.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_seats  # noqa: E402  — run_cli lives here since #129
import panel_core  # noqa: E402  — ReviewerRun/SeatAnswer live here

TOO_OLD = (
    'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error",'
    '"message":"The \'gpt-5.6-luna\' model requires a newer version of Codex. '
    'Please upgrade to the latest app or CLI and try again."}}'
)


# ---------------------------------------------------------------- argv

#: What every codex argv opens with — the seat's `--no-tools`, spelled as the two
#: `-c` overrides codex takes instead of a flag. Named so the tests below assert
#: the model/effort shape without restating it.
NO_TOOLS = ["codex", "exec", "-s", "read-only",
            "-c", 'web_search="disabled"',
            "-c", "features.shell_tool=false",
            "-c", "features.apps=false", "-c", "features.plugins=false"]


def _f():
    """A finding, so a ReviewerRun reads as one that produced something."""
    return panel_core.Finding("codex", "P3", "a.py", 1, "t", "d")


def test_unpinned_codex_passes_no_model_flag():
    """Empty == the CLI's own default, which is the global default: no --model."""
    assert panel.codex_args("", "") == NO_TOOLS


def test_model_and_effort_are_independent():
    """Effort is a `-c` override, not a flag, and applies to the default model
    too — so someone can raise reasoning without pinning a slug that will rot."""
    assert panel.codex_args("", "high")[-2:] == ["-c", "model_reasoning_effort=high"]
    assert panel.codex_args("gpt-5.6-luna", "") == [*NO_TOOLS, "--model", "gpt-5.6-luna"]
    assert panel.codex_args("gpt-5.6-luna", "high") == [
        *NO_TOOLS, "--model", "gpt-5.6-luna", "-c", "model_reasoning_effort=high"]


def test_codex_seat_can_neither_search_the_web_nor_run_a_shell():
    """The seat reviews the diff it was handed, like every other seat.

    Not a preference: `member_sandbox` hands codex an EMPTY repo, so a tool can
    only find the wrong answer. With its tools it went hunting — the empty
    sandbox first, then web searches for a private repo — which is what put runs
    over CLI_TIMEOUT, and it reached the real checkout by passing an absolute
    `workdir`, which read-only mode permits (it bounds writes, not reads).

    Asserted for EVERY argv shape, pinned or not, because both are reachable
    from .harness-rules and a seat that keeps its tools on the unpinned path is
    the same lost reviewer.

    The apps/plugins pair is asserted alongside the obvious two because removing
    only the obvious two is a MEASURED non-fix: the seat enumerated what was left
    in the code-mode runtime and reached the authenticated GitHub connector
    instead, which fetches the PR over the network with credentials. Dropping
    either key reopens that.
    """
    for args in (panel.codex_args("", ""), panel.codex_args("gpt-5.6-luna", "max"),
                 panel.codex_args("gpt-5.6-luna", "max", Path("/tmp/reply.txt"))):
        assert 'web_search="disabled"' in args
        assert "features.shell_tool=false" in args
        assert "features.apps=false" in args
        assert "features.plugins=false" in args
        # Pinned, not inherited: `apply_patch` outlives every -c key above and is
        # inert only because of this. `codex exec --help` documents no default,
        # so an unpinned seat is one release from being write-capable silently.
        assert args[args.index("-s") + 1] == "read-only"


# ---------------------------------------------------------------- failing cleanly

def test_typo_effort_is_refused_without_spending_a_run(monkeypatch):
    """A config error is answered as one, before three CLI invocations discover
    it downstream and report it as an opaque non-zero exit."""
    called = []
    monkeypatch.setattr(panel_seats, "run_cli", lambda *a, **k: called.append(a) or (None, None))
    got = panel.review_llm("codex", "gpt-5.6-luna", "p", effort="hi")
    assert got.findings == [] and called == []
    assert "unknown reasoning effort" in got.skip and "'hi'" in got.skip
    assert "xhigh" in got.skip                      # the valid set is stated, not implied


def test_panel_re_exports_the_shared_cli_failure_plumbing():
    """`stderr_gist` and `cli_outcome` live in harness_rules — how headless CLIs
    fail is not a panel question — and are re-exported here because they read as
    part of run_cli's contract. All a re-export owes anyone is being the same
    object; the behaviour is tested where the function lives
    (test_harness_rules.py), so deleting that copy and growing a private one back
    fails there rather than passing here for the wrong reason."""
    import harness_rules

    assert panel.stderr_gist is harness_rules.stderr_gist
    assert panel.cli_outcome is harness_rules.cli_outcome


def test_hint_blames_the_pin_not_the_login():
    """The old code appended '(auth? run `codex login`)' to every non-zero exit —
    a confident wrong answer for exactly the failure a pin is likeliest to cause."""
    err = f"codex (gpt-5.6-luna, high): exited 1 ({TOO_OLD})"
    hint = panel.cli_hint("codex", err, "gpt-5.6-luna")
    assert "gpt-5.6-luna" in hint and "upgrade the CLI" in hint
    assert "codex login" not in hint


def test_hint_still_offers_login_for_a_real_auth_failure():
    err = "codex (CLI default): exited 1 (Provided authentication token is expired.)"
    assert "codex login" in panel.cli_hint("codex", err, "")


def test_label_names_the_model_that_ran():
    """'codex ran' is not the same claim as 'codex ran on the model you pinned'."""
    assert panel.reviewer_label("codex", "gpt-5.6-luna", "high") == "codex (gpt-5.6-luna, high)"
    assert panel.reviewer_label("codex", "") == "codex (CLI default)"
    assert panel.reviewer_label("claude", "opus") == "claude (opus)"


# ------------------------------------------- a pin this host's provider cannot serve (#215)

#: codex's real stdout on the failing run, trimmed to the shape that matters: the
#: `--json` event stream, whose `error` envelopes carry the only account of what
#: went wrong. Captured on daedalus 2026-08-18 against the employer gateway.
GATEWAY_STDOUT = "\n".join([
    '{"type":"thread.started","thread_id":"01a01529-d54c-78d0-941d-654630410f44"}',
    '{"type":"turn.started"}',
    '{"type":"error","message":"Reconnecting... 1/10 (unexpected status 404 Not Found: '
    'The API deployment for this resource does not exist. If you created the deployment '
    'within the last 5 minutes, please wait a moment and try again., url: '
    'https://example.invalid/openai/responses?api-version=2025-04-01-preview)"}',
    '{"type":"error","message":"Reconnecting... 10/10 (unexpected status 404 Not Found: '
    'The API deployment for this resource does not exist., url: '
    'https://example.invalid/openai/responses?api-version=2025-04-01-preview)"}',
])

#: And its entire stderr. One line, written before the request was even made —
#: which is why a diagnosis built from this stream alone could only ever name it.
GATEWAY_STDERR = "Reading prompt from stdin...\n"

#: The gateway's sentence on its own, as a plain string.
#:
#: Spelled out rather than obtained from `error_events` in the two tests below, and
#: the reason is this PR's own subject: a test that reaches for a NEW symbol fails
#: against the old code with an AttributeError, which demonstrates nothing about the
#: behaviour it claims to pin — it only proves the function is new. Fed a literal,
#: those two tests go red on their assertion against the pre-fix code (`False` and
#: `""`), which is the failure that means something.
GATEWAY_404 = (
    "Reconnecting... 10/10 (unexpected status 404 Not Found: The API deployment for "
    "this resource does not exist., url: https://example.invalid/openai/responses)"
)

#: The gateway's refusal of the EFFORT pin, which is a separate sentence from the
#: model 404 and arrives even once the model is servable. `param` is the only field
#: that says which pin was refused — the message names the MODEL — so this fixture
#: is fed through `error_text` wherever it stands in for what a classifier sees.
GATEWAY_EFFORT = ('{"error":{"param":"reasoning.effort","code":"unsupported_value",'
                  '"message":"Unsupported value: \'max\' is not supported with this model."}}')

#: And the same refusal as it ACTUALLY arrives on stdout: pretty-printed, which is
#: what the line-oriented first version of `error_events` silently skipped. The
#: fragment `stderr_gist` then quoted, `"type": "invalid_request_error",`, is the
#: sentence the panel really printed about a refused effort (219-F12).
GATEWAY_EFFORT_PRETTY = """{
  "error": {
    "message": "Unsupported value: 'max' is not supported with this model.",
    "type": "invalid_request_error",
    "param": "reasoning.effort",
    "code": "unsupported_value"
  }
}
"""


def test_the_error_events_come_off_stdout_because_stderr_never_had_them():
    """The regression, stated as the two streams actually were.

    codex under `--json` puts its event stream on stdout, so a provider refusal
    arrives there while stderr holds a progress banner. `stderr_gist` was not
    picking the wrong line — it picked the only line it was given, and the panel
    reported `exited 1 (Reading prompt from stdin...)` for a 404. Two people
    debugged stdin plumbing off that sentence."""
    assert panel_seats.stderr_gist(GATEWAY_STDERR) == "Reading prompt from stdin..."
    lifted = panel_seats.error_text(GATEWAY_STDOUT)
    assert lifted.count("\n") == 1, "both error envelopes, and only those"
    diag = f"{GATEWAY_STDERR}\n{lifted}"
    gist = panel_seats.stderr_gist(diag)
    assert "404" in gist and "deployment" in gist.lower(), gist
    assert "stdin" not in gist, "the banner must not win once a real error is present"


def test_error_events_ignores_everything_that_is_not_an_error_envelope():
    """Safe to run over any seat's stdout, including the seats whose stdout IS
    their reply — so it has to be strict about what counts."""
    assert panel_seats.error_events("") == []
    assert panel_seats.error_events("Here are my findings:\n- a bug\n") == []
    assert panel_seats.error_events('{"type":"turn.completed","usage":{}}') == []
    assert panel_seats.error_events('not json but has "error" in it') == []
    assert panel_seats.error_events('{"type":"error","message":"boom"}') == [
        {"type": "error", "message": "boom"}]
    # Kept as an envelope (it IS one) and dropped from the TEXT, which is the layer
    # that has to decide whether a line says anything: a bare marker in a diagnosis
    # only pushes a line that names something out of `stderr_gist`'s pick.
    assert panel_seats.error_events('{"type":"error"}') == [{"type": "error"}]
    assert panel_seats.error_text('{"type":"error"}') == "", "no message, nothing to say"
    assert panel_seats.error_text('{"type":"error","message":"  "}') == "", "blank message"
    assert panel_seats.error_text('{"type":"error","message":"boom"}') == (
        '{"type":"error","message":"boom"}')


def test_the_envelope_keeps_the_field_that_says_WHICH_pin_was_refused():
    """F07/F11: the message names the model and `param` names the effort, so a
    reader that keeps only `ev["message"]` cannot tell the two refusals apart.

    That is not a hypothetical loss. It is what happened: the model pin was dropped
    correctly, the effort pin was never even considered, and the seat was lost with
    `model_unavailable: "gpt-5.6-luna"` / `effort_unsupported: null` recorded — half
    a diagnosis, on the run that motivated the second half of this fix."""
    text = panel_seats.error_text(GATEWAY_EFFORT)
    assert text, "the envelope must survive extraction at all"
    assert "reasoning.effort" in text and "unsupported_value" in text
    assert panel_seats.is_effort_unsupported(text)
    assert not panel_seats.is_model_unavailable(text)


def test_a_pretty_printed_envelope_is_lifted_and_flattened_to_one_line():
    """F12: the line-oriented parse assumed strict JSONL, and the gateway's refusal
    is not JSONL — it is an indented body across seven lines. Nothing recognised it,
    so `stderr_gist` ranked those lines as prose and quoted the one that carries no
    information at all: `"type": "invalid_request_error",`. That fragment is exactly
    what the panel printed, and it names neither the parameter nor the value."""
    assert panel_seats.error_events(GATEWAY_EFFORT_PRETTY), "indented is still JSON"
    text = panel_seats.error_text(GATEWAY_EFFORT_PRETTY)
    assert "\n" not in text, "one line per envelope, or stderr_gist cannot rank it"
    assert panel_seats.stderr_gist(text) == (
        "Unsupported value: 'max' is not supported with this model.")
    assert panel_seats.is_effort_unsupported(text)
    assert not panel_seats.is_model_unavailable(text)


def test_a_provider_404_is_recognised_as_the_pin_being_unservable():
    assert panel_seats.is_model_unavailable(GATEWAY_STDOUT)
    assert panel_seats.is_model_unavailable(TOO_OLD), "the older spelling still counts"
    assert panel_seats.is_model_unavailable("unknown model 'gpt-9'")
    assert not panel_seats.is_model_unavailable("unexpected status 429 Too Many Requests")
    assert not panel_seats.is_model_unavailable("404 Not Found"), (
        "a bare 404 is not evidence about a MODEL — it stays retryable")


def test_a_provider_404_is_not_retried():
    """The second cost of reading the wrong stream, and the one nobody had noticed.

    `is_rejection` keys on 4xx invalid-request markers and an explicit
    `"status":400`; a gateway 404 is neither, so an unservable pin read as a flake
    worth another go. codex had already reconnected ten times internally, and each
    outer attempt then spent the seat's full budget — ten minutes at a time — to
    reach the identical 404."""
    assert panel_seats.is_deterministic_failure(GATEWAY_404), (
        "an unservable pin was being retried — three attempts at ten minutes each "
        "to arrive at the identical 404")
    assert not panel_seats.is_deterministic_failure(
        "unexpected status 429 Too Many Requests"), "429 is still worth retrying"


def test_the_hint_names_the_deployment_not_the_cli_version():
    """The two causes share vocabulary and only one of them is true per box.

    Telling someone to upgrade codex when the gateway simply has no deployment for
    the slug is the confident wrong answer `cli_hint` exists to stop giving — and
    it is the answer that was given, twice, on the run this fixes."""
    hint = panel_seats.cli_hint(
        "codex", f"codex (gpt-5.6-luna, max): exited 1 ({GATEWAY_404})", "gpt-5.6-luna")
    assert "deployment" in hint and "gpt-5.6-luna" in hint
    assert "upgrade" not in hint.lower(), (
        "the CLI is current; upgrading it fixes nothing and sent two people the wrong way")


def test_the_too_old_pin_still_gets_the_upgrade_advice():
    """Ordering guard: the 404 branch is checked first and must not shadow this one,
    which is a genuinely different remedy for a genuinely different failure."""
    hint = panel_seats.cli_hint("codex", f"exited 1 ({TOO_OLD})", "gpt-5.6-luna")
    assert "upgrade" in hint.lower() and "codex --version" in hint


def test_the_effort_pin_is_refused_independently_of_the_model():
    """The finding that broke the first version of this fix.

    `.harness-rules` pins codex twice and this gateway serves neither value:
    `gpt-5.6-luna+max` 404s, `gpt-5.5+max` is an `unsupported_value` on
    `reasoning.effort`, `gpt-5.5+high` works. A fallback that dropped only the model
    would keep `-c model_reasoning_effort=max` and lose the seat on the next knob."""
    assert panel_seats.is_effort_unsupported(GATEWAY_EFFORT)
    assert not panel_seats.is_model_unavailable(GATEWAY_EFFORT), (
        "an effort refusal is not a model refusal — dropping the model would not fix it")
    assert panel_seats.is_deterministic_failure(GATEWAY_EFFORT), "and it is settled"


def test_a_generic_unsupported_value_is_not_read_as_an_effort_problem():
    """`unsupported_value` is a generic code — a rejected sampling parameter or tool
    spec carries it too, and lowering the effort would fix neither."""
    assert not panel_seats.is_effort_unsupported(
        '{"error":{"param":"tools[0].type","code":"unsupported_value"}}')


def test_the_fallback_label_names_what_actually_reviewed():
    """`.harness-rules` pins slugs so "codex found 9 issues" still means something
    later. A seat reviewing on the CLI default while the report printed the PIN would
    break that silently, and in the flattering direction."""
    assert panel_seats.fallback_label("codex", "", "max", "gpt-5.6-luna", "") == (
        "codex (CLI default, max; pinned gpt-5.6-luna unavailable)")
    assert panel_seats.fallback_label("codex", "gpt-5.5", "", "", "max") == (
        "codex (gpt-5.5; effort max unsupported)")
    assert panel_seats.fallback_label("codex", "", "", "gpt-5.6-luna", "max") == (
        "codex (CLI default; pinned gpt-5.6-luna unavailable, effort max unsupported)")
    assert panel_seats.fallback_label("codex", "gpt-5.5", "high") == (
        panel_seats.reviewer_label("codex", "gpt-5.5", "high")), (
        "no fallback means no editorialising — it is just the ordinary label")


def _seat_with(monkeypatch, outcomes, model="gpt-5.6-luna", effort="max", seat="codex"):
    """Run a seat against a scripted list of (reply, err) run_cli results, capturing
    the argv and label each attempt would have used.

    `reply` is written to the file that attempt's argv names, not returned as stdout
    — codex is the one seat whose stdout is its EVENT STREAM and whose reply lands in
    `--output-last-message`. A stub answering on stdout would have the seat read None
    and pass for the wrong reason.
    """
    monkeypatch.setattr(panel_seats.shutil, "which", lambda _c: f"/usr/bin/{seat}")
    seen = []
    calls = iter(outcomes)

    def fake_run_cli(args, label, **kw):
        argv = args() if callable(args) else args
        seen.append((argv, label))
        reply, err = next(calls)
        if reply is not None and "--output-last-message" in argv:
            Path(argv[argv.index("--output-last-message") + 1]).write_text(reply)
        return (None if err else "{}"), err

    monkeypatch.setattr(panel_seats, "run_cli", fake_run_cli)
    turn = panel_seats.run_seat(seat, model, "review this", effort)
    return turn, seen


def test_an_unservable_model_falls_back_to_the_cli_default(monkeypatch):
    """Turns a lost vendor into a degraded one. The second attempt must carry NO
    `--model`, or it re-sends the slug the provider just refused."""
    err = f"codex (gpt-5.6-luna, max): exited 1 ({GATEWAY_404})"
    turn, seen = _seat_with(monkeypatch, [(None, err), ("findings here", None)])
    assert len(seen) == 2
    assert "--model" in seen[0][0]
    assert "--model" not in seen[1][0], "the fallback re-sent the refused pin"
    assert "model_reasoning_effort=max" in " ".join(seen[1][0]), (
        "only the pin the error named is dropped — the effort was not refused here")
    assert "-c" in seen[1][0], "the seat's no-tools overrides survive the fallback"
    assert turn.skip is None and turn.reply == "findings here"
    assert turn.model_unavailable == "gpt-5.6-luna" and turn.effort_unsupported == ""
    assert "pinned gpt-5.6-luna unavailable" in seen[1][1]


def test_both_pins_are_dropped_when_both_are_refused(monkeypatch):
    """The daedalus case end to end: model 404 first, then the effort refused on the
    CLI default. Three attempts, and the seat still reviews."""
    turn, seen = _seat_with(monkeypatch, [
        (None, f"exited 1 ({GATEWAY_404})"),
        (None, f"exited 1 ({GATEWAY_EFFORT})"),
        ("findings here", None),
    ])
    assert len(seen) == 3, "one lowering per pin, then the run that works"
    assert "--model" not in seen[2][0]
    assert "model_reasoning_effort" not in " ".join(seen[2][0])
    assert turn.reply == "findings here"
    assert turn.model_unavailable == "gpt-5.6-luna"
    assert turn.effort_unsupported == "max"
    assert seen[2][1] == ("codex (CLI default; pinned gpt-5.6-luna unavailable, "
                          "effort max unsupported)")


def test_the_lowering_is_bounded_at_one_per_pin(monkeypatch):
    """Never "retry with fewer constraints until something answers". With both pins
    gone there is nothing left to drop, so a third refusal ends the seat."""
    turn, seen = _seat_with(monkeypatch, [
        (None, f"exited 1 ({GATEWAY_404})"),
        (None, f"exited 1 ({GATEWAY_EFFORT})"),
        (None, f"exited 1 ({GATEWAY_EFFORT})"),
    ])
    assert len(seen) == 3, "it must stop, not loop"
    assert turn.skip


#: A refusal that is settled but is NOT about either pin — so there is nothing left
#: to lower and no reason to try again. `is_rejection` matches the explicit status.
SETTLED_OTHER = '{"status":400,"error":{"type":"invalid_request_error"}}'


def test_a_failed_fallback_still_reports_the_pin_that_could_not_be_served(monkeypatch):
    """When the lowered seat is refused too the seat is lost anyway — but WHY has to
    survive, or the next reader sees only the second failure and re-diagnoses the
    pin from scratch."""
    turn, seen = _seat_with(monkeypatch, [
        (None, f"exited 1 ({GATEWAY_404})"), (None, f"exited 1 ({SETTLED_OTHER})")])
    assert len(seen) == 2, "a settled non-pin refusal is not a flake — no extra go"
    assert turn.skip and turn.model_unavailable == "gpt-5.6-luna"


def test_a_flake_after_lowering_buys_one_more_go_and_only_one(monkeypatch):
    """The recovered seat was getting zero flake tolerance, which loses the vendor by
    the other road.

    The PINNED attempt gets `run_cli`'s three tries; the lowered one is called with
    `attempts=1`, so a single 500 threw away the seat this whole path exists to keep.
    One more go — once per SEAT, not once per lowering, or a two-pin host turns one
    flake into an unbounded walk."""
    turn, seen = _seat_with(monkeypatch, [
        (None, f"exited 1 ({GATEWAY_404})"),   # pin refused -> lower the model
        (None, "exited 1 (boom)"),             # not a refusal -> one more go
        ("findings here", None),               # and it works
    ])
    assert len(seen) == 3
    assert turn.reply == "findings here"
    assert turn.model_unavailable == "gpt-5.6-luna"

    turn, seen = _seat_with(monkeypatch, [
        (None, f"exited 1 ({GATEWAY_404})"),
        (None, "exited 1 (boom)"),
        (None, "exited 1 (boom again)"),
    ])
    assert len(seen) == 3, "the flake allowance is once per seat, not per failure"
    assert turn.skip


def test_an_unrelated_failure_does_not_fall_back(monkeypatch):
    """Narrow on purpose: a general "retry with fewer constraints" would review on a
    weaker seat for reasons nobody chose."""
    turn, seen = _seat_with(monkeypatch, [(None, "codex: timed out after 1800s")])
    assert len(seen) == 1, "a timeout is not a pin problem"
    assert turn.model_unavailable == "" and turn.effort_unsupported == ""


def test_an_unpinned_seat_has_nothing_to_fall_back_from(monkeypatch):
    """Already the CLI default, so a 404 there is not a pin problem and re-running
    the identical argv would be the futile retry this PR removes."""
    turn, seen = _seat_with(monkeypatch, [(None, f"exited 1 ({GATEWAY_404})")],
                            model="", effort="")
    assert len(seen) == 1
    assert "--model" not in seen[0][0]
    assert turn.model_unavailable == ""


def test_only_codex_falls_back_because_only_its_argv_can_ask_for_a_default(monkeypatch):
    """A real limit, not caution — and the defect the first draft shipped.

    Lowering a pin means rebuilding the argv without it, which needs a seat whose
    argv can SAY "use your default": `codex_args("")` omits `--model`. claude takes
    `--model` unconditionally and would be handed an empty string; `agy` builds its
    argv before any failure exists to react to. Running the fallback for them
    re-sends the identical bad value and then labels it as a fallback — a false
    record on top of a futile retry. Codex caught this in review."""
    turn, seen = _seat_with(monkeypatch, [(None, f"exited 1 ({GATEWAY_404})")],
                            model="opus", seat="claude", effort="")
    assert len(seen) == 1, "claude must not be sent through the codex fallback"
    assert turn.model_unavailable == ""
    assert "--model" in seen[0][0] and "opus" in seen[0][0]


# ------------------------------------------- the seams the review said nothing covered

def test_run_cli_composes_the_diagnostic_so_the_banner_cannot_win(monkeypatch):
    """End to end through the REAL composition, which nothing exercised before.

    `run_cli` joins stderr and the lifted envelopes and hands that to `stderr_gist`.
    The ordering puts the progress banner FIRST, which is only safe because
    `stderr_gist` takes the last of the best rank — so the two are correct together
    or not at all, and a test that builds `diag` by hand (as every other test here
    does) cannot tell. The #219 review flagged exactly this gap, and the comment on
    the composition claimed an end-to-end assertion that did not exist.
    """
    class _Proc:
        returncode, stdout, stderr = 1, GATEWAY_STDOUT, GATEWAY_STDERR

    monkeypatch.setattr(panel_seats.subprocess, "run", lambda *a, **k: _Proc())
    out, err = panel_seats.run_cli(["codex", "exec"], "codex (gpt-5.6-luna, max)",
                                   attempts=1, stdin_text="x")
    assert out is None and err
    # The sentence a human reads names the cause, not the banner.
    assert "stdin" not in err, f"the progress banner won the summary: {err}"
    assert "404" in err or "deployment" in err.lower(), err
    # And the full diagnostic travels with it, which is what the fallback classifies on.
    diag = panel_seats.failure_diag(err)
    assert "Reading prompt from stdin" in diag, "stderr must still be carried"
    assert panel_seats.is_model_unavailable(diag), (
        "the composed diagnostic must classify, or the pin lowering never fires")


def test_seat_label_names_the_configuration_when_the_seat_produced_nothing():
    """The rendering logic both reports share, and the case the duplicated inline
    copy got wrong (#219 F15).

    A seat that lowered its pin and then failed anyway must NOT be named by its
    substitute: `codex (CLI default; pinned … unavailable)` reads as though the CLI
    default reviewed, when nothing did. Truthful about the pin, misleading about the
    brain — and the Seats line exists to say which brain settled the question.
    """
    ran = panel_core.ReviewerRun(model_unavailable="gpt-5.6-luna")
    ran_ok = panel_core.ReviewerRun([_f()], model_unavailable="gpt-5.6-luna")
    skipped = panel_core.ReviewerRun(skip="codex: exited 1", model_unavailable="gpt-5.6-luna")

    assert panel_seats.seat_label("codex", "gpt-5.6-luna", "max", None) == (
        panel_seats.reviewer_label("codex", "gpt-5.6-luna", "max")), (
        "no result yet — name it by what it was configured with")
    assert "CLI default" in panel_seats.seat_label("codex", "gpt-5.6-luna", "max", ran_ok), (
        "it reviewed on the substitute, so the substitute is what to name")
    assert "CLI default" not in panel_seats.seat_label("codex", "gpt-5.6-luna", "max", skipped), (
        "it produced nothing — naming the substitute claims a review that never happened")
    assert "gpt-5.6-luna" in panel_seats.seat_label("codex", "gpt-5.6-luna", "max", skipped)
    del ran


def test_both_reports_render_seats_through_the_one_helper():
    """A round and an ask must not disagree about who reviewed.

    Pinned as source, not behaviour, because the failure it guards is a SECOND copy
    of the expression appearing — which no assertion about one report's output can
    see. The first duplicate was written wrong."""
    import panel as panel_mod
    import panel_ask
    for mod in (panel_mod, panel_ask):
        src = Path(mod.__file__).read_text()
        assert "seat_label(" in src, f"{mod.__name__} does not use the shared helper"
        assert "fallback_label(" not in src, (
            f"{mod.__name__} builds a seat label itself — that is the duplication "
            f"seat_label exists to remove, and the copy is where the bug was")


def test_the_effort_is_lowered_first_when_it_is_what_was_refused(monkeypatch):
    """The tie-break's OTHER branch, which the first test pass left uncovered.

    A servable model whose effort is refused must drop the effort and keep the
    model — the reverse of the daedalus ordering. This is the branch
    `is_model_unavailable`'s effort-wins guard exists for, so it is the one worth
    asserting keeps the model."""
    turn, seen = _seat_with(monkeypatch, [
        (None, f"exited 1 ({GATEWAY_EFFORT})"),
        ("findings here", None),
    ], model="gpt-5.5")
    assert len(seen) == 2
    assert "--model" in seen[1][0] and "gpt-5.5" in seen[1][0], (
        "the model was servable and must survive — only the effort was refused")
    assert "model_reasoning_effort" not in " ".join(seen[1][0])
    assert turn.effort_unsupported == "max" and turn.model_unavailable == ""


# ------------------------------------------- exit 0 carrying nothing but a refusal (#219 R2)

#: A codex stream that refused: the benign events it always prints, and the error.
#: `thread.started`/`turn.started` are why the first version of the predicate could
#: never fire — it demanded everything outside the ERROR events be blank.
ERRORS_ONLY_STREAM = "\n".join([
    '{"type":"thread.started","thread_id":"01a0"}',
    '{"type":"turn.started"}',
    '{"type":"error","message":"unexpected status 404 Not Found: The API deployment '
    'for this resource does not exist."}',
])

#: The same stream, having also produced a reply. Not an error-only stream.
STREAM_WITH_REPLY = ERRORS_ONLY_STREAM + '\n{"type":"item.completed","text":"a finding"}'


def test_a_stream_of_nothing_but_a_refusal_is_a_failure_not_an_empty_reply():
    """The branch that turns exit 0 into a failure, in both directions.

    Untested when it shipped, and it is the one addition that can LOSE a seat that
    would otherwise be reported as a degraded success — so the false-positive side
    matters as much as the true-positive one."""
    assert panel_seats.stdout_is_only_errors(ERRORS_ONLY_STREAM), (
        "a stream whose only outcome is a refusal is a failed run, not a reply "
        "that failed to parse")
    assert not panel_seats.stdout_is_only_errors(STREAM_WITH_REPLY), (
        "it replied — the error alongside must not discard the reply")


def test_the_predicate_cannot_mistake_a_reply_for_an_error_stream():
    """The false-positive side, which is where the cost is asymmetric: a wrong YES
    throws away a working seat's whole reply, and there is no salvage path behind
    it. A reply is not a typed event stream, and that is the discriminator."""
    assert not panel_seats.stdout_is_only_errors(
        '{"findings":[{"severity":"P2","title":"error handling is wrong"}]}'), (
        "a findings array has no `type` — it is a reply, whatever words are in it")
    assert not panel_seats.stdout_is_only_errors(
        'I could not review this. The CLI printed:\n'
        '{"type":"error","message":"boom"}\n'
        'which I could not interpret.'), (
        "prose around a quoted envelope is a reply that quotes an envelope")
    assert not panel_seats.stdout_is_only_errors(""), "nothing at all is not a refusal"
    assert not panel_seats.stdout_is_only_errors(
        '{"type":"turn.completed","usage":{}}'), "no error in it at all"


def test_the_predicate_is_asked_of_every_seat_including_the_one_it_was_written_for():
    """It was gated to `replied is None` — the seats that never emit this shape —
    while codex, the seat it exists for, answered a different question one branch up
    and never reached it. Dead in both directions at once (#219 review).

    Driven through the real `run_cli` rather than asserted against the source, so the
    wiring is what is pinned and not a grep."""
    class _Proc:
        returncode, stdout, stderr = 0, ERRORS_ONLY_STREAM, ""

    for replied in (None, lambda: True):
        import unittest.mock as _m
        with _m.patch.object(panel_seats.subprocess, "run", lambda *a, **k: _Proc()):
            out, err = panel_seats.run_cli(["codex", "exec"], "codex (x)",
                                           attempts=1, replied=replied)
        assert err, f"exit 0 with only a refusal must fail the run (replied={replied!r})"
        assert "reported only errors" in err, err
        assert "404" in panel_seats.failure_diag(err), (
            "and the diagnostic must reach the classifiers, or no pin can be lowered")


def test_a_brace_inside_prose_is_not_lifted_as_an_event():
    """Scanning the whole text for `{` bought a false positive for the false negative
    it fixed: prose quoting an envelope — a stack trace, a reviewer's reply, this
    file's own fixtures — was handed to the decoder as though the CLI emitted it.
    These values suppress retries and drop pins, so that is not cosmetic (#219
    review, codex). The `{` now has to open a line."""
    embedded = 'the CLI said {"type":"error","message":"404 deployment does not exist"} once'
    assert panel_seats.error_text(embedded) == "", (
        "an envelope quoted mid-sentence was never emitted as an event")
    assert not panel_seats.is_deterministic_failure(panel_seats.error_text(embedded))
    indented = '  {\n    "type": "error",\n    "message": "404 deployment does not exist"\n  }'
    assert panel_seats.error_text(indented), (
        "an indented pretty-printed body still opens its line and must survive — "
        "that is the false negative the scan was widened for")


def test_a_lowered_attempt_gets_the_remaining_budget_not_a_fresh_one(monkeypatch):
    """The elapsed guard bounds when a lowering may START. Without passing the
    remainder as the attempt's own timeout, one starting just under the line runs a
    full CLI_TIMEOUT past it — a whole timeout of overshoot per lowering, on a guard
    whose stated job is bounding the seat (#219 review, codex)."""
    seen = []
    monkeypatch.setattr(panel_seats.shutil, "which", lambda _c: "/usr/bin/codex")

    def fake_run_cli(args, label, **kw):
        argv = args() if callable(args) else args
        seen.append(kw.get("timeout"))
        if len(seen) == 1:
            return None, f"exited 1 ({GATEWAY_404})"
        Path(argv[argv.index("--output-last-message") + 1]).write_text("ok")
        return "{}", None

    monkeypatch.setattr(panel_seats, "run_cli", fake_run_cli)
    panel_seats.run_seat("codex", "gpt-5.6-luna", "review this", "max")
    assert seen[0] is None, "the pinned attempt takes run_cli's own default"
    assert seen[1] is not None, "the lowered attempt must be given a bounded timeout"
    assert seen[1] <= panel_seats.FALLBACK_MAX_ELAPSED_S
    assert seen[1] >= panel_seats.FALLBACK_MIN_TIMEOUT_S, (
        "and never so small that it is a kill dressed as a review")
