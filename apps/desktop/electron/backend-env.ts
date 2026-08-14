import fs from 'node:fs'
import path from 'node:path'

// Match the POSIX fallback surface used by the Python terminal environment.
// macOS apps launched from Finder/Dock often inherit only /usr/bin:/bin:/usr/sbin:/sbin,
// which misses Apple Silicon Homebrew and user-installed CLI tools such as codex.
const POSIX_SANE_PATH_ENTRIES = Object.freeze([
  '/opt/homebrew/bin',
  '/opt/homebrew/sbin',
  '/usr/local/sbin',
  '/usr/local/bin',
  '/usr/sbin',
  '/usr/bin',
  '/sbin',
  '/bin'
])

function delimiterForPlatform(platform = process.platform) {
  return platform === 'win32' ? ';' : ':'
}

function pathModuleForPlatform(platform = process.platform) {
  return platform === 'win32' ? path.win32 : path.posix
}

function pathEnvKey(env = process.env, platform = process.platform) {
  if (platform !== 'win32') {
    return 'PATH'
  }

  return Object.keys(env || {}).find(key => key.toUpperCase() === 'PATH') || 'PATH'
}

function currentPathValue(env = process.env, platform = process.platform) {
  const key = pathEnvKey(env, platform)

  return env?.[key] || ''
}

function appendUniquePathEntries(entries, { delimiter = path.delimiter } = {}) {
  const seen = new Set()
  const ordered = []

  for (const entry of entries) {
    if (!entry) {
      continue
    }

    const parts = Array.isArray(entry) ? entry : String(entry).split(delimiter)

    for (const part of parts) {
      if (!part || seen.has(part)) {
        continue
      }

      seen.add(part)
      ordered.push(part)
    }
  }

  return ordered.join(delimiter)
}

/**
 * Paths that are the macOS xcode-select STUB rather than a real git.
 *
 * `/usr/bin/git` on a Mac without the Command Line Tools is a shim whose
 * only behaviour is to pop a modal "install developer tools?" dialog.
 * `/usr/bin/xcrun` fronts the same mechanism. A background process must
 * never invoke either: the user gets a dialog they did not ask for, from
 * an app that looks idle.
 */
const MACOS_XCODE_SHIM_PATHS = Object.freeze(['/usr/bin/git', '/usr/bin/xcrun'])

export function isMacosXcodeShim(binaryPath: string | null | undefined): boolean {
  if (!binaryPath) {
    return false
  }

  return MACOS_XCODE_SHIM_PATHS.includes(binaryPath)
}

/**
 * The first real git on PATH, skipping the xcode-select shim.
 *
 * Returns null when the only git available is the shim — callers must
 * treat that as "no git", because using it is worse than not having one.
 */
export function gitOnPathSkippingShim(
  pathValue: string,
  {
    delimiter = path.delimiter,
    pathModule = path,
    exists
  }: { delimiter?: string; pathModule?: typeof path; exists: (candidate: string) => boolean }
): string | null {
  for (const entry of String(pathValue || '').split(delimiter)) {
    if (!entry) {
      continue
    }

    const candidate = pathModule.join(entry, 'git')

    if (isMacosXcodeShim(candidate)) {
      continue
    }

    if (exists(candidate)) {
      return candidate
    }
  }

  return null
}

/** One managed tool as recorded in `<runtime dir>/runtimes.json`. */
export type RuntimeFact = {
  version: string
  /** Binary path, RELATIVE to the runtime dir. */
  path: string
  installedAt?: string
  /** Optional multi-dir PATH surface (PortableGit: cmd, bin, usr/bin). */
  pathDirs?: string[]
}

export type RuntimeFacts = {
  schemaVersion: number
  /** PATH assembly order, derived from the pin table's `extends` edges. */
  pathOrder?: string[]
  tools: Record<string, RuntimeFact>
}

/** The facts file the provisioner writes; the ONLY writer. */
export const RUNTIME_FACTS_FILENAME = 'runtimes.json'
export const RUNTIME_FACTS_SCHEMA_VERSION = 1

/**
 * Read the runtime registry's facts. Missing/foreign-schema/malformed all
 * mean "nothing provisioned" — the desktop then falls back to system tools,
 * which is a degrade, not a break.
 */
export function readRuntimeFacts(
  runtimeDir: string,
  { fsImpl = fs, pathModule = path }: { fsImpl?: typeof fs; pathModule?: typeof path } = {}
): Record<string, RuntimeFact> {
  return readRuntimeFactsFile(runtimeDir, { fsImpl, pathModule })?.tools || {}
}

/**
 * The whole parsed facts file, or null when there is nothing usable.
 * Callers that need the recorded PATH order read this; `readRuntimeFacts`
 * stays the narrow tools-only accessor it always was.
 */
function readRuntimeFactsFile(
  runtimeDir: string,
  { fsImpl = fs, pathModule = path }: { fsImpl?: typeof fs; pathModule?: typeof path } = {}
): RuntimeFacts | null {
  if (!runtimeDir) {
    return null
  }

  try {
    const raw = fsImpl.readFileSync(pathModule.join(runtimeDir, RUNTIME_FACTS_FILENAME), 'utf8')
    const parsed = JSON.parse(raw) as RuntimeFacts

    if (parsed?.schemaVersion !== RUNTIME_FACTS_SCHEMA_VERSION) {
      return null
    }

    return parsed
  } catch {
    return null
  }
}

/**
 * Managed runtime bin dirs, in assembly order.
 *
 * The registry's facts file decides WHICH tools exist, WHERE, and in WHAT
 * ORDER — this is a reader of the same data installation/env.py
 * serves to the Python side, not a second copy of the layout rules. That
 * mattered twice: an earlier version hard-coded `$HERMES_HOME/node{,/bin}`
 * and had to be kept in sync with `iter_hermes_node_dirs()` by comment,
 * and the order was a literal array here that a test could only police by
 * reading this file's source text. Both are data now; the provisioner
 * derives the order from the pin table's `extends` edges and records it.
 *
 * `main.ts` imports this rather than keeping its own copy.
 */
export function managedRuntimePathEntries(
  runtimeDir: string,
  {
    fsImpl = fs,
    pathModule = path
  }: { fsImpl?: typeof fs; pathModule?: typeof path } = {}
): string[] {
  const parsed = readRuntimeFactsFile(runtimeDir, { fsImpl, pathModule })
  const facts = parsed?.tools || {}
  // A hand-edited facts file may predate pathOrder; its own key order is
  // the only remaining signal, and matches what Python falls back to.
  const order = parsed?.pathOrder || Object.keys(facts)
  const dirs: string[] = []

  for (const tool of order) {
    const fact = facts[tool]

    if (!fact?.path) {
      continue
    }

    const binary = pathModule.join(runtimeDir, fact.path)

    // A recorded-but-vanished binary reads as unprovisioned: never emit a
    // PATH entry for a tool that is not actually there.
    try {
      if (!fsImpl.statSync(binary).isFile()) {
        continue
      }
    } catch {
      continue
    }

    const entries = fact.pathDirs
      ? fact.pathDirs.map(dir => pathModule.join(runtimeDir, dir))
      : [pathModule.dirname(binary)]

    for (const entry of entries) {
      if (!dirs.includes(entry)) {
        dirs.push(entry)
      }
    }
  }

  return dirs
}

function buildDesktopBackendPath({
  runtimeDir,
  venvRoot,
  currentPath = '',
  platform = process.platform,
  pathModule = pathModuleForPlatform(platform),
  fsImpl = fs
}: any = {}) {
  const delimiter = delimiterForPlatform(platform)
  const managedDirs = runtimeDir ? managedRuntimePathEntries(runtimeDir, { fsImpl, pathModule }) : []
  const venvBin = venvRoot ? pathModule.join(venvRoot, platform === 'win32' ? 'Scripts' : 'bin') : null
  const saneEntries = platform === 'win32' ? [] : POSIX_SANE_PATH_ENTRIES

  return appendUniquePathEntries([managedDirs, venvBin, currentPath, saneEntries], { delimiter })
}

function normalizeHermesHomeRoot(hermesHome, { pathModule = pathModuleForPlatform(process.platform) }: any = {}) {
  if (!hermesHome) {
    return hermesHome
  }

  const resolved = pathModule.resolve(String(hermesHome))
  const parent = pathModule.dirname(resolved)

  if (pathModule.basename(parent).toLowerCase() === 'profiles') {
    return pathModule.dirname(parent)
  }

  return resolved
}

function buildDesktopBackendEnv({
  hermesHome,
  runtimeDir,
  pythonPathEntries = [],
  venvRoot,
  currentEnv = process.env,
  platform = process.platform,
  pathModule = pathModuleForPlatform(platform),
  fsImpl = fs
}: any = {}) {
  const delimiter = delimiterForPlatform(platform)
  const currentPythonPath = currentEnv?.PYTHONPATH || ''
  const key = pathEnvKey(currentEnv, platform)

  return {
    PYTHONPATH: appendUniquePathEntries([...pythonPathEntries, currentPythonPath], { delimiter }),
    // Force PEP 540 UTF-8 mode in the spawned Python backend so its stdio and
    // subprocess defaults are UTF-8 even on non-UTF-8 Windows locales (GBK,
    // cp1252, ...). hermes_bootstrap sets this inside the child too, but only
    // after import — anything emitted earlier (interpreter startup errors,
    // pre-bootstrap tracebacks) still decodes with the locale default without
    // this. User's explicit setting wins. Re-port of PR #56499 (echoriver89).
    PYTHONUTF8: currentEnv?.PYTHONUTF8 ?? '1',
    [key]: buildDesktopBackendPath({
      runtimeDir,
      venvRoot,
      currentPath: currentPathValue(currentEnv, platform),
      platform,
      pathModule,
      fsImpl
    })
  }
}

export {
  appendUniquePathEntries,
  buildDesktopBackendEnv,
  buildDesktopBackendPath,
  delimiterForPlatform,
  normalizeHermesHomeRoot,
  pathEnvKey,
  POSIX_SANE_PATH_ENTRIES
}
