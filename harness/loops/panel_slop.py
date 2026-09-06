"""A deterministic seat, run over the FIX PASS's diff (#780, part of #622).

Every seat this panel has is an LLM, so the cheapest defects to detect — the
ones with a fixed shape — cost a vendor call and a judge ruling like everything
else. That is the wrong price for them, and it is not only a money argument:
across seven PRs and 27 review cycles, **128 of 201 findings were written by the
fix pass that was answering the previous round's findings**, and no cycle ever
converged. The defects an agent writes *while it is trying to satisfy a
reviewer* are not a random sample of defects. They have a very fixed shape,
because the pressure that produces them is always the same one: make the
finding go away, cheaply, in a form a diff reader will accept.

**The population is the fix pass, not the PR.** That is the whole difference
between this and a linter, and it is why this is a seat rather than a lint job
bolted onto CI. A linter reports what a file contains; this reports what the
last pass *wrote in order to answer a review*, which is a far smaller set with a
far higher hit rate and a completely different remedy. :func:`review_fix_pass`
therefore takes the diff `panel_scope._fix_range_diff` already computes for
provenance, and no finding is emitted for a line that diff did not add.

**A fixer cannot talk it out of a finding**, which is the property #622 is
short of. A vendor seat can be persuaded — the fix pass writes prose explaining
why the shape is fine, the next round's seat reads that prose, and the finding
is gone without the code changing. There is no prose channel here. Rules in,
diff in, findings out.

What this is NOT
----------------
There is no authorship classifier and no slop score, and `tests/test_panel_slop.py`
fails the build if a rule's text acquires one. "Written by a model" is not a
defect and is not detectable; the panel reviews code the fleet's agents wrote
almost exclusively, so a rule that scored authorship would score every line and
rank nothing. Each rule names ONE concrete defect and ONE remediation, or it
does not ship. Anything that needs a judgement about intent belongs to the LLM
seats, which is what they are good at and what they cost money for.

Shape of the rules
------------------
One flat YAML mapping per file in `slop_rules/`, with a **closed** key set and a
`match.kind` from a **closed** vocabulary. Validation is strict and every
failure is fatal at load: a missing id, missing languages, an unknown key, an
unknown match kind and a duplicate key all raise. Nothing is skipped. A rule
file that does not load is an operator error about a checked-in file, and
degrading it to "that rule quietly did not run" is how a seat comes to report
clean because half of it was misspelt.

Six keys, and every one of them is load-bearing: `id`, `severity`, `languages`,
`message`, `remediation`, `match`. Deliberately NOT `confidence` or `category`,
which the analyzer this borrows its discipline from carries — :class:`Finding`
has neither field, so both would be schema that nothing downstream can read.
Per-finding confidence in this panel is #78's corroboration count, which is a
property of how many seats raised a defect and is not something one seat may
declare about itself.

What parses what
----------------
**Python only, via the standard library's `ast`.** Checked before assuming
otherwise: `pyproject.toml` has no tree-sitter and no parser of any kind, and
adding one is not free here — the nix `loops-tests` check in flake.nix runs this
suite under a python holding **pytest and nothing else**, and it fails the build
on any skip. So a rule needing a parser this repo does not ship would not
degrade to a skip, it would be a collection error wearing a red badge, and the
"install it and it gets better" story would never be true in the sandbox that
matters. `ast` is always there, it is exact, and Python is the language this
repo's fix passes are written in.

That constraint reaches the rule files too. They are read by
:func:`_parse_rule_document`, a strict reader for the flat subset the rules
actually use — plain scalars, single-quoted scalars, one inline list, one level
of nesting — because **PyYAML is not in `pyproject.toml` either**. It arrives
transitively through `uvicorn[standard]` and is therefore present in a
developer's venv and in the GitHub harness job, and absent in exactly the
sandbox that forbids skipping. Every file in `slop_rules/` is nonetheless valid
YAML that `yaml.safe_load` reads identically, and must stay that way: the
subset is a reading restriction, not a dialect.

Purity
------
No network, no clock, no filesystem walk of its own, no module state. The rules
are loaded once by a caller and passed in; the diff is passed in; the post-image
sources are passed in as a mapping. Everything here is a function of its
arguments, which is what makes the fixtures in `tests/fixtures/slop/` a complete
statement of the behaviour.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from panel_core import SEVERITIES, Finding, ReviewerRun

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

#: The name this seat reports under — :attr:`Finding.reviewer`, and the key an
#: operator writes in `reviewers` in the rules file. `sonarqube` is the
#: precedent: a member of `ALL_REVIEWERS` that is not in `LLM_REVIEWERS`, so it
#: is selectable and produces findings and is never dispatched to a CLI.
SEAT = "slop"

#: Where the rule files live, beside this module rather than under a config
#: directory: they are code that happens to be data, they are reviewed as code,
#: and `flake.nix` copies `harness/loops` in as a tree, so they travel into the
#: sandbox for free.
RULES_DIR = Path(__file__).resolve().parent / "slop_rules"

#: The closed vocabulary. A `match.kind` outside this set is a load error, not a
#: skipped rule — see the module docstring on why nothing here degrades quietly.
#: Two regex kinds and five matchers over the `ast` tree, and the split is not
#: cosmetic: a regex kind can be added by writing a YAML file, an ast kind needs
#: a function here and a reviewer to read it.
MATCH_KINDS = frozenset({
    "line_regex",
    "comment_regex",
    "python_placeholder_body",
    "python_swallowed_error",
    "python_sentinel_catch",
    "python_pass_through_wrapper",
    "python_unused_import",
})

#: The kinds that need `match.pattern`; for the rest a pattern is a load error,
#: because a pattern nothing reads is a rule whose author believed it was doing
#: something it was not.
_PATTERN_KINDS = frozenset({"line_regex", "comment_regex"})

_REQUIRED_KEYS = ("id", "severity", "languages", "message", "remediation", "match")
_MATCH_KEYS = frozenset({"kind", "pattern"})

#: Suffix to the language name in `languages`. One entry, and the field is kept
#: anyway: it is the gate that stops `python_swallowed_error` being run over a
#: shell script the day this grows a second language, and a gate added after the
#: rules exist is a gate somebody has to remember to apply to all of them.
_LANGUAGE_BY_SUFFIX = {".py": "python", ".pyi": "python"}

#: The broad catches :data:`slop/swallowed-error` and :data:`slop/sentinel-catch`
#: fire on. Everything narrower is left alone deliberately — see those rule
#: files for the argument.
_BROAD_EXCEPTIONS = frozenset({"Exception", "BaseException"})

#: Decorators that make an empty body the point rather than an omission.
_ABSTRACT_DECORATORS = frozenset({"abstractmethod", "abstractproperty", "overload",
                                  "abstractclassmethod", "abstractstaticmethod"})


class SlopRuleError(ValueError):
    """A rule file that does not load. Fatal by design — see the module docstring."""


@dataclass(frozen=True)
class SlopRule:
    """One rule, as loaded. Frozen because the matchers take it as an argument and
    a rule that a matcher could edit is module state with extra steps."""

    rule_id: str
    source: str
    severity: str
    languages: frozenset[str]
    message: str
    remediation: str
    kind: str
    pattern: str | None
    compiled: re.Pattern[str] | None


@dataclass(frozen=True)
class _Hit:
    """One rule firing at one line, with the text that proves it.

    The evidence travels because a finding whose detail is only the rule's own
    message is a finding the fixer has to go and reconstruct. `line` is a
    post-image line number, which is what :attr:`Finding.line` means and what the
    diff's `@@` header counts in.
    """

    line: int
    evidence: str


@dataclass(frozen=True)
class ChangedFile:
    """One file as the fix pass left it, from that pass's diff alone.

    `added` is the population — post-image line numbers the fix pass wrote —
    and it is the whole reason this is a seat and not a linter. `lines` is every
    post-image line the diff showed, added and context alike, which is enough
    for the line and comment rules and not enough for the ast ones.
    """

    path: str
    added: frozenset[int]
    lines: dict[int, str]


# ------------------------------------------------------------------ loading rules


def load_rules(rules_dir: Path | None = None) -> tuple[SlopRule, ...]:
    """Every rule in `slop_rules/`, sorted by id so a report's order is stable.

    Raises :class:`SlopRuleError` on the first bad file. An empty directory also
    raises: a rules directory with nothing in it means the tree was copied
    without its data, and the alternative — a seat that reports clean because it
    has no rules — is the exact failure mode this repo's flake check is built to
    refuse elsewhere ("a suite that disappears has to be as loud as a suite that
    fails").
    """
    root = rules_dir if rules_dir is not None else RULES_DIR
    if not root.is_dir():
        raise SlopRuleError(f"no rules directory at {root}")
    rules = [_rule_from_document(_parse_rule_document(p.read_text(encoding="utf-8"), p.name),
                                 p.name)
             for p in sorted(root.glob("*.yaml"))]
    if not rules:
        raise SlopRuleError(f"no rule files in {root} — the seat would report clean with no rules")
    seen: dict[str, str] = {}
    for rule in rules:
        if rule.rule_id in seen:
            raise SlopRuleError(f"rule id {rule.rule_id!r} is in both {seen[rule.rule_id]} "
                                f"and {rule.source}")
        seen[rule.rule_id] = rule.source
    return tuple(sorted(rules, key=lambda r: r.rule_id))


def _parse_rule_document(text: str, source: str) -> dict[str, object]:
    """The flat YAML subset the rule files use, read strictly.

    Not `yaml.safe_load`, for the reason in the module docstring: PyYAML is not a
    declared dependency and is missing from the one sandbox that forbids a skip.
    The subset is deliberately tiny — top-level `key: value`, one level of
    two-space nesting under `match`, plain and single-quoted scalars, one inline
    list — and every file in `slop_rules/` is also valid YAML, checked by eye and
    kept that way, so swapping this out later is a deletion rather than a
    migration.

    **Nothing is implicitly typed.** Real YAML would read `no` as False and
    `1.0` as a float; here every scalar is a string, because a rule's fields are
    all strings and implicit typing on a field like `severity` is a footgun with
    no upside.
    """
    doc: dict[str, object] = {}
    block: dict[str, str] | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            # A comment is only a comment when `#` is its first non-whitespace
            # token — the same rule the comment matcher applies to Python — so a
            # `#` inside a quoted regex is left alone.
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent not in (0, 2):
            raise SlopRuleError(f"{source}:{lineno}: indent must be 0 or 2, got {indent}")
        key, sep, value = raw.strip().partition(":")
        if not sep:
            raise SlopRuleError(f"{source}:{lineno}: expected `key: value`, got {raw.strip()!r}")
        key = key.strip()
        if not re.fullmatch(r"[a-z_]+", key):
            raise SlopRuleError(f"{source}:{lineno}: {key!r} is not a rule key")
        parsed = _parse_scalar(value.strip(), source, lineno)
        if indent == 2:
            if block is None:
                raise SlopRuleError(f"{source}:{lineno}: {key!r} is indented under nothing")
            if key in block:
                raise SlopRuleError(f"{source}:{lineno}: {key!r} is written twice")
            if parsed is None:
                raise SlopRuleError(f"{source}:{lineno}: {key!r} needs a value")
            block[key] = parsed  # type: ignore[assignment]
            continue
        if key in doc:
            raise SlopRuleError(f"{source}:{lineno}: {key!r} is written twice")
        if parsed is None:
            # An empty value opens a nested block. `match` is the only one there
            # is; anything else is a typo that would otherwise swallow the lines
            # under it and report a missing key several lines later.
            if key != "match":
                raise SlopRuleError(f"{source}:{lineno}: {key!r} has no value")
            block = {}
            doc[key] = block
            continue
        block = None
        doc[key] = parsed
    return doc


def _parse_scalar(value: str, source: str, lineno: int) -> str | list[str] | None:
    """A plain scalar, a single-quoted scalar, an inline list, or None for "opens a block"."""
    if not value:
        return None
    if value.startswith("["):
        if not value.endswith("]"):
            raise SlopRuleError(f"{source}:{lineno}: an inline list must close on its own line")
        return [item.strip() for item in value[1:-1].split(",") if item.strip()]
    if value.startswith("'"):
        # Single quotes only, and doubling is the one escape — which is real
        # YAML's rule for them. Double-quoted scalars are refused rather than
        # half-supported: they process backslash escapes, every pattern in
        # `slop_rules/` is full of backslashes, and a reader that got that
        # subtly wrong would change what a rule matches without failing.
        if not value.endswith("'") or len(value) < 2:
            raise SlopRuleError(f"{source}:{lineno}: unterminated quoted value")
        return value[1:-1].replace("''", "'")
    if value.startswith('"'):
        raise SlopRuleError(f"{source}:{lineno}: use single quotes — double-quoted scalars "
                            "process backslash escapes and every pattern here is backslashes")
    return value


def _rule_from_document(doc: dict[str, object], source: str) -> SlopRule:
    """Strict validation. Every branch here raises; none of them drops a rule."""
    unknown = sorted(set(doc) - set(_REQUIRED_KEYS))
    if unknown:
        raise SlopRuleError(f"{source}: unknown key(s) {', '.join(unknown)}")
    missing = [k for k in _REQUIRED_KEYS if k not in doc]
    if missing:
        raise SlopRuleError(f"{source}: missing {', '.join(missing)}")

    rule_id = doc["id"]
    if not isinstance(rule_id, str) or not rule_id.startswith("slop/"):
        raise SlopRuleError(f"{source}: id must be a string starting `slop/`, got {rule_id!r}")

    severity = doc["severity"]
    if severity not in SEVERITIES:
        # The panel's own vocabulary, not a private one mapped at the boundary.
        # A band outside it sorts on a lexical accident and lands in a bucket
        # nothing counts — `panel_core.SEVERITIES` says so at length.
        raise SlopRuleError(f"{source}: severity must be one of {', '.join(SEVERITIES)}, "
                            f"got {severity!r}")

    languages = doc["languages"]
    if not isinstance(languages, list) or not languages:
        raise SlopRuleError(f"{source}: languages must be a non-empty list")
    unsupported = sorted(set(languages) - set(_LANGUAGE_BY_SUFFIX.values()))
    if unsupported:
        raise SlopRuleError(f"{source}: no matcher speaks {', '.join(unsupported)}")

    for key in ("message", "remediation"):
        text = doc[key]
        if not isinstance(text, str) or not text.strip():
            raise SlopRuleError(f"{source}: {key} must be a non-empty string")

    match = doc["match"]
    if not isinstance(match, dict):
        raise SlopRuleError(f"{source}: match must be a block")
    stray = sorted(set(match) - _MATCH_KEYS)
    if stray:
        raise SlopRuleError(f"{source}: unknown match key(s) {', '.join(stray)}")
    kind = match.get("kind")
    if kind not in MATCH_KINDS:
        raise SlopRuleError(f"{source}: match.kind {kind!r} is not one of "
                            f"{', '.join(sorted(MATCH_KINDS))}")
    pattern = match.get("pattern")
    if kind in _PATTERN_KINDS and not pattern:
        raise SlopRuleError(f"{source}: match.kind {kind} needs match.pattern")
    if kind not in _PATTERN_KINDS and pattern:
        raise SlopRuleError(f"{source}: match.kind {kind} takes no match.pattern, "
                            "and one written here would never be read")
    try:
        compiled = re.compile(pattern) if pattern else None
    except re.error as exc:
        raise SlopRuleError(f"{source}: match.pattern does not compile ({exc})") from exc

    return SlopRule(
        rule_id=rule_id,
        source=source,
        severity=str(severity),
        languages=frozenset(str(item) for item in languages),
        message=str(doc["message"]).strip(),
        remediation=str(doc["remediation"]).strip(),
        kind=str(kind),
        pattern=pattern,
        compiled=compiled,
    )


# -------------------------------------------------------------- reading the diff


def parse_diff(diff: str) -> tuple[ChangedFile, ...]:
    """The fix pass's diff, as post-image line numbers per file.

    Written against the diff this panel actually produces:
    `panel_scope._fix_range_diff` glues the compare API's per-file `patch` under
    a synthesised `diff --git a/x b/x` header, so there are no `---`/`+++` lines
    to read the path from and the header is the only place it appears. A plain
    `git diff` carries both and is handled by the same reader.

    Renames and deletions land here as a file whose new path has no added lines,
    which is the right answer rather than a special case: this seat reports what
    the pass WROTE, and there is nothing written at a path that no longer exists.
    """
    files: list[ChangedFile] = []
    path: str | None = None
    added: set[int] = set()
    lines: dict[int, str] = {}
    new_no = 0

    def flush() -> None:
        if path is not None:
            files.append(ChangedFile(path=path, added=frozenset(added), lines=dict(lines)))

    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            flush()
            path, added, lines, new_no = _path_from_git_header(raw), set(), {}, 0
            continue
        if raw.startswith("+++ "):
            # A real `git diff` names the post-image here, and it is the more
            # trustworthy of the two: the `diff --git` header is ambiguous for
            # paths containing " b/", which this splits on.
            named = _path_from_marker(raw)
            if named is not None:
                path = named
            continue
        if raw.startswith("--- "):
            continue
        if raw.startswith("@@"):
            new_no = _hunk_start(raw)
            continue
        if path is None or new_no == 0:
            continue
        if raw.startswith("+"):
            added.add(new_no)
            lines[new_no] = raw[1:]
            new_no += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        elif raw.startswith(" ") or raw == "":
            lines[new_no] = raw[1:] if raw else ""
            new_no += 1
    flush()
    return tuple(files)


def _path_from_git_header(raw: str) -> str | None:
    rest = raw[len("diff --git "):].strip()
    marker = " b/"
    at = rest.rfind(marker)
    if at < 0:
        return None
    return rest[at + len(marker):].strip() or None


def _path_from_marker(raw: str) -> str | None:
    name = raw[4:].split("\t", 1)[0].strip()
    if name in ("/dev/null", ""):
        return None
    return name[2:] if name.startswith("b/") else name


def _hunk_start(raw: str) -> int:
    """The first post-image line number a `@@ -a,b +c,d @@` hunk covers, or 0.

    0 for an unreadable header rather than a raise: a diff this cannot count in
    costs its findings, never the round — the same trade
    `panel_scope._fix_range_diff` makes for every other way a range goes
    unreadable.
    """
    match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
    return int(match.group(1)) if match else 0


# --------------------------------------------------------- the two guard devices


def _code_view(line: str) -> str:
    """A line with everything a code rule must not read blanked out.

    Three devices, and they are the reason a rule set like this is usable at all
    rather than a noise generator. All three were measured rather than guessed:
    run over every `.py` file in this repo as if one fix pass had written it, the
    line rules raised four findings and all four were this module's own prose
    describing the rule that fired.

    **String literals go**, so `slop/type-erasure` does not fire on a docstring
    that spells out `-> Any` as an example, and a prose sentence quoting a
    pattern is not evidence of that pattern.

    **Backtick spans go with them.** Not a Python construct — a prose one, and
    the one this repo actually writes: comments here quote code inline as
    `` `# noqa` ``, and every self-inflicted finding in that measurement was of
    that form. A rule set whose own explanation trips it is a rule set nobody
    believes.

    **A WHOLE-LINE comment goes**, and a trailing one does not. The asymmetry is
    load-bearing in both directions: `slop/bare-lint-suppression` matches
    `x = y()  # noqa`, which only ever appears trailing, so trailing comments
    have to stay visible — while a line that is nothing but prose is not code,
    and a code rule reading it is a rule that fires on this file's own
    docstrings.

    What this cannot see from one line is a MULTI-line string; the lines inside
    one are handled in :func:`apply_rules`, where the parse tree knows where they
    start and end.
    """
    if line.lstrip().startswith("#"):
        return ""
    return re.sub(r"""([\'"`])(?:\\.|(?!\1).)*\1""", '""', line)


def _comment_continuations(changed: ChangedFile) -> frozenset[int]:
    """Lines that continue a comment block rather than open one.

    The fourth guard, and the one that took `slop/step-comment` from 24 findings
    over this repo to a handful. Every comment rule here names a CAPTION — text
    written against the line of code below it — and a numbered item three lines
    into a paragraph of argument is not a caption, it is prose. `harness_rules`
    enumerates the three things a per-box overlay may do as `#  1.`, `#  2.`,
    `#  3.` inside one block; `panel_rounds` numbers the clauses of a rule the
    same way. Both are exactly the writing this repo asks for, and a rule that
    calls them step comments is a rule that reads the house style as a defect.

    So only the FIRST comment line of a run is looked at. A block that genuinely
    is narration still reports — on its opening line, once, rather than once per
    line, which is also the better finding.
    """
    return frozenset(n for n in changed.lines
                     if _comment_view(changed.lines.get(n - 1, "")) is not None)


def _comment_view(line: str) -> str | None:
    """The line's text when it is a comment line, else None.

    A comment only when `#` is the **first** non-whitespace token. Trailing
    comments are invisible to every comment rule, and that is the point: the
    defects these rules name — narration, captioning, recorded unfinished
    work — are all things written on a line of their own. Reading
    trailing comments would put `foo()  # step 2 of the retry` in front of
    `slop/step-comment`, and an explanatory aside is not the defect.
    """
    stripped = line.strip()
    return stripped if stripped.startswith("#") else None


# ---------------------------------------------------------------- the ast rules


def _placeholder_body(tree: ast.AST, source_lines: Sequence[str]) -> Iterator[_Hit]:
    """A function whose body, docstring aside, is only `pass`, `...`, or a
    `raise NotImplementedError`.

    The docstring is stripped before counting, deliberately and unlike the
    analyzer this borrows from. A stub carrying a paragraph about what it will
    one day do is the *characteristic* fix-pass shape — the prose is what makes
    it read as finished work in a diff — and a matcher that counts the docstring
    as a statement misses exactly the population this seat exists for.
    """
    for node, in_protocol in _functions_in_scope(tree):
        if in_protocol or _has_abstract_decorator(node):
            continue
        body = _body_without_docstring(node.body)
        if len(body) != 1:
            continue
        only = body[0]
        if isinstance(only, ast.Pass):
            yield _Hit(node.lineno, f"def {node.name}(...): its body is only `pass`")
        elif isinstance(only, ast.Expr) and _is_ellipsis(only.value):
            yield _Hit(node.lineno, f"def {node.name}(...): its body is only `...`")
        elif isinstance(only, ast.Raise) and _raises_not_implemented(only):
            yield _Hit(node.lineno,
                       f"def {node.name}(...): its body only raises NotImplementedError")


def _swallowed_error(tree: ast.AST, source_lines: Sequence[str]) -> Iterator[_Hit]:
    """A broad `except` whose body, docstring aside, is only `pass`."""
    for handler in _broad_handlers(tree):
        body = _body_without_docstring(handler.body)
        if len(body) == 1 and isinstance(body[0], ast.Pass):
            yield _Hit(handler.lineno, f"{_handler_text(handler)}: pass")


def _sentinel_catch(tree: ast.AST, source_lines: Sequence[str]) -> Iterator[_Hit]:
    """A broad `except` whose body, docstring aside, is one `return` of a falsy constant.

    "Falsy constant" and not "None" alone, because the sentinel a fixer reaches
    for follows the function's return type: `return None`, `return False`,
    `return ""`, `return []`. Every one of them collapses "it failed" into "there
    was nothing", and the empty container is the worst of the set — it flows on
    through a loop that then runs zero times and reports success.
    """
    for handler in _broad_handlers(tree):
        body = _body_without_docstring(handler.body)
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            continue
        value = body[0].value
        if _is_falsy_literal(value):
            shown = "return" if value is None else f"return {ast.unparse(value)}"
            yield _Hit(body[0].lineno, f"{_handler_text(handler)}: {shown}")


def _pass_through_wrapper(tree: ast.AST, source_lines: Sequence[str]) -> Iterator[_Hit]:
    """A function whose whole body returns one call forwarding its own parameters.

    Strictly positional, no `*args`, no keywords, no defaults consumed, and the
    forwarded names in the parameters' own order. Every one of those is a
    narrowing that costs real matches and buys the rule its credibility: a
    wrapper that reorders, renames, defaults or filters is doing something, and
    `**kwargs` forwarding is the shape of adapters and decorators, which are the
    legitimate half of this population.

    A decorated function is skipped for the same reason — the decorator IS the
    behaviour, and the body forwarding cleanly is what a good one looks like.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.decorator_list:
            continue
        params = _positional_parameters(node.args)
        if not params:
            continue
        body = _body_without_docstring(node.body)
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            continue
        call = body[0].value
        if not isinstance(call, ast.Call) or call.keywords:
            continue
        forwarded = [arg.id for arg in call.args if isinstance(arg, ast.Name)]
        if len(forwarded) != len(call.args):
            continue
        # `self`/`cls` may be dropped on the way through — a method forwarding to
        # a module-level helper is still a pass-through — but nothing else may.
        accepted = [params] + ([params[1:]] if params[0] in ("self", "cls") else [])
        # A method with no parameters but `self`, calling something else with
        # none either, forwards nothing and is not a pass-through — it is an
        # accessor. Found by the whole-repo measurement, where `def _dial_name(self):
        # return (...).strip()` matched the empty-list case exactly.
        if not forwarded or forwarded not in accepted:
            continue
        callee = _callee_name(call.func)
        if callee is None or callee == node.name:
            continue
        yield _Hit(node.lineno, f"def {node.name}(...) -> {ast.unparse(call)}")


def _unused_import(tree: ast.AST, source_lines: Sequence[str]) -> Iterator[_Hit]:
    """A name bound by an import and referenced nowhere else in the module.

    Three suppressions, each answering a way this repo legitimately imports
    something it does not call:

    * a `# noqa` on the import line — the house convention for a side-effecting
      or re-exported import, written with its reason beside it, and the reason
      `F401` is in pyproject.toml's ignore list at all;
    * a name listed in `__all__`, which is a use;
    * `from x import *`, which binds names this cannot see.

    Usage is read off the tree rather than by regex over the text, so a name
    that appears only inside a comment or a docstring does NOT count as used —
    that is the case a text scan gets wrong, and it gets it wrong in the
    direction that hides a genuinely dead import. String annotations are the
    price: `def f() -> "Widget"` with `Widget` imported only for that would be
    reported. Rare enough, and the `# noqa` road out is the same one the repo
    already uses.
    """
    bound: list[tuple[str, int]] = []
    star = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            # `from __future__ import annotations` binds a name that is never
            # written again by design — it is a compiler directive wearing an
            # import's syntax. 158 of the 383 findings in the whole-repo
            # measurement were this one line, which is a rule that would have
            # been switched off on its first round.
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import x.y` binds `x`; `import x.y as z` binds `z` and NOT `x`,
                # so `x.y.thing()` after it is a NameError rather than a use.
                bound.append((alias.asname or alias.name.split(".", 1)[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    star = True
                    continue
                bound.append((alias.asname or alias.name, node.lineno))
    if star or not bound:
        return

    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    used |= _dunder_all_names(tree)
    for name, lineno in bound:
        if name in used:
            continue
        line = source_lines[lineno - 1] if 0 < lineno <= len(source_lines) else ""
        if "# noqa" in line or "#noqa" in line:
            continue
        yield _Hit(lineno, f"`{name}` is imported and never used")


#: Dispatch for the five ast kinds. One signature for all of them — the tree and
#: the file's lines — even though only `python_unused_import` reads the lines
#: today, because the alternative is a dispatch site that has to know which
#: matcher takes what, and that is where a sixth rule gets wired up wrong.
_AST_MATCHERS = {
    "python_placeholder_body": _placeholder_body,
    "python_swallowed_error": _swallowed_error,
    "python_sentinel_catch": _sentinel_catch,
    "python_pass_through_wrapper": _pass_through_wrapper,
    "python_unused_import": _unused_import,
}


# ------------------------------------------------------------------ ast helpers


def _functions_in_scope(tree: ast.AST, in_protocol: bool = False
                        ) -> Iterator[tuple[ast.FunctionDef | ast.AsyncFunctionDef, bool]]:
    """Every function, paired with whether it sits inside a `Protocol` body.

    A descent rather than :func:`ast.walk` because that flag needs the enclosing
    class, which `walk` throws away. It exists for one guard and the guard is not
    optional: in a Protocol the ellipsis body IS the declaration — there is
    nothing to implement, by design — and a placeholder rule that fires on
    structural typing is a rule that fires on correct code in every typed
    codebase it meets. `@abstractmethod` covers the ABC half of the same idea;
    `Protocol` has no decorator to check, only the base class.
    """
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            protocol = in_protocol or any(_callee_name(base) == "Protocol" for base in node.bases)
            yield from _functions_in_scope(node, protocol)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node, in_protocol
            # Nested functions inherit nothing from a Protocol above them — a
            # closure defined inside a protocol method is ordinary code.
            yield from _functions_in_scope(node, False)
        else:
            yield from _functions_in_scope(node, in_protocol)


def _multiline_string_lines(tree: ast.AST) -> frozenset[int]:
    """Every line covered by a string literal that spans more than one of them.

    :func:`_code_view` blanks a string it can see the whole of on one line, and a
    docstring is exactly the case it cannot: its lines carry no quote character
    at all, so from one line a paragraph of prose about `-> Any` and a line of
    code annotating `-> Any` are indistinguishable. The parse tree knows, and
    this is the only thing it is asked.

    Whole lines rather than character spans, first line included. Code sharing a
    line with the OPENING of a triple-quoted string is vanishingly rare, and the
    cost of getting it wrong is one missed finding against a whole class of
    findings against this repo's own prose.
    """
    covered: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, (str, bytes)):
            continue
        end = node.end_lineno or node.lineno
        if end > node.lineno:
            covered.update(range(node.lineno, end + 1))
    return frozenset(covered)


def _body_without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def _is_ellipsis(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is Ellipsis


def _is_falsy_literal(node: ast.expr | None) -> bool:
    if node is None:
        return True
    if isinstance(node, ast.Constant):
        return node.value is None or node.value in (False, 0, "", b"")
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return not node.elts
    return isinstance(node, ast.Dict) and not node.keys


def _raises_not_implemented(node: ast.Raise) -> bool:
    exc = node.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    return _callee_name(exc) == "NotImplementedError"


def _has_abstract_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = target.attr if isinstance(target, ast.Attribute) else _callee_name(target)
        if name in _ABSTRACT_DECORATORS:
            return True
    return False


def _broad_handlers(tree: ast.AST) -> Iterator[ast.ExceptHandler]:
    """Handlers catching everything: bare `except:`, `except Exception`, or a tuple
    with one of those in it."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        caught = node.type
        if (caught is None
                or _callee_name(caught) in _BROAD_EXCEPTIONS
                or (isinstance(caught, ast.Tuple)
                    and any(_callee_name(i) in _BROAD_EXCEPTIONS for i in caught.elts))):
            yield node


def _handler_text(handler: ast.ExceptHandler) -> str:
    if handler.type is None:
        return "bare `except:`"
    return f"`except {ast.unparse(handler.type)}`"


def _callee_name(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _positional_parameters(args: ast.arguments) -> list[str] | None:
    """The positional parameter names, or None when the signature is not a plain one."""
    if args.vararg or args.kwarg or args.kwonlyargs or args.defaults or args.kw_defaults:
        return None
    return [a.arg for a in (*args.posonlyargs, *args.args)]


def _dunder_all_names(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            out |= {e.value for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return out


# ---------------------------------------------------------------------- the seat


def apply_rules(*, changed: ChangedFile, source: str | None,
                rules: Sequence[SlopRule]
                ) -> tuple[list[tuple[SlopRule, _Hit]], list[tuple[SlopRule, str]]]:
    """Every hit on one file paired with the rule that made it, plus the rules that
    could not be run on it at all.

    The second half of the return is not decoration. An ast rule needs the whole
    post-image, and a diff carries only its hunks; when `source` is None the ast
    rules are skipped, and this says which ones, so
    :func:`review_fix_pass` can put them in `could_not_assess`. This repo's own
    rule, from `panel_scope`: every fallback is stated rather than silent,
    because a seat that claims it read something it could not is wrong about the
    one thing it exists to measure.
    """
    language = _LANGUAGE_BY_SUFFIX.get(Path(changed.path).suffix.lower())
    if language is None:
        return [], []
    applicable = [r for r in rules if language in r.languages]

    tree: ast.AST | None = None
    source_lines: list[str] = []
    parse_failure: str | None = None
    if source is not None:
        source_lines = source.splitlines()
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError) as exc:
            # A file the fix pass left unparseable is a much bigger problem than
            # anything here would report, and it is CI's to report. This costs
            # its ast findings and says so; it never takes the round down.
            parse_failure = f"{type(exc).__name__}"

    prose = _multiline_string_lines(tree) if tree is not None else frozenset()
    hits: list[tuple[SlopRule, _Hit]] = []
    unavailable: list[tuple[SlopRule, str]] = []
    for rule in applicable:
        if rule.kind in _AST_MATCHERS:
            if tree is None:
                why = (f"the file could not be parsed ({parse_failure})" if parse_failure
                       else "only the fix pass's hunks were available, not the whole file")
                unavailable.append((rule, why))
                continue
            found = _AST_MATCHERS[rule.kind](tree, source_lines)
        elif rule.kind == "line_regex":
            found = _regex_hits(rule, changed, view=_code_view, skip=prose)
        else:
            found = _regex_hits(rule, changed, view=_comment_view,
                                skip=prose | _comment_continuations(changed))
        # THE population filter, and the one line that makes this a seat rather
        # than a linter: an ast rule sees the whole file and may fire anywhere in
        # it, and only what the fix pass actually wrote is reported.
        hits.extend((rule, hit) for hit in found if hit.line in changed.added)
    return hits, unavailable


def _regex_hits(rule: SlopRule, changed: ChangedFile,
                view: Callable[[str], str | None],
                skip: frozenset[int]) -> Iterator[_Hit]:
    for lineno in sorted(changed.added):
        if lineno in skip:
            continue
        text = view(changed.lines.get(lineno, ""))
        if text and rule.compiled is not None and rule.compiled.search(text):
            yield _Hit(lineno, changed.lines[lineno].strip()[:200])


def review_fix_pass(*, diff: str, sources: Mapping[str, str] | None = None,
                    rules: Sequence[SlopRule] | None = None) -> ReviewerRun:
    """The seat: a fix pass's diff in, a :class:`ReviewerRun` out.

    `diff` is the range between two rounds — what `panel_scope._fix_range_diff`
    returns, and NOT `gh pr diff`. Handing it the PR's diff would still work and
    would be the wrong instrument: it would report the whole branch's placeholder
    stubs at every round, including the ones earlier rounds already confirmed,
    and this seat's entire claim is that the fix pass is a smaller and much
    richer population than the PR.

    `sources` maps a repo-relative path to that file's text as the fix pass left
    it — a checkout at the fix head, read by the caller. Absent, the five ast
    rules cannot run and every one of them is declared in `could_not_assess`
    per file rather than silently not run.

    No timing is recorded here. :attr:`ReviewerRun.duration_ms` is the caller's
    to fill, because a clock inside this function would be the only impure thing
    in the module and would make the fixture tests depend on the machine.
    """
    active = tuple(rules) if rules is not None else load_rules()
    findings: list[Finding] = []
    gaps: list[str] = []
    for changed in parse_diff(diff):
        if not changed.added:
            continue
        source = sources.get(changed.path) if sources else None
        hits, unavailable = apply_rules(changed=changed, source=source, rules=active)
        for rule, hit in hits:
            findings.append(Finding(
                reviewer=SEAT,
                severity=rule.severity,
                file=changed.path,
                line=hit.line,
                # `title` is one line and `detail` carries the body, which is the
                # split :class:`Finding` already documents. The rule id leads the
                # detail rather than the title so two seats raising the same
                # defect still merge on the title the way #78's threshold needs.
                title=rule.message,
                detail=f"{rule.rule_id} — {hit.evidence}\n\n{rule.remediation}",
            ))
        for rule, why in unavailable:
            gaps.append(f"{changed.path}: {rule.rule_id} did not run — {why}")
    findings.sort(key=lambda f: (f.file, f.line or 0, SEVERITIES.index(f.severity), f.title))
    return ReviewerRun(findings=findings, could_not_assess=gaps)


__all__ = [
    "MATCH_KINDS",
    "RULES_DIR",
    "SEAT",
    "ChangedFile",
    "SlopRule",
    "SlopRuleError",
    "apply_rules",
    "load_rules",
    "parse_diff",
    "review_fix_pass",
]
