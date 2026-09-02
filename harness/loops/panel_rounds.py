"""Synthesis and rounds: merging the seats' reports into one ruling per defect,
and everything about a round's place in a cycle — baselines, provenance, scope,
and the stop.

Split out of `panel.py` (#129). A MOVE, not a rewrite.

Callers that stayed in `panel.run()` still reach these through `panel`'s own
namespace, so `setattr(panel, "adjudicate", …)` keeps working. Anything called
from INSIDE this module is patched here instead — see panel_core's docstring for
why that distinction is not optional.
"""

from __future__ import annotations

from panel_core import *            # noqa: F401,F403
import panel_core                   # noqa: F401
from panel_seats import *           # noqa: F401,F403
import panel_seats                  # noqa: F401
from panel_scope import *        # noqa: F401,F403  — re-exported for callers
import panel_scope               # noqa: F401

# Named directly, and BELOW the star imports on purpose: `escalated: Iterable[str]`
# has to still mean `collections.abc.Iterable` the day one of those modules
# re-exports the name, and the last import wins. Placed above, the guarantee would
# have been a claim about another module's current contents rather than a property
# of this file.
from collections.abc import Iterable, Mapping # noqa: E402
# Named here for the same reason, and used by exactly one check: a baseline's
# recorded finish has to be a FINITE instant, and `json` parses a bare `Infinity`.
import math                                   # noqa: E402
# Same rule again. `hashlib` mints an obligation's key (#547) and `MappingProxyType`
# is what lets :class:`CoverageRuling` default to an empty mapping without the
# shared-mutable-default hazard a bare `{}` on a NamedTuple would carry.
import hashlib                                # noqa: E402
# #555: asked of the `needs_human` this box actually has, because a harness on PATH
# can be older than this file and an unexpected keyword would cost the escalation.
import inspect                                # noqa: E402
from types import MappingProxyType            # noqa: E402
from typing import NamedTuple                 # noqa: E402

# ----------------------------------------------------------------------------- synthesis

def cluster_findings(llm_findings: list[Finding]) -> list[list[Finding]]:
    """Cluster findings that are plainly the same observation, as a HINT for the
    judge — never as the decision about what is a duplicate.

    Same file, and lines within ``CLUSTER_WINDOW`` of a neighbour already in the
    cluster. Two details matter, because the previous version got both wrong:

    * It is a real window over sorted lines, not ``line // 10``. A fixed grid is
      not a distance: lines 39 and 41 (two apart) landed in different buckets
      while 40 and 49 (nine apart) shared one, so whether two findings merged
      depended on where they fell relative to arbitrary multiples of ten.
    * It keys on the full path, not ``Path(f.file).name``. Same-named files in
      different directories (``api/tests/test_x.py`` and ``web/tests/test_x.py``)
      are not the same file, and merging them is the opposite error.

    What no line arithmetic can catch is the case that actually recurs: two
    reviewers describing ONE defect and citing lines 100 and 41 for it. That is
    a semantic judgement, which is why this only pre-clusters and the judge
    decides — see :func:`adjudicate`.

    Findings with no line at all cluster per file: it is the most that can be
    said about them positionally, and the judge sees them individually anyway.
    """
    by_file: dict[str, list[Finding]] = {}
    for f in llm_findings:
        by_file.setdefault(f.file, []).append(f)
    out: list[list[Finding]] = []
    for findings in by_file.values():
        # Sort is stable, so reviewers keep their arrival order within a line.
        ordered = sorted(findings, key=lambda f: (f.line is not None, f.line or 0))
        cur: list[Finding] = []
        last: int | None = None
        for f in ordered:
            if cur and (f.line is None) == (last is None) and (
                    last is None or f.line - last <= CLUSTER_WINDOW):
                cur.append(f)
            else:
                if cur:
                    out.append(cur)
                cur = [f]
            last = f.line
        if cur:
            out.append(cur)
    out.sort(key=lambda grp: min(f.severity for f in grp))
    return out


def _account(f: Finding) -> str:
    """One reviewer's account of a finding, joined for READING: title — detail.

    A presentation field, and only that. The structured pair travels beside it
    (``title``/``detail`` per report in :meth:`Canonical.as_dict`), because a
    consumer cannot split this string back apart — an em dash is a punctuation
    mark reviewers use — and the panel promises the account is kept, not that it
    is recoverable from a rendering of it. This is the text that used to be
    discarded when a positional merge chose a representative, taking with it the
    observations only one reviewer made."""
    return " — ".join(x for x in (f.title, f.detail) if x)


def _first_human(reports: list[Finding]) -> Finding | None:
    """The first account carrying a WHOLE escalation, or none.

    Whole, because a class and a reason are one declaration: taking the class
    from one member and the reason from another manufactures a statement neither
    of them made, and the board would store it as a single seat's. `panel_core`
    already refuses a half — a flag without both is not a flag — so in practice
    this is the first flagged account, and saying it this way keeps that true if
    the parser is ever loosened.
    """
    return next((f for f in reports
                 if f.needs_human and f.needs_human_class and f.needs_human_reason),
                None)


def _human_report(group: list[Finding]) -> dict:
    """One reviewer's escalation fields as the board's `reported_by` wants them."""
    head = _first_human(group)
    return {"needs_human": head is not None,
            "needs_human_class": head.needs_human_class if head else "",
            "needs_human_reason": head.needs_human_reason if head else ""}


def _fold_reports(reports: list[Finding]) -> list[dict]:
    """The accounts as they are SERIALISED: one entry per reviewer.

    A judge may merge two findings from the same reviewer — that is the panel's
    own motivating example (one defect, two line numbers) — and the board stores
    accounts under a ``(finding, reviewer)`` uniqueness constraint, keeping the
    first and dropping the rest. So a reviewer's several accounts are joined
    here, where nothing is lost, rather than at ingest, where the second one
    would vanish. Its severity is the worst it gave and its line the first."""
    order: dict[str, list[Finding]] = {}
    for f in reports:
        order.setdefault(f.reviewer, []).append(f)
    out = []
    for reviewer, group in order.items():
        head = group[0]
        bodies = [head.detail] + [_account(f) for f in group[1:]]
        out.append({
            "reviewer": reviewer,
            "severity": min(f.severity for f in group),
            "line": next((f.line for f in group if f.line is not None), None),
            "title": head.title,
            "detail": "\n\n".join(b for b in bodies if b),
            "account": "\n\n".join(_account(f) for f in group),
            # THIS reviewer's own re-review declaration, which is the grain the
            # board scores it at: a member that called the structural fix and one
            # that missed it are the same row otherwise.
            "needs_rereview": any(f.needs_rereview for f in group),
            # Same grain, same reason, one level sharper: a flag is a way OUT of
            # work, so the rate at which each seat reaches for it has to be on
            # the seat's own row or #67's rule is unenforceable. The class and
            # the reason travel with the flag and are never assembled half from
            # one account and half from another — `_first_human` says why.
            **_human_report(group),
        })
    return out


_NOT_WORD = re.compile(r"[^a-z0-9]+")


def _norm_title(title: str) -> str:
    """A title reduced to the words in it, which is what the defect key hashes and
    what the round diff compares. Must match ``app/api/reviews.py::_derive_key``
    and migration 0012's SQL, character for character."""
    return _NOT_WORD.sub(" ", (title or "").lower()).strip()


def _key_from_title(file: str | None, title: str) -> str:
    """The defect key for a title already chosen — the recipe itself, shared by
    :func:`_defect_key` and the round baselines, which read a title back out of a
    payload rather than off a Finding."""
    return hashlib.md5(f"{file or ''}|{_norm_title(title)}".encode(),
                       usedforsecurity=False).hexdigest()[:16]


#: What a finding key looks like — :func:`_key_from_title` writes exactly 16 hex
#: characters, and the board's `_derive_key` writes the same. The range is wider
#: than that on purpose: a hand-written baseline or a future digest length should
#: not be rejected, while anything carrying markdown, a newline or a paragraph of
#: prose should be.
_KEY_RE = re.compile(r"[0-9a-f]{8,64}")


def _key_norm(value: object) -> str:
    """A key as the register stores it: surrounding whitespace gone, lower-cased.

    Applied wherever a key arrives from OUTSIDE — a caller's ``--escalated`` and a
    baseline's register — and applied before :func:`_is_key` decides, because this
    is the one input read out of a fixer's PROSE report and retyped by a human or
    an orchestrator. ``DEADBEEFDEADBEEF`` and a copy-paste carrying a trailing
    newline both NAME the right finding; rejecting them produced a note blaming
    the caller for a value a human reads as correct and left the escalation
    uncounted, which is the #221 jam with a misleading diagnostic on top.

    It cannot admit anything ``_KEY_RE`` would not: case and surrounding blanks
    are all it touches, and :func:`_key_from_title` writes lower-case hex — so the
    normalised value is the one that matches a finding, and it is what the
    register must store."""
    return str(value).strip().lower()


def _is_key(value: object) -> bool:
    """Is this the shape a finding key comes in?

    Keys reach the loop from a caller's ``--escalated``, which a human or an
    orchestrator read out of a fixer's PROSE report — the one input here that was
    never machine-generated. An unchecked value is interpolated into a
    ``config_notes`` line that ``--post`` puts in a public PR comment, and the
    same value is written into the payload every later round inherits, so the
    shape is checked at the door rather than at the point of use.

    Judged on the NORMALISED value (:func:`_key_norm`), which every caller of this
    must then store rather than the raw one."""
    return isinstance(value, str) and bool(_KEY_RE.fullmatch(_key_norm(value)))


def _key_gist(value: object, limit: int = 24) -> str:
    """A malformed key, cut and flattened to something safe to name in a report.

    The note has to say WHICH value was rejected or it cannot be acted on, and it
    is posted to the PR — so everything that is not plainly a key character
    becomes ``?``, and the result is short. Quoting the raw value would put a
    caller's arbitrary string, markdown and all, into a public comment.

    ASCII alphanumerics only, and that restriction is the whole point of the
    excerpt: ``str.isalnum`` is true for letters and digits in every script, so a
    value made of Cyrillic or Greek homoglyphs (or full-width digits) came through
    verbatim and rendered on the PR as a plausible-looking key — safe as markdown,
    and useless for the one job the excerpt has, which is letting a human
    recognise WHICH value was wrong."""
    flat = "".join(ch if (ch.isascii() and ch.isalnum()) or ch in "-_." else "?"
                   for ch in str(value))
    return (flat[:limit] + "…" if len(flat) > limit else flat) or "(empty)"


#: Why a fix pass did not make a correction it had already identified (#665).
#:
#: Four words, and they are the four ways a pass can END UP not writing a patch it
#: knows is owed. `budget` is a ceiling — the growth cap, a line budget, a token
#: cap — where the pass agrees the fix is right and cannot afford it. `premise` is
#: #84/#491's case arriving one step earlier: the fix rests on an assumption the
#: pass cannot decide. `scope` is a correction that would open files the change
#: never touched, which #619 measures and #624 records. `refuted` is the pass
#: disagreeing with the finding on the merits.
#:
#: A CLOSED vocabulary, and the reason is what the word is FOR. The register's
#: whole job is that the next round does not pay to rediscover the fact, and the
#: next round's reader is briefed off these words: "priced out" and "I think this
#: finding is wrong" call for opposite next moves. An open vocabulary would let a
#: fixer write a sentence, and a sentence is exactly the prose the loop already
#: throws away.
#:
#: An unrecognised word is NOT a rejected declaration — see
#: :func:`declination_or_none`. Losing the declaration to keep the vocabulary
#: clean would be the bug this issue is about, committed in the name of the fix.
DECLINE_REASONS: tuple[str, ...] = ("budget", "premise", "scope", "refuted")

#: What a declaration carrying no usable reason word is recorded as. A real value
#: in the register and not a null: the FACT is that a pass declined a correction,
#: and that fact is worth inheriting whether or not the word beside it survived.
DECLINE_UNSTATED = "unstated"


def declination_or_none(value: object) -> tuple[str, str, str] | None:
    """Read one ``KEY:REASON`` declaration — ``(key, reason, problem)``, or None.

    ``None`` is returned for one failure and one only: the KEY half is not the
    shape of a finding key. That is the one half the register cannot do without,
    because a declaration nothing can be joined to is a row that matches no
    finding for the rest of the cycle while its caller reads the silence as the
    declaration having landed — the same failure ``--escalated`` and
    ``--acknowledge`` are both checked at the door for.

    The REASON half is different in kind and is treated differently on purpose.
    A missing or unrecognised word is recorded as :data:`DECLINE_UNSTATED` and
    NAMED in ``problem``, never dropped: this whole register exists because a
    fact one actor established was thrown away, and throwing the same fact away
    over its adjective would be that bug committed by the fix for it. What the
    refusal protects is narrower — the word is briefed to the next round's fixer
    and posted to the board, so a word nobody recognises must not travel as
    though a fixer had said it.

    ``:`` splits at the FIRST occurrence. A key is hex and cannot contain one, so
    everything after it is the reason however many more it holds — and a caller
    that pasted ``key:budget:whatever`` gets ``budget:whatever`` refused as a
    word rather than silently read as ``budget``."""
    raw = str(value)
    key, _, reason = raw.partition(":")
    if not _is_key(key):
        return None
    word = reason.strip().lower()
    if word in DECLINE_REASONS:
        return _key_norm(key), word, ""
    return (_key_norm(key), DECLINE_UNSTATED,
            (f"`{_key_gist(reason)}` is not one of {', '.join(DECLINE_REASONS)} — "
             f"the declaration was recorded with no reason (`{DECLINE_UNSTATED}`), "
             "so the next round inherits the defect but not what priced it out")
            if reason.strip() else
            (f"carries no reason — the declaration was recorded as "
             f"`{DECLINE_UNSTATED}`, so the next round inherits the defect but not "
             f"why it was left. Pass KEY:REASON, where REASON is one of "
             f"{', '.join(DECLINE_REASONS)}"))


def _defect_title(reports: list[Finding]) -> str:
    """The reporters' own words that identify the defect: the lexicographically
    first of their titles, so it does not move with report ordering, with which
    reviewer the judge picked as representative, or with a severity re-call.

    A finding no reporter titled keys on ``"(untitled)"`` rather than on the
    empty string, because that is the value the BOARD would arrive at: its
    ingest defaults a missing title to that stand-in *before* deriving a key
    (``_prepare``), and migration 0012's SQL keys off the stored column, which
    holds the same. The empty string is the intuitive choice and the wrong one —
    it produces a key no other implementation can reach, so an untitled finding
    would start a fresh chain every run."""
    titles = sorted(f.title.strip() for f in reports if f.title.strip())
    return titles[0] if titles else "(untitled)"


def _defect_key(file: str, reports: list[Finding]) -> str:
    """A stable identity for the DEFECT, sent with the finding.

    The board derives one when a caller sends none — file plus a normalised
    title — and the title it would use is the judge's freshly-worded synthesis,
    which is re-written on every run. Deriving it here from reviewer-authored
    text instead is what lets a re-review of the same PR join the same chain
    ("was this actually fixed?"), rather than starting a new one because the
    judge chose different words for the same bug.

    The title used is the lexicographically first of the reporters' own titles
    (see :func:`_defect_title`). Best-effort by nature — a reviewer that re-words
    its own title still breaks the chain, which is what
    :meth:`Baseline.raised_before` exists to absorb — but the reviewers' words are
    the most stable text a run produces.

    The hash MUST match ``app/api/reviews.py::_derive_key`` (and migration
    0012's SQL): a run that sends this key and an older run that let the board
    derive one only join if the two agree. "Agree" means against the board's
    whole path, not against that one function — its ingest defaults an untitled
    finding to ``"(untitled)"`` before deriving, so comparing with a raw
    ``_derive_key(file, "")`` measures a call the board never makes and
    "fixing" the mismatch is how the two silently diverge."""
    return _key_from_title(file, _defect_title(reports))


def _finding_id(pr: int, n: int) -> str:
    """``1609-F03`` — this finding, in this run. Run-LOCAL by construction: the
    numbering follows output position, so the same defect gets a different number
    on any rerun whose ordering, grouping or dismissals differ. It exists to
    resolve `related` within one payload, which is why the defect's own identity
    is a separate field (see :func:`_defect_key`)."""
    return f"{pr}-F{n:02d}"


@dataclass
class Canonical:
    """One real issue, as the judge settled it — the panel's only finding record.

    Merging is ADDITIVE: ``synthesis`` is the judge's new merged statement and
    ``reported_by`` carries every reviewer's original report beside it — its own
    title and detail as fields, not welded into one string, so a consumer gets
    back what the reviewer wrote rather than a rendering of it.
    Nothing a reviewer wrote is dropped to make a merge, which is what a
    representative-and-discard dedup did and why tightening its key would have
    made the loss worse rather than better.
    """

    id: str
    severity: str
    file: str
    line: int | None
    #: The one-line statement of the issue: the judge's merged one where it
    #: merged, else the reporting reviewer's own title. A line, never a body —
    #: the board stores it as the finding's `title` and derives from it, so a
    #: 4 KB unparsed-reply dump belongs in `detail`, not here.
    synthesis: str
    #: confirmed | dismissed | unjudged | sonar (the hard gate's own issues,
    #: which never reach the judge)
    verdict: str
    #: The body behind the synthesis. The judge writes one merged sentence and no
    #: body, so a merged record takes the worst report's — every reporter's own
    #: text rides along in `reported_by` either way.
    detail: str = ""
    reported_by: list[Finding] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    rationale: str = ""
    #: The judge's answer to #67's question: does this finding show the fix that
    #: preceded it was built on a wrong assumption (``invalidates``), is it a
    #: different defect (``separate``), or can it not be told (``unclear``)?
    #: ``""`` when the question was not put — a round 1, or a round with no
    #: readable earlier round — which is a different state from ``unclear`` and
    #: must stay one.
    #:
    #: On the record rather than added at serialisation time, where
    #: ``new_this_round`` and ``provenance`` ride: those two are facts about this
    #: run's comparison against a baseline, so a ``Canonical`` carrying them would
    #: have to be told about a baseline to know its own shape. This is not — it is
    #: what the judge SAID about this finding, arriving in the same reply as the
    #: severity and the synthesis, and dropping it here would mean parsing the
    #: judge's answer twice.
    premise_verdict: str = ""

    @property
    def reviewers(self) -> list[str]:
        """Who reported it, in arrival order. Attribution is a FIELD here, not an
        inference from a merge that already threw the evidence away."""
        return list(dict.fromkeys(f.reviewer for f in self.reported_by))

    @property
    def key(self) -> str:
        """This defect's identity across runs — see :func:`_defect_key`."""
        return _defect_key(self.file, self.reported_by)

    @property
    def rereview_by(self) -> list[str]:
        """Which members declared that fixing this needs the RESULT read again.

        Derived, not stored: every reporter's own :class:`Finding` is right here,
        so the attribution is simply read off it. The merge used to reconstruct
        this onto a representative finding — carefully, because the
        representative was one of the group's own members and setting its flag
        first would have credited its reviewer with somebody else's
        declaration. There is nothing to reconstruct now, and nothing to get
        wrong."""
        return sorted({f.reviewer for f in self.reported_by if f.needs_rereview})

    @property
    def needs_rereview(self) -> bool:
        """Did ANY reporter say so. One reviewer seeing that the fix will be
        structural is the observation; the others not saying so is not a
        contradiction of it."""
        return any(f.needs_rereview for f in self.reported_by)

    @property
    def needs_human(self) -> bool:
        """Did ANY reporter say no diff can settle this. Same rule as above and
        for the same reason: one member recognising a design question is the
        observation, and the others reviewing the code as code is not a
        contradiction of it."""
        return any(f.needs_human for f in self.reported_by)

    @property
    def needs_human_by(self) -> list[str]:
        """Which members escalated. Read off the reporters, never reconstructed:
        this is what #279 scores `human_flagged` from, and a flag credited to
        everyone who happened to raise the same defect makes the member that saw
        the design question and the member that missed it one row."""
        return sorted({f.reviewer for f in self.reported_by if f.needs_human})

    @property
    def needs_human_class(self) -> str:
        """The class the finding is filed under — the first WHOLE declaration.

        Two members may disagree about the class of one defect. The board keeps
        both, per reporter, in `reported_by`; the finding-level value has to be
        one of them, and taking the first is the same arrival-order rule
        `reviewers` already uses. It is never blended: see :func:`_first_human`.
        """
        head = _first_human(self.reported_by)
        return head.needs_human_class if head else ""

    @property
    def needs_human_reason(self) -> str:
        """The reason belonging to :attr:`needs_human_class`, from the same
        account. The pair is one statement and is never split."""
        head = _first_human(self.reported_by)
        return head.needs_human_reason if head else ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "key": self.key,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "synthesis": self.synthesis,
            "detail": self.detail,
            "verdict": self.verdict,
            "reported_by": _fold_reports(self.reported_by),
            "reviewers": self.reviewers,
            "needs_rereview": self.needs_rereview,
            "rereview_by": self.rereview_by,
            "needs_human": self.needs_human,
            "needs_human_class": self.needs_human_class,
            "needs_human_reason": self.needs_human_reason,
            "needs_human_by": self.needs_human_by,
            "related": self.related,
            "rationale": self.rationale,
            "premise_verdict": self.premise_verdict,
        }


def _unmerged(f: Finding, pr: int, n: int, verdict: str, rationale: str = "") -> Canonical:
    """A single reviewer's finding as a canonical record, no judge involved.

    Its title and detail stay in their own fields rather than being joined into
    the synthesis: the board stores the synthesis as the finding's title and
    keys off it, so joining them would put a whole detail body — up to
    RAW_DETAIL_CHARS of it, for an unparsed reply — into a title column and into
    the defect key."""
    return Canonical(id=_finding_id(pr, n), severity=f.severity, file=f.file,
                     line=f.line, synthesis=f.title, verdict=verdict,
                     detail=f.detail, reported_by=[f], rationale=rationale)


def _judge_listing(clusters: list[list[Finding]],
                   budget: int = MAX_LISTING_CHARS) -> tuple[str, list[Finding]]:
    """The findings as the judge sees them: one numbered line per REVIEWER
    account, with the pre-clustering offered as a hint underneath.

    Individually, because the judge cannot merge what it was shown already
    merged — the previous listing gave it one line per positional bucket, so the
    duplicates it *did* spot (its own output said "duplicate of [12]") were ones
    it had no verb to act on. Returns (listing, flat) where `flat[i]` is the
    finding the judge knows as `[i]`.

    Budgeted, because the whole prompt is ONE argv entry and Linux caps that at
    128 KiB: a panel of four reviewers each allowed RAW_DETAIL_CHARS would
    otherwise fail the review outright with E2BIG. Long accounts are cut first,
    then whole lines — and `flat` still holds every finding, numbered as the
    judge sees it, so an omitted report is simply never claimed and survives as
    unjudged rather than disappearing."""
    flat: list[Finding] = []
    groups: list[range] = []
    for grp in clusters:
        start = len(flat)
        flat.extend(grp)
        if len(grp) > 1:
            groups.append(range(start, len(flat)))

    lines: list[str] = []
    used = 0
    for i, f in enumerate(flat):
        said = _account(f)
        if len(said) > LISTING_ACCOUNT_CHARS:
            said = said[:LISTING_ACCOUNT_CHARS] + " …[account truncated]"
        line = (f"[{i}] {f.severity} {f.file}:{f.line or '?'} "
                f"(reported by {f.reviewer}) — {said}")
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
    shown = len(lines)

    hints = [", ".join(f"[{i}]" for i in rng if i < shown) for rng in groups]
    hints = [h for h in hints if "], [" in h]
    if hints:
        lines.append("\nSame file and adjacent lines (a hint, not a ruling — merge only "
                     "if they are genuinely the same defect): " + "; ".join(hints))
    if shown < len(flat):
        lines.append(f"\n({len(flat) - shown} further report(s) omitted — the listing "
                     f"hit its {budget:,}-character budget. They are KEPT as unjudged "
                     "findings; rule only on what is above.)")
    return "\n".join(lines), flat


def _parse_verdicts(parsed: list, flat: list[Finding], pr: int,
                    asked: bool = False) -> list[Canonical]:
    """Turn the judge's reply into canonical findings.

    Defensive in one direction only: a malformed reply must never SUPPRESS a
    finding. Records naming no valid account are dropped (they attribute to
    nobody and would credit a reviewer that said nothing); an account claimed
    twice stays with the first record that claimed it, since two canonical
    findings sharing one account would double-count it in every per-reviewer
    statistic; and anything the judge never mentioned survives as its own
    unjudged record.

    A dropped verdict is SAID (on stderr), never merely dropped: a judge that
    answers with `"members": ["F01"]` — its own issue labels where report numbers
    belong — loses every merge it made, and without a word about it the run reads
    exactly like one where the judge found no duplicates.

    ``asked`` is whether #67's recurrence brief was in the prompt. A `premise` key
    on a reply to a prompt that never put the question is not an answer to it: the
    model volunteered a word, about a fix pass it was shown nothing of, and
    recording it would put a fabricated verdict in the one column whose value is
    that `unclear` and "not asked" stay apart. Default False, so a caller that has
    not thought about it records nothing rather than something.
    """
    out: list[Canonical] = []
    claimed: set[int] = set()
    links: list[tuple[Canonical, str | None, list]] = []   # (record, judge's id, its `related`)
    dropped: list[str] = []
    for v in parsed:
        # The same rule the reviewers' path applies to a findings entry: the
        # prompt's own example verdict, handed back whole, rules on nobody's
        # authority — it would claim reports 0 and 3, synthesise "the merged
        # statement of the issue" and mark them real. Ranking dropped it and
        # parsing kept it, and the two must not disagree about what a verdict is.
        if not _is_answer(v, "verdicts"):
            continue
        # dict.fromkeys: one verdict listing the same report twice must not
        # credit its reviewer twice.
        members = list(dict.fromkeys(
            i for i in _member_ids(v.get("members"))
            if 0 <= i < len(flat) and i not in claimed))
        if not members:
            if v.get("members"):
                dropped.append(f"{v.get('id') or '?'}: members={v.get('members')!r}")
            continue
        claimed.update(members)
        accounts = [flat[i] for i in members]
        rep = min(accounts, key=lambda f: f.severity)      # P1 < P2 < P3 lexically
        # The judge writes a one-line synthesis and no body, so the body is the
        # worst report's own. Falling back to the whole joined account instead
        # would put a detail — up to RAW_DETAIL_CHARS of it — in the synthesis,
        # which the board stores as the title.
        synthesis = str(v.get("synthesis") or v.get("title") or "").strip()
        c = Canonical(
            id=_finding_id(pr, len(out) + 1),
            severity=_severity(v.get("severity"), rep.severity),
            file=str(v.get("file") or rep.file),
            line=v.get("line") if isinstance(v.get("line"), int) else rep.line,
            synthesis=synthesis or rep.title,
            verdict=_ruling(v.get("real")),
            detail=rep.detail,
            reported_by=accounts,
            rationale=str(v.get("reason") or v.get("rationale") or "").strip(),
            # #67, and only ever present when the recurrence brief was in the
            # prompt — a judge that was never asked cannot answer, and a stray key
            # on a round-1 reply normalises to "" like any other unreadable value.
            premise_verdict=_premise_verdict(v.get("premise")) if asked else "",
        )
        out.append(c)
        rel = v.get("related")
        links.append((c, str(v["id"]) if v.get("id") is not None else None,
                      rel if isinstance(rel, list) else []))

    if dropped:
        print(f"panel: judge verdict(s) named no valid report id and were dropped "
              f"(their findings are kept, unjudged): {'; '.join(dropped)}",
              file=sys.stderr)

    # `related` is resolved from the judge's own ids to ours, and only within
    # this reply: a link to something that is not here names nothing.
    #
    # An id the judge used TWICE resolves to nothing rather than to whichever
    # record happened to be built last: a link is a claim about which finding,
    # and a wrong one sends the fixer to unrelated code. Ids are compared as
    # strings, so `1` and `"1"` are one identifier — which is the point, since
    # the judge that writes both means one issue; a genuine clash is caught here
    # as the duplicate it looks like.
    seen: dict[str, str | None] = {}
    for c, jid, _ in links:
        if jid is not None:
            seen[jid] = None if jid in seen else c.id
    ambiguous = sorted(k for k, v in seen.items() if v is None)
    if ambiguous:
        print(f"panel: judge reused issue id(s) {', '.join(ambiguous)} — `related` "
              "links naming them left unresolved", file=sys.stderr)
    by_judge_id = {k: v for k, v in seen.items() if v}
    for c, _, rel in links:
        c.related = sorted({by_judge_id[str(r)] for r in rel
                            if str(r) in by_judge_id} - {c.id})

    # Never suppress: a finding the judge skipped is kept, unruled.
    for i, f in enumerate(flat):
        if i not in claimed:
            out.append(_unmerged(f, pr, len(out) + 1, "unjudged", "unjudged"))
    return out


#: How many of the previous round's findings the recurrence brief lists, and how
#: much of each one's title. The brief is a QUESTION about the round, not a second
#: copy of it: past a couple of dozen complaints the judge is being asked to hold
#: two whole reviews at once, and the tail buys nothing the head did not. Cut is
#: SAID, for the reason every other cut in this file is said — a judge that is
#: shown fifteen of forty complaints and told so can answer `unclear`; one that is
#: shown fifteen and told nothing answers `separate` about a premise it never saw.
MAX_RECURRENCE_FINDINGS = 25
RECURRENCE_TITLE_CHARS = 200


def recurrence_brief(fixed: list[tuple[str, str, str, int | None, str]],
                     round_no: int | None) -> str:
    """#67's extra question for the judge, or ``""`` when there is nothing to ask.

    Empty whenever the previous round asked its fixer for nothing this can name —
    a round 1, a cycle whose baseline could not be read, a round that reviewed
    nothing. The caller then swaps :data:`panel_core.JUDGE_RECURRENCE_SLOT` for
    the empty string and the prompt is byte-identical to the one every round has
    always been given.
    """
    if not fixed:
        return ""
    shown = fixed[:MAX_RECURRENCE_FINDINGS]
    lines = [f"- [{sev}] {file}{f':{line}' if line else ''} — "
             f"{title[:RECURRENCE_TITLE_CHARS]}{'…' if len(title) > RECURRENCE_TITLE_CHARS else ''}"
             for _key, sev, file, line, title in shown]
    if len(fixed) > len(shown):
        lines.append(f"- (+{len(fixed) - len(shown)} more, not listed — if the assumption you "
                     "are looking for might be among them, answer `unclear`)")
    return RECURRENCE_BRIEF.format(
        prior_round=f"round {round_no}" if round_no else "the previous round",
        prior_findings="\n".join(lines))


# ------------------------------------------------------------------ #547's two cases
#
# A `could_not_assess` line answers whichever of three questions the reader brings
# to it, and until #547 they all produced the same artefact: a veto line.
#
#   DILIGENCE  — a seat did not open a file it could have opened.
#   CAPABILITY — no seat here could have settled it: it needs a running database,
#                a browser, a deployed system, data this checkout does not carry.
#   EVIDENCE   — has anything actually CHECKED the claim? (#546 answers that one,
#                off `ci_status`, and nothing here touches it.)
#
# The first impugns the round. The second is not a statement about the round at
# all — it is a statement about what kind of instrument a panel of models reading a
# diff IS, and it is true of every PR about runtime behaviour this repo will ever
# open. Left as a veto it is `coverage_veto`'s own forbidden constant, one round in
# every one, and an unsatisfiable gate is a gate that gets dropped.
#
# The split is the JUDGE's, because the judge is already asked to adjudicate exactly
# these declarations (`JUDGE_PROMPT`'s `coverage_note`) and was already doing it well
# in prose that decided nothing. What #547 changes is that it now answers in NUMBERS
# against a list this file minted, so the ruling is typed rather than parsed out of
# wording — the rule every exemption in `coverage_veto` keeps.
#
# **A ruling on its own exempts nothing, and that is the load-bearing property.** A
# capability limit does not become "fine"; it becomes a NAMED OBLIGATION, which goes
# on vetoing until a human acknowledges it by key. So the model half of this can
# only ever change what a veto line SAYS. It cannot remove one, it cannot author a
# confident stop, and the incentive gradient that would otherwise point at declaring
# everything unresolvable arrives at a longer ledger rather than a shorter one.

#: What an obligation's key looks like, and it deliberately does NOT look like a
#: finding key (`_is_key`: 8-64 bare hex). An obligation is not a finding — it has
#: no severity, no file, no reporter and no fix — and the two vocabularies meet in
#: `panel.py`'s argument parser, where `--escalated` and `--acknowledge` sit two
#: lines apart. A prefix nothing else uses is what stops one being passed to the
#: other and silently matching nothing.
CLAIM_KEY_PREFIX = "uc-"
CLAIM_KEY_RE = re.compile(rf"^{CLAIM_KEY_PREFIX}[0-9a-f]{{12}}$")


def _claim_norm(claim: str) -> str:
    """A claim reduced past the ways one round's judge and the next round's spell
    the same sentence: case, run-together whitespace, a trailing full stop.

    Crude ON PURPOSE, and its limit is stated rather than papered over. It absorbs
    spelling, not rewording: a judge that says the same thing in different words next
    round mints a different key, and the acknowledgement the human already gave does
    not carry. That is the same limit `--escalated` has lived with since #221 —
    `panel-review-pr.md` §5 says a re-worded premise under a new key "happens very
    often" — and it is handled the same way, by reporting the mismatch (an
    acknowledged key no obligation this round carries is a NOTE, not silence) rather
    than by matching prose, which is the thing this whole design refuses to do."""
    return " ".join(claim.lower().split()).strip(" .;:,!?")


def claim_key(claim: str) -> str:
    """The stable id of an unverifiable claim, derived from the claim itself.

    Content-addressed rather than positional, which is what makes it survive the
    round: two rounds that raise the same claim raise it under the same key, so one
    acknowledgement discharges it for the rest of the cycle instead of being re-asked
    every round — the permanent HOLD this issue exists to remove, arriving by a
    different door.

    Twelve hex characters behind a word prefix, and every part of that is deliberate.
    It is SHORT because a human types it back in an `--acknowledge` flag off a PR
    comment, and it is not a bare 16-hex digest because those read as an API key to
    every secret scanner — the reason `panel-review-pr.md` renders finding IDs into
    the report and keeps keys out of it.

    Twelve and not eight, which was the first cut. A truncated digest can collide, and
    the two consequences are not symmetrical: within one round :func:`_coverage_ruling`
    refuses a collision outright, but ACROSS a cycle an acknowledgement recorded in
    round 1 for one claim would silently discharge a different claim in round 2 — the
    one direction in this design that fails OPEN. Three more characters to type takes
    that from unlikely to negligible, and the in-round refusal below makes it
    fail-closed on top."""
    return CLAIM_KEY_PREFIX + hashlib.sha256(
        _claim_norm(claim).encode("utf-8")).hexdigest()[:12]


def is_claim_key(raw: object) -> bool:
    """Whether a string is the shape :func:`claim_key` mints."""
    return isinstance(raw, str) and bool(CLAIM_KEY_RE.match(raw.strip().lower()))


class Obligation(NamedTuple):
    """A claim this review established that nothing in it could check.

    Not a finding: there is no defect, nobody is asked to write a patch, and no
    severity applies. What it records is that the PR asserts something, that the
    panel read the assertion, and that no seat here had an instrument that could
    settle it — so the honest artefact is a question with a name on it rather than
    either a silence or a veto nobody can discharge."""

    #: :func:`claim_key` of :attr:`claim`.
    key: str
    #: The claim, in the judge's words, merged across every seat that raised it.
    claim: str
    #: What WOULD settle it, in the judge's words. Empty when it said nothing.
    reason: str


class CoverageRuling(NamedTuple):
    """What the judge said about the reviewers' declarations — both halves.

    :attr:`note` is the prose ruling `coverage_note` has always carried and is
    unchanged. :attr:`unresolvable` is #547's typed half, and it is keyed by the
    `(reviewer, declaration)` pair the panel itself minted rather than by anything
    read out of the declaration's text.

    Both defaults are the STRICT answer: no note, and no declaration exempted. A
    caller that constructs one of these without a judge behind it gets the round it
    would have got before #547 existed."""

    note: str = ""
    #: `(reviewer, declaration)` -> the obligation the judge merged it into, for
    #: every declaration it ruled unresolvable. A pair that is absent was NOT ruled
    #: — the reply was malformed, the entry named no declarations, the judge skipped,
    #: the judge is not installed — and an absent pair vetoes exactly as it did
    #: before. Silence never buys the exemption.
    unresolvable: Mapping[tuple[str, str], Obligation] = MappingProxyType({})


def _coverage_ruling(rules: tuple, numbered: list[tuple[str, str]],
                     note: str = "") -> CoverageRuling:
    """Resolve the judge's numbered ruling against the declarations it was shown.

    Every rejection here leaves a declaration UNRULED, which means vetoing. There is
    no branch that fails towards the exemption, deliberately: this function reads a
    model's answer, and the one thing a model must not be able to do on its own
    authority is take a line out of the veto list.

    A declaration claimed by more than one entry is dropped from all of them rather
    than given to the first. `JUDGE_PROMPT` asks for exactly one entry per number, so
    two claims on one number is a reply that did not answer the question asked — and
    resolving it by position would let the ORDER of a model's array decide whether a
    gap vetoes, which is the failure `_agreed` was written to end.

    An entry with no `claim` text is dropped too. #547 asks for a NAMED obligation;
    an unnamed one is a veto line deleted and nothing put in its place, which is the
    model-authored bypass Part 2 exists to prevent.

    **Two declarations that read identically are both left unruled**, and that is the
    one rule here that is not obvious. The mapping this returns is keyed by
    `(reviewer, declaration)` because that is what :func:`coverage_veto` has to look a
    gap up by — it walks seats and gap TEXT, and has no numbers. A seat that repeated
    itself (`could_not_assess: ["X", "X"]`) therefore produces one key for two
    declaration numbers, and two rulings on them would overwrite each other: the
    veto loop would suppress both gaps while only the surviving obligation reached the
    ledger, so a claim would vanish from both the veto list and the payload. That is
    exactly the disappearance Part 2 exists to make impossible, so an ambiguous pair
    is refused rather than resolved.

    **A key collision refuses the SECOND claim, for the same reason.** Two different
    claims hashing to one key would share an obligation: one of them silently absent
    from the ledger while its declarations were suppressed, and one `--acknowledge`
    discharging both. So the first claim keeps the key it minted and the second is
    left unruled, which puts its declarations back on the line they always produced.
    It cannot happen at twelve hex characters in any round a person would read, and it
    is checked anyway — the cost of checking is three lines, and the cost of not
    checking is a claim nobody can see going unanswered."""
    claimed: dict[int, int] = {}
    for i, rule in enumerate(rules):
        for d in rule.declarations:
            claimed[d] = i if d not in claimed else -1
    # A `(reviewer, gap)` pair the listing carries twice cannot be resolved to one
    # ruling, so it is resolved to none. Fail-closed: both declarations go on vetoing
    # under the line they have always produced.
    ambiguous = {pair for pair in numbered if numbered.count(pair) > 1}
    minted: dict[str, str] = {}
    out: dict[tuple[str, str], Obligation] = {}
    for i, rule in enumerate(rules):
        if not rule.unresolvable or not rule.claim:
            continue
        key = claim_key(rule.claim)
        held = minted.setdefault(key, _claim_norm(rule.claim))
        if held != _claim_norm(rule.claim):
            continue
        ob = Obligation(key, rule.claim, rule.reason)
        for d in rule.declarations:
            if claimed.get(d) != i or not 0 <= d < len(numbered):
                continue
            if numbered[d] in ambiguous:
                continue
            out[numbered[d]] = ob
    return CoverageRuling(note, MappingProxyType(out))


def reached_obligations(reviewer_meta: dict[str, dict],
                        ruling: CoverageRuling) -> tuple[Obligation, ...]:
    """The obligations this round's VETOING declarations actually raised.

    Read off the same seats and the same recorded state :func:`coverage_veto` walks,
    because an obligation may only ever stand in for a veto line that would otherwise
    have been emitted. That is what makes #547 unable to make the gate HARDER
    anywhere: a declaration that costs the round nothing today — a blind seat's, an
    absent seat's, one from a seat that never ran — cannot become an obligation, so
    the veto list after this change is a merge of a subset of the veto list before
    it, and never longer.

    Deduplicated by key and in first-seen order, so several seats raising one claim
    is one obligation and the report and payload do not depend on dict ordering."""
    out: dict[str, Obligation] = {}
    for name, meta in sorted(reviewer_meta.items()):
        if not meta.get("ran") or meta.get("code_blind"):
            continue
        for gap in meta.get("could_not_assess") or []:
            ob = ruling.unresolvable.get((name, gap))
            if ob is not None:
                out.setdefault(ob.key, ob)
    return tuple(out.values())


def adjudicate(clusters: list[list[Finding]], diff: str, model: str, pr: int,
               budget: int | None = DEFAULT_DIFF_BUDGET,
               coverage: dict[str, list[str]] | None = None,
               ci: str = "",
               code_tree: Path | None = None,
               budget_usd: float | None = None,
               recurrence: str = ""
               ) -> tuple[list[Canonical], str | None, CoverageRuling]:
    """The 'master' rules on every finding, merges the duplicates it finds, AND
    rules on the coverage the reviewers declared about themselves.

    Returns (canonical findings, skip_reason, coverage ruling). skip_reason is None
    when the judge ran (even if it dismissed nothing); otherwise it explains WHY
    it could not rule — CLI absent, timeout, crash, a zero exit that produced no
    output, or output with no JSON verdict in it — so the caller can surface that
    rather than a bare 'unavailable'. The judge inherits run_cli's empty-output
    guard for free: a judge that printed nothing now reports "produced no output"
    (with its own stderr quoted) instead of blaming the shape of a reply it never
    made.

    The coverage ruling is two extra keys in the object the judge already returns,
    so it costs no additional model call — and its own reply may still be the
    bare verdict array an earlier judge returned, in which case there is simply
    no coverage note and no ruling.

    ``coverage_note`` is the prose half and is unchanged. ``coverage_rulings`` is
    #547's typed half: the declarations are handed over NUMBERED and the judge
    answers in numbers, so which gap an entry covers is never read out of the gap's
    wording. A number the reply leaves out, contradicts itself about, or attaches to
    an entry that names no claim is left UNRULED, and an unruled declaration vetoes
    exactly as every declaration did before. Nothing in this function's failure
    modes points towards the exemption.

    ``recurrence`` is #67's question, rendered by :func:`recurrence_brief` and
    empty on every round with no earlier round to ask it about. It rides in on the
    same terms as the coverage ruling and for the same reason — one more key on a
    verdict the judge is already writing, so the sharper half of the measurement
    costs no second model call. An empty string leaves the prompt byte-identical
    to the one every round was given before this existed.

    Declarations with no findings still run the judge: that is the round where
    "clean versus could-not-tell" most needs adjudicating — two members saying
    they could not read the migration while a third reports clean is a split, and
    a finding count of zero says nothing about it.

    Merging lives here because this is the only step that reads every account and
    can write a new one. Upstream, dedup could only ever pick a survivor and
    discard the rest; the judge can say what the reviewers jointly found, and the
    originals ride along untouched in ``reported_by``.

    A real bug from a single reviewer is confirmed; only genuine false positives
    are dismissed (style and polish are kept). When the judge can't rule, every
    finding is returned unmerged and unjudged — nothing is silently suppressed.
    Neither findings NOR declarations -> ([], None, ""): nothing to rule on.
    """
    declared = {k: v for k, v in (coverage or {}).items() if v}
    if not any(clusters) and not declared:
        return [], None, CoverageRuling()
    # The listing and the diff no longer share one ceiling. They did while the
    # prompt travelled in argv and the two genuinely competed for the kernel's
    # 128 KiB; on stdin they compete only for the model's context, and the diff
    # has no cap by default. Subtracting an uncapped diff from a fixed ceiling
    # drove the listing straight to its 4,000-char floor — starving the one
    # component that is unbounded in the panel's OWN output, on exactly the runs
    # (many findings, big diff) where the judge most needs to see all of them.
    #
    # So each gets its own budget: the diff whatever was configured for it, the
    # listing MAX_LISTING_CHARS. A capped diff leaves the listing more room than
    # it asks for either way, so there is nothing left for the old arithmetic.
    diff_text = diff if budget is None else diff[:budget]
    # Numbered, one line per DECLARATION rather than one per reviewer, because the
    # number is the whole of #547's typed ruling: the judge answers with these ids and
    # this file resolves them back through `numbered`, so no step anywhere matches a
    # ruling to a gap by reading the gap's text. Joining a seat's gaps onto one line —
    # which is what this did — left nothing for a ruling to point AT.
    numbered = [(name, gap) for name, items in sorted(declared.items()) for gap in items]
    stated = "\n".join(f"- [{i}] {name} could not assess: {gap}"
                       for i, (name, gap) in enumerate(numbered)) \
        or "- (no reviewer declared a gap in its coverage)"
    listing, flat = _judge_listing(clusters, MAX_LISTING_CHARS)
    listing = listing or ("- (no findings this round — there is nothing to adjudicate "
                          "but the coverage below; return an empty `verdicts` array)")

    def unruled(reason: str, ruled: CoverageRuling | None = None
                ) -> tuple[list[Canonical], str, CoverageRuling]:
        # A judge that could not be read rules on nothing, so the ruling it carries
        # out is the note it managed and NEVER an exemption. `judge_skip` is a veto
        # line in its own right (`coverage_veto`), so a round that reaches here keeps
        # both halves of the answer: the round was not adjudicated, and every
        # declaration it was shown still costs it its confidence.
        return [_unmerged(f, pr, i + 1, "unjudged", "unjudged")
                for i, f in enumerate(flat)], reason, ruled or CoverageRuling()

    # Through the shared predicate (#222), not an inline `shutil.which`: `run()`
    # now withholds `judge_max_diff_chars` from a box with no `claude` for the same
    # reason it withholds a reviewer's budget, and the gate that decides that has
    # to be the gate that decides this. Two spellings of "is the judge here" is how
    # they come to disagree — a judge skipped as absent while `diff_budgets.judge`
    # says it was given 60,000 chars.
    if not seat_installed("claude"):
        return unruled("judge: claude CLI absent")
    # On stdin, like the reviewers, and for a sharper reason: the judge's prompt
    # is the only one with a component no budget could cover. The findings
    # listing grows with the panel's output, so a legal judge_max_diff_chars
    # plus a long panel used to cross the argv limit on its own — and a judge
    # that dies takes every finding through UNADJUDICATED, which reads like a
    # triaged review rather than like a failure.
    # The judge gets the CI result too, and that is arguably the bigger half of
    # #91: its job is dismissing false positives, and a finding contradicted by a
    # passing suite is the easiest dismissal there is. Today it could not make it.
    prompt = JUDGE_PROMPT.format(findings=listing, coverage=stated,
                                 ci=ci or ci_brief("unknown", [], "not computed for this run"),
                                 diff=diff_text)
    # The judge gets a sandbox of its own on the same reasoning as the reviewers,
    # and one sharper argument: it is the seat whose loss is worst (a judge that
    # dies takes every finding through `unjudged`), so it is the last place to
    # leave depending on the caller's shell.
    #
    # **And it gets the code on the same terms they do** (#113). This is the half
    # the reviewer change alone does not fix: the wrong findings #113 was filed
    # over were not merely raised, they were CONFIRMED. PR #90's round-2 P1 said
    # `headRefOid` was read but never added to the `gh pr view --json` field list;
    # it was already there, so it never appeared in the diff, and the reviewer
    # inferred absence from invisibility — and then a judge with the same blindness
    # had no way to check and confirmed it. On PR #64 three of six confirmed P2s
    # were conditionals from a reviewer that had DECLARED it could not assess the
    # condition, and the judge confirmed them because they are well argued. A judge
    # that can open the file is the only party in the loop positioned to catch
    # that, and dismissing a false positive is its stated job.
    with tempfile.TemporaryDirectory(prefix="panel-judge-") as tmp:
        if code_tree is not None:
            sandbox, reads_code = panel_seats.seat_checkout(code_tree, Path(tmp) / "cwd")
        else:
            sandbox, reads_code = member_sandbox(Path(tmp) / "cwd"), False
        if reads_code:
            # Told, and pinned, exactly as a reviewer seat is — the brief is what
            # stops it treating the diff as the whole record, and the pin is what
            # keeps "read" from meaning "run" in a contributor's checkout.
            prompt = prompt.replace(JUDGE_CODE_SLOT, CODE_ACCESS_BRIEF)
        else:
            # The same slot, and it had been left the one empty one (#459). The
            # judge is a claude seat with `reads_code=False` here, so `claude_args`
            # pins nothing and it holds its full default toolset in an empty
            # sandbox — the most tool-capable code-blind seat on the panel, and the
            # one whose loss is worst, since a dead judge takes every finding
            # through UNADJUDICATED. It gets told what its situation is like
            # everything else.
            prompt = prompt.replace(JUDGE_CODE_SLOT, NO_TOOLS_BRIEF)
        # #67's question, and the empty string on every round that has no earlier
        # round to ask it about — which keeps a round-1 prompt byte-identical to
        # the one it has always been. Replaced unconditionally, like the slot
        # above: an unswapped token would travel to the model as literal text.
        prompt = prompt.replace(JUDGE_RECURRENCE_SLOT, recurrence)
        args = panel_seats.claude_args(model, str(uuid.uuid4()), reads_code=reads_code,
                                      budget_usd=budget_usd if reads_code else None)
        out, err = panel_seats.run_cli(args, "judge", stdin_text=prompt, cwd=sandbox)
        if err:
            return unruled(err)
        parsed = panel_core.extract_json_value(out, "verdicts")
        if parsed is None:
            # The same one-shot reparse retry `review_llm` gets, and the judge
            # needs it more. Agreement strictly ENLARGES the set of replies that
            # resolve to None — an envelope plus a restatement of it, an envelope
            # plus a self-authored illustration, any two candidates that read
            # differently — so a failure that was rare under ranking now fires on
            # ordinary model prose. The asymmetry was the expensive part: a
            # reviewer that cannot be read costs one seat, a judge that cannot be
            # read takes EVERY finding through `unjudged` and adds the "round was
            # not adjudicated" veto. One more turn keeps the pessimistic rule
            # without paying for it with the whole adjudication.
            out2, err2 = panel_seats.run_cli(args, "judge", attempts=1, stdin_text=prompt,
                                 cwd=sandbox)
            if not err2:
                parsed = panel_core.extract_json_value(out2, "verdicts")
    note, ruled = "", CoverageRuling()
    reply = parsed if isinstance(parsed, dict) else None
    if reply is not None:
        # `"coverage_note": "..."` is what JUDGE_PROMPT asks with, not an answer to
        # it. Printed in the PR comment it reads as a coverage ruling nobody made.
        # (A reply that is nothing BUT the schema never gets here — those are not
        # candidates at all — but a real ruling can still carry the stand-in note.)
        note = str(reply.get("coverage_note") or "").strip()
        note = "" if note in SCHEMA_DECLARATIONS["verdicts"] else note
        ruled = _coverage_ruling(
            panel_core._rulings(reply.get(panel_core.COVERAGE_RULINGS)), numbered, note)
        parsed = reply.get("verdicts")
    if not isinstance(parsed, list):
        # With nothing to adjudicate, a reply that carries the coverage answer is
        # a complete answer — not a judge that failed to rule.
        #
        # "Carries the coverage answer" is checked, not assumed from there being
        # no findings. Prose, a crash-truncated reply, an object with neither key
        # — all of them used to land here as skip_reason=None, so `coverage_veto`
        # added no "the round was not adjudicated" entry and the round recorded a
        # CONFIDENT clean verdict on a judge that produced nothing. That is the
        # inversion of the guarantee this release exists for, and it fires on
        # precisely the round where the coverage split most needed adjudicating.
        answered = reply is not None and (bool(note) or "verdicts" in reply)
        if not flat and answered:
            return [], None, ruled
        return unruled("judge: no JSON verdict in output (unparseable)",
                       CoverageRuling(note))
    return _parse_verdicts(parsed, flat, pr, asked=bool(recurrence)), None, ruled


# ----------------------------------------------------------------------------- rounds



#: How alike two titles for the same file must read before the round diff treats
#: them as one defect reworded. Deliberately high: this only ever has to absorb
#: "unused import" vs "import is unused", never two genuinely different defects.
REWORD_RATIO = 0.85

#: Words that never distinguish one defect from another, so a title that has one
#: and a title that does not are still candidates for the same defect. Content
#: words are what this list must leave alone: "not" and "never" are deliberately
#: absent, since "is closed" and "is never closed" are two different defects.
_TITLE_NOISE = frozenset(
    "a an the of in on at to by is it its as be or and for with this that".split())


def _stem(word: str) -> str:
    """A word reduced past the endings a rewrite changes without changing the
    subject — "import"/"imports", "query"/"queries". Crude on purpose: it only has
    to make two spellings of one noun agree, and over-stemming two DIFFERENT words
    into one is the failure that costs a finding, so nothing here shortens a word
    to fewer than three characters.

    Plain ``-s`` is stripped before anything else, so "files" reduces to "file"
    and agrees with its own singular. An ``-es`` rule ahead of it took two
    characters off every word merely ENDING in es — "files"/"file",
    "nodes"/"node", "values"/"value" — which is the noun class review titles are
    made of, so singular and plural never matched and the reword fallback this
    exists for never fired. The cost is the other direction, "boxes"/"box", which
    is rarer in a title and costs a false "new" rather than a lost finding."""
    for suffix, repl in (("ies", "y"), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:len(word) - len(suffix)] + repl
    return word


def _same_words(a: str, b: str) -> bool:
    """Do two normalised titles differ only in how they are WORDED?

    A character-similarity ratio alone cannot answer that. Findings share long
    boilerplate and differ in one short noun — "the N+1 query in the user loop"
    against "…in the order loop" is 0.93 alike and two different defects — and
    calling those one repeat drops the second from ``new_findings`` without ever
    briefing the fixer on it. A false "new" costs a wasted round; a false "already
    raised" costs a defect, so the ambiguous case goes to "new".

    So the two must carry the same content words, up to word order and a plural:
    a word one title has and the other does not names something the other does not
    talk about, and that is a different defect however alike the two strings read.
    """
    def words(text: str) -> set[str]:
        return {_stem(w) for w in text.split() if w not in _TITLE_NOISE}

    return words(a) == words(b)


#: The severity band the cross-round block counts separately (#490). Two, not a
#: full histogram: the question a reader of that block is asking is "is this cycle
#: producing WORSE findings or merely more of them", and P1/P2 against the total is
#: the cheapest split that answers it. It is also the band `round_trigger_floor`
#: defaults to, so the count is the one that decides whether a round buys another.
TREND_SEVERE = "P2"


@dataclass(frozen=True)
class Declination:
    """A correction a fix pass identified and did not make (#665).

    The third register that travels between rounds on the baseline payload, and
    the only one of the three whose value is a PAIR rather than a round number.
    ``escalated`` (#221) and ``acknowledged`` (#547) each record that an act
    happened; this records that an act did NOT happen, and "why not" is the half
    that stops the next round paying to rediscover it — a defect the pass could
    not afford and a defect the pass thinks is not real want opposite next moves,
    and a bare key cannot tell them apart.

    Frozen, because it is inherited: a later round holding a reference to an
    earlier round's declaration must not be able to re-word it in place."""

    #: The round whose fix pass declared it. Earliest wins on a merge, for the
    #: reason the cycle id does — a caller re-passing a key it inherited must not
    #: re-date the declaration to now.
    round: int
    #: One of :data:`DECLINE_REASONS`, or :data:`DECLINE_UNSTATED` where the
    #: declaration arrived without a word this loop recognises. Never null: see
    #: :func:`declination_or_none` on why the fact outlives its adjective.
    reason: str

    def as_dict(self) -> dict:
        """The payload shape — an OBJECT, where the two older registers serialise
        a bare int.

        A second parallel register keyed the same way (``declined`` plus
        ``declined_reasons``) was the cheaper diff and was rejected: it is the
        same answer written twice with two chances to disagree, and the two halves
        would be merged by different code on every baseline read. Nesting keeps
        one entry indivisible, and :func:`_inherit_declined` still reads a bare
        int as "the round, reason unstated" so a hand-written baseline and a
        payload from before the reason existed both load."""
        return {"round": self.round, "reason": self.reason}


@dataclass
class RoundTrend:
    """One earlier round as the cross-round trend block reads it (#490).

    A round's own report states that round's figures and nothing else, and read one
    at a time a diverging cycle looks flat: 8 -> 14 -> 15 findings reads as
    converging right up until you notice the PR tripled underneath it. This is the
    row that puts the rounds beside each other.

    **Derived fresh from each baseline payload, never chained.** Every field here is
    read off fields the payload has recorded since long before this block existed —
    the finding buckets, ``provenance_counts``, ``pr_chars`` — so a cycle whose
    round 2 was skipped, or was run by a panel too old to emit a trend at all, still
    gets a complete block in round 3. Carrying a round's *computed* trend forward in
    its payload would have made the block only as long as its unbroken tail.

    Every count is nullable and none of them is defaulted to zero, because a round
    that did not measure something and a round that measured zero of it are opposite
    readings and this block exists to stop exactly that confusion. A skipped round
    reviewed nothing, so it has no finding count — printing ``0 findings`` for it
    would put the strongest possible convergence signal in the block on the strength
    of a round that never ran.
    """

    #: Which round this row is. From the payload's own ``round``, so a set of
    #: baselines with a gap in it renders the gap rather than renumbering.
    round: int
    #: Did that round review anything at all? Everything below is None when it did
    #: not — see the class docstring on why that is not zero.
    reviewed: bool
    #: Everything the round left the cycle to clear: ``to_fix`` + ``sonar_findings``,
    #: which is exactly the population :data:`panel.outstanding` counts on this run.
    #: ``dismissed`` is deliberately out — the master ruled those not real and no
    #: fixer will ever touch them, so counting them would inflate every row by the
    #: judge's own work.
    findings: int | None = None
    #: How many of those were P1 or P2 (:data:`TREND_SEVERE`). An unreadable
    #: severity counts as severe, which is :func:`panel_core.severity_at_least`'s
    #: standing asymmetry and the right direction here too: a row that under-states
    #: severity is a row that argues for another round.
    p1p2: int | None = None
    #: How many of that round's findings the round before it INTRODUCED — its
    #: ``provenance_counts["introduced"]``.
    #:
    #: None, not 0, wherever the round could not attribute (:func:`attributed`):
    #: round 1, which has no earlier fix pass; a round whose only populated bucket
    #: is ``unknown``, meaning the fix range was unreadable; and a round that
    #: reviewed nothing. ``0 introduced`` in any of those is a claim about a fix
    #: pass made from a measurement that did not happen, and it is the flattering
    #: direction.
    #:
    #: An ALL-ZERO tally is the opposite case and does read 0: the round attributed
    #: and had nothing to attribute, which is what a round of repeats looks like.
    introduced: int | None = None
    #: The size of the WHOLE PR when that round read it (:func:`_whole_pr_chars`),
    #: never the round's review target: under ``increment`` scope the target is one
    #: fix commit, and a size column that cliffs at round 2 would show the change
    #: shrinking while it grows. The same number ``max_fix_growth`` measures (#298).
    pr_chars: int | None = None
    #
    # LAST, although it reads with `findings` above: dataclass field order is a
    # constructor signature, and every positional `RoundTrend(round, reviewed,
    # findings, p1p2, introduced, pr_chars)` in this repo's suites would silently
    # re-bind two columns if a field were inserted among them. Appended, an old
    # positional call keeps meaning what it meant.
    #: How many findings that round raised that NO EARLIER ROUND HAD — its payload's
    #: own ``new_findings``, which is the count :func:`round_stop`'s rule 1 turns on.
    #:
    #: The series of these down the block is #505's rung, and it is the one column
    #: here that is not reporting-only: `not_falling_state` reads it to decide whether
    #: the cycle's new-finding count has stopped falling. Read off the payload rather
    #: than re-derived, so this round's row and the same round's row one round later
    #: are the same number by construction.
    #:
    #: None, not 0, wherever the round did not review — a skipped round records
    #: ``new_findings: 0`` by default, and read as a real zero that is the strongest
    #: possible "the count fell" in the block, from a round that raised nothing
    #: because it read nothing. None for a review-only run too, where the payload
    #: itself sends null: "raised by no earlier round" is vacuous when there was no
    #: earlier round.
    new_findings: int | None = None
    #
    # #618's three columns, APPENDED for the reason `new_findings` was: dataclass
    # field order is a constructor signature, and every positional `RoundTrend(round,
    # reviewed, findings, p1p2, introduced, pr_chars)` in this repo's suites would
    # silently re-bind columns if a field were inserted among them.
    #: What the fix pass BEFORE this round wrote, split by whether anything can check
    #: it — :func:`panel_seats.referee_split`'s three buckets, read back out of the
    #: payload's ``round_stop.unrefereed_fix``.
    #:
    #: The block already printed `introduced` — how many of a round's findings the last
    #: pass authored — and said nothing about what that pass DID. A pass that wrote 330
    #: test lines and a pass that wrote 330 production lines are not the same event,
    #: and until #618 the table rendered them identically: on lexray#1780 the fix
    #: passes after round 1 wrote 1,313 lines of which 848 were test and doc, and the
    #: trend block a reader checks for the shape of a cycle could not show it.
    #:
    #: None, never 0, wherever there was no pass to read: round 1, a round whose fix
    #: range was unreadable, a round that reviewed nothing, and a payload written
    #: before #554 recorded the split. `0 production lines` read off any of those is
    #: the same fabrication `introduced` withholds a zero to avoid — and it is the
    #: flattering direction, since it reads as a pass that wrote nothing.
    production: int | None = None
    #: The test half of that split. See :attr:`production`.
    test: int | None = None
    #: The prose half — comments, docstrings, documentation paths. See
    #: :attr:`production`.
    prose: int | None = None


def attributed(counts: object) -> bool:
    """Was a round's provenance ANSWERABLE — did the attribution run at all?

    Three states live in one ``provenance_counts`` object and only two of them are
    obvious, which is why this is a named predicate rather than a truth test:

    * ``{}`` — the question does not arise. Round 1, or no cycle. False.
    * every bucket 0 — the question was asked and there was nothing to attribute:
      a round whose findings were all repeats, or which had none. **True**, and the
      trend block prints ``0``, because that is a measurement.
    * ``unknown`` the only positive bucket — the fix range was unreadable (no commit
      recorded, a branch rewritten between rounds, an API refusal), so every bucket
      that says something about the fix pass is 0 *by failure*. False: printed as a
      number it reads "0 introduced", a claim about the fix pass made from a
      measurement that did not happen, and it is the flattering direction.

    **Not the same question the report's `of those:` line asks, and they must not be
    merged.** That line asks "is there anything worth a sentence", so it withholds on
    an all-zero tally where this returns True — a round with three repeat findings has
    nothing to say in prose and a perfectly good ``0`` to put in a column. Sharing one
    predicate would force one of the two to lie; the shared thing is the ``unknown``
    rule, which both apply.

    Defensive about its argument on `load_baseline`'s standing reason — this is read
    off a payload that may have been hand-edited or written by another version — and
    it reads only the buckets :data:`panel_scope.PROVENANCE` names, so a stray key
    cannot make an unattributable round look attributed.
    """
    if not isinstance(counts, dict):
        return False
    tally = {b: _nonneg_int(counts.get(b)) for b in PROVENANCE}
    if all(v is None for v in tally.values()):
        # `{}`, or a payload carrying nothing this recognises. Either way there is
        # no tally here, which is the first state above and not the second.
        return False
    return (any(tally[b] for b in PROVENANCE if b != "unknown")
            or not tally["unknown"])


def _nonneg_int(value: object) -> int | None:
    """A count a payload can be believed about, or None.

    :func:`_positive_int`'s sibling, and separate because the two admit different
    numbers for good reasons: a SIZE of 0 cannot be a denominator, while a COUNT of
    0 is the most interesting reading in the trend block ("nothing was introduced").
    Same refusals otherwise — a bool is an `int` in Python, and a float or a string
    arrives from a hand-edited payload.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _introduced(counts: object, findings: int | None) -> int | None:
    """How many of a round's findings the fix pass before it wrote, or None.

    Bounded by the population it is a share OF, which is the one consistency check
    available here: provenance is tallied over the very findings counted beside it,
    so `introduced` can never exceed `findings` in a payload this panel wrote. A
    hand-edited or foreign one saying otherwise is not a large measurement, it is an
    inconsistent pair — and the renderer would turn it into `20 (2000%)`, a
    percentage of a denominator the number does not belong to. Unknown is the honest
    reading, and it is the direction that cannot flatter the cycle.

    Unchecked where `findings` is itself unknown: there is no population to bound it
    against, and refusing on that would throw away the one number that did survive.
    """
    if not attributed(counts):
        return None
    n = _nonneg_int(counts.get("introduced"))
    if n is not None and findings is not None and n > findings:
        return None
    return n


def _countable(payload: dict) -> list[dict] | None:
    """Every finding an earlier round left the cycle to clear, or None where the
    payload cannot be COUNTED.

    The rest of :func:`load_baseline` reads these buckets tolerantly — a record that
    is not a mapping is skipped and the round keeps its other findings — and that is
    right for what those reads produce, which is the `keys` and `titles` sets. A
    dropped record there means one repeat is not recognised, the finding reads as new,
    and the cycle buys a round nobody needed: the safe direction.

    A COUNT cannot be tolerant in that direction. `"to_fix": "corrupt"` iterates into
    single characters, every one of them fails `isinstance(f, dict)`, and the row
    reports **0 findings** — the strongest convergence signal this block can emit,
    from a payload nothing was read out of. One malformed record among ten produces
    the same failure quietly at 9. So a bucket that is present and is not a list, or a
    list holding anything that is not a mapping, makes the counts UNKNOWN rather than
    smaller, and the row prints `?`.

    An ABSENT bucket is empty rather than unknown, which is the one tolerance kept:
    that is how every other reader in this file takes it, and it is what an older
    schema's silence means.
    """
    raised: list[dict] = []
    for bucket in ("to_fix", "sonar_findings"):
        got = payload.get(bucket)
        if got is None:
            continue
        if not isinstance(got, list) or any(not isinstance(f, dict) for f in got):
            return None
        raised.extend(got)
    return raised


def _pass_churn(payload: dict) -> dict:
    """What the fix pass before a round wrote, as `{kind: count-or-None}` (#618).

    Read back out of the payload's own ``round_stop.unrefereed_fix`` — #554's block,
    recorded on every round since it landed — rather than re-derived. The trend block's
    standing rule: this round's row and the same round's row one round later have to be
    the same numbers, and the only way to guarantee that is to read the number the
    round itself published.

    **``churn`` is the presence test and the three buckets are not.** A pass that
    genuinely wrote nothing and a round that had no pass to read both record zeros in
    every bucket, and the payload distinguishes them nowhere else — which is exactly
    why `panel.py`'s own report line gates on ``churn`` before printing the split. So a
    zero total is read as "not measured" here too, and every cell is withheld. The cost
    is a real empty fix range rendering as unknown; the alternative is printing `0
    production` for round 1, which is a claim about a fix pass that did not exist.

    Tolerant of everything, on :func:`load_baseline`'s standing rule that a bad payload
    costs a cell and never the review — and it degrades to None rather than to a
    number, because every wrong number this block can print reads as convergence."""
    stop = payload.get("round_stop")
    return churn_cells((stop or {}).get("unrefereed_fix")
                       if isinstance(stop, dict) else None)


def churn_cells(split: dict | None) -> dict:
    """One :func:`referee_state` mapping as the trend block's three cells (#618).

    Split out of :func:`_pass_churn` because the CURRENT round does not have a payload
    to read — it has the state it is about to write one from — and the two must apply
    one rule. A round's own row and the same round's row one round later disagreeing
    about what its fix pass wrote is the failure this whole block exists to prevent."""
    if not isinstance(split, dict) or not _nonneg_int(split.get("churn")):
        return {kind: None for kind in panel_seats.REFEREE_KINDS}
    return {kind: _nonneg_int(split.get(kind)) for kind in panel_seats.REFEREE_KINDS}


def _trend_row(was: int, payload: dict) -> RoundTrend:
    """Read one accepted baseline as a :class:`RoundTrend` row.

    Every read here degrades to None rather than raising or guessing, on
    :func:`load_baseline`'s standing rule that a bad payload costs a row's cell and
    never the review: this block is a reporting nicety and must never be the reason
    a round does not run. What it must never do is degrade to a NUMBER — a cell that
    is quietly small reads as a measurement, and every wrong number this block can
    print reads as convergence.

    ``reviewed`` is taken exactly as :attr:`Baseline.read_nothing` and
    :attr:`Baseline.first_reviewed` take it — truthiness of the payload's own field,
    so a payload too old to carry it reads as "not run" here, in the growth
    denominator and in the coverage record alike. One reading of one field across the
    module: a third answer here would put a row in the block that the ratio beside it
    disagrees with.
    """
    reviewed = bool(payload.get("reviewed"))
    findings = p1p2 = None
    if reviewed:
        # `dismissed` is not here — see `RoundTrend.findings`.
        raised = _countable(payload)
        if raised is not None:
            findings = len(raised)
            p1p2 = sum(1 for f in raised
                       if severity_at_least(f.get("severity"), TREND_SEVERE))
    counts = payload.get("provenance_counts")
    return RoundTrend(
        round=was, reviewed=reviewed, findings=findings, p1p2=p1p2,
        # Gated on `reviewed` for the reason the two counts above are, and this one
        # matters more than they do because #505's rung reads it: a skipped round
        # writes `new_findings: 0`, and a 0 read off it would say the count fell to
        # nothing on a round that raised nothing because it read nothing. `None`
        # instead, which breaks the streak and does not stop a cycle — the direction
        # every unknown in this module fails in.
        #
        # AND GATED ON THE PAYLOAD KNOWING ITS OWN ROUND, which the other two cells do
        # not need and this one does (found by a codex second opinion on #505). A count
        # here is a point in a SERIES, and a point needs a position: `load_baseline`
        # falls back to round 1 for a payload that does not say which round it is —
        # silently where the field is absent — so such a row would sit at `r1`, read as
        # consecutive with this run's round 2, and let the rung end a cycle off a round
        # number nobody read. `was` is still what the row RENDERS as, because the block
        # has to show the reader something; what is withheld is the number a rule acts
        # on. The `round` cell is deliberately not made to lie about it either — the
        # withheld count prints `?`, which is what "asked and not answered" already
        # means in this block.
        new_findings=(_nonneg_int(payload.get("new_findings"))
                      if reviewed and _positive_int(payload.get("round")) is not None
                      else None),
        # Gated on `reviewed` for the reason the two counts above are: a skipped
        # in-cycle round records an all-zero tally by construction, and `0
        # introduced` read off it is the same fabrication as `0 findings` — it
        # attributed nothing because it reviewed nothing.
        introduced=_introduced(counts, findings) if reviewed else None,
        # Gated on `reviewed` as well as on the field: a skipped round records
        # `pr_chars: 0` by default and `_positive_int` already refuses that, but a
        # refused round records the size of a PR it then did not review — and a
        # growth ratio computed from a round nobody read is a measurement of
        # nothing. `first_reviewed` beside it takes the same view (#298).
        pr_chars=_whole_pr_chars(payload) if reviewed else None,
        # #618's split, gated on `reviewed` for the reason every cell above is: a
        # skipped round has no fix pass in front of it that anybody read, and three
        # zeros there would say a pass wrote nothing when what happened is that
        # nothing was looked at.
        **(_pass_churn(payload) if reviewed
           else {kind: None for kind in panel_seats.REFEREE_KINDS}))


@dataclass
class Baseline:
    """What earlier rounds of THIS PR already raised."""

    keys: set[str] = field(default_factory=set)
    #: normalised title -> every file spelling it was raised against, for the
    #: reworded case. A set rather than one file: the same words about two files
    #: are two defects, and keeping only the last-seen one loses the other.
    titles: dict[str, set[str]] = field(default_factory=dict)
    #: Which earlier rounds are actually represented here, not the highest round
    #: label among them: baselines for rounds 1 and 3 are two earlier rounds, and
    #: calling that three invents one nobody ran. ``len(rounds)`` is what prints.
    rounds: set[int] = field(default_factory=set)
    #: The panel -> fix -> panel CYCLE these baselines came from, inherited from
    #: the earliest one so every round of a cycle carries the same id. None when
    #: there was no usable baseline, in which case the run mints its own.
    cycle: str | None = None
    #: The head SHA the LATEST prior round that named one reviewed. Two consumers,
    #: one commit: it is the anchor a round past the first diffs against to get the
    #: fix commit (see ``DEFAULT_ROUND_SCOPE``), and it is the far end of the range
    #: provenance attributes a new finding to. Both ask "where did the fix pass
    #: start", so a second field would be the same answer twice with two chances to
    #: disagree.
    #:
    #: The LATEST, deliberately, where ``cycle`` comes from the EARLIEST: they are
    #: two different rules over the same set and both are right. A cycle is named
    #: once and every round inherits that name, so the earliest baseline owns it.
    #: An increment is "what changed since anyone last looked", so it anchors on
    #: the most recent round — anchoring on the earliest would hand round 3 the
    #: whole of rounds 1 AND 2's work and re-review round 2's fix commit, which
    #: round 2 already read, and would attribute round 1's repairs to round 2.
    #:
    #: The latest round that SUPPLIED one, which is not the same as the latest
    #: round accepted: a newer payload naming no commit does not clear an anchor an
    #: older one gave, because an older commit we can diff against is worth more
    #: than no increment and no attribution at all.
    #:
    #: None for a payload written before the field existed, which is not an error:
    #: the round falls back to reviewing the whole PR exactly as it did then, and
    #: provenance reads "unknown" rather than attributing against an invented range.
    head_sha: str | None = None
    #: Which round supplied ``head_sha``. Usually the newest one, but not always —
    #: see above — and the briefs name that round to the reviewers, so it has to
    #: travel with the sha rather than being guessed from this run's round number.
    head_round: int | None = None
    #: EVERY accepted round's head commit, `round -> sha`, validated by the same
    #: rule ``head_sha`` is (#559).
    #:
    #: ``head_sha`` alone answers "where does the fix range START", which has one
    #: right answer and takes the latest. This answers a different question — *had
    #: this cycle already seen these exact lines?* — and for that the earlier rounds
    #: are the whole point. :func:`panel_scope.restored_lines` reads the entries
    #: BEFORE the anchor's round to tell a fix pass that restored already-reviewed
    #: code from one that wrote it, which position alone cannot do: a
    #: revert-of-a-revert adds ninety reviewed lines and a line diff calls every one
    #: of them the fixer's own work.
    #:
    #: A dict rather than a list because the round number is what selects "earlier",
    #: and empty for a cycle whose payloads all predate ``head_sha`` — which reads as
    #: "nothing to compare against" and leaves attribution exactly where it was.
    head_shas: dict[int, str] = field(default_factory=dict)
    #: What the round that supplied ``head_sha`` asked its fixer to fix — file
    #: spelling -> the finding keys raised against it. #67's other end of the
    #: recurrence chain (:func:`panel_scope._recurrence`): a new finding standing
    #: where the fix pass was working is only *circling* if that pass was working
    #: there in answer to a complaint, and this is the complaint.
    #:
    #: **From the anchor round alone**, not a union over every earlier round, and
    #: the reason is the same one ``head_sha`` gives for taking the latest rather
    #: than the earliest: the fix range under attribution is one round wide, so the
    #: only complaints it can have been answering are that round's. A union would
    #: read round 1's finding, round 3's finding and round 2's unrelated edit as a
    #: circle.
    #:
    #: **Never the dismissed bucket.** The master ruled those not real, no fixer was
    #: sent to them, and a fix pass cannot have been built on the premise of work
    #: nobody did.
    fixed_here: dict[str, set[str]] = field(default_factory=dict)
    #: The same findings as records, for the judge's brief — ``(key, severity,
    #: file, line, title)``, ordered as the payload listed them. Kept beside
    #: ``fixed_here`` rather than derived from it because the two are read by
    #: different consumers at different grains: the mechanical test wants a file
    #: index, and the judge wants sentences it can recognise the fix in.
    fixed_findings: list[tuple[str, str, str, int | None, str]] = field(default_factory=list)
    #: When the LATEST prior round that recorded one FINISHED, as epoch seconds
    #: (#192). The left-hand end of the fix phase: this round's own start is the
    #: right-hand end, and the span between them is the fixer, the verification
    #: and the push together — the half of a cycle's wall clock that nothing
    #: measured at all.
    #:
    #: Read with the same "latest round that SUPPLIED one" rule as ``head_sha``
    #: directly above, and for the same reason: a newer round written by an older
    #: panel names no finish, and taking the last payload alone would clear an
    #: answer an earlier one gave. An older left-hand end over-states the fix
    #: phase (it spans a round as well), which is why the payload also carries
    #: ``finished_round`` — an over-stated span whose ends are named is checkable,
    #: and a missing one is not.
    #:
    #: None for a payload written before the field existed, which is not an
    #: error: :func:`panel_timing.fix_phase` falls back to deriving the span from
    #: the two rounds' head commit times, and says which source it used.
    finished_at: float | None = None
    #: Which round supplied ``finished_at``. Travels with it for the reason
    #: ``head_round`` travels with ``head_sha``: the pair is quoted in the report,
    #: and a span whose earlier end came from round 1 while round 2 also ran is a
    #: different measurement from one whose ends are adjacent.
    finished_round: int | None = None
    #: Earlier rounds that recorded a head but produced no reviewer read at all —
    #: a title-skipped round, or one whose every seat failed. The anchor advances
    #: over them (a skipped round still moved the head), so a scoped round after
    #: one starts its increment AFTER code that no model looked at.
    unread_rounds: set[int] = field(default_factory=set)
    #: Earlier rounds in which some reviewer read only a PREFIX of its target.
    #:
    #: Carried because increment scope makes an old truncation PERMANENT. Under
    #: whole-PR scope a region round 1 was cut off from is read again by round 2;
    #: under increment scope round 2 only reads the fix commit, so a gap round 1
    #: had is a gap the cycle now never closes. That has to reach
    #: :func:`coverage_veto`, or the cheaper round quietly buys its saving out of
    #: coverage nobody is told it lost.
    truncated_rounds: set[int] = field(default_factory=set)
    #: Earlier rounds that read a MOVE MANIFEST rather than a diff (#138).
    #:
    #: Its own set rather than a reading of the other two, because it is a third
    #: thing. `unread_rounds` means no seat ran; `truncated_rounds` means a seat
    #: got a prefix of its target. A manifest round's seats all ran and all got
    #: their whole target — the target WAS the manifest — so neither is true of it
    #: and the code it reviewed was still read by nobody.
    #:
    #: It matters twice. It must not count as having re-read the PR (see `reread`
    #: below, whose `scope == "pr"` test a manifest round passes while having read
    #: no code at all), and under increment scope the next round's anchor steps
    #: over the code it did not read, exactly as it does past an unread round.
    manifest_rounds: set[int] = field(default_factory=set)
    #: Files that preceding round could not read in full (:func:`_diff_files_cut`).
    #: A new finding in one of them is a coverage failure, not a reviewer miss.
    unread_files: set[str] = field(default_factory=set)
    #: That round REVIEWED nothing — it was skipped. It still banks a head_sha
    #: (the next round's fix range has to start somewhere) but its empty
    #: `unread_files` then means "no coverage recorded", not "read everything":
    #: taken the second way, a skip anywhere in a cycle silently converts every
    #: later coverage failure into a reviewer miss, and erases the truncation
    #: record of the last round that did read something.
    read_nothing: bool = False
    #: Finding keys an earlier round's fixer ESCALATED instead of patching
    #: (`review-pr.md` step 3a), mapped to the round each was first declared in.
    #:
    #: Inherited rather than re-declared because an escalation is answered by a
    #: human, on their own clock, and nothing about a later round makes it stop
    #: being open. A cycle that forgot one between rounds would go straight back
    #: to counting it as work the loop can do — which is the failure this whole
    #: register exists to prevent, arriving one round later.
    #:
    #: The round is kept, not just the key, because "when was this first said"
    #: is the only thing here an auditor can check against the record: the
    #: declaration is a caller's `--escalated`, and #221's honest caveat is that
    #: the loop is trusting a report by the same agent whose fix pass produced
    #: it. Earliest wins on a merge, for the same reason the cycle id does.
    escalated: dict[str, int] = field(default_factory=dict)
    #: Obligation keys a human has ACKNOWLEDGED (`--acknowledge`, #547), mapped to
    #: the round each was first accepted in.
    #:
    #: Inherited for the same reason `escalated` is, and the reason is sharper here.
    #: An unverifiable claim does not stop being unverifiable because a round ended;
    #: a cycle that forgot the acknowledgement between rounds would put the identical
    #: question to the same person every round, which is the permanent HOLD this
    #: register exists to end, arriving one round later and wearing a discharge.
    acknowledged: dict[str, int] = field(default_factory=dict)
    #: Corrections an earlier round's fix pass identified and did NOT make
    #: (``--declined``, #665), mapped to the round and the reason each was first
    #: declared under.
    #:
    #: Inherited for the reason its two siblings above are, and the reason is
    #: different again. An escalation and an acknowledgement each record something
    #: a HUMAN did outside the loop; this records something one of the loop's own
    #: actors decided it could not do — and the loop then threw the decision away.
    #: Observed live: a fix pass declared two corrections it could not pay for
    #: under the growth ceiling, the declaration went nowhere, and the next round
    #: spent its own budget rediscovering one of them (``classify()``'s now-wrong
    #: KEEP reason) and reported it as a fresh finding. The information existed,
    #: the fixer was honest about it, and the cycle paid twice.
    #:
    #: **It subtracts from nothing.** ``escalated`` is a filter in front of all four
    #: stop rules; this is not a filter at all. A declined finding is still
    #: outstanding, still counted at every rule, still handed to the next fix pass,
    #: and still blocks a stop exactly as it did before — the register can only ever
    #: ADD a veto line and take a cycle's claim of clean convergence away. That is
    #: what makes it un-gameable by the one actor that writes it: there is no
    #: declaration a fixer can make that buys it a smaller round, a bigger budget or
    #: an easier stop.
    declined: dict[str, Declination] = field(default_factory=dict)

    #: Declination keys a human has RETRACTED (`--retract`, #674), mapped to the
    #: round the retraction was first recorded in.
    #:
    #: The fourth register of this shape and the only one that CANCELS another. A
    #: declination is what a fix pass could not do; a retraction is a person saying
    #: the correction has since been made, or was never owed. Without it #665's
    #: register is a one-way door: nothing in the loop retracts a declaration, so the
    #: veto it raises stands for the rest of the cycle and — through `confident` and
    #: `preland --require-earned-stop` — holds the landing with it.
    #:
    #: A HUMAN act, and inherited for the same reason `acknowledged` is: a later round
    #: does not make it stop being true, and re-asking would rebuild the permanent HOLD
    #: this removes. Deliberately NOT inferable from a round's own findings — an absent
    #: finding is not evidence of a repair when the round's scope never re-read the
    #: file — and never from a fix pass reporting its own success, which is the actor
    #: attesting to its own work (#622).
    retracted: dict[str, int] = field(default_factory=dict)
    #: ``(round, chars, measurement)`` of the EARLIEST accepted baseline — the
    #: denominator `review_panel.max_fix_growth` measures this round against (#165).
    #:
    #: The earliest, not the latest, and the round number travels with it because the
    #: question the ceiling asks is "how much bigger is the change now than what this
    #: cycle STARTED from". Measured against the previous round instead, a fix pass
    #: could triple the change three rounds running and clear the check every time.
    #:
    #: **A WHOLE-PR size, whatever that round REVIEWED (#298).** ``round_scope``
    #: decides what the reviewers are asked to LOOK AT; this ceiling asks how big the
    #: change has BECOME, and the second must not silently change meaning because the
    #: first was configured. So the size is read off the payload's ``pr_chars``, which
    #: every round records regardless of its scope, and off ``diff_chars`` only where
    #: the baseline's own ``scope`` is ``pr`` and the two are the same number
    #: (:func:`_whole_pr_chars`). Taking ``diff_chars`` unconditionally — as this did
    #: until #298 — put a whole PR on one end and, under the default `increment`
    #: scope, one round's fix commit on the other. That is a real quantity and not the
    #: one that runs away: PR #188 went 185 -> 593 -> 721 churned lines, 3.90x under a
    #: 3.0x ceiling, while its round-2 increment was 128 lines and nowhere near it.
    #: The guard never fired.
    #:
    #: The measurement still travels with the number, because two readings of a size
    #: exist in this payload and whatever reports a ratio has to be able to say which
    #: one it computed. It is the whole-PR one on both ends now by construction rather
    #: than by luck, and printing it is what makes that checkable from the report
    #: instead of from this comment.
    #:
    #: None where no baseline was usable, or where the earliest one records no
    #: whole-PR size — a payload written before `diff_chars` existed, a round that
    #: reviewed nothing, or an increment-scoped round written before `pr_chars`, whose
    #: `diff_chars` is a fix commit and cannot stand in for the PR. The check then
    #: does not run, and says so rather than inventing a denominator.
    first_reviewed: tuple[int, int, str] | None = None
    #: One :class:`RoundTrend` per ACCEPTED baseline, in round order — the earlier
    #: half of #490's cross-round block. This round appends its own row and renders
    #: the lot; nothing here decides anything.
    #:
    #: One row per ROUND, not per accepted payload. Two files claiming round 2 are
    #: not two rounds — the block promises per-round figures, and a column carrying
    #: two `r2` rows with different numbers cannot be read down, which is the whole
    #: of what it is for. The ambiguity is still reported (`problems`), and the row
    #: kept is the last-written of them: the same tie-break that already decides
    #: which payload supplies the anchor and the coverage record.
    trend: list[RoundTrend] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def raised_before(self, finding: Canonical) -> bool:
        """Did an earlier round raise this defect — under this key, or under a
        near-identical title in the same file?

        The key is the finding's own (:func:`_defect_key`), so this compares the
        identity the payload carries rather than re-deriving one. The title
        fallback is for the reviewer that re-words its own report between rounds:
        the key is built from the reporters' words, so any rewording would
        otherwise land a persistent defect in `new_findings` and report the fix
        as having broken something. "The same file" is suffix-aware
        (:func:`_same_file`), since a round where only the short-path reviewer
        raised the defect hashes to a different key too.

        The fallback is deliberately hard to trigger. Its two failures are not
        symmetric: a wrong "new" buys a round nobody needed, while a wrong
        "already raised" deletes a finding from the fixer's brief and can end the
        cycle on a defect nobody was told about. So a high character ratio is only
        the cheap pre-filter, and :func:`_same_words` — the two titles carrying the
        same content words, up to word order and a plural — is what decides.
        """
        if finding.key in self.keys:
            return True
        norm = _norm_title(_defect_title(finding.reported_by))
        if not norm:
            return False
        return any(
            _same_file(finding.file or "", was_file)
            and difflib.SequenceMatcher(None, norm, was).ratio() >= REWORD_RATIO
            and _same_words(norm, was)
            for was, was_files in self.titles.items()
            for was_file in was_files
        )


def _baseline_title(f: dict) -> str:
    """The title an earlier round's serialised finding is identified by: the same
    lexicographically-first reporter title :func:`_defect_key` hashed, read back
    out of the payload. Falls back to the judge's synthesis for a record that
    carries no accounts, which is the most that can be said about it."""
    titles = sorted(t for t in (str(r.get("title", "")).strip()
                                for r in f.get("reported_by") or []
                                if isinstance(r, dict)) if t)
    return titles[0] if titles else str(f.get("synthesis") or "")


#: What a commit id may look like coming off disk. The bound is on the SHAPE —
#: hex, and nothing else — because that is the whole of what this has to refuse:
#: a `/`, a `..` or a `?` in a baseline's SHA re-points the compare API path it
#: is spliced into at another repo's history, whose diff then attributes this
#: round's findings. A length floor would buy nothing on top of that (no hex
#: string of any length can re-point a path) and would reject the short
#: abbreviations git itself hands out.
_SHA_RE = re.compile(r"[0-9a-fA-F]{1,64}")


def _mtime(path: str) -> float:
    """Last-modified time, or 0 for a path that no longer reads. Used only to
    break a tie between two baselines claiming one round, never to decide
    anything on its own."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _positive_int(value: object) -> int | None:
    """A size a payload can be believed about, or None.

    Read defensively and dropped rather than coerced, on `load_baseline`'s standing
    rule that a bad payload costs a `problems` entry and never the review: a size
    that is not a positive int cannot be a denominator (0 divides, a bool is an int
    in Python, a float arrives from a hand-edited file)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _whole_pr_chars(payload: dict) -> int | None:
    """How big the PR was when an earlier round read it, or None where that round's
    payload cannot say.

    The growth ceiling's denominator (#298), and deliberately NOT ``diff_chars``
    wherever the two differ. ``diff_chars`` is the size of what a round REVIEWED, so
    under `increment` scope it is one fix commit — and a cycle whose starting size is
    a fix commit is measuring the wrong thing at the wrong end. ``pr_chars`` is the
    PR's own size on every round whatever its scope, which is the single question
    `max_fix_growth` asks.

    ``diff_chars`` is still read where the baseline's own ``scope`` says it IS the
    whole PR, which is the fallback for payloads written before ``pr_chars`` existed
    — round 1 of a cycle is `pr`-scoped by construction (there is nothing yet to be
    an increment from), so the ordinary cycle keeps its denominator across the
    upgrade. An increment-scoped payload from before then carries no whole-PR size at
    all and gets None: inventing one out of its increment is the bug this closes."""
    return _positive_int(payload.get("pr_chars")) or (
        _positive_int(payload.get("diff_chars"))
        if str(payload.get("scope") or "pr") == "pr" else None)


def _inherit(into: dict[str, int], raw: object, was: int, path, problems: list[str],
             field_name: str, act: str, shaped, norm, shape_gist: str,
             cost: str) -> None:
    """Read one `{key: round}` register out of a baseline payload into `into`.

    Two registers now travel this way — `escalated` (#221) and `acknowledged` (#547)
    — and they are the same object doing the same job: a caller's record of an act
    performed OUTSIDE the loop, by a human, which no later round makes stop being
    true. One implementation rather than two, because the half of this that matters
    is the failure handling, and two copies of failure handling is one copy that
    silently stops matching the other.

    Every branch is the same shape as the one it replaced. Both container shapes are
    accepted: this run writes an object, and a payload from before the field (or a
    hand-written one) may carry a bare list, which is attributed to the round that
    wrote it — the only answer available and never later than the truth. Anything
    else is REPORTED and not dropped silently, because an unreadable register reverts
    the cycle to the exact behaviour the register exists to prevent, and it would
    arrive with nothing said.

    ``shaped`` and ``norm`` are what keep the two registers apart. A key of the wrong
    shape is refused and named rather than stored: it would sit in the register
    forever matching nothing, and the caller would read the cycle's silence as the
    act being honoured."""
    if isinstance(raw, dict):
        declared = list(raw.items())
    elif isinstance(raw, list):
        declared = [(k, was) for k in raw]
    else:
        declared = []
        if raw is not None:
            problems.append(
                f"baseline {path} has an `{field_name}` field that is neither an "
                f"object nor a list ({type(raw).__name__}) — round {was}'s "
                f"{field_name} entries were NOT inherited, so {cost}")
    for k, when in declared:
        if not shaped(k):
            problems.append(
                f"baseline {path} carries `{_key_gist(k)}` in its `{field_name}` "
                f"register, which is not {shape_gist} — it was NOT inherited")
            continue
        # The NORMALISED key, which is what `shaped` judged and what the round's own
        # key will equal — storing the raw one would put a padded or upper-case
        # spelling in the register, matching nothing.
        key = norm(k)
        # The declaration round is the one auditable fact in a register the loop
        # otherwise takes on trust, so it is range-checked rather than coerced.
        # `bool` is excluded explicitly: it is an `int` subclass, so `True` would
        # otherwise be read as "declared in round 1". Out of range falls back to the
        # round of the payload carrying it — the same answer a bare list gets, and
        # never later than the truth.
        ok = isinstance(when, int) and not isinstance(when, bool) and 1 <= when <= was
        if not ok:
            problems.append(
                f"baseline {path} dates {act} {key} to {when!r}, which is not "
                f"a round of this cycle at or before {was} — read as round {was}, "
                "so the round shown against it is this payload's, not the "
                "declaration's")
        first = when if ok else was
        into[key] = min(first, into.get(key, first))


#: What a cycle loses when a `declined` register cannot be read. Written out once
#: rather than inline, because :func:`_inherit` takes it as a sentence and the
#: reason has to be the same one in every branch that reports a failure.
DECLINED_COST = ("a correction an earlier fix pass already declared it could not "
                 "make is rediscovered from scratch, and reported as a fresh finding")


def _inherit_declined(into: dict[str, Declination], raw: object, was: int, path,
                      problems: list[str]) -> None:
    """Read the `{key: {round, reason}}` register out of a baseline payload (#665).

    **The key and the round half go through :func:`_inherit` and are not
    re-implemented here**, which is that function's own rule applied to a third
    caller: the half that matters is the failure handling — an unreadable
    container, a key of the wrong shape, a round outside the cycle — and a third
    copy of it is a third chance for one copy to silently stop matching the
    others. Everything below the call is the half `_inherit` cannot have, because
    it is the half no other register carries.

    A value that is not an object is read as the ROUND alone, exactly as
    `_inherit` reads a bare list: a hand-written baseline saying ``{"abc…": 2}``
    means "round 2 declared this", and the reason is simply not known. It is
    recorded :data:`DECLINE_UNSTATED` and not reported, because nothing went
    wrong — the payload never claimed to carry a word.

    A reason word this loop does not recognise IS reported, and the entry is still
    inherited under :data:`DECLINE_UNSTATED`. Dropping it would lose a defect over
    its adjective, which is the failure this whole register exists to stop; and
    passing the word through would put a string no vocabulary contains into the
    next round's brief and onto the board, wearing a fixer's authority.

    Earliest round wins a collision across baselines, and the earliest round's
    REASON travels with it. The two cannot be merged separately without inventing
    a declaration nobody made: round 2 said `budget`, round 3 re-declared the same
    key as `scope`, and a register holding round 2's date beside round 3's word
    would be a sentence neither pass wrote."""
    rounds: dict[str, int] = {}
    reasons: dict[str, object] = {}
    flat: object = raw
    if isinstance(raw, dict):
        split: dict[object, object] = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                split[k] = v.get("round")
                if _is_key(k):
                    reasons[_key_norm(k)] = v.get("reason")
            else:
                # A bare round, from a hand-written baseline. `_inherit` judges
                # it; nothing is claimed about a reason nobody sent.
                split[k] = v
        flat = split
    _inherit(rounds, flat, was, path, problems, "declined", "the declaration",
             _is_key, _key_norm, "the shape of a finding key", DECLINED_COST)
    for key, when in rounds.items():
        word = reasons.get(key)
        if word is None:
            word = DECLINE_UNSTATED
        elif str(word).strip().lower() in DECLINE_REASONS:
            word = str(word).strip().lower()
        else:
            problems.append(
                f"baseline {path} declines {key} for `{_key_gist(word)}`, which is "
                f"not one of {', '.join(DECLINE_REASONS)} — the declaration was "
                f"inherited as `{DECLINE_UNSTATED}`, so the defect carries forward "
                "and what priced it out does not")
            word = DECLINE_UNSTATED
        held = into.get(key)
        if held is None or when < held.round:
            into[key] = Declination(when, word)


def load_baseline(paths: list[str], expect: dict | None = None) -> Baseline:
    """Every finding earlier rounds of this PR already raised, from their
    ``--json-file`` payloads.

    Keyed on what was RAISED, not on what was confirmed: a finding the judge
    dismissed in round 1 and a reviewer raises again in round 2 is not new
    information, and counting it as new is how a loop fails to converge.

    The key is READ from the payload rather than re-derived: the panel has sent
    one with every finding since the merge moved into the judge, and it is the
    same identity the board chains runs on. A payload that carries none (a
    hand-written baseline, a record from before the field) falls back to the same
    recipe over the reporters' titles.

    A baseline that cannot be read is reported rather than swallowed. Its absence
    makes every finding look new, which reads as "the fix broke things" — the
    exact opposite of the truth — so the caller marks the round's verdict
    unearned instead of quietly believing it. Every defect in a payload is
    downgraded to a ``problems`` entry for that reason — including a malformed
    ``round``, which used to raise out of ``run()`` and kill a review after the
    diff had been fetched and every reviewer CLI had been paid for.

    ``expect`` (``github``/``pr``/``round``) is checked against what the
    payload says it is. A baseline from another PR is not a thinner baseline, it
    is a wrong one: its keys would make real findings read as repeated and stop
    the loop early, so a mismatched payload is REPORTED and its keys dropped. An
    identity field the CALLER knows must be present as well as equal: a
    hand-edited or truncated payload that omits it is a payload nobody can
    attribute, and accepting it suppresses this run's findings on the word of a
    file that never said whose it was. A field the caller does not know (``None``
    in ``expect``) is not checked at all — testing key *presence* instead made
    ``{"repo": None}`` reject every baseline ever written, which silently
    no-opped the whole round diff for any caller that did not resolve its repo
    name. The same reported-and-excluded rule covers a payload whose round is not
    earlier than this one's, since a current or future round's keys make
    genuinely new findings read as repeated.

    All usable baselines must belong to ONE cycle, and the earliest of them names
    it. Two concurrent cycles on a PR have unrelated keys and titles; merging them
    into one history classifies findings only the other cycle raised as repeats,
    which can suppress a fix round — the exact confusion the ``cycle`` id was
    minted to prevent, so a payload from a different cycle is reported and
    excluded like any other wrong baseline.

    A round past the first with NO baseline at all is itself a problem: every
    finding then reads as new, ``prior_rounds`` prints zero, and the round would
    otherwise be free to record a *confident* verdict about a comparison it never
    made."""
    b = Baseline()
    #: Rounds that re-read the whole PR with nothing cut — see the end of the loop.
    reread: set[int] = set()
    want = dict(expect or {})
    if "round" in want:
        # Normalised once, and never raised out of: this function's rule is that a
        # bad input costs a problems entry, not a review that every reviewer CLI
        # has already been paid for.
        try:
            want["round"] = int(want["round"])
        except (TypeError, ValueError):
            del want["round"]
    if not paths and want.get("round", 1) > 1:
        b.problems.append(
            f"round {want['round']} ran with no --baseline — nothing to compare against, "
            "so every finding here reads as one no earlier round raised")
    #: (round, cycle, path, payload) of each baseline that passed identity and
    #: ordering. Collected before anything is merged, because which cycle the run
    #: belongs to is a property of the SET — the earliest round names it, and the
    #: rest are checked against that.
    usable: list[tuple[int, str, str, dict]] = []
    for path in paths:
        try:
            payload = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as e:
            b.problems.append(f"baseline {path} unreadable ({e.__class__.__name__})")
            continue
        if not isinstance(payload, dict):
            b.problems.append(f"baseline {path} is not a panel payload")
            continue
        # Only the fields whose expected value is KNOWN. `k in want` would check a
        # key the caller passed as None, and then reject every payload for not
        # matching a value nobody has.
        # `github` + `pr` and nothing else: those name the REVIEW, which is what
        # a baseline has to be from. `repo` is the local checkout's directory
        # name, so the same review run from a worktree and from the main
        # checkout ("quarterback-feat-issue-24" and "quarterback") disagrees on
        # it — and /panel-review-pr's own parallel mode gives every PR a
        # throwaway worktree, so that is the normal case rather than a corner.
        # Checking it would reject a baseline for having been written somewhere
        # else, which is not a property of the review at all.
        checked = [k for k in ("github", "pr") if want.get(k) is not None]
        missing = [k for k in checked if payload.get(k) is None]
        wrong = [f"{k}={payload.get(k)!r} (this run: {want[k]!r})"
                 for k in checked if payload.get(k) is not None and payload.get(k) != want[k]]
        if missing:
            b.problems.append(f"baseline {path} does not say which review it is from "
                              f"(no {', '.join(missing)}) — its findings were NOT counted "
                              "as earlier rounds")
            continue
        if wrong:
            b.problems.append(f"baseline {path} is another review's — " + ", ".join(wrong)
                              + " — its findings were NOT counted as earlier rounds")
            continue
        try:
            was = int(payload.get("round") or 1)
        except (TypeError, ValueError):
            b.problems.append(f"baseline {path} has a malformed round "
                              f"({payload.get('round')!r}) — counted as round 1")
            was = 1
        if "round" in want and was >= want["round"]:
            b.problems.append(f"baseline {path} is round {was}, which is not earlier than "
                              f"this run's round {want['round']} — pass the round this "
                              "run actually is — its findings were NOT counted as earlier "
                              "rounds")
            continue
        # A cycle id the caller minted, or — for a round 1 that predates the
        # field — the run_key it recorded itself under, which is unique to that
        # process and is exactly as stable. "" for a payload that says neither,
        # which conflicts with nothing and inherits whatever the set decides.
        usable.append((was, str(payload.get("cycle") or payload.get("run_key") or ""),
                       path, payload))

    # The cycle named by the EARLIEST round that names one, keyed on the round
    # alone rather than by min() over the whole tuple: min() fell through to
    # comparing opaque hex ids whenever two baselines shared a round, so the winner
    # was lexicographic — neither earliest nor first, contradicting the rule
    # written beside it. Keyed this way, min() keeps the first at that round.
    named = [e for e in usable if e[1]]
    b.cycle = min(named, key=lambda e: e[0])[1] if named else None
    distinct_rounds = {e[0] for e in usable}
    if len(distinct_rounds) != len(usable):
        # Two payloads for one round is not fatal — their keys are still findings
        # an earlier round raised — but `rounds` then under-counts, and the cycle
        # was inherited from one of two equals, so the ambiguity is stated rather
        # than resolved in silence.
        b.problems.append(f"{len(usable)} baselines cover {len(distinct_rounds)} round(s) — "
                          "two payloads for one round, so which of them named the cycle "
                          "was arbitrary, and the commit and coverage record provenance "
                          "attributes against came from the last-written of them")
    accepted: list[tuple[int, str, dict]] = []
    for was, got, path, payload in usable:
        if got and b.cycle and got != b.cycle:
            b.problems.append(f"baseline {path} is from cycle {got}, not this run's "
                              f"{b.cycle} — a concurrent cycle's findings would read as "
                              "repeats here — its findings were NOT counted as earlier "
                              "rounds")
            continue
        b.rounds.add(was)
        accepted.append((was, path, payload))
        # Read off each member's recorded `truncated`, never off a run-level
        # flag: the run-level one says SOMEBODY was cut, and the question here is
        # whether a gap exists at all, so any member is enough — but it has to be
        # the per-member record, because a payload from a panel where one seat
        # was uncapped and another was not sets the run-level flag either way.
        #
        # The CONTAINER is guarded as well as its members: `or {}` substitutes only
        # for a falsy value, so a hand-edited baseline whose `reviewers` is a list
        # or a string went straight into `.values()` and killed the run — in the
        # one function whose rule is that a bad payload costs a `problems` entry.
        members = payload.get("reviewers")
        members = list(members.values()) if isinstance(members, dict) else []
        #: The members that actually recorded something. Kept apart from
        #: `members` because `reread` below needs POSITIVE evidence, and an empty
        #: list of records is the shape both "nobody said" cases arrive in.
        recorded = [m for m in members if isinstance(m, dict)]
        # TWO questions, and one variable used to answer both — which is how the
        # argv exemption turned into a fail-open bug the first time it was written.
        #
        # `truncated_any` — did any seat read a PREFIX of its target? That is what
        # `reread` below needs, and only ONE exemption applies to it — `absent`,
        # never `argv_capped` (the asymmetry is argued where it is applied): a round where
        # the kernel-capped seat saw two thirds of the diff did not read the whole
        # PR, so it cannot be the round that closes every earlier round's gap.
        # Exempting a seat says "this gap will never close, stop vetoing on it" —
        # it must not also say "this round closed everyone else's".
        # `absent` is exempt HERE as well, and `argv_capped` is not — the two
        # exemptions genuinely differ on this question (225-R2-F01).
        #
        # An argv-capped seat RAN and saw a prefix, so the round really did not read
        # its target whole and cannot be the round that closes an earlier gap. An
        # ABSENT seat read nothing and is no evidence either way: the seats that did
        # run may have read everything, and on a box where a configured seat can
        # never be installed there is no future round that would clear the gap
        # either. Leaving it in was the first spelling of this merge and it looked
        # like the safe direction; it is not. It lets one legacy payload's phantom
        # record block `reread` forever, which keeps every earlier gap open and the
        # cycle non-confident — the exact permanent veto #222 exists to remove,
        # surviving in the one path the fix had not reached.
        truncated_any = any(m.get("truncated") and not m.get("absent")
                            for m in recorded)
        # `cut` — does this round leave an inherited veto? Here two exemptions
        # apply, for two different reasons, and neither subsumes the other.
        #
        # `argv_capped` (#113): a seat the KERNEL cut was never going to be closed
        # by a later round either, on this box, at this diff size. The veto it buys
        # is not "a gap this cheaper round failed to re-read", it is the same
        # constant arriving one round later and standing for the rest of the cycle
        # — `/panel-review-pr` drives multiple rounds, so the loop would go right
        # back to never stopping confidently. Truncation by a BUDGET still carries,
        # which is the whole point of telling them apart: raise the number and the
        # next round genuinely does read what this one could not.
        #
        # `absent` (#222): a seat that never ran cannot have been cut. Until #222 it
        # was recorded `truncated: True` anyway, because `budgets` was built from
        # the CONFIGURED seats, so this banked a truncated round on every cycle of
        # every box configuring a seat it cannot carry — and the inherited veto then
        # told a later round that code had "been read by no round of this cycle"
        # when nothing had been cut off from anything.
        #
        # Both terms are needed. `argv_capped` covers only seats the kernel bounded
        # — antigravity — so an absent `pi` or `codex` carrying a configured
        # `max_diff_chars` smaller than the target lands in `truncated_for` with
        # `argv_capped` False, and the argv exemption alone would still bank a
        # phantom round for it.
        #
        # NOT `ran and truncated`, which was #222's first spelling and over-corrects
        # in the optimistic direction: `ran` is `not skip`, false for EVERY way of
        # not running. An INSTALLED seat with a small budget reads a genuine prefix
        # and then times out, crashes, or is skipped for a bad effort pin — it is
        # written `ran: False, truncated: True`, and a real tail nobody read would
        # stop being banked. `absent` is the one absence that is a fact about the
        # HOST rather than about the round; every other way of not running still
        # counts here, exactly as it still vetoes in `coverage_veto`.
        #
        # Both exemptions are also what keep OLD payloads honest, which is why the
        # reader had to be fixed at all: baselines outlive the release that wrote
        # them, and `--baseline` is fed earlier rounds' payloads by design. A
        # payload written before either field existed has neither, so both `not`
        # terms are True and its recorded truncation is banked — the old reading,
        # preserved, rather than a real coverage gap silently dropped because the
        # writer was too old to say.
        cut = any(m.get("truncated") and not m.get("argv_capped")
                  and not m.get("absent") for m in recorded)
        if cut:
            b.truncated_rounds.add(was)
        # Two facts about coverage that only matter once a later round stops
        # re-reading the PR. A round that read the WHOLE PR with nothing truncated
        # has closed the gaps every earlier round left (resolved after the loop,
        # since an earlier round may not have been seen yet); a round that read
        # NOTHING leaves one that the anchor then advances straight over.
        #
        # `reread` takes POSITIVE evidence and nothing less, because one entry in
        # it erases every earlier round's truncation. `not cut` is false both when
        # nothing was truncated and when the payload records nothing at all — a
        # pre-v2.15 payload, a hand-edited `reviewers` that is not a dict, a
        # skipped round whose `reviewers_ran` is absent so the branch above never
        # sees it — and the comment on the truncation read already reasons that
        # "nobody said" is not "nothing happened". So a whole-PR round only counts
        # as having re-read the PR if at least one seat recorded that it was
        # there. The conservative direction: an old baseline keeps an inherited
        # veto standing rather than silently clearing it.
        # A manifest round (#138) reviewed the SHAPE of a move and none of its
        # code. Read off the payload's own verdict, defensively: a payload written
        # before `preflight` existed has no such key and is not a manifest round,
        # which is both true and the conservative direction.
        pre = payload.get("preflight")
        was_manifest = (isinstance(pre, dict)
                        and str(pre.get("verdict") or "") == "manifest")
        if was_manifest:
            b.manifest_rounds.add(was)
        # `ran`, not `not absent` (#222): a seat that was present and then crashed
        # is "not absent" while having read nothing, so the weaker test let a round
        # where every seat failed still qualify to erase earlier gaps.
        read_something = any(m.get("ran") for m in recorded)
        ran = payload.get("reviewers_ran")
        if isinstance(ran, list) and not ran:
            b.unread_rounds.add(was)
        # FOUR terms, and each rules out a different way a round can look like it
        # read the whole PR without having done so. `reread` erases every earlier
        # round's recorded gap, so it is the most destructive thing in this
        # function and every one of them is load-bearing:
        #
        #   `recorded`      — somebody wrote a per-seat record at all.
        #   `read_something`— at least one seat actually RAN (#222).
        #   `truncated_any` — no seat read a prefix of its target (#113).
        #   `not was_manifest` (#138) — the round read a diff, not a description of
        #     one. A manifest round records `scope: "pr"` (the manifest travels as
        #     the round's material, so it is a whole-target round by construction)
        #     with nothing truncated, because the manifest fitted. It therefore
        #     satisfies every OTHER term here while having read not one line of the
        #     diff, which would make it the round that closes everyone else's gaps.
        elif (recorded and read_something and not truncated_any and not was_manifest
                and str(payload.get("scope") or "pr") == "pr"):
            reread.add(was)
        # Tolerant of both shapes on purpose: this run writes a {key: round}
        # object, and a payload from before the field (or a hand-written one)
        # may carry a bare list. A list is attributed to the round that wrote it,
        # which is the only answer available and never later than the truth.
        #
        # Anything else is REPORTED, not dropped. Same rule as every other field
        # this function reads, and it matters more here than most: an unreadable
        # register reverts the cycle to counting an escalated finding as work a
        # fix round can clear, which is the exact failure the register exists to
        # prevent — and it would arrive with nothing said.
        _inherit(b.escalated, payload.get("escalated"), was, path, b.problems,
                 "escalated", "escalation", _is_key, _key_norm,
                 "the shape of a finding key",
                 "a finding only a human can close counts as work a fix round can "
                 "clear again")
        # #547's register, on exactly the same terms and through the same function:
        # both are a caller's word about an act performed outside the loop, both are
        # answered by a human on their own clock, and both silently revert the thing
        # they exist for if a round drops them.
        #
        # The KEY SHAPES differ and are checked apart — an obligation key is `uc-`
        # plus eight hex, a finding key is bare hex — so a key pasted into the wrong
        # flag is reported here rather than inherited into a register where it would
        # match nothing for the rest of the cycle while the caller read the silence
        # as the acknowledgement being honoured.
        _inherit(b.acknowledged, payload.get("acknowledged"), was, path, b.problems,
                 "acknowledged", "acknowledgement", is_claim_key,
                 lambda k: k.strip().lower(), "the shape of an obligation key",
                 "an unverifiable claim a human already accepted goes back to costing "
                 "the round its confidence and is put to them again next round")
        # #665's register, the third of the shape and the only one whose entries
        # are objects. Read through its own reader for the reason stated there:
        # the key and the round go through `_inherit` above, and only the reason
        # word — which no other register has — is judged here.
        #
        # It rides the same tolerant-and-loud rule as the two above, and the cost
        # of losing it is the sharpest of the three because it is measurable: the
        # cycle pays a second time for a fact one of its own actors already
        # established, and books the second payment as a discovery.
        _inherit_declined(b.declined, payload.get("declined"), was, path, b.problems)
        # #674's register, the fourth of the shape and the mirror of the one above:
        # `declined` records what a pass could not do, this records a person saying it
        # no longer needs doing. Inherited on `acknowledged`'s exact terms — a human
        # act performed outside the loop that no later round makes untrue — and read
        # by the same function for the same reason, since a key and a round is all it
        # is. The cost of losing one is stated as a HOLD rather than a cost in rounds
        # because that is what it is: the declination comes back, the veto with it,
        # and the PR stops being strictly landable until somebody types the flag again.
        _inherit(b.retracted, payload.get("retracted"), was, path, b.problems,
                 "retracted", "retraction", _is_key,
                 lambda k: k.strip().lower(), "the shape of a finding key",
                 "a declination a human already retracted comes back, and with it the "
                 "veto that stops this PR landing on an earned stop")
        for bucket in ("to_fix", "dismissed", "sonar_findings"):
            for f in payload.get(bucket) or []:
                if not isinstance(f, dict):
                    continue
                file, title = f.get("file"), _baseline_title(f)
                b.keys.add(str(f.get("key") or "") or _key_from_title(file, title))
                norm = _norm_title(title)
                if norm:
                    b.titles.setdefault(norm, set()).add(file or "")
    # An inherited truncation is only permanent while nothing has re-read the
    # region since. A whole-PR round with no truncated seat DID re-read it, so the
    # gap it closed is not still open — and a veto that says otherwise asserts
    # something the baselines themselves disprove.
    #
    # `unread_rounds` closes the same way and for the same reason. A round nobody
    # read is a gap the anchor steps over, but a later whole-PR round read the
    # code it stepped over along with everything else — so a veto saying "that
    # code has been read by no round of this cycle" states something the baselines
    # themselves disprove.
    if reread:
        newest = max(reread)
        b.truncated_rounds = {r for r in b.truncated_rounds if r > newest}
        b.unread_rounds = {r for r in b.unread_rounds if r > newest}
        # A manifest round's gap closes the same way and for the same reason: a
        # later whole-PR round read the code the manifest only described, so a veto
        # saying otherwise states something the baselines themselves disprove.
        b.manifest_rounds = {r for r in b.manifest_rounds if r > newest}
    # The commit and the coverage record come from the END of the set rather than
    # from a merge of all of it: `keys` and `titles` are a union over every earlier
    # round ("has anyone raised this before"), while "which commit did the fix pass
    # start from" has exactly one right answer and the earlier rounds' answers are
    # stale.
    #
    # Ties on the round number are broken by mtime and then by path, so which of
    # two payloads for one round supplies the anchor is decided by which was
    # written last rather than by the order a caller happened to pass them in.
    if accepted:
        ordered = sorted(accepted, key=lambda e: (e[0], _mtime(e[1]), e[1]))
        # The anchor comes from the latest round that actually SUPPLIED one, which
        # is not the same as the latest round accepted. Read off the last payload
        # alone, a newer round WITHOUT a `head_sha` cleared an anchor an older one
        # had given — so the same set of baselines anchored or did not depending on
        # nothing but which of them sorted last, and the increment silently fell
        # back to the whole PR. An older commit we CAN diff against is worth more
        # than no increment and no attribution at all, and the fallbacks are still
        # there if nothing in the set names one.
        anchor_payload: dict | None = None
        for was, path, payload in ordered:
            sha = payload.get("head_sha") or None
            if sha is None:
                continue
            # Validated rather than trusted: this string is interpolated into an
            # API path (`repos/{repo}/compare/{a}...{b}`), and a hand-edited or
            # corrupted baseline carrying a `/`, a `..` or a query string would
            # re-point the request at other history — whose diff then becomes this
            # round's review target and attributes its findings. Absent already
            # degrades cleanly (whole-PR scope, provenance `unknown`), so refusing
            # a value that cannot be a commit costs nothing.
            if not (isinstance(sha, str) and _SHA_RE.fullmatch(sha)):
                b.problems.append(f"baseline {path} records head_sha {sha!r}, which is not a "
                                  "commit id — it cannot be a commit or a ref, so it "
                                  "anchored no increment and provenance reads `unknown` "
                                  "rather than attributing against whatever that names")
                continue
            b.head_sha, b.head_round, anchor_payload = sha, was, payload
            # Banked for every round, not only the newest, because #559's filter
            # needs the ones this loop is walking PAST. Written after the same
            # validation the anchor gets — an unvalidated sha here would reach
            # `git show` — and a later payload for one round overwrites an
            # earlier one, which is the tie-break `ordered` already settled.
            b.head_shas[was] = sha
        # What that anchor round asked its fixer to fix (#67). Read off the SAME
        # payload the anchor came from and no other: the fix range this round
        # attributes against runs from that commit, so those are the complaints
        # the pass in between can have been answering. `to_fix` and
        # `sonar_findings` are the two buckets a fixer's brief is built from —
        # `dismissed` is deliberately absent, since a finding the master ruled
        # not real is nobody's premise.
        if anchor_payload is not None:
            for bucket in ("to_fix", "sonar_findings"):
                for f in anchor_payload.get(bucket) or []:
                    if not isinstance(f, dict):
                        continue
                    file = str(f.get("file") or "")
                    key = str(f.get("key") or "") or _key_from_title(file, _baseline_title(f))
                    if not file or not key:
                        # A finding nothing can place is not evidence that the
                        # fixer was working anywhere. Dropped rather than filed
                        # under "" — which `_same_file` would suffix-match against
                        # every path there is.
                        continue
                    b.fixed_here.setdefault(file, set()).add(key)
                    line = f.get("line")
                    b.fixed_findings.append(
                        (key, str(f.get("severity") or "?"), file,
                         line if isinstance(line, int) and not isinstance(line, bool) else None,
                         str(f.get("synthesis") or _baseline_title(f) or "")))
        # The fix phase's earlier end (#192), on its own pass rather than folded
        # into the loop above: a round can record a head and no finish (a payload
        # from before the field) or a finish and no head (a round whose PR read
        # failed after the clock started), and pairing them would let either
        # absence discard the other's answer. Same "latest that supplied one"
        # rule; same reason.
        for was, path, payload in ordered:
            when = (payload.get("timing") or {}).get("finished_at")
            if when is None:
                continue
            # Validated rather than trusted, like `head_sha` beside it. A string,
            # a bool (an `int` subclass, so `True` would read as 1970) or a
            # negative reading is not a wall-clock instant, and the span computed
            # from one would be reported as a fix phase — a number that looks
            # measured and is not, which is the failure #192 is about.
            # `math.isfinite` as well as the range check: Python's `json`
            # parses a bare `Infinity`, which passes `> 0` and then dates the
            # previous round to the end of time — a fix phase computed from it is
            # a negative infinity, and the skew branch is not where that belongs.
            # NaN fails `> 0` already and needs no separate term.
            ok = (isinstance(when, (int, float)) and not isinstance(when, bool)
                  and math.isfinite(when) and when > 0)
            if not ok:
                b.problems.append(
                    f"baseline {path} records a finish time of {when!r}, which is not a "
                    "wall-clock instant — the fix phase before this round was NOT "
                    "measured from it")
                continue
            b.finished_at, b.finished_round = float(when), was
        # Coverage, unlike the anchor, is a property of the LAST round alone: it is
        # what that round could not read, and an older round's list describes a
        # different diff at a different budget.
        _, path, latest = ordered[-1]
        # Same care the findings buckets above take with a non-dict: a bare
        # string here iterates into a set of single characters, and `_same_file`
        # would then suffix-match those against real paths.
        unread = latest.get("unread_files") or []
        if not isinstance(unread, list):
            b.problems.append(f"baseline {path} records unread_files as a "
                              f"{type(unread).__name__}, not a list — that round's coverage "
                              "record is ignored")
            unread = []
        b.unread_files = {f for f in unread if f and isinstance(f, str)}
        b.read_nothing = not latest.get("reviewed")
        # The growth denominator, off the EARLIEST accepted round — `ordered` is
        # sorted by round, so its head. A WHOLE-PR size and never that round's review
        # target (#298): see `first_reviewed`, and :func:`_whole_pr_chars` for which
        # field supplies it. `None` is the ordinary case for a round that reviewed
        # nothing or a payload older than the field, and the check simply does not run.
        first_round, _first_path, first = ordered[0]
        chars = _whole_pr_chars(first)
        if chars is not None and first.get("reviewed"):
            # `"pr"` because that is what was just read, not because the label is
            # decorative: both ends of this ratio are whole-PR sizes now, and the
            # report prints the measurement so a reader can check that rather than
            # take it on trust.
            b.first_reviewed = (first_round, chars, "pr")
        # #490's rows, off the SAME accepted, cycle-checked, round-ordered set
        # everything above is read from — a baseline this run refused as belonging
        # to another cycle must not appear in the block either, or the reader is
        # shown a trend across two PRs' worth of rounds.
        #
        # ONE ROW PER ROUND. Two payloads for one round is a state this function
        # tolerates and warns about (see `problems` above), but two files claiming
        # round 2 are not two rounds, and a block whose whole value is being read
        # down a column must not carry two `r2` rows with different figures in it.
        # The winner is the LAST in `ordered`, which is the same last-written
        # tie-break (round, then mtime, then path) that already decides which of two
        # payloads supplies the anchor and the coverage record — so the row and the
        # commit the round is attributed against come from the same file rather than
        # from whichever rule was applied last.
        rows = {was: _trend_row(was, payload) for was, _path, payload in ordered}
        b.trend = [rows[was] for was in sorted(rows)]
    return b


#: The two CI states that carry a SETTLED result — a suite ran on this exact
#: commit and reported. Green is evidence the project's own tests pass; red is
#: evidence they do not, and `ci_brief` already tells every seat to "treat that as
#: a fact you may reason from, not as a finding to re-report". A round that read a
#: real failure is not a round that read nothing, and `preland.check_ci` refuses
#: the merge on red anyway, so FAIL costs the round nothing here. That division is
#: only sound while both gates are applied: this says the round HAD evidence, never
#: that a red build is harmless.
#:
#: Written as the states that DO NOT veto rather than as the four that do, and that
#: is the fail-closed direction on purpose: a seventh CI state added to
#: `CI_STATE_WORDS` next year vetoes until somebody argues it into this set, rather
#: than passing silently the way `none` did before #546.
#:
#: #548's two are the first states argued in, and the argument is the one this set is
#: for: a suite that RAN on this exact commit and reported is execution evidence,
#: which is the only thing the veto asks about. Weaker evidence than a CI run, and
#: every renderer says so — but "weaker" is not the axis here. Nothing in this
#: function grades evidence; it asks whether the round had any.
#:
#: **`local-fail` sits beside `FAIL` rather than vetoing, and that was a reversal.**
#: The first draft vetoed it, reasoning that `FAIL`'s exemption comes with a stated
#: precondition — `preland.check_ci` refuses the merge on red anyway, and "that
#: division is only sound while both gates are applied" — which is false of a local
#: run, since `check_ci` reads GitHub. Codex called that special pleading on PR #604
#: and was right, on two counts. It answers a question this set does not ask: whether
#: a second gate consumes the evidence is a fact about deployment policy, and
#: `coverage_veto`'s standing rule is that this list comes off recorded state.
#: And it closed nothing: the only repo that could reach `local-fail` with the merge
#: gate satisfied has written `preland.disabled_checks: ["ci"]`, and that repo merges
#: a red GitHub `FAIL` too — the check is not run at all. An asymmetry that buys no
#: safety costs only coherence.
CI_SETTLED = frozenset({"PASS", "FAIL", panel_scope.LOCAL_PASS, panel_scope.LOCAL_FAIL})

#: One sentence per state, because each says a different thing — the discipline
#: `_ci_line` and `ci_brief` already keep. Every line names a cause somebody can
#: discharge; none of them is discharged by a human acknowledging it.
#:
#: "No settled result" and NOT "nothing executed", which is the stronger claim and
#: is false of three of these five: `PENDING` can be a suite whose other checks have
#: already passed, `unknown` is a lookup that failed and says nothing either way
#: about what ran, and `local-unknown` is a command that was started and did not
#: report. Only `none` and `blocked` are claims about execution. The veto is the same
#: for all five — none of them gives the round a result it can earn its confidence on
#: — but the wording has to survive being read closely, because could-not-check is
#: not nothing-to-report and this whole change is about not conflating those.
CI_UNSETTLED = {
    # #501 already gives a PENDING build a bounded wait before the seats are
    # dispatched. This is the residue AFTER that wait — the honest case its own
    # docstring names — not a substitute for it. Deliberately not "nothing ran":
    # `review_ci` reports PENDING when ANY check is pending, and the others may
    # have finished green.
    "PENDING": "CI had not settled when the seats were dispatched — no complete "
               "suite result exists for this commit yet",
    # The case #546 is about, and one of the two here that really is a statement
    # about execution. #324 is what made this distinguishable from `blocked` at
    # all. Since #548 it also carries a second fact by implication: a round that
    # reaches this state either declared no `review_panel.local_suite` or could not
    # run the one it declared, because a suite that RAN would have replaced this
    # status. The config note says which, and it is a note rather than a longer
    # sentence here — this is the veto's vocabulary, and the reason a channel is
    # empty belongs to whoever configures it.
    "none": "no CI run exists for this commit — nothing mechanical executed "
            "this code",
    # #324's state, and it must not borrow `none`'s sentence: a run EXISTS. It
    # simply will not execute until a person clicks, so it contributes nothing.
    "blocked": "a CI run exists for this commit and is gated on a human "
               "approval — it has executed nothing",
    # #548's one vetoing state, and it is like `unknown` rather than like `none`: a
    # command was started and told us nothing. Whether it executed any of the code is
    # exactly what is not known — which is also what a passing suite becomes when the
    # checkout moved out from under it mid-run (`review_local_suite`).
    panel_scope.LOCAL_UNREAD:
        "the repo's own suite was run locally on this commit and produced no "
        "result — no settled suite result exists for it either way",
    # Not "nothing ran" — nobody knows whether anything ran. Could-not-check is
    # not nothing-to-report, and stating it as the former would be the same
    # conflation this veto exists to undo.
    "unknown": "CI could not be read — whether anything executed this code is "
               "unknown",
}

#: The one state a repo can declare its way out of, and the declaration that does
#: it. `preland.check_ci` refuses `none` with, in its own words, *"if this repo
#: genuinely has no CI, say so with `"preland": {"disabled_checks": ["ci"]}` in
#: .harness-rules rather than reading silence as green"* — so a repo that HAS
#: said so has answered this question in writing, and asking it again every round
#: is `coverage_veto`'s own forbidden constant: an observation true of every round
#: the repo will ever run, which distinguishes nothing and makes `confident`
#: unreachable rather than rare.
#:
#: Exactly `none`, and not the other three. The declaration explains an ABSENT
#: run; it does not explain a run that exists and is gated, a suite that did not
#: settle, or a lookup that failed — each of those contradicts the declaration
#: rather than being covered by it, and a repo with no CI cannot produce them.
#:
#: Recorded state, like every other exemption in this file: a key in a rules file,
#: not a model's prose and not a seat's account of itself. And an EXPLICIT one —
#: an unexplained `none` still vetoes, which is the whole difference between "this
#: repo has no CI" and "nothing ran on this commit".
CI_NOT_APPLICABLE = "none"


def coverage_veto(reviewer_meta: dict[str, dict], judge_skip: str | None,
                  flagged: int, diff_chars: int, *, ci_status: str,
                  ci_declared_absent: bool = False,
                  coverage: CoverageRuling = CoverageRuling(),
                  acknowledged: Iterable[str] = ()) -> list[str]:
    """Reasons a quiet round is not evidence of a quiet PR.

    A counter cannot tell a genuinely dry round from a broken one — a reviewer
    that read half the diff, one that never ran, and one whose reply did not parse
    all look identical to "found nothing". These are the observations that
    distinguish them, and they exist to stop a failure being read as convergence.
    They do NOT drive the loop: a truncated reviewer is truncated again next round
    at the same budget, so treating that as a reason to go again is a loop with no
    exit. It is a reason to stop CLAIMING the PR is clean.

    **What does NOT belong here is a constant.** Three of these observations are
    true of every round the panel runs, so they distinguish nothing and cost the
    signal everything: a reviewer whose CLI this box does not carry
    (:attr:`ReviewerRun.absent`), a seat that cannot read the code it is reviewing
    (:attr:`ReviewerRun.code_blind`, whose `could_not_assess` entries are
    therefore reported and not counted), and the one seat the kernel cannot hand a
    whole diff to (`argv_capped` — `agy`'s prompt travels in argv). All three are
    facts about the HOST or about the panel's DESIGN. Because `round_stop`
    computes `confident` as `not veto`, leaving them in made a confident stop
    unreachable — permanently on a headless box, and on any PR that so much as
    mentions a file it does not change. A signal that is never positive carries no
    information and trains its reader to ignore it, which is worse than not
    emitting it.

    Each is exempted off RECORDED STATE, never off the wording of a message or a
    declaration, and each has a floor beneath it so that exempting seats one at a
    time cannot empty the list on a round where nothing was read. Every other way
    of coming up short — a crash, a timeout, a budget someone typed, a reply that
    would not parse — is about THIS run and still vetoes.

    **`ci_status` asks the same question about EXECUTION EVIDENCE that everything
    above asks about READING** (#546). Every observation above is about whether a
    seat saw the diff; a round can satisfy all of them and still have had no test
    run against the code. Until #546 that fact reached
    the report as a warning and reached this function not at all, so a round with a
    green suite behind it and a round where no run exists were the same round as
    far as `confident` was concerned. It is `coverage_veto`'s shape and not a
    seat's: derived from recorded state (`review_ci_settled`'s answer), one line,
    no adjudication, no exemption doctrine — where today the same fact arrives as
    four seats each declaring in prose that they could not run the tests.

    It distinguishes four things rather than merging them, and the wording of each
    line is load-bearing: a settled result exists (`PASS`/`FAIL`), no run executed
    (`none`), a run exists and is gated so it executed nothing (`blocked`), and the
    result is not settled or not readable (`PENDING`, `unknown`). Only the middle
    two are claims about EXECUTION — a `PENDING` suite may have several green
    checks in it, and `unknown` is a lookup that failed and says nothing either way.
    Could-not-check is not nothing-to-report, so each keeps its own sentence, as it
    does in `ci_brief`.

    **The one exemption, and why it is not the constant the paragraph above rules
    out.** An absent CLI is true of every round this box will ever run and says
    nothing about any of them; "nothing executed" is false the moment CI runs, so
    on a repo that has CI it is a fact about the round and vetoes. On a repo that
    genuinely has none it WOULD be a standing veto — so `ci_declared_absent`
    carries the declaration `preland.check_ci` already asks such a repo to make
    (`"preland": {"disabled_checks": ["ci"]}`), and a repo that has made it is not
    asked again every round. Exactly `none` is exempted; see
    :data:`CI_NOT_APPLICABLE`. An UNEXPLAINED `none` still vetoes, which is the
    whole distance between "this repo has no CI" and "nothing ran on this commit".

    `ci_status` is keyword-only and has NO DEFAULT, which is the fail-closed
    direction: a caller that forgets it raises rather than quietly buying a
    confident stop for a round with no settled CI result behind it, which is the
    failure mode this whole function exists to make impossible. `ci_declared_absent`
    does default, because its default is the STRICT answer — a caller that knows
    nothing about the repo's rules has not been told CI is inapplicable, and must
    not assume it.

    **The fourth constant, and the one this function was itself producing** (#547).
    A declaration that no seat here could have settled — the claim needs a running
    database, a browser, a deployed system — is true of every round of every PR
    about runtime behaviour, so as a veto it distinguishes nothing and makes a
    confident stop unreachable on exactly the changes that most need one. It is the
    same shape as `absent`, `code_blind` and `argv_capped`, arriving through the one
    branch that read a seat's own prose.

    So it is exempted on the same terms as those three: off RECORDED STATE. The
    record is `coverage`, and the state is the judge's typed ruling against a list of
    declarations THIS FILE numbered — not a regex over the declaration's wording,
    which the paragraph above rules out and which would exempt a genuine
    round-specific gap whose phrasing happened to match while missing the structural
    one that did not.

    **Unlike those three it is not free, because a judgement is not a fact.** `absent`
    and `code_blind` are things the host and the sandbox did; a ruling is a model's
    opinion about a model's sentence, and an exemption resting on one alone would be
    a confidence gate the panel could open by writing about itself. So the ruling
    does not exempt. It CONVERTS: the declaration stops being "claude could not
    assess X" and becomes a named obligation, which goes on vetoing until a human
    passes its key to `--acknowledge`. `acknowledged` is that act, and it is recorded
    state of the plainest kind — an argument on the command line, inherited across a
    cycle's rounds through the payload exactly as `--escalated` is.

    Two properties follow, and they are the ones to check any change to this against:

    * **The veto list can only get shorter, and never empty where it was not empty
      before.** Merging is the point, so a ruling DOES delete lines: four seats
      stating one capability limit become one obligation where they were four vetoes.
      What it cannot do is delete the last one. An obligation stands in only for lines
      this function would have emitted anyway (:func:`reached_obligations` walks the
      same seats under the same recorded state), and every obligation reached is
      either acknowledged by a human or emitted — so a set of declarations that
      vetoed before still vetoes after, and `confident` is unchanged by any ruling
      alone. No round vetoes for a reason it did not veto for before, and #546's
      separation is untouched: a round with no settled CI result still vetoes on
      `ci_status`, whatever the seats did or did not say.
    * **Adding a seat no longer costs a confident stop by construction.** Under the
      old rule each new seat contributed its own copy of the same capability limit
      and each copy was a veto, so a fifth seat made a confident stop strictly less
      reachable while adding findings rather than evidence. Now a seat restating a
      claim already on the ledger adds nothing to this list. What a new seat can
      still cost is a gap it found that the others missed and that this panel COULD
      have closed — which is diligence, is discharged by going and looking, and is
      the behaviour worth keeping."""
    out = []
    for name, meta in sorted(reviewer_meta.items()):
        if not meta.get("ran"):
            skip = str(meta.get("skip") or "")
            # A seat whose CLI is not INSTALLED on this box is a fact about the
            # host, not about the round: it is absent every round, so vetoing on
            # it makes `confident` permanently unreachable on the headless
            # machines — which is where the unattended loops run and where the
            # signal has to mean something. A repo that lists a workstation-only
            # vendor would otherwise buy every one of its unattended runs a
            # standing veto and train the reader to discount all of them. The
            # skip is still REPORTED (result.skipped carries it, and the header
            # names who ran); what it is not is evidence a quiet round hid
            # something. Every other way of not running — a crash, a timeout, a
            # bad model pin, a CLI that produced nothing — is about THIS run and
            # still vetoes.
            #
            # Read off the recorded state, never off the skip TEXT: the message
            # is free-form, so `skip.endswith(CLI_ABSENT)` would let an installed
            # CLI whose stderr tail happens to read that way skip the veto, and
            # would silently restore the standing veto the first time this
            # branch's wording gained a suffix.
            if meta.get("absent"):
                continue
            out.append(f"{name} did not run ({skip or 'no reason recorded'})")
            continue
        if meta.get("truncated") and not meta.get("argv_capped"):
            budget = meta.get("max_diff_chars") or 0
            out.append(f"{name} saw {budget:,} of {diff_chars:,} diff chars")
        if meta.get("unstructured"):
            out.append(f"{name} returned no structured reply — its coverage is unknown")
        # A blind seat's declarations are reported and do not vote. See
        # `ReviewerRun.code_blind`: with an empty sandbox and no file tools the
        # diff is the seat's whole evidence, so "I could not read a function this
        # diff does not change" is true of every round it sits. A constant cannot
        # distinguish a quiet round from a broken one, which is the only thing
        # this function is for — and `confident` being `not veto` meant one
        # unreadable neighbour permanently denied a confident stop to any PR that
        # merely REFERENCES a file it does not touch. That is most of them.
        #
        # Kept out on recorded state, never by reading the entries: they are
        # free-form model prose, and a regex over them would exempt a genuine
        # round-specific gap whose wording happened to match while missing the
        # structural one that did not. Same argument, and the same failure in both
        # directions, as `absent` a few lines up.
        #
        # A seat that CAN read the tree (#113's per-repo setting, second half)
        # comes back with `code_blind` False and its declarations veto again,
        # which is right: at that point "I could not read it" is a fact about the
        # round and worth the round's confidence.
        if not meta.get("code_blind"):
            for gap in meta.get("could_not_assess") or []:
                # An unresolvable one is not dropped here — it is deferred to the
                # obligation block below, which emits one line for the CLAIM rather
                # than one per seat that raised it. A gap the judge did not rule on
                # falls through to the line it has always produced.
                if (name, gap) not in coverage.unresolvable:
                    out.append(f"{name} could not assess: {gap}")
    # What the declarations above became. One line per CLAIM, not per seat, and only
    # for the claims a VETOING declaration raised — a blind seat's or an absent
    # seat's cost the round nothing today and must not start costing it something
    # here, which is what keeps this change unable to lengthen the list anywhere.
    #
    # Still a veto, and that is the half that makes the other half safe. The judge
    # can say "nothing here could have checked this" and all that buys is a better
    # sentence; what ENDS the veto is a person reading the claim and passing its key
    # back, which is a bounded one-time act instead of a question no round can ever
    # answer. Acknowledging is deliberately per claim rather than per round: a blanket
    # "yes, fine" is the cheap gate that looks like assurance, and it is the failure
    # mode on the far side of this one.
    ack = {k.strip().lower() for k in acknowledged if isinstance(k, str)}
    for ob in reached_obligations(reviewer_meta, coverage):
        if ob.key in ack:
            continue
        out.append(f"an unverifiable claim is unacknowledged [{ob.key}]: {ob.claim}"
                   + (f" — {ob.reason}" if ob.reason else ""))
    # The floor under the absence exemption above. Exempting absent seats one by
    # one means a box carrying NONE of the reviewer CLIs produces an empty veto
    # list, and `confident` is `not veto` — a confident stop on a diff nobody
    # read, which is the strongest wrong signal this file can emit and lands
    # exactly on the unattended hosts the exemption was added for. At least one
    # reviewer has to have actually run.
    if not any(m.get("ran") for m in reviewer_meta.values()):
        out.append("no reviewer ran — nothing read this diff")
    # The same floor, one storey up, and it exists for the same reason: the
    # `argv_capped` exemption is applied per seat, so a panel whose every running
    # seat was cut by the kernel produces an empty veto list and a confident stop
    # on a diff nobody saw whole. Today that means an antigravity-only panel —
    # `--reviewers antigravity`, or a repo that switched the others off — which is
    # a narrow case and exactly the kind that reaches an unattended loop and is
    # believed. A budget-truncated panel does not need this: those seats already
    # filed their own lines above.
    # Over the LLM seats only, not every entry. `sonarqube` shares this mapping and
    # carries no `truncated` key, so counting it made one running static analyser
    # silently switch this floor off — a round could then stop confidently with
    # `--reviewers antigravity` and sonar enabled, no LLM having read the diff
    # whole. Sonar is the hard gate alongside the panel, not a substitute for a
    # reviewer reading the change, so it cannot stand in for one here. The floor
    # above it asks a different question ("did ANYTHING run?") and counts sonar
    # deliberately, which is why the two are separate.
    ran = [m for n, m in reviewer_meta.items()
           if m.get("ran") and n in LLM_REVIEWERS]
    if ran and all(m.get("truncated") and m.get("argv_capped") for m in ran):
        out.append("every reviewer that ran was cut by the argv ceiling — "
                   "nothing read this diff whole")
    # Read off the state `review_ci_settled` RECORDED, never off `ci_brief`'s
    # prose or a seat's account of it — the same rule every exemption above
    # keeps, applied to an inclusion. The fallback line is reached only by a
    # status outside the six `CI_STATE_WORDS` names; it vetoes rather than
    # passing, because a status this function does not recognise is not a pass.
    exempt = ci_declared_absent and ci_status == CI_NOT_APPLICABLE
    if ci_status not in CI_SETTLED and not exempt:
        out.append(CI_UNSETTLED.get(
            ci_status, f"CI reported {ci_status or 'nothing'} — no settled "
                       "suite result for this commit"))
    if judge_skip:
        # Phrased for both halves of the judge's job: on a round with no findings
        # it is the coverage split that went unadjudicated, not the findings.
        out.append(f"the round was not adjudicated ({judge_skip})")
    if flagged:
        out.append(f"{flagged} finding(s) whose reporter said the FIX needs re-reading")
    return out


# ------------------------------------------------------------- #84's futility brake
#
# The round cap bounds COST: N rounds and stop, whatever is happening. This bounds
# FUTILITY — stop when the rounds have stopped being about different things.
#
# **The measurement (#84, and PR #299 on 2026-08-21).** Five rounds on one PR.
# Rounds 1, 2 and 3 each found the PREVIOUS round's fix reopening the same hole,
# patched three different ways — merge parents (`_merged_in()`), then same-named
# refs (`_inherited`/`_fresher_bases`), then a purely local branch — and the
# premise underneath all three, *that a local repository can say where a release
# number LANDED*, was named only at round 3, by the orchestrating human, and the
# answer to it was to delete the machinery. Measured across that cycle, **39 of
# the 53 findings after round 1 were introduced by the previous fix pass, and
# round 2 was 17 out of 17.** Nothing mechanical stopped it. The cap could not
# have: the cap was the thing the cycle was still short of.
#
# **The rule**, #84's, stated as a count of OCCURRENCES rather than of rounds: the
# second time a fix is written against a premise the previous round invalidated,
# stop. Not the third. Which is why the brake is evaluated when a fix is
# **proposed** — :func:`declare_premise`, through `panel.py --premise`, before the
# fix pass runs — and not when a round completes. End-of-round is one whole fix
# pass and one whole panel too late, and #84's own words for it are "that is too
# late, and PR #62 is the measurement".
#
# **What it deliberately does NOT do.** It does not infer a premise from the
# findings. Same-premise detection is not a string match: #62's three proxies were
# `rc == 0`, an artefact's existence, and a head SHA moving — textually unrelated,
# and identical in what they assumed. #84 says to start with the declaration and
# "treat an undeclared fix as unescalatable rather than pretending to infer", so
# the fixer DECLARES its premise (`review-pr.md` step 3a) and declarations are the
# only thing compared. A fix pass that declared nothing cannot be braked, and that
# is REPORTED (`undeclared_rounds`) rather than passed over: a cycle nobody could
# have braked is a different claim from a cycle that did not need braking, and the
# second is what silence would assert.
#
# **The honest limit, which is the same one `escalated` carries and is not closed
# here.** A declaration is a claim by the agent whose fix pass is about to be
# written. Across #299's five rounds the fixers escalated ZERO times — the premise
# was named by the human orchestrator — so a brake that waits for a fixer to
# volunteer would not have fired that day either. What this adds is the COUNT and
# the STOP, which is the half that was missing; what makes the declaration happen
# is prose in two briefs, and prose is what it is. The register is the audit trail
# that makes the claim checkable afterwards, and the round cap still binds.

#: `review_panel.escalate_on` read from where it is declared and documented, rather
#: than spelled a second time here (`panel_ask.ASK_DEFAULTS`'s precedent, and for
#: its reason: a default changed in the file that documents it would otherwise go
#: on being ignored by the file that applies it).
ESCALATE_ON_DEFAULTS = harness_rules.DEFAULTS["review_panel"]["escalate_on"]

#: #78's other two reserved matters. A repo that writes one is TOLD it is recorded
#: and not enforced, on `require_failing_test`'s precedent — a governance switch
#: believed to be on and quietly off is the loudest possible way to make a process
#: look governed.
ESCALATE_ON_UNBUILT = ("quorum_failed", "judge_absent")

#: Exit code for "a premise was declared for the Nth time and N reached the dial".
#: Its own code, not 1: the caller has to be able to tell the brake FIRING from the
#: command failing to run, and both are non-zero. 2 is argparse's usage error and 3
#: is :data:`panel_core.UNWRITTEN_PAYLOAD_EXIT`.
#:
#: **Shared with the undecidability brake (#491)**, deliberately. A caller reads this
#: code to mean one thing — *do not write this fix, escalate the finding instead* —
#: and both brakes end in exactly that instruction, with the same `--escalated` keys
#: to hand the next round. A second code would make every caller learn a second
#: number to take the identical action, and the reason they differ is in the report,
#: which is where a reader is looking for it.
PREMISE_REPEATED_EXIT = 4

#: What a declaration answered about the property its fix asserts (#491): whether the
#: RUNTIME the assertion runs in can observe it. `unknown` is the honest value for a
#: declaration that was not asked — every declaration made before this existed, and
#: every caller that has not passed `--premise-decidable`.
DECIDABILITY = ("yes", "no", "unknown")

#: The register's shape, so a future one can be told from a hand-written file.
PREMISE_REGISTER_VERSION = 1

#: New outstanding findings a round needs before #489's injection rate is a RATE at
#: all, and the reason it is a constant rather than a second dial.
#:
#: A strict majority of three findings is two of them, and two findings is not a
#: measurement of anything: `panel_scope._provenance` is documented as routinely
#: wrong by a line or two in both directions, so at n<4 the cycle's verdict is one
#: reviewer's line number. At 4 the majority the rule fires on is three findings
#: agreeing, which is the smallest number that is a pattern rather than a coin.
#:
#: Not a dial, deliberately. #489's own open question is that nothing calibrates
#: where a healthy cycle sits, and the honest answer to one uncalibrated number is
#: not to ship two of them — a repo asked to tune a sample size it has no data for
#: will either leave it alone or turn the brake off by accident. `fix_injection:
#: null` is the supported way to switch this off, and it is one line.
FIX_INJECTION_MIN_NEW = 4

#: New outstanding findings a round needs before "the count did not fall" is
#: evidence of anything (#505), and the reason it is a constant rather than a second
#: dial.
#:
#: The rung compares two counts, and at the bottom of the range the comparison is
#: arithmetic rather than divergence: one new finding then two is a rise of 100% and
#: is a cycle that is very nearly done. What #505 is about is the shape Rich read off
#: a real cycle — 44, then 15 new, then 18 new — where both ends of the comparison
#: are volumes. Below four the round is not producing volume, whatever the round
#: before it did, and another fix pass is cheap.
#:
#: Applied to BOTH ends of every comparison, so it fails in the direction that does
#: not stop a cycle: a round that produced three new findings cannot end one however
#: flat the series is, and neither can a round whose PREDECESSOR produced three. The
#: second half is the one that is easy to leave out and it is not a symmetry for its
#: own sake — 1 then 4 satisfies "did not decrease" and is a cycle whose first round
#: under-read, which argues for more coverage rather than for stopping.
#: `not_falling_state` records which existing test found that.
#:
#: Four rather than three for `FIX_INJECTION_MIN_NEW`'s reason and
#: deliberately the same number — two uncalibrated floors that differ by one would be
#: two things to defend and one of them would be defended by "it is not the other
#: one".
#:
#: Not a dial, for its sibling's reason: #505 says in as many words that what a
#: HEALTHY cycle looks like is uncalibrated, and the honest answer to one uncalibrated
#: number is not to ship two of them. `new_findings_not_falling: null` is the
#: supported way to switch this off and it is one line.
NOT_FALLING_MIN_NEW = 4


def premise_repeat_limit(panel: dict, notes: list[str]) -> int | None:
    """`review_panel.escalate_on.premise_repeated` — the occurrence a declared
    premise is stopped ON, or ``None`` for "do not brake".

    Read per KEY rather than per block, so a repo that writes `escalate_on` at all
    still gets the shipped default for the matters it did not mention. `review_panel`
    is merged one level deep (`harness_rules._DEEP_BLOCKS`), so a written
    `escalate_on` REPLACES the default object wholesale; without the per-key
    fallback, `{"quorum_failed": true}` would silently switch the brake off.

    A malformed value is a HARD EXIT through :func:`panel_seats._refuse_value`, the
    same line the other dials draw between an unknown KEY (warned about and dropped
    — it may be a setting only a newer harness knows) and a malformed value of a
    known one (a typo, and applying the default anyway runs the cycle under a policy
    the file did not ask for). ``1`` is refused with the rest: it would escalate the
    FIRST time any premise was declared, which is not a repeat and would make every
    declaration a stop — the fastest way to teach a fixer never to declare one."""
    raw = panel.get("escalate_on", _ABSENT)
    if raw is _ABSENT or raw is None or raw == "":
        rules: dict = dict(ESCALATE_ON_DEFAULTS)
    elif isinstance(raw, dict):
        rules = raw
    else:
        _refuse_value("escalate_on", raw,
                      'a JSON object of reserved matters, e.g. {"premise_repeated": 2}')
        return None                                   # unreachable
    for key in ESCALATE_ON_UNBUILT:
        if rules.get(key):
            notes.append(
                f"`escalate_on.{key}` is recorded and NOT enforced — #78 reserves the "
                "name, and nothing implements it yet; the brakes that do anything are "
                "`premise_repeated`, `premise_undecidable`, `fix_injection` and "
                "`new_findings_not_falling`")
    want = rules.get("premise_repeated", ESCALATE_ON_DEFAULTS.get("premise_repeated"))
    if want is None or want is False or want == "":
        return None
    n = None
    if isinstance(want, bool):
        n = None
    elif isinstance(want, int):
        n = want
    elif isinstance(want, float) and want.is_integer():
        n = int(want)
    elif isinstance(want, str):
        try:
            n = int(want.strip())
        except ValueError:
            n = None
    if n is None or n < 2:
        _refuse_value("escalate_on.premise_repeated", want,
                      "a whole number of OCCURRENCES >= 2 (2 means 'the second time'), "
                      "or null to switch the brake off — 1 would escalate the first "
                      "time any premise was declared, which is not a repeat")
    return n


def premise_undecidable_brake(panel: dict, notes: list[str]) -> bool:
    """`review_panel.escalate_on.premise_undecidable` (#491) — does a declaration that
    answers "the runtime cannot observe this property" refuse the fix?

    Read per KEY through the same fallback :func:`premise_repeat_limit` uses and for
    the identical reason: `review_panel` merges one level deep, so a repo writing
    `escalate_on` at all replaces the default object wholesale, and without the
    per-key fallback `{"premise_repeated": 2}` would silently switch THIS brake off.
    That is the exact failure mode #84 hit and it is worth not shipping twice.

    **No number, unlike its sibling, and that asymmetry is the point.**
    `premise_repeated` counts because one declaration is not evidence — writing a fix
    against a premise is what a fix pass DOES, and only the repeat says the rounds
    have stopped being about different things. This one is not counting. It reads a
    fixer's answer to a question with a fact for an answer, and a `no` is already the
    whole finding: if the property cannot be observed where the assertion runs, every
    fix for it is an approximation, the next round finds the gap, and the cap is the
    only thing that can end the cycle. A second occurrence would confirm nothing the
    first did not say, at the price of a fix pass and a panel.

    So the value is a FLAG. A count over it would be counting how many times one
    fixer said one `no`, and `2` would mean "approximate once first", which is the
    behaviour the brake exists to refuse."""
    raw = panel.get("escalate_on", _ABSENT)
    if raw is _ABSENT or raw is None or raw == "":
        rules: dict = dict(ESCALATE_ON_DEFAULTS)
    elif isinstance(raw, dict):
        rules = raw
    else:
        # Already refused by `premise_repeat_limit` on every real path — both readers
        # run off one config — but this function is public and is called directly by
        # tests, so it does not rely on a sibling having been called first.
        _refuse_value("escalate_on", raw,
                      'a JSON object of reserved matters, e.g. {"premise_repeated": 2}')
        return False                                  # unreachable
    want = rules.get("premise_undecidable",
                     ESCALATE_ON_DEFAULTS.get("premise_undecidable"))
    if want is None or want is False or want == "":
        return False
    if want is True:
        return True
    _refuse_value("escalate_on.premise_undecidable", want,
                  "true or false — this brake reads a fixer's yes/no answer about one "
                  "property, so there is no occurrence to count and a number here "
                  "would mean 'approximate it once first'")
    return False                                      # unreachable


def fix_injection_limit(panel: dict, notes: list[str]) -> float | None:
    """`review_panel.escalate_on.fix_injection` (#489) — the fraction of a round's
    new outstanding findings that may have been INTRODUCED by the previous fix pass
    before the cycle ends, or ``None`` for "do not brake".

    Read per KEY through the same fallback :func:`premise_repeat_limit` uses and for
    the identical reason: `review_panel` merges one level deep
    (`harness_rules._DEEP_BLOCKS`), so a repo that writes `escalate_on` at all
    replaces the default object wholesale, and without the per-key fallback
    `{"premise_repeated": 2}` would silently switch THIS brake off. That is the
    exact failure mode #84 hit and it is worth not shipping a third time.

    **The bounds are `0 < x < 1`, and both ends are refused rather than clamped.**
    Zero or below is not a fraction of anything and would fire on any attributable
    round with a single introduced finding in it — every round of every cycle,
    which is a brake with no discrimination in it. One or above can never be
    EXCEEDED, because a rate is at most 1.0 and the comparison is strict: it is the
    brake switched off behind a value that reads as armed, which is precisely the
    posture `require_failing_test` exists to refuse having silently. `null` is the
    spelling for off, `0.99` is the spelling for "only when every one of them was
    introduced", and a repo that meant either gets the one it typed.

    ``false`` is a second spelling of ``null`` and is honoured as one, exactly as
    `premise_repeat_limit` honours it: it is what an operator reaches for to turn a
    brake off, and refusing it would be this harness telling somebody their "off" was
    a typo. ``true`` is refused, because there is no number it could mean — a
    threshold is not a switch, and guessing one would be inventing the policy. Both
    are settled before the numeric read for `fix_growth_limit`'s reason:
    ``isinstance(True, int)`` is True, so a bool that fell through would become 1.0
    or 0.0, and 0.0 is a brake that fires on every attributable round. Non-finite is
    rejected with the rest: ``inf`` is the check off behind a value that reads like a
    number, and ``nan`` compares false against everything, which is the same thing."""
    raw = panel.get("escalate_on", _ABSENT)
    if raw is _ABSENT or raw is None or raw == "":
        rules: dict = dict(ESCALATE_ON_DEFAULTS)
    elif isinstance(raw, dict):
        rules = raw
    else:
        # Already refused by `premise_repeat_limit` on every real path — both readers
        # run off one config, and `run()` calls that one first — but this function is
        # public and is called directly by tests, so it does not rely on a sibling
        # having been called before it. The unbuilt-name notes
        # (`ESCALATE_ON_UNBUILT`) are deliberately NOT repeated here: they are a fact
        # about the block rather than about either dial, and one report carrying the
        # same sentence once per reader is the "loud and wrong" a reader learns to
        # skip.
        _refuse_value("escalate_on", raw,
                      'a JSON object of reserved matters, e.g. {"premise_repeated": 2}')
        return None                                   # unreachable
    want = rules.get("fix_injection", ESCALATE_ON_DEFAULTS.get("fix_injection"))
    if want is None or want is False or want == "":
        return None

    def refuse(what: str) -> float | None:
        _refuse_value("escalate_on.fix_injection", want,
                      f"{what} — the fraction of a round's new findings that may have "
                      "been introduced by the previous fix pass, or null to switch the "
                      "brake off. 1 or more can never be exceeded, which is the brake "
                      "off behind a value that reads as on")
        return None                                   # unreachable

    if isinstance(want, bool) or not isinstance(want, (int, float, str)):
        return refuse("a number above 0 and below 1")
    try:
        n = float(want)
    except (TypeError, ValueError):
        return refuse("a number above 0 and below 1")
    if n != n or n in (float("inf"), float("-inf")):
        return refuse("a finite number above 0 and below 1")
    if not 0 < n < 1:
        return refuse("above 0 and below 1")
    return n


def not_falling_limit(panel: dict, notes: list[str]) -> int | None:
    """`review_panel.escalate_on.new_findings_not_falling` (#505) — how many
    CONSECUTIVE rounds whose new-finding count did not decrease end the cycle, or
    ``None`` for "do not brake".

    The volume rung beside :func:`fix_injection_limit`'s attribution one. That dial
    asks *did the fix cause this?*; this one asks *is the count still falling?*, and
    the two have different answers on the same cycle — findings a reviewer reading
    deeper produced, or a widened scope, are not attributable to any fix pass, and
    `panel_scope._provenance` under-counts what is. A diverging cycle can therefore
    sit under `fix_injection`'s threshold for its whole life and be stopped only by
    the cap.

    Read per KEY through the same fallback :func:`premise_repeat_limit` uses and for
    the identical reason: `review_panel` merges one level deep
    (`harness_rules._DEEP_BLOCKS`), so a repo that writes `escalate_on` at all
    replaces the default object wholesale, and without the per-key fallback
    `{"premise_repeated": 2}` would silently switch THIS brake off. That is the exact
    failure mode #84 hit, and it is worth not shipping a fourth time.

    **A whole number of ROUNDS, at least 1.** ``0`` is refused rather than clamped:
    zero consecutive not-falling rounds is satisfied by every round, including one
    whose count fell, which is a brake with no discrimination in it and is the
    posture `require_failing_test` exists to refuse having silently. A negative is
    the same value written differently. ``1`` is the default and is NOT refused here
    — the asymmetry with `premise_repeated`, which refuses 1, is that a premise
    declared once is an ordinary event while a round whose count did not fall is
    already the whole observation.

    ``false``/``null``/``""`` are the spellings of off, exactly as they are for
    `fix_injection`, and ``true`` is refused because a window is not a switch and
    there is no number it could mean. Bools are settled before the numeric read for
    `fix_growth_limit`'s reason: ``isinstance(True, int)`` is True, so a bool that
    fell through would become 1 — the brake at its default behind a value that means
    something else. A float that is not whole is refused rather than truncated: 1.5
    rounds is not a number of rounds, and a harness that quietly read it as 1 would
    be applying a policy the file did not write."""
    raw = panel.get("escalate_on", _ABSENT)
    if raw is _ABSENT or raw is None or raw == "":
        rules: dict = dict(ESCALATE_ON_DEFAULTS)
    elif isinstance(raw, dict):
        rules = raw
    else:
        # Refused here as well as by its siblings, for the reason `fix_injection_limit`
        # gives: `run()` calls `premise_repeat_limit` first on every real path, but
        # this function is public and is called directly by tests, and one that relied
        # on a sibling having run would be one test double away from applying a policy
        # nobody wrote. The unbuilt-name notes are left to that first reader — they are
        # a fact about the block, not about this dial.
        _refuse_value("escalate_on", raw,
                      'a JSON object of reserved matters, e.g. {"premise_repeated": 2}')
        return None                                   # unreachable
    want = rules.get("new_findings_not_falling",
                     ESCALATE_ON_DEFAULTS.get("new_findings_not_falling"))
    if want is None or want is False or want == "":
        return None
    n = None
    if isinstance(want, bool):
        n = None
    elif isinstance(want, int):
        n = want
    elif isinstance(want, float) and want.is_integer():
        n = int(want)
    elif isinstance(want, str):
        try:
            n = int(want.strip())
        except ValueError:
            n = None
    if n is None or n < 1:
        _refuse_value("escalate_on.new_findings_not_falling", want,
                      "a whole number of consecutive ROUNDS >= 1 (1 means 'the first "
                      "round whose new-finding count did not fall'), or null to switch "
                      "the brake off — 0 is satisfied by every round, including one "
                      "whose count fell, which is the brake off behind a value that "
                      "reads as armed")
    return n


def not_falling_state(series: list[tuple[int, int | None]],
                      limit: int | None) -> dict:
    """#505's measurement as this round read it, for :func:`round_stop` and the
    payload — `injection_state`'s sibling and deliberately its shape.

    ``series`` is one ``(round, count)`` pair per round of the cycle IN ROUND ORDER,
    ending with this round: each round's own ``new_findings``, the count
    `round_stop`'s rule 1 turns on. The count is ``None`` where a round did not
    measure it — a round that reviewed nothing, a baseline whose payload could not be
    read, a payload older than the field. The ROUND NUMBER rides along because a
    missing round has to be told from a falling one; see the streak rules. Nothing
    here is derived from `panel_scope._provenance`, and that is the rung's point: #500
    (a rebase between rounds silently disarms provenance) disarms `fix_injection` and
    cannot disarm this, because a round's own count of its own new findings survives
    the range under it being unreadable.

    **The streak is counted backwards from this round.** A round is part of it when
    four things hold, and any one of them failing ends it:

    - its predecessor in the list is the round immediately BEFORE it. A cycle with a
      gap — round 3 with only round 1's baseline readable, because round 2's payload
      was lost or was never passed to it — has a missing round between the two counts,
      and a missing round is missing data, which must never end a cycle. Comparing
      across the gap would also make this rung's own `reason` untrue: it says "the
      round before", and across a gap that is not the round before. (Found by a codex
      second opinion on #505; the first cut compared adjacent list entries and put the
      decision in a comment, which is a decision documented rather than defended.);
    - both its count and its predecessor's are known. An unknown is not a fall and is
      not a rise; it is the absence of the comparison, and it resets rather than
      being guessed at either way. That is the direction that does not stop a cycle.
      `run()` withholds a round's count for three reasons, and the third is the one
      worth naming here: a round that reviewed nothing, a payload that cannot say
      which round it is, and a round whose BASELINE HISTORY was incomplete — where
      "no earlier round raised it" was decided against baselines this run could not
      read, so the count is inflated by findings an earlier round did raise;
    - its count did not DECREASE — ``>=``, so a flat series counts. A cycle producing
      fifteen new findings a round forever is not converging, and a rule that only
      caught the rise would let it run to the cap;
    - BOTH its count and its predecessor's are at least
      :data:`NOT_FALLING_MIN_NEW`. Both ends, because "not falling" is a claim about a
      SERIES and a series needs two volumes to be one: a round that went from one
      finding to four has not stopped falling, it was never falling — there was no
      volume for it to fall from. See :data:`NOT_FALLING_MIN_NEW` for the floor's own
      argument, and note which half of this the existing suite found: with the floor
      on the current round alone, `test_panel_provenance`'s "a round that mostly found
      what the last one MISSED is not diverging" — 1 finding, then 4 of which one was
      the fix pass's — was ended by this rung rather than by the cap. That round is
      the case the rule's own docstring names as its false positive (an earlier round
      that under-read), and the fix is to require the comparison to be between two
      measurements rather than to make an exception for one fixture.

    Round 1 is never part of a streak, because it has no predecessor — which is the
    whole of why the shipped default is 1 rather than 2. This holds however the caller
    numbers its rounds: what round 1 lacks is a row before it, not the number 1. See
    the `escalate_on` comment in `harness_rules.DEFAULTS`.

    ``over`` is the RULE and is decided here rather than in `round_stop`, on
    `injection_state`'s precedent: what the stop rule receives is a verdict about a
    measurement it has no other way to make, and keeping the arithmetic beside the
    thing it measures is what lets the stop rule stay a rule about findings.

    Every field is present on every round, `premise_state`'s rule and for its reason:
    an absent key and "the brake was off" are different claims. ``count`` and ``was``
    are this round's and its predecessor's, null where there is no such round or the
    round did not measure — never 0, because zero new findings is a claim about a
    round and this is the absence of one. ``rounds`` rides beside ``counts`` so a
    reader can see the gap a streak stopped at rather than having to infer it."""
    rows = list(series)
    rounds = [r for r, _ in rows]
    counts = [n for _, n in rows]
    streak = 0
    for i in range(len(rows) - 1, 0, -1):
        cur, was = counts[i], counts[i - 1]
        if (rounds[i] != rounds[i - 1] + 1
                or cur is None or was is None
                or cur < was
                or cur < NOT_FALLING_MIN_NEW or was < NOT_FALLING_MIN_NEW):
            break
        streak += 1
    return {"limit": limit,
            "rounds": rounds,
            "counts": counts,
            "count": counts[-1] if counts else None,
            "was": counts[-2] if len(counts) > 1 else None,
            "streak": streak,
            "min_new": NOT_FALLING_MIN_NEW,
            "over": bool(limit is not None and streak >= limit)}


def premise_key(text: str) -> str:
    """A stable identity for a premise a fixer DECLARED, from its own words.

    :func:`_key_from_title`'s recipe applied to prose, and deliberately the same
    shape (16 hex characters) so a register is readable beside the finding keys it
    sits next to — but its own namespace, because a premise and a finding are not
    the same kind of thing and a collision between the two would be unreadable.

    Exact on the NORMALISED text (:func:`_norm_title`: words only, lower-cased),
    which is what a re-declaration has to match. The near-miss is
    :func:`same_premise`, and it is a separate step on purpose — this one is the
    identity the register stores, and an identity that moved when a word did could
    not be stored at all."""
    norm = _norm_title(text)
    if not norm:
        return ""
    return hashlib.md5(f"premise|{norm}".encode(),
                       usedforsecurity=False).hexdigest()[:16]


def same_premise(a: str, b: str) -> bool:
    """Are two DECLARED premises the same premise, restated?

    The rule :meth:`Baseline.raised_before` already uses for a reworded finding
    title — a high character ratio as the cheap pre-filter, and :func:`_same_words`
    (the same content words, up to word order and a plural) as the decision — applied
    to a declaration instead of to a title. Reused rather than invented: a second
    similarity rule in this file would be a second thing to calibrate, and #84's
    instruction is explicitly not to build a similarity heuristic.

    Its reach is exactly that of the rule it borrows, and no further: "a repository
    can say where a release number landed" restated in those words is caught, and
    the same premise re-stated through a DIFFERENT PROXY — `rc == 0` one round and
    an artefact's existence the next — is not, because the two share almost no
    words. That gap is why the brake is a declaration and not a detector, and it is
    the same gap `round_stop`'s docstring records for the escalation register: a key
    is not a premise, and neither is a sentence.

    Takes NORMALISED text on both sides (:func:`_norm_title`), which is what the
    register stores."""
    if not a or not b:
        return False
    if a == b:
        return True
    return (difflib.SequenceMatcher(None, a, b).ratio() >= REWORD_RATIO
            and _same_words(a, b))


def working_head(repo_path: str = ".") -> str:
    """The commit the tree a fix is about to be written in is sitting on, or ``""``.

    #560. The brake derives its whole value from running BEFORE the patch — it fires
    on the FIRST declaration and exit 4 means *do not write this fix* — and that
    ordering is unobservable in the one configuration `panel-review-pr.md` recommends
    for more than one PR. Where the orchestrator IS the fixer there is no brief handed
    to a second agent, so no moment between "decide the premise" and "start editing"
    for the brake to occupy, and the premise gets declared against the round it
    followed once the pass is already written and pushed. That declaration and an
    honest one produce byte-identical register entries, so nothing downstream can tell
    a brake from an annotation. This is the one field that can.

    **Read from the current working directory, not from a flag.** A flag was the
    obvious shape and it is the wrong one for the reason `declare_premise` gives for
    making a `no` sticky: the actor supplying the evidence is the actor the evidence is
    about, and a fixer that has to pass `--premise-head` can pass the head it wishes it
    had. `cfg["path"]` was the other candidate and it is wrong differently — in
    worktree mode, which is the mode the collapsed case runs in, the canonical checkout
    is not the tree the patch lands in, so it would stamp a commit that has nothing to
    do with this fix pass.

    **Unreadable is `""` and never a guess**, on :func:`panel_scope._git`'s contract:
    no `git` on PATH, a declaration made outside a checkout, a repository with no
    commit yet. All of them mean "nobody recorded where the tree was", which reads
    downstream as unknown and accuses nobody. Inferring it from the PR instead would
    mint a stamp for a tree the declaration was never made in, and the ordering check
    would then say something false rather than nothing.

    **WHAT THIS IS NOT, said plainly because the first version of #560 overstated
    it.** The directory is the one the declaring PROCESS was launched in, and that is
    the tree the patch lands in only when the declaration was made there. Run from
    the canonical checkout, from a sibling worktree, or outside a repository
    altogether, this records some other tree's head or none — and the ordering check
    then reports nothing, because a stamp matching no round's head is silence. So an
    actor that would rather not be stamped does not have to falsify an argument; it
    can pick a directory. That is #622 — every brake on the fix pass is measured by
    the actor it constrains — and it is not closed here. What IS closed is the
    accident this issue was opened on: an orchestrator-that-is-also-the-fixer
    declaring in its own patch tree, after the pass, with no intent to deceive."""
    sha = (panel_scope._git(repo_path, "rev-parse", "HEAD") or "").strip()
    return sha if _SHA_RE.fullmatch(sha) else ""


def _declared_heads(raw: object, rounds: Iterable[int]) -> dict[int, str]:
    """The heads a register recorded against one declaration's rounds (#560).

    JSON has no integer keys, so the rounds come back as strings and are read to ints
    here — the same round numbers `rounds` already carries, and anything naming a
    round this entry does not claim is dropped along with anything that is not a
    commit id.

    DROPPED, not reported, unlike every other malformed thing `load_premises` finds.
    The register field is younger than the register: every declaration made before
    this existed carries no `heads` at all, and a `problems` line for each of them
    would fire on ordinary history and say nothing a caller can act on. A missing or
    unreadable stamp already has a meaning — the ordering was not checkable on that
    declaration — and it is the meaning that accuses nobody."""
    out: dict[int, str] = {}
    if not isinstance(raw, dict):
        return out
    claimed = set(rounds)
    for key, sha in raw.items():
        try:
            was = int(key)
        except (TypeError, ValueError):
            continue
        if was in claimed and isinstance(sha, str) and _SHA_RE.fullmatch(sha):
            out[was] = sha
    return out


def new_premise_register(repo: str = "", pr: int | None = None) -> dict:
    """An empty register for one PR's cycle."""
    return {"version": PREMISE_REGISTER_VERSION, "repo": repo, "pr": pr,
            "premises": []}


def load_premises(path: str, repo: str = "", pr: int | None = None
                  ) -> tuple[dict, list[str]]:
    """The cycle's premise register, and everything wrong with it.

    A MISSING file is not a problem: the first declaration of a cycle creates it,
    and a cycle that never declared one is the ordinary undeclared case the brake
    reports rather than an error.

    Everything else is REPORTED and read as empty, never guessed at. The failure a
    silent read would cause is the one the brake exists to prevent, arriving with
    nothing said: an unreadable register makes the second occurrence of a premise
    look like the first, and the fix gets written.

    ``repo``/``pr`` are checked when they are given, on `load_baseline`'s rule and
    for its reason — a register wired to another PR's path counts occurrences from
    another cycle, and a mis-wired path must show up as a reported problem rather
    than as a brake that fires or does not for reasons nobody can see."""
    problems: list[str] = []
    reg = new_premise_register(repo, pr)
    if not path:
        return reg, problems
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return reg, problems
    except (OSError, ValueError) as e:
        problems.append(f"premise register {path} could not be read "
                        f"({e.__class__.__name__}) — read as EMPTY, so a premise "
                        "declared before this round counts as declared for the first "
                        "time and the futility brake will not fire on it")
        return reg, problems
    if not isinstance(raw, dict) or not isinstance(raw.get("premises"), list):
        problems.append(f"premise register {path} is not a premise register (no "
                        "`premises` list) — read as EMPTY, so no earlier declaration "
                        "counts")
        return reg, problems
    was_repo, was_pr = str(raw.get("repo") or ""), raw.get("pr")
    if repo and was_repo and was_repo != repo:
        problems.append(f"premise register {path} belongs to {was_repo}, not {repo} — "
                        "read as EMPTY rather than counting another repo's premises")
        return reg, problems
    if pr is not None and isinstance(was_pr, int) and was_pr != pr:
        problems.append(f"premise register {path} belongs to PR #{was_pr}, not #{pr} — "
                        "read as EMPTY rather than counting another cycle's premises")
        return reg, problems
    kept = []
    for entry in raw["premises"]:
        if not isinstance(entry, dict) or not str(entry.get("text") or "").strip():
            problems.append(f"premise register {path} carries an entry that is not a "
                            "declaration — it was NOT counted")
            continue
        rounds = [r for r in (entry.get("rounds") or [])
                  if isinstance(r, int) and not isinstance(r, bool)]
        if not rounds:
            problems.append(f"premise register {path} carries a declaration that names "
                            "no round — it was NOT counted, so the occurrence it stands "
                            "for is invisible to the brake")
            continue
        text = str(entry["text"]).strip()
        # An unrecognised `decidable` reads as "unknown" HERE, unlike in
        # `declare_premise` where it raises. The two are different failures: a bad
        # argument is a caller to correct, and a bad value on disk is a register a
        # later harness (or a hand edit) wrote, which must not stop the cycle. It
        # degrades to the value that never brakes, and the entry is otherwise kept.
        answer = str(entry.get("decidable") or "unknown").strip().lower()
        kept.append({"key": premise_key(text), "text": text,
                     "norm": _norm_title(text), "rounds": sorted(rounds),
                     "decidable": answer if answer in DECIDABILITY else "unknown",
                     "heads": _declared_heads(entry.get("heads"), rounds),
                     "findings": sorted({_key_norm(k) for k in (entry.get("findings") or [])
                                         if _is_key(k)})})
    reg["premises"] = kept
    reg["repo"], reg["pr"] = repo or was_repo, pr if pr is not None else was_pr
    return reg, problems


def find_premise(reg: dict, text: str) -> dict | None:
    """The register's entry for this premise, restatements included, or None."""
    norm = _norm_title(text)
    key = premise_key(text)
    for entry in reg.get("premises") or []:
        if entry.get("key") == key or same_premise(norm, entry.get("norm") or ""):
            return entry
    return None


def declare_premise(reg: dict, text: str, round_no: int,
                    findings: Iterable[str] = (), limit: int | None = None,
                    decidable: str = "unknown",
                    undecidable_brake: bool = False, head: str = "") -> dict:
    """Record that a fix pass is about to be written against ``text``, and say
    whether it may be.

    The occurrence is recorded whether or not the brake fires, and that is the
    point of it being a register rather than a check: what #84 counts is
    DECLARATIONS, the log has to hold the one that was stopped as well as the ones
    that were allowed, and "the second time" is not a fact any single call knows on
    its own.

    ``round_no`` is the round whose findings this fix pass is answering — so a
    declaration for round 2 is about the fix that follows round 2, which is what
    :func:`undeclared_passes` counts against. Re-declaring the same premise inside
    ONE round is not a repeat and does not count twice: a fixer that states its
    premise, is interrupted and states it again has proposed one fix pass, and
    counting the restatement would fire the brake on a cycle that never circled.

    ``findings`` are the keys this fix pass would have cleared. They are what the
    caller passes to the next round's ``--escalated`` when the brake fires, which
    is how this composes with `round_stop` instead of growing a second stop: a
    braked premise becomes an ESCALATION, the outcome the loop already knows how to
    end a cycle on.

    ``decidable`` is #491's question, asked of the declaration rather than of the
    cycle: **can the runtime this fix's assertion runs in observe the property the
    fix asserts?** ``"no"`` with ``undecidable_brake`` on refuses the fix on its
    FIRST occurrence, which is the one thing the occurrence counter structurally
    cannot do.

    The reason it cannot is not a bug in the matching. A fixer circling an
    unobservable property replaces one PROXY with a better one each round and
    declares, accurately, a different premise every time — four were declared on one
    cycle and no two matched, so the counter sat at 1 while three fix passes circled.
    :func:`same_premise` records the same gap from the other side, and #84 rules out
    closing it with a similarity heuristic. What closes it is not a better comparison
    between declarations; it is one more question put to each declaration on its own,
    whose answer does not depend on the words the fixer chose.

    ``"unknown"`` is the honest default and never brakes: every declaration made
    before this existed reads that way, and a caller that has not been taught
    ``--premise-decidable`` must not have an answer inferred for it. #84's rule for
    the undeclared fix pass is the same rule — report the gap, never guess at it.

    A ``"no"`` already on the entry, though, is not a gap — it is an answer, and it
    STICKS. Neither a later ``"yes"`` nor a later silence clears it, and the brake
    reads the entry rather than the declaration in front of it. See the comment on
    the assignment for why: everything else here would let the one agent whose fix is
    being refused lift its own refusal by changing its answer.

    ``head`` is where the tree stood when this was declared (#560), from
    :func:`working_head`. It records nothing about the premise and everything about
    WHEN the sentence was said, and it settles exactly one of the two ways the
    ordering fails: the pass that was already COMMITTED when the premise was declared,
    which is the shape #560 reported and which :func:`retroactive_declarations` reads.
    The pass that was merely WRITTEN moves no head and is not settled here — see that
    function for the three attempts at it and why the evidence does not carry it."""
    text = " ".join(str(text).split())
    answer = str(decidable or "unknown").strip().lower()
    if answer not in DECIDABILITY:
        # Named rather than coerced to "unknown". A typo silently read as "unknown"
        # is a brake that does not fire on a declaration that answered "no", which is
        # this mechanism failing in exactly the direction it exists to prevent.
        raise ValueError(
            f"declare_premise(decidable={decidable!r}) takes one of "
            f"{', '.join(DECIDABILITY)} — the fixer's answer to whether the runtime "
            "can observe the property the fix asserts")
    keys = sorted({_key_norm(k) for k in findings if _is_key(k)})
    entry = find_premise(reg, text)
    if entry is None:
        entry = {"key": premise_key(text), "text": text, "norm": _norm_title(text),
                 "rounds": [], "findings": [], "decidable": "unknown", "heads": {}}
        reg.setdefault("premises", []).append(entry)
    if round_no not in entry["rounds"]:
        entry["rounds"] = sorted([*entry["rounds"], round_no])
    entry["findings"] = sorted({*entry["findings"], *keys})
    entry.setdefault("decidable", "unknown")
    # **THE FIRST STAMP FOR A ROUND WINS**, which is the same rule the round count
    # already applies to a restatement and for the same reason. A fixer that states
    # its premise, is interrupted and states it again has proposed one fix pass; if
    # the second statement moved the stamp forward, an honest fixer that declared
    # first and restated mid-pass would be recorded as having declared after its own
    # patch — the exact accusation this field exists to make, aimed at the one case
    # it must never be aimed at. Overwriting also hands the actor the eraser: any
    # declaration could be back-dated by re-declaring from a stale checkout.
    stamp = str(head or "").strip()
    entry.setdefault("heads", {})
    if stamp and _SHA_RE.fullmatch(stamp) and round_no not in entry["heads"]:
        entry["heads"][round_no] = stamp
    # **A `no` is STICKY, and the brake reads the ENTRY rather than this declaration.**
    # Both halves close the same hole, and it is the hole every self-reported signal in
    # this loop has: the agent whose fix is being refused is the one supplying the
    # answer. Without stickiness a fixer refused on `no` re-declares the same premise
    # with `yes` and the refusal is gone, with nothing recording that it ever happened;
    # without reading the entry, it re-declares with the flag simply OMITTED and
    # "unknown" brakes nothing. Either way the actor clears its own brake by changing
    # what it says, which is precisely what `round_stop`'s docstring says cannot be
    # left to self-report.
    #
    # So: `no` is established about the PROPERTY, not about one pass's opinion of it,
    # and a property the runtime cannot observe does not become observable because a
    # later declaration says otherwise. `yes` records freely until a `no` lands, which
    # keeps the ordinary case — a fixer answering honestly, round after round — exactly
    # as cheap as it was.
    if answer == "no" or (answer == "yes" and entry["decidable"] != "no"):
        entry["decidable"] = answer
    occurrence = len(entry["rounds"])
    repeated = limit is not None and occurrence >= limit
    undecidable = bool(undecidable_brake) and entry["decidable"] == "no"
    escalate = repeated or undecidable
    reasons = []
    if undecidable:
        reasons.append(
            "the property this fix asserts is NOT decidable in the runtime the "
            "assertion runs in, so every fix for it is an approximation and the next "
            "round finds the gap between the approximation and the property "
            "(`escalate_on.premise_undecidable`): a human answers this, not a better "
            "approximation")
    if repeated:
        reasons.append(
            f"premise declared {occurrence} time(s) — rounds "
            f"{', '.join(str(r) for r in entry['rounds'])} — and the brake is set "
            f"at {limit}: a human answers this premise, not another fix pass")
    if reasons:
        reason = "; and ".join(reasons)
    elif limit is None:
        reason = (f"recorded (occurrence {occurrence}) — `escalate_on.premise_repeated` "
                  "is off, so nothing brakes on a repeat")
    else:
        reason = (f"recorded (occurrence {occurrence} of {limit}) — write the fix")
    return {"key": entry["key"], "text": text,
            "restates": entry["text"] if entry["norm"] != _norm_title(text) else "",
            "occurrence": occurrence, "rounds": list(entry["rounds"]),
            "first_round": entry["rounds"][0], "findings": list(entry["findings"]),
            "limit": limit, "escalate": escalate, "reason": reason,
            "decidable": entry["decidable"], "answered": answer,
            "repeated": repeated, "undecidable": undecidable,
            "undecidable_brake": bool(undecidable_brake),
            "head": entry["heads"].get(round_no, ""),
            "undeclared_rounds": undeclared_passes(reg, round_no)}


def undeclared_passes(reg: dict, round_no: int) -> list[int]:
    """Rounds of this cycle whose fix pass declared no premise at all.

    A fix pass follows every round the cycle did not stop on, so rounds ``1`` to
    ``round_no - 1`` each had one; a round with no declaration against it is a fix
    pass the brake could not have evaluated. #84's rule for it is explicit — treat
    an undeclared fix as UNESCALATABLE rather than pretending to infer its premise —
    and this is the half that makes "unescalatable" a thing the payload says out
    loud instead of a silence."""
    declared = {r for e in (reg.get("premises") or []) for r in e.get("rounds") or []}
    return [r for r in range(1, max(round_no, 1)) if r not in declared]


def retroactive_declarations(reg: dict, heads: Mapping[int, str] | None) -> list[dict]:
    """Declarations this cycle's own records place after the fix pass they explain
    (#560), as ``[{key, text, round, head, head_round}, …]``.

    ``heads`` is round -> the commit that round reviewed, which the round already has:
    every earlier round's from `Baseline.head_shas`, and this round's from its own
    `head_sha`. A declaration for round R stamped with the head of some LATER round
    was said after the round-R fix pass was written, committed and pushed, so exit 4
    had no patch left to refuse. That is what this reports, and it is the shape #560
    was opened on — the issue says the premise was declared "after the pass had been
    written, committed and pushed", twice.

    **Positive identification only, and everything else is silence.** A stamp
    matching no round's head is not reported — a fixer that declared from an
    unpushed local commit, an amend, a rebase, a stray checkout and a declaration
    made from the wrong directory all land there, and the honest reading of all of
    them is that the ordering was not checkable. The alternative rule — "anything
    that is not the round's own head is late" — would accuse an honest fixer for a
    rebase it did not perform, and a check that cries wolf on ordinary history is one
    an orchestrator learns to pass over. This fires on a fact rather than on an
    absence: the tree was carrying a commit the cycle itself recorded as arriving
    after the round in question, and there is no innocent way for that to be true.

    **WHAT THIS DELIBERATELY DOES NOT REPORT, and the three attempts it cost.** A fix
    pass that edits the working tree and declares BEFORE committing has not moved
    `HEAD`, so its stamp equals its own round's head and is byte-identical to the
    honest declaration made before the first edit. Edit, then commit is the ordinary
    shape of a fix pass, so this is not a corner. It is also **not detectable from
    anything this loop can read**, and each attempt failed in its own way:

    * *Ask whether the declaring tree is dirty.* An unrelated edit predating the
      round, a tracked generated file, a staged unrelated change and a dirty submodule
      all answer yes — and `review-pr.md` permits pre-existing dirt outright in
      fix-in-place mode. It accused the honest fixer, which is worse than the
      detection it bought: an operator switches such a check off.
    * *Compare a porcelain fingerprint the round took against one the declaration
      took.* Better, and still wrong twice over. A concurrent agent in a shared
      checkout, an editor autosave or a background build moves the tree between the
      two readings with no fix pass involved — and this fleet runs several agents in
      shared checkouts routinely. Meanwhile a file ALREADY modified when the round ran
      fingerprints as ` M path` both times however much the fix pass then changed it,
      so the case the comparison existed for slipped through.
    * *Hash contents instead of status codes.* Closes the second half and leaves the
      first untouched, because the ambiguity is not in the digest. It is in the
      inference: a tree change cannot be attributed to a particular actor from
      evidence read in that actor's own environment. That is #622, and it is not
      solvable here.

    So the ordering ahead of an uncommitted patch is UNCHECKED rather than checked and
    found clean, and this list's silence must be read that way. The alternative — an
    entry that says "the tree differed, and I cannot tell you why" — was considered
    and rejected: in this fleet it would fire on ordinary concurrent work, which is
    the loud-and-wrong the `config_notes` gate on `undeclared_passes` already argues
    against, and it would enumerate part of a category (declarations whose ordering
    could not be confirmed) while the far larger part of it went unlisted.

    Ties go to the EARLIEST round holding a head, which matters when a fix pass
    pushed nothing and two rounds reviewed the same commit. Then "which round does
    this stamp belong to" has more than one answer and the earliest is the only one
    that cannot manufacture an accusation out of a pass that changed nothing.

    Compares commit ids and asks `git` nothing. Every sha here was recorded by the
    cycle itself, so this costs a round no subprocess, no network call and no local
    checkout — which is what lets it run on every round rather than on the terminal
    one, the way :func:`panel_scope.fix_pass_commits` has to."""
    first_at: dict[str, int] = {}
    for was, sha in sorted((heads or {}).items()):
        if isinstance(sha, str) and sha:
            first_at.setdefault(sha, was)
    late = []
    for e in reg.get("premises") or []:
        stamps = e.get("heads") or {}
        for was in sorted(e.get("rounds") or []):
            at = first_at.get(stamps.get(was) or "")
            if at is None or at <= was:
                continue
            late.append({"key": e["key"], "text": e["text"], "round": was,
                         "head": stamps[was], "head_round": at})
    return sorted(late, key=lambda d: (d["round"], d["key"]))


def premise_state(reg: dict, round_no: int, limit: int | None = None,
                  undecidable_brake: bool = False,
                  heads: Mapping[int, str] | None = None,
                  wired: bool = False) -> dict:
    """What the cycle's declarations say, for `round_stop` and for the payload."""
    entries = reg.get("premises") or []

    def view(e: dict) -> dict:
        return {"key": e["key"], "text": e["text"], "rounds": list(e["rounds"]),
                "occurrences": len(e["rounds"]),
                "decidable": e.get("decidable") or "unknown",
                "findings": list(e.get("findings") or [])}

    repeated = [view(e) for e in entries
                if limit is not None and len(e.get("rounds") or []) >= limit]
    # Reported whether or not the brake is armed, on `undeclared_rounds`' rule: the
    # payload says what the cycle DECLARED, and a repo that switched the brake off
    # still gets to see that a fix pass was written against a property nothing in its
    # runtime can observe. `round_stop` is what gates on the flag.
    undecidable = [view(e) for e in entries
                   if (e.get("decidable") or "unknown") == "no"]
    # #560's two halves, and they answer two different questions a reader of
    # `undeclared_rounds` alone cannot tell apart.
    #
    # `wired` is whether THIS ROUND'S INVOCATION was given a `--premise-file` path,
    # and that sentence is the whole of what it claims. It is worth carrying because
    # without it a cycle that never wired a register and a cycle whose fixer skipped a
    # declaration produce the same `undeclared_rounds` list, and on lexray#1697 the
    # first happened: the orchestrator handed the fixer no register path, `panel.py
    # --premise` was uncallable during the pass, and that was visible only because the
    # fixer said so.
    #
    # **It does NOT establish that the fixer could reach the register**, and the first
    # version of #560 claimed it drew that distinction. The round and the fix pass are
    # separate invocations: a round can be handed a path the fixer was never told
    # about, or a path the fixer could not write. What the payload does hold about the
    # fixer's side is `declared` — a register with entries in it was reached by
    # somebody — and `undeclared_rounds`, which names the passes that left none. The
    # honest reading of `wired: false` with rounds listed is "the reader of this
    # cycle was not even pointed at a register", not "the fixer had no brake".
    #
    # `stamped` is how many round-declarations carried a head, which is what makes
    # `retroactive`'s silence readable: zero stamps means the ordering was not
    # checkable on this cycle, not that it checked out. It is a floor on
    # checkability and not a ceiling — a stamped declaration is checkable against
    # `retroactive`'s one shape, the pass already committed when it was said, and
    # nothing in this payload checks the pass that was written and not yet committed.
    # `retroactive_declarations` has the three attempts at that and why none held.
    return {"limit": limit,
            "declared": len(entries),
            "repeated": repeated,
            "undecidable": undecidable,
            "undecidable_brake": bool(undecidable_brake),
            "wired": bool(wired),
            "stamped": sum(len(e.get("heads") or {}) for e in entries),
            "retroactive": retroactive_declarations(reg, heads),
            "undeclared_rounds": undeclared_passes(reg, round_no)}


def injection_state(counts: dict | None, limit: float | None) -> dict:
    """#489's measurement as this round read it, for `round_stop` and the payload.

    `counts` is `panel.py`'s `provenance_counts` and nothing else: it is
    `panel_scope.PROVENANCE`'s four buckets, non-negative integers, over every NEW
    outstanding finding — empty (`{}` or ``None``) on a round with nothing to
    attribute, which is round 1 or a cycle whose fix range could not be read at all.
    That is a CONTRACT and not a defended boundary. Unlike `round_stop`'s two
    door checks, which exist because a `str` where a list belongs fails SILENTLY and
    goes on running the loop, every wrong shape here is either loud at once (a
    non-mapping raises) or arrives from a caller this module ships beside — and
    validation that can only fire on a bug in the file next door buys a second place
    for the schema to be written down and disagree with the first. The
    rate is `introduced / (every bucket)`, and the three buckets that are not
    `introduced` sit in the DENOMINATOR on purpose:

    - `missed` belongs there because it is the other half of the same question, and
      the ratio between them is the whole signal;
    - `unknown` and `missed-unread` belong there because they DEPRESS the rate, and
      that is the direction a stop should fail in. A round the harness could not
      place is a round that does not end the cycle, which is the same posture
      `_provenance` itself takes when it declines to guess.

    Every field is present on every round, `premise_state`'s rule and for its
    reason: an absent key and "the brake was off" are different claims, and a
    consumer that had to tell them apart would be reading a payload's age rather
    than a cycle's state. `rate` is `None` — not `0.0` — where there is nothing to
    divide, because zero is a claim about a fix pass and this is the absence of one.

    `over` is the RULE and it is decided here rather than in `round_stop`, on
    `premise_state`'s precedent: what the stop rule receives is a verdict about a
    measurement it has no other way to make, and keeping the arithmetic beside the
    thing it measures is what lets the stop rule stay a rule about findings."""
    counts = counts or {}
    introduced = int(counts.get("introduced") or 0)
    total = sum(int(counts.get(b) or 0) for b in PROVENANCE)
    # ROUNDED FIRST, AND THE VERDICT IS TAKEN ON THE ROUNDED NUMBER. The payload
    # carries `rate`, `limit` and `over` side by side, and a reader has to be able to
    # check one against the other: deciding on the full float and publishing four
    # decimal places would let a round record `rate: 0.5, limit: 0.5, over: true`,
    # which reads as the strict comparison being broken. Four places is far finer
    # than any denominator a round of findings can produce, so this changes no real
    # verdict — it only stops the artifact contradicting itself.
    rate = None if not total else round(introduced / total, 4)
    over = bool(limit is not None and rate is not None
                and total >= FIX_INJECTION_MIN_NEW and rate > limit)
    return {"limit": limit, "introduced": introduced, "new": total, "rate": rate,
            "min_new": FIX_INJECTION_MIN_NEW, "over": over}


def unrefereed_fix_brake(panel: dict, notes: list[str]) -> bool:
    """`review_panel.escalate_on.unrefereed_fix` (#554) — does a fix pass that wrote
    nothing anything can check end the cycle?

    Read per KEY through the same fallback :func:`premise_repeat_limit` uses and for
    the identical reason: `review_panel` merges one level deep, so a repo writing
    `escalate_on` at all replaces the default object wholesale, and without the
    per-key fallback `{"premise_repeated": 2}` would silently switch THIS brake off.

    **A flag, on :func:`premise_undecidable_brake`'s precedent and for its reason.**
    That one is not counting either: it reads a fixer's answer to a question with a
    fact for an answer. This reads a fact about the pass itself — whether a single
    line of it landed where red/green, the suite or CI could catch it being wrong —
    and "none did" is already the whole finding. A count over it would mean "write
    unrefereed passes N times first", which is the behaviour the rule refuses.

    **And a flag rather than a FRACTION, which is what makes it shippable as a gate
    on one cycle's evidence.** #67's rule is that an instrument earns a threshold over
    a few dozen cycles or not at all — the reason `panel_seats.guard_ratio` ships
    report-only. There is no threshold here to earn: the rule is a predicate, and a
    predicate has nothing to calibrate. A fraction would need a number nobody has
    measured AND would be wrong on the commonest healthy shape there is, a small
    production fix carrying a large regression test.

    **The exact scope of that claim, which a Codex second opinion was right to press
    on.** "Zero production lines" is not ground truth; it is what
    :func:`panel_seats.referee_split` returned, and that reader is a set of
    heuristics — a comment-marker table, a docstring fence tracker, a path
    classifier. So the argument is not "the predicate cannot be wrong". It is that
    **every way it can be wrong leans the same way**: toward counting a line as
    production, which makes the pass look refereed and the brake decline to fire.
    That property is what a threshold-free rule buys, and it is the whole of the case
    for gating here — it is asserted by `referee_split`'s own docstring, tested
    directly, and two violations of it (a bare `*` marker eating a pointer store, a
    fence tracker ending a docstring a line early) were real and are fixed. A third
    would be a bug of the same class and should be treated as one, not as a reason to
    add a number.

    (:data:`panel_seats.UNREFEREED_MIN_CHURN` is not that number. It is a minimum
    SAMPLE — the same structural role :data:`FIX_INJECTION_MIN_NEW` plays for a rule
    whose threshold is 0.5 — and it can only make this rung fire less often. A floor
    under a predicate is not a threshold on it.)

    ``false`` is a second spelling of ``null`` and is honoured as one, exactly as its
    two siblings honour it. ``true`` is the only other value; anything else is a hard
    exit through :func:`panel_seats._refuse_value`, on the line every dial in this
    file draws between an unknown key and a malformed value of a known one."""
    raw = panel.get("escalate_on", _ABSENT)
    if raw is _ABSENT or raw is None or raw == "":
        rules: dict = dict(ESCALATE_ON_DEFAULTS)
    elif isinstance(raw, dict):
        rules = raw
    else:
        # Already refused by `premise_repeat_limit` on every real path — all four
        # readers run off one config, and `run()` calls that one first — but this
        # function is public and is called directly by tests, so it does not rely on
        # a sibling having been called before it.
        _refuse_value("escalate_on", raw,
                      'a JSON object of reserved matters, e.g. {"premise_repeated": 2}')
        return False                                  # unreachable
    want = rules.get("unrefereed_fix", ESCALATE_ON_DEFAULTS.get("unrefereed_fix"))
    if want is None or want is False or want == "":
        return False
    if want is True:
        return True
    _refuse_value("escalate_on.unrefereed_fix", want,
                  "true to end a cycle whose fix pass wrote nothing any mechanism "
                  "can check, or false/null to leave the brake off. There is no "
                  "number here: the rule is a predicate on the pass, not a threshold")
    return False                                      # unreachable


def guard_lines_brake(panel: dict, notes: list[str]) -> bool:
    """`review_panel.escalate_on.guard_lines` (#618) — does a fix pass that wrote
    more guard lines than `max_fix_guard_lines` allows END the cycle, or is the
    crossing only reported?

    Read per KEY through the same fallback its four siblings use and for the identical
    reason: `review_panel` merges one level deep, so a repo writing `escalate_on` at
    all replaces the default object wholesale.

    **A flag whose threshold lives one key away**, which is `escalate_on.unrefereed_fix`
    over :data:`panel_seats.UNREFEREED_MIN_CHURN` exactly. The number and the verdict
    are separable decisions and #67 is the reason they have to be here: an instrument
    earns a gate over a few dozen cycles or not at all, and this one has ONE — the five
    rounds of lexray#1780. So a repo that writes a ceiling gets it measured and
    reported, and has to say so a second time before a round ends on it. Writing the
    threshold here as well would be one number in two files with two chances to
    disagree.

    **Off by default, and the asymmetry with `unrefereed_fix` is the whole argument.**
    That rung is a PREDICATE — zero refereed lines — and a predicate has nothing to
    calibrate, which is what made it shippable as a gate on one cycle's evidence. This
    is a THRESHOLD, so the same evidence buys the weaker action only. `max_fix_growth`
    ends a cycle on #188 and #236; this has one PR, and the dial is what keeps the
    stronger reading one flag away rather than a release away.

    ``false`` is a second spelling of ``null`` and is honoured as one; ``true`` is the
    only other value, and anything else is a hard exit through
    :func:`panel_seats._refuse_value` — the line every dial in this file draws between
    an unknown key and a malformed value of a known one."""
    raw = panel.get("escalate_on", _ABSENT)
    if raw is _ABSENT or raw is None or raw == "":
        rules: dict = dict(ESCALATE_ON_DEFAULTS)
    elif isinstance(raw, dict):
        rules = raw
    else:
        # Already refused by `premise_repeat_limit` on every real path — `run()` calls
        # that one first — but this function is public and is called directly by
        # tests, so it does not rely on a sibling having been called before it.
        _refuse_value("escalate_on", raw,
                      'a JSON object of reserved matters, e.g. {"premise_repeated": 2}')
        return False                                  # unreachable
    want = rules.get("guard_lines", ESCALATE_ON_DEFAULTS.get("guard_lines"))
    if want is None or want is False or want == "":
        return False
    if want is True:
        return True
    _refuse_value("escalate_on.guard_lines", want,
                  "true to end a cycle whose fix pass churned more guard lines than "
                  "`max_fix_guard_lines` allows, or false/null to report the crossing "
                  "and go on. The threshold is that key and is not written here")
    return False                                      # unreachable


def referee_state(split: dict | None, armed: bool) -> dict:
    """#554's measurement as this round read it, for `round_stop` and the payload.

    ``split`` is :func:`panel_seats.referee_split` over the fix range and nothing
    else — the churn of the pass that landed between the last round and this one,
    classified into `production`/`test`/`prose`. ``None`` (or an empty mapping) on a
    round with no pass to read, which is round 1 or a cycle whose fix range could not
    be got at all. That is a CONTRACT and not a defended boundary, on
    :func:`injection_state`'s terms and for its reason: every wrong shape here is
    either loud at once or arrives from the file next door, and validation that can
    only fire on a bug in a sibling buys a second place for the schema to disagree
    with the first.

    ``armed`` is :func:`unrefereed_fix_brake`'s answer. It is carried into the state
    rather than consulted in `round_stop` because the payload has to record what the
    cycle MEASURED whether or not the repo asked for it to be acted on — the same
    split `premise_state` keeps between its `undecidable` list and its
    `undecidable_brake` flag, and for the same reason: a repo that switched the brake
    off must still be able to see that a fix pass wrote nothing checkable.

    ``over`` is the RULE, and it is decided here rather than in `round_stop` on
    :func:`injection_state`'s precedent: what the stop rule receives is a verdict
    about a measurement it has no other way to make. Three conjuncts, and each is
    load-bearing:

    * **``armed``** — the repo did not switch it off;
    * **``churn >= UNREFEREED_MIN_CHURN``** — the pass is big enough for "none of it
      was refereed" to be a statement about the pass rather than about one line;
    * **``production == 0``** — and this is the predicate itself. Not a share over a
      threshold: a 5-line production fix carrying a 40-line regression test is 89%
      unrefereed and is exactly the work the panel wants, so the claim has to be the
      ABSENCE of a refereed component, which is what #554 measured.

    Every field is present on every round, `premise_state`'s and `injection_state`'s
    rule and for their reason. ``share`` is `None` where there was no churn to divide,
    because zero is a claim about a fix pass and this is the absence of one."""
    split = split or {}
    counts = {k: int(split.get(k) or 0) for k in panel_seats.REFEREE_KINDS}
    churn = sum(counts.values())
    unrefereed = counts["test"] + counts["prose"]
    over = bool(armed and churn >= panel_seats.UNREFEREED_MIN_CHURN
                and counts["production"] == 0)
    return {"armed": bool(armed), **counts, "churn": churn,
            "unrefereed": unrefereed,
            "share": round(unrefereed / churn, 4) if churn else None,
            "min_churn": panel_seats.UNREFEREED_MIN_CHURN, "over": over}


def guard_churn_state(referee: dict | None, limit: int | None, armed: bool) -> dict:
    """#618's measurement as this round read it, for `round_stop` and the payload.

    ``referee`` is :func:`referee_state`'s own output and nothing else — the churn of
    the pass that landed between the last round and this one, already classified. This
    reads the SAME object rather than re-splitting the diff, on the rule every count in
    this file follows: two derivations of one quantity are two things that can
    disagree, and the report prints both numbers next to each other.

    ``lines`` is `test + prose`, which is #554's `unrefereed` bucket. That is not a
    coincidence and it is worth saying why the two features share a numerator: the
    lines with no referee and the lines that are guard rather than guarded are the same
    lines, so a second definition here would let a repo's budget (`unrefereed_line_weight`
    prices exactly this bucket) and its ceiling count different things.

    **It is a ceiling on the PASS and it does not bank.** ``referee`` is one round's fix
    range; nothing earlier is in it, and nothing carries forward. A quiet round
    therefore cannot fund a loud one, which is the case the ceiling exists for and the
    thing a cumulative reading gets wrong — on lexray#1780 `guard_ratio` fell every
    round of a five-round runaway because the runaway moved both halves of the
    proportion together.

    ``over`` is a property of the MEASUREMENT and takes no notice of ``armed``, on
    :func:`injection_state`'s split rather than :func:`referee_state`'s: the whole point
    of shipping this uncalibrated is that a repo may set a ceiling to WATCH it, and a
    round that crossed a watched ceiling must record that it did. ``armed`` says
    whether the crossing may also end the cycle, and `round_stop` publishes ``fired``
    beside both.

    Every field is present on every round, its three siblings' rule. ``limit: None`` is
    the honest reading of "nothing was being checked", and it is the shipped one.

    **``lines`` IS ``None`` WHERE NOBODY READ THE PASS, AND MUST NEVER BE A ZERO**
    (found by a codex second opinion, which was right that the first draft had this
    backwards). Round 1 has no pass to read and a rewritten branch has no readable
    range, and a `0` published for either says a fix pass wrote no guard line when what
    happened is that nothing was looked at — :func:`fix_surface_state`'s rule, and it
    is the FLATTERING direction here, since "wrote nothing" is the strongest possible
    version of the very claim this ceiling exists to make. The VERDICT was safe either
    way — ``over: False``, the #500 posture its two neighbours take, so a round that
    could not see the pass never ends a cycle on it — but the number a human reads was
    not.

    The presence test is ``churn``, on :func:`churn_cells`' terms and with its one
    accepted conflation: a pass that genuinely churned nothing records zeros in every
    bucket and is indistinguishable in this payload from a round that read no range.
    That costs an empty fix range a printed `0` and buys never printing one for round
    1."""
    referee = referee or {}
    lines = (int(referee.get("unrefereed") or 0)
             if _nonneg_int(referee.get("churn")) else None)
    return {"limit": limit, "lines": lines, "armed": bool(armed),
            "over": bool(limit is not None and lines is not None and lines > limit)}


def fix_budget_state(referee: dict | None, limit: int | None, weight: int,
                     band: bool) -> dict:
    """#622's measurement as this round read it, for `round_stop` and the payload —
    what the last fix pass SPENT, priced the way `low_severity_fix_lines` is spent,
    counted by something that is not the fixer.

    Every other bound on a fix pass in this file is measured from outside it. This one
    was not: `low_severity_fix_lines` is resolved here, relayed into the fixer's brief
    as a paragraph ("measure each fix's churned lines (`git diff --numstat`) … stop
    when the budget is spent"), and then counted by the agent it constrains. On
    lexray#1780 the relayed number was correct at every round and the passes came out
    at 850, 322, 356 and 142 added lines against a budget of 40 — and nothing anywhere
    recorded that, because the only reader was the actor. `harness_rules.py` already
    says the principle beside the dial: the fixer "is never asked 'does this risk
    ballooning?', because that is a judgement by the actor whose judgement the 85%
    impugns". The counting was delegated to that same actor. This is the reader.

    ``referee`` is :func:`referee_state`'s own output and nothing else, on
    :func:`guard_churn_state`'s rule and for its reason: `low_severity_fix_lines`,
    `max_fix_guard_lines` and #554's predicate are three readings of ONE split, and a
    second derivation here would let the report print two numbers for the same lines.
    ``limit`` and ``weight`` are `low_severity_fix_lines` and `unrefereed_line_weight`
    off the resolved dials — the very values relayed in the brief, so the reader and
    the actor are held to one number by construction rather than by two copies.

    ``spend`` prices the pass exactly as the brief prices it: production at 1 and test
    plus prose at ``weight``, over churn (insertions plus deletions), which is the unit
    `git diff --numstat` reports and :func:`panel_seats._referee_kind_lines` already
    counts in.

    **``within`` IS ONE-SIDED, AND SAYING SO IS THE WHOLE HONESTY OF THIS BLOCK.** The
    budget does not bound the pass; it bounds the part of the pass spent on the 💸 band
    — findings at or above `fix_severity_floor` and below the `round_trigger_floor`
    cut — and a diff cannot attribute a line to the finding it was written for. So the
    priced total is an UPPER BOUND on the budgeted spend, and only one of its two
    readings is a fact:

    * ``within: True`` — the WHOLE pass, mandatory work included, priced under the
      budget. The budgeted part is under it too, whatever anybody counted. That is
      decidable from the diff alone and it is the verdict this block exists to
      publish.
    * ``within: False`` — the pass cannot be SHOWN to have stayed inside its budget.
      Not a breach: a round clearing two P1s may spend three hundred production lines
      the budget never applied to. It is the absence of the assurance above, and a
      reader must not read it as an accusation.
    * ``within: None`` — nothing was measured. Round 1, an unreadable fix range, a
      repo with no budget written, or a repo whose `fix_severity_floor` meets the
      trigger cut so there is no band to pay for (``band``).

    **The sharper measurement that was considered and left out.** Where a round's
    entire To fix list is budgeted — no mandatory finding in it at all — every line of
    the pass answering it IS budget spend, and ``spend > limit`` is then a breach
    outright rather than an unproven one. That needs the PRIOR round's list and the
    dials it was banded under, read back out of a baseline payload, and it would put
    this block's verdict at the mercy of a payload written under different dials by a
    different version. #622 asks for the cheap half first and says so; the upper bound
    needs nothing but the split already in hand, and the strict form can be added over
    it later without either number changing meaning.

    **Reported, never gated, on #67's rule** — and unlike `max_fix_guard_lines` there
    is not even a flag to arm, because arming one would be the 29th dial #621 forbids.
    Nothing in :func:`round_stop` reads this to move ``stop``. What it does is put the
    count somewhere a human and an orchestrator can both read it, in the same register
    `unrefereed_fix` and `fix_surface` already occupy: a pass that overspent is
    evidence about the pass.

    Every field is present on every round, its three siblings' rule. ``spend`` is
    ``None`` and never ``0`` where nobody read the pass, on
    :func:`guard_churn_state`'s argument and with its presence test (``churn``): a
    published ``0`` for round 1 would say a fix pass spent nothing when what happened
    is that there was no fix pass, and "spent nothing" is the flattering direction on
    exactly the claim this block exists to make."""
    referee = referee or {}
    measured = _nonneg_int(referee.get("churn"))
    production = int(referee.get("production") or 0) if measured else None
    unrefereed = int(referee.get("unrefereed") or 0) if measured else None
    spend = (None if production is None or unrefereed is None
             else production + weight * unrefereed)
    # `limit` is published as null wherever nothing is being paid for out of it, which
    # is a repo with no budget AND a repo whose bands meet (`Dials.budgeted_band`). A
    # reader must not be able to tell those apart from the number alone — both mean
    # "there is no budget in force on this round" — and `band` is beside it for the
    # one who needs to know which.
    in_force = limit if band else None
    return {"limit": in_force, "weight": int(weight), "band": bool(band),
            "production": production, "unrefereed": unrefereed, "spend": spend,
            "within": (None if in_force is None or spend is None
                       else spend <= in_force)}


def fix_surface_state(surface: object) -> dict | None:
    """#619's measurement as `round_stop` publishes it, or ``None`` where there was
    none to make.

    ``surface`` is `panel.py`'s reading of the fix range: the files the last fix pass
    touched, and the subset of them that no earlier round's diff contained. The
    quantity is SURFACE and not size, which is the point of it — every other number
    downstream of a fix pass counts lines or findings (`max_fix_growth`,
    `max_fix_growth_chars`, `fix_injection`), and a fix that adds fifteen lines to two
    nginx templates nobody had reviewed is invisible to all three. On lexray#1780 it
    was the P1: round 3's pass touched twelve files, seven of them never in front of a
    reviewer, and both of the cycle's later P1s were in that new surface.

    **REPORTED AND NOT GATED, on #67's rule.** Nothing in :func:`round_stop` reads this
    to move ``stop``, and the decision to gate it — `max_fix_new_files`, or report-only
    — has not been taken. #619 asks for the instrument first in as many words, and the
    comment beside #67's other withheld tallies in `panel.py` argues the same thing for
    them.

    **``None`` IS THE ANSWER "NOT MEASURED", AND IT MUST NEVER BECOME A ZERO.** Round 1
    has no fix pass to read and a rewritten branch has no readable range — the same
    conditions under which `unrefereed_fix` has nothing to say — and a payload that
    published ``count: 0`` for those would be claiming a pass opened no new files when
    what happened is that nobody looked. So a mapping carrying neither ``count`` nor
    ``new_files`` is treated as an absent measurement rather than as an empty one; a
    mapping carrying either is normalised and published in full, ``count`` from the
    caller where it gave one and from ``new_files`` where it did not."""
    if not isinstance(surface, dict):
        return None
    count, raw_new = _nonneg_int(surface.get("count")), surface.get("new_files")
    if count is None and raw_new is None:
        return None
    new_files = [str(f) for f in (raw_new or ())]
    return {"files": [str(f) for f in (surface.get("files") or ())],
            "new_files": new_files,
            "count": len(new_files) if count is None else count,
            "prior_files": _nonneg_int(surface.get("prior_files"))}


# --------------------------------------------------------------------- #506: and the
# fix pass that did it is STILL ON THE BRANCH.
#
# `escalate_on.fix_injection` (#489) ends the cycle when more than half a round's new
# outstanding findings were attributed to the pass immediately before it. Ending it is
# right and it is half an answer: the PR then ships carrying a change the panel has
# just finished saying generated more work than the pull request did, minus the round
# that would have found the rest of it. Stopping means the loop no longer makes it
# worse; it does not make it better.
#
# **Why this is sayable now and was not before.** A stop says "we ran out of
# confidence". A revert says "we know WHICH change made it worse", which is a much
# stronger claim and needs attribution to make — and `panel_scope._provenance` is that
# attribution, calibrated by #489. The instrument came first (#67's rule), the gate
# came in #489, and this is the first step that can act on which change was at fault
# rather than on how the round ended.
#
# **A PROPOSAL AND NOT AN ACTION, and that is the load-bearing constraint.** Reverting
# a fix pass also reverts the real fixes in it: a pass that cleared three P2s and
# introduced eight P3s is a net loss to revert wholesale, and nothing here knows which
# is which without asking. So what this builds is the two columns of the decision — what
# a revert would REMOVE and what it would COST — and hands them to a human with the
# commit range already named. Nothing in this file reverts anything.

#: The one kind of revert proposal that is not a reading of the fix range: there was no
#: fix pass between two rounds to propose undoing (round 1, or a cycle with no earlier
#: round). Every other kind IS :func:`panel_scope._fix_range_diff`'s own verdict, reused
#: rather than restated — `ok`, `no-fix`, `blind` — because #500 already settled the
#: vocabulary for "we cannot see this" and a second one would be two answers to one
#: question. `blind` is the rebase case and the whole reason this constant is not a
#: substring match on a sentence.
REVERT_NOT_ASKED = "not-asked"


def fix_pass_outcome(fixed_findings: Iterable[tuple], outstanding: Iterable[Canonical]
                     ) -> tuple[list[dict], list[dict]]:
    """What the fix pass under attribution ACHIEVED, as `(cleared, still_open)`.

    ``fixed_findings`` is :attr:`Baseline.fixed_findings` — the complaints the ANCHOR
    round (the one at the near end of the fix range) sent its fixer to answer, as
    `(key, severity, file, line, title)`. ``outstanding`` is what this round still has
    to clear. A complaint this round no longer carries is one a revert would put back;
    one it still carries is work the pass did not do, and reverting costs nothing there.

    **Keys, and nothing else.** :meth:`Baseline.raised_before` has a reworded-title
    fallback and this deliberately does not reuse it, because the two want opposite
    biases. There, a wrong "already raised" deletes a finding from a fixer's brief, so
    the fallback is worth its complexity. Here the same match would move a finding out
    of `cleared` and SHRINK the cost of the revert this function exists to price —
    which is the one direction a proposal must never fail in. On keys alone a defect
    the panel re-worded reads as cleared, the cost is overstated, and the argument
    against reverting is the one that gets the benefit of the doubt.

    That bias is deliberate and it is the pair of the one on the other column:
    ``removes`` is counted from `introduced`, which `_provenance` documents as a FLOOR
    rather than a measurement, so the benefit is understated by the same design. Cost
    high, benefit low — a revert this still argues for is one the numbers cannot have
    talked anybody into.

    **What `cleared` does NOT mean is "verified fixed".** It means this round did not
    raise it again, and under the default `increment` scope this round re-read only the
    fix commit — so a complaint in a file it never looked at again is in here too. That
    is the same limit :func:`round_stop` records for its own rule 3, it pushes in the
    safe direction (a longer cost list), and :func:`revert_state` carries the round's
    scope beside these lists so the sentence a human reads says which it was."""
    open_keys = {c.key for c in outstanding}
    cleared: list[dict] = []
    still_open: list[dict] = []
    for key, severity, file, line, title in fixed_findings:
        rec = {"key": key, "severity": severity, "file": file, "line": line,
               "title": title}
        (still_open if key in open_keys else cleared).append(rec)
    return cleared, still_open


def _no_command_why(shape: dict) -> str:
    """Why a named range is NOT handed a `git revert`. Three sentences rather than one
    absent field, because they are three different things to do next."""
    merges = shape.get("merges")
    if not isinstance(merges, int) or isinstance(merges, bool):
        return ("the commits in this range could not be listed, so nothing here can "
                "say the range holds only the fix pass's own work")
    if merges:
        return (f"the range holds {merges} merge commit(s) — `git revert` refuses a "
                "merge without `-m`, and a merge is how the base branch got into this "
                "range, so reverting it wholesale would undo commits no fix pass wrote")
    # Zero merges over a range that came back SHORT. GitHub's compare stops at 250
    # commits, so the count is a floor and a merge past the ceiling is invisible.
    return (f"the range is {shape.get('total')} commit(s) and GitHub's compare returned "
            f"only {len(shape.get('commits') or [])} of them, so the merge count is a "
            "floor rather than a measurement — this range is not KNOWN to hold only "
            "the fix pass's own work")


def revert_state(kind: str, *, why: str | None = None, base_sha: str | None = None,
                 head_sha: str | None = None, head_round: int | None = None,
                 round_no: int | None = None, scope: str = "",
                 removes: Iterable[dict] = (), costs: Iterable[dict] = (),
                 still_open: Iterable[dict] = (), shape: dict | None = None) -> dict:
    """#506's proposal as this round can make it, for :func:`round_stop` and the
    payload — the same division of labour :func:`injection_state` has, and for its
    reason: the arithmetic lives beside the thing it measures, and the stop rule stays
    a rule about findings.

    ``kind`` is :func:`panel_scope._fix_range_diff`'s own verdict for this round's fix
    range, or :data:`REVERT_NOT_ASKED` where there was no earlier round to have a range
    with. Only :data:`panel_scope.FIX_RANGE_OK` can name a commit range, and that is
    the whole of #500's constraint arriving here: on a rebased PR the range is
    ``blind``, the offending pass cannot be named, and this says so in #500's words
    rather than guessing at a range or returning nothing at all. ``why`` is the
    sentence `_fix_range_diff` wrote for the reader; the gate is on ``kind``.

    Every field is present on every round, :func:`injection_state`'s rule and for its
    reason: an absent key and "there was nothing to propose" are different claims, and
    a consumer forced to tell them apart would be reading a payload's age rather than a
    cycle's state.

    ``costs`` is carried even where the range is unreadable, because it does not come
    from the range — it comes from the anchor round's own brief — and "here is what the
    pass this cannot name was sent to do" is worth more to an operator than a blank.
    ``removes`` is not, and cannot be: it is the `introduced` bucket, and a blind round
    has none.

    ``shape`` is :func:`panel_scope.fix_pass_commits` — the commits inside the range —
    and it is what decides whether a COMMAND is offered at all, which is a distinction
    the range on its own cannot make (found by Codex).

    - **A merge commit inside the range makes a wholesale revert wrong twice over.**
      `git revert A..B` refuses a merge without `-m`, so the invocation cannot run as
      written; and a merge is how the base branch gets INTO the range in the first
      place, which is the lean `_fix_range_diff`'s docstring already records for
      attribution — there it over-counts `introduced`, here it would propose undoing
      other people's commits. So the command is withheld unless `merges` is zero.
    - **An unreadable shape withholds it too.** `{}` means the commits could not be
      listed, and "we did not check" must not render as "we checked and it is clean".
      The RANGE is still named — that is #506's requirement and it costs nothing to
      be wrong about — and only the paste-and-run half is held back.
    - **The SHAs in the command are the full ones**, never the eight-character form the
      `range` label uses (also Codex). A display span is read; a command is executed,
      and an abbreviation ambiguous in this repository resolves to nothing or to
      something else.

    ``round_no`` is this round, against ``head_round``'s anchor, and the difference is
    ``spans`` — **how many fix phases the range actually covers** (also Codex).
    :attr:`Baseline.head_sha` is the latest earlier round that SUPPLIED one, not the
    latest that ran: a round 2 whose payload records no commit leaves round 3
    anchored on round 1, and the range is then two fix passes rather than the one
    "the fix pass that did it" describes.

    It is REPORTED rather than refused, unlike the merge above, and the difference is
    which claim goes wrong. A merge makes the offered command wrong — it would undo
    commits no fix pass wrote. A wide span does not: the range is still exactly the
    one provenance attributed over, so the rate accused every commit in it and so does
    this. What it makes wrong is the WORD "pass", singular, and the answer to that is
    to say how many."""
    ranged = kind == FIX_RANGE_OK and bool(base_sha) and bool(head_sha)
    span = f"{base_sha[:8]}..{head_sha[:8]}" if ranged else None
    shape = shape if isinstance(shape, dict) else {}
    merges = shape.get("merges")
    # A zero merge count over a range the compare endpoint TRUNCATED says nothing: a
    # merge past its 250-commit ceiling is invisible, so `merges` is a floor there, and
    # `complete` is what tells the two zeroes apart (Codex, second pass). Both are
    # required, and a shape missing either withholds the command.
    clean = (ranged and isinstance(merges, int) and not isinstance(merges, bool)
             and merges == 0 and shape.get("complete") is True)
    return {
        "kind": kind,
        "why": why or None,
        "base": base_sha or None,
        "head": head_sha or None,
        # How many fix phases the range covers: 1 in the ordinary case, more where an
        # intervening round recorded no commit to anchor on. None where either end is
        # unknown, which is not the same as 1 — see the docstring.
        "spans": (round_no - head_round
                  if isinstance(round_no, int) and isinstance(head_round, int)
                  and not isinstance(round_no, bool) and not isinstance(head_round, bool)
                  and round_no > head_round else None),
        # Which round sits at the near end of the range — the one whose complaints
        # `costs` is drawn from. It travels with the SHAs for `Baseline.head_round`'s
        # own reason: the pair is quoted at a human, and "the pass after round 1" is
        # the half of it they can check.
        "round": head_round,
        "range": span,
        # The action, spelled out, because the point of naming a range is that
        # somebody can act on it without deriving the command from two SHAs. Not run
        # by anything here — and NOT offered at all unless the range is known to hold
        # only the fix pass's own commits (see `shape` above). FULL SHAs, because this
        # one is meant to be executed.
        "command": (f"git revert --no-commit {base_sha}..{head_sha}" if clean else None),
        # Why there is no command, when there is a range but no command. Its own field
        # rather than a `None` a reader has to interpret: "this range holds a merge"
        # and "nobody could list its commits" are different things to do next.
        "no_command": None if clean or not ranged else _no_command_why(shape),
        # The pass itself, named commit by commit — #506 asks for the RANGE and this is
        # the range's contents, which is what a human weighing a revert actually reads.
        # Capped by `fix_pass_commits`; `commit_count` is the untruncated total.
        "commits": list(shape.get("commits") or []),
        "commit_count": shape.get("total"),
        "merges": merges,
        # What this round REVIEWED, which decides how `costs` should be read — see
        # `fix_pass_outcome`. Recorded rather than described, so the sentence and the
        # payload cannot drift.
        "scope": scope or "",
        "removes": list(removes),
        "costs": list(costs),
        "still_open": list(still_open),
    }


def _by_severity(records: Iterable[dict]) -> str:
    """`2×P2, 1×P3` — a severity census of a finding list, worst first, for the one
    line a human reads. Empty string for an empty list, so a caller can drop it into a
    sentence without a branch."""
    counts = Counter(str(r.get("severity") or "?") for r in records)
    # `SEVERITIES` order, with anything it does not name sorted after it rather than
    # dropped: a payload written by another harness, or a Sonar issue whose severity
    # did not map, still has to appear in a census a human is weighing a revert on.
    ranked = sorted(counts, key=lambda s: (SEVERITIES.index(s) if s in SEVERITIES
                                           else len(SEVERITIES), s))
    return ", ".join(f"{counts[s]}\u00d7{s}" for s in ranked)


def premise_report(verdict: dict, register_path: str, notes: list[str],
                   problems: list[str], board: str = "") -> str:
    """The one screen a fixer sees when it declares a premise. Plain text, because
    the reader is an agent about to decide whether to write a patch and the decision
    has to survive being read out of a Bash tool's stdout.

    ``board`` is what :func:`announce_escalation` did with it, appended verbatim
    when there is anything to say. It is a phrase and not a verdict: the
    escalation stands whether or not the board took the row."""
    out = [f"premise  {verdict['text']}",
           f"key      {verdict['key']}",
           f"cycle    {register_path}"]
    if verdict["restates"]:
        out.append(f"restates {verdict['restates']!r} — matched as the same premise "
                   "reworded, not as a new one")
    rounds = ", ".join(str(r) for r in verdict["rounds"])
    out.append(f"declared occurrence {verdict['occurrence']}"
               + (f" of {verdict['limit']}" if verdict["limit"] else " (brake off)")
               + f" — after round(s) {rounds}")
    # Said on every declaration, not only the braking one. A fixer that has never
    # seen the question does not know it was asked, and "decidable unknown — not
    # answered" is what teaches it the flag exists at the one moment it is deciding
    # patch-or-escalate. A line that only appears when it stops you is a line nobody
    # reads until it is too late to have answered.
    answer = verdict.get("decidable", "unknown")
    if answer == "unknown":
        out.append("decidable  NOT ANSWERED — pass --premise-decidable yes|no: can "
                   "the runtime this assertion runs in observe the property the fix "
                   "asserts? An unanswered declaration cannot brake on #491")
    else:
        out.append(f"decidable  {answer} — the runtime this assertion runs in "
                   + ("can" if answer == "yes" else "CANNOT")
                   + " observe the property the fix asserts")
    # #560, on the line above's rule: said on every declaration, because a fixer that
    # never sees the stamp does not know the ordering is being recorded, and an
    # unreadable checkout has to be visible at the moment it happens rather than three
    # rounds later in somebody else's payload.
    if verdict.get("head"):
        out.append(f"at       {verdict['head'][:12]} — the tree this was declared "
                   "from. A later round reads it to tell a premise declared before "
                   "its own fix pass from one declared after it (#560)")
    else:
        out.append("at       NOT RECORDED — this tree's HEAD could not be read, so no "
                   "round can tell whether this premise preceded its own fix pass or "
                   "followed it (#560)")
    if verdict["undeclared_rounds"]:
        # #84: an undeclared fix pass is UNESCALATABLE, and saying so is the point.
        # A cycle nobody could have braked reads exactly like one that did not need
        # braking, and only this line tells them apart.
        undeclared = ", ".join(str(r) for r in verdict["undeclared_rounds"])
        out.append(f"UNESCALATABLE: the fix pass after round(s) {undeclared} declared "
                   "no premise, so the brake could not have been evaluated on it. "
                   "That is a gap in this count, not a clean record")
    for line in (*problems, *notes):
        out.append(f"note     {line}")
    out.append("")
    if verdict["escalate"]:
        keys = " ".join(f"--escalated {k}" for k in verdict["findings"])
        # Which brake fired changes what the fixer is being told NOT to do, so the
        # instruction is written from the one that fired rather than assuming the
        # repeat. #84's sentence tells a fixer not to patch the same premise again;
        # to a fixer stopped on its FIRST declaration that sentence is simply untrue
        # about its own cycle, and a stop whose explanation does not match what
        # happened is one a caller argues with.
        if verdict.get("undecidable"):
            why = ("The property is not decidable where the assertion runs (#491), so "
                   "the fix you are about to write is an approximation of it and the "
                   "next round's findings are the gap between the two. A better "
                   "approximation is still an approximation — that is the loop this "
                   "brake exists to refuse.")
        else:
            why = (f"This is fix pass {verdict['occurrence']} against one premise the "
                   "previous round invalidated (#84).")
        out += [
            "STOP — DO NOT WRITE THIS FIX.",
            verdict["reason"],
            "",
            f"{why} Escalate it instead (review-pr.md step 3a): write no "
            "patch for the findings it explains, fix everything else in the pass, and "
            "report the premise, what it explains and what removing it would cost.",
        ]
        # THE PARTITION, named as two halves (#555). The brief has told a fixer to
        # "fix everything else" since 2026-08-18 and lexray#1697 still spent a whole
        # pass on findings the premise explains — four of the five it fixed — so the
        # sentence is evidently not the mechanism. This is: the halves are listed,
        # the downstream one by key, and the fixer is told which list its next
        # command belongs to. What it cannot do is compute the independent half —
        # `declare` never sees the round's findings, only the keys this declaration
        # named — so that half is described rather than enumerated, which is the
        # honest shape and not an omission to be filled in with a guess.
        if verdict["findings"]:
            rounds = ", ".join(str(r) for r in verdict["rounds"])
            out += ["", "DOWNSTREAM OF THE PREMISE — write no patch for these. Every "
                        f"key declared against it, across round(s) {rounds}:"]
            out += [f"    {k}" for k in verdict["findings"]]
            out += ["", "INDEPENDENT — every outstanding finding of THIS round that is "
                        "not listed above. Fix those, exactly as step 3 says: an "
                        "escalation partitions the pass, it does not end it.",
                    "", "The list above is cumulative, so it can name a key from an "
                        "earlier round that is already fixed or no longer outstanding. "
                        "That costs nothing — it forbids a patch nobody was going to "
                        "write. What it must NOT be read as is this round's finding "
                        "list: subtract it from your own, and anything left over is "
                        "the independent half.",
                    "", "The next round must not count the downstream half as work a "
                        "fix pass can clear:", f"    panel.py ... {keys}"]
        else:
            out += ["", "NO PARTITION WAS DECLARED. --premise-for named no findings, so "
                        "nothing here says which of this round's findings are "
                        "downstream of the premise and which are independent — and "
                        "the difference is what you are about to act on. Declare it "
                        "(--premise-for <key> ...) or map the finding IDs yourself "
                        "(panel-review-pr.md §4b) before you patch anything or run "
                        "the next round."]
    else:
        out.append(verdict["reason"])
    if board:
        out += ["", f"board    {board}"]
    return "\n".join(out)


def _door_takes_condition(door: object) -> bool:
    """Does this box's ``needs_human`` know about #576's ``condition``?

    Asked rather than assumed, and copied in shape from `qb-doctor`'s guard of the
    same name because the hazard is identical. The harness on PATH goes stale —
    that is what the `harness` row is FOR — while this file may be a fresh
    checkout, so a signature mismatch between the two is the live case and not a
    hypothesis. An unexpected keyword is a ``TypeError``, the caller's guard would
    turn that into "NOT announced", and the escalation would be lost for the sake
    of a field that only makes the row tidier. A stale door gets the call it
    understands and yesterday's collapsing behaviour, which is strictly what it
    would have done anyway.

    A ``**kwargs`` door counts as taking it: that is what accepting a keyword means.
    """
    try:
        params = inspect.signature(door.announce).parameters.values()
    except (TypeError, ValueError, AttributeError):
        return False
    return any(p.name == "condition" or p.kind is inspect.Parameter.VAR_KEYWORD
               for p in params)


def announce_escalation(verdict: dict, gh_repo: str, pr_number: int | None) -> str:
    """An escalated premise becomes a question a human owes an answer to (#555).

    ``gh_repo`` is the ``owner/name`` SLUG (``cfg["github"]``), never the repo's
    local ``name``. They are different strings — ``cfg["name"]`` falls back to the
    checkout's directory name — and only the slug is what a plan item stores in
    ``repo``, so scoping the row by the other one would file every question under a
    name no item carries and quietly undo the half of #555 that makes the plan
    partition. `load_premises` reads the same field, two lines above the call.

    **The escalation was already the best-defined blocker this fleet produces, and
    it was the only one that went nowhere.** It names a premise in one sentence,
    the findings it explains, the rounds it survived and what the brake was set at
    — everything `plan_block`'s own docstring asks a caller for — and #328's table
    measured **0 rows** two months after it was built, because the four things
    that form this judgement all wrote it into prose. `qb-doctor` reaches
    :func:`needs_human.announce` for a stalled queue; the panel, which produces
    the sharper question, reached it for nothing.

    So this is the join, and it is deliberately the SAME door (#274) rather than a
    second one: `announce` posts the `stuck`, writes the `blockers` row, dedupes
    on `key` and collapses re-raises on `condition`. Nothing new is invented here
    except the mapping from a verdict to that call.

    **`decision`, and not `other`.** A premise asks *which of these, or whether at
    all* about the shape of a change — #279's own gloss on `decision`, and the
    class the human surface is built to show. `other` is for a judgement none of
    the six names, and this one is named.

    **`condition` is the premise key, not the sentence.** #576 is exactly this
    hazard: without it, two different premises escalated on one pull request
    collapse into one row and the second is answered "already an open blocker".
    :func:`premise_key` is stable across a rewording — that is what it is for, and
    :func:`find_premise` maps restatements onto the same entry — so a premise
    restated in round 3 re-raises the row it opened in round 2 instead of opening
    a second one.

    **That holds only where the door takes the field**, and the qualifier is not a
    quibble: against a `needs_human` predating #576 the call goes without it (see
    :func:`_door_takes_condition`) and two premises on one pull request DO collapse
    into one row, exactly as they did before #576. That is the trade `qb-doctor`
    and `qb-bump` already make — a stale harness loses the field, never the
    escalation — but it means "two premises are two rows" is a statement about a
    current board and a current harness, not an invariant of this function.

    **Best-effort, and it never raises.** `declare`'s contract is that it is cheap
    enough to run before every fix pass, and a fix pass must not fail because a
    board is down. A failure comes back as a phrase for the report rather than an
    exception, on `announce`'s own rule: an escalation that cannot be stored is
    still an escalation. Only the escalating path reaches here at all, so the
    ordinary declaration still costs no network.

    Returns the phrase to print, or "" when there is nothing to say.
    """
    if not gh_repo:
        # Without a repo the row has no scope, `_subject_from` has no ref to read
        # and the question would arrive as "something, somewhere" — which is the
        # noise `needs_human` refuses to store rather than the escalation it is.
        return "needs-human NOT announced: no repo, so nothing scopes the question"
    try:
        import needs_human  # noqa: PLC0415 — resolved on the escalating path only
    except Exception as exc:  # noqa: BLE001 — an escalation must survive its courier
        return f"needs-human NOT announced: {type(exc).__name__} importing needs_human"
    refs: list[dict] = [{"kind": "repo", "value": gh_repo}]
    if pr_number:
        refs.append({"kind": "pr", "value": str(pr_number), "repo": gh_repo})
    where = f"#{pr_number}" if pr_number else gh_repo
    detail = "\n".join([
        f"premise: {verdict['text']}",
        f"key:     {verdict['key']}",
        f"rounds:  {', '.join(str(r) for r in verdict['rounds'])}"
        f" (occurrence {verdict['occurrence']}"
        + (f" of {verdict['limit']}" if verdict["limit"] is not None else "")
        + ")",
        f"decidable where the assertion runs: {verdict['decidable']}",
        "",
        verdict["reason"],
        "",
        ("findings the premise explains, which no fix pass may clear:\n"
         + "\n".join(f"    {k}" for k in verdict["findings"])
         if verdict["findings"] else
         "no --premise-for keys were declared, so which findings this explains is "
         "not recorded — that partition is the fixer's and it did not state one"),
        "",
        "What is being asked: whether the premise holds. If it does not, the "
        "findings above describe something that should not exist, and the fix pass "
        "they would have bought is spend against an open question. Nothing here "
        "decides that — no patch was written for them and none will be until this "
        "is answered.",
    ])
    try:
        return needs_human.announce(
            cls="decision",
            reason=verdict["reason"],
            summary=f"premise escalated on {where}: {verdict['text']}",
            repo=gh_repo,
            detail=detail,
            refs=refs,
            # The KEY is per-occurrence and the CONDITION is per-premise, which is
            # the distinction #576 draws: a post is news (this premise escalated
            # again, in a later round, explaining more findings) and a row is a
            # standing state (this premise is unanswered). Without the round and
            # the findings in the key, a premise that escalates again three rounds
            # later says nothing for twelve hours.
            key=needs_human.digest("panel-premise", gh_repo, pr_number or "",
                                   verdict["key"], verdict["occurrence"],
                                   *verdict["findings"]),
            # A stale door loses the FIELD and never the escalation — `qb-doctor`
            # and `qb-bump` both ask this before passing it, for the reason
            # `_door_takes_condition` states.
            **({"condition": f"premise:{verdict['key']}"}
               if _door_takes_condition(needs_human) else {}))
    except Exception as exc:  # noqa: BLE001 — the courier, not the news
        return (f"needs-human NOT announced: {type(exc).__name__} from "
                "needs_human.announce")


def declare(repo_name: str | None, premise: str, register_path: str,
            round_no: int, findings: list[str] | None = None,
            pr_number: int | None = None, json_out: bool = False,
            decidable: str = "unknown") -> int:
    """`panel.py --premise` — #84's futility brake, evaluated where a fix is PROPOSED.

    No seats, no diff, no judge and no vendor call: it reads the repo's dial, reads
    the cycle's register, counts the occurrences of this premise and either records
    it and returns 0, or refuses the fix and returns :data:`PREMISE_REPEATED_EXIT`.

    **One board write, and only on the refusal** (#555): a refusal IS the
    escalation, and :func:`announce_escalation` is what stops it being a sentence
    in a PR comment that nobody counts. The ordinary declaration — the one that
    runs before every fix pass and returns 0 — still touches no network, which is
    what keeps the paragraph below true.

    Cheap on purpose. `--ask` (#79) is the other half of the same argument and costs
    a vendor call per seat, so it is best-effort and skippable; this one must run
    before EVERY fix pass or the count is not a count, and a check that cost money
    per fix pass would be the first thing dropped.

    **No `review_refusal`**, unlike the review and ask paths on either side of it,
    and the difference is what each one spends. Both of those convene SEATS — a
    panel nobody chose, on models nobody chose, striking a tally whose whole standing
    is that somebody configured it — so a repo with no rules file is refused. This
    convenes nobody. The only setting it reads is one integer with a documented
    default at the safe end, and refusing would take the brake off exactly the repos
    that have configured the least while breaking the loop that calls it.

    The occurrence is recorded even when the brake fires, and the exit code is what
    carries the refusal. A caller that ignores it has written the fix anyway, and the
    register is then the record that says so — which `round_stop` reads on the round
    that follows, ending the cycle late rather than not at all."""
    cfg = load_repo_cfg(repo_name)
    repo_name = cfg.get("name") or repo_name
    notes: list[str] = []
    limit = premise_repeat_limit(cfg["review_panel"], notes)
    undecidable_brake = premise_undecidable_brake(cfg["review_panel"], notes)
    reg, problems = load_premises(register_path, cfg.get("github") or "", pr_number)
    # #560's stamp, read here rather than taken from the caller. The declaration is
    # made from the tree the patch is about to be written in, so that tree's HEAD is
    # the fact — see :func:`working_head` for why it is not a flag, and
    # :func:`retroactive_declarations` for the one ordering failure it settles and the
    # one it does not.
    verdict = declare_premise(reg, premise, round_no, findings or [], limit,
                              decidable, undecidable_brake, working_head())
    if verdict["decidable"] == "no" and not undecidable_brake:
        # The repo switched it off, and the declaration still says the fix cannot be
        # verified where it runs. Recorded and reported rather than swallowed, on
        # `ESCALATE_ON_UNBUILT`'s rule: a governance answer that changes nothing must
        # not be indistinguishable from one that was never given.
        notes.append("this declaration answered `decidable: no`, and "
                     "`escalate_on.premise_undecidable` is off — the fix is not "
                     "refused, and the answer is in the register for the round to "
                     "report")
    # Announced BEFORE the register write is checked, and that ordering is the
    # deliberate half: an unwritten register loses the occurrence, so the next
    # declaration counts as the first and the brake does not fire again. That is
    # precisely the run where the question most needs to be somewhere durable, and
    # the board row is the only durable thing left.
    #
    # THE COST OF THAT CHOICE, said out loud rather than left to be discovered: on
    # a failed write the two states disagree. The board holds an open question that
    # parks the work, while the register has no occurrence — so a later declaration
    # counts as the first, the brake does not fire, and a fix pass is written
    # against a premise a human has not answered. The alternative disagreement is
    # strictly worse: the question would exist nowhere at all, and the exit code
    # already tells the caller the declaration was not recorded. Resolving it is a
    # person answering or withdrawing the blocker, which is what the row is for.
    board = (announce_escalation(verdict, cfg.get("github") or "", pr_number)
             if verdict["escalate"] else "")
    write_failed = write_payload(register_path, reg)
    if json_out:
        print(json.dumps({**verdict, "register": register_path, "notes": notes,
                          "problems": problems, "write_failed": write_failed,
                          "board": board}, indent=2))
    else:
        print(premise_report(verdict, register_path, notes, problems, board))
    if write_failed:
        # The same rule `finish` applies to a round's payload, for a sharper reason:
        # an unwritten register loses the occurrence entirely, so the NEXT
        # declaration of this premise counts as the first and the brake never fires.
        # A caller told the declaration was recorded when it was not is worse off
        # than one told nothing.
        print(f"\npanel: FAILED — the premise register was not written: "
              f"{write_failed}. This declaration was NOT recorded, so the next one "
              "counts as the first and the brake will not fire on it.",
              file=sys.stderr)
        return UNWRITTEN_PAYLOAD_EXIT
    return PREMISE_REPEATED_EXIT if verdict["escalate"] else 0


def round_stop(round_no: int, max_rounds: int, new_keys: list[str],
               outstanding: list[Canonical], veto: list[str],
               baseline_ok: bool = True, repeated: Iterable[str] = (),
               escalated: Iterable[str] = (), *,
               trigger_floor: str = NO_SEVERITY_FLOOR,
               cleared_floor: str = NO_SEVERITY_FLOOR,
               narrowed: Iterable[str] = (),
               declined: Iterable[str] = (),
               premises: dict | None = None,
               injection: dict | None = None,
               revert: dict | None = None,
               not_falling: dict | None = None,
               unrefereed: dict | None = None,
               guard_churn: dict | None = None,
               fix_budget: dict | None = None,
               surface: dict | None = None) -> dict:
    """Whether the panel/fix cycle should go again, and what decided it.

    ``outstanding`` is every finding the cycle still has to clear, which is wider
    than "confirmed" and deliberately so: it holds anything the judge did not
    dismiss (including the ``unjudged`` findings of a round whose judge crashed)
    plus Sonar's hard-gate issues, which nobody adjudicates at all. A P2 nobody
    ruled on is not a reason to STOP. The parameter used to be called
    ``confirmed``, and the word reached the PR comment: a reader reconciling
    "still confirmed after the fix" against a round with no judge was told
    something untrue about how the verdict was reached.

    The rule is mechanical on purpose. Asking reviewers to forecast "will another
    round be needed?" measures the wrong thing — a model that just wrote five
    findings is primed on problems and says yes, one that found nothing says no,
    and the vote only re-encodes a finding count already known. So the loop turns
    on what actually happened:

    1. findings this round that no earlier round raised, **at or above**
       ``trigger_floor`` -> go again;
    2. a P1/P2 still outstanding **at or above** ``cleared_floor``, or a Sonar hard-gate
       issue still outstanding at **any** severity -> go again, whatever
       anyone declared (a blocker
       raised again is a blocker that was not fixed) — **except one in**
       ``escalated``, which the filter below has already subtracted. Said here
       rather than left to the filter's own paragraph because it is the largest
       behavioural consequence of #221 and it reverses this rule's own sentence:
       a declaration now DOES override rule 2, and a cycle whose only remaining
       work is an escalated P1 stops with that blocker present. That is the point
       of the feature — no fix round may touch such a finding, so another round
       buys nothing — and the stop is never dressed up as convergence: it takes a
       veto line, ``confident`` is false, and ``reason`` says a human is owed an
       answer;
    3. ``repeated`` — the KEYS of findings an earlier round already raised that
       are STILL outstanding, **at or above** ``trigger_floor`` -> go again. The
       fixer was told about them and they are still there. This used to only cost
       the stop its confidence, which ended the cycle with a judge-confirmed defect
       present and nothing acting on the veto that said so. Keys rather than a count
       so the escalation filter below can subtract the escalated ones: a count
       computed before this function sees it puts the jam straight back, filtered by
       whichever caller remembered to, which is not a rule but a convention with
       one participant;
    4. otherwise dry -> stop.

    **RULE 3 TAKES THE TRIGGER FLOOR AND NOT THE CLEARED ONE, and that is the
    convergence fix (#621).** The rule it replaces bounded the repeat by
    ``cleared_floor`` — anything the fix pass was ASKED to clear and did not. That was
    sound while the two floors sat a tier apart, and it stops being sound the moment
    the fix floor drops far enough that a pass may spend its budget on P3/P4 work: a
    single unpaid P3 is then a finding the fixer was asked to clear, did not, and can
    only close by WRITING LINES — and writing lines is what authors the next round's
    findings, which is the loop this whole branch exists to break. The decided rule is
    that **a repeated finding keeps the cycle going only if it would have bought a
    round in the first place.** A finding under the trigger floor never bought one
    when it was new and does not buy one by being raised twice; it is reported — in
    ``repeated_below_trigger_floor``, in the stop's own ``reason``, and in
    ``outstanding.fixable`` where a fix pass could still take it — and the cycle stops.
    Reported is the load-bearing half of that sentence: such a round is NOT dry, and it
    says which findings it stood down on rather than borrowing the word, on exactly the
    terms #165's below-floor branch already set for new findings.

    What that does NOT change, said explicitly because each is the kind of thing a
    reader assumes went with it: a repeated **P1/P2 still goes again**, under rule 2
    and under rule 3 both — rule 2's bound is untouched, and a P1/P2 is at or above
    any trigger floor a repo can set. A **Sonar hard-gate issue** keeps exactly the
    standing it has today: ``exempt`` is a property of the KEY and not of one rule, so
    it passes both floors at every rule, and a still-open gate issue goes again
    however Sonar graded it. **Escalations are still subtracted before every rule**,
    and so are ``narrowed`` keys — the filters run once, in front of all four, and
    this changes neither.

    **``declined`` IS NOT A FILTER AND IS NOT A RULE (#665).** It is the register of
    corrections an earlier fix pass identified and could not make, and it is the one
    input to this function that changes no rule's answer. Nothing is subtracted for
    it, nothing is exempted by it, and no branch below can be reached BECAUSE of it
    that could not be reached without it. What it does is take a stop's claim to
    have converged: a cycle ending with declarations on the record has not run out
    of defects, it has run out of corrections anybody was willing to make, and those
    two ending in the same word is how a PR lands with known-unfixed defects and a
    clean verdict.

    So it costs a veto line and it costs ``converged``, and it costs them only on a
    STOP — on a ``go again`` the round is not claiming anything. The direction is
    the one this whole register is bound by: it can make a verdict less confident
    and never more, which is what makes it safe to be written by the actor it
    reports on.

    **Every declaration in the register counts, not only the ones this round raised
    again**, and that is the one place it parts company with ``escalated``'s
    ``blocking``. Bounding it to what the round raised was written first and is
    wrong here: under the default `increment` scope a later round never re-reads the
    file the declined correction was owed in, so the bound would silence the
    register in exactly the case that motivated it — the quiet round after the pass
    that declined. The cost of the wider rule is stated rather than hidden: nothing
    RETRACTS a declaration, so a correction genuinely made in a later pass goes on
    costing the cycle its confidence until the cycle ends. That is the conservative
    direction (a defect nobody recorded as fixed reads as unfixed), it never adds a
    round, and #617's ``--new-cycle`` is the clean start for a PR that has actually
    had the work done.

    The cap is what stops rule 3 running forever when two reviewers disagree
    about a P2 — the cycle ends either way, and a cap reached with work
    outstanding is recorded as such rather than as convergence.

    **``outstanding`` IS THE SECOND QUESTION, AND IT IS NOT THE ONE ``stop``
    ANSWERS (#42).** ``stop`` answers *should another PANEL run* — a question about
    cost, the cap and convergence. It was read as also answering *should these
    findings be FIXED*, which it is not computed from and which has a different
    answer: by ``/panel-review-pr``'s own bar that one is always yes, for every
    confirmed finding and every Sonar hard-gate issue. The cap is where the two come
    apart. ``panel-review-pr.md`` §5 launched a fixer only on ``stop: false``, so a
    capped round's findings — P1/P2s still outstanding, repeats whose fix did not
    land, gate issues, and everything the round newly found — were found, judged,
    posted to the PR, recorded on the board, and **handed to nobody**.

    So the payload states both. ``outstanding.fixable`` /``below_floor`` /
    ``escalated`` are the MEASUREMENT and are true of every round including a ``go
    again``; ``handed_to`` is the VERDICT and is null unless this round is ending the
    cycle — ``fix_injection``'s ``over``/``fired`` split, applied once more, and for
    its reason: a caller gating a final, unreviewed fix pass on that field must not
    have it answered by a round that is mid-cycle.

    **The honest end state is the point of the block rather than a caveat on it.** A
    cycle that ends with clearable work left ends with either unfixed findings or an
    unreviewed fix; there is no third option, and the workflow used to pick the first
    in silence. ``handed_to: "fixer"`` names the second as the default and ``why``
    says in as many words that the resulting commit ships unreviewed — **a proposal
    and not an action**, on #506's terms: nothing here runs a fixer and the choice
    belongs to the operator.

    **A futility rung's leftovers go to a human, not to a fixer**, and that is the
    distinction the cap does not have. ``premises``, ``injection``, ``not_falling``
    and ``unrefereed`` each end the cycle by saying, in their own ``reason``, that a
    human answers this rather than another fix pass — so handing their remainder to
    one would contradict a sentence the same payload is carrying. A cap says only
    that the cycle has spent enough, which is not a claim about what the next fix
    pass is worth. Below-floor findings are handed to nobody **deliberately** (#165's
    policy stop), and are listed rather than dropped, because silence about them is
    what lets such a stop read as a dry one.

    **THE TWO FLOORS (#165), and why there are two.** Both default to
    :data:`NO_SEVERITY_FLOOR`, so a caller that has not heard of them gets exactly
    the behaviour above; `panel.py` passes the repo's
    ``review_panel.round_trigger_floor`` and :attr:`panel_seats.Dials.cleared_floor`
    — which is the ``fix_severity_floor`` until a budget is in force and the cut
    afterwards, and is NOT ``Dials.fix_floor``. The parameter is called
    ``cleared_floor`` because that is the concept it carries and because the other
    name is live one module over holding a different value (#549); the payload key
    follows it.

    ``trigger_floor`` bounds rules 1 and 3: a new finding below it is still counted,
    still reported and still in the payload, it simply does not by itself buy a
    panel, a fix pass and another panel. That rule is the one the measurement
    indicts. From round 2 the thing under review IS the previous round's fix, so
    rule 1's input is the loop's own output — 128 of 201 new findings across seven
    PRs were created by the fix pass immediately before them — and a termination
    test fed by its own output can only end on the cap, which is what all seven
    panels did.

    ``cleared_floor`` bounds rule 2 alone, and it bounds the DISPOSAL below. A
    finding under it is one the fix round was never asked to clear, so it is
    outstanding every round by construction and no rule may read that as the fixer
    having failed. Rule 2 takes the bound for that reason, which matters only where
    the floor is ``P1``: at ``P2`` every P1/P2 is already at or above it and the
    filter is a no-op. Read the other way round, that is the honest scope of "``P4``
    restores the old behaviour" — true of rule 1, and vacuous for rule 2, whose bar
    is the hardcoded ``("P1", "P2")`` tuple, so a floor can only ever RAISE it and
    only ``P1`` moves it at all.

    **The two floors now bound different rules because they answer different
    questions, and rule 3 asks the first one.** ``trigger_floor`` answers *is this
    finding worth another panel, another fix pass and another panel?*;
    ``cleared_floor`` answers *was this finding this pass's work?*. Rule 3 is a
    question about whether to spend another round, so it takes the round dial —
    which also makes the two rules that can extend the cycle bounded by one number a
    repo sets once, instead of by whichever of two dials the reader guessed. Rule 2
    and the disposal are questions about work, so they keep the work dial. The one
    consequence worth naming: a finding above the cleared floor and below the trigger
    floor is a finding a fix pass IS asked to clear and whose non-clearance buys no
    round — reported, in ``outstanding.fixable``, and not a reason to go again.

    **NEITHER FLOOR APPLIES TO A SONAR HARD-GATE ISSUE**, at any rule. A finding with
    ``verdict == "sonar"`` in ``outstanding`` is a red quality gate, not a judged
    opinion about severity, and the floors are a policy about what is worth fixing
    and what is worth another round — a question a merge gate does not get to be
    asked. Sonar's own severities are routinely P3/P4, so filtering by them dropped a
    new gate issue into ``new_below_trigger_floor`` and stopped the cycle
    ``confident`` with a failing gate on the PR. See the comment on ``exempt``, and
    the one above ``outstanding = to_fix + sonar`` in `panel.py` that this restores.

    **A stop under either floor is reported as what it is, and is NOT vetoed.** The
    ``reason`` names the floor and counts what was left under it, so nothing reads as
    "dry" that was not dry. But it is a POLICY stop, not a cap: the repo said which
    findings are worth a round, the round obeyed, and the findings it did not act on
    are in the report and in the payload. Calling that unearned would make every
    configured convergence non-confident and hand the cap back its monopoly on
    ending the loop, which is the failure this whole change exists to remove. The
    growth ceiling (``max_fix_growth``, applied by the caller) is the opposite case
    and is vetoed, because there the round is stopping over something that WENT
    WRONG.

    ``escalated`` is not a fifth rule but a FILTER in front of all four (#221), and
    it is the exception none of them can express. A finding whose fixer reported
    that the APPROACH is wrong rather than the code (``review-pr.md`` step 3a) is
    outstanding, correctly, and may never be handed to another fixer, correctly —
    so under the four rules alone it returns ``stop: False`` every round until the
    cap, on a finding no round can close. The mechanism built to stop a loop
    circling a premise instead guaranteed it ran to the cap. So escalated keys are
    subtracted from ``new_keys``, from ``outstanding`` and from ``repeated`` before
    the rules are applied: what remains is the work a fix round can actually clear,
    and the cycle goes again exactly while there is some. The mixed case falls out
    — one escalation beside two real findings goes again for the two, and stops
    when they are gone rather than when the counter runs out.

    Only the escalated keys THIS round raised do that. The register is inherited
    and only grows, so a key that no longer names anything — a premise a human has
    since answered, a finding withdrawn, a caller's typo — must not go on costing
    every later round its confidence. A round that is genuinely dry is reported as
    dry even while the cycle's register is non-empty; the open question lives in
    the relay and its issue, which is where a human is looking for it.

    ``narrowed`` is #615, and it is the SECOND filter in front of the four rules. The
    fixer's vocabulary was ``fixed | refuted | deferred`` plus escalation, and all four
    answer *whether* to act; none of them answers *how far*. Rich's decision adds the
    fourth outcome: the finding is real, this pass fixed it **at the point it was
    raised**, and the general form is not this pass's work.

    **It CLEARS.** A narrowed key is subtracted alongside the escalated ones, before
    the rules, so it is not outstanding under rule 2, rule 3 does not count it, and it
    is in neither ``outstanding.fixable`` nor ``below_floor``. It takes NO veto line
    and costs the round no confidence — that is the whole difference from an
    escalation, which is an open question a human owes an answer to, where this is an
    answer already given.

    **But the stop SAYS SO, by name.** A round whose quiet was bought by a narrowing
    gets its own ``reason`` — never "dry", which is a claim that nothing was raised —
    and the reason counts how many of the cleared keys were at or above the trigger
    floor, because a P1 answered narrowly and a P4 answered narrowly are the same
    mechanism and not the same news. The keys are repeats by construction: the flag
    is passed on the round after the pass that declared it, and only the keys THIS
    round raised are honoured, so every one of them is a finding a fresh panel put up
    again after a fixer said it was answered.

    **And that is the whole of the charge.** Costing ``confident`` or ``converged`` at
    or above the trigger floor was considered on the review of #631 and declined: it
    would leave a fixer's only way to end a cycle cleanly the class-wide fix, which is
    the pressure this outcome exists to remove, and it would price an ANSWER as though
    it were an open question. The asymmetry with ``escalated`` — which costs a veto,
    ``confident`` and ``converged`` — is the point rather than an oversight: one names
    work nobody has done, the other names work that was done and bounded. What a
    narrowing costs is two lines of justification, a board row and, where the general
    form is itself a claim-miss, an issue; that bill is the caller's to collect, and
    the reason line is what tells a human there is one outstanding.

    **Why clearing is the point rather than a leniency.** A fixer that cannot answer a
    finding partially will answer it maximally, because the maximal answer is the only
    one that fully satisfies a brief which says "never note a problem and move on".
    The maximal answer is what makes a pass edit files the finding never named, and
    those files are where the next round's findings come from — one finding about one
    route became server-level nginx ``gzip``, and the round after that was a P1
    (lexray#1780, and ``surface`` below is the instrument for the same failure). Give
    the partial answer a name and let it CLEAR, and it becomes reachable; leave it
    counting as outstanding and rule 3 holds the cycle open until somebody writes the
    class-wide fix, which is the pressure this branch exists to remove wearing a
    kinder name.

    It is not free. A narrowing owes two lines — why the narrow fix is complete for the
    finding as raised, and what the general form would be — and a board row, and a
    GitHub issue **only where the general form is itself a claim-miss**. None of that
    is enforceable here: this function sees keys, and the cost is the caller's to
    collect (``panel-review-pr.md``). What this function does guarantee is the one
    thing a fixer could otherwise buy with the word: a **Sonar hard-gate issue cannot
    be narrowed away**. See the comment on ``answered``.

    The register carries the same two caveats the escalated one does, one paragraph
    up, and for the same reasons — the keys are a claim by the agent that wrote the
    fix, and a key is not a finding, so a fresh panel that words the defect
    differently mints a key this cannot match and the finding is simply outstanding
    again.

    **A stop that is HOLDING an escalation is never reported as convergence.** It
    takes a veto line, which costs ``confident`` by the existing rule, and it says
    so in ``reason``: the loop has finished, and the PR has a question on it that
    only a human closes. Reporting that as dry would be the "clean" this whole
    payload is organised against. Note the exact scope of the guarantee, which is
    the paragraph above read the other way round: it covers the round that RAISED
    the escalated finding again, not every later round of the cycle. A round under
    ``--scope increment`` that reviews only a fix commit, or a round whose fresh
    panel words the same premise differently enough to mint a new key, raises
    nothing the register matches and is reported dry and confident with the
    question still open. What tracks an open premise across a whole cycle is the
    relay and its issue, not this field.

    ``premises`` is #84's futility brake seen from the ROUND, and it is the second
    half of a mechanism whose first half runs somewhere else entirely. The brake
    proper is evaluated when a fix is PROPOSED (:func:`declare_premise`, through
    `panel.py --premise`), because #84's whole argument is that end-of-round is one
    fix pass and one panel too late. What arrives here is the register that check
    wrote, and it does two things with it:

    - **A premise declared as many times as the dial allows ENDS THE CYCLE**, at
      any of the four rules, and never as convergence: it takes a veto line,
      ``confident`` is false, and ``reason`` says a human answers the premise. This
      is the case where the brake was overruled or never consulted and the round
      ran anyway — late, and better than the cap. It is deliberately the same
      terminal state a held escalation gets, because it IS one: a repeated premise
      is #67's circling, and the answer to it was never another fix pass.
    - **A premise whose declaration answered ``decidable: no`` ENDS THE CYCLE** on
      the same terms (#491), and gated on ``undecidable_brake`` — the register lists
      such a declaration whether or not the repo armed the brake, because the payload
      records what the cycle SAID, and a repo that switched it off must not have the
      policy applied anyway. This is the late half of the same mechanism: the refusal
      belongs at ``declare_premise``, and what reaches here is the record that a
      caller wrote the fix regardless.

      It is a SEPARATE rule from the repeat above and not a special case of it,
      because the repeat is exactly what it cannot rely on. A fixer circling an
      unobservable property replaces the proxy each round and declares a genuinely
      different premise every time, so the occurrence counter stays at 1 while the
      cycle circles — four declarations on one cycle, no two matching. What is shared
      across those four is not their words but their answer to one question, which is
      why the question is asked of each declaration alone.

    - **A fix pass that declared no premise is reported as unescalatable**
      (``undeclared_rounds``), and costs the round nothing. #84 is explicit that an
      undeclared fix is unescalatable rather than inferred, and the reason it is
      said out loud is that a cycle nobody COULD have braked reads exactly like a
      cycle that did not need braking — silence would assert the second. An
      ``unknown`` decidability answer is the same rule one level down: a declaration
      that was never asked the question is reported as unanswered, never guessed at.

    Declarations never buy a round, only end one. A register is a claim by the
    agent that is about to write the fix, and the one thing #67's evidence says
    cannot be self-reported is whether the loop is making progress; letting it
    extend the loop would hand that agent the other lever too.

    ``injection`` is #489, and it is the third futility bound in this function
    after ``premises`` and the cap. The cap bounds COST; ``premises`` bounds one
    assumption being patched twice; this one bounds **rule 1 being fed by its own
    output**, which is the failure ``trigger_floor``'s paragraph above indicts by
    name and which nothing until now acted on. From round 2 what a round reviews IS
    the previous round's fix, so a finding that fix created buys another panel,
    another fix pass and another round of the same — and 128 of the 201 new findings
    across the seven PRs counted above were exactly that.

    **This is the gate `panel.py` deliberately withheld, and the withholding was
    right.** The comment beside #67's tallies in that file says nothing reads them to
    stop a run, that #67 asks for the instrument before the gate, and that "a few dozen
    cycles of it are what would justify wiring it to anything". This is not that
    decision being reversed as an oversight; it is the condition it named being met.
    128/201 across seven PRs, 39/53 after round 1 on PR #299 with its round 2 at 17
    of 17, and 64% then 87% on the cycle #489 was filed from — over a pull request
    whose actual change was 113 lines. #67's other two tallies stay withheld, and the
    comment beside them in `panel.py` now argues that on its own terms rather than by
    inheritance from this one. Provenance is the one of the three that can carry a
    threshold, because ``introduced`` is a floor and a floor has a known direction to
    err in.

    :func:`injection_state` is the measurement and it arrives already decided, on
    ``premises``' precedent, so this function applies a rule rather than computing a
    rate over a diff it has no business seeing.

    **It may only take away the round RULE 1 was buying.** The justification is
    about rule 1 and nothing else, so the rule is bounded to it: a round going again
    under rule 2 (a P1/P2 or a Sonar gate issue still outstanding) or rule 3 (a
    finding an earlier round already raised) is going again for work the fix pass
    FAILED to do, which is not the same claim as the fix pass generating work — and
    four below-floor findings, mostly introduced, must not be able to cancel the
    repair round for an unrelated P1. This is where it parts company with #84's
    brake, which fires at any of the four rules: a repeated premise is the fixer's
    own DECLARATION about the patch it is about to write, and this is a threshold on
    a statistic. A statistic may end the loop it is a statistic about; it may not
    overrule a named blocker.

    **It can only turn a `go again` into a STOP, and it is CHECKED on that
    condition rather than merely obeying it.** A round that is already stopping has
    no next round to prevent, and a dry round rewritten as "diverging" would be an
    accusation about a cycle that converged. So a dry stop, a stop under either
    floor and a stop holding an escalation all keep their own reason and their own
    confidence. What that costs is the case where a below-floor stop hides a high
    injection rate, and it is deliberate: #165's floor stops are POLICY stops that
    are explicitly not vetoed, and vetoing them through this door would make every
    configured convergence non-confident and hand the cap back its monopoly on
    ending the loop.

    **Never dressed up as convergence** — a veto line naming the dial, ``confident``
    false by the existing rule, and a ``reason`` that says a human is owed an answer:
    the same discipline ``max_fix_growth``, the round cap and a held escalation get.
    Applied BEFORE the cap for ``premises``' reason, and before ``circling`` so that
    a cycle doing both is reported as the premise repeat. That one NAMES the
    assumption a fixer wrote against; this one only counts, and the more specific
    truth wins the ``reason``.

    ``not_falling`` is #505, and it is the FOURTH futility bound here — beside
    ``injection`` rather than instead of it, and asking a different question. That one
    asks *did the fix cause this?*; this one asks *is the new-finding count still
    falling?*, which is the rule a human stated on #480 over a cycle of this
    codebase's own: 44 findings, then 15 new, then 18 new — stop, and triage the
    remainder. The 18 need not be attributable to the fix at all, and
    :func:`panel_scope._provenance` under-counts the ones that are, so a genuinely
    diverging cycle can sit under ``injection``'s threshold for its whole life and be
    stopped only by the cap.

    :func:`not_falling_state` is the measurement and it arrives already decided, on
    ``premises``' and ``injection``'s precedent.

    **It is computed from the ROUNDS' OWN COUNTS and never from provenance**, which is
    the property that makes it worth having rather than a tighter threshold on the
    dial above. #500 — a rebase between rounds silently disarms provenance — disarms
    ``injection`` outright and cannot touch this: a round's count of its own new
    findings survives the range under it being unreadable, so on a busy queue where
    most PRs are rebased mid-cycle this is the rung that still works.

    **It takes the same two bounds as ``injection``, and for the same reasons.** It
    may only turn a ``go again`` into a STOP, checked on that condition rather than
    merely obeying it; and it may only take away the round RULE 1 was buying, because
    its whole justification is about rule 1's input. A round going again under rule 2
    or rule 3 is going again for work the fix pass FAILED to do, and a count of news
    must not cancel the repair round for a named blocker. Both flags are therefore
    computed from the SAME pre-brake state as ``injection``'s, so a round that is over
    both thresholds records both rather than whichever was applied first.

    That second bound is now enforced as what it says rather than as "rule 1 won the
    ``reason``" — the two are different, because rules 1-3 are an if/elif chain and a
    round can be going again under all three at once. What disarms both rungs is a
    P1/P2 or Sonar gate issue **an earlier round raised** and this one still has, or a
    ``repeated`` key: work the fix pass failed to do. This round's OWN new P1s do not,
    and must not — they are the news being counted, and a rung that stood down for them
    could not fire on the cycle #489 was measured from, where every new finding was a
    P2. See the comment at ``held_over``.

    **It inherits ``injection``'s honest mismatch, and inherits it knowingly.** The
    series is each round's own ``new_findings`` — every finding no earlier round
    raised — while the rules above are applied to the CLEARABLE ones, so a cycle
    holding an escalation counts a finding no fix round may touch. It is allowed to
    differ for that rule's reason: the series is a property of what the ROUNDS
    PRODUCED, and the work bound is a property of what the next round could do about
    it. Closing it would mean a second per-round count beside the one every payload
    since the field existed already carries, and a second count is a second thing that
    can disagree with the first. What keeps it from mattering is ``triggering``: a
    round whose only news is escalated buys nothing under rule 1 and this rung cannot
    fire on it.

    **``injection`` owns the ``reason`` when both fire**, on the ordering
    ``circling`` already establishes: the more specific truth wins. A rate that names
    the fix pass as the author of this round's work says more than a count that only
    says the work is not shrinking, and both veto lines are on the record either way.

    **What it does not do.** #505 asks for two clauses — stop the cycle, and triage
    the remainder into an issue — and only the first is decided here. What became of
    the second is #42: the remainder is no longer handed to nobody, but it is handed
    to a HUMAN rather than into an issue, because this rung's own ``reason`` says a
    human triages what is left. ``outstanding.handed_to`` carries that and
    ``outstanding.fixable`` carries the remainder itself; filing it is the caller's
    step (``panel-review-pr.md`` §5). It still trades a round for a stop a human has
    to act on.

    **One honest mismatch, written down rather than fixed**, on the same terms
    :func:`panel_scope._provenance` records its own two: the rate's denominator is
    every new outstanding finding, while the rules above are applied to the
    CLEARABLE ones — escalated keys are subtracted from the work and are not
    subtracted from the tally. So a cycle holding an escalation can compute its rate
    partly over a finding no fix round may touch. Closing it needs the tally keyed by
    finding rather than by bucket, which is a second measurement beside
    ``provenance_counts`` and a second thing that can disagree with it; and the two
    numbers are answers to different questions, which is why they are allowed to
    differ. The rate is a property of what this ROUND produced — an escalated finding
    the last fix pass introduced is still a finding the last fix pass introduced —
    and the work bound is a property of what the NEXT round could do about it.

    **Why a threshold on this number errs in the safe direction.**
    :func:`panel_scope._provenance` records that its split is biased toward
    ``missed`` in BOTH directions — a defect a fix introduced by DELETING a guard has
    no added line to sit on, and ``introduced`` requires exact membership in the
    added lines while LLM reviewers and Sonar routinely report a line or two off —
    and it says the count "should be read as a floor rather than as a measurement".
    A floor that is over a threshold is genuinely over it. The unattributable
    buckets sit in the denominator for the same reason (see
    :func:`injection_state`): they depress the rate, so a round the harness could
    not place is a round that does not end the cycle.

    ``unrefereed`` is #554, and it is the FIFTH futility bound here. The other four
    ask how much a cycle has cost (the cap), whether one assumption is being patched
    twice (``premises``), whether the fix pass authored this round's findings
    (``injection``) and whether the new-finding count is still falling
    (``not_falling``). This one asks something none of them do: **whether anything
    can check what the last fix pass wrote.**

    The measurement is lexray#1697 round 1, since reverted. A 93-line fix pass across
    three files changed NO production logic at all — the production file's entire
    share of it was a docstring and a comment — and introduced ten findings, nine of
    them in the test files it wrote and the tenth in the docstring it corrected.
    Red/green ran and went red 4 of 4 and could not have caught any of them, because
    it asks whether a test detects the thing it was written for and never whether the
    test also opens a socket, whether its assertion is sufficient, or whether it is as
    strong as the test beside it.

    That is structural rather than unlucky: **a production fix has an external referee
    and a test fix has none, because nothing tests a test.** A docstring fix has none
    either. Every other dial the panel owns measures COST — lines, chars, multiples,
    rounds — and ``injection`` measures the real quantity but only retrospectively,
    one round late. Nothing priced work by whether anything could catch it being
    wrong.

    :func:`referee_state` is the measurement and it arrives already decided, on
    ``premises``', ``injection``'s and ``not_falling``'s precedent.

    **It is a PREDICATE and not a threshold, which is why it may gate at all.** #67's
    rule is that an instrument earns a threshold over a few dozen cycles or not at
    all, and it is why :func:`panel_seats.guard_ratio` ships report-only beside this
    one. There is no threshold here to earn: the rule fires when the pass contains
    zero refereed lines, which is a fact rather than a number somebody guessed. A
    fraction would need the number AND would be wrong on the commonest healthy shape
    there is — a 5-line production fix carrying a 40-line regression test is 89%
    unrefereed and is exactly the work the panel wants.

    **It takes the same two bounds as ``injection`` and ``not_falling``, and for the
    same reasons.** It may only turn a ``go again`` into a STOP, checked on that
    condition rather than merely obeying it; and it may only take away the round RULE
    1 was buying, so it cannot cancel the repair round for a P1 an earlier round
    raised. It reads the same ``going_again`` state they do, so a round over all three
    records all three rather than whichever was applied first.

    **What it buys over ``injection``**, since the two accuse the same pass: that rung
    needs four new findings AND a rate over the threshold AND a readable range, so a
    pass that wrote only tests and drew three findings sails past it. This one needs
    the range and :data:`panel_seats.UNREFEREED_MIN_CHURN` churned lines, and it fires
    on the SHAPE of the pass rather than on its consequences — which is why #554 calls
    it the ex-ante half of #489. It does share #500's blindness with that rung, and
    the sharing is worth saying because a reader could reasonably assume otherwise:
    both read the fix range, so a rewrite between rounds that #504 cannot rebuild
    disarms both — the caller passes whatever `fix_diff` it ended up with, so the
    reconstruction is inherited here for free and so is its failure. ``not_falling``
    remains the only rung computed from the rounds' own counts and therefore the only
    one a rewrite cannot touch at all.

    **``injection`` owns the ``reason`` when both fire**, on ``circling``'s ordering
    rule: the more specific truth wins. A rate that names the fix pass as the AUTHOR
    of this round's findings says more than a fact about the pass's shape, and both
    veto lines are on the record either way.

    **The honest case against it, recorded rather than argued away.** A round whose
    only finding is "this branch has no test" gets a fix pass that is legitimately all
    test, and this ends the cycle on it. Three things make that acceptable and none of
    them is that it will not happen: the round it removes is a round that would have
    reviewed those tests, which is the measured failure rather than a hypothetical;
    the rule can only stop a cycle, so the worst case is one fewer round with a veto
    line saying exactly why, never a merge and never a review that reads cleaner than
    it is; and ``escalate_on.unrefereed_fix: false`` switches it off in one line.

    ``guard_churn`` is #618, and it is the SIXTH bound here — the only one measured per
    FIX PASS rather than per PR, and the only one whose threshold ships unset.

    **What it measures, and why the obvious statistic could not.** The panel already
    reports `guard_ratio`: test and doc lines over source lines, cumulative over the
    whole PR. On lexray#1780 that ratio read 2.21 -> 2.19 -> 2.13 -> 2.09 -> 2.02 across
    five rounds in which source went 476 -> 941 and test went 883 -> 1,632. **It fell
    monotonically through the runaway it was watching**, because a proportion cannot
    tell "this change is well guarded" from "this change and its guards are both running
    away" — the runaway moves numerator and denominator together. A ceiling on it would
    have fired on none of those five rounds and would fire at round 1 on a heavily
    guarded PR that never churned: wrong in both directions.

    The per-pass DELTA is the quantity that can see the event. Rounds 2-5 wrote 380, 205,
    205 and 58 lines of test and prose against 177, 116, 114 and 58 of production, and
    that is a shape. :func:`guard_churn_state` reads it off ``unrefereed``'s own split,
    so the ceiling and #554's budget weight count the same lines.

    **It does not bank.** Each round's measurement is its own fix range and nothing
    earlier. A quiet round cannot fund a loud one, which is the case a ceiling on a pass
    exists for and the case a cumulative reading gets wrong.

    **It ships UNCALIBRATED, and that is the answer rather than a placeholder.**
    `max_fix_guard_lines` defaults to ``None``. The evidence is one cycle; a threshold
    drawn between its quiet round's 58 lines and its loud round's 380 would be a number
    chosen to fit one PR, which #67 forbids. So the count is taken and published every
    round and nothing fires until a repo writes a number. `escalate_on.guard_lines` is
    then a SECOND thing to write before a round can end on it, defaulting to false: a set
    ceiling is watched, an armed one stops. That split is the whole design — the weaker
    action first, the stronger one a flag away rather than baked in.

    **The same two bounds as ``injection``, ``not_falling`` and ``unrefereed``**, and
    deliberately NOT ``max_fix_growth``'s: that ceiling is applied by the caller and
    forces a stop unconditionally, on years of measurement. This one may only turn a ``go
    again`` into a STOP, and only the round rule 1 was buying.

    ``fix_budget`` is #622, and it is the argument here that closes a gap rather than
    opening a measurement: `low_severity_fix_lines` is the one bound on a fix pass that
    was counted by the pass itself. The dial is resolved in `panel.py`, relayed into
    the fixer's brief as a paragraph asking the fixer to run `git diff --numstat` after
    each fix and stop when the budget is gone, and read by nobody else. On lexray#1780
    the relayed number was right every round and the passes came out at 850, 322, 356
    and 142 lines against a budget of 40; the only reason anyone knows is that the
    fixer said so. :func:`fix_budget_state` prices the same split ``unrefereed`` and
    ``guard_churn`` read — production at 1, test and prose at `unrefereed_line_weight`
    — and puts the number beside the limit.

    **Its verdict is one-sided and the payload says which side.** The budget bounds the
    💸 band and not the pass, and a diff cannot attribute a line to the finding it paid
    for, so ``within: True`` is a fact (the whole pass priced under the budget, so the
    budgeted part is too) and ``within: False`` is the absence of one, never an
    accusation. :func:`fix_budget_state` has the argument and the stricter form that
    was left for later.

    **Reported, never gated, and with no flag to arm** — #67's rule for the first half
    of that and #621 for the second: an `escalate_on` key here would be the new dial
    that epic is explicit about not adding. It moves ``stop`` in neither direction and
    files no veto line.

    ``surface`` is #619, and it is the second argument to this function that decides
    nothing — reported, never gated. It is the set of files the last fix pass touched
    that no earlier round had read, measured in `panel.py` and arriving on a fixed
    contract (``files``, ``new_files``, ``count``, ``prior_files``), or ``None`` where
    there was no measurement to make.

    **It is a quantity none of the other dials can see.** Everything downstream of a
    fix pass counts lines or findings — ``max_fix_growth`` at 3.0x,
    ``max_fix_growth_chars`` at 30,000, ``fix_injection`` at 0.5 — and a fix that adds
    fifteen lines to two nginx templates nobody had reviewed is invisible to all three.
    Surface is not size. On lexray#1780 round 3's pass touched twelve files and seven
    of them had never been in front of a reviewer; both of the cycle's later P1s were
    in that new surface, and ten of the PR's files arrived from a fix pass rather than
    from the change under review. ``reviewer_scope`` bounds where a REVIEWER's findings
    may land and nothing bounded where a FIXER's edits may.

    **It is the instrument for the failure ``narrowed`` gives a vocabulary to**, which
    is why the two arrived together: that one lets a fixer decline the class-wide fix,
    this one counts the rounds where it was available and went unused.

    **No gate, on #67's rule.** Nothing here reads it to move ``stop``, and the choice
    between a dial (``max_fix_new_files``) and report-only has not been made. The
    payload's ``fix_surface`` is null rather than zero where the measurement could not
    be taken, on :func:`fix_surface_state`'s argument: round 1 has no pass to read, and
    "the pass opened no new files" is a claim about a fix pass where what happened is
    that nobody looked.

    ``revert`` is #506, and it is the only argument to this function that DECIDES
    NOTHING. Every other one can move ``stop``; this one cannot, in either direction.
    It exists because ending the cycle on ``injection`` is half an answer — **the fix
    pass that caused the damage is still on the branch** when the round finishes, so
    the PR ships carrying a change this function has just finished saying generated
    more of the round's work than the pull request did, minus the round that would
    have found the rest of it.

    So a round that fires #489's rule adds one more veto line: the commit range of the
    offending pass, what reverting it would REMOVE (the findings attributed to it) and
    what it would COST (the complaints it was sent to answer that this round no longer
    raises), with the ``git revert`` invocation spelled out. A PROPOSAL AND NOT AN
    ACTION, which is the constraint the whole shape is built around — reverting a pass
    reverts the real fixes in it too, and a pass that cleared three P2s and introduced
    eight P3s is a net loss to undo wholesale. Nothing here reverts anything, nothing
    here recommends, and the two columns are biased in opposite directions on purpose
    (:func:`fix_pass_outcome`) so that the argument AGAINST reverting always gets the
    benefit of the doubt.

    **On a rebased branch there is no proposal to make, and it says so.** #500's
    finding is that a rewrite between rounds disarms provenance, and this reads the
    same range: ``revert_state`` carries ``panel_scope._fix_range_diff``'s own verdict
    rather than a second vocabulary for "we cannot see this", and a round whose range
    is ``blind`` records that instead of naming a range it cannot see. ``offered`` is
    the field that says a proposal was actually put, and it is ``fired``'s counterpart
    one rule down.

    **``converged`` (#626) is the payload saying, in one boolean, whether this was a
    clean finish** — and it exists because until now the reader had to assemble that
    from four fields and could assemble it wrong. It is true only where the cycle
    STOPPED, ``confident``, with no veto line, nothing outstanding and no escalation
    held; false in every other stop and on every ``go again``.

    It is computed FROM ``confident`` rather than beside it, so a capped stop and a
    vetoed stop are false here by construction rather than by two expressions
    agreeing. Then it is stricter: it also requires ``outstanding.fixable``,
    ``below_floor`` and the held escalations to be empty — the disposal's own "nothing
    is outstanding — the cycle ends with nothing to hand on".

    That strictness is where the judgement is, so it is stated. A below-floor policy
    stop keeps its ``confident: True`` — #165 argues that at length and nothing here
    revisits it — and is nevertheless NOT converged, because its ``reason`` is
    "reported, not fixed here" rather than "dry" and the metric this field serves is
    the share of cycles ending in a confident **dry** round. Counting it would count a
    cycle that ended with real findings unfixed by policy as a clean finish. The
    asymmetry is deliberate: a false negative costs such a round nothing it had, and a
    false positive is the one reading this field exists to make impossible.

    Two honest caveats, recorded here because they are properties of the design
    and not of the code, and because this docstring is where they are KEPT — the
    READMEs and ``panel-review-pr.md`` point at it rather than restating it, since
    five paraphrases of one rule is five things to keep in step:

    - The keys arrive from the caller, which read them out of a fixer's prose
      report. The loop is therefore trusting a claim by the same agent whose fix
      pass produced the finding — the one signal #67's own evidence says cannot be
      self-reported. What that buys is convergence; what it costs is that a fixer
      can end its own cycle by calling a finding a premise. ``escalated`` rides in
      the payload with the round each key was first declared in so the claim is at
      least auditable after the fact, and the cap still binds.
    - A key is not a premise. The register identifies an escalation by the
      finding key it was declared under, which holds exactly while a later round
      re-derives that key — and a fresh panel over the same code very often words
      the same premise differently, which mints a new one
      (``panel-review-pr.md`` §5 says so in as many words). The caller carries
      that gap: it must escalate the NEW key when it recognises the premise
      restated. Closing it mechanically needs premise-level identity, which is
      #67's first piece and is not built. The register also only grows — there is
      no retraction — so once a human ANSWERS a premise the answer ends the cycle:
      the key would otherwise go on subtracting its finding from the work a fix
      round can clear, and go on rendering ⛔, for every round that inherits the
      baseline."""
    # Both key collections are checked at the door, the way every other shape in
    # this file is, because both wrong shapes fail SILENTLY and both failures are
    # the #221 jam this function exists to close.
    #
    # A bare `str` is itself iterable, so `escalated=key` instead of
    # `escalated=[key]` — the natural slip now that these take keys — makes `held`
    # a set of single characters, leaves `blocking` empty against real
    # multi-character keys, and ignores the escalation while the cycle runs to its
    # cap. `repeated="<key>"` is the same slip in the other direction and worse: it
    # reports "N finding(s) an earlier round already raised" with an N invented out
    # of the string's distinct characters.
    #
    # `repeated` also took an `int` COUNT until #221, so a caller outside this diff
    # still on the old contract arrives here; it is named rather than left to a
    # bare `TypeError: 'int' object is not iterable`, which says nothing about what
    # to pass instead.
    #
    # A `dict` is deliberately NOT rejected: it iterates its keys, which is correct
    # and is what the production call site passes (the register itself).
    for name, value in (("repeated", repeated), ("escalated", escalated),
                        ("narrowed", narrowed), ("declined", declined)):
        if isinstance(value, str):
            raise TypeError(
                f"round_stop({name}=...) takes a COLLECTION of finding keys, not one "
                f"string ({_key_gist(value)!r}): a bare str iterates character by "
                f"character, so it matches no finding and says nothing — pass a list")
        if isinstance(value, int):
            raise TypeError(
                f"round_stop({name}=...) takes finding KEYS, not a count ({value!r}): "
                "the escalated and the narrowed ones are subtracted here by key, and a "
                "count computed by the caller cannot express that")
    # Severity by key, off `outstanding` — which carries it for the same findings
    # `new_keys` and `repeated` name. Deriving it here rather than widening either
    # parameter keeps every existing caller's contract: they pass bare keys today,
    # and a key whose severity this cannot find is treated as ABOVE the floor (the
    # `SEVERITIES[0]` fallback), so an unrecognised key costs a round rather than
    # silently dropping a finding out of the loop.
    #
    # This and `exempt` sit ABOVE the escalation/narrowing filters rather than below
    # them because `answered` reads `exempt` — a Sonar gate issue may not be narrowed
    # away — and the filters are what the four rules are applied to. Nothing here
    # depends on them in return: both are read straight off `outstanding`.
    severity = {c.key: c.severity for c in outstanding}
    # SONAR'S HARD-GATE ISSUES ARE EXEMPT FROM BOTH FLOORS, AT EVERY RULE, whatever
    # severity Sonar itself gave them — and Sonar's own severities are routinely P3
    # and P4. The keys are collected off `outstanding` because that is where the
    # verdict lives: `panel.py` builds `outstanding` as `to_fix + sonar` precisely
    # so this function counts them, and the comment above that line says why in its
    # own words — "Sonar's hard-gate issues MUST end up resolved
    # (/panel-review-pr §3), so a round whose only outstanding item is a new or
    # still-open gate issue is not a dry round. Leaving them out classified exactly
    # that as convergence and ended the cycle without another fixer."
    #
    # #165's floors are a policy about what is worth FIXING and what is worth
    # another ROUND, and a red quality gate is neither: it is a merge gate the repo
    # does not get to trade against convergence, and a P3 `python:S1481` keeps the
    # PR unmergeable exactly as a P1 does. Filtered by severity, a new P3 gate issue
    # dropped out of `triggering`, landed in `quiet_new`, and the cycle stopped with
    # `confident: True` and "reported, not fixed here" — re-introducing, through the
    # floors, the bug `outstanding = to_fix + sonar` was written to fix.
    #
    # THE EXEMPTION IS THE PROPERTY OF THE KEY, NOT OF ONE RULE. A THIRD FLOOR MUST
    # GO THROUGH IT TOO: `above` is what rules 1 and 3 ask, and rule 2's own
    # comprehension names `exempt` first for the same reason. Bounding a new floor by
    # severity alone would put the gate issues back under it silently, which is how
    # this came back the first time.
    exempt = frozenset(c.key for c in outstanding if c.verdict == "sonar")

    def above(key: str, floor: str) -> bool:
        return (key in exempt
                or severity_at_least(severity.get(key, SEVERITIES[0]), floor))

    held = frozenset(k for k in escalated if k)
    # The escalated keys THIS round actually saw. The register is a property of
    # the cycle and only grows; what is blocking is a property of the round, and
    # conflating them meant one stale or mistyped key made every later round of
    # the cycle non-confident forever — including rounds that were genuinely dry,
    # and including after a human had answered the premise and the code moved.
    # A permanently vetoed cycle is the "loud and wrong" a reader learns to
    # ignore, which is worse than the jam this whole rule closes.
    blocking = held & ({*new_keys} | {c.key for c in outstanding})
    # #615's fourth outcome, and the SECOND filter in front of the four rules. A
    # `narrowed` finding is one the fix pass answered AT THE POINT IT WAS RAISED,
    # declaring the general form a separate change — so unlike an escalation it is
    # answered rather than forbidden, and it CLEARS: it is not outstanding, rule 3
    # does not count it, and no veto line is owed for it.
    #
    # Why it has to clear rather than merely soften the stop: the vocabulary was
    # `fixed | refuted | deferred` plus escalation, and none of those is "I fixed this
    # one and not its class". A fixer with no way to answer a finding partially
    # answers it MAXIMALLY — which is what makes a pass touch files the finding never
    # named (#619), and the files a fix pass pulls in are where the next round's
    # findings come from. Measured on lexray#1780: one finding about one route became
    # server-level nginx `gzip`, and the round after that was a P1. Leaving `narrowed`
    # as a flavour of "still outstanding" would leave rule 3 holding the cycle open
    # until somebody wrote the class-wide fix, which is the same pressure under a
    # kinder name.
    #
    # A SONAR HARD-GATE ISSUE CANNOT BE NARROWED, and the guard is here rather than in
    # a rule for the reason the `exempt` comment above gives: the exemption is a
    # property of the KEY. Narrowing is a judgement about how far to fix a judged
    # finding; a red quality gate is not a judgement and keeps the PR unmergeable at
    # any severity, so "answered at the point it was raised" is not something a caller
    # gets to say about one. Subtracting it would end the cycle confident with the
    # gate still red — the exact bug `outstanding = to_fix + sonar` was written to fix,
    # arriving through a third door.
    answered = frozenset(k for k in narrowed if k) - exempt
    #: The narrowed keys THIS round actually raised, on `blocking`'s terms and for its
    #: reason: the register is a property of the cycle and what was subtracted is a
    #: property of the round, and a payload that reported the first would go on
    #: crediting a narrowing long after the code moved.
    narrowed_cleared = answered & ({*new_keys} | {c.key for c in outstanding})
    #: #665's register, and the only collection here that is NOT narrowed to the
    #: keys this round raised. The docstring argues that at length; the short of it
    #: is that `blocking`'s bound exists because an escalation waits on a human who
    #: may have answered it since, while a declined correction waits on a fix pass
    #: this cycle can see the output of — and under `increment` scope the round that
    #: would have seen it never re-reads the file. Sorted where it is published, for
    #: `escalated_outstanding`'s reason: the artifact's bytes must not move with the
    #: order of the flags.
    #:
    #: Subtracted from nothing. It appears in no filter, in `cleared_out`, in
    #: `work`, or in any of the four rules — the greps that would show otherwise are
    #: the review this feature is owed.
    unfixed = frozenset(k for k in declined if k)
    # The work a fix round can actually clear, under names of their own. The
    # subtraction happens ONCE, before the rules, because every rule below asks
    # "is there work outstanding" and an escalated finding is precisely work the
    # cycle has been forbidden to do — and a narrowed one is work it has already
    # done — but the parameters keep meaning what they are called, so the cap
    # message and anything else downstream that wants "what the cycle still has to
    # clear, escalations and all" can still say so.
    #
    # ONE subtracted set for the rules and TWO reported ones, because the rules ask
    # the same question of both ("is there work here a fix round can clear?") and a
    # reader asks different ones ("what is a human owed?" against "what did the fixer
    # decline to generalise?"). Folding them into one name would make an escalation
    # and a narrowing indistinguishable in the payload; keeping two subtractions would
    # be two places for the filter to fall out of step.
    cleared_out = held | answered
    clearable_new = [k for k in new_keys if k not in cleared_out]
    clearable = [c for c in outstanding if c.key not in cleared_out]
    #: New findings that buy a round, and the ones that were raised and do not.
    triggering = [k for k in clearable_new if above(k, trigger_floor)]
    quiet_new = [k for k in clearable_new if not above(k, trigger_floor)]
    # Rule 3, at the TRIGGER floor and not the cleared one (#621). The rule is "would
    # this finding have bought a round when it was new?", and a finding that never
    # bought one does not start buying them by being raised twice. Bounded by
    # `cleared_floor` it did: at a fix floor low enough for a budget to reach P3/P4,
    # one unpaid sub-trigger finding is outstanding every round by construction, and
    # the only move that closes it is the fixer writing lines — which is what authors
    # the next round's findings. Rules 2 and the disposal keep the cleared floor;
    # `exempt` still carries a Sonar gate issue past both, and `held`/`answered` are
    # still subtracted in front of all four.
    repeats = len({k for k in repeated
                   if k and k not in cleared_out and above(k, trigger_floor)})
    #: What the new floor stood down on: an earlier round raised them, they are still
    #: outstanding, and they are under the trigger floor. Counted so that the stop can
    #: SAY so. #621's decision is that these are "reported and do not go again", and
    #: the reporting half is not optional — a round holding one is not dry, and letting
    #: it fall through to the dry branch would put a judge-confirmed defect the fixer
    #: was told about behind the word this whole payload is organised against. #165's
    #: below-floor branch for NEW findings already settled the shape: a policy stop
    #: names what it left, keeps its confidence, and takes no veto line, because the
    #: repo said which findings are worth a round and the round obeyed.
    quiet_repeats = {k for k in repeated
                     if k and k not in cleared_out and not above(k, trigger_floor)}
    # Rule 2. The hardcoded ``("P1", "P2")`` — :data:`panel_core.BLOCKING_SEVERITIES`
    # since #78, named rather than spelled because the corroboration threshold has to
    # read the same set to know which findings it may never stand down — is what makes
    # the exemption necessary HERE as well as in `above`: without the first clause a
    # P3 gate issue could not be a blocker at all, however red the gate, so a
    # still-open one had to fall through to rule 3 and would be lost with it the moment
    # `repeated` did not carry the key (a round whose baseline could not be attributed,
    # for one).
    blockers = [c for c in clearable
                if c.key in exempt
                or (c.severity in BLOCKING_SEVERITIES
                    and severity_at_least(c.severity, cleared_floor))]
    #: How many of them are gate issues rather than judged P1/P2s — the `reason`
    #: has to be true of what it counted, and "P1/P2 still outstanding" is not true
    #: of a P3 `python:S1128`.
    gated = sum(1 for c in blockers if c.key in exempt)
    # ---- #42: WHAT THE CYCLE IS LEAVING BEHIND. The verdict about who gets it is
    # taken at the bottom, once every rule has run; these three lists are the
    # measurement and are computed here, where the parameters they read are still
    # the parameters (`held` is shadowed further down, in #506's veto).
    #
    # ONE UNIVERSE, so that nothing falls between the parameters. Rules 2 and 3 read
    # `outstanding` and `repeated`, rule 1 reads `new_keys`, and a key reaching only
    # one of them would otherwise be counted for the STOP and dropped from the
    # disposal — which is this bug one level down. In production all three are built
    # from the same round (`panel.py` derives `new_keys` and `repeated` FROM
    # `outstanding`), so the union changes nothing there and closes the gap for every
    # other caller.
    work = ({c.key for c in outstanding} | {k for k in new_keys if k}
            | {k for k in repeated if k}) - cleared_out
    #: Split by the CLEARED floor on both sides, never the trigger floor. #165's two
    #: dials answer different questions — which findings buy another ROUND, and which a
    #: fix pass was asked to CLEAR — and a disposal is the second question. Since #621
    #: moved rule 3 onto the trigger floor this is the ONLY reader of the cleared floor
    #: besides rule 2, and it is why the two are still separate dials: a finding above
    #: the cleared floor and below the trigger one is work a fix pass can take and no
    #: reason to spend another round, which is exactly what `fixable` is for.
    fixable = sorted(k for k in work if above(k, cleared_floor))
    #: Reported, not fixed here, and NOT handed to anybody — the repo's own policy.
    #: #165 is explicit that a below-floor stop is a POLICY stop and not a failure, so
    #: listing these as work awaiting a fixer would re-open a decision the repo has
    #: already taken. They are named because the alternative is silence, and silence
    #: about them is what lets a below-floor stop read as a dry one.
    below_floor = sorted(k for k in work if not above(k, cleared_floor))
    if triggering:
        stop, reason = False, (f"{len(triggering)} finding(s) no earlier round raised")
    elif blockers:
        stop, reason = False, (
            f"{len(blockers)} P1/P2 or SonarCloud gate issue(s) still outstanding "
            "after the fix" if gated else
            f"{len(blockers)} P1/P2 still outstanding after the fix")
    elif repeats:
        stop, reason = False, (f"{repeats} finding(s) an earlier round already raised "
                               "are still outstanding")
    elif quiet_new:
        # Checked AFTER the three go-again rules and BEFORE the escalation stop, so
        # the reason names the most specific true thing: a round with a below-floor
        # new finding and an outstanding P1 goes again for the P1, and a round whose
        # only news is below the floor stops saying exactly that rather than "dry".
        stop, reason = True, (
            f"{len(quiet_new)} new finding(s), none at or above the "
            f"{trigger_floor} round trigger floor — reported, not fixed here")
    elif quiet_repeats:
        # After `quiet_new` for its own reason one rule up — the most specific true
        # thing wins, and "this round found something new" says more than "this round
        # still carries something old". Before the escalation stop and the dry branch
        # because both would be false here.
        stop, reason = True, (
            f"{len(quiet_repeats)} finding(s) an earlier round raised are still "
            f"outstanding, none at or above the {trigger_floor} round trigger floor "
            "— reported, not fixed here")
    elif blocking:
        # Not "dry": something WAS raised and is unanswered. A reader reconciling
        # "dry" against a PR carrying an open premise question would be told
        # something untrue about why the loop stopped.
        stop, reason = True, (f"nothing left that a fix round can clear — "
                              f"{len(blocking)} escalated finding(s) await a human")
    elif narrowed_cleared:
        # #615's own stop, and it is `quiet_repeats`' argument applied to the fourth
        # outcome. A narrowing CLEARS — that is the whole of the feature and nothing
        # here revisits it — but a round that stopped because a fix pass declared
        # findings answered AT THE POINT THEY WERE RAISED did not stop because
        # nothing was raised, and "dry" is the one word this payload is organised
        # against lending to a round that was not.
        #
        # The keys here are repeats by construction. `--narrowed` is passed on the
        # round that follows the pass which declared it and on no other
        # (`panel-review-pr.md`), and `narrowed_cleared` is bounded to the keys THIS
        # round raised — so every key in it is a finding a fresh panel put up again
        # after the fix pass said it had answered it. Without this branch the loudest
        # such round there is, a judge-confirmed P1 cleared on the fixer's own
        # say-so, reported "dry — nothing raised that an earlier round had not" with
        # `converged: True` beside it.
        #
        # The trigger-floor count is said OUT LOUD, because it is the one number that
        # separates a P1 answered narrowly from a P4 answered narrowly and nothing
        # else in the reason carries it. It costs no confidence: see the docstring —
        # an escalation is an open question a human owes an answer to, a narrowing is
        # an answer already given, and charging it a veto rebuilds the pressure to
        # write the class-wide fix that #615 exists to remove.
        #
        # BELOW `blocking` and below the two floor stops, on the chain's own rule
        # that the most specific TRUE thing wins: each of those names work that is
        # still open, and an answer already given says less than an open question.
        # Above the dry branch, which is the only one it must never fall through to.
        loud = sorted(k for k in narrowed_cleared if above(k, trigger_floor))
        stop, reason = True, (
            f"nothing left that a fix round can clear — {len(narrowed_cleared)} "
            "finding(s) were answered at the point they were raised and the general "
            "form declined"
            + (f", {len(loud)} of them at or above the {trigger_floor} round trigger "
               "floor" if loud else ""))
    elif unfixed:
        # #665, and the branch the issue asks for in as many words: a cycle ending
        # with declarations outstanding has not converged, and must not read as a
        # dry round. This is the case the register was filed over — the pass
        # declined two corrections, the round after it re-read only the fix commit,
        # found nothing, and would have reported "dry" over a defect one of its own
        # actors had already written down.
        #
        # LAST in the chain, under every branch that names open work, on the chain's
        # own rule that the most specific TRUE thing wins. Each branch above says
        # what THIS round observed; this one says what an EARLIER round declared, and
        # a live observation says more. It is above the dry branch, which is the only
        # one it must never fall through to — and every round it catches would
        # otherwise have landed there, because a declared finding this round raises
        # again is a repeat and one of the branches above has it.
        stop, reason = True, (
            "nothing raised that an earlier round had not — but this cycle carries "
            f"{len(unfixed)} correction(s) an earlier fix pass declared it could not "
            "make, so it did not converge: it ran out of corrections anybody was "
            "willing to make, which is not the same as running out of defects")
    else:
        stop, reason = True, ("dry — nothing raised that an earlier round had not"
                              if round_no > 1 else "dry — no findings to fix")
    # #84, and BEFORE the cap on purpose: both can be true of the same round, and
    # "the rounds stopped being about different things" is the more specific truth
    # than "the counter ran out". A cap reached is a cost bound; this is a futility
    # bound, and a reader told only the first would go looking for a bigger cap.
    #
    # It can only ever turn a `go again` into a STOP. There is no branch where a
    # declaration makes the loop run longer — see the docstring's paragraph on why
    # the agent writing the fix does not get that lever.
    # #489, and BEFORE both #84's brake and the cap. Before the cap for the reason
    # given there — a futility bound is the more specific truth than "the counter ran
    # out" — and before `circling` because a cycle doing both is better reported as
    # the premise repeat: that one names the assumption, this one only counts it.
    #
    # `not stop` is a CONDITION and not a redundancy. It is the whole of the guarantee
    # that this dial can never make a review look cleaner than it is: the only
    # transition it can make is `go again` -> STOP, so a dry round, a below-floor
    # policy stop and a round holding an escalation each keep the reason and the
    # confidence they earned.
    #
    # `triggering` IS THE SECOND CONDITION, and it is what keeps the rule inside its
    # own justification. The argument for this brake is about RULE 1 specifically —
    # new findings buy another round, and from round 2 those findings are the loop's
    # own output — so it may only take away the round rule 1 was buying. A round going
    # again under rule 2 or rule 3 is going again for a P1 the fix did not clear or a
    # finding an earlier round already raised, and neither of those is the fix pass
    # generating work: they are work it FAILED to do, and a rate computed over
    # below-floor news must not cancel the repair round for an unrelated blocker. That
    # is the one place this differs from #84's brake, which fires at any of the four
    # rules — and the difference is that a repeated premise is a fixer's own
    # DECLARATION about the patch it is about to write, while this is a threshold on a
    # statistic. A statistic may end the loop it is a statistic about; it may not
    # overrule a named P1.
    #
    # Recorded in a local because the veto and the payload both have to know whether
    # this FIRED, and by then `stop` says only that something did.
    #
    # ONE pre-brake state, read by BOTH volume rungs before either can move `stop`.
    # `injected` used to read `not stop` directly and meant exactly this; #505's rung
    # applies first (it owns the less specific `reason`, so `injection` overwrites it
    # below), and had the second flag gone on reading `stop` it would have been false
    # on every round the first one stopped — recording `fired: false` for a rule that
    # was over its threshold and would have ended the round on its own. Two rules over
    # one pre-brake state answer independently, which is what lets the payload carry
    # both.
    #
    # `held_over` IS PART OF THE BOUND, and leaving it out was a real under-enforcement
    # of #489's own stated rule rather than a nicety (found by a codex second opinion on
    # #505). "It may only take away the round rule 1 was buying" is not the same test as
    # "rule 1 won the `reason`": rules 1, 2 and 3 are an if/elif chain, so a round with
    # four triggering news AND an outstanding P1 from an earlier round reports rule 1
    # while going again for BOTH — and with `triggering` as the only condition, either
    # rung ended it with that P1 unfixed. The docstring's own sentence, "a statistic may
    # end the loop it is a statistic about; it may not overrule a named P1", is
    # precisely what that did.
    #
    # WHAT IT IS NOT is `not blockers`. `blockers` is every outstanding P1/P2, and on
    # the ordinary round those ARE this round's news — four new P2s and nothing else
    # makes `blockers` four items long. Bounded on that, neither rung could fire on the
    # very cycle #489 was measured from, which is how this was caught: the end-to-end
    # test for "a round whose findings are mostly its own damage" went back to ending
    # on the cap. The question rule 2 and rule 3 ask that rule 1 does not is whether
    # there is work here the fix pass FAILED to do, and that is work an EARLIER round
    # raised — so the subtraction is this round's own news. `repeats` is already only
    # that, by construction: `repeated` is the keys an earlier round raised.
    #
    # Both rungs take the corrected bound rather than #505's alone: they state the same
    # rule in the same words, and two brakes whose shared sentence means two different
    # things is worse than either being wrong on its own. It is a strict NARROWING —
    # each fires on a subset of the rounds it fired on before — so nothing it changes
    # can make a review look cleaner: a round it now declines to stop goes again and is
    # read again, and the cap still binds with `confident` false.
    fresh = {*clearable_new}
    held_over = [c for c in blockers if c.key not in fresh]
    going_again = bool(not stop and triggering and not held_over and not repeats)
    injecting = injection_state(None, None) if injection is None else injection
    # #506's proposal, built by the caller for `injection_state`'s reason and read
    # here in exactly one place: it changes no verdict. It cannot make the cycle stop
    # and it cannot keep it going — a REMEDY is not a rule — so it hangs entirely off
    # `injected` below and adds one veto line beside the one that already fired.
    reverting = revert_state(REVERT_NOT_ASKED) if revert is None else revert
    # `going_again` below, and NOT #506's original `not stop and triggering`:
    # #505 named the corrected rule-1 bound after codex found the old form
    # let either rung end a cycle that was going again for a P1 an earlier
    # round raised. The stricter definition wins this merge; taking #506's
    # line would have quietly reverted that fix while both features looked
    # like they had landed intact.
    injected = bool(injecting["over"] and going_again)
    # #505, applied BEFORE `injected` so that `injected` owns the `reason` when both
    # fire — `circling`'s ordering rule, one level down. A rate that names the fix pass
    # as the author of this round's work is the more specific truth than a count saying
    # only that the work is not shrinking, and both veto lines are on the record.
    #
    # `going_again` carries both of this rule's bounds, the same two `injected` takes:
    # `not stop` is the guarantee that it can only make a `go again` into a STOP, and
    # the rest of it — `triggering`, and no held-over blocker or repeat — is what keeps
    # the rule inside its own justification. The argument is about rule 1's input, so it
    # may only take away the round rule 1 was buying, and may not cancel the repair
    # round for a P1 an earlier round raised that this fix pass did not clear.
    flattening = (not_falling_state([], None) if not_falling is None else not_falling)
    flat = bool(flattening["over"] and going_again)
    if flat:
        stop, reason = True, (
            f"{flattening['count']} new finding(s) this round against "
            f"{flattening['was']} the round before — {flattening['streak']} "
            f"consecutive round(s) whose new-finding count did not fall, at the "
            f"`escalate_on.new_findings_not_falling` limit of {flattening['limit']} — "
            "the count is not coming down, and a human triages what is left rather "
            "than another fix pass")
    # #618's rung, applied BETWEEN #505's and #554's, on the chain's ordering rule that
    # the more specific truth wins the `reason`. `flat` says only that the work is not
    # shrinking; this says HOW MUCH guard work the last pass wrote; `unchecked` says the
    # pass wrote no refereed line at all, which is the sharper claim about the same
    # quantity and takes the reason from it; `injected` names that pass as the author of
    # this round's findings.
    #
    # **Two conditions the four rungs beside it do not have, and both are #67.** The
    # ceiling has to be SET — it ships `None`, because one cycle is not a calibration —
    # and `escalate_on.guard_lines` has to be ARMED, because a repo may reasonably set a
    # ceiling to watch it. `over` is recorded either way (`guard_churn_state` keeps the
    # measurement apart from the arming for `referee_state`'s reason), so a round that
    # crossed a watched ceiling says so in the payload and in the report without ending
    # anything.
    #
    # `going_again` carries the same two bounds its siblings take: `not stop` is the
    # guarantee that it can only make a `go again` into a STOP, and the rest of it keeps
    # the rule inside its own justification — the argument is about rule 1's input, so it
    # may not cancel the repair round for a P1 an earlier round raised and this fix pass
    # did not clear. That is a deliberate difference from `max_fix_growth`, which the
    # CALLER applies unbounded: that ceiling has years of measurement behind it and this
    # one has a single PR, so it takes the narrower of the two available shapes.
    guarding = (guard_churn_state(None, None, False) if guard_churn is None
                else guard_churn)
    overguarded = bool(guarding["over"] and guarding["armed"] and going_again)
    if overguarded:
        stop, reason = True, (
            f"the fix pass before this round churned {guarding['lines']} line(s) of "
            f"test and prose, past the `max_fix_guard_lines` ceiling of "
            f"{guarding['limit']} — that is guard work this round would be reviewing "
            "instead of the change, and a human decides what of it was wanted")
    # #554, applied BETWEEN #505's rung and #489's so that the ordering matches what
    # each one knows. `flat` says only that the work is not shrinking; this says what
    # KIND of work the last pass did; `injected` names that pass as the author of this
    # round's findings. The more specific truth wins the `reason` and every rung that
    # fired keeps its veto line, which is `circling`'s ordering rule applied twice.
    #
    # `going_again` carries both of this rule's bounds, the same two the rungs either
    # side of it take: `not stop` is the guarantee that it can only make a `go again`
    # into a STOP, and the rest of it keeps the rule inside its own justification —
    # the argument is about rule 1's input, so it may not cancel the repair round for
    # a P1 an earlier round raised and this fix pass did not clear.
    refereeing = (referee_state(None, False) if unrefereed is None else unrefereed)
    unchecked = bool(refereeing["over"] and going_again)
    if unchecked:
        stop, reason = True, (
            f"the fix pass before this round churned {refereeing['churn']} line(s) "
            f"and not one of them was production code ({refereeing['test']} test, "
            f"{refereeing['prose']} prose) — nothing in the loop can check what it "
            "wrote, so another round would review artefacts no mechanism refereed: "
            "a human answers this, not another fix pass")
    if injected:
        stop, reason = True, (
            f"{injecting['introduced']} of {injecting['new']} new finding(s) "
            f"({injecting['rate']:.0%}) were introduced by the fix pass before this "
            f"round, past the `escalate_on.fix_injection` threshold of "
            f"{injecting['limit']:g} — the fix pass is generating this round's work, "
            "and a human answers that, not another fix pass")
    circling = list((premises or {}).get("repeated") or [])
    # #491's half, on exactly the same terms and gated on the same arming flag the
    # declaration path reads. A repo that switched `escalate_on.premise_undecidable`
    # off asked for its fixers to be allowed to approximate; ending its cycle on the
    # answer anyway would enforce a policy it declined, which is the failure
    # `ESCALATE_ON_UNBUILT` exists to keep on the other side.
    #
    # `premise_state` lists these regardless of the flag, deliberately — the payload
    # records what a cycle DECLARED — so the arming check has to happen here rather
    # than being assumed from the list being non-empty.
    unobservable = (list((premises or {}).get("undecidable") or [])
                    if (premises or {}).get("undecidable_brake") else [])
    if unobservable:
        worded = "; ".join(
            f"{p['text']!r} (rounds {', '.join(str(r) for r in p['rounds'])})"
            for p in unobservable)
        stop, reason = True, (
            f"{len(unobservable)} premise(s) a fix pass was written against assert a "
            f"property the runtime cannot observe — {worded} — so every fix for them "
            "is an approximation and the next round finds the gap: a human answers "
            "this, not a better approximation")
    if circling:
        worded = "; ".join(
            f"{p['text']!r} declared {p['occurrences']}x "
            f"(rounds {', '.join(str(r) for r in p['rounds'])})" for p in circling)
        stop, reason = True, (
            f"{len(circling)} premise(s) a fix pass was written against more than once "
            f"— {worded} — a human answers this, not another fix pass")
    capped = False
    if not stop and round_no >= max_rounds:
        stop, capped = True, True
        reason = f"round cap ({max_rounds}) reached — {reason}, unreviewed"
    # Only on a STOP. The veto list is printed under "why this round's quiet is
    # not evidence of a quiet PR", and on a `go again` round the repeat IS the
    # reason — printing it there told a reader that a round which was not quiet
    # had untrustworthy quiet. `confident` is unaffected: it already requires
    # `stop`.
    if repeats and stop:
        veto = [*veto, f"{repeats} finding(s) an earlier round already raised are "
                       "still outstanding — the fix for them did not land"]
    # Same "only on a STOP" rule as the repeat above, and the same reason: on a
    # `go again` round the escalation is not why the round was not quiet.
    if blocking and stop:
        veto = [*veto, f"{len(blocking)} finding(s) escalated instead of patched are "
                       "outstanding and no round can close them — a human answers "
                       "these, not another fix pass"]
    # #665, and on the same "only on a STOP" rule as the two above. This is the
    # whole of what the register costs, and it is deliberately the least it could
    # cost: no rule changed its answer, no finding was subtracted, and the round
    # went exactly as far as it would have gone. What it loses is `confident`, and
    # with it `converged` — a cycle that ends holding a correction its own fix pass
    # said it could not make is not a clean finish, and until this line the payload
    # had no way to say so.
    #
    # Says the reasons out loud, because the word is what a reader acts on: a
    # correction priced out under a ceiling is a budget conversation, and a finding
    # the fixer refuted is an argument with the panel. Both were "not fixed" before.
    # AND THIS IS A LANDING HOLD, not only a withheld word — said here because the
    # consequence is two files away and a reader of this line will not find it.
    # `confident` below requires an empty veto, so this sets `stop_confident: false`,
    # which `preland`'s `_round_stop_earned` turns into a FAILED check rather than a
    # warning under `--require-earned-stop` — the mode `/panel-review-pr` §7 uses
    # precisely when it is about to offer to land. So a cycle holding one declaration
    # cannot land strictly until the declaration is gone, and nothing retracts one
    # (#674): a fresh cycle is the only exit. That is the intended direction — a
    # correction the loop admitted it could not make is not a clean landing — but it
    # is a bigger cost than "the cycle does not report converged", and a key typed
    # into `--declined` that names nothing is retained too, so a typo holds the PR.
    if unfixed and stop:
        veto = [*veto, f"{len(unfixed)} correction(s) an earlier fix pass identified "
                       "and declared it could not make are still on record for this "
                       "cycle — this stop is the end of what the loop was willing to "
                       "attempt, not the end of what it found (#665)"]
    # #505, and BEFORE #489's line for the reason the stop below it is applied first:
    # a reader coming down the veto list meets the count, then the attribution that
    # explains part of it. Unconditional for `injected`'s reason — `flat` is only ever
    # true on a round this rule itself stopped, so there is no `go again` round it can
    # fire on.
    #
    # The #500 sentence is not decoration. `fix_injection`'s veto and this one look
    # alike to a reader, and the one thing that distinguishes them is which of the two
    # is still armed on a PR that was rebased mid-cycle: provenance is computed against
    # a range a rebase destroys, and a round's own count of its own new findings is
    # not. A reader deciding what to do about this stop needs to know it was not
    # computed from the thing that quietly stopped working.
    if flat:
        series = " -> ".join("?" if n is None else str(n)
                             for n in flattening["counts"])
        veto = [*veto, "the new-finding count has not fallen for "
                       f"{flattening['streak']} consecutive round(s) — {series}"
                       " — at the `escalate_on.new_findings_not_falling` limit of "
                       f"{flattening['limit']}. Counted from the ROUNDS' own totals "
                       "and not from provenance, so unlike `fix_injection` a rebase "
                       "between rounds cannot have disarmed it (#500): this stop is "
                       "that count, not convergence (#505)"]
    # #618, and BEFORE #554's line for the reason its stop is applied first: a reader
    # coming down the veto list meets how much the pass wrote, then what kind of work it
    # was, then what it cost. Unconditional for `injected`'s reason — `overguarded` is
    # only ever true on a round this rule itself stopped, so there is no `go again` round
    # it can fire on.
    #
    # It says the ceiling is UNCALIBRATED out loud. Every other veto here rests on a
    # number with a measurement behind it, and a reader deciding what to do about this
    # stop needs to know that this one rests on a number their own repo wrote — which is
    # also the honest reading of #67 on the one rung that could not satisfy it.
    if overguarded:
        veto = [*veto, f"the fix pass before this round churned {guarding['lines']} "
                       "line(s) of test and prose — past the `max_fix_guard_lines` "
                       f"ceiling of {guarding['limit']}, counted over THAT PASS and "
                       "nothing earlier, so a quiet round cannot have funded it. The "
                       "ceiling is a number this repo wrote and not one anybody has "
                       "calibrated (#67): the shipped value is null, and this stop is "
                       "that count against that number, not convergence (#618)"]
    # #554, and BEFORE #489's line for the reason its stop is applied first: a reader
    # coming down the veto list meets what the pass WAS, then the attribution saying
    # what it cost. Unconditional for `injected`'s reason — `unchecked` is only ever
    # true on a round this rule itself stopped, so there is no `go again` round it can
    # fire on.
    #
    # It names red/green explicitly, because that is the mechanism a reader will
    # otherwise assume covered this. It ran on the pass #554 measured and went red 4
    # of 4; what it asks is whether a test detects the thing it was written for, never
    # whether the test is sound in any other way.
    if unchecked:
        veto = [*veto, f"the fix pass before this round churned "
                       f"{refereeing['churn']} line(s) — {refereeing['test']} test, "
                       f"{refereeing['prose']} prose, and NO production code. Nothing "
                       "in the loop can check any of it: a production fix has an "
                       "external referee in red/green, the suite and CI, and a test "
                       "fix has none because nothing tests a test. Red/green passing "
                       "on such a pass says only that each new test detects the thing "
                       "it was written for — not that it is sound. This stop is that "
                       "fact, not convergence (#554)"]
    # #489. Unconditional for `circling`'s reason and one of its own: `injected` is
    # only ever true on a round this rule itself stopped, so there is no `go again`
    # round it can fire on. Said in full — both counts, the rate, the dial and the
    # floor caveat — because this veto is the only place a reader is told WHY a
    # number that is not a measurement is nevertheless enough to end a cycle.
    if injected:
        veto = [*veto, f"{injecting['introduced']} of {injecting['new']} new "
                       f"outstanding finding(s) — {injecting['rate']:.0%} — were "
                       "introduced by the fix pass before this round, past the "
                       f"`escalate_on.fix_injection` threshold of "
                       f"{injecting['limit']:g}. `introduced` is a documented FLOOR "
                       "and not a measurement (#48), so the real share is at least "
                       "that: this stop is that number, not convergence (#489)"]
    # #506, and it is the OTHER HALF of the line above. That one ends the cycle; this
    # one says what to do about the change that ended it, which is still on the branch
    # and ships with the PR unless somebody acts. Its own bullet rather than a clause
    # on the veto above, because the two are read by a reader at different moments: the
    # first is why this round's quiet does not count, and the second is a decision
    # somebody has to take.
    #
    # `offered` is `fix_injection`'s `over`/`fired` distinction applied one rule down.
    # The proposal is only makeable when a commit range can be NAMED, and #500 is the
    # case where it cannot: on a rebased branch the range is `blind`, every finding is
    # `unknown`, and this rule is disarmed by the same absence that disarms the gate.
    # It cannot normally be reached (a blind round cannot be `injected` at all, since
    # `introduced` is then zero), and it is written as a branch rather than as an
    # assertion because a caller that hands this an unreadable range must be told so
    # plainly instead of shown a proposal with no range in it.
    offered = bool(injected and reverting["range"])
    if offered:
        removes, costs = reverting["removes"], reverting["costs"]
        priced = (f"COST the {len(costs)} it was sent to answer that this round no "
                  f"longer raises ({_by_severity(costs)})" if costs else
                  "COST nothing this round can see — it cleared none of the "
                  "complaints it was sent to answer")
        # Said only under `increment`, because under whole-PR scope the cost list IS a
        # re-review and the caveat would be false. `fix_pass_outcome` has the argument:
        # a round that re-read only the fix commit did not look at most of what the
        # pass was sent to fix, so "no longer raises" is an upper bound on the cost.
        upper = (" This round re-read only the fix commit, so some of that cost is "
                 "code nobody looked at again rather than defects the pass fixed — "
                 "read it as a ceiling." if reverting["scope"] == "increment" else "")
        # NOT `held`, which is this function's frozenset of escalated keys and was
        # shadowed here by a string. Nothing read it after this line, so it cost
        # nothing until #42 gave the escalated set a second reader — and a name that
        # is safe only while nobody uses it again is a trap rather than a saving.
        still_open = (f" {len(reverting['still_open'])} of its complaint(s) are still "
                      "outstanding either way, so reverting costs nothing there."
                      if reverting["still_open"] else "")
        # The command is offered only where the range is known to hold nothing but the
        # fix pass's own commits; otherwise the reason is printed in its place. A
        # wholesale `git revert` over a range carrying a base-branch merge is not a
        # smaller version of the right action, it is the wrong one, and printing it
        # with a caveat beside it invites the paste.
        how = (f"Reverting it (`{reverting['command']}`)" if reverting["command"]
               else f"Reverting it — no wholesale command is offered here, because "
                    f"{reverting['no_command']} —")
        pass_of = (f" The pass is {reverting['commit_count']} commit(s)."
                   if reverting["commit_count"] else "")
        # Said whenever the range is wider than one fix phase, because the sentence
        # above calls it "the fix pass" and there is then more than one of them. The
        # range is still the one the rate accused — that is the guarantee this feature
        # rests on — so this widens what a revert would undo, not what it would be
        # wrong about.
        spans = reverting["spans"]
        wide = (f" NOTE: round {reverting['round']} is the last earlier round that "
                f"recorded a commit, so this range covers {spans} fix passes rather "
                "than one — the rate was computed over all of it too."
                if spans and spans > 1 else "")
        veto = [*veto, (
            f"the fix pass that did it is `{reverting['range']}` — everything that "
            f"landed after round {reverting['round']} — and it is STILL ON THE "
            "BRANCH: the cycle ending does not take it off, so this PR ships the "
            "change the line above says generated more work than the pull request "
            f"did.{pass_of}{wide} {how} would REMOVE the "
            f"{len(removes)} finding(s) attributed to it ({_by_severity(removes)}) "
            f"and {priced}.{still_open}{upper} A PROPOSAL AND NOT AN ACTION — reverting a "
            "pass reverts the real fixes in it too, and nothing here knows which "
            "those are without asking. `round_stop.revert` carries the commits and "
            "both lists in full (#506)")]
    # Only where a range was ATTEMPTED and did not come back — `blind` (the rebase
    # whose attribution came from nowhere else) or `no-fix`. :data:`REVERT_NOT_ASKED`
    # is excluded because it is not a failure to read anything: it means no fix pass
    # sat between two rounds, which cannot be true of an injected round and is what a
    # caller that passed no `revert` at all gets. Telling such a caller that its branch
    # was rewritten would be inventing a diagnosis out of an argument nobody supplied.
    #
    # `FIX_RANGE_REWRITTEN` is deliberately NOT here, and the omission is the kind that
    # reads like an oversight on the next merge, so: `main` split the rebase case in
    # two after #506 was written, and a rewritten range attributes NOTHING (#512) —
    # every new finding is recorded `unknown`, which is precisely why `fix_injection`
    # cannot fire on such a round. `injected` is therefore false whenever the kind is
    # `rewritten`, and a rung added for it would be unreachable. The case is not silent:
    # #512's veto in `panel.py` fires on every rewritten range and says in as many words
    # that #506 cannot name the offending pass either. `blind` stays because #512 gave
    # it a second source — attribution can succeed from the diff the seats read even
    # when the range did not come back — so `blind` and `injected` do co-occur.
    elif injected and reverting["kind"] in (FIX_RANGE_BLIND, FIX_RANGE_NO_FIX):
        veto = [*veto, (
            "the fix pass that did it is still on the branch and CANNOT BE NAMED: "
            f"{reverting['why'] or 'this round had no readable fix range'}. The range "
            "that would identify the offending pass is the range that is missing "
            "(#500), so there is no revert to propose here — what ships is a change "
            "this cycle can measure and cannot point at (#506)")]
    # #84. Unconditional rather than "only on a STOP", because `circling` forces the
    # stop a few lines above — there is no `go again` round this can fire on, and
    # writing the guard anyway would say there was.
    if circling:
        veto = [*veto, f"{len(circling)} premise(s) were declared more than once in "
                       "this cycle — the rounds have stopped being about different "
                       "things, and the next fix pass would be the third patch on one "
                       "assumption (#67, #84)"]
    # Unconditional for the same reason `circling`'s is: it forces the stop above, so
    # there is no `go again` round this can fire on. Its own line rather than folded
    # into the one above, because the two say different things to a human deciding
    # what to do next — one asks whether to keep patching an assumption, the other
    # asks whether the property can be checked here at all.
    if unobservable:
        veto = [*veto, f"{len(unobservable)} premise(s) in this cycle assert a property "
                       "nothing in the runtime can observe, so no fix for them can be "
                       "verified where it runs and each round patches the last "
                       "approximation (#491)"]
    # ---- #42: WHO GETS IT. The verdict half, taken here because it is the only
    # point at which every rule has run and `stop` is final.
    #
    # A rung that ended the cycle by saying a HUMAN answers this. Every one of them
    # says exactly that in its own `reason` — "a human triages what is left rather
    # than another fix pass" (#505), "a human answers that, not another fix pass"
    # (#489, #554, #84, #491) — so handing their leftovers to a final fix pass would
    # contradict a sentence this same payload is carrying. That is the distinction
    # the cap does not have and the reason #42 is about the cap: a cap is a COST
    # bound, and "the cycle has spent enough" is not a claim about what the next fix
    # pass would be worth. `capped` is deliberately not read here — what makes a
    # disposal a fixer's is that no rule said otherwise, not that one particular rule
    # fired, so a stop added later inherits the safe answer by saying so in this list
    # rather than by being remembered here.
    futile = bool(flat or unchecked or injected or circling or unobservable)
    if not stop:
        # The cycle is going again, so there is no disposal to make: §5's ordinary
        # path hands this round's findings to the next fix pass and the round after
        # reviews the result. Null rather than "fixer" for the reason `fix_injection`
        # keeps `over` apart from `fired` — a caller gating a FINAL, unreviewed fix
        # pass on this field must not have it answered by a round that is mid-cycle.
        handed_to, why = None, None
    elif futile and (fixable or blocking):
        handed_to = "human"
        why = (f"{len(fixable) + len(blocking)} finding(s) are outstanding, and this "
               "cycle ended on a rule that says a human answers them rather than "
               "another fix pass — sending them to one would contradict the reason "
               "above. Triage what is left (#42)")
    elif fixable:
        handed_to = "fixer"
        # The escalations beside them, said HERE and not left to the `escalated` list
        # alone. `why` is the sentence the relay repeats, so a reader acting on it and
        # on nothing else would otherwise send a mixed round's whole remainder to a
        # fix pass — including the one class of finding no fix round may touch.
        beside = (f" {len(blocking)} escalated finding(s) are outstanding beside them "
                  "and are NOT a fixer's — those go to a human (#221)."
                  if blocking else "")
        why = (f"{len(fixable)} finding(s) are outstanding and a fix pass can clear "
               "them, but no round is left to read the result. So they are fixed and "
               "the resulting commit ships UNREVIEWED, or they are not fixed at all: "
               "there is no third option, and until now the cycle silently took the "
               f"second. The default is the first, said plainly in the relay.{beside} "
               "A PROPOSAL AND NOT AN ACTION — nothing here runs a fixer, and the "
               "choice is the operator's (#42)")
    elif blocking:
        # Escalations alone. Not a fixer's, at any stop: no fix round may touch an
        # escalated finding, which is the whole of #221.
        handed_to = "human"
        why = (f"{len(blocking)} escalated finding(s) are outstanding and no fix round "
               "may touch them — a human answers the premise (#221, #42)")
    else:
        handed_to = "nobody"
        why = ("nothing is outstanding — the cycle ends with nothing to hand on"
               if not below_floor else
               f"{len(below_floor)} finding(s) are outstanding and every one is under "
               f"the {cleared_floor} cleared floor: the repo's own policy is that "
               "these are reported and not fixed here, so nothing is handed on (#165)")
        # #665, appended rather than branching: the disposal is still `nobody` — a
        # declined correction is by definition one no fix pass would take, so there
        # is nobody in the loop to hand it to — but "nothing is outstanding" is not
        # a true sentence to end on while the cycle holds one, and this branch's
        # `why` is the sentence a relay repeats. The keys are in `outstanding.declined`
        # beside it.
        if unfixed:
            why += (f". {len(unfixed)} correction(s) an earlier fix pass declared it "
                    "could not make are still on record: nothing in the loop takes "
                    "them, so they land with the PR unless a human decides otherwise "
                    "(#665)")
    # Computed here rather than inline in the payload so that `converged` below is
    # built FROM it and cannot drift out of step with it: a capped or vetoed stop is
    # then unable to read as a clean finish by construction rather than by two
    # expressions agreeing.
    confident = bool(stop and not capped and not veto and baseline_ok)
    return {
        "stop": stop,
        "reason": reason,
        # What this ROUND was holding — the register it was given, narrowed to the
        # keys this round raised. Named apart from the payload's `escalated`,
        # which is the cycle's whole register and only grows: a reader asking
        # "what stopped round 3" wants the first, and a later round inheriting the
        # question wants the second. Sorted, so a round that declares the same set
        # twice writes the same bytes and a diff means something changed.
        "escalated_outstanding": sorted(blocking),
        # #665's register as this round was given it, sorted for
        # `escalated_outstanding`'s reason. Not narrowed to the keys this round
        # raised — the docstring argues why, and the field name says "the cycle's",
        # which is what it is.
        "declined_outstanding": sorted(unfixed),
        # What this ROUND cleared narrowly (#615), on `escalated_outstanding`'s terms:
        # the register it was given, narrowed to the keys this round raised. These are
        # the findings that did NOT count at any rule and are not in `outstanding`
        # below, so a reader reconciling this round's finding count against the
        # disposal would otherwise find them missing from both and conclude the payload
        # had dropped them. No veto is owed and none is taken: a narrowing is an answer,
        # not an open question — the two lines of justification and the board row it
        # owes are the caller's (`panel-review-pr.md`), and an issue is owed only where
        # the general form is itself a claim-miss.
        "narrowed": sorted(narrowed_cleared),
        # "Nothing left to find" is a claim; "the counter hit zero" is not the
        # same claim, and the difference is exactly what a reader of a clean
        # verdict needs to see.
        "confident": confident,
        # #626, and it is the number this whole convergence epic is judged on: the
        # share of cycles ending in a confident dry round. Every field it is built
        # from was already here and a reader had to ASSEMBLE it — `stop` and
        # `confident` and an empty `veto` and an empty `outstanding.fixable` and an
        # empty `escalated_outstanding` — which is four joins to answer one question,
        # and four places to get it wrong in the direction that flatters the loop.
        #
        # `confident` carries the first four conjuncts (a stop, not capped, no veto,
        # a baseline that loaded), so a CAPPED stop and a VETOED stop are both false
        # here by construction — that is the guarantee, and it is why this is computed
        # off `confident` rather than beside it: the two cannot disagree.
        #
        # The rest is "and nothing was left": nothing a fix pass could take
        # (`fixable`), nothing under the cleared floor either (`below_floor`), and no
        # escalation being held (`blocking`). That is exactly the disposal's own
        # "nothing is outstanding — the cycle ends with nothing to hand on", and it is
        # deliberately STRICTER than `confident`. A below-floor policy stop is a
        # legitimate configured convergence and keeps its confidence (#165 argues that
        # at length and nothing here revisits it) — but its `reason` is "reported, not
        # fixed here" rather than "dry", and a metric that counted it would be counting
        # a cycle that ended with real findings unfixed by policy as a clean finish.
        # False here costs such a round nothing; a false TRUE would be the one reading
        # this field exists to make impossible.
        # `unfixed` is already carried by `confident` through the veto line above,
        # and it is named here as well for the reason `blocking` is: this flag is
        # what the epic is judged on, and a reader checking it against the payload
        # must be able to see every conjunct rather than reconstruct one from a veto
        # string. The two cannot disagree — both read the same local.
        "converged": bool(confident and not fixable and not below_floor
                          and not blocking and not unfixed),
        "veto": veto,
        "round": round_no,
        "max_rounds": max_rounds,
        # The floors this verdict was reached under, and what they held back. A
        # consumer comparing two rounds' `stop` has to be able to see that the
        # answer changed because the policy did — and `new_below_trigger_floor`
        # is the count that would otherwise be invisible: those findings ARE in
        # the payload's buckets, and nothing else says they were new and did not
        # buy a round.
        # #549: the second is `Dials.cleared_floor` and is published under that name.
        # It used to be published as `fix_floor` while `Dials.fix_floor` — live, one
        # module over, with its own docstring — held a DIFFERENT value at the shipped
        # defaults (`P2` here against `P3` there). An orchestrator briefing a fixer
        # from the JSON rather than from the report briefed the wrong floor and
        # silently dropped a whole band out of the round's work. Concept three under
        # concept two's name is not two concepts sharing a name loosely; the test at
        # `test_panel_dials.py::test_the_dials_answer_the_three_floor_questions_separately`
        # already writes the invariant down and only the serialisation disagreed.
        "trigger_floor": trigger_floor,
        "cleared_floor": cleared_floor,
        "new_below_trigger_floor": sorted(quiet_new),
        # #621's counterpart to the line above, and there for its reason: these
        # findings ARE in the payload's buckets, and nothing else says an earlier
        # round raised them and they still did not buy this one. The pair is what
        # lets an aggregator tell a repo that its trigger floor is where the cycle's
        # unfinished work is going, rather than only that the cycle stopped.
        "repeated_below_trigger_floor": sorted(quiet_repeats),
        # #84's register as this round read it, and ALWAYS present — an absent key
        # and "nothing was declared" are different claims, and a consumer that had
        # to tell them apart would be reading a payload's age rather than a cycle's
        # state. `undeclared_rounds` is the honest half: those fix passes could not
        # have been braked, whatever this round's stop says.
        "premises": premise_state({"premises": []}, round_no, None) if premises is None
        else {"limit": premises.get("limit"),
              "declared": premises.get("declared", 0),
              "repeated": circling,
              # The DECLARED list, not the armed one: a payload records what the
              # cycle said, and `undecidable_brake` beside it says whether this run
              # was going to act on it. Collapsing the two would make a repo that
              # switched the brake off indistinguishable from one where no fixer ever
              # answered the question.
              "undecidable": list(premises.get("undecidable") or []),
              "undecidable_brake": bool(premises.get("undecidable_brake")),
              # #560. `wired` is whether this round was handed a register at all,
              # kept apart from `undeclared_rounds` on exactly the terms
              # `undecidable_brake` is kept apart from `undecidable`: one is what
              # the cycle said and the other is whether it was ever in a position
              # to say it. `retroactive` is the declarations this cycle's own
              # records place after the pass they explain — evidence about the
              # cycle, not a rung, and not a proof: each entry rests on a reading
              # taken in the actor's environment. Nothing here stops anything,
              # deliberately: the
              # brake's whole claim is that it runs before the patch, and a stop
              # taken on a round is the late half that `repeated` and
              # `undecidable` already occupy.
              "wired": bool(premises.get("wired")),
              "stamped": int(premises.get("stamped") or 0),
              "retroactive": list(premises.get("retroactive") or []),
              "undeclared_rounds": list(premises.get("undeclared_rounds") or [])},
        # #489's measurement as this round read it, and ALWAYS present for the reason
        # `premises` is: a payload with no key and a round with nothing to attribute
        # are different claims, and a consumer forced to tell them apart would be
        # reading the payload's age rather than the cycle's state. `rate` is null where
        # there was nothing to divide, and `limit` is null where the repo switched the
        # brake off.
        #
        # `over` AND `fired`, because they are different questions and reading the
        # first as the second is a lie about a clean round. `over` is a property of the
        # MEASUREMENT — this round's rate crossed the threshold — and it is true of
        # plenty of rounds this rule deliberately does not touch: a below-floor policy
        # stop, a round holding an escalation, a round going again for a P1 under rule
        # 2. `fired` is the property of the VERDICT: this rule is why the cycle
        # stopped. A consumer that gated a "the cycle ended on divergence" sentence on
        # `over` would attach it to a confident, converged round, which is exactly the
        # misreporting the rest of this function is organised against.
        "fix_injection": {**injecting, "fired": injected},
        # #506's remedy for the rule above, and ALWAYS present for the reason
        # `fix_injection` and `premises` are: an absent key and "there was nothing to
        # propose" are different claims. `kind` says which of those it is, in
        # `_fix_range_diff`'s own words — `ok`, `no-fix`, `blind` (#500) or
        # `not-asked` — so a consumer never has to read `range: null` and guess
        # whether the branch was rebased or the round was simply the first one.
        #
        # `offered` is `fix_injection.fired`'s counterpart and is the only field here
        # that is a verdict rather than a measurement: the cycle stopped on injection
        # AND a commit range could be named, so this round is putting a revert to a
        # human. Every other round records what it knows and proposes nothing.
        "revert": {**reverting, "offered": offered},
        # #505's measurement, ALWAYS present for the reason `fix_injection` beside it
        # is: a payload with no key and a cycle with nothing to compare are different
        # claims. `counts` is the whole series the verdict was taken over rather than
        # just the streak, so a reader can check `streak` against it instead of taking
        # it on trust — the same reason `injection_state` publishes `rate`, `limit` and
        # `over` side by side.
        #
        # `over` AND `fired` kept apart on exactly `fix_injection`'s terms. `over` is a
        # property of the MEASUREMENT and is true of plenty of rounds this rule
        # deliberately does not touch — a below-floor policy stop, a round holding an
        # escalation, a round going again for a P1 under rule 2. `fired` is the
        # property of the VERDICT. A consumer that read the first as the second would
        # attach "the cycle ended without converging" to a confident, converged round.
        "new_findings_not_falling": {**flattening, "fired": flat},
        # #554's measurement, ALWAYS present for the reason its three siblings are: a
        # payload with no key and a round with no fix pass to read are different
        # claims, and a consumer forced to tell them apart would be reading the
        # payload's age rather than the cycle's state. `armed` says whether the repo
        # asked for this to be acted on, kept apart from the counts for the reason
        # `premises` keeps `undecidable_brake` apart from `undecidable`: a repo that
        # switched the brake off still gets to see that a fix pass wrote nothing
        # checkable.
        #
        # `over` AND `fired` kept apart on exactly `fix_injection`'s terms. `over` is a
        # property of the MEASUREMENT and is true of plenty of rounds this rule
        # deliberately does not touch — a below-floor policy stop, a round holding an
        # escalation, a round going again for a P1 under rule 2. `fired` is the
        # property of the VERDICT.
        "unrefereed_fix": {**refereeing, "fired": unchecked},
        # #618's measurement, ALWAYS present for the reason its siblings are: a payload
        # with no key and a round that measured no guard churn are different claims.
        # `limit` is null on every repo that has not written one, which is every repo
        # today, and that is the field a consumer reads to tell "under the ceiling" from
        # "there was no ceiling". `over` is the measurement and `armed` is the policy,
        # kept apart on `referee_state`'s terms; `fired` is the verdict, kept apart from
        # both on `fix_injection`'s.
        "guard_churn": {**guarding, "fired": overguarded},
        # #622's measurement, ALWAYS present for the reason its siblings are, and with
        # no `fired` field for `fix_surface`'s: there is no verdict to have, because
        # nothing above reads this to move `stop`. `within` is the field a consumer
        # wants and it is THREE-STATE on purpose — true is "the pass priced under the
        # budget", false is "that could not be shown", null is "nothing was measured or
        # no budget was in force" — so a reader that treats false as a breach is wrong
        # about a round that cleared two P1s. `fix_budget_state` has the argument.
        "fix_budget": (fix_budget_state(None, None, 1, False)
                       if fix_budget is None else fix_budget),
        # #619's measurement, and the ONE block here whose key can be null: the files
        # the last fix pass touched that no earlier round had read. Reported and not
        # gated — #67's instrument-before-gate rule, and the gate has not been decided
        # — so unlike its four siblings it has no `fired` field, because there is no
        # verdict to have. Null where it could not be measured (round 1, or no readable
        # fix range), never a zero: `fix_surface_state` has the argument, and the short
        # of it is that "no pass opened a new file" and "nobody looked" are different
        # claims and only one of them is ever true of round 1.
        "fix_surface": fix_surface_state(surface),
        # #42, and it is the only block here that is not about whether to go again.
        # Every other field answers "should another PANEL run"; this one answers the
        # second question `stop` was being read as answering and was never computed
        # from — "should these findings be FIXED" — which is why it is a block beside
        # `stop` rather than a nuance inside it.
        #
        # ALWAYS present, for the reason its four siblings are: an absent key and "the
        # cycle left nothing behind" are different claims, and a consumer forced to
        # tell them apart would be reading the payload's age rather than the cycle's
        # state. The one exception is a payload no cycle produced — a spend-ceiling
        # refusal builds a `round_stop` by hand (`panel.py`) and has no findings to
        # dispose of; a consumer reading `handed_to` there gets null, which is the
        # answer.
        #
        # The key shares the NAME of this function's `outstanding` parameter and does
        # not share its meaning: the parameter is the Canonical findings the cycle has
        # to clear, and this is the disposal of what is left of them. #42 asks for it
        # under this name and the payload is the artefact people read, so the name
        # follows the issue rather than the local.
        #
        # `escalated` is `escalated_outstanding` above under a second name, off the
        # same `blocking` local and so unable to disagree with it. It is repeated
        # because this block is the whole answer to "who gets what is left", and a
        # reader that had to join it against a sibling key to find the one class of
        # finding no fixer may take is a reader who will not.
        "outstanding": {
            "fixable": fixable,
            "below_floor": below_floor,
            "escalated": sorted(blocking),
            # #615, and here for the same reason `escalated` is repeated here: this
            # block is the whole answer to "who gets what is left", and a narrowed
            # finding is the one class that is in none of the three lists above
            # because it was ANSWERED. Absent, a reader joining the round's findings
            # against this block would find them nowhere and read the gap as a
            # dropped finding rather than as a fixer's declared decision.
            "narrowed": sorted(narrowed_cleared),
            # #665. In this block for the reason `escalated` and `narrowed` are: it
            # is the whole answer to "who gets what is left", and a known-unfixed
            # defect is left to whoever lands the PR. It is not a fourth disposal —
            # a declined finding that is still outstanding is in `fixable` too, and
            # that is not double counting: `fixable` says a fix pass COULD take it,
            # this says a fix pass already looked at it and said it would not.
            "declined": sorted(unfixed),
            # null on a `go again`, where no disposal is being made; otherwise
            # `fixer`, `human` or `nobody`. `why` is the sentence a relay repeats.
            "handed_to": handed_to,
            "why": why,
        },
    }


#: Everything this module offers, INCLUDING the underscore names — the suites
#: reach for several of them through `panel`, and a plain star import would drop
#: them silently. Generated from the module's own top level, so a helper added here
#: is exported without anyone remembering to list it.
__all__ = [
    "panel_core", "panel_seats", "cluster_findings", "_account",
    "_fold_reports", "_NOT_WORD", "_norm_title", "_key_from_title",
    "_defect_title", "_defect_key", "_finding_id", "Canonical",
    "_KEY_RE", "_key_norm", "_is_key", "_key_gist",
    "DECLINE_REASONS", "DECLINE_UNSTATED", "declination_or_none", "Declination",
    "_unmerged", "_judge_listing", "_parse_verdicts", "adjudicate",
    "MAX_RECURRENCE_FINDINGS", "RECURRENCE_TITLE_CHARS", "recurrence_brief",
    "REWORD_RATIO", "_TITLE_NOISE", "_stem", "_same_words",
    "Baseline", "_baseline_title", "_SHA_RE", "_mtime",
    "_positive_int", "_nonneg_int", "_whole_pr_chars",
    "TREND_SEVERE", "RoundTrend", "attributed", "_countable",
    "_introduced", "_trend_row",
    "_inherit", "DECLINED_COST", "_inherit_declined", "load_baseline", "coverage_veto", "CI_SETTLED", "CI_UNSETTLED",
    "CI_NOT_APPLICABLE", "round_stop",
    "CLAIM_KEY_PREFIX", "CLAIM_KEY_RE", "_claim_norm", "claim_key",
    "is_claim_key", "Obligation", "CoverageRuling", "_coverage_ruling",
    "reached_obligations",
    "ESCALATE_ON_DEFAULTS", "ESCALATE_ON_UNBUILT", "PREMISE_REPEATED_EXIT",
    "DECIDABILITY", "premise_undecidable_brake",
    "FIX_INJECTION_MIN_NEW", "fix_injection_limit", "injection_state",
    "REVERT_NOT_ASKED", "fix_pass_outcome", "revert_state", "_by_severity",
    "_no_command_why",
    "NOT_FALLING_MIN_NEW", "not_falling_limit", "not_falling_state",
    "unrefereed_fix_brake", "referee_state", "fix_surface_state", "fix_budget_state",
    "guard_lines_brake", "guard_churn_state", "_pass_churn", "churn_cells",
    "PREMISE_REGISTER_VERSION", "premise_repeat_limit", "premise_key",
    "same_premise", "new_premise_register", "load_premises", "find_premise",
    "declare_premise", "undeclared_passes", "premise_state",
    "working_head", "retroactive_declarations",
    "premise_report", "declare", "announce_escalation",
]
