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
from collections.abc import Iterable          # noqa: E402
# Named here for the same reason, and used by exactly one check: a baseline's
# recorded finish has to be a FINITE instant, and `json` parses a bare `Infinity`.
import math                                   # noqa: E402

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
            "related": self.related,
            "rationale": self.rationale,
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


def _parse_verdicts(parsed: list, flat: list[Finding], pr: int) -> list[Canonical]:
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


def adjudicate(clusters: list[list[Finding]], diff: str, model: str, pr: int,
               budget: int | None = DEFAULT_DIFF_BUDGET,
               coverage: dict[str, list[str]] | None = None,
               ci: str = "",
               code_tree: Path | None = None,
               budget_usd: float | None = None
               ) -> tuple[list[Canonical], str | None, str]:
    """The 'master' rules on every finding, merges the duplicates it finds, AND
    rules on the coverage the reviewers declared about themselves.

    Returns (canonical findings, skip_reason, coverage_note). skip_reason is None
    when the judge ran (even if it dismissed nothing); otherwise it explains WHY
    it could not rule — CLI absent, timeout, crash, a zero exit that produced no
    output, or output with no JSON verdict in it — so the caller can surface that
    rather than a bare 'unavailable'. The judge inherits run_cli's empty-output
    guard for free: a judge that printed nothing now reports "produced no output"
    (with its own stderr quoted) instead of blaming the shape of a reply it never
    made.

    The coverage ruling is one extra key in the object the judge already returns,
    so it costs no additional model call — and its own reply may still be the
    bare verdict array an earlier judge returned, in which case there is simply
    no coverage note.

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
        return [], None, ""
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
    stated = "\n".join(f"- {name}: could not assess {'; '.join(items)}"
                       for name, items in sorted(declared.items())) \
        or "- (no reviewer declared a gap in its coverage)"
    listing, flat = _judge_listing(clusters, MAX_LISTING_CHARS)
    listing = listing or ("- (no findings this round — there is nothing to adjudicate "
                          "but the coverage below; return an empty `verdicts` array)")

    def unruled(reason: str, note: str = "") -> tuple[list[Canonical], str, str]:
        return [_unmerged(f, pr, i + 1, "unjudged", "unjudged")
                for i, f in enumerate(flat)], reason, note

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
            prompt = prompt.replace(JUDGE_CODE_SLOT, "")
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
    note = ""
    reply = parsed if isinstance(parsed, dict) else None
    if reply is not None:
        # `"coverage_note": "..."` is what JUDGE_PROMPT asks with, not an answer to
        # it. Printed in the PR comment it reads as a coverage ruling nobody made.
        # (A reply that is nothing BUT the schema never gets here — those are not
        # candidates at all — but a real ruling can still carry the stand-in note.)
        note = str(reply.get("coverage_note") or "").strip()
        note = "" if note in SCHEMA_DECLARATIONS["verdicts"] else note
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
            return [], None, note
        return unruled("judge: no JSON verdict in output (unparseable)", note)
    return _parse_verdicts(parsed, flat, pr), None, note


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
        esc = payload.get("escalated")
        if isinstance(esc, dict):
            declared = list(esc.items())
        elif isinstance(esc, list):
            declared = [(k, was) for k in esc]
        else:
            declared = []
            if esc is not None:
                b.problems.append(
                    f"baseline {path} has an `escalated` field that is neither an "
                    f"object nor a list ({type(esc).__name__}) — round {was}'s "
                    "escalations were NOT inherited, so a finding only a human can "
                    "close counts as work a fix round can clear again")
        for k, when in declared:
            if not _is_key(k):
                # A key the payload carries but nothing can match: it would sit in
                # the register forever, matching no finding, and the caller would
                # read the cycle's silence as the escalation being honoured.
                b.problems.append(
                    f"baseline {path} carries `{_key_gist(k)}` in its `escalated` "
                    "register, which is not the shape of a finding key — it was "
                    "NOT inherited")
                continue
            # The NORMALISED key, which is what `_is_key` judged and what a
            # finding's own key will equal — storing the raw one would put a
            # padded or upper-case spelling in the register, matching nothing.
            key = _key_norm(k)
            # The declaration round is the one auditable fact in a register the
            # loop otherwise takes on trust, so it is range-checked rather than
            # coerced. `bool` is excluded explicitly: it is an `int` subclass, so
            # `True` would otherwise be read as "declared in round 1". Out of
            # range falls back to the round of the payload carrying it — the same
            # answer a bare list gets, and never later than the truth.
            ok = isinstance(when, int) and not isinstance(when, bool) and 1 <= when <= was
            if not ok:
                b.problems.append(
                    f"baseline {path} dates escalation {key} to {when!r}, which is not "
                    f"a round of this cycle at or before {was} — read as round {was}, "
                    "so the round shown against it is this payload's, not the "
                    "declaration's")
            first = when if ok else was
            b.escalated[key] = min(first, b.escalated.get(key, first))
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
            b.head_sha, b.head_round = sha, was
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
    return b


def coverage_veto(reviewer_meta: dict[str, dict], judge_skip: str | None,
                  flagged: int, diff_chars: int) -> list[str]:
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
    would not parse — is about THIS run and still vetoes."""
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
                out.append(f"{name} could not assess: {gap}")
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
PREMISE_REPEATED_EXIT = 4

#: The register's shape, so a future one can be told from a hand-written file.
PREMISE_REGISTER_VERSION = 1


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
                "name, and nothing implements it yet; only `premise_repeated` brakes "
                "anything today")
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
        kept.append({"key": premise_key(text), "text": text,
                     "norm": _norm_title(text), "rounds": sorted(rounds),
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
                    findings: Iterable[str] = (), limit: int | None = None) -> dict:
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
    end a cycle on."""
    text = " ".join(str(text).split())
    keys = sorted({_key_norm(k) for k in findings if _is_key(k)})
    entry = find_premise(reg, text)
    if entry is None:
        entry = {"key": premise_key(text), "text": text, "norm": _norm_title(text),
                 "rounds": [], "findings": []}
        reg.setdefault("premises", []).append(entry)
    if round_no not in entry["rounds"]:
        entry["rounds"] = sorted([*entry["rounds"], round_no])
    entry["findings"] = sorted({*entry["findings"], *keys})
    occurrence = len(entry["rounds"])
    escalate = limit is not None and occurrence >= limit
    if escalate:
        reason = (f"premise declared {occurrence} time(s) — rounds "
                  f"{', '.join(str(r) for r in entry['rounds'])} — and the brake is set "
                  f"at {limit}: a human answers this premise, not another fix pass")
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


def premise_state(reg: dict, round_no: int, limit: int | None = None) -> dict:
    """What the cycle's declarations say, for `round_stop` and for the payload."""
    entries = reg.get("premises") or []
    repeated = [{"key": e["key"], "text": e["text"], "rounds": list(e["rounds"]),
                 "occurrences": len(e["rounds"]), "findings": list(e.get("findings") or [])}
                for e in entries
                if limit is not None and len(e.get("rounds") or []) >= limit]
    return {"limit": limit,
            "declared": len(entries),
            "repeated": repeated,
            "undeclared_rounds": undeclared_passes(reg, round_no)}


def premise_report(verdict: dict, register_path: str, notes: list[str],
                   problems: list[str]) -> str:
    """The one screen a fixer sees when it declares a premise. Plain text, because
    the reader is an agent about to decide whether to write a patch and the decision
    has to survive being read out of a Bash tool's stdout."""
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
        out += [
            "STOP — DO NOT WRITE THIS FIX.",
            verdict["reason"],
            "",
            f"This is fix pass {verdict['occurrence']} against one premise the previous "
            "round invalidated (#84). Escalate it instead (review-pr.md step 3a): write no "
            "patch for the findings it explains, fix everything else in the pass, and "
            "report the premise, what it explains and what removing it would cost.",
        ]
        if keys:
            out += ["", "The next round must not count them as work a fix pass can "
                        "clear:", f"    panel.py ... {keys}"]
        else:
            out += ["", "No --premise-for keys were given, so nothing here names the "
                        "findings to escalate — pass them, or map the finding IDs "
                        "yourself (panel-review-pr.md §4b) before the next round."]
    else:
        out.append(verdict["reason"])
    return "\n".join(out)


def declare(repo_name: str | None, premise: str, register_path: str,
            round_no: int, findings: list[str] | None = None,
            pr_number: int | None = None, json_out: bool = False) -> int:
    """`panel.py --premise` — #84's futility brake, evaluated where a fix is PROPOSED.

    No seats, no diff, no judge, no vendor call and no board record: it reads the
    repo's dial, reads the cycle's register, counts the occurrences of this premise
    and either records it and returns 0, or refuses the fix and returns
    :data:`PREMISE_REPEATED_EXIT`.

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
    reg, problems = load_premises(register_path, cfg.get("github") or "", pr_number)
    verdict = declare_premise(reg, premise, round_no, findings or [], limit)
    write_failed = write_payload(register_path, reg)
    if json_out:
        print(json.dumps({**verdict, "register": register_path, "notes": notes,
                          "problems": problems, "write_failed": write_failed},
                         indent=2))
    else:
        print(premise_report(verdict, register_path, notes, problems))
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
               fix_floor: str = NO_SEVERITY_FLOOR,
               premises: dict | None = None) -> dict:
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
    2. a P1/P2 still outstanding **at or above** ``fix_floor``, or a Sonar hard-gate
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
       are STILL outstanding, at any severity **at or above** ``fix_floor``
       -> go again. The fixer was told
       about them and they are still there, and ``/panel-review-pr``'s bar is
       every finding fixed, not every P1/P2. This used to only cost the stop its
       confidence, which ended the cycle with a judge-confirmed defect present and
       nothing acting on the veto that said so. Keys rather than a count so the
       escalation filter below can subtract the escalated ones: a count computed
       before this function sees it puts the jam straight back, filtered by
       whichever caller remembered to, which is not a rule but a convention with
       one participant;
    4. otherwise dry -> stop.

    The cap is what stops rule 3 running forever when two reviewers disagree
    about a P4 — the cycle ends either way, and a cap reached with work
    outstanding is recorded as such rather than as convergence.

    **THE TWO FLOORS (#165), and why there are two.** Both default to
    :data:`NO_SEVERITY_FLOOR`, so a caller that has not heard of them gets exactly
    the behaviour above; `panel.py` passes the repo's
    ``review_panel.round_trigger_floor`` and ``fix_severity_floor``.

    ``trigger_floor`` bounds rule 1 alone: a new finding below it is still counted,
    still reported and still in the payload, it simply does not by itself buy a
    panel, a fix pass and another panel. That rule is the one the measurement
    indicts. From round 2 the thing under review IS the previous round's fix, so
    rule 1's input is the loop's own output — 128 of 201 new findings across seven
    PRs were created by the fix pass immediately before them — and a termination
    test fed by its own output can only end on the cap, which is what all seven
    panels did.

    ``fix_floor`` bounds rules 2 and 3, and it has to or the other dial does
    nothing. A finding below the fix floor is one the fix round was never asked to
    clear, so it is outstanding every round by construction: rule 3's own
    justification — "the fixer was told about them and they are still there" — is
    simply false of it, and left unbounded it would go again until the cap on a P3
    nobody ever intended to fix. Rule 2 takes the same bound for the same reason,
    which matters only where the fix floor is ``P1``: at ``P2`` every P1/P2 is
    already at or above it and the filter is a no-op. Read the other way round, that
    is the honest scope of "``P4`` restores the old behaviour" — true of rules 1 and
    3, and vacuous for rule 2, whose bar is the hardcoded ``("P1", "P2")`` tuple, so
    a floor can only ever RAISE it and only ``P1`` moves it at all.

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
    - **A fix pass that declared no premise is reported as unescalatable**
      (``undeclared_rounds``), and costs the round nothing. #84 is explicit that an
      undeclared fix is unescalatable rather than inferred, and the reason it is
      said out loud is that a cycle nobody COULD have braked reads exactly like a
      cycle that did not need braking — silence would assert the second.

    Declarations never buy a round, only end one. A register is a claim by the
    agent that is about to write the fix, and the one thing #67's evidence says
    cannot be self-reported is whether the loop is making progress; letting it
    extend the loop would hand that agent the other lever too.

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
    for name, value in (("repeated", repeated), ("escalated", escalated)):
        if isinstance(value, str):
            raise TypeError(
                f"round_stop({name}=...) takes a COLLECTION of finding keys, not one "
                f"string ({_key_gist(value)!r}): a bare str iterates character by "
                f"character, so it matches no finding and says nothing — pass a list")
        if isinstance(value, int):
            raise TypeError(
                f"round_stop({name}=...) takes finding KEYS, not a count ({value!r}): "
                "the escalated ones are subtracted here, and a count computed by the "
                "caller cannot express that")
    held = frozenset(k for k in escalated if k)
    # The escalated keys THIS round actually saw. The register is a property of
    # the cycle and only grows; what is blocking is a property of the round, and
    # conflating them meant one stale or mistyped key made every later round of
    # the cycle non-confident forever — including rounds that were genuinely dry,
    # and including after a human had answered the premise and the code moved.
    # A permanently vetoed cycle is the "loud and wrong" a reader learns to
    # ignore, which is worse than the jam this whole rule closes.
    blocking = held & ({*new_keys} | {c.key for c in outstanding})
    # The work a fix round can actually clear, under names of their own. The
    # subtraction happens ONCE, before the rules, because every rule below asks
    # "is there work outstanding" and an escalated finding is precisely work the
    # cycle has been forbidden to do — but the parameters keep meaning what they
    # are called, so the cap message and anything else downstream that wants "what
    # the cycle still has to clear, escalations and all" can still say so.
    clearable_new = [k for k in new_keys if k not in held]
    clearable = [c for c in outstanding if c.key not in held]
    # Severity by key, off `outstanding` — which carries it for the same findings
    # `new_keys` and `repeated` name. Deriving it here rather than widening either
    # parameter keeps every existing caller's contract: they pass bare keys today,
    # and a key whose severity this cannot find is treated as ABOVE the floor (the
    # `SEVERITIES[0]` fallback), so an unrecognised key costs a round rather than
    # silently dropping a finding out of the loop.
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

    #: New findings that buy a round, and the ones that were raised and do not.
    triggering = [k for k in clearable_new if above(k, trigger_floor)]
    quiet_new = [k for k in clearable_new if not above(k, trigger_floor)]
    repeats = len({k for k in repeated
                   if k and k not in held and above(k, fix_floor)})
    # Rule 2. The hardcoded ``("P1", "P2")`` is what makes the exemption necessary
    # HERE as well as in `above`: without the first clause a P3 gate issue could not
    # be a blocker at all, however red the gate, so a still-open one had to fall
    # through to rule 3 and would be lost with it the moment `repeated` did not
    # carry the key (a round whose baseline could not be attributed, for one).
    blockers = [c for c in clearable
                if c.key in exempt
                or (c.severity in ("P1", "P2")
                    and severity_at_least(c.severity, fix_floor))]
    #: How many of them are gate issues rather than judged P1/P2s — the `reason`
    #: has to be true of what it counted, and "P1/P2 still outstanding" is not true
    #: of a P3 `python:S1128`.
    gated = sum(1 for c in blockers if c.key in exempt)
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
    elif blocking:
        # Not "dry": something WAS raised and is unanswered. A reader reconciling
        # "dry" against a PR carrying an open premise question would be told
        # something untrue about why the loop stopped.
        stop, reason = True, (f"nothing left that a fix round can clear — "
                              f"{len(blocking)} escalated finding(s) await a human")
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
    circling = list((premises or {}).get("repeated") or [])
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
    # #84. Unconditional rather than "only on a STOP", because `circling` forces the
    # stop a few lines above — there is no `go again` round this can fire on, and
    # writing the guard anyway would say there was.
    if circling:
        veto = [*veto, f"{len(circling)} premise(s) were declared more than once in "
                       "this cycle — the rounds have stopped being about different "
                       "things, and the next fix pass would be the third patch on one "
                       "assumption (#67, #84)"]
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
        # "Nothing left to find" is a claim; "the counter hit zero" is not the
        # same claim, and the difference is exactly what a reader of a clean
        # verdict needs to see.
        "confident": bool(stop and not capped and not veto and baseline_ok),
        "veto": veto,
        "round": round_no,
        "max_rounds": max_rounds,
        # The floors this verdict was reached under, and what they held back. A
        # consumer comparing two rounds' `stop` has to be able to see that the
        # answer changed because the policy did — and `new_below_trigger_floor`
        # is the count that would otherwise be invisible: those findings ARE in
        # the payload's buckets, and nothing else says they were new and did not
        # buy a round.
        "trigger_floor": trigger_floor,
        "fix_floor": fix_floor,
        "new_below_trigger_floor": sorted(quiet_new),
        # #84's register as this round read it, and ALWAYS present — an absent key
        # and "nothing was declared" are different claims, and a consumer that had
        # to tell them apart would be reading a payload's age rather than a cycle's
        # state. `undeclared_rounds` is the honest half: those fix passes could not
        # have been braked, whatever this round's stop says.
        "premises": premise_state({"premises": []}, round_no, None) if premises is None
        else {"limit": premises.get("limit"),
              "declared": premises.get("declared", 0),
              "repeated": circling,
              "undeclared_rounds": list(premises.get("undeclared_rounds") or [])},
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
    "_unmerged", "_judge_listing", "_parse_verdicts", "adjudicate",
    "REWORD_RATIO", "_TITLE_NOISE", "_stem", "_same_words",
    "Baseline", "_baseline_title", "_SHA_RE", "_mtime",
    "_positive_int", "_whole_pr_chars",
    "load_baseline", "coverage_veto", "round_stop",
    "ESCALATE_ON_DEFAULTS", "ESCALATE_ON_UNBUILT", "PREMISE_REPEATED_EXIT",
    "PREMISE_REGISTER_VERSION", "premise_repeat_limit", "premise_key",
    "same_premise", "new_premise_register", "load_premises", "find_premise",
    "declare_premise", "undeclared_passes", "premise_state",
    "premise_report", "declare",
]
