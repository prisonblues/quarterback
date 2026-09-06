"""How dangerous is the REPAIR — the blast radius of the surface a fix pass touches.

A finding is graded on severity and nothing else, so a fixer is told how bad the
defect is and never how far the fix reaches. On `lexray#1780` a round-3 pass
answering a P2 about one route put `gzip` at nginx **server** level and broke an
unrelated endpoint's conditional requests. Nothing in that finding said the repair
reached past the line it named, because nothing anywhere computes that.

This is the first input to #770's `fix_risk` axis, and the only one of the three
that needs no history and no model. It is a classification of PATHS: give it the
files a fix touched — or, before the pass, the files a finding says it would have
to touch — and it answers `low` / `medium` / `high` with the categories that fired
and a sentence naming the file that fired each one.

**Pointed at the fix pass, not at the pull request.** mergeCraft's blast-radius
classifier (MIT; the idea, not the file) answers a merge-gating question — "may
this PR land unattended". The lane here answers a narrowing question: a `high`
finding is not deferred, it is one whose fix must declare its collateral and gets
read against a tighter surface budget (#615, #619). Same shape of computation,
different consumer, so the argument is spelled `fix_blast_radius(paths)` and the
paths are a fix range, not a diff against base.

**Derived, never asked of the model.** #770 is explicit and mergeCraft wrote the
same rule down as review doctrine C12: a number a model reports about its own work
is an uncalibrated self-report, and the actor scoring the risk of its own fix is
the one the 63.7% injection figure already indicts. Everything below is a rule
table over path strings. Nothing here consults a reviewer's words, a severity, or
a confidence.

**Pure by rule**, on the same terms as :mod:`panel_locality`: arguments in, value
out. No repo read, no environment, no network, no clock, no module state. It is
called mid-round, from a test, and from a replay over recorded rounds, and all
three must get the same answer from the same bytes. It imports `panel_locality`
and nothing else from this tree — the two are a pair and ship together, so the
guard against a partial install belongs at the CALLER (`panel.py` already wraps
`import panel_locality` in a try/except and would wrap this the same way), not
here where it would mean a second copy of the test/prose tables.

**Two guards do most of the calibration work**, and both exist because the naive
version of this over-fires spectacularly on the two repos it has to serve:

- a **generated or vendored** path contributes only its NAMED categories and is
  never fed to the word-matching rules. lexray carries 2,855 files under
  `apps/static/assets/vendor/`, three of which are FontAwesome's Stripe brand
  icons; without the guard a `stripe.svg` refresh reads as a payment change.
- a **prose or test** path is likewise never fed to the word-matching rules.
  lexray's `docs/security.md`, `docs/data_security.md` and twenty-one files
  under `docs/standards/legalruleml/` whose names carry `stripe` — after an XSD
  expansion module — are documents ABOUT dangerous things, not code that does
  them.

The guards suppress; they never promote. Anything that cannot be classified is
scanned, because a path this table does not understand must not be excused.

**Every diff heuristic fails toward the LOWER lane.** The diff is read only for
lines that are positively identifiable as additions inside a well-formed `@@`
hunk. A diff with no parseable hunk yields no added lines and therefore no
categories — an unparseable diff can never manufacture a `high`. That is the
opposite of the obvious implementation, which lowercases the whole blob and
substring-matches it: that version fires `migrations: high` on a test docstring
containing the words "drop table", and on the REMOVED half of a diff that was
deleting exactly such a line.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from panel_locality import (
    KIND_PROSE,
    KIND_TEST,
    finding_kind,
    normalize_path,
)

LANE_LOW, LANE_MEDIUM, LANE_HIGH = "low", "medium", "high"

#: Lane order. The lane of a change is the MAX over the categories that fired —
#: never a sum and never an average. Two medium signals are not a high: a
#: dependency bump next to an API tweak is two ordinary risks, not a dangerous
#: one, and an additive score would have to be re-tuned every time a category was
#: added. Max is the only rule that stays meaningful as the table grows.
_LANE_RANK: dict[str, int] = {LANE_LOW: 0, LANE_MEDIUM: 1, LANE_HIGH: 2}

MIGRATIONS = "migrations"
AUTH_SECURITY_PAYMENT = "auth_security_payment"
SECRETS_CONFIG_DEPLOYMENT = "secrets_config_deployment"
IRREVERSIBLE_INFRA = "irreversible_infra"
DEPENDENCY_CHANGES = "dependency_changes"
PUBLIC_API_CHANGES = "public_api_changes"
SOURCE_WITHOUT_TESTS = "source_without_tests"
GENERATED_FILES = "generated_files"

#: The lane each category carries, as an auditable table rather than as branches
#: buried in the classifier. `generated_files` is `low` and that is the whole of
#: the "generated only counts alongside another high" rule: max-of-lanes does the
#: rest, so a lockfile refresh on its own is low and the same lockfile beside a
#: migration is high. The guard that makes this LOAD-BEARING rather than a
#: tautology is upstream, in :func:`path_categories` — a generated path is barred
#: from the word-matching rules, so it cannot raise its own lane.
CATEGORY_LANES: dict[str, str] = {
    MIGRATIONS: LANE_HIGH,
    AUTH_SECURITY_PAYMENT: LANE_HIGH,
    SECRETS_CONFIG_DEPLOYMENT: LANE_HIGH,
    IRREVERSIBLE_INFRA: LANE_HIGH,
    DEPENDENCY_CHANGES: LANE_MEDIUM,
    PUBLIC_API_CHANGES: LANE_MEDIUM,
    SOURCE_WITHOUT_TESTS: LANE_MEDIUM,
    GENERATED_FILES: LANE_LOW,
}

#: What each category is called in the reason sentence. A lane with no reason is a
#: number nobody can argue with, and this one will be argued with — by a fixer
#: told to narrow a change, and by whoever eventually decides what `high` gates.
CATEGORY_PHRASES: dict[str, str] = {
    MIGRATIONS: "a schema migration",
    AUTH_SECURITY_PAYMENT: "auth, security or payment code",
    SECRETS_CONFIG_DEPLOYMENT: "secrets, deploy or production config",
    IRREVERSIBLE_INFRA: "an irreversible infrastructure action",
    DEPENDENCY_CHANGES: "a dependency or lockfile",
    PUBLIC_API_CHANGES: "a public API surface",
    SOURCE_WITHOUT_TESTS: "source changed with no test beside it",
    GENERATED_FILES: "generated or vendored output",
}

#: How many pieces of evidence a category shows in the reason before it says
#: "+N more". The full list stays on the value; this only bounds the sentence.
_REASON_EVIDENCE = 2


# ----------------------------------------------------------------- NAMED rules
# Anchored on a whole directory segment, a whole basename, or a suffix. These run
# on every path including generated ones, because they identify a file by what it
# IS rather than by a word that appears in its name.

#: Both repos are alembic, so `migrations/` is the directory in each and the 218
#: files under the two of them are the largest single high-risk population here.
#: `versions` is deliberately absent: it only ever appears beneath `migrations`,
#: and on its own it is a word generic enough to catch an unrelated tree.
MIGRATION_DIR_SEGMENTS = frozenset({"migration", "migrations", "alembic"})

#: The migration machinery that lives OUTSIDE `migrations/`: lexray's root
#: `migration_check.py`, quarterback's `scripts/migration_reconcile.py`, and the
#: alembic config that says where the whole tree lives. A segment-only rule of
#: mergeCraft's shape misses all three.
MIGRATION_NAME_PREFIXES = ("migration_", "migrate_")
MIGRATION_FILE_NAMES = frozenset({"alembic.ini"})

#: Config and deployment by directory. `nginx` is here because of the incident in
#: the module docstring — `nginx/production.conf.template` is a file where a
#: three-line fix changes every route in the service, which is the exact property
#: this lane exists to name. `workflows` rather than a `.github/workflows/` path
#: prefix: no other directory in either repo is called that, and lexray's
#: `.github/skills/` and `.github/prompts/` are prose that must NOT fire.
DEPLOY_DIR_SEGMENTS = frozenset({
    "nginx", "workflows", "pipelines", "systemd", "deploy", "deployment",
    "terraform", "helm", "charts", "k8s", "kubernetes", "ansible", "infra",
    "azure_ops", "ops",
})

#: Deployment by the file's own name. The `.env` rules are a suffix and not an
#: equality because the four spellings across the two repos are `.env`,
#: `.env.example`, `sample.env`, `test.env` and `portainer.stack.env`, and an
#: exact-match list would have missed three of them and gone stale on the fifth.
#: `dockerfile`/`docker-compose` are prefixes for `Dockerfile.analyzers` and
#: `docker-compose.test.yml`.
DEPLOY_NAME_PREFIXES = ("dockerfile", "docker-compose", ".env", "procfile")
DEPLOY_NAME_SUFFIXES = (".env", ".conf", ".conf.template", ".service", ".timer",
                        ".tf", ".tfvars")
DEPLOY_FILE_NAMES = frozenset({
    "docker-entrypoint.sh", "sshd_config", "gunicorn-cfg.py", "build.sh",
    ".gitleaks.toml", "sonar-project.properties", "portainer.stack.env",
})

#: `flake.nix` and `flake.lock` are here and NOT in the deployment table, which is
#: the one place this disagrees with instinct. They pin the toolchain for both the
#: dev shell and the container, so "deployment" is arguable — but a `nix flake
#: update` is a lockfile refresh, and a lockfile refresh reading as `high` is the
#: precise failure the generated-files guard exists to prevent. Medium is what a
#: dependency bump is worth; if one breaks something, it breaks it visibly.
DEPENDENCY_FILE_NAMES = frozenset({
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
    "pyproject.toml", "uv.lock", "poetry.lock", "pipfile", "pipfile.lock",
    "flake.nix", "flake.lock", "go.mod", "go.sum", "cargo.toml", "cargo.lock",
    "gemfile", "gemfile.lock", "composer.json", "composer.lock",
    ".python-version", ".tool-versions", ".nvmrc",
})

#: The public surface by directory. mergeCraft keys this on `__init__.py`, which
#: is calibrated OUT here: three of quarterback's six `__init__.py` files are
#: empty package markers, so the rule fires on a zero-byte file — and it misses
#: `app/api/` (18 modules of actual HTTP surface), `app/schemas.py` (the wire
#: contract) and `mcp/` (the tool surface every agent in the fleet calls). In
#: lexray the same surface is `apps/api/` at thirty route modules.
API_DIR_SEGMENTS = frozenset({"api", "apis", "mcp", "routes", "endpoints",
                              "graphql", "openapi"})

#: Auth by directory, as a PREFIX on the segment. The exact-segment version of
#: this rule — mergeCraft's — matches `auth/` and therefore matches nothing in
#: lexray, whose entire authentication surface is the eighteen modules under
#: `apps/authentication/`. It is the largest miss in the shipped table.
SECURITY_DIR_PREFIXES = ("auth",)
SECURITY_DIR_SEGMENTS = frozenset({"security", "permissions", "payments", "billing"})

#: Generated and vendored output. mergeCraft looks for a `generated` path segment,
#: which fires on exactly zero files across both repos — decoration. What is
#: actually generated here is lockfiles, minified bundles and `vendor/` trees, and
#: the `vendor/` tree alone is 2,855 of lexray's files.
GENERATED_DIR_SEGMENTS = frozenset({
    "generated", "vendor", "vendored", "node_modules", "dist", "build", "_build",
    "__pycache__", "site-packages", ".venv", "venv",
})
GENERATED_NAME_SUFFIXES = (".lock", ".min.js", ".min.css", ".map", "_pb2.py",
                           "_pb2_grpc.py", ".pb.go")
GENERATED_FILE_NAMES = frozenset({"package-lock.json", "yarn.lock",
                                  "pnpm-lock.yaml", "go.sum", "bun.lockb"})


# --------------------------------------------------------------- INFERRED rules
# Word matches against the tokens of a basename. Suppressed for generated, prose
# and test paths (see the module docstring), because a word in a filename is
# evidence about a file that DOES the thing and not about one that describes it.

#: `token`/`secret`/`password` earn their place on both trees; `stripe` and
#: `checkout` are here for completeness and fire on nothing in either repo, which
#: is the correct outcome for two products that take no payments — the rule is
#: dormant rather than wrong, and the guards are what keep it dormant instead of
#: firing on FontAwesome's brand icons and LegalRuleML's `stripe_*` schemas.
#: `key` and `access` are deliberately ABSENT: they would catch `key_routes.py`,
#: `qb-seat-key` and `access_starter.py` alongside everything else in either tree
#: with the word in it, and a rule that fires on everything is noise.
SECURITY_STEM_TOKENS = frozenset({
    "auth", "authn", "authz", "authentication", "authorization", "authorisation",
    "oauth", "login", "logout", "signup", "password", "passwords", "credential",
    "credentials", "token", "tokens", "secret", "secrets", "identity",
    "permission", "permissions", "entitlement", "entitlements", "impersonation",
    "rbac", "acl", "csrf", "cors", "crypto", "payment", "payments", "billing",
    "invoice", "stripe", "checkout",
})

API_STEM_TOKENS = frozenset({"routes", "router", "endpoints", "urls", "schemas",
                             "schema", "openapi", "api"})

#: Irreversibility by filename. mergeCraft anchors this on `infra/terraform/`;
#: neither repo has terraform, so that rule is decoration too. What is genuinely
#: irreversible here is quarterback's own worktree tooling — `remove-worktree`
#: and `prune-worktrees` both run `rm -rf` over a directory that may hold a peer
#: agent's uncommitted work, which is the failure `harness/README.md` refuses at
#: the shell. A fix pass editing either of those is the highest-consequence edit
#: in this repo and had no signal at all before this.
IRREVERSIBLE_STEM_TOKENS = frozenset({"destroy", "teardown", "purge", "prune",
                                      "wipe", "nuke", "drop", "remove", "rollback"})

#: Source, for the one category that is about what is MISSING. Extensionless files
#: under a `bin/` segment count: quarterback's forty `harness/bin/qb-*` scripts
#: carry no suffix and are the busiest source tree in the repo. `.html` is
#: excluded — lexray has 160 Jinja templates and firing "untested source" on every
#: UI fix would make the category unreadable.
SOURCE_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh",
                   ".bash", ".nix", ".sql", ".go", ".rs", ".rb", ".java", ".kt",
                   ".php", ".c", ".h", ".cpp", ".vue", ".svelte")


# ------------------------------------------------------------------ DIFF rules
# Read only from lines positively identified as additions inside a well-formed
# hunk, and only for files the guards did not suppress.

#: The new-side hunk header, matched strictly. Strictness IS the fail-low rule:
#: a blob with nothing matching this contributes no added lines and so no
#: categories, which is how an unparseable diff is prevented from inventing a
#: `high`.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")

#: Destructive schema operations. Both repos are alembic, so the operative
#: spellings are `op.drop_*` and `op.alter_column` rather than raw SQL, and
#: mergeCraft's raw-SQL-only list fires on neither. **`create table` is dropped
#: from their set on purpose**: creating a table is additive and reversible, and
#: including it makes every new-feature migration read identically to one that
#: destroys data — which is the distinction the lane exists to draw.
DESTRUCTIVE_SCHEMA_TOKENS = (
    "op.drop_table", "op.drop_column", "op.drop_constraint", "op.drop_index",
    "op.alter_column", "op.execute", "drop table", "drop column", "drop database",
    "truncate table",
)

#: Irreversible actions in an added line. The git half of this is lifted from the
#: list `harness/README.md` refuses while a peer is live in a shared tree, for the
#: same reason: each of these destroys work that has no other copy.
IRREVERSIBLE_DIFF_TOKENS = (
    "rm -rf", "rm -fr", "git clean -", "git reset --hard", "git checkout -- ",
    "git worktree remove", "git push --force", "push -f ", "terraform destroy",
    "docker system prune", "az webapp delete",
)

#: A secret name assigned a STRING LITERAL. The literal is required, and that is
#: the whole improvement over a substring match: `password: str` and
#: `secret_key: SecretStr` are type annotations on a settings class — quarterback's
#: `app/config.py` is full of them — and a rule without the quote grades every
#: config field as a leaked credential.
_SECRET_LITERAL_RE = re.compile(
    r"(password|secret_key|api_key|private_key|client_secret|access_token"
    r"|aws_secret_access_key)\s*[:=]\s*[\"']")
_PEM_MARKER = "-----begin "


@dataclass(frozen=True)
class BlastRadius:
    """A lane, the categories that produced it, and the evidence for each.

    The categories and the evidence are not decoration. A `high` handed to a fixer
    with instructions to narrow its change is useless unless the fixer can see
    WHICH file made it high — and the operator who eventually decides what `high`
    gates needs to be able to disagree with a specific rule rather than with a
    word.
    """

    lane: str
    categories: tuple[str, ...]
    evidence: Mapping[str, tuple[str, ...]]
    reason: str

    def as_dict(self) -> dict:
        """The wire shape, for a board payload or a round record."""
        return {"lane": self.lane, "categories": list(self.categories),
                "evidence": {k: list(v) for k, v in self.evidence.items()},
                "reason": self.reason}


_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _stem_tokens(name: str) -> frozenset[str]:
    """The words in a basename, with its extension dropped and a leading dot
    ignored. `permissions_service.py` is {permissions, service}; `auth-manager.js`
    is {auth, manager}; `production.conf.template` is {production, conf}."""
    stem = name[1:] if name.startswith(".") else name
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    return frozenset(t for t in _TOKEN_SPLIT.split(stem.lower()) if t)


def _named_categories(name: str, parents: list[str]) -> set[str]:
    """The categories a path earns from what the file IS — its directory, its
    whole basename, its suffix. Applied to every path, generated ones included:
    a lockfile is a dependency change whether or not it is also machine-written."""
    fired: set[str] = set()
    if (name in GENERATED_FILE_NAMES or name.endswith(GENERATED_NAME_SUFFIXES)
            or any(s in GENERATED_DIR_SEGMENTS for s in parents)):
        fired.add(GENERATED_FILES)
    if (any(s in MIGRATION_DIR_SEGMENTS for s in parents)
            or name.startswith(MIGRATION_NAME_PREFIXES) or name in MIGRATION_FILE_NAMES):
        fired.add(MIGRATIONS)
    if (any(s in DEPLOY_DIR_SEGMENTS for s in parents) or name in DEPLOY_FILE_NAMES
            or name.startswith(DEPLOY_NAME_PREFIXES) or name.endswith(DEPLOY_NAME_SUFFIXES)):
        fired.add(SECRETS_CONFIG_DEPLOYMENT)
    if name in DEPENDENCY_FILE_NAMES or (name.startswith("requirements")
                                         and name.endswith(".txt")):
        fired.add(DEPENDENCY_CHANGES)
    if any(s in API_DIR_SEGMENTS for s in parents):
        fired.add(PUBLIC_API_CHANGES)
    if any(s in SECURITY_DIR_SEGMENTS or s.startswith(SECURITY_DIR_PREFIXES)
           for s in parents):
        fired.add(AUTH_SECURITY_PAYMENT)
    return fired


def path_categories(path: str) -> frozenset[str]:
    """Every blast-radius category one path fires, ignoring the set-level rules.

    Named rules first, then — **only if neither guard trips** — the word matches
    against the basename's tokens.

    The two guards are one line each and they are where most of this module's
    calibration lives. A generated or vendored path returns with its named
    categories alone, so `vendor/…/brands/stripe.svg` is generated output and not
    a payment change. A prose or test path does the same, so `docs/security.md`
    is a document about security rather than security code. Neither guard can
    RAISE a lane; both only decline to infer, and a path this table cannot place
    at all falls through to the word matches, because a file that is not
    understood must not be excused on the strength of not being understood.
    """
    normal = normalize_path(path)
    segments = [s.lower() for s in normal.split("/") if s]
    if not segments:
        return frozenset()
    fired = _named_categories(segments[-1], segments[:-1])
    if GENERATED_FILES in fired or finding_kind(normal) in (KIND_PROSE, KIND_TEST):
        return frozenset(fired)
    tokens = _stem_tokens(segments[-1])
    if tokens & SECURITY_STEM_TOKENS:
        fired.add(AUTH_SECURITY_PAYMENT)
    if tokens & API_STEM_TOKENS:
        fired.add(PUBLIC_API_CHANGES)
    if tokens & IRREVERSIBLE_STEM_TOKENS:
        fired.add(IRREVERSIBLE_INFRA)
    return frozenset(fired)


def is_source(path: str) -> bool:
    """Is this path code that a test could cover?

    Not "is it production" — :func:`panel_locality.finding_kind` answers that, and
    it answers `production` for `uv.lock` and `docker-compose.yml` because
    production is its catch-all. Feeding that answer straight into
    :func:`source_without_tests` would grade a lockfile refresh as untested source,
    which is the noise case. So a source file is production kind, not generated,
    and either carries a code extension or is an extensionless script under a
    `bin/` directory — the shape of all forty `harness/bin/qb-*` commands.
    """
    normal = normalize_path(path)
    segments = [s.lower() for s in normal.split("/") if s]
    if not segments or finding_kind(normal) in (KIND_PROSE, KIND_TEST):
        return False
    if GENERATED_FILES in path_categories(normal):
        return False
    name = segments[-1]
    return name.endswith(SOURCE_SUFFIXES) or ("." not in name and "bin" in segments[:-1])


def source_without_tests(paths: Iterable[str]) -> bool:
    """Did this change touch source and no test at all?

    **The signature takes the whole changed-path set and not one path**, because
    the property is a property of the CHANGE. It is the one category here that is
    about something missing, and nothing about a single file can tell you whether
    a test for it moved in the same pass. A per-path version would have to answer
    "does this file have a test somewhere", which needs the repository — and this
    module does not read the repository.

    A fix pass that touched no source at all answers False rather than True: a
    docs-only or lockfile-only pass has no source to have left uncovered, and
    calling it untested would put the whole low lane into medium.
    """
    normalised = [normalize_path(p) for p in paths or ()]
    if not any(is_source(p) for p in normalised):
        return False
    return not any(finding_kind(p) == KIND_TEST for p in normalised)


def _added_lines(diff: str) -> list[tuple[str, str]]:
    """`(path, text)` for each line the diff ADDS, inside a well-formed hunk.

    Three things this refuses to do, each of which is a way the naive version
    manufactures a `high` out of nothing:

    - it never reads a REMOVED line, so a fix that DELETES `rm -rf` does not get
      graded as one that adds it;
    - it never reads a context line, so an unchanged neighbour cannot implicate a
      change that did not touch it;
    - it opens a hunk only on a header matching :data:`_HUNK_RE`, so a blob that
      is not a diff yields nothing rather than yielding its whole text.

    Path attribution follows `+++ b/P` and requires the `--- ` line before it, the
    same pairing :func:`panel_locality.parse_diff_scope` uses. An added line whose
    path could not be determined is still returned, under the empty path: the
    guards downstream SUPPRESS on a known-harmless path and must not be able to
    excuse a line by failing to place it.
    """
    added: list[tuple[str, str]] = []
    path, in_hunk, prev_minus = "", False, False
    for line in str(diff or "").splitlines():
        if line.startswith("diff --git "):
            path, in_hunk, prev_minus = "", False, False
            continue
        if prev_minus and line.startswith("+++ "):
            token = line[4:].strip()
            path = "" if token == "/dev/null" else normalize_path(token.strip('"'))
            prev_minus, in_hunk = False, False
            continue
        prev_minus = line.startswith("--- ")
        if line.startswith("@@"):
            in_hunk = _HUNK_RE.match(line) is not None
            continue
        if in_hunk and line.startswith("+") and not line.startswith("+++"):
            added.append((path, line[1:]))
    return added


def _diff_hits(diff: str) -> list[tuple[str, str]]:
    """`(category, what fired it)` for every destructive marker in an added line,
    in the order encountered and without repeats. Suppressed for lines belonging
    to a generated, prose or test file, on the same argument as the path rules: a
    fixture containing `DROP TABLE` is a fixture, and a runbook quoting `rm -rf`
    is a runbook."""
    hits: list[tuple[str, str]] = []

    def note(category: str, evidence: str) -> None:
        if (category, evidence) not in hits:
            hits.append((category, evidence))

    for path, text in _added_lines(diff):
        if path and (GENERATED_FILES in path_categories(path)
                     or finding_kind(path) in (KIND_PROSE, KIND_TEST)):
            continue
        lowered = text.lower()
        for token in DESTRUCTIVE_SCHEMA_TOKENS:
            if token in lowered:
                note(MIGRATIONS, f"diff adds `{token}`")
        for token in IRREVERSIBLE_DIFF_TOKENS:
            if token in lowered:
                note(IRREVERSIBLE_INFRA, f"diff adds `{token.strip()}`")
        if _PEM_MARKER in lowered or _SECRET_LITERAL_RE.search(lowered):
            note(SECRETS_CONFIG_DEPLOYMENT, "diff assigns a secret a literal value")
    return hits


def _reason(lane: str, categories: tuple[str, ...],
            evidence: Mapping[str, tuple[str, ...]]) -> str:
    """One sentence naming the lane, each category, and the file that fired it."""
    if not categories:
        return ("Fixing here is LOW risk: nothing in the fix's surface matched an "
                "elevated blast-radius rule.")
    clauses = []
    for category in categories:
        seen = evidence.get(category, ())
        shown = ", ".join(seen[:_REASON_EVIDENCE])
        more = len(seen) - _REASON_EVIDENCE
        tail = f", +{more} more" if more > 0 else ""
        clauses.append(f"{CATEGORY_PHRASES[category]} [{CATEGORY_LANES[category]}]"
                       f" ({shown}{tail})")
    return f"Fixing here is {lane.upper()} risk: " + "; ".join(clauses) + "."


def fix_blast_radius(paths: Iterable[str], *, diff: str = "") -> BlastRadius:
    """How dangerous is the repair whose surface is `paths`?

    `paths` is the set of files a fix pass touched or would have to touch —
    `fix_surface_state(...)["files"]` is exactly this argument, and so is the file
    list a finding names when it declares its collateral before the pass runs.
    `diff` is optional and only ever ADDS categories through
    :func:`_diff_hits`; a caller with no diff to hand gets the path answer, never
    a degraded one, and a caller with an unreadable diff gets the same.

    Categories are ordered worst-lane first and then alphabetically, so the reason
    leads with the thing that set the lane and two runs over the same set produce
    the same sentence.
    """
    normalised = [p for p in (normalize_path(p) for p in paths or ()) if p]
    evidence: dict[str, list[str]] = {}
    for path in normalised:
        for category in sorted(path_categories(path)):
            evidence.setdefault(category, []).append(path)
    for category, marker in _diff_hits(diff):
        evidence.setdefault(category, []).append(marker)
    untested = [p for p in normalised if is_source(p)] if source_without_tests(normalised) else []
    if untested:
        evidence[SOURCE_WITHOUT_TESTS] = untested
    categories = tuple(sorted(evidence, key=lambda c: (-_LANE_RANK[CATEGORY_LANES[c]], c)))
    lane = LANE_LOW
    for category in categories:
        if _LANE_RANK[CATEGORY_LANES[category]] > _LANE_RANK[lane]:
            lane = CATEGORY_LANES[category]
    # Evidence is SORTED and not left in the order the caller happened to hold the
    # paths in. The lane never depended on it, but the reason sentence did, and a
    # value that reads differently depending on how a list was assembled is
    # reproducible in a test and not in a replay — the worst of both. The same
    # argument `panel_locality.match_across_rounds` makes for sorting its priors.
    frozen = {c: tuple(sorted(evidence[c])) for c in categories}
    return BlastRadius(lane=lane, categories=categories, evidence=frozen,
                       reason=_reason(lane, categories, frozen))


__all__ = [
    "AUTH_SECURITY_PAYMENT", "CATEGORY_LANES", "CATEGORY_PHRASES",
    "DEPENDENCY_CHANGES", "GENERATED_FILES", "IRREVERSIBLE_INFRA", "LANE_HIGH",
    "LANE_LOW", "LANE_MEDIUM", "MIGRATIONS", "PUBLIC_API_CHANGES",
    "SECRETS_CONFIG_DEPLOYMENT", "SOURCE_WITHOUT_TESTS", "BlastRadius",
    "fix_blast_radius", "is_source", "path_categories", "source_without_tests",
]
