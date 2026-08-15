{ lib, stdenvNoCC, python3, bash }:

# The harness is plain bash and stdlib Python — no build step, no third-party
# imports. So this derivation copies rather than compiles, and its only real job
# is deciding what lands on PATH and what merely lands in the store.
#
# Deliberately NOT wrapped with a PATH of git/jq/gh/docker/psql. Those are the
# HOST's tools by definition: create-worktree clones the host's database, drives
# the host's docker, and edits the host's nginx. Pinning a docker or psql from
# nixpkgs into the wrapper would point the scripts at a different machine's idea
# of the world than the one they are provisioning. Requirements are documented in
# harness/README.md and checked at runtime by the scripts themselves.
stdenvNoCC.mkDerivation {
  pname = "quarterback-harness";
  version = "0.1.0";

  src = lib.cleanSource ./.;

  # `loops` and `commands` go to share/ rather than bin/ because neither is
  # something you invoke by name: the loops are driven by the slash commands
  # (which reference them by path), and the commands are read by Claude Code out
  # of ~/.claude. Only the worktree scripts are genuine CLI entry points — and
  # they must land in one directory together, because each finds `worktree-holder`
  # as a sibling of $0 when it is not otherwise on PATH.
  installPhase = ''
    runHook preInstall

    mkdir -p $out/bin $out/share/quarterback-harness
    install -m 0755 bin/* $out/bin/
    # templates/ ships alongside them: create-worktree does nothing for a repo
    # until that repo has a .worktree.json, so an installed harness that omits
    # the starting points makes the user go back to the source tree for them.
    cp -r loops commands templates $out/share/quarterback-harness/
    install -m 0644 worktree.example.json README.md $out/share/quarterback-harness/

    runHook postInstall
  '';

  # Rewrites `#!/usr/bin/env bash|python3` to store paths, so an installed
  # harness does not depend on what happens to be on the user's PATH.
  postFixup = ''
    patchShebangs $out/bin $out/share/quarterback-harness/loops
  '';

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
