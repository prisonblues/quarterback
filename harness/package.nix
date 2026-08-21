{ lib, stdenvNoCC, python3, bash, makeWrapper }:

# The harness is plain bash and stdlib Python — no build step, no third-party
# imports. So this derivation copies rather than compiles, and its only real job
# is deciding what lands on PATH and what merely lands in the store.
#
# `bin/qb-board` is bash for that reason and no other: the terminal board client
# it launches is Python with real dependencies (httpx, and Textual for the
# full-screen half), and lives in mcp/ beside the HTTP client it reuses. Building
# it here would mean either a second copy of that client or a dependency closure
# this derivation is deliberately without, so the launcher resolves an
# interpreter at runtime and says so plainly when it cannot find one.
#
# Deliberately NOT wrapped with a PATH of git/jq/gh/docker/psql. Those are the
# HOST's tools by definition: create-worktree clones the host's database, drives
# the host's docker, and edits the host's nginx. Pinning a docker or psql from
# nixpkgs into the wrapper would point the scripts at a different machine's idea
# of the world than the one they are provisioning. Requirements are documented in
# harness/README.md and checked at runtime by the scripts themselves.
let
  # The dashboard's interpreter, and the ONLY third-party imports in the harness.
  # Carried by the package rather than hunted for on the host: `qb` is the first
  # thing typed after a rebuild, and "no Python here can import rich" is a poor
  # welcome. The board client it uses is stdlib on purpose, so this list is two
  # entries rather than a requirements file.
  dashPython = python3.withPackages (ps: [ ps.rich ps.textual ]);
in
stdenvNoCC.mkDerivation {
  pname = "quarterback-harness";
  version = "0.1.0";

  src = lib.cleanSource ./.;

  # `loops`, `commands` and `claude` go to share/ rather than bin/ because none of them
  # is something you invoke by name: the loops are driven by the slash commands
  # (which reference them by path), the commands are read by Claude Code out
  # of ~/.claude, and `claude/` is data — the settings fragment the wiring merges
  # and the workflow doc the module links into ~/.claude. Only `bin/` holds genuine
  # CLI entry points — the worktree
  # scripts, which must land in one directory together because each finds
  # `worktree-holder` as a sibling of $0 when it is not otherwise on PATH;
  # `qb-stage`, which the slash commands call by name and so needs PATH;
  # `qb-seat`, which a multiplexer layout names as the command for each pane —
  # a layout is data on another machine, so it can only refer to it by name;
  # `qb-board`, which is a thing a human types on a headless box, which is
  # the whole point of it existing; `qb-reconcile`, which a systemd timer names
  # as its ExecStart, for the same reason as the layout; and the board client's
  # own four (#230) — `qb-hook`, which ~/.claude/settings.json names by absolute
  # path, `qb-mcp`, which ~/.claude.json does, `qb-claude-setup`, which writes
  # both of those from the home-manager activation, and `qb` for a human.
  #
  # Two files land in bin/ that are not entry points at all, for the same reason
  # as each other. `qbdata.py` is the library the dashboards and `qb-reconcile`
  # import as a SIBLING OF $0; `qb-env` is the library the board client's four
  # SOURCE, each finding it as a sibling of $0 too. Neither relationship survives
  # anywhere else — home-manager links each file in as its own flat store path,
  # so "beside the script" is the only one there is. Same rule keeps
  # `worktree-holder` here.
  installPhase = ''
    runHook preInstall

    mkdir -p $out/bin $out/share/quarterback-harness
    install -m 0755 bin/* $out/bin/
    # templates/ ships alongside them: create-worktree does nothing for a repo
    # until that repo has a .worktree.json, so an installed harness that omits
    # the starting points makes the user go back to the source tree for them.
    cp -r loops commands templates claude $out/share/quarterback-harness/
    install -m 0644 worktree.example.json README.md $out/share/quarterback-harness/

    runHook postInstall
  '';

  # qb-dash-tui execs qb-dash, so wrapping the one covers both. --set-default,
  # not --set: a developer running against a venv of their own still wins.
  postInstall = ''
    wrapProgram $out/bin/qb-dash --set-default QB_DASH_PYTHON ${dashPython}/bin/python
  '';

  # Rewrites `#!/usr/bin/env bash|python3` to store paths, so an installed
  # harness does not depend on what happens to be on the user's PATH.
  postFixup = ''
    patchShebangs $out/bin $out/share/quarterback-harness/loops
  '';

  nativeBuildInputs = [ makeWrapper ];
  buildInputs = [ bash python3 ];

  meta = with lib; {
    description = "Agent coding loops and worktree tooling coordinated by the quarterback board";
    longDescription = ''
      The workflow half of quarterback: the reviewer panel, the epic and lander
      loops, and the worktree-per-issue tooling, plus the Claude Code slash
      commands that drive them. Usable without the board — the panel's board
      recording is best-effort and no-ops when no board is configured.
    '';
    platforms = platforms.unix;
    mainProgram = "create-worktree";
  };
}
