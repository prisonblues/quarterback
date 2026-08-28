"""The one place "a human has to look at this" is spelled — #279.

The harness forms this judgement in four places and every one of them throws it
away: ``epic.py`` prints a not-agent-doable ruling and keeps a free-text reason,
``panel-review-pr``'s step 3a leaves a ``deferred`` outcome, ``preland`` leaves an
exit code, a panel seat leaves prose in a JSONB list. Four vocabularies, none
shared, none countable — so "how many things are waiting on a human, and what
kind of judgement do they need?" had no answer. #63's watcher is written against
a ``needs-decision`` label that has never existed in any repo here.

This module is the vocabulary those four are meant to converge on. It holds no
state and imports nothing from the app, so a producer, the API, the database
CHECK and the label set all read the same tuple rather than four paraphrases of
it (#65's class of drift, which the board has already paid for once over
``provenance``).

**Not ``could_not_assess``.** The temptation is to reuse the field that already
exists, and ``panel_seats.py``'s own measurement is why it would be wrong: on
PR #160 round 1, nine ``could_not_assess`` declarations asked about a file in
this repo — 47% of that round's veto lines — and all nine were answered with
``grep`` in about four minutes. ``could_not_assess`` means *I lacked context*, a
gap a tool or a wider scope closes. ``needs_human`` means *no context would close
this*. Collapsing them puts a grep-able question and a design decision in one
bucket, and the whole design of that column is that two states which look alike
must not collapse.

**``chore`` says nobody has to judge this, and grants nothing.** The other six
name a kind of judgement a person owes. There was no word for the case where no
judgement is owed at all and the fleet merely is not permitted to act, so those
escalations filed themselves under ``environment`` — measured, ten ``stuck``
posts over two days, every one of them ``environment``, including *"4 pull
requests ready to land and the tip of main was committed 2h 21m ago"*. Nothing
about that was unclear and nobody had to weigh anything; the whole remedy was
``gh pr merge`` four times. It landed in ``environment`` because that was the
least-wrong of six wrong options, and the cost is that the one surface a person
opens fills with things nobody needs to think about — which is how a surface
stops being opened, by the same fleet that has spent a week closing defects
whose shape is *a signal that is present and unread*.

The name is an assertion about the ITEM, not an instruction about what happens
next: *this contains no judgement*. It is deliberately not ``permission``,
``approve`` or ``ready``, all of which were considered and all of which name the
act rather than the item, and so read as arguing for the very thing that is not
decided here. **What the fleet may DO with this class is not settled by this
module and must not be read out of it.** #85, #86, #78 and #335 each settled the
same point from a different direction: the party that classifies must not also
be the party that acts on the classification. A producer says "no judgement is
owed"; nothing in this repo lets that sentence merge, land, deploy or retry
anything. Landing the vocabulary first, and any policy separately and
deliberately, is what keeps those two hands apart.

**A flag costs a reason.** ``#67`` is explicit that an agent must not escalate to
end a cycle it finds tedious, so the rate at which each seat reaches for this is
exactly the thing to measure — which means a bare flag, a confident assertion
with nothing behind it, is refused. Not only in the API: the CHECK constraints
this vocabulary feeds refuse one at the database too, the same rule
``ReviewFindingOutcome``'s ``refuted`` note follows and for the same reason.
"""

from __future__ import annotations

#: The classes where **no reviewer of any kind can settle the question from a
#: diff**. Closed, and constrained in the database as well as here: this feeds a
#: count, and an unknown value would silently leave the numerator while still
#: counting as coverage.
#:
#: ``other`` is the escape hatch and is deliberately last. It is how the
#: vocabulary GROWS — a class that keeps turning up under a word that does not fit,
#: with the same reason, is the evidence for adding one — and it is not a place to
#: file a judgement one of the classes above already names.
#:
#: That rule used to say "under ``other``", and #578 is why it no longer does.
#: ``chore``'s evidence arrived under ``environment``: ten ``stuck`` posts over two
#: days, none of them owing anybody a judgement, all filed as the least-wrong of
#: six. A producer reaching for the nearest real word rather than the hatch is the
#: ORDINARY way a missing class hides, and a growth rule that only watches ``other``
#: watches the quieter half. #328 proposed ``authorisation`` and was refused under
#: the old wording for want of evidence; the refusal was right, and it was right
#: because nothing had turned up anywhere — not because nothing had turned up here.
NEEDS_HUMAN_CLASSES = ("decision", "taste", "ui", "environment", "auth", "chore",
                       "other")

#: What each class means, as the question a diff cannot answer. Published by the
#: API so a producer can discover the vocabulary rather than hardcode a copy of
#: it, and used verbatim as the GitHub label descriptions.
NEEDS_HUMAN_CLASS_HELP = {
    "decision": "which of these, or whether at all — product, architecture, policy",
    "taste": "is this the right name, the right sentence, the right shape",
    "ui": "does it actually look and behave right on a real screen",
    "environment": "does it work on the box it has to work on",
    "auth": "does the credential path actually work, end to end",
    "chore": "nobody has to judge this — the remedy is known and stated in the reason",
    "other": "a human judgement none of the named classes above — say which in the reason",
}

if set(NEEDS_HUMAN_CLASS_HELP) != set(NEEDS_HUMAN_CLASSES):  # pragma: no cover - import guard
    raise RuntimeError(
        "NEEDS_HUMAN_CLASS_HELP does not cover NEEDS_HUMAN_CLASSES: "
        f"{sorted(set(NEEDS_HUMAN_CLASSES) ^ set(NEEDS_HUMAN_CLASS_HELP))}"
    )

#: The GitHub label each class projects onto. A slash-namespaced set rather than
#: one bare ``needs-human``, because a bare "needs a human" says stop without
#: saying who or what for — five ``ui`` checks and one ``decision`` is a different
#: afternoon from six decisions.
#:
#: #229's warning about a second store does not apply: this is not the board
#: keeping its own copy of something GitHub owns, it is a projection of a
#: judgement the board owns onto the surface a human already has open. The board
#: row stays authoritative; the label is how a phone finds it.
LABEL_PREFIX = "needs-human"


def label_for(cls: str) -> str:
    """The GitHub label a class projects onto (``needs-human/ui``)."""
    return f"{LABEL_PREFIX}/{cls}"


#: class -> label, in vocabulary order.
NEEDS_HUMAN_LABELS = {c: label_for(c) for c in NEEDS_HUMAN_CLASSES}

#: Label colours, six digits and no leading ``#`` (what ``gh label create`` wants).
#: Warm for the two a person answers at a desk, cool for the three that need a
#: real screen or a real box, grey for the escape hatch — so a label list sorts
#: into "think about it" and "go and look at it" at a glance on a phone. Amber for
#: ``chore``, which is neither: it is the pile a person clears without thinking,
#: and it wants to be told apart from the six that cost attention rather than
#: ranked among them.
LABEL_COLOURS = {
    "decision": "b60205",
    "taste": "d93f0b",
    "ui": "1d76db",
    "environment": "0e8a16",
    "auth": "5319e7",
    "chore": "fbca04",
    "other": "586069",
}

if set(LABEL_COLOURS) != set(NEEDS_HUMAN_CLASSES):  # pragma: no cover - import guard
    raise RuntimeError(
        "LABEL_COLOURS does not cover NEEDS_HUMAN_CLASSES: "
        f"{sorted(set(NEEDS_HUMAN_CLASSES) ^ set(LABEL_COLOURS))}"
    )

#: The longest reason stored. The same bound ``MAX_NOTE_CHARS`` sets on a
#: refutation, and for the same reason: long enough for the argument itself, and
#: bounded because an authenticated sender is not a bounded one.
MAX_REASON_CHARS = 4000


def needs_human_class_or_none(v: object) -> str | None:
    """One of :data:`NEEDS_HUMAN_CLASSES`, or nothing at all.

    Case and surrounding space are normalised because a producer writing
    ``"UI"`` means the class; nothing else is. A value a consumer *filters on*
    must never be stored verbatim when it is not one the consumer knows — a
    misspelt class would silently leave every by-class count while still reading
    as a flag, which is the direction that hides the signal.

    Returning ``None`` is not the end of it: the caller is expected to report the
    drift (``needs_human_unknown``) rather than swallow it, exactly as the
    provenance path does. Dropping what you do not understand without a word is
    the failure #279 was filed about, so the repair must not ship a quieter
    version of it.
    """
    if not isinstance(v, str):
        return None
    c = v.strip().lower()
    return c if c in NEEDS_HUMAN_CLASSES else None


def needs_human_reason_or_none(v: object) -> str | None:
    """The reason line, or nothing — where "nothing" refuses the flag.

    Whitespace-only collapses to ``None`` deliberately, so the API and the CHECK
    agree on what counts as evidence without the CHECK having to hold a second
    opinion about trimming. A non-string is not a reason: a type-confused
    producer sending ``5`` has said nothing, and coercing it would manufacture
    the very thing this field exists to require.
    """
    if not isinstance(v, str):
        return None
    return v.strip()[:MAX_REASON_CHARS] or None
