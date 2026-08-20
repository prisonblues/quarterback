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
        "epic" "lander" "loops" "fix-and-land" "fix-and-review"
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

    seats.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Install the multiplexer `qb-seats` needs. The scripts themselves are in
        the package already — this only adds tmux, which is the one runtime
        dependency the harness cannot assume is present.

        Off by default, and that is deliberate rather than timid. A consumer
        typically enables this whole module once, for every host they own; a
        seat screen is something they want on ONE of them. Defaulting to on would
        push a package onto machines that will never run a seat — including work
        machines somebody else owns the disk of — as a side effect of enabling
        the loops and the worktree tooling, which is not a trade a default gets
        to make on their behalf.
      '';
    };

    installScripts = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Put create-worktree / remove-worktree / prune-worktrees / worktree-holder
        on PATH by adding the package to home.packages. Turn this off to take the
        loops and commands without the worktree tooling — the two halves are
        independent.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages =
      lib.optionals cfg.installScripts [ cfg.package ]
      # NOT wrapped into the package's PATH, unlike a build dependency would be.
      # The user attaches to tmux by hand, reattaches over ssh, and has their own
      # config for it — so it has to be the tmux they can see and configure, not
      # one hidden inside a wrapper where `tmux attach` from their own shell
      # would find a different binary.
      ++ lib.optionals cfg.seats.enable [ pkgs.tmux ];

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
