#!/usr/bin/env python3
"""Create the ``needs-human/*` labels a human actually reads issues under — #279.

#63's issue watcher is written against a ``needs-decision`` / ``blocked`` label.
``gh label list`` on this repo returns the nine GitHub defaults, unmodified:
nothing has ever created either one, and nothing in ``harness/`` writes a label
at all. #63's most reliable signal is a convention nobody adopted.

This is the projection that makes it real. The board owns the judgement — it is a
row, with a class and a required reason, and ``GET /review/needs-human`` is the
query. The label is the cheapest possible interoperability with the tool the
human already has open, which is why #229's warning about a second store does not
apply: this is not GitHub's fact being copied onto the board, it is the board's
fact being shown where somebody will see it on a phone.

    scripts/needs_human_labels.py list
    scripts/needs_human_labels.py check  --repo owner/name
    scripts/needs_human_labels.py apply  --repo owner/name

``check`` exits non-zero when a label is missing, so it can gate. ``apply`` is
idempotent: ``gh label create --force`` updates an existing label's colour and
description rather than failing, so a rerun after the vocabulary grows a class
brings a repo up to date without a human deciding which of the six to type.

The vocabulary is imported, never restated. A copy here would be the fifth
spelling of "a human has to look at this" and would drift exactly as the four
before it did.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.needs_human import (  # noqa: E402  (path set above)
    LABEL_COLOURS,
    NEEDS_HUMAN_CLASS_HELP,
    NEEDS_HUMAN_CLASSES,
    NEEDS_HUMAN_LABELS,
)


def _gh(args: list[str], repo: str | None) -> subprocess.CompletedProcess:
    cmd = ["gh", *args]
    if repo:
        cmd += ["--repo", repo]
    return subprocess.run(cmd, capture_output=True, text=True)


def existing(repo: str | None) -> set[str]:
    """Every label the repo already has, by name.

    ``--limit`` well past any repo's label list, because ``gh``'s default page is
    30 and a repo past it would report labels missing that are plainly there —
    which ``apply`` would then "fix" by recreating them.
    """
    r = _gh(["label", "list", "--limit", "500", "--json", "name"], repo)
    if r.returncode != 0:
        raise SystemExit(f"gh label list failed: {r.stderr.strip() or r.returncode}")
    return {row["name"] for row in json.loads(r.stdout or "[]")}


def cmd_list(_args: argparse.Namespace) -> int:
    for c in NEEDS_HUMAN_CLASSES:
        print(f"{NEEDS_HUMAN_LABELS[c]:<24} #{LABEL_COLOURS[c]}  {NEEDS_HUMAN_CLASS_HELP[c]}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    have = existing(args.repo)
    missing = [NEEDS_HUMAN_LABELS[c] for c in NEEDS_HUMAN_CLASSES
               if NEEDS_HUMAN_LABELS[c] not in have]
    for name in missing:
        print(f"missing: {name}")
    if missing:
        print(f"{len(missing)} of {len(NEEDS_HUMAN_LABELS)} needs-human labels are missing — "
              "run `scripts/needs_human_labels.py apply`")
        return 1
    print(f"all {len(NEEDS_HUMAN_LABELS)} needs-human labels present")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    have = existing(args.repo)
    for c in NEEDS_HUMAN_CLASSES:
        name = NEEDS_HUMAN_LABELS[c]
        # `--force` so a rerun updates colour and description instead of failing
        # on "already exists". The alternative — skip what is present — leaves a
        # repo whose labels were created before a description changed looking
        # correct and reading wrong.
        r = _gh(["label", "create", name, "--force",
                 "--color", LABEL_COLOURS[c],
                 "--description", NEEDS_HUMAN_CLASS_HELP[c]], args.repo)
        if r.returncode != 0:
            print(f"FAILED {name}: {r.stderr.strip() or r.returncode}", file=sys.stderr)
            return 1
        print(f"{'updated' if name in have else 'created'} {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="print the label set this vocabulary projects onto")
    for name, fn in (("check", cmd_check), ("apply", cmd_apply)):
        p = sub.add_parser(name, help=f"{name} the labels on a repo")
        p.add_argument("--repo", help="owner/name; defaults to the checkout's own remote")
        p.set_defaults(fn=fn)
    sub.choices["list"].set_defaults(fn=cmd_list)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
