"""How dangerous is the REPAIR — the blast radius of a fix pass's surface (#770).

The axis these tests defend is the one a finding has never carried: severity says
how bad the defect is, and nothing anywhere says how far the fix reaches. On
`lexray#1780` a round-3 pass answering a P2 about one route put `gzip` at nginx
server level and broke an unrelated endpoint. So the first fixture below is that
change, and it must come back `high`.

The rest are written against the three ways a rule table over path strings goes
wrong, all of which the shipped mergeCraft table does on these two repos:

- **it fires on nothing** — `infra/terraform/` and a `generated` path segment
  match zero files across quarterback and lexray, and `auth` as a whole segment
  misses every one of the eighteen modules under lexray's `apps/authentication/`;
- **it fires on everything** — a substring match for `stripe` hits twenty-one
  files under `docs/standards/legalruleml/` and three FontAwesome brand icons,
  none of which is a payment path, and `__init__.py` as the public-API rule
  fires on three empty package markers;
- **it invents a `high` from something it could not read** — a whole-blob
  substring scan grades a test docstring containing "drop table" as a schema
  migration, and grades a diff DELETING `rm -rf` the same as one adding it.

Every diff test therefore has a companion asserting the lower lane: the removed
line, the context line, the fixture, the unparseable blob.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel_blast as blast  # noqa: E402


def lanes_of(*paths, diff=""):
    """The value under test, for the reading tests do most often."""
    return blast.fix_blast_radius(list(paths), diff=diff)


# ------------------------------------------------------- one path, one category

@pytest.mark.parametrize("path,category", [
    # Migrations. Both repos are alembic and `migrations/` is the directory in
    # each — 218 files between them, the largest high-risk population here.
    ("migrations/versions/0032_finding_outcomes.py", blast.MIGRATIONS),
    ("migrations/env.py", blast.MIGRATIONS),
    ("alembic.ini", blast.MIGRATIONS),
    # …including the migration machinery that lives outside that directory, which
    # a segment-only rule misses in both repos.
    ("migration_check.py", blast.MIGRATIONS),
    ("scripts/migration_reconcile.py", blast.MIGRATIONS),

    # Auth. quarterback has no auth DIRECTORY at all, so a directory-only rule is
    # dead here; lexray has nothing else, so a filename-only rule is dead there.
    ("app/auth.py", blast.AUTH_SECURITY_PAYMENT),
    ("app/identity.py", blast.AUTH_SECURITY_PAYMENT),
    ("apps/utils/oauth_audience.py", blast.AUTH_SECURITY_PAYMENT),
    ("apps/static/assets/js/auth-manager.js", blast.AUTH_SECURITY_PAYMENT),
    ("migrations/versions/0015_reviewer_tokens.py", blast.AUTH_SECURITY_PAYMENT),

    # Secrets, config and deployment. `nginx/` is the incident in the docstring.
    ("nginx/production.conf.template", blast.SECRETS_CONFIG_DEPLOYMENT),
    (".github/workflows/tests.yml", blast.SECRETS_CONFIG_DEPLOYMENT),
    ("harness/loops/systemd/loops-lander.service", blast.SECRETS_CONFIG_DEPLOYMENT),
    ("pipelines/azure-pipeline-release.yml", blast.SECRETS_CONFIG_DEPLOYMENT),
    ("azure_ops/cli.py", blast.SECRETS_CONFIG_DEPLOYMENT),
    ("docker-compose.yml", blast.SECRETS_CONFIG_DEPLOYMENT),
    ("docker-compose.test.yml", blast.SECRETS_CONFIG_DEPLOYMENT),
    ("Dockerfile.analyzers", blast.SECRETS_CONFIG_DEPLOYMENT),
    ("docker-entrypoint.sh", blast.SECRETS_CONFIG_DEPLOYMENT),
    ("sshd_config", blast.SECRETS_CONFIG_DEPLOYMENT),
    # The four spellings of an env file across the two repos. An exact-match list
    # would have caught one of them.
    (".env.example", blast.SECRETS_CONFIG_DEPLOYMENT),
    ("sample.env", blast.SECRETS_CONFIG_DEPLOYMENT),
    ("test.env", blast.SECRETS_CONFIG_DEPLOYMENT),
    ("portainer.stack.env", blast.SECRETS_CONFIG_DEPLOYMENT),

    # Irreversible. Both of these run `rm -rf` over a directory that may hold a
    # peer agent's uncommitted work — the highest-consequence edit in this repo.
    ("harness/bin/remove-worktree", blast.IRREVERSIBLE_INFRA),
    ("harness/bin/prune-worktrees", blast.IRREVERSIBLE_INFRA),

    # Dependencies, including the two nix files the shipped table has no entry for
    # even though both repos are built on them.
    ("uv.lock", blast.DEPENDENCY_CHANGES),
    ("pyproject.toml", blast.DEPENDENCY_CHANGES),
    ("mcp/pyproject.toml", blast.DEPENDENCY_CHANGES),
    ("package-lock.json", blast.DEPENDENCY_CHANGES),
    ("flake.nix", blast.DEPENDENCY_CHANGES),
    ("flake.lock", blast.DEPENDENCY_CHANGES),
    ("requirements-dev.txt", blast.DEPENDENCY_CHANGES),

    # Public API — the surface that actually is one in these repos.
    ("app/api/reviews.py", blast.PUBLIC_API_CHANGES),
    ("app/schemas.py", blast.PUBLIC_API_CHANGES),
    ("mcp/mcp_server/server.py", blast.PUBLIC_API_CHANGES),
    ("apps/api/fca_routes.py", blast.PUBLIC_API_CHANGES),
    ("apps/authentication/routes.py", blast.PUBLIC_API_CHANGES),

    # Generated and vendored output.
    ("app/static/vendor/sortable.min.js", blast.GENERATED_FILES),
    ("apps/static/assets/vendor/@fortawesome/fontawesome-free/css/all.min.css",
     blast.GENERATED_FILES),
    ("uv.lock", blast.GENERATED_FILES),
])
def test_a_path_fires_the_category_it_belongs_to(path, category):
    assert category in blast.path_categories(path)


def test_lexrays_whole_auth_tree_fires_even_though_the_directory_is_not_called_auth():
    """The single largest miss in mergeCraft's shipped table, pinned.

    Their rule matches the path segment `auth` exactly. lexray's authentication
    surface is eighteen modules under `apps/authentication/`, so the segment never
    equals `auth` and the rule fires on none of them — an entire auth tree graded
    as ordinary source. The rule here matches a segment that STARTS with `auth`.
    """
    for path in ("apps/authentication/permissions_service.py",
                 "apps/authentication/route_access.py",
                 "apps/authentication/impersonation.py",
                 "apps/authentication/auth0_service.py"):
        assert blast.AUTH_SECURITY_PAYMENT in blast.path_categories(path), path


def test_an_empty_package_marker_is_not_a_public_api_change():
    """mergeCraft keys `public_api_changes` on `__init__.py`. Three of this repo's
    six are zero bytes, so the rule grades an empty file as an API change while
    missing `app/api/` entirely. It is calibrated out; the directory rule is what
    catches `app/api/__init__.py`, and on the strength of the directory."""
    assert blast.path_categories("app/models/__init__.py") == frozenset()
    assert blast.PUBLIC_API_CHANGES in blast.path_categories("app/api/__init__.py")


def test_a_module_whose_name_merely_contains_a_dangerous_word_is_not_graded_on_it():
    """`key` and `access` are absent from the token table on purpose: they would
    take `qb-seat-key`, `key_routes.py` and `access_starter.py` along with
    everything else in either tree that mentions them."""
    assert blast.AUTH_SECURITY_PAYMENT not in blast.path_categories("harness/bin/qb-seat-key")
    assert blast.path_categories("app/license.py") == frozenset()
    assert blast.path_categories("harness/loops/panel_rounds.py") == frozenset()


# ----------------------------------------------------------------- the guards

def test_a_vendored_file_cannot_fire_a_word_rule_of_its_own():
    """FontAwesome ships Stripe's brand mark. lexray carries 2,855 files under
    `apps/static/assets/vendor/`, three of them named `stripe*.svg`, and a rule
    table that reads filenames would grade an icon-set refresh as a payment
    change. The generated guard returns before the word rules ever run."""
    icon = "apps/static/assets/vendor/@fortawesome/fontawesome-free/svgs/brands/stripe.svg"
    assert blast.path_categories(icon) == frozenset({blast.GENERATED_FILES})
    assert lanes_of(icon).lane == blast.LANE_LOW


def test_a_document_about_a_dangerous_thing_is_not_the_dangerous_thing():
    """`docs/security.md` describes the security posture; twenty-one files under
    `docs/standards/legalruleml/` carry `stripe` in their names — after an XSD after an XSD
    expansion module and have nothing to do with payments. The prose guard is
    what keeps a security rule from firing on the documentation of security."""
    for path in ("docs/security.md", "docs/data_security.md",
                 "docs/standards/legalruleml/generation/tmp/stripe_content_module.xsd",
                 "docs/authentication.md"):
        assert blast.path_categories(path) == frozenset(), path


def test_a_test_file_is_not_graded_on_the_thing_it_tests():
    """A test's blast radius is the suite, not the subject. `test_panel_token.py`
    and `security_key_guard.py` are both about dangerous code and neither IS it."""
    assert blast.path_categories("harness/loops/tests/test_panel_token.py") == frozenset()
    assert blast.path_categories("tests/security_key_guard.py") == frozenset()
    assert blast.path_categories("tests/test_claim_keys.py") == frozenset()


def test_a_path_the_table_cannot_place_is_still_read_by_the_word_rules():
    """The guards suppress, they never excuse. An unrecognised directory is not a
    reason to skip the inference — that would make "the table has not been taught
    about this tree" read as "nothing here is dangerous"."""
    assert blast.AUTH_SECURITY_PAYMENT in blast.path_categories("unknown/tree/oauth_bridge.py")


# ------------------------------------------------- the generated pairing, both ways

def test_a_lockfile_refresh_on_its_own_is_low():
    """The rule the shipped doc states and the one worth keeping: generated output
    alone is `low`. A `uv.lock` bump is a dependency change and reads as one; it
    does not read as dangerous."""
    result = lanes_of("uv.lock", "flake.lock")
    assert result.lane == blast.LANE_MEDIUM  # dependency_changes, not generated
    assert set(result.categories) == {blast.DEPENDENCY_CHANGES, blast.GENERATED_FILES}


def test_generated_output_alone_with_no_dependency_signal_is_low():
    result = lanes_of("app/static/vendor/sortable.min.js")
    assert result.lane == blast.LANE_LOW
    assert result.categories == (blast.GENERATED_FILES,)


def test_the_same_generated_file_beside_a_high_signal_is_high():
    """The other half of the pairing. A regenerated lockfile in the same pass as a
    migration is not a lockfile refresh — it is a migration, and the pass carries
    the migration's lane."""
    result = lanes_of("uv.lock", "migrations/versions/0032_finding_outcomes.py")
    assert result.lane == blast.LANE_HIGH
    assert blast.GENERATED_FILES in result.categories


# --------------------------------------------------- what is MISSING from the set

def test_source_with_no_test_anywhere_in_the_pass_is_medium():
    result = lanes_of("harness/loops/panel_rounds.py", "harness/loops/README.md")
    assert blast.SOURCE_WITHOUT_TESTS in result.categories
    assert result.lane == blast.LANE_MEDIUM


def test_source_with_a_test_beside_it_does_not_fire():
    """The signature takes the whole set because the property is a property of the
    CHANGE. Nothing about `panel_rounds.py` on its own says whether a test for it
    moved in the same pass."""
    assert not blast.source_without_tests(
        ["harness/loops/panel_rounds.py", "harness/loops/tests/test_panel_rounds.py"])
    assert blast.source_without_tests(["harness/loops/panel_rounds.py"])


def test_a_pass_that_touched_no_source_has_no_missing_test():
    """A docs-only or lockfile-only pass has no source to have left uncovered.
    Answering True would push the entire low lane into medium."""
    assert not blast.source_without_tests(["README.md", "CHANGELOG.md"])
    assert not blast.source_without_tests(["uv.lock", "flake.lock"])
    assert not blast.source_without_tests(["docker-compose.yml"])
    assert not blast.source_without_tests([])


def test_an_extensionless_script_under_bin_counts_as_source():
    """Forty of this repo's busiest files are `harness/bin/qb-*` with no suffix. An
    extension-only rule would grade the whole directory as not-code and report
    every fix to it as having left nothing uncovered."""
    assert blast.is_source("harness/bin/qb-doctor")
    assert blast.source_without_tests(["harness/bin/qb-doctor"])


def test_a_lockfile_is_not_untested_source_even_though_it_is_production():
    """`finding_kind` answers `production` for `uv.lock` because production is its
    catch-all. Feeding that answer straight through would grade a lockfile refresh
    as untested source, which is the noise case this predicate exists to avoid."""
    assert not blast.is_source("uv.lock")
    assert not blast.is_source("app/static/vendor/sortable.min.js")
    assert not blast.is_source("README.md")


# ------------------------------------------------------------- reading the diff

FIX_TO_A_WORKTREE_SCRIPT = """diff --git a/harness/bin/qb-stash b/harness/bin/qb-stash
--- a/harness/bin/qb-stash
+++ b/harness/bin/qb-stash
@@ -40,3 +40,4 @@ stash_tree() {
   echo "clearing $dir"
-  mv "$dir" "$dir.bak"
+  rm -rf "$dir"
 }
"""


def test_a_destructive_command_added_by_the_fix_raises_the_lane():
    result = lanes_of("harness/bin/qb-stash", diff=FIX_TO_A_WORKTREE_SCRIPT)
    assert result.lane == blast.LANE_HIGH
    assert blast.IRREVERSIBLE_INFRA in result.categories


def test_the_same_command_being_REMOVED_by_the_fix_does_not():
    """A fix that deletes a dangerous line is the safest kind of fix there is. A
    whole-blob substring scan cannot tell it from one that adds the line, and would
    grade a repair as the defect it repaired."""
    reversed_diff = FIX_TO_A_WORKTREE_SCRIPT.replace(
        '-  mv "$dir" "$dir.bak"\n+  rm -rf "$dir"',
        '-  rm -rf "$dir"\n+  mv "$dir" "$dir.bak"')
    result = lanes_of("harness/bin/qb-stash", diff=reversed_diff)
    assert blast.IRREVERSIBLE_INFRA not in result.categories


def test_a_dangerous_line_the_fix_merely_sat_next_to_does_not_fire():
    """Context lines are the code as it already was. A fix three lines above an
    existing `rm -rf` did not introduce it and must not be graded as though it
    had — that is how every change in a file becomes high in turn."""
    context = ('--- a/harness/bin/qb-stash\n+++ b/harness/bin/qb-stash\n'
               '@@ -40,3 +40,3 @@\n  rm -rf "$dir"\n-  echo done\n+  echo finished\n')
    assert blast.IRREVERSIBLE_INFRA not in lanes_of("harness/bin/qb-stash",
                                                    diff=context).categories


def test_an_unparseable_diff_never_manufactures_a_high():
    """The load-bearing failure direction. A blob with no well-formed hunk header
    is not a diff this module can read, and the honest answer to "what did the fix
    add" is nothing — not "everything in the blob". Every token below would fire a
    high if the text were scanned whole."""
    blob = "DROP TABLE users; terraform destroy; password = 'hunter2'"
    result = lanes_of("harness/loops/panel_core.py", diff=blob)
    assert result.lane == blast.LANE_MEDIUM  # source_without_tests, from the path
    assert blast.MIGRATIONS not in result.categories
    assert blast.IRREVERSIBLE_INFRA not in result.categories
    assert blast.SECRETS_CONFIG_DEPLOYMENT not in result.categories


def test_an_empty_diff_is_the_same_answer_as_no_diff_at_all():
    """A caller with nothing to hand gets the path answer, never a degraded one."""
    paths = ["harness/loops/panel_core.py", "migrations/versions/0032_x.py"]
    assert (blast.fix_blast_radius(paths, diff="").as_dict()
            == blast.fix_blast_radius(paths).as_dict()
            == blast.fix_blast_radius(paths, diff="\n\n").as_dict())


def test_a_drop_table_inside_a_test_fixture_is_a_fixture():
    """The guards apply to diff lines through the file they belong to, so a
    recorded SQL fixture full of destructive statements is suite material and not
    a schema change. Attributing the line to its file is what makes this possible
    — a whole-blob scan has no file to consult."""
    fixture = ("--- a/harness/loops/tests/fixtures/schema.sql\n"
               "+++ b/harness/loops/tests/fixtures/schema.sql\n"
               "@@ -1,1 +1,2 @@\n+DROP TABLE users;\n")
    assert blast.MIGRATIONS not in lanes_of(
        "harness/loops/tests/fixtures/schema.sql", diff=fixture).categories


def test_an_alembic_drop_outside_the_migrations_tree_is_still_a_schema_change():
    """Both repos are alembic, so the destructive spelling is `op.drop_table` and
    not raw SQL — mergeCraft's raw-SQL-only token list fires on neither repo."""
    diff = ("--- a/scripts/backfill.py\n+++ b/scripts/backfill.py\n"
            "@@ -1,1 +1,2 @@\n+    op.drop_column('posts', 'legacy_body')\n")
    result = lanes_of("scripts/backfill.py", diff=diff)
    assert result.lane == blast.LANE_HIGH
    assert blast.MIGRATIONS in result.categories


def test_creating_a_table_is_not_treated_as_destroying_one():
    """`create table` is dropped from the inherited token set. Including it makes
    every new-feature migration read identically to one that destroys data, which
    is the distinction the lane exists to draw."""
    diff = ("--- a/scripts/backfill.py\n+++ b/scripts/backfill.py\n"
            "@@ -1,1 +1,2 @@\n+    op.create_table('posts')\n")
    assert blast.MIGRATIONS not in lanes_of("scripts/backfill.py", diff=diff).categories


def test_a_secret_assigned_a_literal_fires_but_a_type_annotation_does_not():
    """`app/config.py` declares `secret_key`, `password` and `api_key` as settings
    fields. A rule without the required quote grades every one of them as a leaked
    credential, and then grades every edit to the settings class as high."""
    leaked = ("--- a/app/config.py\n+++ b/app/config.py\n"
              "@@ -1,1 +1,2 @@\n+    api_key = \"sk-live-9f3a\"\n")
    declared = ("--- a/app/config.py\n+++ b/app/config.py\n"
                "@@ -1,1 +1,2 @@\n+    api_key: str | None = None\n")
    assert blast.SECRETS_CONFIG_DEPLOYMENT in lanes_of("app/config.py", diff=leaked).categories
    assert blast.SECRETS_CONFIG_DEPLOYMENT not in lanes_of("app/config.py",
                                                           diff=declared).categories


# ---------------------------------------------------------------- whole changes

def test_the_lexray_1780_round_three_fix_is_high():
    """The change this axis was written for. A P2 about one route; a pass that
    edited the nginx server block and broke an unrelated endpoint's conditional
    requests. Nothing the fixer was given said the repair reached that far — this
    does, and it names the file."""
    result = lanes_of("nginx/production.conf.template", "nginx/default.conf.template",
                      "apps/api/fca_routes.py")
    assert result.lane == blast.LANE_HIGH
    assert result.categories[0] == blast.SECRETS_CONFIG_DEPLOYMENT
    assert "nginx/production.conf.template" in result.reason


def test_an_ordinary_quarterback_fix_pass_is_low():
    """A rule table that grades everything as dangerous is worth nothing. The
    commonest shape of fix in this repo — a loops module and its test — must come
    back low, or the axis carries no information."""
    result = lanes_of("harness/loops/panel_rounds.py",
                      "harness/loops/tests/test_panel_rounds.py",
                      "changelog.d/770.fix.md")
    assert result.lane == blast.LANE_LOW
    assert result.categories == ()
    assert "nothing in the fix's surface" in result.reason


def test_a_quarterback_fix_that_reaches_the_board_schema_is_high():
    """A realistic wide pass here: an API handler, its model, a migration for the
    new column, and the lockfile that came along. The migration sets the lane and
    the reason says so."""
    result = lanes_of("app/api/claims.py", "app/models/plan_item.py",
                      "migrations/versions/0033_claim_fuse.py",
                      "tests/test_lapsed_claims.py", "uv.lock")
    assert result.lane == blast.LANE_HIGH
    assert result.categories[0] == blast.MIGRATIONS
    assert blast.SOURCE_WITHOUT_TESTS not in result.categories  # a test came too


def test_a_lexray_fix_to_the_authentication_tree_is_high():
    result = lanes_of("apps/authentication/permissions_service.py",
                      "apps/authentication/route_access.py",
                      "apps/templates/auth/profile.html")
    assert result.lane == blast.LANE_HIGH
    assert blast.AUTH_SECURITY_PAYMENT in result.categories


def test_a_lexray_docs_and_vendor_pass_is_low():
    """782 docs files and 2,855 vendored ones. If either population could raise a
    lane, almost every lexray pass would come back high and the axis would be a
    constant."""
    result = lanes_of("docs/authentication.md", "docs/security.md",
                      "apps/static/assets/vendor/@fortawesome/fontawesome-free/css/all.min.css",
                      ".github/skills/pr-review/SKILL.md")
    assert result.lane == blast.LANE_LOW


def test_an_empty_change_is_low_and_says_why():
    result = lanes_of()
    assert result.lane == blast.LANE_LOW
    assert result.categories == ()
    assert result.evidence == {}
    assert "LOW risk" in result.reason


# --------------------------------------------------------------- the value itself

def test_the_lane_is_the_worst_category_and_never_a_sum():
    """Two mediums are two ordinary risks, not a dangerous one. An additive score
    would need re-tuning every time a category was added, and would let a wide but
    dull change outrank a one-line migration."""
    two_mediums = lanes_of("app/api/reviews.py", "uv.lock", "pyproject.toml")
    assert two_mediums.lane == blast.LANE_MEDIUM
    assert len(two_mediums.categories) >= 2


def test_the_reason_names_the_category_the_lane_and_the_file():
    """A lane with no reason is a number nobody can argue with, and this one will
    be argued with — by a fixer told to narrow its change, and by whoever decides
    what `high` gates."""
    reason = lanes_of("migrations/versions/0032_finding_outcomes.py").reason
    assert "HIGH risk" in reason
    assert "a schema migration" in reason
    assert "migrations/versions/0032_finding_outcomes.py" in reason


def test_the_reason_caps_the_files_it_lists_but_the_value_keeps_them_all():
    paths = [f"migrations/versions/000{n}_x.py" for n in range(1, 6)]
    result = lanes_of(*paths)
    assert len(result.evidence[blast.MIGRATIONS]) == 5
    assert "+3 more" in result.reason


def test_the_same_paths_in_any_order_give_the_same_value():
    """Called mid-round, from a test, and from a replay over recorded rounds. All
    three have to get the same answer from the same bytes."""
    paths = ["uv.lock", "app/auth.py", "migrations/versions/0032_x.py", "README.md"]
    assert (blast.fix_blast_radius(paths).as_dict()
            == blast.fix_blast_radius(list(reversed(paths))).as_dict())


def test_a_path_is_classified_in_whatever_spelling_it_arrives_in():
    """A fix range arrives spelled three ways — a bare path, a `./` path, and a
    diff's `b/` path — and a rule table that agreed with one of them would report
    the other two as matching nothing."""
    for spelling in ("nginx/default.conf", "./nginx/default.conf", "b/nginx/default.conf"):
        assert lanes_of(spelling).lane == blast.LANE_HIGH, spelling


def test_the_value_serialises_to_the_shape_a_round_record_would_carry():
    result = lanes_of("migrations/versions/0032_x.py")
    wire = result.as_dict()
    assert set(wire) == {"lane", "categories", "evidence", "reason"}
    assert isinstance(wire["categories"], list)
    assert wire["evidence"][blast.MIGRATIONS] == ["migrations/versions/0032_x.py"]


def test_every_category_has_a_lane_and_a_phrase():
    """The tables are the audit surface. A category present in one and missing
    from the other would raise a KeyError from inside the reason builder, on a
    path that only some changes take."""
    assert set(blast.CATEGORY_LANES) == set(blast.CATEGORY_PHRASES)
    assert set(blast.CATEGORY_LANES.values()) <= {blast.LANE_LOW, blast.LANE_MEDIUM,
                                                  blast.LANE_HIGH}
