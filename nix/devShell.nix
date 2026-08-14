# nix/devShell.nix — Dev shell that delegates setup to each package
#
# Each npm workspace package exposes passthru.packageJsonPath (e.g.
# "ui-tui/package.json").  This file collects them all and passes the
# list to mkNpmDevShellHook, which stamps all package.jsons at once,
# then runs a single `npm i --package-lock-only` if any changed and
# `npm ci` if the lockfile changed.
{ ... }:
{
  perSystem =
    { pkgs, self', ... }:
    let
      packages = builtins.attrValues self'.packages;
      hermesNpmLib = self'.packages.default.passthru.hermesNpmLib;

      # The managed runtime tools, from runtime-pins.json — the same
      # table a source install, `hermes update` and the nix package all
      # use. A developer's node/npm/git/gh/ripgrep are therefore the
      # versions users get, not whatever nixpkgs carries this week.
      runtimeDir = pkgs.callPackage ../nix/runtime-pins.nix { };

      # Collect all packageJsonPath values from npm workspace packages.
      npmPackageJsonPaths = builtins.filter (p: p != null) (
        map (p: p.passthru.packageJsonPath or null) packages
      );
    in
    {
      devShells.default = pkgs.mkShell {
        packages =
          with pkgs;
          [
            (pkgs.runCommand "hermes" { } ''
              mkdir -p $out/bin
              install -Dm755 ${../hermes} $out/bin/hermes
            '')
            # hermes egress setup (iron-proxy) shells out to openssl for CA
            # generation; tests/test_iron_proxy_cli.py exercises that wizard.
            # Not a managed tool, so the pin table does not supply it.
            openssl
            # Validate GitHub Actions workflows before pushing CI changes.
            actionlint
          ]
          # The sandbox (bubblewrap) and the Wayland E2E stack only exist on
          # Linux. The macOS devshell carries the build toolchain alone.
          ++ pkgs.lib.optionals pkgs.stdenv.isLinux [
            self'.packages.sandbox
            # Headless Wayland compositor for E2E tests (test:e2e:visual).
            # cage renders a single client with no window management, so
            # the Electron window opens at a fixed size without tiling.
            # libglvnd provides libEGL.so.1 that cage needs on NixOS.
            cage
            libglvnd
            # Graphical terminal + Wayland screenshot client for CLI/TUI UI
            # evidence. `cage -- ghostty ...` keeps captures off the user's
            # live compositor; grim runs inside that isolated client session.
            ghostty
            grim
          ]
          ++ self'.packages.default.passthru.devDeps
          ++ self'.packages.desktop.passthru.devDeps;
        shellHook = ''
          ${self'.packages.default.passthru.devShellHook}
          ${self'.packages.desktop.passthru.devShellHook}
          ${hermesNpmLib.mkNpmDevShellHook npmPackageJsonPaths}

          # Point the devshell's Hermes at the SAME runtime dir the nix
          # package ships, so `hermes doctor` here reports what a nix
          # install reports. Without this it resolves the runtime dir
          # relative to the checkout and finds nothing provisioned —
          # true, but not the thing a developer is testing.
          export HERMES_RUNTIME_DIR="${runtimeDir}"

          # The pinned tools go on PATH FIRST, in the dirs the bundle's
          # own assembler recorded (runtime_env.managed_path_dirs), with
          # the matching tool env from managed_tool_env. A developer gets
          # the node/npm/uv/git/gh/ripgrep the pin table names — the same
          # ones users get — rather than whatever nixpkgs carries.
          #
          # Both files are read here rather than rebuilt in Nix: the
          # layouts are per-tool (uv and ripgrep keep their binary at the
          # tree root, the rest use bin/) and the git contract is
          # dugite's, and all of that already lives in the Python
          # assembler. Recomputing either in `mkShell` would also mean
          # import-from-derivation at eval time.
          #
          # PATH and the tool env must travel TOGETHER: a relocated
          # dugite-native git resolves its helpers against a build-time
          # prefix, so git on PATH without GIT_EXEC_PATH dies on
          # "'remote-http' is not a git command". Anything that scrubs
          # the environment has to keep both (see scripts/run_tests.sh).
          export PATH="$(tr '\n' ':' < "${runtimeDir}/path-dirs")$PATH"
          . "${runtimeDir}/tool-env"

          # Force Node to use Nix's playwright-test binary instead of node_modules/.bin
          export PATH="${pkgs.playwright-test}/bin:$PATH"

          # for the devshell to pick up the src
          export HERMES_PYTHON_SRC_ROOT=$(git rev-parse --show-toplevel)

          # Let `uv run --active --no-sync` reuse Nix's provisioned Python
          # environment instead of creating an empty project .venv.
          export VIRTUAL_ENV="$(dirname "$(dirname "$(readlink -f "$(command -v python)")")")"

          echo "Hermes Agent dev shell in $HERMES_PYTHON_SRC_ROOT"
          echo "Ready. Run 'hermes' or 'sandbox hermes' to start."
        '';
      };
    };
}
