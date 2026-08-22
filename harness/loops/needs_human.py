"""The one door a decision owed to a human leaves the fleet by — #274.

The harness stops and says *a human has to answer this* in four places, and
until now each one reported somewhere different: ``epic.py`` printed its ruling
to stdout, ``preland`` returned an exit code, a fixer wrote a PR comment, a panel
seat wrote prose into a JSONB list. Four destinations, none of them the board —
the one surface a human actually watches, already carrying exactly this traffic
from every other project in the fleet. #274 measured the result: ``deferred: 0``
across sixty-five rounds and thirty days, and zero ``stuck`` posts from any part
of quarterback's own review machinery.

This module is the door. Every producer calls :func:`announce` and nothing else
decides where an escalation goes, which is the whole point: **one function, not
four call sites**. #328 proposes a ``blockers`` row as the durable store for the
same judgement, and the two are not alternatives — the row is where a blocker
lives, the post is how a human finds out one was raised. When that row lands,
this function grows a write to it and the four producers do not change at all.
A destination spread across four modules is how "two stores for one fact" gets
built by accident; a destination behind one function cannot.

**The vocabulary is not defined here.** ``app/needs_human.py`` is the one
definition (#279) and this module imports it when the repository tree is in
reach. It is not always in reach: the harness is installed by home-manager as
``share/quarterback-harness/loops`` with no ``app/`` beside it, and the loops
test sandbox copies ``harness/loops`` alone. So there is a pinned fallback for
that case, and ``tests/test_needs_human_drift.py`` — which runs where both files
are readable — fails if the two ever disagree. The import is the real path; the
fallback exists because a distribution boundary is not a reason to restate a
vocabulary, only a reason to prove the restatement is identical.

**A flag costs a reason, here as well as at the database.** #279 made the
evidence CHECK a biconditional so a bare flag is refused. :func:`announce`
follows the same rule at its own boundary: an announcement with no non-blank
reason is refused and says so, because "a human is needed" with nothing behind
it is the confident assertion #67 warns about, and it is worse on a queue than
in a column — it costs somebody an interruption.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness_rules import BOARD_TIMEOUT, board_config, ssl_context  # noqa: E402

# ------------------------------------------------------------------ vocabulary

#: The canonical definition, when this harness is running out of a checkout.
#: ``parents[2]`` because this file is ``<repo>/harness/loops/needs_human.py``.
CANONICAL = Path(__file__).resolve().parents[2] / "app" / "needs_human.py"

#: The pinned copy, for an installed harness with no ``app/`` beside it. Kept
#: honest by ``tests/test_needs_human_drift.py``, which reads this literal out of
#: the source and compares it against the imported module — a test that runs in
#: the app suite, where both halves exist. A fifth spelling of this tuple would
#: drift exactly as the four before it did, so there must never be a sixth.
_FALLBACK_CLASSES = ("decision", "taste", "ui", "environment", "auth", "other")

#: Same rule, same test. Only the two functions below read it.
_FALLBACK_REASON_MAX = 4000


def _canonical(path: Path = CANONICAL):
    """``app.needs_human`` loaded from ``path``, or ``None`` if it is not there.

    By file path rather than by package name: ``app`` is not importable from the
    harness — the two are separate distributions and always have been — but the
    module itself was deliberately written to hold no state and import nothing
    from the app, precisely so that one file can be the single definition on
    both sides of that boundary.

    Anything that goes wrong is ``None``, never an exception. This runs at import
    time of a module that every loop imports, so a half-written ``app/`` in a
    dirty checkout must cost the fallback and not the run.
    """
    try:
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location("_qb_needs_human", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001 - see docstring: never at the cost of the run
        return None
    return mod if hasattr(mod, "NEEDS_HUMAN_CLASSES") else None


_CANON = _canonical()

#: ``decision | taste | ui | environment | auth | other``, from #279.
NEEDS_HUMAN_CLASSES: tuple[str, ...] = (
    tuple(_CANON.NEEDS_HUMAN_CLASSES) if _CANON else _FALLBACK_CLASSES)

#: Whether the vocabulary above came from the canonical file or the fallback.
#: Reported rather than hidden: a producer running against the pinned copy on a
#: box whose checkout has moved on is a fact worth being able to see.
VOCABULARY_FROM = str(CANONICAL) if _CANON else "harness/loops/needs_human.py (pinned copy)"

MAX_REASON_CHARS: int = getattr(_CANON, "MAX_REASON_CHARS", _FALLBACK_REASON_MAX)


def class_or_none(v: object) -> str | None:
    """One of :data:`NEEDS_HUMAN_CLASSES`, or nothing — #279's normaliser.

    Delegated to the canonical module when it is loadable, so there is one
    implementation of "is this a class" and not two that agree today.
    """
    if _CANON:
        return _CANON.needs_human_class_or_none(v)
    if not isinstance(v, str):
        return None
    c = v.strip().lower()
    return c if c in NEEDS_HUMAN_CLASSES else None


def reason_or_none(v: object) -> str | None:
    """The reason line, trimmed and bounded, or nothing — where nothing refuses."""
    if _CANON:
        return _CANON.needs_human_reason_or_none(v)
    if not isinstance(v, str):
        return None
    return v.strip()[:MAX_REASON_CHARS] or None


def label_for(cls: str) -> str:
    """The GitHub label a class projects onto (``needs-human/ui``)."""
    if _CANON:
        return _CANON.label_for(cls)
    return f"needs-human/{cls}"


# ------------------------------------------------------------------- the door

#: Set it to ``0``, ``off``, ``no`` or the empty string and nothing announces.
#: A named switch rather than "unset the board URL", because those are different
#: intentions: a box with no board is not enrolled, a box with this set has
#: decided its escalations go somewhere else (or nowhere) for now. The test
#: suites set it off, and the two tests that exercise the door turn it back on
#: around a stubbed opener.
ANNOUNCE_ENV = "QUARTERBACK_NEEDS_HUMAN"

#: Who the post is addressed to, when the repo config does not say. A post with
#: no ``to`` still reaches the board and still answers "what is the fleet stuck
#: on"; it does not answer "what is the fleet waiting on *me* for", which is the
#: question #274 wants a count of. So this is configurable and deliberately has
#: no default — inventing an addressee would put somebody's name on a queue they
#: never agreed to hold.
ADDRESSEE_ENV = "QUARTERBACK_NEEDS_HUMAN_TO"

#: How long the board gets to take a post. Shorter than :data:`BOARD_TIMEOUT`:
#: this is a notification riding on a decision that has already been made, and
#: the producer is on its way to stopping. Fifteen seconds of a wedged board per
#: escalation is how a loop that escalates four things takes a minute to say so.
ANNOUNCE_TIMEOUT = 5

#: An announcement with the same key is not repeated inside this window. A
#: pre-land gate runs on every attempt to land and an epic driver runs on a
#: timer, so without this the first unanswerable question in a repo becomes a
#: post every few minutes — and #253's measurement (78% of what agents volunteer
#: is not work) is exactly what that turns the board into. Twelve hours, because
#: the thing being announced is a question owed to a person, and a person's
#: working day is the granularity at which repeating yourself is useful.
REPEAT_AFTER = 12 * 60 * 60

#: Where the "already said this" record lives. Under the cache dir and not the
#: config dir: losing it costs one duplicate post, which is the right failure.
SEEN_PATH = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") \
    / "quarterback" / "needs-human-announced.json"

#: The most keys kept in that file. Dropping the oldest bounds a file written by
#: every loop on the box; the cost of dropping one is a repeated post.
SEEN_MAX = 500


def enabled() -> bool:
    """Is announcing switched on for this process?"""
    raw = os.environ.get(ANNOUNCE_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in ("", "0", "off", "no", "false")


def addressee(cfg: dict | None = None) -> str | None:
    """Who to address an escalation to: the environment, then the repo config.

    Environment first, matching :func:`harness_rules.board_config`'s precedence,
    which is the fleet's rule rather than this module's preference.
    """
    env = (os.environ.get(ADDRESSEE_ENV) or "").strip()
    if env:
        return env
    block = (cfg or {}).get("needs_human")
    who = block.get("to") if isinstance(block, dict) else None
    return who.strip() if isinstance(who, str) and who.strip() else None


def digest(*parts: object) -> str:
    """A short stable hash of `parts`, for building a dedupe key.

    A key has to be narrow enough that re-running a gate on one commit says the
    same thing once, and wide enough that a NEW question on that commit is not
    swallowed by the old one. Everything that distinguishes two questions goes
    through here — the finding keys behind a panel escalation, the reasons behind
    a HOLD — so the second half is not left to whoever wrote the key.
    """
    raw = "\x00".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _load_seen() -> dict[str, float]:
    """The "already said this" record. An unreadable one is an empty one: the
    cost of losing it is a duplicate post, which is the right failure."""
    try:
        raw = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
        return {k: float(v) for k, v in raw.items()
                if isinstance(k, str) and isinstance(v, (int, float))}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def _recently_announced(key: str, now: float) -> bool:
    """Was `key` announced inside :data:`REPEAT_AFTER`?"""
    return now - _load_seen().get(key, 0.0) < REPEAT_AFTER


def _remember(key: str, now: float) -> None:
    """Record that `key` WAS announced — and only ever after it actually was.

    Split from the check, and called after the post rather than before it,
    because the two orderings fail in opposite directions and only one of them is
    survivable. Recording first means a board that refused one post suppresses
    every retry of that question for twelve hours, and the escalation is simply
    lost — which is the exact failure this whole module exists to end. Recording
    afterwards means a crash between the post and the write costs one duplicate.

    Never raises. An unwritable cache is a duplicate post, not a lost one.
    """
    seen = _load_seen()
    seen[key] = now
    fresh = {k: v for k, v in seen.items() if now - v < REPEAT_AFTER}
    if len(fresh) > SEEN_MAX:
        fresh = dict(sorted(fresh.items(), key=lambda kv: kv[1])[-SEEN_MAX:])
    try:
        SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        SEEN_PATH.write_text(json.dumps(fresh), encoding="utf-8")
    except OSError:
        pass


def _post(body: dict) -> tuple[int | None, str]:
    """``POST /post`` to this host's board. ``(post id, why-there-is-none)``.

    Written here rather than piped through ``qb`` — which is how ``panel.py``
    records a review — because ``qb`` has no post verb and lives in another
    repository. It resolves board and token through
    :func:`harness_rules.board_config`, which is the same site-config contract
    ``qb-env`` states, so an escalation cannot land on another island's board.

    Never raises. The error shapes are ``_dial_body``'s, for the same reasons it
    gives: ``HTTPError`` before ``URLError`` because a status says more than
    "unreachable", ``OSError`` rather than ``URLError`` because a reset partway
    through the read is not wrapped.
    """
    url, token, why = board_config()
    if why:
        return None, why
    req = urllib.request.Request(
        f"{url}/post", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=ANNOUNCE_TIMEOUT,
                                    context=ssl_context()) as r:
            answer = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        hint = " — the token this host resolved was refused" if e.code == 401 else ""
        return None, f"the board answered HTTP {e.code}{hint}"
    except OSError as e:
        return None, f"the board was unreachable ({e.__class__.__name__})"
    except ValueError:
        return None, "the board answered with something that is not JSON"
    got = answer.get("id") if isinstance(answer, dict) else None
    if not isinstance(got, int):
        return None, "the board accepted the post without saying which one it is"
    return got, ""


def announce(*, cls: str, reason: str, summary: str, repo: str = "",
             detail: str = "", refs: list[dict] | None = None,
             key: str = "", cfg: dict | None = None,
             session: str | None = None) -> str:
    """Tell the board a human has to answer something. Returns a line to print.

    This is the single destination for every escalation the harness raises. It
    is deliberately the only place that knows the post type, the addressee and
    the wire format, so #328's ``blockers`` row can become the store by changing
    this function and nothing else.

    Args:
        cls: one of :data:`NEEDS_HUMAN_CLASSES`. An unrecognised spelling is
            announced as ``other`` and NAMED as unrecognised, never dropped
            silently — that is #279's rule for the same value at ingest.
        reason: why no reviewer of any kind can settle it. Required: a blank one
            refuses the announcement rather than making it.
        summary: the decision in one line, for the board stream.
        repo: ``owner/name``, for the summary and the dedupe key.
        detail: the long half — options, what each costs, what the agent would
            do absent an answer.
        refs: post refs, ``{"kind": …, "value": …, "repo": …}``.
        key: dedupe key. The same key is not announced twice inside
            :data:`REPEAT_AFTER` — and it is recorded only once a post has
            actually landed, so a board outage costs a retry rather than the
            escalation. Empty means announce unconditionally, which is right for
            a thing that happens once per run and wrong for anything a loop
            re-derives. It must carry everything that distinguishes two
            questions, or a NEW one is swallowed by an old one on the same
            commit — :func:`digest` is what the producers fold that into.
        cfg: the resolved repo config, read only for its addressee.
        session: the session id to file the post under.

    Returns:
        A one-line note for the caller to print, or ``""`` when there is nothing
        worth saying — announcing is off, this box is on no board, or the same
        question was announced an hour ago. Never an exception: an escalation
        that cannot be announced must still be an escalation.
    """
    said = reason_or_none(reason)
    if not said:
        return ("needs-human NOT announced: a flag costs a reason, and this one "
                "carried none")
    known = class_or_none(cls)
    unknown = "" if known else f" (unrecognised class {str(cls)[:40]!r})"
    known = known or "other"
    if not enabled():
        return ""
    now = time.time()
    if key and _recently_announced(key, now):
        return ""
    where = f"[{repo}] " if repo else ""
    lines = [f"class:  {known}{unknown}",
             f"label:  {label_for(known)}",
             f"reason: {said}"]
    if repo:
        lines.append(f"repo:   {repo}")
    if detail.strip():
        lines += ["", detail.strip()]
    body: dict = {
        "type": "stuck",
        "summary": f"{where}needs a human ({known}): {summary}".strip()[:900],
        "detail": "\n".join(lines),
    }
    to = addressee(cfg)
    if to:
        body["to"] = to
    if refs:
        body["refs"] = list(refs)
    if session:
        body["session"] = session
    post_id, why = _post(body)
    if why:
        return f"needs-human NOT announced ({known}): {why}"
    if key:
        _remember(key, now)
    addressed = f" to {to}" if to else ""
    return f"needs-human announced on the board{addressed} as post {post_id} ({known})"
