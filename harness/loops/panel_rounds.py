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
        # `ran`, not `not absent` (225-R4-F13): a seat that was present and then
        # crashed is "not absent" while having read nothing, so the weaker test let
        # a round where every seat failed still qualify to erase earlier gaps.
        read_something = any(m.get("ran") for m in recorded)
        ran = payload.get("reviewers_ran")
        if isinstance(ran, list) and not ran:
            b.unread_rounds.add(was)
        # `read_something` is the positive evidence `reread` needs and
        # `truncated_any` cannot supply (225-R3-F01). Exempting `absent` from
        # `truncated_any` is right for a round where seats ran, but it makes an
        # ALL-absent round indistinguishable from one that read everything: both
        # come out False. The branch above catches that whenever `reviewers_ran` is
        # a list, which every payload this release writes has — but a hand-edited or
        # truncated one may not, and `reread` is the single most destructive thing
        # in this function: one entry erases every earlier round's recorded gap. So
        # it takes evidence that somebody was actually there, on the same principle
        # the `reread` comment below already states for `recorded`.
        elif (recorded and read_something and not truncated_any
                and str(payload.get("scope") or "pr") == "pr"):
            # `truncated_any`, not `cut`: clearing an earlier gap is a claim that
            # this round READ the region, and a kernel-capped seat did not.
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


#: Everything this module offers, INCLUDING the underscore names — the suites
#: reach for several of them through `panel`, and a plain star import would drop
#: them silently. Generated from the module's own top level, so a helper added here
#: is exported without anyone remembering to list it.
__all__ = [
    "panel_core", "panel_seats", "cluster_findings", "_account",
    "_fold_reports", "_NOT_WORD", "_norm_title", "_key_from_title",
    "_defect_title", "_defect_key", "_finding_id", "Canonical",
    "_unmerged", "_judge_listing", "_parse_verdicts", "adjudicate",
    "REWORD_RATIO", "_TITLE_NOISE", "_stem", "_same_words",
    "Baseline", "_baseline_title", "_SHA_RE", "_mtime",
    "load_baseline", "coverage_veto", "round_stop",
]
