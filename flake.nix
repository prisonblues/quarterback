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

      # Wires the harness into ~/.claude and ~/.local/bin. This is what a
      # home-manager consumer imports instead of hand-listing every command file.
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
          cp -r ${./harness/loops} loops
          chmod -R u+w loops
          cd loops
          # -p no:cacheprovider: the store is read-only and pytest would otherwise
          # try to write .pytest_cache beside the tests.
          pytest -q -p no:cacheprovider
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
          chmod -R u+w harness
          # test_release_numbers.py is not a harness test and cannot run in this
          # sandbox: it reads CHANGELOG.md, README.md, pyproject.toml and
          # app/main.py at the repo root, which this check does not contain. It
          # has its own check below, with a source that does. Removed rather
          # than --ignore'd so that renaming the file fails HERE, loudly, in the
          # build that would otherwise carry on collecting it — a `rm` of a path
          # that is gone is an error, and the person renaming it has to look at
          # both checks.
          rm harness/tests/test_release_numbers.py
          # Same treatment the package gets: there is no /usr/bin/env in the
          # sandbox, so an unpatched `#!/usr/bin/env bash` fails to exec at all
          # — and every test then fails for a reason that has nothing to do with
          # the code under test.
          patchShebangs harness/bin
          cd harness
          export HOME=$TMPDIR
          git config --global init.defaultBranch main
          pytest -q -p no:cacheprovider tests
          touch $out
        '';

        # The release-metadata suite, which is not a harness suite at all: it
        # asserts that the release number written in CHANGELOG.md, README.md,
        # pyproject.toml and app/main.py agrees with itself. It lives under
        # `harness/tests/` for the reason its own docstring gives — the top-level
        # `tests/conftest.py` resolves DATABASE_URL and imports the app, which
        # made the cheapest check in the repo the hardest to run — and that is
        # what put it in a sandbox holding only `harness/`, where every one of
        # its assertions errored on missing files instead of being evaluated
        # (#163). So it gets a sandbox holding exactly the files it reads —
        # counting them here would only go stale the next time the suite reads
        # one more.
        #
        # This is the check that would have caught the v2.34 collision two
        # branches created on 2026-08-17, and it has never once run here.
        #
        # The `cp`s are enumerated rather than the repo root being copied
        # wholesale: `./.` would drag a developer's `mcp/.venv` and every
        # `__pycache__` into the store. Enumeration is the thing that can go
        # stale, so it does not rely on someone remembering —
        # `test_the_flake_check_supplies_every_repo_root_file_this_suite_reads`
        # in the suite itself compares this list against the paths the suite
        # reads, and that is why flake.nix is copied in below alongside them.
        release-metadata-tests = pkgs.runCommand "quarterback-release-metadata-tests"
          {
            # git is deliberately ABSENT. One test asks git for the repo's
            # tracked files to check that no test file is named after a release;
            # a store sandbox is not a checkout, so with git present that test
            # skips on "not a git checkout" and without it skips on "git is not
            # on PATH". Identical outcome, one less dependency. It is not going
            # unchecked: CI runs this suite in a real checkout on every push,
            # and that is where the question "what does this repo track" can be
            # answered at all.
            nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.pytest ])) ];
          } ''
          # The layout matters, not just the file set: the suite computes its
          # repo root as the test file's parent.parent.parent, so the test has
          # to sit two directories below the files it reads.
          mkdir -p repo/harness/tests repo/app
          cp ${./harness/tests/test_release_numbers.py} repo/harness/tests/test_release_numbers.py
          cp ${./CHANGELOG.md}    repo/CHANGELOG.md
          cp ${./README.md}       repo/README.md
          cp ${./pyproject.toml}  repo/pyproject.toml
          cp ${./app/main.py}     repo/app/main.py
          cp ${./flake.nix}       repo/flake.nix
          chmod -R u+w repo
          cd repo/harness
          # -c /dev/null: pyproject.toml is copied in above, which makes it this
          # run's ini file and hands the sandbox every `[tool.pytest.ini_options]`
          # the project ever grows. Today that is three pytest-asyncio settings
          # which merely warn, because the plugin is deliberately not installed
          # here; tomorrow it is `--strict-config`, an `addopts` naming a plugin
          # nixpkgs is not supplying to this check, or a coverage flag — and then
          # this build dies in collection rather than making the one assertion it
          # exists to make, for a reason that has nothing to do with release
          # numbers. An empty ini severs that entirely. The copy itself has to
          # stay: the suite READS pyproject.toml to find the version it compares,
          # so the file is present as data and ignored as configuration.
          #
          # The `tests` argument is load-bearing rather than tidy: with an empty
          # ini there is no `testpaths` to fall back on at all, so nothing else
          # tells pytest which directory to collect.
          #
          # -rs prints the reason for every skip, and the two guards below turn
          # those reasons into assertions. Printing alone would not: this check
          # exists because a whole suite of assertions was inert and said so only
          # in ERROR lines nobody read, and an unread ERROR line traded for an
          # unread build log is the same failure wearing the same green badge.
          # pytest exits 0 whether one test skipped or every last one did, so the
          # exit status by itself cannot tell "the release numbers agree" from
          # "nothing looked".
          set -o pipefail
          if ! pytest -q -rs -c /dev/null -p no:cacheprovider tests 2>&1 | tee pytest.log; then
            echo "release-metadata: the suite failed — the assertions are printed above."
            exit 1
          fi

          # Guard 1: something actually ran and passed. A run that collected
          # nothing, or that skipped its way from end to end, prints no "N passed"
          # and is precisely the inert-but-green outcome this check was added to
          # make impossible. Deliberately a floor and not a count, so adding a
          # test to the suite does not also mean editing this line.
          if ! grep -qE '[1-9][0-9]* passed' pytest.log; then
            echo "release-metadata: not one test in this suite passed."
            echo "pytest still exited 0, which means every assertion either skipped or"
            echo "was never collected — a renamed path, a dropped precondition or a"
            echo "collection-level skip will all do this, and none of them are a green"
            echo "build. The pytest summary above says which."
            exit 1
          fi

          # Guard 2: every skip is one of the two reasons this sandbox is allowed
          # to produce. The git-tracked-filenames test skips because git is
          # deliberately off PATH — see the comment on nativeBuildInputs — and the
          # unstamped-entry test skips on a stamped main, where there is no
          # `## vNEXT` heading left for it to look at. On a branch that is writing
          # a release the second one runs instead, so how MANY tests skip here is
          # a property of the branch and is deliberately not asserted. WHICH
          # reasons are permitted is, because a third reason means a precondition
          # stopped holding and nobody was told.
          unexpected=$(grep '^SKIPPED' pytest.log \
            | grep -v -e 'git is not on PATH' -e 'nothing unstamped' || true)
          if [[ -n "$unexpected" ]]; then
            echo "release-metadata: a test skipped for a reason this sandbox does not permit:"
            echo "$unexpected"
            echo "Either the sandbox stopped supplying something the suite needs, in which"
            echo "case fix the file set copied in above, or the new skip is legitimate on a"
            echo "clean checkout too — in which case add its reason to the grep here and"
            echo "write down why it is allowed to be inert."
            exit 1
          fi
          touch $out
        '';

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
