{
  description = "quarterback — the agent coordination board, and the harness it coordinates";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      # Step 2 of the install: the harness. The service (step 1) is deployed as
      # containers and is deliberately NOT a flake output — it is a Dockerfile and
      # a compose stack, and pretending otherwise would imply a nix deployment path
      # that does not exist. See DEPLOY.md.
      packages = forAllSystems (pkgs: rec {
        harness = pkgs.callPackage ./harness/package.nix { };
        default = harness;
      });

      # Wires the harness into ~/.claude: the loops engine, the slash commands, and —
      # since #230 — the board itself. Enabling it and naming a board gives a host the
      # seven Claude Code lifecycle hooks, the stdio MCP registration and the site
      # config that qb-hook, qb and qb-mcp all read, so presence, leases, the ask
      # courier, overlap detection and sync advice work without the consumer
      # reassembling any of it by hand. This is what a home-manager consumer imports
      # instead of hand-listing every command file and hand-writing the wiring.
      #
      # That sentence used to name a second directory this module has never written to,
      # and to deliver no board at all: it installed the package, ~/.claude/loops and the
      # command files, while every mechanism that makes the harness a board CLIENT lived
      # in whatever personal config the consumer happened to keep — on a different pin
      # from this flake, which is skew nothing could see. It does not any more (#230).
      homeManagerModules = rec {
        quarterback-harness = import ./harness/hm-module.nix;
        default = quarterback-harness;
      };

      # `nix flake check` runs the harness's own test suites. Worth having as flake
      # check rather than only in GitHub Actions: the harness's consumers are nix
      # builds, so a consumer that pins a broken revision finds out at build time
      # instead of the next time a review runs.
      checks = forAllSystems (pkgs: {
        harness-build = self.packages.${pkgs.system}.harness;

        loops-tests = pkgs.runCommand "quarterback-loops-tests"
          {
            # git is a genuine test dependency, not incidental: harness_rules
            # detects a repo's GitHub slug and default branch by shelling out to
            # it, and the panel's token resolution asks git whether .env is tracked.
            nativeBuildInputs = [
              (pkgs.python3.withPackages (ps: [ ps.pytest ]))
              pkgs.git
            ];
          } ''
          # The suite is laid out as repo/harness/loops, not as a bare loops/,
          # because test_panel_dials.py reads the two review briefs at
          # `Path(__file__).parents[3] / "harness/commands"`. Copied flat, that
          # resolves to / and the reads error as FileNotFoundError rather than
          # being asserted — the same way #163's did, in a different check.
          mkdir -p repo/harness
          cp -r ${./harness/loops} repo/harness/loops
          cp -r ${./harness/commands} repo/harness/commands
          # `harness_rules.check_status` reads the CI check vocabulary out of
          # `qbdata.py`, which lives in bin/ because the dashboards can only import a
          # sibling of their own $0 (harness/package.nix says why). One file, not the
          # tree: nothing here execs a worktree script, it is imported for #324's
          # six-state classifier and nothing else.
          install -Dm644 ${./harness/bin/qbdata.py} repo/harness/bin/qbdata.py
          # The rules baseline, because the panel refuses to review a repo that
          # configured nothing — so without it the cap guard's two tests get that
          # refusal instead of the cap they are asserting.
          cp ${./.harness-rules.sample} repo/.harness-rules.sample
          # flake.nix itself, so the brief-coupling guard runs HERE rather than
          # skipping — it compares this check's copy list against the paths the suite
          # reads, and a guard that is inert in the sandbox it protects is no guard.
          # Same reason release-metadata-tests copies it in.
          cp ${./flake.nix} repo/flake.nix
          chmod -R u+w repo
          # A real repository, because `panel.main()` resolves the round cap
          # through harness_rules, which shells out to git and refuses a
          # directory that is not a checkout. Without this the cap guard's two
          # tests fail on "is not a git repository" instead of asserting the cap
          # — a check that reports the sandbox rather than the code.
          export HOME=$TMPDIR
          git -C repo init -q -b main
          # …and an origin, because the harness addresses repos as
          # `gh --repo owner/name` and derives that slug from this remote.
          git -C repo remote add origin https://github.com/prisonblues/quarterback.git
          git -C repo -c user.email=b@build -c user.name=build \
              -c commit.gpgsign=false commit -q --allow-empty -m "sandbox"
          cd repo/harness/loops
          # -p no:cacheprovider: the store is read-only and pytest would otherwise
          # try to write .pytest_cache beside the tests.
          pytest -q -rs -p no:cacheprovider > report.txt 2>&1 || {
            cat report.txt >&2
            exit 1
          }
          cat report.txt
          # -rs prints the reason for every skip, and the check below turns those lines
          # into a build result. pytest exits 0 with any number of skips, and a skip
          # nobody reads is what #163 and #246 both looked like — an assertion that
          # reported nothing, in a check wearing a green badge.
          #
          # NO skip is expected here. The one test that can skip is the brief-coupling
          # guard, when flake.nix is not beside the suite; it is copied in above
          # precisely so that it is, so a skip from it means the copy line went away
          # and took this sandbox's only staleness check out of the build with it.
          if grep -q '^SKIPPED' report.txt; then
            echo "a test skipped in the loops sandbox, and none is expected here. If it" >&2
            echo "is the brief-coupling guard, flake.nix stopped being copied in and" >&2
            echo "nothing is checking this copy list any more:" >&2
            grep '^SKIPPED' report.txt >&2
            exit 1
          fi
          touch $out
        '';

        # The worktree scripts have their own suite, driving the bash as a
        # subprocess against a stub board. Separate from loops-tests because it
        # needs the layout the scripts assume — bin/ and tests/ as siblings —
        # and different runtime tools (curl and jq, which the scripts shell out
        # to; a build without them would pass by taking the "could not tell"
        # path everywhere, which is the one outcome the suite must not fake).
        worktree-tests = pkgs.runCommand "quarterback-worktree-tests"
          {
            nativeBuildInputs = [
              (pkgs.python3.withPackages (ps: [ ps.pytest ]))
              pkgs.bash
              pkgs.git
              pkgs.curl
              pkgs.jq
              # tmux is not incidental either: the qb-seats suite drives a real
              # server and asserts on the panes it ends up with. Without it here
              # those tests would SKIP rather than fail, and a skipped test that
              # nobody notices is worse than an absent one — the CI summary reads
              # green either way.
              pkgs.tmux
            ];
          } ''
          mkdir harness
          cp -r ${./harness/bin} harness/bin
          cp -r ${./harness/tests} harness/tests
          # harness/loops as a tree, and NOT because a test in it runs here — the
          # pytest invocation below names `tests` and collects nothing from it.
          # test_runtime_stub_shebangs.py READS it: the rule it enforces is about
          # the nix sandbox rather than about a directory, and `loops-tests` is a
          # sandbox with no /usr/bin/env in it either. Without this line the guard
          # would quietly scan one of the two trees it names, here, in the only
          # check that runs it — a guard that is inert in the sandbox it protects
          # is no guard, which is the lesson prose-consistency-tests below was
          # built out of. Same shape as that check's own copy of this tree: input
          # to a guard, not a suite to run.
          cp -r ${./harness/loops} harness/loops
          # harness/githooks holds the two scripts `qb-hooks` copies into a repo's
          # common git dir — the shared-refs/stash guard and its delegating forwarder
          # (#210). test_stash_guard.py installs them into real fixture repos and then
          # runs git against them, so without this line every assertion about the guard
          # errors on a missing file rather than being evaluated (#163).
          cp -r ${./harness/githooks} harness/githooks
          # test_claude_wiring.py's reads (#230). It stays in THIS check rather than
          # joining prose-consistency-tests, and the split is the same one that check's
          # comment draws: two thirds of it drives `qb-claude-setup` and `qb-hook` as
          # subprocesses against a temporary $HOME, so it needs the jq and bash above,
          # which that sandbox deliberately does not have. Its assertions ON this repo's
          # own text — the module's options, what the package ships, this very list —
          # ride along, because the subject is one mechanism and splitting the file by
          # which tool each assertion needs would put the coupling guard (the hook's
          # dispatch switch against the fragment) in neither half.
          #
          # harness/claude as a TREE: the fragment is read, and so is the workflow doc's
          # presence, and a wiring that grows a second data file should not need a line here.
          cp -r ${./harness/claude} harness/claude
          # -D so a file whose parent this sandbox has never held brings the parent with
          # it, and 644 because the chmod below only reaches what `cp -r` made read-only.
          install -Dm644 ${./harness/hm-module.nix} harness/hm-module.nix
          install -Dm644 ${./harness/package.nix} harness/package.nix
          # test_qb_doctor.py's two shipping guards (#204). It asserts that
          # `package.nix`'s installPhase comment — the argued list of what belongs on
          # PATH and why — accounts for `qb-doctor`, and that the README documents all
          # four verdicts, because a doctor whose `?` is undocumented gets read as a
          # `warn` and then as an `ok`. Both are reads of repo prose from a suite that
          # otherwise drives real git repositories, which is why they ride along here
          # rather than moving to prose-consistency-tests, exactly as test_claude_wiring's
          # do. Without the README line those two assertions would ERROR on a missing
          # file rather than be evaluated — #163's mechanism, and the reason five suites
          # before them sat red in a check no workflow runs.
          install -Dm644 ${./harness/README.md} harness/README.md
          # At the top, not under harness/: the suite computes the repo root as its own
          # parent.parent, so this is where a repo-root file has to sit. It is here so the
          # sandbox guard runs HERE — it compares this list against the paths the suite
          # reads, and a guard inert in the sandbox it protects is no guard, which is the
          # point Codex made on #264's earlier cut and it holds identically for this one.
          install -Dm644 ${./flake.nix} flake.nix
          chmod -R u+w harness
          # test_release_numbers.py is not a harness test and cannot run in this
          # sandbox: it reads CHANGELOG.md, README.md, pyproject.toml, app/main.py
          # and flake.nix at the repo root, none of which this check contains. It
          # has its own check below, with a source that does. Removed rather
          # than --ignore'd so that renaming the file fails HERE, loudly, in the
          # build that would otherwise carry on collecting it — a `rm` of a path
          # that is gone is an error, and the person renaming it has to look at
          # both checks.
          rm harness/tests/test_release_numbers.py
          # Nor is test_fixer_escalation.py, for the same reason and by the same
          # mechanism (#251): it reads ten repo-root files across app/ and
          # harness/commands/, none of which this check contains, so all ten errored
          # here. It is a prose-consistency suite, not a worktree-scripts one — this
          # check exists for the layout the bash scripts assume and the curl/jq they
          # shell out to — so it gets its own check below rather than dragging the
          # application tree into this sandbox. `rm`, not --ignore, for the reason
          # above.
          rm harness/tests/test_fixer_escalation.py
          # And test_regression_test_redgreen.py (#257), which reads the command briefs
          # via its own parents[1] and imports panel_core out of harness/loops. Fourth
          # instance of the same mismatch, and the one that made a category check the
          # answer rather than a fourth check. To its credit it is the only one of the
          # four that failed LOUDLY here — "this suite is now green about nothing" —
          # rather than erroring on a missing file.
          rm harness/tests/test_regression_test_redgreen.py
          # The category's own guards and their helpers go with them: they compare the
          # prose-consistency check's install list against what its suites read, which is a
          # question about a different check than this one. test_commands_wired.py is NOT one
          # of those — it compares hm-module.nix's `commands` default against the briefs
          # directory and knows nothing about install lists. It leaves for the same reason as
          # the two suites above it: it reads harness/hm-module.nix and globs harness/commands,
          # neither of which this check holds. Its module-level `parametrize` calls read both at
          # COLLECTION time, so it has been erroring here since it landed — which is why the
          # count of this mechanism's instances is five, not four.
          rm harness/tests/test_commands_wired.py
          rm harness/tests/test_prose_sandbox.py
          rm harness/tests/_prose_sandbox.py
          rm harness/tests/test_flake_sandbox.py
          # _flake_sandbox.py STAYS, and it is the one member of that group that does.
          # It is a helper rather than a guard — parsing a check's copy lines, which #264
          # factored out precisely because the job is identical for every suite with this
          # problem — and since #230 this check has a member that uses it:
          # test_claude_wiring.py compares this list against the four paths it reads. Remove
          # it here and those assertions do not fail, they SKIP; and this check allowlists no
          # skips (it has legitimate ones), so they would go quiet in the sandbox they exist
          # to protect. That is the point Codex made on #264's earlier cut, one check along.
          # `test_flake_sandbox.py` above is the guard ON this helper and still belongs to
          # the prose check, which is where the helper's own behaviour is asserted.
          # Same treatment the package gets: there is no /usr/bin/env in the
          # sandbox, so an unpatched `#!/usr/bin/env bash` fails to exec at all
          # — and every test then fails for a reason that has nothing to do with
          # the code under test.
          # githooks too: they are execed BY GIT as hooks, so an unpatched
          # `#!/usr/bin/env bash` makes every ref transaction in the fixture
          # repos fail to run the guard at all.
          patchShebangs harness/bin harness/githooks
          cd harness
          export HOME=$TMPDIR
          git config --global init.defaultBranch main
          pytest -q -p no:cacheprovider tests
          touch $out
        '';

        # The release-metadata suite, which is not a harness suite at all: it asserts
        # that the release number written in CHANGELOG.md, README.md, pyproject.toml
        # and app/main.py agrees with itself. It lives under `harness/tests/` for the
        # reason its own docstring gives — the top-level `tests/conftest.py` resolves
        # DATABASE_URL and imports the app, which made the cheapest check in the repo
        # the hardest to run — and that is what put it in a sandbox holding only
        # `harness/`, where every one of its assertions errored on a missing file
        # instead of being evaluated (#163). So it gets a sandbox holding the
        # repo-root files it actually reads.
        #
        # Those files are copied in one by one rather than the repo root wholesale:
        # `./.` would drag a developer's `mcp/.venv` and every `__pycache__` into the
        # store. Enumeration is the thing that goes stale, so nothing relies on
        # somebody remembering it —
        # `test_the_flake_check_supplies_every_repo_root_file_this_suite_reads` in the
        # suite compares this list against the paths the suite reads, in both
        # directions, and that is why flake.nix is copied in below alongside them.
        release-metadata-tests = pkgs.runCommand "quarterback-release-metadata-tests"
          {
            # git is deliberately ABSENT. One test asks git for the repo's tracked
            # files to check that no test file is named after a release; a store
            # sandbox is not a checkout, so with git present that test skips on "not
            # a git checkout" and without it skips on "git is not on PATH". Identical
            # outcome, one less dependency, and both skip paths are asserted in the
            # suite itself. It is not going unchecked: CI runs this suite in a real
            # checkout on every push, and that is where the question "what does this
            # repo track" can be answered at all.
            nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.pytest ])) ];
          } ''
          # The layout matters, not just the file set: the suite computes its repo root
          # as the test file's parent.parent.parent, so the test has to sit two
          # directories below the files it reads. `install -D` rather than `cp` so a
          # file under a directory this sandbox has never held brings its own parent
          # with it — the coupling guard's remediation text says to add a copy line for
          # each missing path, and following that advice should not produce a second,
          # unrelated "No such file or directory".
          install -Dm644 ${./harness/tests/test_release_numbers.py} repo/harness/tests/test_release_numbers.py
          install -Dm644 ${./harness/tests/_flake_sandbox.py} repo/harness/tests/_flake_sandbox.py
          install -Dm644 ${./CHANGELOG.md}   repo/CHANGELOG.md
          install -Dm644 ${./README.md}      repo/README.md
          # The README's release list is RENDERED from the CHANGELOG's order (#296), and the
          # suite asserts the file matches the render — so the renderer is part of the
          # question, not a tool beside it. It imports the stamper by path for the one
          # definition of a release heading, which is why both are here; the suite records
          # the stamper in `_COPIED_BUT_NOT_READ` because it reaches it through an import.
          install -Dm644 ${./scripts/readme_releases.py} repo/scripts/readme_releases.py
          install -Dm644 ${./scripts/release_stamp.py}   repo/scripts/release_stamp.py
          install -Dm644 ${./pyproject.toml} repo/pyproject.toml
          install -Dm644 ${./app/main.py}    repo/app/main.py
          install -Dm644 ${./flake.nix}      repo/flake.nix
          cd repo/harness
          # An empty inifile, and `-c` to pin it. pyproject.toml is copied in above
          # because the suite READS it; left to itself pytest would also adopt it as
          # this run's config, activating everything under `[tool.pytest.ini_options]`
          # — `testpaths`, `asyncio_mode`, and whatever the app suite needs next — in a
          # sandbox that holds pytest and nothing else. That would turn an edit made
          # for the app suite into an "unrecognized arguments" failure in a check
          # nobody touched. It also pins the rootdir here, so the `tests` argument
          # below resolves against this directory rather than the repo root.
          printf '[pytest]\n' > pytest.ini
          pytest -q -rs -p no:cacheprovider -c pytest.ini tests > report.txt 2>&1 || {
            cat report.txt >&2
            exit 1
          }
          cat report.txt
          # -rs prints the reason for every skip; the three checks below are what turn
          # those lines into a build result. pytest exits 0 with any number of skips,
          # and this check exists because assertions were inert and said so only in
          # ERROR lines nobody read — a skip is the same failure wearing a green badge.
          #
          # Exactly two skips are expected: the git-tracked-filenames test (no git,
          # above) and the unstamped-entry test on a branch not writing a release. The
          # skip this is really guarding against is the coupling guard's own — it skips
          # when flake.nix is not beside the suite, so dropping the copy line above
          # would take this sandbox's only staleness check out of the build silently.
          grep '^SKIPPED' report.txt \
            | grep -v -e 'git is not on PATH' -e 'nothing unstamped' > unexpected.txt || true
          if [ -s unexpected.txt ]; then
            echo "a test skipped in the release-metadata sandbox for a reason this check" >&2
            echo "does not expect. A test that reports nothing is what #163 looked like:" >&2
            echo "give it what it needs here, or add the reason to the list in flake.nix." >&2
            cat unexpected.txt >&2
            exit 1
          fi
          grep -q 'git is not on PATH' report.txt || {
            echo "the git-tracked-filenames test did not skip. There is no git in this" >&2
            echo "sandbox, so either the suite was not collected at all or that test" >&2
            echo "stopped reporting — both are the failure this check exists to catch." >&2
            exit 1
          }
          touch $out
        '';

        # The prose-consistency suites: the ones under `harness/**/tests` whose subject
        # is this repo's own text and the code it describes, rather than the worktree
        # scripts. They read briefs, READMEs and the modules those describe, and need
        # nothing else — no git, no tmux, no network, no database.
        #
        # A check for the CATEGORY rather than one per suite, deliberately. Every one of
        # these lived in `worktree-tests`, whose sandbox holds `harness/bin` and
        # `harness/tests`, so their repo-root reads errored on missing files instead of
        # being asserted — #163's mechanism, five times over across four checks (#163,
        # #246, #251, #257, and test_commands_wired.py, which had been erroring at
        # COLLECTION since it landed). A fifth near-identical check was the alternative;
        # one place to add the sixth suite is worth more than per-suite precision here,
        # since these sandboxes want the same thing.
        #
        # Members are listed in `_prose_sandbox.MEMBERS`, not here: that list is what the
        # guard iterates, and it is held against the suites installed below in both
        # directions, so a second copy of it in a comment could only ever go stale. A suite
        # in this sandbox but absent from MEMBERS would have its reads compared against
        # nothing, and a member this check does not run would have its declaration checked
        # against a sandbox it never sees; both are failures, and both are asserted.
        #
        # Each member declares its reads and routes every one through an accessor that
        # refuses an undeclared path — three members, three accessors, no shared name for
        # them. `test_the_check_supplies_every_path_its_suites_read` and its converse in
        # `test_prose_sandbox.py` are the comparison; `_flake_sandbox` is the reader they
        # share with release-metadata-tests, so there is one parser for this file rather
        # than one per suite.
        #
        # Copied one by one rather than the repo root wholesale, for the reason
        # release-metadata-tests gives: `./.` would drag a developer's `mcp/.venv` and
        # every `__pycache__` into the store. flake.nix is copied in so the comparison
        # named above runs HERE and not only in a checkout — a guard that is inert in the
        # sandbox it protects is no guard.
        prose-consistency-tests = pkgs.runCommand "quarterback-prose-consistency-tests"
          {
            nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.pytest ])) ];
          } ''
          # The layout matters as much as the file set: the suite computes its repo root
          # as the test file's parent.parent.parent, so the test has to sit two
          # directories below the files it reads. `install -D` rather than `cp` so a
          # file under a directory this sandbox has never held brings its own parent
          # with it, and following the guard's "add an install line" advice cannot
          # produce a second, unrelated "No such file or directory".
          install -Dm644 ${./harness/tests/test_fixer_escalation.py}         repo/harness/tests/test_fixer_escalation.py
          install -Dm644 ${./harness/tests/test_regression_test_redgreen.py} repo/harness/tests/test_regression_test_redgreen.py
          install -Dm644 ${./harness/tests/test_commands_wired.py}           repo/harness/tests/test_commands_wired.py
          install -Dm644 ${./harness/tests/test_prose_sandbox.py}            repo/harness/tests/test_prose_sandbox.py
          install -Dm644 ${./harness/tests/_prose_sandbox.py}                repo/harness/tests/_prose_sandbox.py
          install -Dm644 ${./harness/tests/_flake_sandbox.py}                 repo/harness/tests/_flake_sandbox.py
          install -Dm644 ${./harness/tests/test_flake_sandbox.py}             repo/harness/tests/test_flake_sandbox.py
          # The briefs as a tree, and not as a file list, because one of the suites GLOBS
          # this directory to ask which briefs exist — the directory is the question, so a
          # list of files could not express it. The others name individual briefs, and which
          # ones is a judgement that moves as loops are added. No count here on purpose: the
          # union is in the suites' own READS, and a number in a comment is the thing that
          # goes stale.
          cp -r ${./harness/commands} repo/harness/commands
          # harness/loops as a tree because it has to be IMPORTABLE, not because its
          # files are read: test_regression_test_redgreen.py asks Python for
          # panel_core.REVIEW_PROMPT — the one read of a prompt that cannot drift from
          # what the panel sends — and panel_core imports harness_rules, which imports
          # further modules in the package. A file list for a Python package goes stale
          # on every refactor, and the failure would be an ImportError naming a module
          # rather than a path. Recorded as such in `_prose_sandbox.TREES`.
          cp -r ${./harness/loops} repo/harness/loops
          install -Dm644 ${./harness/hm-module.nix}                          repo/harness/hm-module.nix
          install -Dm644 ${./harness/README.md}                              repo/harness/README.md
          install -Dm644 ${./app/api/reviews.py}                             repo/app/api/reviews.py
          install -Dm644 ${./app/models/review.py}                           repo/app/models/review.py
          install -Dm644 ${./flake.nix}                                      repo/flake.nix
          chmod -R u+w repo/harness/loops repo/harness/commands
          cd repo/harness
          # An empty inifile, and `-c` to pin it: the same reasoning as
          # release-metadata-tests, minus the part about pyproject.toml, which this
          # sandbox does not hold. It pins the rootdir here so `tests` below resolves
          # against this directory.
          printf '[pytest]\n' > pytest.ini
          pytest -q -rs -p no:cacheprovider -c pytest.ini tests > report.txt 2>&1 || {
            cat report.txt >&2
            exit 1
          }
          cat report.txt
          # pytest exits 0 with any number of skips, and a skip nobody reads is exactly what
          # every instance of this mechanism looked like from the outside.
          #
          # The one skip this check expects is none at all, and the reason is specific: the
          # only test here that can skip is test_prose_sandbox.py's flake_text fixture, when
          # flake.nix is not beside the suites. It is installed above precisely so that it is,
          # so a skip from it means that line went away and took the whole comparison with it.
          #
          # Named by test rather than counted, because this check serves three suites and a
          # bare "expect zero" becomes the wrong diagnosis the day any member gains a skipif —
          # the sibling check above allowlists its skips by reason for the same reason.
          if grep -q '^SKIPPED' report.txt; then
            echo "a test skipped in the prose-consistency sandbox, where none is expected." >&2
            echo "If it is test_prose_sandbox.py's flake_text fixture, flake.nix stopped" >&2
            echo "being installed and nothing is comparing this check against its suites" >&2
            echo "any more. If it is a member's own skipif, add its reason to an allowlist" >&2
            echo "here rather than deleting this guard:" >&2
            grep '^SKIPPED' report.txt >&2
            exit 1
          fi
          touch $out
        '';

        # The home-manager module, EVALUATED. Until #230 nothing in this repo evaluated it
        # at all: `nix flake check` prints "unknown flake output 'homeManagerModules'" and
        # walks past, the GitHub jobs run pytest, and the only thing that ever forced this
        # file was a consumer's own rebuild. So the module — the artifact a consumer
        # actually imports — was the least-checked file in the tree, and a syntax error or a
        # bad option type in it would ship and break every consumer's switch rather than
        # anything here.
        #
        # The suite in harness/tests asserts on the module's TEXT, deliberately, so it runs
        # in CI with no nix. This asserts on what the module PRODUCES, which text cannot
        # reach: that the activation entry exists with the right dependencies, that the site
        # config renders, and that each opt-out actually removes something.
        #
        # It is not a home-manager integration test and does not pretend to be. The stub
        # below declares the four options the module writes to, and `lib.hm.dag` is stubbed
        # faithfully enough for `entryAfter` to be inspected. If the module grows a write to
        # an option this stub does not declare, THIS check fails with "The option `x' does
        # not exist" — loudly, locally, with the fix being one line here. That is the right
        # failure; a stub that accepted anything would make this check an expensive way of
        # confirming the file parses.
        hm-module-eval =
          let
            # home-manager extends nixpkgs' lib with `hm`; the module uses exactly one thing
            # out of it. Injected through specialArgs, which is how home-manager itself
            # hands its extended lib to modules.
            hmLib = pkgs.lib.extend (final: prev: {
              hm.dag = {
                entryAfter = after: data: { inherit after data; before = [ ]; };
                entryBefore = before: data: { inherit before data; after = [ ]; };
                entryAnywhere = data: { inherit data; after = [ ]; before = [ ]; };
              };
            });
            stub = { lib, ... }: {
              options = {
                home.packages = lib.mkOption { type = lib.types.listOf lib.types.package; default = [ ]; };
                home.file = lib.mkOption { type = lib.types.attrsOf (lib.types.attrsOf lib.types.anything); default = { }; };
                home.activation = lib.mkOption { type = lib.types.attrsOf lib.types.anything; default = { }; };
                xdg.configFile = lib.mkOption { type = lib.types.attrsOf lib.types.anything; default = { }; };
                # home-manager's own, via modules/misc/assertions.nix.
                assertions = lib.mkOption { type = lib.types.listOf lib.types.unspecified; default = [ ]; };
                warnings = lib.mkOption { type = lib.types.listOf lib.types.str; default = [ ]; };
              };
            };
            with' = settings: (pkgs.lib.evalModules {
              specialArgs = { lib = hmLib; inherit pkgs; };
              modules = [ stub ./harness/hm-module.nix { programs.quarterback-harness = settings; } ];
            }).config;

            # `throw` and not `assert`: an assert prints the expression, and the expression
            # here is a set membership nobody can read a diagnosis out of.
            expect = cond: msg: if cond then true else throw "hm-module-eval: ${msg}";
            has = set: name: builtins.hasAttr name set;

            wired = with' {
              enable = true;
              board.url = "https://board.example";
              board.tokenCommand = "cat /run/secrets/tok";
            };
            noBoard = with' { enable = true; };
            noWiring = with' { enable = true; claude.enable = false; };
            ordered = with' { enable = true; claude.activationAfter = [ "theirMerge" ]; };
            noMcp = with' { enable = true; claude.registerMcp = "never"; };
            disabled = with' { enable = false; };
            badQuote = with' { enable = true; board.url = "x"; board.tokenCommand = "op read op://a/b's/c"; };
            # A URL that is entirely legal and, unquoted, ends the assignment at the `&`
            # and runs the rest as a background command every time the config is sourced.
            queryUrl = with' {
              enable = true;
              board.url = "https://board.example/x?a=1&b=2";
              board.tokenCommand = "cat /run/secrets/tok";
            };

            act = c: c.home.activation.quarterbackClaudeWiring;
          in
          pkgs.writeText "quarterback-hm-module-eval" (builtins.toJSON {
            # AC: enabling the module and naming a board gives a host the wiring.
            activationExists = expect (has wired.home.activation "quarterbackClaudeWiring")
              "enabling the module does not add the wiring activation entry";
            activationRunsTheScript = expect
              (pkgs.lib.hasInfix "/bin/qb-claude-setup" (act wired).data)
              "the activation does not run qb-claude-setup out of the package";
            activationAfterWriteBoundary = expect
              ((act wired).after == [ "writeBoundary" ])
              "the wiring no longer waits for writeBoundary, so it can run before the files land";
            # AC: it composes with a consumer that also merges into settings.json.
            activationAfterIsExtensible = expect
              ((act ordered).after == [ "writeBoundary" "theirMerge" ])
              "claude.activationAfter does not reach the DAG entry, so a consumer cannot order us";
            # AC: a host that names a board gets the site config every wrapper reads.
            configRendered = expect
              (pkgs.lib.hasInfix "QUARTERBACK_BASE_URL='https://board.example'"
                wired.xdg.configFile."quarterback/config".text)
              "the site config does not carry the board URL, single-quoted";
            # The config file is SOURCED. An unquoted value ends at the first `&`, `;` or
            # space, and the rest of the line is executed — silently, on every board call.
            configQuotesTheUrl = expect
              (pkgs.lib.hasInfix "QUARTERBACK_BASE_URL='https://board.example/x?a=1&b=2'"
                queryUrl.xdg.configFile."quarterback/config".text)
              "a URL with a query string is emitted unquoted, so sourcing the config runs the half after the &";
            configQuotesTheAgent = expect
              (pkgs.lib.hasInfix "QUARTERBACK_AGENT='zeus box'"
                (with' { enable = true; board.url = "x"; board.tokenCommand = "t"; board.agent = "zeus box"; })
                  .xdg.configFile."quarterback/config".text)
              "the agent name is emitted unquoted, so a value with a space becomes a command";
            configQuotesTheTokenCommand = expect
              (pkgs.lib.hasInfix "QUARTERBACK_TOKEN_CMD='cat /run/secrets/tok'"
                wired.xdg.configFile."quarterback/config".text)
              "the token command is not single-quoted, so it is expanded before it is evaluated";
            # ...and a host that names none collides with nobody: `null` means "I own that file".
            noConfigWithoutABoard = expect (!(has noBoard.xdg.configFile "quarterback/config"))
              "the module renders quarterback/config with no board named, which collides with every consumer who renders it themselves";
            warnsAboutAUselessBoard = expect (builtins.length noMcp.warnings == 0
              && builtins.length (with' { enable = true; board.url = "x"; }).warnings == 1)
              "a board URL with no token command no longer warns, and the failure it causes is silent";
            # AC: opting out is possible.
            optOutRemovesTheActivation = expect (!(has noWiring.home.activation "quarterbackClaudeWiring"))
              "claude.enable = false still installs the activation";
            optOutRemovesTheWorkflowDoc = expect
              (!(has noWiring.home.file ".claude/quarterback-workflow.md"))
              "claude.enable = false still links the workflow doc, whose @import the wiring adds";
            mcpModeReachesTheScript = expect (pkgs.lib.hasInfix "--mcp never" (act noMcp).data)
              "claude.registerMcp does not reach the wiring invocation";
            # The eval-time guard on a value that would fail at runtime as "no token".
            singleQuoteIsRefused = expect
              (builtins.any (a: !a.assertion) badQuote.assertions)
              "a tokenCommand containing a single quote is accepted, and it breaks token resolution on every call";
            singleQuoteIsRefusedInTheUrlToo = expect
              (builtins.any (a: !a.assertion)
                (with' { enable = true; board.url = "https://a'b"; }).assertions)
              "a board.url containing a single quote is accepted, and it breaks the config file it is emitted into";
            doubleQuoteIsRefusedInTheRepo = expect
              (builtins.any (a: !a.assertion)
                (with' { enable = true; board.url = "x"; board.tokenCommand = "t"; board.repo = "$HOME/a\"b"; }).assertions)
              "a board.repo containing a double quote is accepted, and it is emitted double-quoted";
            quotelessIsAccepted = expect (builtins.all (a: a.assertion) wired.assertions)
              "an ordinary tokenCommand trips an assertion";
            # And the module is inert when it is off.
            disabledInstallsNothing = expect
              (disabled.home.file == { } && disabled.home.activation == { }
                && disabled.home.packages == [ ])
              "the module writes something with enable = false";
            # The commands still land, which is what the module did before it did anything else.
            commandsStillLand = expect
              (has wired.home.file ".claude/loops" && has wired.home.file ".claude/commands/fix-issue.md")
              "the loops or the slash commands stopped being linked";
          });

        # The board client (#110) and the HTTP client it shares with the MCP
        # server. A check rather than only a GitHub job for the same reason the
        # two above are: `harness/bin/qb-board` ships in the package, so a
        # consumer pinning a revision whose client is broken should find out at
        # build time.
        #
        # `mcp[cli]` is deliberately absent — nothing under test imports the MCP
        # SDK, and pulling it in would make this check fail on the day that
        # package does, for a reason unrelated to the client. That holds because
        # `mcp/mcp_server/__init__.py` is one docstring and imports nothing, so
        # `import mcp_server.board` executes no SDK code on the way. It is an
        # assumption about a file NOT in this expression, which is why
        # `tests/test_package_contract.py` asserts it: add a re-export to that
        # `__init__.py` and the suite says so here, rather than this check going
        # red for a consumer while the GitHub job — which installs more — stays
        # green. The same file pins `python3` against `requires-python` and
        # `textual` against the `tui` extra's `>=1.0`, neither of which anything
        # else compares the (floating) nixpkgs versions below to.
        mcp-tests = pkgs.runCommand "quarterback-mcp-tests"
          {
            nativeBuildInputs = [
              (pkgs.python3.withPackages (ps: with ps; [
                pytest pytest-asyncio httpx textual
              ]))
              pkgs.git
              # bash: config resolution sources the per-host config file, and
              # the local-action tests build real repositories.
              pkgs.bash
              # A CA bundle, not because anything here talks to a board: httpx
              # builds its default SSL context when a client is CONSTRUCTED, and
              # in a sandbox with no /etc/ssl that raises before a single header
              # can be inspected.
              pkgs.cacert
            ];
          } ''
          # The two directories by name, not the whole of mcp/: that directory
          # also holds a developer's .venv, which is a large symlinked tree and
          # has no business in the store. pyproject.toml comes too, because the
          # constraints this check floats against — `requires-python` and the
          # `tui` extra's `textual>=1.0` — are written there and read by
          # tests/test_package_contract.py; without it those assertions can only
          # be made where nixpkgs is not what supplies the packages.
          mkdir mcp
          cp -r ${./mcp/mcp_server} mcp/mcp_server
          cp -r ${./mcp/tests} mcp/tests
          cp ${./mcp/pyproject.toml} mcp/pyproject.toml
          chmod -R u+w mcp
          cd mcp
          export HOME=$TMPDIR
          export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
          git config --global user.email "nix@example.invalid"
          git config --global user.name "Nix"
          git config --global init.defaultBranch main
          # -o asyncio_mode=auto: pytest.ini_options in mcp/pyproject.toml is not
          # read here (no project install), and without it every pilot-driven
          # test is collected as an un-awaited coroutine and skipped.
          pytest -q -p no:cacheprovider -o asyncio_mode=auto tests
          touch $out
        '';
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: with ps; [ pytest pytest-asyncio ]))
            pkgs.ruff
            pkgs.jq
          ];
        };
      });
    };
}
