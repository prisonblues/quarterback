#!/usr/bin/env python3
"""Reviewer panel — multi-reviewer PR review with consensus synthesis.

Runs the enabled reviewers for a repo (per its .harness-rules) in parallel over a
PR's diff, then synthesises:
  - SonarQube  -> HARD gate (quality-gate pass/fail) + issues
  - Claude     -> SOFT findings (read-only; diff in, JSON out)
  - Codex      -> SOFT findings (different vendor)
  - Antigravity-> SOFT findings (third vendor; off unless a repo enables it)
No consensus gate: every finding is judged on its merits by a "master" reviewer.
A real defect flagged by only ONE reviewer (e.g. Codex) is still fixed — agreement
is a confidence signal, not a filter. Reviewers apply the full /review-pr bar
(correctness, security, tests, docs, related code, craft — P1–P4); only genuine
false positives are dropped, so style and polish findings are kept, not filtered.

The master also MERGES the duplicates, and that is the only place a merge happens.
Deduping upstream of it could only pick one reviewer's text and discard the rest,
so a better key made the loss worse: the observation only one reviewer made
survived precisely when the merge FAILED. The judge instead writes a synthesis and
every reviewer's own title and detail ride along beside it (`reported_by`), so
merging is additive, attribution is a field rather than an inference, and the fix
loop and the board consume one canonical record instead of re-deriving it.

Reviewers whose prerequisites are missing (codex CLI absent, SONAR* env unset)
are reported as SKIPPED, not failed — the panel still produces a report.

LLM replies are parsed leniently: a balanced-bracket scan (not a greedy regex)
pulls the JSON out of ``` fences or surrounding prose, as either the object
envelope reviewers now return or the bare findings array they used to; an
unparseable reply is retried once, then kept as a single markdown finding rather
than dropped — so malformed JSON degrades into one ungrouped finding, never a
crash or a silent loss. A reply holding SEVERAL JSON-shaped values is settled by
agreement, never by rank: the prompts' own examples are identified and dropped,
and what remains must say one thing or the reply is treated as unstructured.

That is deliberately the pessimistic rule. Choosing among candidates — by
position, by size, by which strings they carry — can silently swap a review for
an echo or file a finding no reviewer made, and both artefacts read exactly like
a clean round.

Reviewers also DECLARE their own coverage — what they could not assess, and which
of their findings need the fix re-read — and the panel measures what they cannot
observe (whether the diff they got was truncated). Those are observations, not
forecasts: asking a model "will another round be needed?" asks it to predict its
own future findings, and a reviewer that silently produced nothing would answer
"no" with complete confidence. Rounds are driven mechanically instead — --round
and --baseline say which findings no earlier round raised, and that plus severity
decides whether to go again; the declarations only stop a broken round being read
as a clean one. The cycle belongs to the CALLER (/panel-review-pr): a run given
none of --round/--baseline/--max-rounds is a single review and says nothing about
rounds, rather than promising a re-review nothing will run.

Default prints a report. Pass --post to also comment the summary on the PR, or
--json to emit the whole run as JSON on stdout instead (progress goes to stderr,
so the payload parses without a preamble to strip); --json-file writes that same
JSON *and* keeps the report.

Every run is recorded on the quarterback board (`qb record-review`, best-effort —
a down board never fails a review) so which model finds the real issues, and
whether a pricier tier earns its keep, accumulate into an answer instead of an
impression. --no-record opts out; a machine with no board configured no-ops.

Which reviewers run comes from the repo's .harness-rules; --reviewers overrides
that for one run, which is how you get a single-vendor read (--reviewers codex)
without editing config to get it.

--ask challenges ONE premise instead of reviewing a PR: the same seats, no diff,
no clustering and no judge — each answers holds / fails / cannot tell in one line
and the vote is the output. It exists because a round is the only thing that
currently reviews a fix, and a round is twenty minutes and thirty findings when
what was wanted was one answer to one question. PR #62 spent three of them
answering "did a review actually happen?", each round trusting a fresh proxy for
it (the exit code, then the push, then the payload artefact), every one of them a
yes/no question about one branch of this file. It is deliberately NOT a gate: a
point of order a fixer runs before committing, exiting 0 on every verdict, since
a required minute is a minute that gets skipped.

Usage:
    python3 ~/.claude/loops/panel.py --pr 734
    python3 ~/.claude/loops/panel.py --pr 734 --post
    python3 ~/.claude/loops/panel.py --pr 734 --json
    python3 ~/.claude/loops/panel.py --pr 734 --reviewers codex
    python3 ~/.claude/loops/panel.py --pr 734 --reviewers claude,codex,antigravity
    python3 ~/.claude/loops/panel.py --pr 734 --post --json-file "$rundir/panel.json"
    python3 ~/.claude/loops/panel.py --pr 734 --post --round 2 --max-rounds 2 \
        --baseline "$rundir/r1.json" --json-file "$rundir/r2.json"
    python3 ~/.claude/loops/panel.py \
        --ask "panel.py exits non-zero when it skips a PR on a title pattern" \
        --context harness/loops/panel.py:3500-3560

(`$rundir` being a `mktemp -d` of the caller's: a fixed /tmp path is a symlink
away from writing the payload somewhere else, and world-readable meanwhile.)
"""

from __future__ import annotations

# The imports, tunables, prompts and helpers this file used to carry live in
# panel_core (#129). Star-imported so every name this module already used stays
# a plain global here — which is also what keeps `monkeypatch.setattr(panel, …)`
# working for anything CALLED from this file. A helper called from inside
# panel_core resolves there instead, so tests driving one of those patch
# `panel_core` directly; the module docstring there says so.
from panel_core import *          # noqa: F401,F403
import panel_core                 # noqa: F401
from panel_core import (          # noqa: F401  — explicit, so a reader can grep
    sh, load_repo_cfg)            #   the two with the widest blast radius

# The seats moved to panel_seats (#129) — that section alone was over
# antigravity's argv cap, so the one seat whose prompt travels in argv could
# never read the file reviewing it. Imported back so `run()` below still calls
# `review_llm(...)` as a plain global, which is what keeps the suites' 
# `setattr(panel, "review_llm", …)` doubles working.
from panel_seats import *        # noqa: F401,F403
import panel_seats               # noqa: F401

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
               ci: str = ""
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

    if not shutil.which("claude"):
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
    args = ["claude", "-p"] + (["--model", model] if model else [])
    # The judge gets a sandbox of its own on the same reasoning as the reviewers,
    # and one sharper argument: it is the seat whose loss is worst (a judge that
    # dies takes every finding through `unjudged`), so it is the last place to
    # leave depending on the caller's shell.
    with tempfile.TemporaryDirectory(prefix="panel-judge-") as tmp:
        sandbox = member_sandbox(Path(tmp) / "cwd")
        out, err = panel_seats.run_cli(args, "judge", stdin_text=prompt, cwd=sandbox)
        if err:
            return unruled(err)
        parsed = extract_json_value(out, "verdicts")
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
                parsed = extract_json_value(out2, "verdicts")
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
        cut = any(m.get("truncated") for m in recorded)
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
        ran = payload.get("reviewers_ran")
        if isinstance(ran, list) and not ran:
            b.unread_rounds.add(was)
        elif recorded and not cut and str(payload.get("scope") or "pr") == "pr":
            reread.add(was)
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

    The one absence that is not an observation about the round is a reviewer
    whose CLI this box does not carry — see below."""
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
        if meta.get("truncated"):
            budget = meta.get("max_diff_chars") or 0
            out.append(f"{name} saw {budget:,} of {diff_chars:,} diff chars")
        if meta.get("unstructured"):
            out.append(f"{name} returned no structured reply — its coverage is unknown")
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
    if judge_skip:
        # Phrased for both halves of the judge's job: on a round with no findings
        # it is the coverage split that went unadjudicated, not the findings.
        out.append(f"the round was not adjudicated ({judge_skip})")
    if flagged:
        out.append(f"{flagged} finding(s) whose reporter said the FIX needs re-reading")
    return out


def round_stop(round_no: int, max_rounds: int, new_keys: list[str],
               outstanding: list[Canonical], veto: list[str],
               baseline_ok: bool = True, repeated: int = 0) -> dict:
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

    1. findings this round that no earlier round raised -> go again;
    2. a P1/P2 still outstanding -> go again, whatever anyone declared (a blocker
       raised again is a blocker that was not fixed);
    3. ``repeated`` — a finding an earlier round already raised that is STILL
       outstanding, at any severity -> go again. The fixer was told about it and
       it is still there, and ``/panel-review-pr``'s bar is every finding fixed,
       not every P1/P2. This used to only cost the stop its confidence, which
       ended the cycle with a judge-confirmed defect present and nothing acting on
       the veto that said so;
    4. otherwise dry -> stop.

    The cap is what stops rule 3 running forever when two reviewers disagree
    about a P4 — the cycle ends either way, and a cap reached with work
    outstanding is recorded as such rather than as convergence."""
    blockers = [c for c in outstanding if c.severity in ("P1", "P2")]
    if new_keys:
        stop, reason = False, (f"{len(new_keys)} finding(s) no earlier round raised")
    elif blockers:
        stop, reason = False, f"{len(blockers)} P1/P2 still outstanding after the fix"
    elif repeated:
        stop, reason = False, (f"{repeated} finding(s) an earlier round already raised "
                               "are still outstanding")
    else:
        stop, reason = True, ("dry — nothing raised that an earlier round had not"
                              if round_no > 1 else "dry — no findings to fix")
    capped = False
    if not stop and round_no >= max_rounds:
        stop, capped = True, True
        reason = f"round cap ({max_rounds}) reached — {reason}, unreviewed"
    # Only on a STOP. The veto list is printed under "why this round's quiet is
    # not evidence of a quiet PR", and on a `go again` round the repeat IS the
    # reason — printing it there told a reader that a round which was not quiet
    # had untrustworthy quiet. `confident` is unaffected: it already requires
    # `stop`.
    if repeated and stop:
        veto = [*veto, f"{repeated} finding(s) an earlier round already raised are "
                       "still outstanding — the fix for them did not land"]
    return {
        "stop": stop,
        "reason": reason,
        # "Nothing left to find" is a claim; "the counter hit zero" is not the
        # same claim, and the difference is exactly what a reader of a clean
        # verdict needs to see.
        "confident": bool(stop and not capped and not veto and baseline_ok),
        "veto": veto,
        "round": round_no,
        "max_rounds": max_rounds,
    }


# ----------------------------------------------------------------------------- run

def _changed_files(meta: dict) -> tuple[list[dict], int | None, int]:
    """The PR's touched paths, with each one's own share of ``changed_lines``.

    Returns ``(files, total, dropped)``. ``total`` is GitHub's own count of the
    PR's changed files, carried separately and NOT derived from ``len(files)``,
    because the two are allowed to disagree: `gh` pages the `files` connection
    and GitHub caps a PR's file list at 3,000. A consumer comparing them learns
    the list is partial; one told only ``len(files)`` reads a truncated list as a
    complete one, which is this repo's standing disease — a shortfall presenting
    as a clean result.

    **``total`` is None when GitHub did not state it, never ``len(files)``.** The
    first version of this fell back to the list's own length and called that
    "falling back to what we can prove" — but agreeing by construction is not
    proof, and when BOTH fields were missing it returned ``([], 0)``, turning an
    unknown file list into a *known empty* PR. That is the same absent-vs-zero
    collapse this release exists to prevent, committed by the code enforcing it.
    None travels to a NULL column that already means "nobody said".

    ``dropped`` counts entries discarded for having no usable path, so the caller
    can tell "GitHub paged us short" from "we discarded a malformed row" — two
    different facts with different fixes, and the partial-list warning is only
    about the first.

    Paths, not hunk ranges. Paths answer "will these two PRs collide", which is
    what #80 orders by; ranges would answer "and exactly where", which nothing
    asks yet. The per-file additions/deletions ride along because the same `gh`
    call already returns them, and they turn ``changed_lines`` from a bare total
    into something attributable to a file — as themselves, so a file GitHub
    stated nothing about stays distinguishable from a pure-deletion file.

    A rename is recorded under its DESTINATION path only: `gh pr view --json
    files` offers `path`, `additions`, `deletions` and `changeType` and no
    previous filename (verified — `previousFilename` is not an available field),
    so another PR still touching the old path is a collision this cannot see.
    The REST `pulls/{n}/files` endpoint does carry `previous_filename`; wiring it
    up is a second call and is deliberately left to whoever needs rename-grain.
    """
    files, dropped = [], 0
    for f in meta.get("files") or []:
        # Shape-checked, not assumed. A bare string, a number or a null in the
        # array raised AttributeError inside `run()` — after the PR read and
        # before any review — killing a run that had not started yet. The board
        # end of this same field coerces exactly these shapes into a droppable
        # row (`ChangedFileIn._coerce`), and the two ends of one field should not
        # disagree about what they tolerate.
        if not isinstance(f, dict) or not isinstance(f.get("path"), str):
            dropped += 1
            continue
        path = f["path"].strip()
        if not path:
            dropped += 1
            continue
        files.append({
            "path": path,
            # `.get` without `or 0`: 0 and "not stated" are different facts, and
            # the board's columns are nullable precisely to keep them apart.
            "additions": f.get("additions"),
            "deletions": f.get("deletions"),
        })
    files.sort(key=lambda f: f["path"])
    total = meta.get("changedFiles")
    if total is not None:
        try:
            total = int(total)
        except (TypeError, ValueError):
            # A shape `gh` has never returned. Degrade like everything else in
            # this neighbourhood rather than killing a review that has not run
            # yet with an uncaught TypeError from inside `run()`.
            total = None
    return files, total, dropped


def _payload_defaults() -> dict:
    """Every key a run payload carries, valued as "this run never got that far".

    One shape on every non-error exit, because the skip-pattern path emits a
    payload too: a consumer reading `payload['judged']` or `payload['run_key']`
    should not have to know which exit produced it. It used to be a hand-written
    literal of nine keys against this one's two dozen, so the skipped PR — the
    case that payload exists FOR — was the one that raised KeyError."""
    return {
        "changed_lines": 0,
        # The PR's file list, not this round's. Under #41 a later round reviews
        # only the increment, so the round's files narrow while the PR's
        # collision surface does not — and collision is what this is for.
        "changed_files": [],
        # None, not 0. This structure exists to describe a run that never got
        # that far, so the one value it must not assert is "this PR changed zero
        # files" — the release's whole distinction is NULL ("nobody said") versus
        # 0 ("counted, and it was none").
        "changed_files_total": None,
        # As of this PR's last panel, not live. Same currency as the file list,
        # and `ts` is what a reader judges staleness by.
        "pr_state": None,
        "is_draft": None,
        "reviewed": False,
        "skip_reason": None,
        # The commit this round reviewed. NOTHING else in the payload identifies
        # one — `base` holds a branch NAME — and two later readers need it: the
        # next round diffs against it to get the fix commit (an increment is
        # defined by the head its baseline read), and provenance uses it as the
        # far end of the range that says whether that fix pass INTRODUCED a
        # finding or MISSED it. Present on the skip path too — a skipped round
        # still moved the head, and a round 3 whose only baseline is a skipped
        # round 2 must still be able to find its anchor.
        "head_sha": None,
        # The other end of the range, and the two are NOT interchangeable (#98).
        #
        # `merge_base` is the PR's base commit: `gh pr diff` is the three-dot
        # diff, so a whole-PR round reads `merge_base...head` and nothing else in
        # the payload named that commit. It moves only when the PR merges its
        # base in or is rebased.
        #
        # It is the PR's anchor, NOT necessarily this round's target anchor.
        # Under #41's increment scope the target is `since_sha...head_sha` and
        # `merge_base` is where the tier-2 context is measured from instead. Read
        # `scope` before treating this as the left-hand side of what was
        # reviewed — the same warning `diff_chars` carries one field over.
        #
        # `base_sha` is the live tip of the base branch at review time — what the
        # PR would be merged INTO. It is the end that moves on its own, and the
        # only one a staleness check can be built on. Recording just the merge
        # base would produce a check that reports "unmoved" however far the base
        # ran away; see :func:`_base_tip_now` for the measurement behind that.
        #
        # Null on both means the panel did not say. Neither is ever derived from
        # the other.
        "merge_base": None,
        "base_sha": None,
        # What this round could not read in full, for the NEXT round's
        # `missed-unread` bucket. See :func:`_diff_files_cut`. Empty on a payload
        # whose `reviewed` is false means "no coverage at all", not "read
        # everything" — a skipped round never fetched a diff to name files from,
        # and the reader tells the two apart by `reviewed` (see `Baseline`).
        "unread_files": [],
        # Per-round tally of the buckets below, so a consumer gets the shape of a
        # round without walking every finding. Empty where the question does not
        # arise — outside a cycle, or in a cycle's round 1, which has no earlier
        # round to attribute against. All-zero is a different statement: a round
        # that could have attributed and had nothing to attribute.
        "provenance_counts": {},
        # Where a run sits in the panel -> fix -> panel cycle. Defaulted here too,
        # so the skipped PR answers `payload['round_stop']` with "no cycle ran"
        # rather than with a KeyError.
        "round": 1,
        "cycle": None,
        # What this round actually REVIEWED: "pr" (the whole diff) or "increment"
        # (the commits since `since_sha`, with the rest of the PR as context).
        # Recorded rather than inferred from the round number, because scope
        # falls back to "pr" whenever the anchor is missing or the fetch failed —
        # so "round 2" does not imply "increment", and a consumer comparing
        # `diff_chars` across rounds is comparing two different measurements
        # unless it reads this first.
        "scope": "pr",
        "since_sha": None,
        # Chars of PR context prepared ALONGSIDE the target under increment scope.
        # Separate from `diff_chars` (which is the target) because losing context
        # and losing the thing under review are not the same event: context is
        # the part a reviewer can lose and still know it lost it.
        "context_chars": 0,
        "prior_rounds": 0,
        "prior_findings": 0,
        "new_findings": 0,
        "new_finding_keys": [],
        "round_stop": None,
        "stop_reason": None,
        "coverage_note": None,
        "diff_truncated": False,
        "diff_chars": 0,
        "diff_budgets": {},
        "config_notes": [],
        "sonar_gate": "skipped",
        "ci_status": "unknown",
        "ci_failing": [],
        "judged": False,
        "judge_model": None,
        "judge_skip": None,
        "reviewers_ran": [],
        "reviewers": {},
        "reviewers_selected": [],
        "reviewers_override": None,
        "to_fix": [],
        "sonar_findings": [],
        "dismissed": [],
        "skipped": [],
    }


def _rounds_phrase(rounds: list[int]) -> str:
    """A list of round numbers as a noun phrase: ``round 1``, or ``rounds 1, 2``.

    These land in the veto list, which the operator is told to read as the reason
    a quiet round is not convergence — so it is one of the more closely-read lines
    the tool emits, and ``round 1, 2`` reads as a typo in it."""
    return f"round{'s' if len(rounds) > 1 else ''} {', '.join(str(r) for r in rounds)}"


def _veto_gist(text: str, limit: int = 80) -> str:
    """The identifying head of a veto that is also a config note — enough to say
    WHICH note without repeating its full text on the PR comment. The problem
    strings put their consequence after an em-dash, so the head is the fact."""
    head = text.split(" — ", 1)[0].strip()
    return head if len(head) <= limit else head[:limit - 1] + "…"


def write_payload(json_file: str, payload: dict) -> str:
    """Write a run payload where ``--json-file`` asked for it.

    Returns "" on success (or when nothing was asked for), else a description of
    the failure for :func:`finish` to fail the run with. Shared by every non-error
    exit, because the file is the NEXT round's baseline and a caller told "the
    round did not happen unless the panel wrote that file" must get that answer
    from the skip-pattern exit too.

    Opened ``O_NOFOLLOW``, so a pre-planted symlink at the requested path
    (``/tmp/panel-34-r1.json`` -> ``~/.ssh/authorized_keys``) fails the write
    instead of following it — the hazard ``panel-review-pr.md`` §3 warns about,
    enforced here rather than left to an instruction the caller may never read."""
    if not json_file:
        return ""
    try:
        fd = os.open(json_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(payload, indent=2))
    except OSError as e:
        failed = f"{json_file} ({e.__class__.__name__})"
        print(f"panel: could not write {failed}", file=sys.stderr)
        return failed
    return ""


#: Exit code for "the review ran, but the requested --json-file was not written".
#: Deliberately not 2: argparse exits 2 on its own usage errors, and the caller is
#: told a non-zero exit means the round did not happen for cycle purposes — which
#: it cannot tell from a mistyped flag if the two share a code.
UNWRITTEN_PAYLOAD_EXIT = 3


def finish(write_failed: str, code: int = 0) -> int:
    """The exit code, failing the run when the requested ``--json-file`` was not
    written.

    Without that file round r+1 classifies every repeated finding as new, prints
    "N of N raised by no earlier round" and drives a fix pass over work already
    done. Warning and exiting 0 let the caller advance the cycle on a baseline
    that does not exist.

    Reported at the END of a run rather than at the write: the report, the board
    record and the PR comment are a review that has already been paid for, and
    throwing them away would push the caller towards re-running the panel — which
    the workflow forbids, because each run is an observation and re-rolling one
    corrupts the record."""
    if write_failed:
        print(f"\npanel: FAILED — the requested --json-file was not written: "
              f"{write_failed}. The review above is complete, but the next round "
              "has no baseline: fix the path and re-run the CYCLE from round 1 "
              "rather than treating this round as done.", file=sys.stderr)
        return UNWRITTEN_PAYLOAD_EXIT
    return code


def fit_comment(report: str, limit: int = COMMENT_CHARS) -> str:
    """The report, cut to fit a GitHub comment (65,536 chars, hard).

    The per-reviewer accounts are the part that grows without bound — one block
    per reporter per merged finding — so they go first and the verdicts survive;
    a report still over the limit is cut with a marker. The terminal copy is
    never trimmed, and neither are `--json` or the board record: this is about
    what `--post` can physically send, and a review that succeeded must not be
    lost to a comment one account too long.

    The round verdict is the one block a cut is taken AROUND rather than through:
    it sits at the foot of the report, it is what the caller of a cycle acts on,
    and a truncation from the end would drop precisely it.

    Reserved, not exempt. The verdict block carries one veto line per reviewer per
    declared gap, from free text a model wrote, so it is unbounded in principle —
    and reserving all of it clamped the SLICE rather than the RESULT, returning
    `cut + tail` over the limit and losing the whole comment to a hard API
    rejection. When the block alone will not fit it is cut from its own end, which
    keeps the mechanical verdict (its first line) and drops the vetoes."""
    if len(report) <= limit:
        return report
    note = ("\n\n_Per-reviewer accounts omitted — the full report exceeds GitHub's "
            "comment limit. They are intact in `--json` and on the board._")
    trimmed = "\n".join(ln for ln in report.splitlines() if not ln.startswith("  - _"))
    if len(trimmed) + len(note) <= limit:
        return trimmed + note
    cut = ("\n\n_…report truncated at GitHub's comment limit. The full run is in "
           "`--json` and on the board._")
    if limit <= len(cut):
        # No room for even the marker: the caller asked for a length no honest
        # report fits in, so give it the report's own first characters.
        return trimmed[:max(0, limit)]
    head, sep, tail = trimmed.partition("\n\n" + ROUNDS_HEADING)
    tail = sep + tail if sep else ""
    room = limit - len(cut)
    if len(tail) > room:
        tail = tail[:room - 1] + "…"
    return head[:room - len(tail)] + cut + tail


def run(repo_name: str | None, pr_number: int, post: bool, json_out: bool = False,
        reviewers: str | None = None, json_file: str = "", record: bool = True,
        round_no: int = 1, baseline: list[str] | None = None,
        max_rounds: int | None = None, scope: str = "auto",
        since: str = "") -> int:
    # A cycle is something the CALLER drives, and only /panel-review-pr does:
    # naming a cap (or a round, or a baseline) is what says this run is part of
    # one. A review-only /panel run left to the default is a single pass, and
    # must not report itself as "round 1 of at most 2 — go again", promising a
    # re-review nothing will run.
    in_cycle = max_rounds is not None or round_no > 1 or bool(baseline)
    cap = DEFAULT_MAX_ROUNDS if max_rounds is None else max_rounds
    # Idempotency key for the board record, minted once per process so a retry of
    # the POST cannot double-count the run into the stats. A fresh panel run is a
    # genuinely new observation and gets a new key — re-reviewing a PR after a fix
    # loop is data, not a duplicate.
    run_key = uuid.uuid4().hex
    cfg = load_repo_cfg(repo_name)
    # The name RESOLVED from the checkout, never the argument. `--repo` is
    # optional — `panel.py --pr N` in a repo is the documented single-PR form —
    # and the unresolved None went straight into the payload as `"repo": null`.
    # A payload that does not say which review it is from cannot be a baseline:
    # round 2 discarded round 1 as unattributable, called every finding new, and
    # could never record a confident stop. The whole round diff no-opped for
    # anyone who did not pass a flag they were never told to pass.
    repo_name = cfg.get("name") or repo_name
    gh_repo = cfg["github"]
    rev = cfg["reviewers"]
    panel = cfg["review_panel"]
    # Resolved before anything is fetched, so a typo'd --reviewers fails on the
    # spot rather than after a PR read and a diff download.
    selected, override_note = select_reviewers(rev, reviewers)

    try:
        meta = json.loads(panel_core.sh(["gh", "pr", "view", str(pr_number), "--repo", gh_repo,
                              "--json", "title,additions,deletions,baseRefName,"
                                        "baseRefOid,headRefName,headRefOid,files,"
                                        "changedFiles,state,isDraft"]))
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()
        # `gh pr view --json` rejects the WHOLE command on a field it does not
        # know ("Unknown JSON field: …", exit 1) rather than omitting it — which
        # is why no absent-field fallback lives in `_changed_files` any more: on
        # a `gh` too old for `files`/`changedFiles`/`state` the run dies here, and
        # the branch that claimed to handle it could never have run. Say so, since
        # "cannot read PR" reads like a network or permissions problem.
        sys.exit(f"panel: cannot read PR #{pr_number} in {gh_repo}"
                 + (f" — {tail[-1][:160]}" if tail else "")
                 + ("\n  (a `gh` predating --json files/changedFiles/state fails the whole "
                    "call on an unknown field; panel needs gh >= 2.40)"
                    if tail and "Unknown JSON field" in tail[-1] else ""))
    title, base = meta["title"], meta["baseRefName"]
    # The commit under review. Already fetched for the Sonar staleness check; it
    # is carried into the payload now because the next round needs it to tell a
    # defect its own fix pass created from one this round simply missed.
    head_sha = meta["headRefOid"]
    # The base end of the same range (#98). `baseRefOid` is the MERGE BASE — the
    # commit `gh pr diff`'s three-dot diff is built from — and not the base
    # branch's tip, which is why the tip is fetched separately below rather than
    # read off this call. `.get`, not `[...]`: every other key here is required
    # because the run cannot proceed without it, and a base commit is not that.
    merge_base = meta.get("baseRefOid") or None
    changed = meta["additions"] + meta["deletions"]
    # Same call that already produced `changed`, three fields wider — so the board
    # gets the paths behind the number, and the PR's state, without a second
    # round-trip, and gets them on the skip path too, where no diff is ever
    # fetched. The state is as of THIS panel: the board is told about panels, not
    # about merges, which is what the payload's timestamp is for.
    changed_files, changed_files_total, dropped_files = _changed_files(meta)
    pr_state, is_draft = meta.get("state"), meta.get("isDraft")

    # Built BEFORE the skip branch, because the skip branch returns. It used to
    # sit with the diff budgets forty lines below, so a skipped PR carrying two
    # paths and a total of 3,000 said nothing at all — and the skip path is the
    # one this release argues is most likely to be merged unattended, which makes
    # it the worst possible place for the warning to go missing.
    notes: list[str] = []
    # `dropped` is excluded on purpose: a discarded malformed row is not GitHub
    # paging us short, and one note covering both would send a reader looking for
    # a truncation that never happened.
    listed = len(changed_files) + dropped_files
    if changed_files_total is not None and listed < changed_files_total:
        notes.append(f"the PR's file list came back partial — {listed:,} of "
                     f"{changed_files_total:,} changed files; collision queries "
                     "against this run will under-report")
    if dropped_files:
        notes.append(f"{dropped_files:,} file entr{'y' if dropped_files == 1 else 'ies'} "
                     "had no usable path and were dropped")

    # Progress goes to stderr in --json mode, so stdout is the payload and only
    # the payload: it is a machine-readable artifact, and a consumer that has to
    # strip a two-line preamble before parsing is one preamble away from breaking.
    chatter = sys.stderr if json_out else sys.stdout

    # Title-pattern skip (merges/promotes/format-the-world — not worth LLM review)
    for pat in panel.get("skip_title_patterns", []):
        if re.search(pat, title, re.I):
            print(f"[{repo_name}#{pr_number}] '{title[:50]}' matches skip pattern "
                  f"/{pat}/ — not worth panel review. Skipping.", file=chatter)
            # A consumer gets a payload on every non-error exit, or "reviewed
            # and found nothing" and "never reviewed at all" arrive as the
            # same empty stdout — and the second one silently reads as a
            # clean PR. Same SHAPE as a reviewed run, too, so reading any
            # other key of it is not a KeyError. Not recorded on the board:
            # no review happened.
            #
            # It says WHICH round it is and which cycle that round belongs to,
            # because the caller is told to feed every round's --json-file
            # forward as the next round's --baseline. Left on the defaults it
            # serialised a skipped round 2 as round 1 with a fresh id, which then
            # collided with the real round 1 over the round number and renamed
            # the cycle out from under every later round.
            skip_prior = load_baseline(baseline or [],
                                       {"repo": repo_name, "github": gh_repo,
                                        "pr": pr_number, "round": round_no})
            skipped_payload = {
                **_payload_defaults(),
                "repo": repo_name, "github": gh_repo, "pr": pr_number,
                "title": title, "base": base,
                # A skipped round still moved the head, and both of the next
                # round's readers have to start somewhere: its increment anchors
                # here, and its fix range runs from here. Left null, a skip
                # anywhere in a cycle loses the anchor entirely (round 3 silently
                # re-reads the whole PR) and blinds provenance for the round
                # after it.
                "head_sha": head_sha,
                # Free off the metadata this path already fetched. `base_sha` is
                # NOT here and is left at its null default: reading the base
                # branch's tip is a second API call, and this path is the one
                # that exists to cost nothing and is never recorded on the board.
                "merge_base": merge_base,
                # Zeroed rather than left `{}` when there ARE earlier rounds:
                # `{}` is the shape for a round where the question does not arise,
                # and a skipped round 3 of a cycle is not that — it attributed
                # nothing because it reviewed nothing, which a consumer must be
                # able to tell from "not a cycle run".
                "provenance_counts": ({b: 0 for b in PROVENANCE}
                                      if skip_prior.rounds else {}),
                # A skipped PR still collides with everything it touches, and it
                # is the case most likely to be re-merged unattended. The paths
                # are already in hand here — the diff never is.
                "changed_lines": changed,
                "changed_files": changed_files,
                "changed_files_total": changed_files_total,
                "pr_state": pr_state,
                "is_draft": is_draft,
                # The file-list warnings, built above this branch for exactly this
                # reason, plus any baseline problem — a baseline this run could not
                # read is a fact about the cycle, not about the review it skipped,
                # so it travels rather than being dropped on the floor.
                "config_notes": notes + skip_prior.problems,
                "round": round_no,
                "cycle": skip_prior.cycle,
                "prior_rounds": len(skip_prior.rounds),
                "prior_findings": len(skip_prior.keys),
                "skip_reason": f"title matches skip pattern /{pat}/",
                "run_key": run_key,
            }
            # --json-file is honoured here too, and its failure fails the run the
            # same way. The caller is told "if the panel could not write that file
            # the round did not happen", and it then feeds the file to the next
            # round as `--baseline`: a skipped PR that exited 0 leaving no file
            # gave that caller no signal at all.
            failed = write_payload(json_file, skipped_payload)
            if json_out:
                print(json.dumps(skipped_payload, indent=2))
            return finish(failed)
    print(f"\n[{repo_name}#{pr_number}] {title[:60]}", file=chatter)
    print(f"  base={base}  changed={changed} lines\n", file=chatter)

    # The head is read BEFORE the diff, and the order is load-bearing. The two are
    # separate requests, so a push that lands between them makes them disagree —
    # and this way round the recorded head is the OLDER of the two, so the next
    # round's increment starts at or before the last commit this round read. It
    # re-reads a little; it cannot skip anything. Read after the diff, the same
    # race would record a head ahead of what was reviewed, and the next round's
    # increment would begin after code no round had seen.
    try:
        diff = panel_core.sh(["gh", "pr", "diff", str(pr_number), "--repo", gh_repo])
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()
        sys.exit(f"panel: cannot fetch diff for PR #{pr_number} in {gh_repo}"
                 + (f" — {tail[-1][:160]}" if tail else ""))
    changed_lines = _diff_added_lines(diff)

    # ---- what this round REVIEWS. Round 1 reads the PR; a later round reads what
    # the fixer wrote since the last round read it, with the PR behind it as
    # context. Decided here, before budgets, because scope is what the budgets are
    # then spent on.
    prior = load_baseline(baseline or [],
                          {"repo": repo_name, "github": gh_repo, "pr": pr_number,
                           "round": round_no})
    want_scope = resolve_round_scope(scope, panel, notes)
    # `--since` wins over the baseline, and it is checked here rather than trusted:
    # the anchor is interpolated into a REST path, and a value carrying `..` or a
    # query string addresses a different endpoint — a fetch error where one of the
    # explained fallbacks belongs.
    if since and not _is_ref(since):
        notes.append(f"--since {since!r} is not a commit or a ref — it was ignored")
        since = ""
    anchor = since or prior.head_sha or ""
    review, scope_notes = ReviewScope.decide(
        want_scope, round_no, diff, (anchor, head_sha), gh_repo, base,
        None if since else prior.head_round)
    notes.extend(scope_notes)

    # Diff budgets: panel-wide value, then each model's own override. Every
    # reviewer used to get the same 60k prefix regardless of its context window.
    # `notes` is already populated — the file-list warnings are built above the
    # skip branch so a skipped run carries them too. Deliberately NOT re-initialised
    # here: this line used to be `notes: list[str] = []`, and keeping it through the
    # merge with #82 would have silently discarded every file-list warning built
    # between there and here.
    #
    # Time-of-check/time-of-use: `headRefOid` was read from the PR metadata BEFORE
    # the diff was fetched, so a push landing in between leaves the payload naming
    # one commit while the reviewers read another — and the next round then
    # attributes its findings to a range that never produced the diff anyone
    # reviewed. Re-read straight after the diff fetch above, which NARROWS that
    # window but does not close it: the push could have landed either side of the
    # fetch, and nothing here can tell which. So the note reports the move rather
    # than claiming which commit produced the diff. The later commit is recorded
    # because it is the one the next round's fix range has to start from — and
    # "could not tell" (a None) leaves the earlier answer standing.
    moved_to = _head_sha_now(gh_repo, pr_number)
    if moved_to and moved_to != head_sha:
        notes.append(f"the PR head moved from {head_sha[:8]} to {moved_to[:8]} while this "
                     "round was running — the diff was fetched somewhere in that window and "
                     "which of the two produced it cannot be told from here; the later commit "
                     "is recorded, and provenance against this round is that much less certain")
        head_sha = moved_to
        # `merge_base` came off the metadata read before the round started, and
        # GitHub recomputes `baseRefOid` on every push to the head branch. Leaving
        # it would pair a re-stamped right end with a left end computed for the
        # commit it replaced — and the pair being replayable is this release's
        # whole claim. Worse, the common reason a head moves here is a merge of
        # the base branch INTO the PR (~1.8 integration merges per PR landed on
        # this repo, #80), which is exactly the case that moves the merge base:
        # the stored range would then start before an integration merge its right
        # end contains, and it is a range no round ever reviewed.
        #
        # One extra call, on a path that fires rarely, and only when something has
        # already gone irregular. If it fails, the pair is not silently mismatched
        # — the note says which end is stale, because "unknown" and "stale" want
        # different treatment from whatever reads this later.
        moved_meta = _merge_base_now(gh_repo, pr_number)
        if moved_meta and moved_meta != merge_base:
            notes.append(f"the merge base moved with it, from {merge_base[:8] if merge_base else '?'} "
                         f"to {moved_meta[:8]} — both ends are re-read, so the recorded range is "
                         "the one that exists now rather than a pair straddling the push")
            merge_base = moved_meta
        elif moved_meta is None:
            # DROP it rather than keep the earlier head's answer (128-F12). Keeping
            # it stores a merge_base/head_sha pair that no programmatic consumer can
            # tell from a good one — the prose note below is not readable by #96,
            # which is the consumer this release exists to serve, and a
            # plausible-but-wrong range is worse than an absent one: the first is
            # acted on, the second is noticed. The board already treats a commit id
            # it will not store as null-plus-an-echo rather than as a best guess
            # (`merge_base_dropped`, app/api/reviews.py), and this is the same
            # judgement one layer up.
            #
            # Null is expressible today and costs no schema change, which is why it
            # is this rather than a new `merge_base_stale` field: adding a payload
            # field means adding it board-side too or it is silently dropped on
            # ingest (#93, #65), and that is a migration this round cannot carry.
            if merge_base is None:
                # Nothing was ever recorded, so "the one computed for the EARLIER
                # head" would name a commit that never existed (128-F11).
                notes.append("the merge base could not be read before or after the head moved, "
                             "so this round records neither end of its base — a later staleness "
                             "check has nothing to anchor against")
            else:
                notes.append(f"the merge base could not be re-read after the head moved, so the "
                             f"base end computed for the earlier head ({merge_base[:8]}) is "
                             "DROPPED rather than paired with the new head — the range is "
                             "recorded as unknown, not as one that was never reviewed")
            merge_base = None
    # The base end (#98), read here rather than above the skip branch: this is one
    # more API round trip and the skip path exists to be cheap, never fetches a
    # diff, and never reaches the board — so a base tip recorded there would have
    # no consumer to be worth the call. A skipped payload keeps `merge_base`,
    # which is free off the metadata already fetched, and leaves this null.
    #
    # No note when the two differ. `base_sha != merge_base` is the ordinary state
    # of every PR whose base gained a commit after it forked, so a warning there
    # would fire on almost every run and be trained away — the same reasoning
    # `unread_files` records for not warning about its own dedup. What the base's
    # movement MEANS is a verdict, and the verdict belongs to #96.
    base_sha = _base_tip_now(gh_repo, base)
    if base_sha is None:
        notes.append(f"the tip of base branch '{base}' could not be read, so this round "
                     "records what its diff was built from and not what the PR would be "
                     "merged into — a later staleness check has one end of the range only")
    panel_budget = diff_budget(panel, "max_diff_chars", DEFAULT_DIFF_BUDGET, notes)
    # Only for the reviewers actually running: a budget warning about a model
    # this run never asked for is noise, and a "truncated for antigravity" footnote
    # under a claude-only panel is a lie.
    budgets = {name: diff_budget(rev.get(name, {}), "max_diff_chars", panel_budget, notes)
               for name in LLM_REVIEWERS if name in selected}
    judge_budget = diff_budget(panel, "judge_max_diff_chars", panel_budget, notes)

    # Read BEFORE the seats are dispatched, because its result now travels in
    # their prompt (#91). It used to run concurrently with them and be collected
    # afterwards, which is why the panel could compute CI on every run and still
    # tell no reviewer about it. One `gh pr checks` against a round that takes
    # minutes is a couple of seconds of wall-clock for a fact that refutes a whole
    # class of finding, so the overlap is not worth keeping.
    ci_status, ci_failing, ci_skip = review_ci(gh_repo, pr_number)
    if ci_skip:
        result.skipped.append(ci_skip)
    ci_text = ci_brief(ci_status, ci_failing, ci_skip)

    def prompt_for(budget: int | None) -> str:
        return REVIEW_PROMPT.format(n=pr_number, repo=gh_repo, base=base,
                                    ci=ci_text, diff=review.material(budget)[0])

    # `agy` is the only reviewer whose prompt must travel in argv, so it is the
    # only one the kernel can veto. Clamp it to what execve will carry and say
    # so — the alternative, honouring the number and dying at exec, is how a
    # panel came to report "LLM reviewers ran: none" as a clean review.
    #
    # It is also the only seat an UNCAPPED budget can still cut, which is why the
    # clamp starts from the material's own length when there is no budget: "no
    # cap" means "as much as this machine can hand over", and on this one seat
    # that is a smaller number than on the others. The note says so in chars of
    # the material rather than in config terms, since there is no config value to
    # blame.
    #
    # `sendable` is that length — everything this round would hand a reviewer,
    # target and context together, which under increment scope is not the PR's
    # length. Starting from the PR's would tell antigravity it had been cut on a
    # round whose material fits whole.
    sendable = len(review.target) + len(review.near) + len(review.far)
    if "antigravity" in budgets:
        asked = budgets["antigravity"]
        fitted = fit_argv_budget(prompt_for, sendable if asked is None else asked)
        if fitted < (sendable if asked is None else asked):
            notes.append(
                f"antigravity gets {fitted:,} of {sendable:,} diff chars — its prompt "
                f"travels in argv and the kernel caps one element at "
                f"{ARGV_PROMPT_MAX_BYTES:,} bytes. It is the only reviewer with no way "
                "to read a prompt off stdin.")
            budgets["antigravity"] = fitted

    # Truncation is measured against the review TARGET, not against everything
    # sent. Under increment scope losing context is the design — that is what the
    # priority order in `ReviewScope.material` is for, and a reviewer short of
    # context is told so in the prompt and can declare it. Losing the target is
    # the thing that must never pass silently, because a reviewer handed a prefix
    # of the thing it is reviewing cannot see what it was not given. Counting a
    # trimmed context tier here would make `truncated` fire on almost every
    # increment round and stop meaning anything on the round where it matters.
    #
    # Measured by COMPOSING each reviewer's material rather than by comparing its
    # budget against the target's length. The two are not the same number under
    # increment scope: the budget also has to pay for the brief and the section
    # headers, so a budget a little over the target's size still cuts it, and a
    # comparison against the raw budget would report that as untruncated.
    composed = {n: review.material(b) for n, b in budgets.items()}
    sent = {n: composed[n][1:] for n in composed}
    truncated_for = {n: b for n, b in budgets.items()
                     if sent[n][0] < len(review.target)}
    truncated = bool(truncated_for)
    # A budget below the scoped frame's OWN size cannot be honoured. The brief and
    # the section headers are over a kilobyte and they are what makes the target
    # legible as the target; cutting them to fit would hand the reviewer an
    # unlabelled increment, which is worse than overshooting. So the prompt runs
    # over — and says so here, because "the budget buys the whole PROMPT" is the
    # contract everywhere else, and a silent overshoot in exactly the regime where
    # a small budget was set to protect a small context window is the one place
    # that contract has to be visible when it cannot be kept.
    over = [(n, len(composed[n][0]), b) for n, b in sorted(budgets.items())
            if b is not None and len(composed[n][0]) > b]
    if review.scope == "increment" and over:
        notes.append(
            "the scoped prompt does not fit the budget it was given for "
            + ", ".join(f"{n} ({got:,} chars against {b:,})" for n, got, b in over)
            + " — a scoped prompt's brief and section headers are over a kilobyte and "
              "cannot be cut, so a budget below them buys no diff at all and is still "
              "exceeded")
    # Context loss is still REPORTED, just not as truncation. Without this the
    # saving is invisible in one direction and so is its cost: nothing else in
    # the payload distinguishes "the whole PR fitted behind the increment" from
    # "the increment used the entire budget and the reviewer saw no context".
    short_context = sorted(n for n in budgets
                           if n not in truncated_for
                           and sent[n][1] < len(review.near) + len(review.far))
    if review.scope == "increment" and short_context:
        notes.append(
            f"{', '.join(short_context)} got the whole target and only part of the PR "
            f"context ({sendable:,} chars of material, budget cut it) — expect "
            "`could_not_assess` entries about code outside the fix commit")
    # Increment scope always shrinks the review TARGET; it does not always shrink
    # the bill. The near tier is the anchor-era version of every file the fix
    # touched, so a fix spread across the files that carry most of the PR leaves
    # little to leave out. That is the price of reading the seam properly and it
    # is worth paying — but it is a cost, and an uncapped run should not discover
    # it from an invoice.
    #
    # The condition MEASURES that ("the near tier is most of the context") rather
    # than restating the arithmetic. It used to be `sendable > len(diff)`, which
    # was true on every scoped round — near and far were then a partition of the
    # whole PR, so the material was always the PR plus the target — and it fired
    # on a one-file fix in a fifty-file PR with a reason that was plainly false of
    # it.
    if review.scope == "increment" and len(review.near) > len(review.far):
        notes.append(
            f"scoping this round cut the review target to {len(review.target):,} chars "
            f"from the PR's {len(diff):,}, but the fix touches the files that carry most "
            f"of it: {len(review.near):,} of {len(review.near) + len(review.far):,} "
            f"context chars are the near tier, and {sendable:,} chars go out in all. The "
            "reviewer's attention is narrower; the token bill is not")

    result = PanelResult()
    # Resolved ONCE, so the label in the report cannot drift from the model that
    # actually ran (the fallbacks live here, not in two places). Effort is a
    # knob codex, pi and antigravity share (spelled differently on each CLI, and
    # over a different scale — see EFFORTS); claude takes its own default reasoning.
    models = {n: rev.get(n, {}).get("model", SEAT_MODEL_DEFAULTS.get(n, ""))
              for n in LLM_REVIEWERS}
    efforts = {n: rev.get(n, {}).get("effort", "") for n in EFFORTS}
    labels = {n: reviewer_label(n, models[n], efforts.get(n, "")) for n in LLM_REVIEWERS}

    tasks = {}
    with ThreadPoolExecutor(max_workers=len(ALL_REVIEWERS) + 1) as ex:
        # Every selected LLM reviewer runs — no de-minimis gate. If we asked for
        # the panel, we want each vendor's eyes regardless of diff size.
        for name in LLM_REVIEWERS:
            if name in selected:
                tasks[name] = ex.submit(review_llm, name, models[name],
                                        prompt_for(budgets[name]), efforts.get(name, ""))
        sonar_future = None
        sonar_filed = False
        if "sonarqube" in selected:
            sonar_future = ex.submit(
                review_sonarqube, rev.get("sonarqube", {}),
                {"number": pr_number, "base": base,
                 "head": meta["headRefName"], "head_sha": meta["headRefOid"]},
                changed_lines, cfg["path"])

        llm_findings: list[Finding] = []
        ran_llm: list[str] = []
        # The same seats as `ran_llm`, under their BARE names. `ran_llm` holds
        # display labels (`claude (opus)`) for the report, and everything keyed by
        # reviewer — `budgets`, `models`, `efforts` — is keyed by the bare name.
        # Looking one up with the other returns None for every seat, silently.
        ran_names: list[str] = []
        llm_skipped: list[str] = []
        # Which brain each member actually used. Findings carry the bare vendor
        # name for attribution, which is the right grain for a report and the
        # wrong one for a record: "codex found 9 issues" means nothing six weeks
        # later without the model and effort behind it, and those drift (a repo
        # repins, a slug retires, --reviewers hand-picks a set).
        reviewer_meta: dict[str, dict] = {}
        for name, fut in tasks.items():
            got = fut.result()
            reviewer_meta[name] = {
                "model": models[name] or None,
                "effort": efforts.get(name) or None,
                "ran": not got.skip,
                "skip": got.skip,
                "max_diff_chars": budgets[name],
                # The mechanical half of "did this reviewer see the whole thing":
                # checked against the budget rather than asked for, because the
                # one thing a truncated reviewer cannot notice is the truncation.
                "truncated": name in truncated_for,
                "duration_ms": got.duration_ms,
                "could_not_assess": got.could_not_assess,
                "unstructured": got.unstructured,
                # A fact about the HOST rather than about the round — see
                # coverage_veto, which is the one consumer that treats it
                # differently from every other way of not running.
                "absent": got.absent,
                # Spread, not nested: a member whose usage could not be read
                # contributes no keys at all, so the board stores nulls and
                # renders "not recorded" — rather than a zero it would average in
                # as though the reviewer had cost nothing.
                **(got.usage or {}),
            }
            if got.skip:
                result.skipped.append(got.skip)
                llm_skipped.append(got.skip)
            else:
                ran_llm.append(labels[name])
                ran_names.append(name)
                llm_findings.extend(got.findings)
        if sonar_future:
            gate, hard, soft, skip = sonar_future.result()
            result.sonar_gate = gate
            reviewer_meta["sonarqube"] = {"ran": gate != "skipped", "skip": skip}
            # The 4th value is a skip reason ONLY when the reviewer didn't run
            # (gate == "skipped"); otherwise it's a caveat about a run that DID
            # produce findings — a degraded base branch, files it couldn't read.
            # Branching on its mere presence would drop those findings on the
            # floor and report the reviewer as skipped, which is the silent
            # zero this whole path exists to avoid.
            if skip:
                result.skipped.append(skip)
            if gate != "skipped":
                # PR-scanned issues are the hard gate; base-branch fallback
                # issues are soft — judged on merits alongside the LLM reviewers.
                result.sonar_findings = hard
                llm_findings.extend(soft)
                # Recorded HERE, where it is a fact rather than an inference: the
                # consensus count below needs to know whether sonarqube put
                # anything into the population the judge clusters, and only this
                # branch can. See `filers`.
                sonar_filed = bool(soft)

    # Pre-cluster as a hint, then let the master MERGE the duplicates and rule on
    # each issue in one step (no consensus gate). Dedup cannot happen upstream of
    # the judge without discarding what the other reviewers said — see adjudicate.
    clusters = cluster_findings(llm_findings)
    coverage = {n: m.get("could_not_assess") or [] for n, m in reviewer_meta.items()}
    # The judge reads the same material the reviewers did, composed the same way.
    # Handing it the whole PR while the panel reviewed an increment would put the
    # adjudicator and the parties in front of different evidence — it would rule
    # "not in the diff" on a finding whose diff it was looking at a different
    # version of, and it would do so with the authority of the final call.
    # `budget=None`, because the material arrives already fitted: composing to the
    # judge's budget and THEN cutting to it again would trim the tail a second
    # time, through the "[cut: …]" marker that says how much is missing. Nothing
    # else is lost by not passing it — `adjudicate` only ever used `budget` to
    # slice the diff it was handed.
    #
    # What the judge was actually given is kept, not discarded: `judge_budget` can
    # cut the judge's copy at a different point from every reviewer's, and until
    # this was measured nothing in the round reported or vetoed on it. A judge
    # short of the material dismisses a finding whose evidence sat in the part it
    # did not get, and the round records that as convergence.
    judge_text, judge_target, judge_context = review.judge_material(judge_budget)
    judge_gaps: list[str] = []
    if judge_target < len(review.target):
        judge_gaps.append(
            f"the judge ruled on {judge_target:,} of the review target's "
            f"{len(review.target):,} chars (its budget is {judge_budget:,}) — a finding "
            "about the part it did not get could only be dismissed as unsupported")
    elif judge_context < len(review.near) + len(review.far):
        judge_gaps.append(
            f"the judge saw {judge_context:,} of the "
            f"{len(review.near) + len(review.far):,} chars of context the panel was "
            f"offered (its budget is {judge_budget:,}) — it ruled on findings about code "
            "it was shown less of than the reviewers were")
    notes.extend(judge_gaps)
    findings, judge_skip, coverage_note = adjudicate(
        clusters, judge_text, panel.get("judge_model", ""), pr_number, None, coverage,
        ci=ci_text)
    judged = judge_skip is None and bool(findings)
    to_fix = sorted((c for c in findings if c.verdict != "dismissed"),
                    key=lambda c: c.severity)
    dismissed = [c for c in findings if c.verdict == "dismissed"]
    # Sonar's hard-gate issues never reach the judge, so each is a canonical
    # record of its own single account — numbered after the judged ones, since
    # `related` is resolved against ids that must be unique across the payload.
    sonar = [Canonical(id=_finding_id(pr_number, len(findings) + i + 1),
                       severity=f.severity, file=f.file, line=f.line,
                       synthesis=f.title, verdict="sonar", reported_by=[f],
                       rationale=f.detail)          # the Sonar rule that fired
             for i, f in enumerate(result.sonar_findings)]

    # ---- this round against the ones before it. Mechanical: which findings are
    # ones no earlier round raised, and does that make the loop done?
    # `prior` was loaded before the budgets: which commit the last round reviewed
    # is what decides this round's SCOPE, and scope decides what the budgets are
    # spent on. Loading it twice would also double every `problems` entry into
    # `notes`.
    prior_keys, prior_rounds = prior.keys, len(prior.rounds)
    notes.extend(prior.problems)
    seen_before: dict[str, bool] = {}

    def is_new(c: Canonical) -> bool:
        """Did no earlier round raise this? One predicate for the round diff, the
        🆕 marker and the serialised `new_this_round`, so the payload cannot
        disagree with the report about which findings are fresh. Memoised: the
        reworded-title fallback is a sequence comparison against every title the
        baseline holds."""
        if c.key not in seen_before:
            seen_before[c.key] = prior.raised_before(c)
        return not seen_before[c.key]

    # Every finding the cycle has to clear, not just the judged ones: Sonar's
    # hard-gate issues MUST end up resolved (/panel-review-pr §3), so a round
    # whose only outstanding item is a new or still-open gate issue is not a dry
    # round. Leaving them out classified exactly that as convergence and ended the
    # cycle without another fixer.
    outstanding = to_fix + sonar
    new_keys = sorted({c.key for c in outstanding if is_new(c)})
    flagged = sum(1 for c in to_fix if c.needs_rereview)
    # A baseline that could not be read, could not be attributed, or was never
    # passed is a veto in its own right, not just a lost confidence flag: the
    # operator is told to LIST the vetoes, and "not convergence" with an empty
    # list leaves the one question this exists to answer unanswered.
    # An earlier round's truncation becomes PERMANENT under increment scope, and
    # that is the one cost this feature has that its own numbers cannot show. Under
    # whole-PR scope a region round 1 was cut off from is read again by round 2, so
    # the gap closes on its own. Under increment scope round 2 reads only the fix
    # commit and never returns to it: the cycle can now converge — no new findings,
    # nothing outstanding — over code that no round in it ever read. So a quiet
    # round here is not evidence about that region, and says so.
    inherited: list[str] = []
    # Context the budget cut is a coverage gap in its own right, and it has to
    # veto rather than merely be noted. A scoped round is still allowed to raise a
    # defect nobody raised before, wherever it sits — the brief says so in as many
    # words — so the context is not decoration, it is the only part of the PR this
    # round can find a pre-existing defect in. Cut it and that becomes
    # unreachable, and the round would report the resulting quiet as convergence.
    if review.scope == "increment" and short_context:
        inherited.append(
            f"{', '.join(short_context)} saw only part of the PR behind the increment — a "
            "defect earlier rounds misjudged, in the part that did not fit, could not have "
            "been raised this round")
    if review.scope == "increment" and prior.truncated_rounds:
        cut = sorted(prior.truncated_rounds)
        inherited.append(
            f"{_rounds_phrase(cut)} had a truncated reviewer and this round reviewed only "
            f"the increment since {review.since[:8]} — whatever "
            f"{'those rounds were' if len(cut) > 1 else 'that round was'} cut off from has "
            "now been read by no round of this cycle, and re-reviewing the fix commit does "
            "not reach it")
    # The anchor advances over a round that read nothing — a title-skipped round
    # records a head, and so does one whose every seat failed. This round's
    # increment therefore starts AFTER code the cycle has no read of, and the
    # payload cannot show that: `scope` and `since_sha` say what was reviewed, not
    # what was stepped over.
    if review.scope == "increment" and prior.unread_rounds:
        skipped = sorted(prior.unread_rounds)
        inherited.append(
            f"{_rounds_phrase(skipped)} recorded a head but no reviewer read it, and this "
            f"round's increment starts after it — that code has been read by no round of "
            "this cycle")
    veto = (coverage_veto(reviewer_meta, judge_skip, flagged, len(review.target))
            + judge_gaps + inherited + prior.problems)
    stop = round_stop(round_no, cap, new_keys, outstanding, veto, not prior.problems,
                      repeated=len({c.key for c in outstanding if not is_new(c)}))
    # Whether a CYCLE exists at all, and the one predicate that decides it — for
    # the report's Rounds block and for the payload alike. They used to disagree:
    # the report suppressed the block for a review-only run while the payload sent
    # `round_stop` regardless, so `record_review` stored a `/panel` read with
    # findings as `stopped: false` (the board shows a cycle mid-flight that nothing
    # will advance) and one without as `stopped: true, stop_confident: true` — a
    # confident-convergence record for a PR that had no cycle.
    cycle_run = bool(in_cycle or prior_rounds)

    # ---- provenance: did the last fix pass INTRODUCE this, or did it MISS it? --
    #
    # What this round could not read, banked for the next one. A file is unread
    # only if EVERY reviewer that ran was cut on it: one seat that read it means
    # the ROUND saw it, and blaming coverage for a defect some reviewer could
    # plainly see would let the panel off the hook for its own miss.
    #
    # Keyed on the BARE names of the seats that ran, not on `ran_llm`, which
    # holds the report's display labels: `budgets.get("claude (opus)")` is None
    # for every seat, every cut set comes back empty, and `unread_files` was
    # therefore empty on every run this ever made — the `missed-unread` bucket
    # unreachable in production while 487 unit tests, which call the helpers
    # directly with correct inputs, stayed green over it.
    # Defensive rather than expected: `budgets` is built over the same selected
    # seats `tasks` is, so a seat that ran normally has an entry (possibly None,
    # meaning uncapped). It survives so a future change to how `budgets` is built
    # cannot quietly turn "no budget recorded" into "read nothing".
    no_budget = [n for n in ran_names if n not in budgets]
    if no_budget:
        notes.append("no diff budget is recorded for " + ", ".join(sorted(no_budget))
                     + " — those seats are left out of the unread-file record rather than "
                       "silently emptying it")
    cut = [_diff_files_cut(diff, budgets[n]) for n in ran_names if n in budgets]
    if cut:
        unread_files = sorted(set.intersection(*cut))
    elif not ran_names:
        # NO SEAT RAN, so nothing was read: every file is unread. The empty set
        # says the opposite — that the round read all of it — and would hand the
        # next round a `missed` for every defect in a diff nobody ever saw.
        unread_files = sorted(_diff_files_cut(diff, 0))
    else:
        # Seats ran, none of them with a recorded budget. That is a lookup miss,
        # not zero coverage, and the guard has to be on `ran_names` rather than on
        # `cut`: keyed on `cut`, one config miss would record a round that read the
        # whole diff as having read none of it, and the next round would bucket
        # every new finding `missed-unread` — a blanket indictment of the harness
        # bought by a missing dict key. Empty is what the `no_budget` note above
        # already promises ("left out of the record rather than silently emptying
        # it"), and the note is what tells the operator coverage is unrecorded.
        unread_files = []
    # Attribution needs both ends of the fix range, and `_fix_range_diff` says why
    # when it has none: a baseline written before `head_sha` was recorded, a
    # head that never moved, a branch rewritten between rounds, an API refusal.
    # All of them degrade to "unknown" rather than to a wrong answer.
    #
    # `cycle_run` is `in_cycle or prior_rounds`, so `cycle_run and prior_rounds`
    # only ever meant `prior_rounds`: round 1 has no earlier round to attribute
    # against whether it is in a cycle or not.
    attributable = bool(prior_rounds)
    fix_diff, no_range_why = (_fix_range_diff(gh_repo, prior.head_sha, head_sha)
                              if attributable else (None, None))
    # ONE predicate for "is there a range", used by the added lines, by the note
    # and by the attribution itself. Two of them disagreed over an EMPTY compare:
    # truthiness called it no range, `fix_diff is not None` called it a readable
    # range with no added lines — and that reading labels every new finding
    # `missed`, confidently, with no note to say the range was empty.
    fix_added = _diff_added_lines(fix_diff) if fix_diff else {}
    if attributable and not fix_diff:
        notes.append(f"provenance unavailable: {no_range_why} — new findings are recorded "
                     "as `unknown`, not attributed")

    def provenance_of(c: Canonical) -> str | None:
        """None where the question does not arise — outside a cycle, in round 1,
        or for a defect an earlier round already raised. A repeat's provenance is
        not "unknown", it is not asked: it was not introduced by the fix pass
        under attribution, because it predates it. Same discipline as
        `new_findings` being None rather than 0 for a review-only run — the
        board's column already means "the panel did not say"."""
        if not attributable or not is_new(c):
            return None
        return _provenance(c.file or "", c.line, fix_added, prior.unread_files,
                           bool(fix_diff), all_unread=prior.read_nothing)

    # Counted over `outstanding` — the findings the cycle actually has to clear —
    # so the tally matches `new_findings` rather than roping in the dismissed
    # ones, which no fixer will ever touch. ONE pass rather than one per bucket:
    # `provenance_of` walks `fix_added` through `_same_file` on every call.
    tally = Counter(provenance_of(c) for c in outstanding)
    provenance_counts = {b: tally.get(b, 0) for b in PROVENANCE} if attributable else {}
    # A cycle's rounds share one id, inherited from the earliest baseline, so the
    # board can tell "the re-review of THIS declaration" from "whatever ran next
    # on this PR". Only a round 1 of an actual cycle MINTS one.
    #
    # A later round whose baseline was missing, unreadable, from another PR or
    # not earlier sends null rather than a fresh id: `followed_by` requires the
    # cycles to match, so a minted one would make round 1 and round 2 of the same
    # PR two unrelated cycles forever and void every re-review declaration round 1
    # made — a permanent hole in a published measure, bought by a mistyped path.
    # Null records "unattributable", which is the truth and is recoverable.
    # A review-only run sends null too, which is what `ReviewIn.cycle` has always
    # documented ("for a standalone review that is nobody's round 2") and what the
    # producer never emitted.
    cycle = prior.cycle or (run_key if cycle_run and round_no == 1 else None)

    def loc(x: Canonical | Finding) -> str:
        return f"{x.file}:{x.line}" if x.line else x.file

    # ---- the run, as data. Built on every path, not just --json: it is what
    # --json prints, what --json-file writes, and what gets recorded on the
    # board. One structure, so the fix loop and the stats can never be looking
    # at different accounts of the same review — and one finding record per
    # defect, carrying every reviewer's own report, so a consumer reads the merge
    # instead of re-deriving it from an over-counted list.
    payload = {
        **_payload_defaults(),
        "repo": repo_name, "github": gh_repo, "pr": pr_number,
        "title": title, "base": base, "changed_lines": changed,
        # The commit reviewed: the NEXT round anchors its increment on it, and
        # provenance measures its fix range to it. One key, because two would be
        # one fact with two chances to disagree.
        "head_sha": head_sha,
        # Both ends of what this round was judged against (#98). `merge_base` is
        # the PR's base commit — read `scope` before calling it this round's own
        # anchor, which under increment scope is `since_sha`. `base_sha` is where
        # the base branch had got to while the round was being read.
        "merge_base": merge_base,
        "base_sha": base_sha,
        "unread_files": unread_files,
        "changed_files": changed_files, "changed_files_total": changed_files_total,
        "pr_state": pr_state, "is_draft": is_draft,
        # Always True in a payload the BOARD sees — the skip path returns before
        # `record_run` because no review happened. It is here for `--json`
        # consumers, which get both shapes and need to tell them apart.
        "reviewed": True,
        "diff_truncated": truncated,
        # Where this run sits in the panel -> fix -> panel cycle, and what the
        # mechanical stopping rule made of it.
        "round": round_no,
        "cycle": cycle,
        # What was reviewed, and against what. Sent even under "pr" scope, where
        # `since_sha` is null: a consumer must be able to tell a round that chose
        # whole-PR scope from one written before scope existed, and the second one
        # sends no key at all.
        "scope": review.scope,
        "since_sha": review.since or None,
        "prior_rounds": prior_rounds,
        "prior_findings": len(prior_keys),
        # Gated on there being a cycle, exactly as the report's Rounds block is.
        # For a review-only run `len(new_keys)` is every finding — the vacuous
        # count "raised by no earlier round" when there was no earlier round — and
        # `round_stop` is a verdict about a loop nobody is running. None rather
        # than 0, because the board's column already means "the panel did not
        # say", and a zero there is a claim.
        "new_findings": len(new_keys) if cycle_run else None,
        "new_finding_keys": new_keys if cycle_run else [],
        "round_stop": stop if cycle_run else None,
        "stop_reason": stop["reason"] if cycle_run else None,
        "coverage_note": coverage_note or None,
        # The REVIEW TARGET's size — the whole PR under "pr" scope, the increment
        # under "increment". Its meaning is scope-dependent and always has been
        # in spirit ("how big was the thing we reviewed"); what is new is that
        # the answer can now be smaller than the PR, so `scope` must be read
        # beside it. A consumer plotting this across a cycle's rounds without
        # reading `scope` will see a cliff at round 2 and call it a shrinking PR.
        #
        # This pair is what the round PREPARED, which is what a reviewer with no
        # budget was given. It is deliberately not "what each reviewer read":
        # budgets are per reviewer, so there is no single true number for that —
        # `reviewers.<name>.max_diff_chars` and `.truncated` carry the per-seat
        # answer, and a seat that got the whole target and only part of the
        # context is named in `config_notes`.
        "diff_chars": len(review.target),
        # Everything prepared ALONGSIDE the target: 0 under "pr" scope, where
        # there is no such thing. This plus `diff_chars` is what a round put in
        # front of an uncapped reviewer, and the pair is the measurement issue #41
        # exists to produce.
        "context_chars": len(review.near) + len(review.far),
        "diff_budgets": {**budgets, "judge": judge_budget},
        "config_notes": notes,
        "sonar_gate": result.sonar_gate,
        "ci_status": ci_status,
        "ci_failing": ci_failing,
        "judged": judged,
        "judge_model": panel.get("judge_model", "") or None,
        "judge_skip": judge_skip,
        "reviewers_ran": ran_llm,
        "reviewers": reviewer_meta,
        "reviewers_selected": sorted(selected),
        "reviewers_override": override_note,
        # `new_this_round` is added HERE rather than on the record: it is a fact
        # about this run's comparison against a baseline, not a property of the
        # defect, and a Canonical that carried it would have to be told about a
        # baseline to know its own shape.
        # `provenance` rides beside `new_this_round` and for the same reason: both
        # are facts about this run's comparison against a baseline rather than
        # properties of the defect, and a Canonical that carried either would have
        # to be told about a baseline to know its own shape.
        "to_fix": [{**c.as_dict(), "new_this_round": is_new(c),
                    "provenance": provenance_of(c)} for c in to_fix],
        "sonar_findings": [{**c.as_dict(), "new_this_round": is_new(c),
                            "provenance": provenance_of(c)} for c in sonar],
        "dismissed": [{**c.as_dict(), "new_this_round": is_new(c),
                       "provenance": provenance_of(c)} for c in dismissed],
        "provenance_counts": provenance_counts,
        "skipped": result.skipped,
        "run_key": run_key,
    }

    # So a caller can have BOTH the PR comment and the machine-readable run.
    # Without --json-file, --json suppresses the report and the only way to get
    # both was to review the PR twice — several CLI invocations, for a copy. A
    # requested file that could not be written FAILS the run (see `finish`).
    write_failed = write_payload(json_file, payload)

    if record:
        record_run(payload)

    # ---- machine-readable mode: the whole run as JSON, no report/post. Same
    # shape as the skip-pattern exit's payload, so a consumer can read any key of
    # either without checking which exit it came from.
    if json_out:
        print(json.dumps(payload, indent=2))
        return finish(write_failed)

    # How many LLM seats the run was CONFIGURED to fill, against how many filled.
    # Both halves are needed and neither is derivable from the other: "claude ran"
    # is the same sentence whether it was the only seat asked for or the only one
    # of four that answered, and those are a hand-picked single-vendor read and a
    # panel that lost three quarters of its eyes.
    seats_asked = [n for n in LLM_REVIEWERS if n in selected]
    seats_filled = len(ran_llm)
    # A seat whose CLI this box does not carry is NOT a degraded panel, and this
    # is the same distinction `coverage_veto` makes at length a few hundred lines
    # up: an absent CLI is a fact about the HOST, not about the round. It is
    # absent every run, so counting it as degradation prints the warning on every
    # unattended run of a repo that enables a workstation-only vendor — where
    # nothing was lost and nothing could be recovered. That is the alert fatigue
    # `test_a_full_panel_says_none_of_it` exists to prevent, and it would take the
    # real degraded case down with it. Read off recorded state, never off the skip
    # TEXT, for the reason `ReviewerRun.absent` was added.
    seats_absent = [n for n in seats_asked if reviewer_meta.get(n, {}).get("absent")]
    seats_lost = len(seats_asked) - seats_filled - len(seats_absent)
    # The consensus signal needs two seats to exist AT ALL. Below that, "no
    # finding earned ⋆consensus" and "there was nobody to agree with" render
    # identically, and a reader takes the first meaning — the pessimistic
    # reading of a review that never had the chance to be pessimistic.
    #
    # Counted over everything that can FILE a finding, not over the LLM seats:
    # sonarqube's base-branch issues are judged alongside them (`llm_findings`
    # takes `soft`), so a canonical finding's `reviewers` can legitimately read
    # ["claude", "sonarqube"]. Counting LLM seats alone let `conf()` stamp
    # ⋆consensus on that finding while the header two dozen lines below declared
    # consensus impossible — the report contradicting itself in the exact place
    # this was added to stop it being misread.
    #
    # `sonar_filed`, not the gate STATUS, and the distinction is the same one #62
    # spent three rounds on: a status is a side effect, not the thing itself. Only
    # the `no-pr-analysis` fallback yields soft findings that can share a canonical
    # record — the scanned paths return `hard`, which renders in its own section
    # and never reaches `conf()`. So keying on the gate over-counted at one end (a
    # scanned "OK" repo with one LLM seat suppressed both the banner and the
    # sole-reviewer note, on findings nobody could corroborate) and under-counted
    # at the other ("ERROR" can still return hard findings, so the report could
    # claim nobody reviewed while Sonar issues were displayed beneath it).
    filers = seats_filled + (1 if sonar_filed else 0)
    consensus_possible = filers > 1

    def conf(c: Canonical) -> str:
        revs = c.reviewers
        if len(revs) > 1:
            return f" _(via {', '.join(revs)} ⋆consensus)_"
        # Said per finding rather than once at the top, because this is the line a
        # reader is looking at when they decide how much a finding is worth.
        sole = " — sole reviewer, no second opinion" if not consensus_possible else ""
        return f" _(via {', '.join(revs)}{sole})_"

    def accounts(c: Canonical) -> list[str]:
        """What each reviewer actually said, under a MERGED finding.

        The synthesis is the judge's statement of the issue; these are the
        reports it was made from, and they are shown because one reviewer
        routinely makes a point the others didn't. Truncated here (the whole
        report is a PR comment) but kept whole in `--json` and on the board."""
        if len(c.reported_by) < 2:
            return []
        out = []
        for f in c.reported_by:
            said = _account(f)
            cut = said[:ACCOUNT_CHARS] + ("…" if len(said) > ACCOUNT_CHARS else "")
            out.append(f"  - _{f.reviewer}_ ({f.severity} `{loc(f)}`): {cut}")
        return out

    # ---- report
    heading = f"## Reviewer panel — PR #{pr_number}"
    # One predicate for the heading and the summary beneath it: a baseline that
    # parsed but held no findings is still an earlier round, and used to produce a
    # "· round 1" heading with nothing under it.
    in_rounds = round_no > 1 or bool(prior_rounds)
    if in_rounds:
        heading += f" · round {round_no}"
    lines = [heading, ""]
    if in_rounds:
        # Counted over everything the cycle has to clear (Sonar's hard gate
        # included), so the numerator and the denominator are the same population.
        lines.append(f"**Round {round_no}** — re-reviewing after the fix. "
                     f"{len(new_keys)} of {len(outstanding)} finding(s) here were raised by "
                     f"no earlier round ({len(prior_keys)} known from {prior_rounds} earlier "
                     f"round{'s' if prior_rounds != 1 else ''}).")
        # The split those "new" findings hide. Printed rather than left to the
        # payload because the operator deciding whether to go again is the one
        # the distinction is FOR: findings the fix pass created argue for a
        # smaller next pass, findings the last round missed argue for more
        # coverage, and the two read identically as a count.
        pc = payload["provenance_counts"]
        # Only the buckets with something in them, and nothing at all when the
        # only populated bucket is `unknown`. Leading with "**0 introduced**,
        # **0 missed**" under a config note explaining that nothing could be
        # attributed reads as a bolded claim about the fix pass, and a false one.
        phrasing = {"introduced": "**{n} introduced** by the last fix pass",
                    "missed": "**{n} missed** by the last round",
                    "missed-unread": "{n} in files that round could not read",
                    "unknown": "{n} unattributable"}
        if any(pc.get(b) for b in PROVENANCE if b != "unknown"):
            detail = [t.format(n=pc[b]) for b, t in phrasing.items() if pc.get(b)]
            lines.append("  - of those: " + ", ".join(detail)
                         + ". A signal, not a verdict — a fix can break something at a "
                           "distance, so `missed` is evidence rather than proof.")
    ci_txt = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "PENDING": "⏳ pending",
              "none": "no checks reported", "unknown": "unknown"}.get(ci_status, ci_status)
    lines.append(f"**CI (`gh pr checks`, hard gate):** {ci_txt}")
    if ci_failing:
        lines.append("  - failing: " + ", ".join(ci_failing[:10])
                     + (f" (+{len(ci_failing) - 10} more)" if len(ci_failing) > 10 else ""))
    if ci_status in ("FAIL", "PENDING"):
        lines.append(f"  - ⚠️ CI is {ci_txt.split()[-1]} — do not merge until green, "
                     "even if the review below is clean")
    gate_txt = {"OK": "✅ PASS", "ERROR": "❌ FAIL"}.get(result.sonar_gate, result.sonar_gate)
    if result.sonar_gate in ("OK", "ERROR"):
        lines.append(f"**SonarCloud quality gate (hard):** {gate_txt}")
    elif result.sonar_gate == "no-pr-analysis":
        lines.append("**SonarCloud:** PR not scanned — base-branch issues on the "
                     "lines this PR adds, surfaced as soft findings (judged). "
                     "No hard gate: publish a PR analysis to get one.")
    else:
        lines.append(f"**SonarCloud:** {gate_txt}")
    # The seat count rides with the reviewer list on EVERY run, not only degraded
    # ones. A round's finding count is not comparable across different panel
    # sizes, and the convergence table this repo keeps has been read as if it
    # were: #32 went 22 -> 43 between rounds and gained a reviewer in the same
    # step. The number that disambiguates it has to be in the artifact.
    lines.append(f"**LLM reviewers ran:** {', '.join(ran_llm) or 'none'}"
                 f" — {seats_filled} of {len(seats_asked)} configured")
    # The panel-level version of #19's per-reviewer fix. #19 stopped a reviewer
    # that produced nothing from reading as a reviewer that found nothing; this
    # stops a PANEL that lost half its seats from reading as a panel that agreed.
    # A run with empty seats is a materially weaker artifact than a full one and
    # was presented identically — on PR #64 that meant 23 findings from a single
    # reviewer, whose own master wrote that nine self-declared coverage gaps
    # "stand unchallenged and unread", laid out exactly like 23 from a full panel.
    # It is stated here, above the findings, rather than in a footer: under the
    # epic (#52) nobody is reading this in a terminal as it happens.
    if seats_lost > 0:
        lines.append(f"  - ⚠️ **panel degraded** — {seats_lost} of {len(seats_asked)} "
                     f"configured reviewer{'s' if len(seats_asked) != 1 else ''} did not "
                     "run. Read what follows as a weaker review, not a cleaner one: "
                     "an empty seat cannot report what it would have found.")
    if seats_absent:
        # Quieter, and separate, for the reason above: this one is about the box,
        # is true every run on it, and is nobody's fault.
        lines.append(f"  - _{', '.join(seats_absent)} not installed on this host — "
                     "configured, but never a seat here_")
    if not consensus_possible and seats_asked:
        # Two different sentences, because the one-seat and no-seat cases are
        # different claims and the single wording asserted "one filed" on a run
        # where nobody had — the exact class of misreport this block exists to
        # prevent, in the block itself.
        if filers == 0:
            lines.append("  - ⚠️ **nothing below was reviewed by a panel member** — "
                         "every configured seat is empty, so there are no findings to "
                         "agree about and no ⋆consensus notation appears at all.")
        else:
            lines.append("  - ⚠️ **no ⋆consensus is possible this round** — it takes two "
                         "reviewers to agree, and one filed. Absence of ⋆consensus below "
                         "means nobody was there to agree, NOT that nobody agreed.")
    if override_note:
        # Said on the PR, not just in the terminal: a reader of the comment needs
        # to know this panel was hand-picked before reading "reviewed by one".
        lines.append(f"  - {override_note}")
    # A lost reviewer is stated where the reviewer list is, not only in a section
    # at the foot of the report: a pinned model the CLI cannot use costs you a
    # whole vendor, and a one-reviewer panel that reads like a two-reviewer panel
    # is the failure mode worth shouting about.
    for skip in llm_skipped:
        lines.append(f"  - ⚠️ **not reviewed** — {skip}")
    if not findings:
        judge_txt = ("ruled on coverage only — no findings to judge" if coverage_note
                     else "n/a — no findings to judge")
    elif judged:
        judge_txt = reviewer_label("claude", panel.get("judge_model", ""))
    else:
        judge_txt = f"⚠️ {judge_skip} — all findings KEPT unjudged (re-run to get a verdict)"
    lines.append(f"**Master judge:** {judge_txt}")
    for note in notes:
        lines.append(f"  - ⚠️ config: {note}")
    if truncated:
        # Named per reviewer, since the budgets can now differ: "truncated" alone
        # would hide that one model saw the whole diff and another saw a third of
        # it, which is exactly what you need to know when they disagree.
        cut = ", ".join(f"{n} ({b:,})" for n, b in sorted(truncated_for.items()))
        what = "increment" if review.scope == "increment" else "diff"
        lines.append(f"\n_{what} is {len(review.target):,} chars — truncated for {cut}_")

    lines.append(f"\n### To fix ({len(to_fix)}) — master-confirmed, any reviewer count")
    if to_fix:
        for c in to_fix:
            tail = f" — {c.rationale}" if c.rationale and c.rationale != "unjudged" else ""
            rel = f" _(same decision as {', '.join(c.related)})_" if c.related else ""
            # Said per finding, not only in the header: the rationale is blank
            # for these, so an unruled finding otherwise renders identically to
            # an adjudicated one under a header naming the judge.
            unruled = " _(unjudged — the master never ruled on this one)_" \
                if c.verdict == "unjudged" else ""
            # Only where there IS an earlier round to be new against: on a first
            # round every finding is new and the marker would be decoration.
            fresh = " 🆕" if prior_rounds and is_new(c) else ""
            again = (" ↻ _fix needs re-reading (" + ", ".join(c.rereview_by) + ")_"
                     if c.needs_rereview else "")
            lines.append(f"- **{c.severity}**{fresh} `{loc(c)}` [{c.id}] — {c.synthesis}"
                         f"{conf(c)}{unruled}{tail}{rel}{again}")
            lines += accounts(c)
    else:
        lines.append("- none")

    if sonar:
        lines.append(f"\n### SonarCloud issues ({len(sonar)}) — part of the gate")
        for c in sorted(sonar, key=lambda x: x.severity):
            # Same 🆕 rule as the judged findings: these count towards the round
            # diff too, because the gate has to end up clear either way.
            fresh = " 🆕" if prior_rounds and is_new(c) else ""
            lines.append(f"- {c.severity}{fresh} `{loc(c)}` — {c.synthesis}")

    if dismissed:
        lines.append(f"\n### Dismissed by master ({len(dismissed)})")
        for c in dismissed:
            lines.append(f"- ~~{c.severity} `{loc(c)}` — {c.synthesis}~~"
                         f"{conf(c)} — {c.rationale}")
            lines += accounts(c)

    if result.skipped:
        lines.append("\n### Skipped reviewers\n" +
                     "\n".join(f"- {s}" for s in result.skipped))

    # What the reviewers said about their OWN coverage, and what the judge made of
    # the split. This is the difference between "clean" and "I could not tell",
    # which no finding count can express — and it is on the PR comment, not just
    # the terminal, because the person deciding whether a clean verdict was earned
    # is reading the comment.
    declared = {n: m["could_not_assess"] for n, m in sorted(reviewer_meta.items())
                if m.get("could_not_assess")}
    if declared or coverage_note:
        lines.append("\n### Coverage declared by the reviewers")
        for name, gaps in declared.items():
            lines.append(f"- **{name}** could not assess: " + "; ".join(gaps))
        if coverage_note:
            lines.append(f"- _master:_ {coverage_note}")

    verdict = "**stop**" if stop["stop"] else "**go again**"
    unearned = stop["stop"] and not stop["confident"]
    # Only where a loop actually exists. `--max-rounds` is the CALLER's cap and
    # only /panel-review-pr drives the loop; a review-only run (`/panel`, or
    # panel.py by hand) that printed "round 1 of at most 2 — go again" promised a
    # round nothing would run, and counted every finding of a first review as one
    # "no earlier round raised" — which is vacuously all of them.
    if cycle_run:
        lines.append(f"\n{ROUNDS_HEADING} round {round_no} of at most {cap} — {verdict}: "
                     + stop["reason"]
                     + (" — a stop, not convergence" if unearned else ""))
        # What this round READ, said next to what it concluded — and INSIDE the
        # Rounds block, because `fit_comment` trims around that block and this is
        # the sentence that makes the rest of the comment mean what it says.
        # Without it "round 2 found 4 findings" is unreadable: against the whole
        # PR that is a quiet round, against one fix commit it is a busy one, and
        # the two render identically. The char count is the TARGET's, matching
        # `diff_chars`; the context is named separately because it is the half the
        # budget is allowed to eat.
        if review.scope == "increment":
            lines.append(
                f"  _scope: the increment since `{review.since[:8]}` "
                f"({len(review.target):,} chars), plus "
                f"{len(review.near) + len(review.far):,} chars of the rest of the PR as "
                "context. Findings are about the fix commit, or about the seam it "
                "landed in — not a re-read of code earlier rounds cleared._")
        veto_head, bullet = "  _why this round's quiet is not evidence of a quiet PR:_", "  - ⚠️ "
    else:
        veto_head, bullet = ("\n**Coverage caveats** — why this review's quiet is not "
                             "evidence of a quiet PR:"), "- ⚠️ "
    if stop["veto"]:
        lines.append(veto_head)
        # A baseline problem is deliberately BOTH a config note (what went wrong)
        # and a veto (why the quiet does not count), and the payload carries it in
        # both roles on purpose — `config_notes` never reaches the board, so the
        # veto list is the record's only copy. On the PR comment, though, printing
        # the same sentence twice reads as two problems. The second appearance is
        # rendered as a pointer to the first.
        was_a_note = set(notes)
        for why in stop["veto"]:
            if why in was_a_note:
                lines.append(f"{bullet}{_veto_gist(why)} — _the config note above, "
                             "which is also why this round's quiet does not count_")
            else:
                lines.append(f"{bullet}{why}")

    report = "\n".join(lines)
    print(report)

    if post:
        # Bounded, and NOT check=True. This is the last step of a run that has
        # already succeeded and already printed its report above: a hung network
        # call here would block after every expensive thing is done, and raising
        # would throw away a completed review over a failed comment. The comment
        # is how the fix loop finds the findings, so a failure has to be LOUD —
        # but it degrades the run, it doesn't void it.
        try:
            proc = subprocess.run(["gh", "pr", "comment", str(pr_number), "--repo",
                                   gh_repo, "--body", fit_comment(report)],
                                  capture_output=True,
                                  text=True, stdin=subprocess.DEVNULL, timeout=120)
            if proc.returncode == 0:
                print(f"\n(posted panel summary to {gh_repo}#{pr_number})")
            else:
                why = stderr_gist(proc.stderr or "") or f"exited {proc.returncode}"
                print(f"\n! panel summary NOT posted to {gh_repo}#{pr_number} ({why})"
                      f" — the report above is the only copy", file=sys.stderr)
        except (subprocess.TimeoutExpired, OSError) as e:
            why = "timed out after 120s" if isinstance(e, subprocess.TimeoutExpired) \
                else e.__class__.__name__
            print(f"\n! panel summary NOT posted to {gh_repo}#{pr_number} ({why})"
                  f" — the report above is the only copy", file=sys.stderr)
    else:
        print("\n(report only — pass --post to comment on the PR)")
    return finish(write_failed)


# ----------------------------------------------------------------------------- ask

#: `path`, `path:12`, `path:3500-3560`. Anchored, so a colon inside a path
#: (`odd:dir/x.py`) is a path and not a malformed range. Digits are bounded
#: because `int()` REFUSES a string of more than 4,300 digits (CPython's
#: integer-from-string limit) with a ValueError — a `--context x:9999…` long
#: enough to trip it would have crashed the command rather than been reported as
#: the nonsense it is. Nine digits is past any file anyone will read.
_RANGE = re.compile(r"^(\d{1,9})(?:-(\d{1,9}))?$")

#: The most one `--context` file will be READ from disk, whatever the char budget
#: then does with it. A separate ceiling from `ask_max_context_chars` because it
#: bounds a different cost: the budget bounds what the seats are SENT (and so
#: what is paid for), this bounds what is materialised in memory to slice a range
#: out of. A source file this big is not context for a premise either way, and it
#: is said rather than silently cut, so a stale spec against a generated file
#: does not look like a file that was read.
ASK_CONTEXT_FILE_MAX_BYTES = 4_000_000

#: Directories an ask will not read out of, however contained they are.
#: Containment answers "is this the repo under review?" and nothing else — and
#: the repo under review is precisely where the credentials are. `.git/config`
#: carries a personal access token in the remote URL on every https clone that
#: was authenticated once, and `.git/` holds every blob the working tree no
#: longer does, so a secret deleted a year ago is still readable through it.
ASK_SECRET_DIRS = frozenset({".git"})

#: Files that are nothing but secrets, by the names they are always given. Short
#: and exact on purpose: this is a denylist, not a secret scanner, and it is not
#: claimed to be one. It closes the routes an agent composing a `--context`
#: actually types, and every refusal is a stated :class:`ContextProblem`, so a
#: false positive costs one visible sentence and a miss costs no more than the
#: containment check alone already did.
ASK_SECRET_FILES = frozenset({".env", ".envrc", ".npmrc", ".netrc", ".pgpass",
                              ".pypirc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"})

#: Extensions that are key material whatever the file is called.
ASK_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


class AskContext(NamedTuple):
    """One `--context` argument, resolved and read."""

    spec: str
    #: Repo-relative, resolved — what the report and the payload name it by.
    path: str
    first: int | None
    last: int | None
    text: str


class ContextProblem(NamedTuple):
    """A `--context` spec that did not become context, and why.

    The spec is kept BESIDE the sentence rather than only inside it, because
    "was this verdict reached with all the context the asker intended?" is a
    question the payload has to be able to answer without string-matching prose —
    and that distinction (an answer from missing material, versus an answer from
    unclear material) is the whole reason this feature exists."""

    spec: str
    problem: str


def _readable_file(path: Path) -> bool:
    """Is `path` a file right now — a question asked only to DISAMBIGUATE a spec,
    never to decide a read. Every containment check still runs afterwards."""
    try:
        return path.is_file()
    except OSError:
        return False


def _context_spec(spec: str, root: Path | None = None) -> tuple[str, int | None, int | None,
                                                                str | None]:
    """Split `path[:first[-last]]` into (path, first, last, problem). A bare
    `path:12` is the single line 12.

    **An existing file wins over a line range.** `--context config:2024` names the
    file `config:2024` when that file is there, and line 2024 of `config` when it
    is not. Without that test the range reading was unconditional, so a file whose
    own name ends in `:digits` could never be selected — and, worse, a repo
    holding both `config` and `config:2024` silently read line 2024 of the wrong
    one. There is no escaping syntax (`./notes:12` does not help), so the
    filesystem is the tie-breaker.

    A tail that is not a range, after a path that IS a file, is a bad RANGE and
    said so: `sub/a.py:abc` used to be reported as `sub/a.py:abc` not being a
    file, which is accurate and points at the wrong half of what was typed."""
    head, sep, tail = spec.rpartition(":")
    if not sep:
        return spec, None, None, None
    if root is not None and _readable_file(root / spec):
        return spec, None, None, None
    m = _RANGE.match(tail)
    if not m:
        if root is not None and head and _readable_file(root / head):
            return spec, None, None, (f"`--context {spec}`: {tail!r} is not a line range "
                                      "— expected N or N-M, counting from 1")
        return spec, None, None, None
    first = int(m.group(1))
    return head, first, int(m.group(2)) if m.group(2) else first, None


def _read_confined(root: Path, resolved: Path, limit: int) -> bytes:
    """Read at most `limit` + 1 bytes of `resolved` by walking DOWN from a
    descriptor on `root`, refusing a symlink at every step — the ROOT's own open
    included.

    The containment test in :func:`read_context` states the rule; this enforces
    it. Resolving a path and then opening it by that path are two traversals of
    the same string, and between them any component can become a symlink out of
    the repo — so the check would pass and the read would leave. Opening each
    component `O_NOFOLLOW` relative to the descriptor of the one above it never
    re-traverses anything: the file read is the file checked, or the open fails.

    It narrows nothing a caller can reach by typing. `resolved` is symlink-free
    by construction — `Path.resolve` followed every link before the containment
    test — so a spec naming a link inside the repo still reads its target, and the
    walk sees only real directories. `O_NOFOLLOW` firing here means a component
    turned into a symlink AFTER it was checked, which is the race and nothing
    else, and the caller is told so in those words.

    The ROOT is opened `O_NOFOLLOW` too, and it is the step that used not to be:
    every component below it was anchored to a descriptor while the first was
    still opened by pathname, so a repo root (or an ancestor of it) replaced
    between `resolve()` and this call redirected the whole walk out of the tree
    that was checked. `root` is itself resolved by the caller, so its last
    component is not a symlink and the flag narrows nothing reachable by typing —
    it closes the same race one step higher up.

    Bytes, not text, and bounded: what is on disk decides whether this is context
    at all (see :func:`read_context`, which refuses what does not decode), and
    `errors="replace"` would have turned a PNG into a wall of U+FFFD that reads
    as a successful read. `limit` + 1 so the caller can tell "exactly `limit`"
    from "more than `limit`" without a stat that would race the read."""
    parts = resolved.relative_to(root).parts
    if not parts:
        raise IsADirectoryError(errno.EISDIR, "the repo root is not a file", str(root))
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    finally:
        os.close(fd)
    # `leaf` is a raw descriptor until fdopen adopts it, and fdopen can fail
    # (MemoryError, a bad encoding name) — leaving it open forever in a caller
    # that is not a one-shot CLI. Closed by hand on exactly that path, and by the
    # file object on every other.
    fh = None
    try:
        fh = os.fdopen(leaf, "rb")
        return fh.read(limit + 1)
    finally:
        if fh is None:
            os.close(leaf)
        else:
            fh.close()


def _secret_context(rel: Path) -> str | None:
    """Why an ask will not read this repo-relative path, or None to read it.

    Containment is not the whole rule. `is_relative_to(root)` answers one
    question — "is this the repo under review?" — and the answer being yes is
    exactly the case where `--context .git/config` hands a PAT to four
    third-party CLIs, because a seat's reply is a place its prompt can come back
    out. `.env`, `.envrc`, `.npmrc` and committed key material are the same
    shape: readable, contained, and not context for a premise.

    Named components, not content: this refuses the files that ARE credentials,
    and says nothing about a token pasted into a source file. It is the cheap
    half of the rule and is documented as such (see `harness/loops/README.md`)."""
    parts = rel.parts
    if not parts:
        return None
    for part in parts:
        if part in ASK_SECRET_DIRS:
            return (f"it is inside `{part}/` — the repo's own object store, where "
                    "`config` carries the access token an https remote was cloned with")
    name = parts[-1]
    if name in ASK_SECRET_FILES or name.startswith(".env."):
        return f"`{name}` is a credentials file, not context for a premise"
    if rel.suffix in ASK_SECRET_SUFFIXES:
        return f"`{rel.suffix}` files are key material"
    return None


def read_context(root: Path, specs: list[str], problems: list[ContextProblem],
                 budget: int | None = None) -> list[AskContext]:
    """The files (or line ranges) an ask hands its seats, read from the repo under
    review.

    **Confined to that repo, and refused rather than clamped when it is not.**
    The path comes off a command line that an agent composes, so `--context
    ../../.ssh/id_ed25519` is a real shape: this is a prompt builder, and every
    seat's reply is a place its contents could come back out. Resolution follows
    symlinks before the containment test for the same reason `write_payload`
    opens `O_NOFOLLOW` — a link inside the repo is not a file inside the repo.

    **And containment is not the whole rule**, because the repo under review is
    where the credentials live: `--context .git/config` is contained, readable,
    and on an https remote it is a personal access token. So `.git/` and the
    usual secret filenames are refused too — see :func:`_secret_context`, which
    states each refusal as a problem naming why.

    A spec that cannot be read is a PROBLEM and never a silent omission. A seat
    given less context than the asker believes it has will answer `cannot tell`
    about a question the asker thinks it supplied the answer to, and the asker
    will read that as the code being unclear rather than as the file being
    missing.

    **`budget` bounds what the seats are sent, and the clamp is SAID.** An ask is
    the cheap check — that is its entire claim on anyone's attention — and
    `--context` had no ceiling at all: one spec naming a generated file, or this
    5,700-line module, built a multi-megabyte prompt and shipped a copy of it to
    every vendor on the panel. That is the #117 cost shape (one release-merge
    ≈ $750) reappearing on the path advertised as costing a minute. So the total
    is capped like a round's diff is, per the same rule and with the same
    reporting: the config wins as far as it can, and where it was cut the report
    says which spec and by how much."""
    root = root.resolve()
    out: list[AskContext] = []
    used = 0
    #: Exact repeats only. `--context a.py --context a.py` is one request typed
    #: twice — it read the file twice and formatted two identical sections into
    #: every seat's prompt, which is tokens spent on nothing in the one feature
    #: whose argument is that it is cheap. Overlapping ranges are left alone:
    #: `a.py:1-40` beside `a.py:20-30` is a legible thing to ask for.
    seen: set[str] = set()
    for spec in specs:
        if spec in seen:
            continue
        seen.add(spec)
        path, first, last, bad_range = _context_spec(spec, root)
        if bad_range:
            problems.append(ContextProblem(spec, bad_range))
            continue
        if not path.strip():
            problems.append(ContextProblem(spec, f"`--context {spec}` names no file"))
            continue
        try:
            resolved = (root / path).resolve()
        except (OSError, RuntimeError, ValueError) as e:
            # RuntimeError is a symlink loop, ValueError an embedded NUL or
            # another path the OS will not take — both reach here off a command
            # line an agent composed, and neither is worth a traceback that loses
            # the other seats' answers and the payload with them.
            problems.append(ContextProblem(
                spec, f"`--context {spec}` could not be resolved ({e.__class__.__name__})"))
            continue
        if not resolved.is_relative_to(root):
            problems.append(ContextProblem(
                spec, f"`--context {spec}` is outside {root} — an ask reads the "
                      "repo under review and nothing else"))
            continue
        secret = _secret_context(resolved.relative_to(root))
        if secret:
            problems.append(ContextProblem(
                spec, f"`--context {spec}` was refused: {secret}. An ask hands its "
                      "context to four third-party CLIs, so being inside the repo is "
                      "not on its own a reason to read a file"))
            continue
        if not resolved.is_file():
            # Saying where paths are anchored, because the plausible mistake is
            # an agent running this from `harness/loops/` and typing `panel.py`.
            problems.append(ContextProblem(
                spec, f"`--context {spec}` is not a file in {root} — `--context` "
                      "paths are relative to the repo root, not to the cwd"))
            continue
        try:
            data = _read_confined(root, resolved, ASK_CONTEXT_FILE_MAX_BYTES)
        except OSError as e:
            # ELOOP or ENOTDIR from the walk means the tree changed under it —
            # a directory that was checked is now a symlink (Linux answers a
            # no-follow open of one with ENOTDIR when O_DIRECTORY is also set,
            # which is why both codes read the same way here). Nothing a caller
            # can type reaches either: `resolve()` already settled the links, and
            # a non-directory component fails `is_file()` before the read.
            why = ("a component of the path changed after it was checked — it is "
                   "now a symlink, or no longer a directory"
                   if e.errno in (errno.ELOOP, errno.ENOTDIR) else e.__class__.__name__)
            problems.append(ContextProblem(spec, f"`--context {spec}` could not be read ({why})"))
            continue
        rel = str(resolved.relative_to(root))
        if len(data) > ASK_CONTEXT_FILE_MAX_BYTES:
            problems.append(ContextProblem(
                spec, f"`--context {spec}`: {rel} is over {ASK_CONTEXT_FILE_MAX_BYTES:,} "
                      "bytes — larger than an ask will read, and not context for a premise"))
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # `errors="replace"` guaranteed this read SUCCEEDED, so `--context
            # assets/logo.png` became a wall of U+FFFD in every seat's prompt and
            # the asker was never told. A file that is not text is a stated
            # problem, like every other spec that did not become context.
            problems.append(ContextProblem(
                spec, f"`--context {spec}`: {rel} is not UTF-8 text — an ask hands its "
                      "seats source, not bytes"))
            continue
        if "\x00" in text:
            problems.append(ContextProblem(
                spec, f"`--context {spec}`: {rel} carries NUL bytes — an ask hands its "
                      "seats source, not bytes"))
            continue
        #: Newlines KEPT, so a range is a substring of the whole file rather than
        #: a re-joining of it. `path` and `path:1-N` over the same N lines used to
        #: differ by one character (and so by one in the payload's `chars`),
        #: which is nothing to a seat and confusing to anyone diffing two
        #: payloads. `len()` is unchanged — keepends splits at the same points.
        lines = text.splitlines(keepends=True)
        if first is None:
            kept = _budgeted(AskContext(spec, rel, None, None, text),
                             budget, used, problems)
            if kept is not None:
                out.append(kept)
                used += len(kept.text)
            continue
        if first < 1:
            problems.append(ContextProblem(spec, f"`--context {spec}`: lines are numbered from 1"))
            continue
        if first > len(lines):
            problems.append(ContextProblem(
                spec, f"`--context {spec}`: {rel} has {len(lines):,} lines"))
            continue
        if last < first:
            problems.append(ContextProblem(
                spec, f"`--context {spec}`: the range ends before it starts"))
            continue
        if last > len(lines):
            # Clamped and SAID, rather than clamped quietly: "3500-3560" against a
            # 3,510-line file is usually a stale line number, and a seat answering
            # from ten lines where the asker meant sixty is the failure this whole
            # feature exists to make cheap to notice.
            problems.append(ContextProblem(
                spec, f"`--context {spec}`: {rel} has {len(lines):,} lines — "
                      f"the seats got {first}-{len(lines)}"))
            last = len(lines)
        kept = _budgeted(AskContext(spec, rel, first, last, "".join(lines[first - 1:last])),
                         budget, used, problems)
        if kept is not None:
            out.append(kept)
            used += len(kept.text)
    return out


def _budgeted(ctx: AskContext, budget: int | None, used: int,
              problems: list[ContextProblem]) -> AskContext | None:
    """`ctx` cut to what is left of the ask's context budget, saying so when it
    cut anything — the same shape as the line-range clamp above it, and for the
    same reason: a seat answering from a fragment of what the asker meant to hand
    it is exactly the failure this feature exists to make cheap to notice.

    None when nothing at all was left, because a section with no content in it is
    a header telling the seats a file was supplied when it was not.

    **`last` moves with the text.** Left at the range that was ASKED for, a
    clamped `sub/a.py:1-200` still serialised `{"first": 1, "last": 200}` and
    rendered as `` `sub/a.py:1-200` `` while the seats saw ten lines — two
    records in one payload disagreeing about what was read, and the wide one is
    the one #77's board row ("was this verdict reached with all the context the
    asker intended?") would answer from."""
    if budget is None:
        return ctx
    left = budget - used
    whole = len(ctx.text)
    if left <= 0:
        problems.append(ContextProblem(
            ctx.spec, f"`--context {ctx.spec}`: the {budget:,}-char context budget "
                      "(`review_panel.ask_max_context_chars`) was spent by the specs "
                      "before it — the seats got none of this one"))
        return None
    if whole > left:
        problems.append(ContextProblem(
            ctx.spec, f"`--context {ctx.spec}`: the seats got {left:,} of {whole:,} chars "
                      f"— the {budget:,}-char context budget "
                      "(`review_panel.ask_max_context_chars`) stopped it"))
        cut = ctx.text[:left]
        # The last line the seats saw any of, counted from the text they got: a
        # cut landing mid-line still showed them that line's beginning, and
        # reporting the line before it would be the same lie in the other
        # direction. `first` is untouched — where the range starts is not what
        # the clamp changed. A whole-file spec has no range to correct.
        kept = cut.count("\n") + (0 if cut.endswith("\n") else 1)
        last = None if ctx.first is None else ctx.first + kept - 1
        return ctx._replace(text=cut, last=last)
    return ctx


def _context_chars(contexts: list[AskContext]) -> int:
    """How much CONTENT the seats are being handed — the quantity a budget is
    about, and the one :func:`_context_block` cuts. Not the length of the
    assembled block, which also counts delimiters that no clamp may touch."""
    return sum(len(c.text) for c in contexts)


#: What goes where the context would have been when there is none — and it is a
#: sentence rather than an empty string on purpose. See :func:`_context_block`.
NO_CONTEXT = ("\n--- CONTEXT ---\nNone was given. Answer from the premise's own terms, "
              "and where those do not settle it answer \"cannot tell\" — you have "
              "nothing to check it against and must not answer from memory.\n")


def _context_block(contexts: list[AskContext], budget: int | None = None) -> str:
    """The context as the seats see it, or the sentence that goes where it would
    have been.

    `budget` cuts the FILE CONTENT, section by section, and never the assembled
    block: slicing the finished string is how a clamp lands in the middle of a
    `--- CONTEXT: path ---` line and hands a seat a prompt whose last section has
    a half-written header on it. Every delimiter that is emitted is whole, and a
    section the budget leaves nothing for is dropped with its header rather than
    announced as a file that was supplied. A budget that leaves nothing of ANY
    of them falls through to the no-context sentence below, because that is what
    the seat is looking at.

    An ask with no context is legitimate — some premises are settled by their own
    terms — but a model handed a bare assertion and no material will reach for
    what it remembers about a library, or about this repo, and answer with real
    confidence from nothing. Saying out loud that it was given nothing is what
    makes `cannot tell` the available answer rather than a gap it has to invent
    its way across."""
    out = []
    left = budget
    for c in contexts:
        if left is not None and left <= 0:
            break
        text = c.text if left is None else c.text[:left]
        if left is not None:
            left -= len(text)
        where = f"{c.path}:{c.first}-{c.last}" if c.first else c.path
        out.append(f"\n--- CONTEXT: {where} ---\n{text}\n")
    # No sections is no sections, whether nothing was given or the budget left
    # nothing of what was. Returning "" for the second ended the prompt straight
    # after `--- PREMISE ---`: no material, and — worse — not the sentence above
    # either, so the one seat that can reach a zero budget (antigravity, whose
    # prompt travels in argv) was invited to answer from memory by a prompt that
    # never told it there was nothing to read.
    return "".join(out) or NO_CONTEXT


#: The ask's declared defaults — read from where they are declared and
#: documented, rather than spelled a second time here. See :func:`_ask_rule`.
ASK_DEFAULTS = harness_rules.DEFAULTS["review_panel"]


def _ask_rule(panel: dict, key: str, notes: list[str]) -> int:
    """A tally rule (or the context budget) as a positive int, saying so when the
    config is not one.

    Same discipline as :func:`diff_budget`: what cannot be the thing at all falls
    back and is reported, because silently honouring `ask_quorum: 0` would let a
    tally of nobody decide, and silently dropping it would leave you believing a
    rule you never got.

    The fallback comes from :data:`harness_rules.DEFAULTS`, which is where the
    default is declared and documented. Passing it in meant every call site
    spelled the number a second time, so a default changed in the file that
    documents it would go on being ignored by the file that applies it."""
    fallback = ASK_DEFAULTS[key]
    raw = panel.get(key)
    if raw is None or raw == "":
        return fallback
    n = None
    if not isinstance(raw, bool) and isinstance(raw, (int, str)):
        try:
            n = int(raw)
        except ValueError:
            n = None
    if n is None:
        notes.append(f"`{key}`={raw!r} is not a number — using {fallback}")
        return fallback
    if n < 1:
        notes.append(f"`{key}`={n} would let a tally of nobody decide — using {fallback}")
        return fallback
    return n


#: Environment that says an agent, rather than a person at a prompt, is running
#: this challenge — and which seat that agent is. Claude Code exports both of
#: these into every command it runs, so an agent that asks does not have to
#: remember to declare itself; forgetting is precisely how a premise gets
#: "confirmed" by the model that wrote it.
#:
#: **This is Claude Code's environment and only Claude Code's.** codex, pi and
#: `agy` export nothing this file can recognise as "seat X is running me", so an
#: agent driven by one of them gets no asker and the self-challenge guard does
#: not fire. That is not silent any more: :func:`ask` says in its notes that
#: nothing was detected, because a guard believed to be on and quietly off is
#: worse than one known to need `--asker`.
ASKER_ENV = {"CLAUDE_CODE_SESSION_ID": "claude", "CLAUDECODE": "claude"}


def asking_seat(explicit: str | None) -> str:
    """Which seat is asking, from `--asker` or from the environment.

    `--asker ''` is an explicit "nobody" — for a human at a terminal, where there
    is no agent and so no self-challenge to guard against. It is honoured, since
    the alternative is a person unable to turn off a rule that does not apply to
    them; it is the one hole in this, and it is one an agent has to type — and
    typing it while an agent's environment is present is now reported, so the
    hole cannot be used quietly."""
    if explicit is not None:
        return explicit.strip().lower()
    return detected_asker()


def detected_asker() -> str:
    """The seat :data:`ASKER_ENV` says is running this, or "" for nobody."""
    return next((seat for var, seat in ASKER_ENV.items() if os.environ.get(var)), "")


def ask(repo_name: str | None, premise: str, contexts: list[str] | None = None,
        reviewers: str | None = None, pr_number: int | None = None,
        json_out: bool = False, json_file: str = "", record: bool = True,
        asker: str | None = None) -> int:
    """Put one premise to the panel's seats and print what they said.

    No diff, no clustering, no judge. A round already votes on fixes — that is
    what a round IS — so the gap this fills is granularity and latency, not
    absence: three of PR #62's rounds each spent twenty minutes and thirty
    findings answering a yes/no question about one branch of `panel.py`.

    **Not a gate.** It exits 0 on every verdict, including `fails`. Making it a
    pass/fail step turns a one-minute question into a required wait, and a
    required wait gets skipped.

    `asker` is `None` for "work it out" and a seat name (or "") for a caller that
    already has. It used to default to "" — no asker, guard off — so every caller
    but `main()` silently lost the self-challenge rule, which is the one rule
    this feature is built around. **Whatever a caller passes is normalised and
    checked in here**, not at the command line: how a name is spelled must not be
    able to turn the guard off. See the comment at the point it arrives."""
    run_key = uuid.uuid4().hex
    cfg = load_repo_cfg(repo_name)
    repo_name = cfg.get("name") or repo_name
    rev, panel = cfg["reviewers"], cfg["review_panel"]
    selected, override_note = select_reviewers(rev, reviewers)
    # Progress and warnings go to stderr under --json, so stdout is the payload
    # and only the payload — the same rule the review path follows.
    chatter = sys.stderr if json_out else sys.stdout

    notes: list[str] = []
    if "sonarqube" in selected and reviewers:
        # Selectable for a review, and meaningless here: it is a scanner with a
        # rule set, not a correspondent. Said rather than silently dropped —
        # `--reviewers claude,sonarqube` otherwise looks like a two-seat ask.
        # Only when it was ASKED for, though: firing on the resolved set put a
        # permanent warning about a seat nobody tried to ask on every ask in
        # every repo that merely enables sonarqube for its reviews.
        notes.append("sonarqube cannot be asked a question — it scans code against a "
                     "rule set and has no reply to give. Not a seat on this ask.")
    seats = [n for n in LLM_REVIEWERS if n in selected]
    quorum = _ask_rule(panel, "ask_quorum", notes)
    threshold = _ask_rule(panel, "ask_threshold", notes)
    # The unsatisfiable configuration is a rule above the SEAT COUNT, not a
    # threshold above the quorum. Quorum is a minimum, not a maximum: with
    # `ask_quorum: 2`, `ask_threshold: 3` and four seats, three agreeing seats
    # reach the threshold and the ask resolves — so the warning that used to be
    # here fired on configurations that work, and named an invariant that is not
    # one. What can never be reached is a rule no number of seats can satisfy: a
    # one-seat repo with the default quorum of 2 returns `unchallenged` forever,
    # having run and paid for the seat first, and that reads as "nobody checked"
    # rather than as a config that could not have been met.
    unreachable = [f"`ask_{k}` ({v})" for k, v in (("quorum", quorum), ("threshold", threshold))
                   if v > len(seats)]
    if unreachable:
        notes.append(f"{' and '.join(unreachable)} above the {len(seats)} seat"
                     f"{'s' if len(seats) != 1 else ''} on this ask — no answer can reach "
                     "it, so this ask cannot come back as anything but unchallenged or "
                     "unresolved")

    # **The one place an asker enters this function** — detected, normalised and
    # checked here, not at the command line, because that is the only shape of
    # fix this guard has not already been through twice. It was first lost by
    # `ask()` not detecting an asker at all (every caller but `main()` ran with
    # the guard off); it was lost again by `ask()` taking whatever spelling a
    # caller passed, so `"Claude"` or `"claude "` compared a lower-cased seat key
    # against a string that could never equal it and a premise put to itself came
    # back `holds`. A third route in would be a third silent hole, so `main()`'s
    # strip/lower and its seat-name check live HERE and `main()` is one more
    # caller. Anything that is not a seat is refused rather than carried: a name
    # the tally cannot match is a guard that does not fire, and it says so.
    detected = detected_asker()
    if asker is None:
        asker = detected
        if not asker:
            notes.append("no asker was detected — the self-challenge guard is inactive for "
                         "this run. Only Claude Code's environment says which seat is "
                         "running a command; an agent on another vendor's CLI has to pass "
                         "`--asker <seat>` itself")
    else:
        given = str(asker)
        asker = asking_seat(given)
        if asker and asker not in LLM_REVIEWERS:
            notes.append(f"asker {given!r} is not one of {', '.join(LLM_REVIEWERS)} — the "
                         "self-challenge guard is inactive for this run, because a name no "
                         "seat answers to can never match a vote. Recorded as no asker")
            asker = ""
        elif not asker and detected:
            notes.append(f"`--asker ''` was passed while {detected}'s environment is "
                         "present — the self-challenge guard is off by request, so this "
                         "tally may rest entirely on the agent that wrote the premise")

    context_budget = _ask_rule(panel, "ask_max_context_chars", notes)
    context_problems: list[ContextProblem] = []
    read = read_context(Path(cfg["path"]), contexts or [], context_problems, context_budget)
    context = _context_block(read)

    print(f"\n[{repo_name}] premise challenge — {len(seats)} seat"
          f"{'s' if len(seats) != 1 else ''}", file=chatter)
    print(f"  {premise[:120]}\n", file=chatter)

    models = {n: rev.get(n, {}).get("model", SEAT_MODEL_DEFAULTS.get(n, ""))
              for n in LLM_REVIEWERS}
    efforts = {n: rev.get(n, {}).get("effort", "") for n in EFFORTS}

    def prompt_for(budget: int | None) -> str:
        # The budget cuts the file CONTENT inside the block, never the assembled
        # block — see _context_block. Slicing the finished string is how a clamp
        # lands halfway through a `--- CONTEXT: … ---` delimiter.
        return ASK_PROMPT.format(premise=premise,
                                 context=context if budget is None
                                 else _context_block(read, budget))

    # One prompt, shared: it is the same string for every seat, and building it
    # per seat made N copies of every context file to no end.
    base = prompt_for(None)
    prompts = dict.fromkeys(seats, base)

    answers: dict[str, SeatAnswer] = {}
    # `agy`'s prompt travels in argv and the kernel caps one element, whatever is
    # in it — a premise is small but a `--context` file need not be. Same clamp,
    # same report, as the diff gets on a round. The seat is `antigravity`
    # everywhere it is named; `agy` is only the command it runs (see CLI_BIN).
    if "antigravity" in prompts:
        whole = _context_chars(read)
        fitted = fit_argv_budget(prompt_for, whole)
        if fitted < whole:
            notes.append(f"antigravity gets {fitted:,} of {whole:,} context chars "
                         "— its prompt travels in argv and the kernel caps one element "
                         f"at {ARGV_PROMPT_MAX_BYTES:,} bytes")
            prompts["antigravity"] = prompt_for(fitted)
        # The fitting only ever takes CONTEXT out, and the premise and the
        # ASK_PROMPT template have no budget at all — so a long premise leaves a
        # prompt still over the ceiling with nothing left to cut, and
        # `fit_argv_budget` returning 0 is not the same claim as "it fits". Asked
        # of the RENDERED prompt rather than inferred from the reduction, because
        # the alternative is what used to happen: the oversized argv went to
        # execve, `agy` died there with an opaque error, and no note said why.
        # A stated skip is the panel's idiom for a seat that could not be run,
        # and it keeps the seat's absence in the tally instead of in a traceback.
        over = len(prompts["antigravity"].encode()) - ARGV_PROMPT_MAX_BYTES
        if over > 0:
            label = reviewer_label("antigravity", models["antigravity"],
                                   efforts.get("antigravity", ""))
            answers["antigravity"] = SeatAnswer(skip=(
                f"{label}: its prompt is {over:,} bytes over the "
                f"{ARGV_PROMPT_MAX_BYTES:,}-byte argv ceiling with no context left to cut "
                "— `agy` takes a prompt only as one argv element, and the premise alone "
                "does not fit in one"))

    # Only the seats that still need running. A seat the argv check above already
    # settled has its answer, and starting a CLI for a prompt known not to
    # survive exec would spend a turn to arrive at the same skip.
    to_run = [n for n in seats if n not in answers]
    if to_run:
        with ThreadPoolExecutor(max_workers=len(to_run)) as ex:
            tasks = {n: ex.submit(ask_llm, n, models[n], prompts[n], efforts.get(n, ""))
                     for n in to_run}
            for n, fut in tasks.items():
                try:
                    answers[n] = fut.result()
                except Exception as e:  # noqa: BLE001 - one seat never takes the ask down
                    # `run_seat` does filesystem work — a sandbox, temp dirs, an
                    # `os.open` — and ENOSPC or a permission error on any of it
                    # raises outside the err-string path. Re-raised here it took
                    # the whole ask with it: every other seat's finished answer
                    # discarded, no tally, no payload, no --json-file, and a
                    # traceback where the documented exit-0 report should be. The
                    # seat is recorded as not having answered, which is what
                    # happened, and the tally stays honest about it.
                    answers[n] = SeatAnswer(skip=f"{n}: raised {e.__class__.__name__} — {e}")

    tally = ask_tally(answers, quorum, threshold, asker)
    payload = {
        "kind": "ask",
        "repo": repo_name, "github": cfg["github"],
        # The PR this premise is being asked ON BEHALF of, when there is one.
        # Nothing is fetched for it: an ask reads the context it was handed, and
        # a PR number it never opened is a link, not a claim about the PR.
        "pr": pr_number,
        "premise": premise,
        "context": [{"spec": c.spec, "path": c.path, "first": c.first, "last": c.last,
                     "chars": len(c.text)} for c in read],
        # The specs that did NOT become context, machine-readably. "Was this
        # verdict reached with all the context the asker intended?" is the
        # question a later audit (and #77's board row) has to be able to answer,
        # and it could only be answered by string-matching English out of
        # `config_notes` — where these did not belong in the first place: a
        # missing file is not a repo whose configuration wants tuning.
        "context_problems": [{"spec": p.spec, "problem": p.problem} for p in context_problems],
        "asker": asker or None,
        "verdict": tally.verdict,
        "verdict_reason": tally.reason,
        "quorum": quorum,
        "threshold": threshold,
        "answered": tally.answered,
        "counts": tally.counts,
        "seats_selected": sorted(selected),
        "seats_override": override_note,
        # Usage FIRST, so a telemetry key that happens to collide with a primary
        # field (`model`, `verdict`, `reason`, `duration_ms`, …) cannot overwrite
        # what the seat actually answered. Still spread rather than nested,
        # matching the round: a seat whose usage could not be read contributes no
        # keys at all, so the board stores nulls and renders "not recorded"
        # instead of a zero it would average in as a free reviewer.
        "answers": {n: {**(a.usage or {}),
                        "verdict": a.verdict, "reason": a.reason, "gist": a.gist or None,
                        "skip": a.skip, "unreadable": a.unreadable, "absent": a.absent,
                        "model": models[n] or None, "effort": efforts.get(n) or None,
                        "duration_ms": a.duration_ms}
                    for n, a in sorted(answers.items())},
        "config_notes": notes,
        "run_key": run_key,
    }
    write_failed = write_payload(json_file, payload)
    # Not recorded when the local artefact could not be written. The run is about
    # to exit non-zero through `finish(write_failed)`, and a board row for a run
    # its caller was told had failed is two records that disagree about whether
    # this ask happened. (`run()` has the same shape on the review path and is
    # left alone here — it is not what this change is about.)
    if record and not write_failed:
        record_ask(payload)
    if json_out:
        print(json.dumps(payload, indent=2))
        return finish(write_failed)

    # Separated, because "demo#62" reads as one token. The round's heading spells
    # it `PR #<n>` (see `heading` in run()), and one spelling across both reports
    # is one less thing for a reader to parse.
    lines = [f"## Premise challenge — {repo_name}"
             + (f", PR #{pr_number}" if pr_number else ""), ""]
    lines.append(f"**Premise:** {premise}")
    if read:
        lines.append("**Context:** " + ", ".join(
            f"`{c.path}:{c.first}-{c.last}`" if c.first else f"`{c.path}`" for c in read))
    else:
        lines.append("**Context:** none given — the seats answered from the premise alone")
    lines.append("**Seats:** " + (", ".join(reviewer_label(n, models[n], efforts.get(n, ""))
                                            for n in seats) or "none"))
    if asker:
        # Only the seats on THIS ask have a vote to be the only one, so
        # `--reviewers codex --asker claude` gets the other sentence: the first
        # asserts something untrue of the run it is describing.
        lines.append(f"**Asked by:** {asker}" + (
            " — its own answer is one vote and cannot be the only one" if asker in seats
            else " — not a seat on this ask, so it has no vote here"))
    if override_note:
        lines.append(f"  - {override_note}")
    for note in notes:
        lines.append(f"  - ⚠️ config: {note}")
    # Kept apart from the config notes, and labelled for what they are: a reader
    # told that a missing file is a "config" problem goes looking for a key that
    # does not exist, and the remedy for a context that never got read is a
    # different one entirely.
    for problem in context_problems:
        lines.append(f"  - ⚠️ context: {problem.problem}")
    lines.append("")

    # One column per seat, whether or not it answered, because the absences are
    # the part a tally hides: "2 of 2 say it holds" over a four-seat panel is a
    # different sentence from the same words over a two-seat one.
    width = max((len(n) for n in seats), default=0)
    for name in seats:
        a = answers[name]
        if a.verdict:
            lines.append(f"    {name.ljust(width)}  {a.verdict.ljust(11)}"
                         + (f" — {a.reason}" if a.reason else ""))
        elif a.unreadable:
            lines.append(f"    {name.ljust(width)}  ⚠️ no verdict — its reply could not be "
                         "read as one, and is NOT counted as `cannot tell`"
                         + (f" (it said: {a.gist})" if a.gist else ""))
        else:
            lines.append(f"    {name.ljust(width)}  ⚠️ did not answer — {a.skip}")
    arrow = {"holds": "the premise HOLDS", "fails": "the premise FAILS",
             "unresolved": "UNRESOLVED", "unchallenged": "UNCHALLENGED"}[tally.verdict]
    lines.append(f"\n→ **{arrow}** — {tally.reason}")
    if tally.verdict == "unchallenged":
        lines.append("  _An unchallenged premise is not a confirmed one. Read this as "
                     "\"nobody checked\", which is where it started._")
    lines.append("\n_Not a gate: this is a point of order, and it decides nothing on its "
                 "own. It is one question to the seats — no diff was read and no judge "
                 "ruled, so it is evidence about the premise and not a review._")
    print("\n".join(lines))
    return finish(write_failed)


def main() -> int:
    ap = argparse.ArgumentParser(description="Reviewer panel for a PR")
    ap.add_argument("--repo", help="repo path, or a name under ~/source (default: cwd)")
    ap.add_argument("--pr", type=int,
                    help="the PR to review. With --ask, the PR the premise is being "
                         "asked on behalf of — recorded as a link, never fetched")
    ap.add_argument("--ask", metavar="PREMISE",
                    help="challenge one premise instead of reviewing a PR: put this "
                         "yes/no question to the enabled seats, with no diff, no judge "
                         "and no cycle, and print the tally. NOT a gate — it exits 0 on "
                         "every verdict, including `fails`")
    ap.add_argument("--context", action="append", default=[], metavar="PATH[:A-B]",
                    help="a file (or line range) from the repo under review to hand the "
                         "seats with the premise, e.g. harness/loops/panel.py:3500-3560. "
                         "Paths are relative to the REPO ROOT, not to the cwd. Repeatable, "
                         "and capped in total by review_panel.ask_max_context_chars. "
                         "--ask only")
    ap.add_argument("--asker", metavar="SEAT", default=None,
                    help="which seat the agent running this challenge IS, so its own "
                         f"vote cannot be the only one ({', '.join(LLM_REVIEWERS)}). "
                         "Detected from CLAUDE CODE's environment only — an agent on any "
                         "other CLI must pass this itself or the guard does not fire. "
                         "Pass an empty string to say there is no asker. --ask only")
    ap.add_argument("--post", action="store_true", help="post summary as a PR comment")
    ap.add_argument("--json", action="store_true", dest="json_out",
                    help="emit the whole run as JSON on stdout; no report/post")
    ap.add_argument("--reviewers", metavar="LIST",
                    help="comma-separated panel members to run instead of the repo's "
                         f"configured set ({', '.join(ALL_REVIEWERS)}); e.g. "
                         "--reviewers codex for a single-vendor read. "
                         "Default: whatever .harness-rules enables")
    ap.add_argument("--json-file", metavar="PATH", default="", dest="json_file",
                    help="also write the JSON payload here, keeping the report "
                         "(and --post) — unlike --json, which replaces them")
    ap.add_argument("--no-record", action="store_false", dest="record",
                    help="don't record this run on the quarterback board")
    # Defaulted to None rather than 1, so "not passed" and "passed as 1" stay
    # distinguishable. They are the same round to `run()` — resolved to 1 a few
    # lines below — but not to the `--ask` guard: comparing against the default
    # accepted `--ask --round 1` silently, which is a caller believing it asked
    # for something this run does not do.
    ap.add_argument("--round", type=int, default=None, dest="round_no", metavar="N",
                    help="which panel/fix cycle this is (default 1). Round 2+ is the "
                         "re-review of the fix commit — the one nobody reads otherwise")
    ap.add_argument("--baseline", action="append", default=[], metavar="PATH",
                    help="a previous round's --json-file payload, so this run can say "
                         "which findings no earlier round raised. Repeatable")
    ap.add_argument("--scope", choices=ROUND_SCOPES, default="auto",
                    help="what a round past the first REVIEWS. increment: the "
                         "commits since the last round's head, with the rest of the "
                         "PR as context — cheaper as the PR grows, and it is the fix "
                         "commit the cycle exists to read. pr: re-read the whole diff, "
                         "as every release before v2.28 did. auto (default): the repo's "
                         f"review_panel.round_scope, itself defaulting to "
                         f"{DEFAULT_ROUND_SCOPE}. Round 1 is always the whole PR")
    ap.add_argument("--since", default="", metavar="SHA",
                    help="the commit the PREVIOUS round reviewed, for --scope "
                         "increment. Normally unnecessary: it is read from the "
                         "--baseline payload's `head_sha`. Pass it to review a "
                         "specific range, or when the baseline predates that field")
    ap.add_argument("--max-rounds", type=int, default=None,
                    dest="max_rounds", metavar="N",
                    help=f"the CALLER's round cap ({DEFAULT_MAX_ROUNDS} when this run is "
                         "part of a cycle); used to tell a round that stopped because it "
                         "was done from one that stopped because it ran out. Passing it "
                         "is what says this run belongs to a panel -> fix -> panel cycle: "
                         "without it (and without --round/--baseline) the run is a single "
                         "review and reports no rounds. `/panel-review-pr` spells it "
                         "--rounds N and passes it here on every invocation")
    args = ap.parse_args()
    # Validated at the edge, on both paths. A review would have GitHub refuse it
    # eventually; an ask fetches nothing, so nothing else ever looks at this
    # number — `--ask p --pr -5` put `"pr": -5` in the payload as a link for the
    # board to render.
    if args.pr is not None and args.pr < 1:
        raise SystemExit("--pr: pull requests are numbered from 1")
    # The ask is settled before the round flags are validated, because it accepts
    # none of them: an ask that reached those checks would be answering a question
    # about a cycle it is not part of.
    if args.ask is not None:
        if not args.ask.strip():
            raise SystemExit("--ask: the premise is empty — say what is being challenged")
        wrong = [f for f, used in (("--post", args.post),
                                   ("--round", args.round_no is not None),
                                   ("--baseline", bool(args.baseline)),
                                   ("--max-rounds", args.max_rounds is not None)) if used]
        if wrong:
            raise SystemExit(f"--ask does not take {', '.join(wrong)}: an ask is one "
                             "question to the seats, not a round — there is no diff to "
                             "post about, no judge, and no cycle for a baseline to be "
                             "part of")
        asker = asking_seat(args.asker)
        if asker and asker not in LLM_REVIEWERS:
            raise SystemExit(f"--asker: unknown seat {asker!r} — expected one of "
                             f"{', '.join(LLM_REVIEWERS)}, or '' for no asker")
        return ask(args.repo, args.ask.strip(), args.context, args.reviewers,
                   args.pr, args.json_out, args.json_file, args.record,
                   # The NORMALISED value when one was typed, None when none was.
                   # `ask` needs the difference: "nobody, and I mean it" is a
                   # person at a terminal, while "nothing was detected" is an
                   # agent whose CLI this file cannot recognise, and only the
                   # second is worth a note in the report.
                   asker if args.asker is not None else None)
    if args.pr is None:
        raise SystemExit("--pr is required — or pass --ask to challenge one premise "
                         "instead of reviewing a PR")
    for flag, given in (("--context", bool(args.context)),
                        ("--asker", args.asker is not None)):
        if given:
            raise SystemExit(f"{flag} belongs to --ask — a PR review takes neither")
    # The sentinel has done its one job (telling `--ask --round 1` from `--ask`);
    # from here down a round that was not named is round 1, exactly as before.
    round_no = 1 if args.round_no is None else args.round_no
    if round_no < 1:
        raise SystemExit("--round: rounds are numbered from 1")
    if args.max_rounds is not None and args.max_rounds < 1:
        raise SystemExit("--max-rounds: at least one round has to run")
    # Checked against the EFFECTIVE cap, not only against an explicit one. The
    # default is the cap `run()` actually applies, so `--round 3` with no
    # --max-rounds used to pass this guard and then hit the cap branch on the
    # spot — writing "round cap (2) reached … unreviewed" into a round 3 and
    # printing "round 3 of at most 2". That is precisely the corrupted cycle
    # metadata this guard exists to prevent, leaking through the one spelling it
    # did not cover.
    cap = DEFAULT_MAX_ROUNDS if args.max_rounds is None else args.max_rounds
    if round_no > cap:
        default_note = "" if args.max_rounds is not None else \
            " (the default, since --max-rounds was not passed)"
        raise SystemExit(f"--round {round_no} is past --max-rounds "
                         f"{cap}{default_note}: raise the cap, or pass the round "
                         "this run actually is")
    return run(args.repo, args.pr, args.post, args.json_out, args.reviewers,
               args.json_file, args.record, round_no, args.baseline,
               args.max_rounds, args.scope, args.since)


if __name__ == "__main__":
    raise SystemExit(main())
