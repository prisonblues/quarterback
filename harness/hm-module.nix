{ config, lib, pkgs, ... }:

let
  cfg = config.programs.quarterback-harness;
  share = "${cfg.package}/share/quarterback-harness";
in
{
  options.programs.quarterback-harness = {
    enable = lib.mkEnableOption "the quarterback agent harness (loops, worktree tooling, slash commands)";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
      description = "The harness package to install.";
    };

    commands = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [
        "panel" "panel-review-pr" "review-pr"
        "epic" "lander" "loops" "fix-and-land"
        "fix-issue" "fix-issue-here"
        "wt" "drop-worktree" "tree-shake"
      ];
      description = ''
        Which slash commands to link into ~/.claude/commands. Defaults to all of
        them. Narrow it if the host already provides a command of the same name —
        home-manager will collide rather than silently pick a winner, which is
        the behaviour you want.
      '';
    };

    installScripts = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Put create-worktree / remove-worktree / prune-worktrees on PATH by adding
        the package to home.packages. Turn this off to take the loops and commands
        without the worktree tooling — the two halves are independent.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = lib.mkIf cfg.installScripts [ cfg.package ];

    home.file = lib.mkMerge (
      # ~/.claude/loops is a store symlink, i.e. READ-ONLY. epic.py already
      # accounts for this by writing its run state to ~/.local/state/loops rather
      # than beside itself; anything added here must do the same.
      [{ ".claude/loops".source = "${share}/loops"; }]
      ++ map
        (name: { ".claude/commands/${name}.md".source = "${share}/commands/${name}.md"; })
        cfg.commands
    );
  };
}
