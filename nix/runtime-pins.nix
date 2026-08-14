# nix/runtime-pins.nix — managed runtime tools, built from runtime-pins.json
#
# runtime-pins.json is the ONE table of managed tool versions and digests.
# It already feeds the Python provisioner (source installs, `hermes
# update`, desktop payload staging). This file makes Nix a fourth consumer
# of that table rather than a second table: every version, URL and digest
# here is read from the JSON, so a pin bump stays one edit.
#
# Shape:
#
#   * one derivation per pinned tool, each holding that tool's own tree
#     exactly as upstream ships it;
#   * `extends` in the table becomes a real Nix dependency — npm's
#     derivation takes node's, so Nix orders the builds and neither this
#     file nor a reader restates "npm needs node";
#   * `bundle` symlinks those derivations into the directory layout
#     `hermes_cli/runtime_registry.py` describes, and writes `runtimes.json`
#     with the registry's own code.
#
# Nothing here wraps a program or exports an environment variable. The
# bundle is a runtime dir, and `hermes_cli/runtime_env.py` already knows
# how to turn one of those into PATH, GIT_EXEC_PATH, npm_config_cache and
# the rest — on every install kind. A Nix-specific version of any of that
# would be a second implementation of tested behaviour.
{
  lib,
  stdenv,
  fetchurl,
  autoPatchelfHook,
  unzip,
  python3,
  runCommand,
  curl,
  expat,
  fontconfig,
  zlib,
}:
let
  repoRoot = ../.;
  pins = (builtins.fromJSON (builtins.readFile ../runtime-pins.json)).tools;

  # Pin-table target keys use Node/Python spellings so one string works on
  # both sides of the JS/Python boundary; Nix systems spell it the other
  # way round. This is the only place the two vocabularies meet.
  targetBySystem = {
    "x86_64-linux" = "linux-x64";
    "aarch64-linux" = "linux-arm64";
    "x86_64-darwin" = "darwin-x64";
    "aarch64-darwin" = "darwin-arm64";
  };

  target =
    targetBySystem.${stdenv.hostPlatform.system}
      or (throw "runtime-pins: no pin target for ${stdenv.hostPlatform.system}");

  # A tool either pins one target-independent artifact ('any', a registry
  # tarball whose bytes do not vary) or one per target. Same resolution
  # the Python registry does — see `pinned_file`.
  artifactOf =
    name: entry:
    entry.files.any
      or entry.files.${target}
      or (throw "runtime-pins: ${name} has no pinned download for ${target}");

  # fetchurl's `sha256` takes the bare lowercase hex the table already
  # stores — the same string the Python provisioner verifies, so there is
  # no second encoding to keep in sync. Nix enforces it as a fixed-output
  # derivation: a tampered pin fails the build, as it fails provisioning.
  fetchPinned = name: entry: fetchurl { inherit (artifactOf name entry) url sha256; };

  extendsOf = entry: entry.extends or [ ];

  # Prebuilt upstream binaries link against a normal FHS glibc, which does
  # not exist here. autoPatchelfHook rewrites the interpreter and RPATH
  # onto the nixpkgs runtime; macOS binaries are already relocatable.
  #
  # One library set covers every tool: these are the shared objects the
  # pinned artifacts actually ask for (zlib broadly; curl/expat for
  # dugite's http helpers; fontconfig for the Skia lib dugite ships).
  patchelfInputs = lib.optionals stdenv.hostPlatform.isLinux [
    stdenv.cc.cc.lib
    zlib
    curl
    expat
    fontconfig
  ];

  mkToolBase =
    name: entry: extra:
    stdenv.mkDerivation (
      {
        pname = "hermes-runtime-${name}";
        version = entry.version;
        src = fetchPinned name entry;

        nativeBuildInputs =
          [ unzip ] ++ lib.optionals stdenv.hostPlatform.isLinux [ autoPatchelfHook ];
        buildInputs = patchelfInputs;

        dontUnpack = true;
        dontBuild = true;
        dontConfigure = true;

        passthru = {
          pinnedVersion = entry.version;
          pinnedUrl = (artifactOf name entry).url;
          extends = map (dep: tools.${dep}) (extendsOf entry);
        };

        meta = {
          description = "Hermes managed runtime ${name} ${entry.version} (pinned in runtime-pins.json)";
          platforms = lib.platforms.unix;
        };
      }
      // extra
    );

  # The common case: unpack the artifact and keep upstream's own layout.
  #
  # Un-nesting a lone versioned wrapper directory is decided by what the
  # archive CONTAINS, not by a per-tool list — the same rule the Python
  # provisioner uses, and for the same reason (uv nests on POSIX and not
  # on Windows, so a hardcoded list gets it wrong).
  mkUnpackedTool =
    name: entry:
    mkToolBase name entry {
      installPhase = ''
        runHook preInstall
        mkdir -p unpacked
        tar -xf "$src" -C unpacked 2>/dev/null || unzip -q "$src" -d unpacked

        inner=unpacked
        entries=("$inner"/*)
        if [ ''${#entries[@]} -eq 1 ] && [ -d "''${entries[0]}" ]; then
          inner="''${entries[0]}"
        fi

        mkdir -p "$out"
        cp -R "$inner"/. "$out/"
        runHook postInstall
      '';
    };

  # npm is the one tool that cannot simply be unpacked. Its own bin/npm
  # resolves npm-cli.js from dirname(process.execPath), so unpacked onto a
  # PATH it finds the npm BUNDLED inside node — the copy this pin exists
  # to supersede — and dies with MODULE_NOT_FOUND when that copy is gone.
  # Letting npm install itself produces the launchers the platform
  # actually needs instead of hand-written shims, and is the same install
  # the Python provisioner performs (`_stage_npm`) from the same
  # digest-verified tarball, offline.
  #
  # `extends` is what makes this work without a special case: node is a
  # real build input, so Nix has already built and patched it.
  mkNpmTool =
    name: entry:
    let
      node = tools.node;
    in
    mkToolBase name entry {
      installPhase = ''
        runHook preInstall
        mkdir -p "$out"
        # npm insists on a writable HOME and cache; the sandbox's HOME is
        # deliberately unwritable. Neither belongs in $out — a real
        # install keeps its cache in the runtime dir, which
        # managed_tool_env points npm_config_cache at.
        HOME="$TMPDIR" npm_config_cache="$TMPDIR/npm-cache" \
          ${node}/bin/node ${node}/lib/node_modules/npm/bin/npm-cli.js \
          install --global --prefix "$out" --offline --no-audit --no-fund \
          "$src"

        # npm writes its launchers with `#!/usr/bin/env node`, which does
        # not exist inside a Nix build sandbox — any derivation using this
        # npm as a BUILD tool dies with "bad interpreter". Point them at
        # the node this tool extends, which is also more correct: the
        # pinned npm should run on the pinned node, not on whatever node
        # a PATH lookup finds first.
        #
        # patchShebangs is not enough on its own here: it resolves `env
        # node` against the build PATH, which is not necessarily the node
        # in the pin table.
        for launcher in "$out"/bin/*; do
          [ -f "$launcher" ] || continue
          case "$(head -c 2 "$launcher")" in
            '#!') substituteInPlace "$launcher" \
                    --replace-quiet '#!/usr/bin/env node' '#!${node}/bin/node' ;;
          esac
        done
        runHook postInstall
      '';
    };

  # An `extends` edge means "staged by what it extends", which today is
  # npm-shaped: run the extended tool's installer. A second extender with
  # different mechanics would add a branch here; one edge, one meaning
  # until then.
  mkTool =
    name: entry: if extendsOf entry == [ ] then mkUnpackedTool name entry else mkNpmTool name entry;

  tools = lib.mapAttrs mkTool pins;

  # The registry code, as a store path. Only the leaf modules the
  # fact-writing and PATH assembly import — all pure-stdlib, so a bare
  # python3 loads them with no venv, and the bundle does not depend on
  # the whole repo (which would rebuild it on any source change).
  registrySrc = runCommand "hermes-runtime-registry-src" { } ''
    mkdir -p "$out/hermes_cli"
    touch "$out/hermes_cli/__init__.py"
    cp ${../hermes_cli/runtime_registry.py} "$out/hermes_cli/runtime_registry.py"
    cp ${../hermes_cli/runtime_provisioner.py} "$out/hermes_cli/runtime_provisioner.py"
    cp ${../hermes_cli/runtime_env.py} "$out/hermes_cli/runtime_env.py"
    cp ${../hermes_cli/runtime_tree.py} "$out/hermes_cli/runtime_tree.py"
    cp ${../hermes_constants.py} "$out/hermes_constants.py"
    cp ${../runtime-pins.json} "$out/runtime-pins.json"
  '';

  # The bundle IS a runtime dir: `<dir>/<tool>/...` per the registry's
  # layout, plus the `runtimes.json` facts manifest. Symlinks, so the
  # tools stay separately built and separately cached.
  #
  # Facts are written by runtime_registry.py itself, and the PATH dirs
  # are then read back out with runtime_env.managed_path_dirs — the same
  # call every Hermes subprocess makes. Nix consumers take that list
  # instead of guessing, which matters because the layout is per-tool:
  # node/git/gh/npm expose `<tool>/bin`, uv and ripgrep put the binary at
  # `<tool>/` directly, and lib.makeBinPath (which only ever appends
  # /bin) silently drops the second kind.
  bundle =
    runCommand "hermes-runtime-dir"
      {
        passthru = tools // {
          inherit target;
          pinnedVersions = lib.mapAttrs (_: entry: entry.version) pins;
        };
      }
      ''
    mkdir -p "$out"
    ${lib.concatStringsSep "\n" (
      lib.mapAttrsToList (name: drv: ''ln -s ${drv} "$out/${name}"'') tools
    )}

    export PYTHONPATH=${registrySrc}
    ${python3}/bin/python3 - "$out" ${registrySrc} "${target}" <<'PY'
    import sys
    from pathlib import Path

    from hermes_cli.runtime_registry import (
        RuntimeFact, install_order, load_pins, path_order, save_facts,
    )
    from hermes_cli.runtime_provisioner import _binary_rel, _path_dirs

    runtime_dir, registry_src, target = (
        Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3],
    )
    pins = load_pins(registry_src)

    facts = {}
    for tool in install_order(pins):
        rel = _binary_rel(tool, target)
        if not (runtime_dir / rel).exists():
            raise SystemExit(f"runtime-pins: {tool} is missing {rel}")
        facts[tool] = RuntimeFact(
            version=pins[tool]["version"],
            path=rel,
            path_dirs=_path_dirs(tool, target),
        )

    save_facts(facts, runtime_dir, path_order=path_order(pins))

    # Emit the assembled PATH dirs and tool env for Nix consumers,
    # straight out of the assembler every Hermes subprocess uses. Written
    # as files rather than recomputed in Nix because both are genuinely
    # per-tool and already encoded here: uv and ripgrep keep their binary
    # at the tree root while the rest use bin/ (`_dirs_for`), and dugite's
    # git needs GIT_EXEC_PATH or it cannot find its own remote helpers
    # (`managed_tool_env`).
    from hermes_cli.runtime_env import managed_path_dirs, managed_tool_env  # noqa: E402

    dirs = managed_path_dirs(runtime_dir)
    assert len(dirs) >= len(facts), (
        f"assembled {len(dirs)} PATH dirs for {len(facts)} tools — "
        "a provisioned tool contributed nothing"
    )
    (runtime_dir / "path-dirs").write_text(
        "".join(f"{d}\n" for d in dirs), encoding="utf-8"
    )

    # Shell-sourceable, one `export K=V` per line. shlex.quote because a
    # store path is well-behaved but this is going through a shell.
    import shlex  # noqa: E402

    (runtime_dir / "tool-env").write_text(
        "".join(
            f"export {key}={shlex.quote(value)}\n"
            for key, value in sorted(managed_tool_env(runtime_dir).items())
        ),
        encoding="utf-8",
    )
    PY
  '';
in
bundle
