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
          # sandbox: it reads the release metadata at the repo root — the files
          # the check below enumerates, which this one does not contain. It
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
        # what put it in a sandbox holding only `harness/`, where all eight of
        # its assertions errored on missing files instead of being evaluated
        # (#163). So it gets a sandbox holding the files it actually reads.
        #
        # This is the check that would have caught the v2.34 collision two
        # branches created on 2026-08-17, and it has never once run here.
        #
        # The `cp`s below are enumerated one by one rather than the repo root
        # being copied wholesale: `./.` would drag a developer's `mcp/.venv` and
        # every `__pycache__` into the store. Enumeration is the thing that can
        # go stale, so it does not rely on someone remembering —
        # `test_the_flake_check_supplies_every_repo_root_file_this_suite_reads`
        # in the suite itself compares this list against the paths the suite
        # reads, and that is why flake.nix is copied in below alongside them.
        # `test_the_flake_check_supplies_every_conftest_that_would_load_beside_this_suite`
        # covers the other half of the coupling: a conftest added under
        # `harness/` is loaded by a developer's run and, unless it is copied in
        # here too, not by this one.
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
            #
            # `pkgs.python3` needs to be 3.11 or newer here: the suite reads
            # pyproject.toml's version with `tomllib`, which is stdlib only from
            # then on. nixpkgs' default is well past that and has no reason to go
            # back, so this is documented and asserted rather than pinned — a pin
            # would freeze this check on an interpreter the rest of the flake has
            # moved off. The assertion is in the script below.
            nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.pytest ])) ];
          } ''
          # The floor, named where it can be read. Without this the failure would
          # arrive as `ModuleNotFoundError: tomllib` from inside a release-number
          # check, which is a long way from "nixpkgs moved python3 backwards".
          if ! python3 -c 'import tomllib' 2>/dev/null; then
            echo "release-metadata-tests: this python3 has no tomllib, so it is" >&2
            echo "older than 3.11. The suite reads pyproject.toml's version with" >&2
            echo "it. Pin an interpreter that has it." >&2
            exit 1
          fi

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

          # pyproject.toml is copied in above because the suite reads the version
          # out of it — and that also makes it what pytest would take as its
          # rootdir config, so the whole of `[tool.pytest.ini_options]` would
          # apply here. Only `testpaths` does anything today, worked around by
          # naming the path; a future `addopts` (a coverage flag, `-n auto`, a
          # plugin flag whose plugin is not in the withPackages env above) would
          # break this check for a reason unrelated to anything it asserts. So
          # pytest is pointed at an empty ini instead: nothing from pyproject.toml
          # is inherited, and the only thing that changes is the rootdir, which
          # nothing here depends on — the suite locates the repo from its own
          # `__file__`, not from pytest's idea of where the project starts. The
          # path argument stays explicit for the same reason it always was.
          ini=$PWD/sandbox-pytest.ini
          log=$PWD/pytest.log
          cat > "$ini" <<'EOF'
          [pytest]
          EOF

          cd repo/harness
          # -rs: print the reason for every skip. This check exists because eight
          # assertions were inert and said so only in ERROR lines nobody read;
          # a silent skip is the same failure wearing a green badge. Printing
          # them is not enough on its own — nobody reads a green build's log
          # either — so the reasons are checked below.
          if ! pytest -q -rs -p no:cacheprovider -c "$ini" tests > "$log" 2>&1; then
            cat "$log"
            exit 1
          fi
          cat "$log"

          # Which skips are allowed, by REASON rather than by count. The count is
          # not a constant and an exact one would be wrong: the unstamped-entry
          # test skips on a released tree and RUNS on a branch carrying a
          # `## vNEXT` heading, so a hard number would go red on precisely the
          # branches this suite exists for. The reasons are the constant. Any
          # other test that starts skipping is the failure this whole check is
          # about, and it fails here rather than reading green.
          if grep '^SKIPPED' "$log" |
               grep -qv -e 'git is not on PATH' -e 'nothing unstamped'; then
            echo "release-metadata-tests: a test skipped for a reason this check" >&2
            echo "does not expect. A skip here is an assertion that did not run." >&2
            echo "Fix it, or add the reason above once it is understood:" >&2
            grep '^SKIPPED' "$log" >&2
            exit 1
          fi
          # And the one skip that is not conditional. git is absent from
          # nativeBuildInputs on purpose, so the tracked-filenames test must skip
          # every time. If it stops, git arrived from somewhere and that test is
          # now asking a store path what a checkout tracks.
          if ! grep -q 'SKIPPED.*git is not on PATH' "$log"; then
            echo "release-metadata-tests: the git-tracked-filenames test did not" >&2
            echo "skip, so git is on PATH in this sandbox. It is left out on" >&2
            echo "purpose — a store path is not a checkout and cannot answer." >&2
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
