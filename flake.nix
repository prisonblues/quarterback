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
          # sandbox: it reads CHANGELOG.md, README.md, pyproject.toml, app/main.py
          # and flake.nix at the repo root, none of which this check contains. It
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
          install -Dm644 ${./CHANGELOG.md}   repo/CHANGELOG.md
          install -Dm644 ${./README.md}      repo/README.md
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
