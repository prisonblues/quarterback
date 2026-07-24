"""Subject-overlap scoring for topic-based self-discovery (v2.7).

Two agents working the *same problem from different angles* rarely share an
identical ``cwd`` — but their session titles and recaps describe the same thing.
We score that textual overlap so ``/overlap`` can surface a genuine peer ("server
is also on the merge-test flakiness") instead of pinging every session that
merely shares a big repo. Pure and deterministic — no model, no I/O — so it's
cheap to run in a hook and easy to test.
"""

from __future__ import annotations

import re

# Common English + coordination-domain filler that carries no topic signal. Kept
# small on purpose: the goal is to drop noise words, not to stem or lemmatize.
_STOP_WORDS = """
    a an and the of to in on for with without at by from into over as is are be was were
    this that these those it its it's i we you they them our your their my me
    do does did done doing make makes made making get gets got getting
    fix fixes fixed fixing add adds added adding update updates updated updating
    work works working investigate investigating look looking check checking
    session agent claude code task run running test tests testing issue pr branch repo
"""
_STOP = frozenset(_STOP_WORDS.split())

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str | None) -> set[str]:
    """Lowercase word set, minus stopwords and 1-2 char noise."""
    if not text:
        return set()
    return {t for t in _TOKEN.findall(text.lower()) if len(t) > 2 and t not in _STOP}


def overlap_score(a: str | None, b: str | None) -> float:
    """Overlap coefficient of two subjects: |A∩B| / min(|A|,|B|), in [0, 1].

    The overlap coefficient (not Jaccard) is deliberate: a terse title should
    still match a verbose recap that contains it, without the longer text's
    extra tokens diluting the score toward zero.
    """
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))
