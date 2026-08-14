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

      # `nix flake check` runs the loops' own test suite. Worth having as a flake
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
