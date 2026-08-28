"""The delegated write path — one extra header, the same host, the same bearer.

`app.auth.delegated` authorises a NAMED set of writes for an agent holding its
machine's own credential. It is not a way to be a person, and the shape of the
request is what makes that true: this goes to the ordinary agent host with the
ordinary bearer, carrying `X-Agent-Elevated` beside it. Nothing here forges
`Remote-User`, nothing asks the edge for anything, and no vhost changes.

Pinned here: the header goes out only on the two delegated calls; a rotated
secret is re-read once on a 403; and "this host has no credential" is separated
from "the board said no", because only the first is a setup step.
"""

from __future__ import annotations

import httpx
import pytest
from mcp_server.client import ELEVATED_HEADER, QuarterbackClient

BASE = "https://board.example"
# Deliberately low-entropy and obviously not a credential: gitleaks runs on this
# repo's pre-commit hook and a realistic-looking fixture trips it on every commit.
SECRET = "test-not-a-real-secret"


class Recorder:
    def __init__(self, response: httpx.Response | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response or httpx.Response(200, json={"reordered": 2})


def _counting(tmp_path) -> str:
    """A command printing a different value each call, so a retry that reused the
    cached secret would send the same header twice and be visible."""
    n = tmp_path / "n"
    # Quoted: a temp directory with a space or a metacharacter in it would
    # otherwise turn this fixture into a different command.
    return (f"n=$(cat '{n}' 2>/dev/null || echo 0); n=$((n+1)); "
            f"echo $n > '{n}'; echo v$n")


def client(rec: Recorder, *, secret=SECRET, cmd=None) -> QuarterbackClient:
    return QuarterbackClient(BASE, "tok-machine", transport=httpx.MockTransport(rec),
                             elevated=secret, elevated_cmd=cmd)


def test_a_reorder_goes_to_the_ordinary_agent_host():
    """Not a second vhost. The credential is client-supplied like the bearer, so
    the edge neither injects nor strips it and `qb.fo.ls` is the right address."""
    rec = Recorder()
    client(rec).plan_reorder({"repo": "acme/one", "order": ["a", "b"]})
    (req,) = rec.requests
    assert str(req.url) == f"{BASE}/plan/reorder"


def test_it_carries_both_the_bearer_and_the_delegated_secret():
    """The bearer says which machine; the secret says that machine may do this.
    `delegated()` looks the secret up BY the machine, so dropping either half is
    not a weaker request, it is a different one that cannot be authorised."""
    rec = Recorder()
    client(rec).plan_reorder({"order": ["a"]})
    (req,) = rec.requests
    assert req.headers["Authorization"] == "Bearer tok-machine"
    assert req.headers[ELEVATED_HEADER] == SECRET


def test_ordinary_calls_do_not_carry_the_delegated_secret():
    """A credential sent on every request is a credential in every log and every
    proxy. It rides only the two calls that need it.

    The delegated call comes FIRST on purpose: the header is set per-request, and a
    client that had instead set it on the shared session would pass a test that
    only ever made the ordinary call."""
    rec = Recorder(httpx.Response(200, json={"ok": True}))
    c = client(rec)
    c.plan_reorder({"order": ["a"]})
    c.plan({"repo": "acme/one"})
    delegated_req, ordinary_req = rec.requests
    assert delegated_req.headers[ELEVATED_HEADER] == SECRET
    assert ELEVATED_HEADER not in ordinary_req.headers, "the header leaked onto the session"
    assert ordinary_req.headers["Authorization"] == "Bearer tok-machine"


def test_no_credential_refuses_before_the_network_and_names_the_remedy():
    rec = Recorder()
    with pytest.raises(RuntimeError) as e:
        client(rec, secret=None).plan_reorder({"order": []})
    assert not rec.requests, "it must not spend a request to discover it has none"
    assert "QUARTERBACK_ELEVATED_TOKEN" in str(e.value)


def test_a_board_refusal_stays_an_http_error():
    """"Your secret is wrong" and "this host has none" are different answers and
    only one of them is a setup step."""
    rec = Recorder(httpx.Response(403, json={"detail": "does not match"}))
    with pytest.raises(httpx.HTTPStatusError):
        client(rec).plan_reorder({"order": []})


def test_item_update_uses_the_same_credential():
    rec = Recorder()
    client(rec).plan_item_update({"item_id": "x", "note": "corrected"})
    (req,) = rec.requests
    assert str(req.url) == f"{BASE}/plan/item/update"
    assert req.headers[ELEVATED_HEADER] == SECRET


# --------------------------------------------------------- the secret's own life

def test_the_command_is_not_run_until_a_delegated_write_is_attempted(tmp_path):
    """This client is constructed once per MCP session on EVERY session start and
    the command is usually `op read`, which can prompt. Resolving eagerly would
    put a credential prompt in front of every agent that starts, to serve two
    tools it will probably never call."""
    marker = tmp_path / "ran"
    QuarterbackClient(BASE, "t", elevated_cmd=f"touch {marker}; echo s").close()
    assert not marker.exists(), "constructing the client must run nothing"


def test_the_command_supplies_the_secret():
    rec = Recorder()
    client(rec, secret=None, cmd="echo from-the-store").plan_reorder({"order": ["a"]})
    assert rec.requests[0].headers[ELEVATED_HEADER] == "from-the-store"


def test_only_the_first_line_of_the_command_is_used():
    """A store that prints a warning after the value must not put it in a header —
    `qb_resolve_token` trims the same way."""
    rec = Recorder()
    client(rec, secret=None,
           cmd="printf 'good\\nWARNING: rate limited\\n'").plan_reorder({"order": ["a"]})
    assert rec.requests[0].headers[ELEVATED_HEADER] == "good"


def test_a_command_that_fails_is_not_an_exception_it_is_no_credential():
    """`op` not installed, or not signed in. Same situation as having none, and it
    must arrive as the actionable refusal rather than an OSError from a client."""
    rec = Recorder()
    with pytest.raises(RuntimeError) as e:
        client(rec, secret=None, cmd="definitely-not-a-real-binary-xyz").plan_reorder(
            {"order": []})
    assert "QUARTERBACK_ELEVATED_TOKEN" in str(e.value)


def test_a_403_re_reads_the_command_and_retries_once(tmp_path):
    """A rotated secret's first symptom is a write that worked yesterday, and
    asking the store again is the whole remedy.

    The refusal has to NAME the header — see the test below for why."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(ELEVATED_HEADER, ""))
        if len(seen) == 1:
            return httpx.Response(403, json={
                "detail": f"the {ELEVATED_HEADER} secret presented does not match"})
        return httpx.Response(200, json={"reordered": 1})

    c = QuarterbackClient(BASE, "t", transport=httpx.MockTransport(handler),
                          elevated_cmd=_counting(tmp_path))
    assert c.plan_reorder({"order": ["a"]}) == {"reordered": 1}
    assert seen == ["v1", "v2"], seen


def test_a_literal_secret_is_not_retried_because_there_is_nowhere_fresher():
    rec = Recorder(httpx.Response(403, json={"detail": "nope"}))
    with pytest.raises(httpx.HTTPStatusError):
        client(rec).plan_reorder({"order": []})
    assert len(rec.requests) == 1


def test_a_command_that_exits_non_zero_is_not_a_credential_however_much_it_prints():
    """`op` writes diagnostics to stdout on some failures. Adopting that as a
    secret produces a 403 nobody can explain, from a value that never was one — so
    the exit code decides, not the presence of output."""
    rec = Recorder()
    with pytest.raises(RuntimeError) as e:
        client(rec, secret=None, cmd="echo '[ERROR] could not read secret'; exit 1"
               ).plan_reorder({"order": []})
    assert not rec.requests
    assert "QUARTERBACK_ELEVATED_TOKEN" in str(e.value)


def test_a_failed_refresh_drops_the_stale_secret_rather_than_replaying_it(tmp_path):
    """The cached value has already been refused once. Keeping it lets the next
    call sail past the "have I got one" check and replay the same rejected secret;
    dropping it turns that into the actionable refusal instead of a second 403."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403, json={
            "detail": f"the {ELEVATED_HEADER} secret presented does not match"})

    # Prints once, then fails — a store that went away after the first read.
    once = tmp_path / "once"
    cmd = f"if [ -f {once} ]; then exit 1; fi; touch {once}; echo first"
    c = QuarterbackClient(BASE, "t", transport=httpx.MockTransport(handler),
                          elevated_cmd=cmd)
    with pytest.raises(httpx.HTTPStatusError):
        c.plan_reorder({"order": ["a"]})
    assert len(calls) == 1, "a refresh that produced nothing must not be retried"

    with pytest.raises(RuntimeError) as e:
        c.plan_reorder({"order": ["a"]})
    assert "QUARTERBACK_ELEVATED_TOKEN" in str(e.value), "the stale value was kept"


def test_a_403_about_the_ACT_is_not_retried_as_a_stale_credential(tmp_path):
    """`delegated()` is not the only thing that answers 403 on these paths — the
    board refuses the act too (dropping an item, writing an exemption marker).
    Re-reading 1Password to ask the same question again is useless and misleading."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(ELEVATED_HEADER, ""))
        return httpx.Response(403, json={
            "detail": "dropping or reopening an item is a person's decision"})

    c = QuarterbackClient(BASE, "t", transport=httpx.MockTransport(handler),
                          elevated_cmd=_counting(tmp_path))
    with pytest.raises(httpx.HTTPStatusError):
        c.plan_item_update({"item_id": "x", "state": "dropped"})
    assert len(seen) == 1, "a refusal of the ACT must not be retried"
    # And the store was asked exactly once — a client that re-read the secret and
    # then declined to retry would send one request too, and would still be
    # spending an `op read` (which can prompt) on a refusal it cannot fix.
    assert (tmp_path / "n").read_text().strip() == "1", "the secret store was re-read"


def test_item_update_gets_the_same_retry_and_refusal_behaviour(tmp_path):
    """Both delegated calls share one path; only one of them was exercised for it."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(ELEVATED_HEADER, ""))
        if len(seen) == 1:
            return httpx.Response(403, json={
                "detail": f"the {ELEVATED_HEADER} secret presented does not match"})
        return httpx.Response(200, json={"ok": True})

    c = QuarterbackClient(BASE, "t", transport=httpx.MockTransport(handler),
                          elevated_cmd=_counting(tmp_path))
    assert c.plan_item_update({"item_id": "x", "note": "n"}) == {"ok": True}
    assert seen == ["v1", "v2"]

    rec = Recorder()
    with pytest.raises(RuntimeError):
        client(rec, secret=None).plan_item_update({"item_id": "x"})
    assert not rec.requests


# ------------------------------------------- concurrency on the shared client (#498)


def _run_log(tmp_path):
    """A secret command that APPENDS one line per invocation, and a reader for it.

    Counting with `n=$(cat f); echo $((n+1)) > f` does not work here and the first
    version of this test used it: four concurrent shells all read 0 and all write 1,
    so the instrument reports "ran once" for a race it is supposed to be detecting.
    The command is also deliberately SLOW, because `op read` is — network, and
    sometimes an authorisation prompt — and a fetch faster than thread startup
    serialises by accident and proves nothing.
    """
    log = tmp_path / "runs"
    return (f"sleep 0.4; echo run >> '{log}'; echo v$(wc -l < '{log}' | tr -d ' ')",
            lambda: sum(1 for _ in log.open()) if log.exists() else 0)


def test_concurrent_callers_refused_the_same_secret_fetch_it_once(tmp_path):
    """`op read` can prompt, so N concurrent retries must not become N prompts for
    one logical fetch.

    Measured against the unfixed client: four callers, command ran **4 times**.
    With the lock and the compare-and-swap: **1**. The later callers pass the value
    they were refused, find the cache has already moved past it, and take the new
    one without running anything.
    """
    import threading
    cmd, runs = _run_log(tmp_path)
    c = QuarterbackClient(BASE, "t", elevated="stale", elevated_cmd=cmd)
    barrier = threading.Barrier(4)
    out: list[str | None] = []

    def go():
        barrier.wait()
        out.append(c._resolve_elevated(stale="stale"))

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)

    assert runs() == 1, f"one logical fetch ran the command {runs()} times"
    assert out == ["v1"] * 4, out


def test_a_request_carries_the_secret_ITS_caller_resolved(tmp_path):
    """`_send_delegated` takes the secret as an argument and never reads it off
    `self`, so a concurrent rotation cannot change a request already being built.

    Honest about what this is: a guard against reintroduction, not a reproduction.
    The window in the old code was two statements wide and the header dict is built
    before `post` is entered, so a threaded test could not reliably enter it — the
    first version of this test claimed to and passed against the unfixed client,
    which made it worse than no test. This asserts the property that removes the
    window instead of trying to time it.
    """
    import inspect
    sig = inspect.signature(QuarterbackClient._send_delegated)
    assert "secret" in sig.parameters, "the secret must be passed, not read off self"
    src = inspect.getsource(QuarterbackClient._send_delegated)
    assert "self._elevated" not in src, (
        "_send_delegated reads the shared cache; a concurrent clear can empty the "
        "header of a request another caller already authorised")


def test_a_refresh_that_finds_nothing_new_is_not_retried(tmp_path):
    """`_resolve_elevated` returns the same value when there was nothing fresher,
    and replaying it would spend a second request to be refused identically."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(ELEVATED_HEADER, ""))
        return httpx.Response(403, json={
            "detail": f"the {ELEVATED_HEADER} secret presented does not match"})

    c = QuarterbackClient(BASE, "t", transport=httpx.MockTransport(handler),
                          elevated="same", elevated_cmd="echo same")
    with pytest.raises(httpx.HTTPStatusError):
        c.plan_reorder({"order": ["a"]})
    assert len(seen) == 1, "an unchanged secret was replayed"


# ---- the cache, and the way past it (prisonblues/nix-fleet#50) ---------------
#
# The fleet now hydrates this credential to /run/op-secrets at boot, so the
# ordinary command stopped being `op read` and became `if [ -s <file> ]; then cat
# <file>; else op read …; fi`. That is right for the hot path — `op read` against
# a desktop integration blocks on a prompt when the vault session is cold, which
# is always, because this credential is read about once a fortnight.
#
# It is wrong for the retry above, and silently so: re-running a file-backed
# command returns the same bytes for ever, `fresh != secret` is never true, and
# the one mechanism that existed to survive a rotation stops firing without
# anything failing. The bearer has had QUARTERBACK_TOKEN_REFRESH_CMD for exactly
# this reason since long before; this is that split, for this credential.


def test_a_cached_command_alone_cannot_survive_a_rotation(tmp_path):
    """The defect, pinned as the reason the refresh command exists. A command
    that reads a file — which is what the fleet now generates — hands back the
    same value on the re-read, so the retry is correctly suppressed and the
    caller is left with the 403."""
    cached = tmp_path / "cache"
    cached.write_text("stale\n")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(ELEVATED_HEADER, ""))
        return httpx.Response(403, json={
            "detail": f"the {ELEVATED_HEADER} secret presented does not match"})

    c = QuarterbackClient(BASE, "t", transport=httpx.MockTransport(handler),
                          elevated_cmd=f"cat '{cached}'")
    with pytest.raises(httpx.HTTPStatusError):
        c.plan_reorder({"order": ["a"]})
    assert seen == ["stale"], "a file-backed command cannot refresh itself"


def test_the_refresh_command_goes_past_the_cache_and_the_retry_fires(tmp_path):
    """The fix. The ordinary command keeps reading the boot cache; the refresh
    command reaches the store, and only after the board has refused the cached
    value by name."""
    cached = tmp_path / "cache"
    cached.write_text("stale\n")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        got = request.headers.get(ELEVATED_HEADER, "")
        seen.append(got)
        if got == "rotated":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(403, json={
            "detail": f"the {ELEVATED_HEADER} secret presented does not match"})

    c = QuarterbackClient(BASE, "t", transport=httpx.MockTransport(handler),
                          elevated_cmd=f"cat '{cached}'",
                          elevated_refresh_cmd="echo rotated")
    assert c.plan_reorder({"order": ["a"]}) == {"ok": True}
    assert seen == ["stale", "rotated"], seen


def test_the_cold_fetch_still_uses_the_cache(tmp_path):
    """The refresh spelling is for a re-read after a refusal and nothing else. A
    first fetch that reached the vault would spend the prompt this whole change
    exists to avoid."""
    cached = tmp_path / "cache"
    cached.write_text("from-the-cache\n")
    seen: list[str] = []

    c = QuarterbackClient(BASE, "t",
                          transport=httpx.MockTransport(
                              lambda r: (seen.append(r.headers.get(ELEVATED_HEADER, "")),
                                         httpx.Response(200, json={"ok": True}))[1]),
                          elevated_cmd=f"cat '{cached}'",
                          elevated_refresh_cmd="echo should-not-be-run")
    c.plan_reorder({"order": ["a"]})
    assert seen == ["from-the-cache"], seen


def test_without_a_refresh_command_the_ordinary_one_is_still_re_read(tmp_path):
    """A host whose ordinary command already reaches the store — no sidecar, or a
    literal — must keep the behaviour it had. The fallback is the old path."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        got = request.headers.get(ELEVATED_HEADER, "")
        seen.append(got)
        if got == "v2":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(403, json={
            "detail": f"the {ELEVATED_HEADER} secret presented does not match"})

    c = QuarterbackClient(BASE, "t", transport=httpx.MockTransport(handler),
                          elevated_cmd=_counting(tmp_path))
    assert c.plan_reorder({"order": ["a"]}) == {"ok": True}
    assert seen == ["v1", "v2"], seen
