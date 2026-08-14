import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import {
  appendUniquePathEntries,
  buildDesktopBackendEnv,
  buildDesktopBackendPath,
  gitOnPathSkippingShim,
  isMacosXcodeShim,
  managedRuntimePathEntries,
  normalizeHermesHomeRoot,
  pathEnvKey,
  POSIX_SANE_PATH_ENTRIES,
  readRuntimeFacts,
  toolStoreDir
} from './backend-env'

/**
 * A fake fs exposing only what backend-env uses, so these tests describe the
 * runtimes.json contract without touching disk.
 */
function fakeFs(files: Record<string, string>, dirsWithBinaries: string[] = []) {
  const binaries = new Set(dirsWithBinaries)

  return {
    readFileSync(target: string) {
      if (!(target in files)) {
        throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' })
      }

      return files[target]
    },
    statSync(target: string) {
      if (!binaries.has(target)) {
        throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' })
      }

      return { isFile: () => true }
    }
  } as never
}

function factsFile(tools: Record<string, unknown>, schemaVersion = 2, pathOrder?: string[]) {
  return JSON.stringify({ schemaVersion, pathOrder, tools })
}

const RUNTIME = '/install/.hermes-runtime'
const FACTS_PATH = '/install/.hermes-runtime/runtimes.json'
// A source install's facts name entries in the machine-wide store; the
// runtime dir holds only runtimes.json. Passing no storeDir (as the
// self-contained payload does) makes the runtime dir its own store,
// which is why most tests below can still join against RUNTIME.
const STORE = '/home/u/.hermes/tools'

test('managed runtime dirs come from the registry facts, in the recorded order', () => {
  // Recorded out of order on purpose: pathOrder decides, not the tools map.
  const fsImpl = fakeFs(
    {
      [FACTS_PATH]: factsFile(
        {
          ripgrep: { version: '14.1.0', path: 'ripgrep/rg' },
          node: { version: '26.5.1', path: 'node/bin/node' },
          uv: { version: '0.12.1', path: 'uv/uv' }
        },
        2,
        ['node', 'uv', 'ripgrep']
      )
    },
    ['/install/.hermes-runtime/node/bin/node', '/install/.hermes-runtime/uv/uv', '/install/.hermes-runtime/ripgrep/rg']
  )

  assert.deepEqual(managedRuntimePathEntries(RUNTIME, { fsImpl, pathModule: path.posix }), [
    '/install/.hermes-runtime/node/bin',
    '/install/.hermes-runtime/uv',
    '/install/.hermes-runtime/ripgrep'
  ])
})

test('an extender is assembled ahead of what it extends', () => {
  // npm extends node in the pin table, so the provisioner records npm
  // first. It has to win on PATH or node's bundled npm shadows it — and
  // this module must take that from the file, never from a list of its own.
  const fsImpl = fakeFs(
    {
      [FACTS_PATH]: factsFile(
        {
          node: { version: '26.7.0', path: 'node/bin/node' },
          npm: { version: '12.0.2', path: 'npm/bin/npm' }
        },
        2,
        ['npm', 'node']
      )
    },
    ['/install/.hermes-runtime/node/bin/node', '/install/.hermes-runtime/npm/bin/npm']
  )

  assert.deepEqual(managedRuntimePathEntries(RUNTIME, { fsImpl, pathModule: path.posix }), [
    '/install/.hermes-runtime/npm/bin',
    '/install/.hermes-runtime/node/bin'
  ])
})

test('a facts file with no recorded order still yields every provisioned tool', () => {
  // Hand-edited files predate pathOrder. An empty PATH would be a worse
  // answer than an arbitrary order, so fall back to the tools map's keys.
  const fsImpl = fakeFs(
    {
      [FACTS_PATH]: factsFile({
        node: { version: '26.7.0', path: 'node/bin/node' },
        uv: { version: '0.12.3', path: 'uv/uv' }
      })
    },
    ['/install/.hermes-runtime/node/bin/node', '/install/.hermes-runtime/uv/uv']
  )

  assert.deepEqual(managedRuntimePathEntries(RUNTIME, { fsImpl, pathModule: path.posix }), [
    '/install/.hermes-runtime/node/bin',
    '/install/.hermes-runtime/uv'
  ])
})

test('pathDirs spreads a multi-directory tool across every entry', () => {
  // PortableGit's surface is cmd + bin + usr/bin, and the fact points at
  // cmd/git.exe. A single dirname() would lose bash.exe and the coreutils.
  const fsImpl = fakeFs(
    {
      [FACTS_PATH]: factsFile({
        git: {
          version: '2.55.0',
          path: 'git/cmd/git.exe',
          pathDirs: ['git/cmd', 'git/bin', 'git/usr/bin']
        }
      })
    },
    ['/install/.hermes-runtime/git/cmd/git.exe']
  )

  assert.deepEqual(managedRuntimePathEntries(RUNTIME, { fsImpl, pathModule: path.posix }), [
    '/install/.hermes-runtime/git/cmd',
    '/install/.hermes-runtime/git/bin',
    '/install/.hermes-runtime/git/usr/bin'
  ])
})

test('a source install joins its facts onto the shared store, not the runtime dir', () => {
  // The store split: runtimes.json lives with the install, the bytes live
  // in ~/.hermes/tools under a <tool>-<version>-<target> entry. Joining a
  // fact onto the wrong base yields a PATH of directories that do not
  // exist, which degrades silently to system tools.
  const fsImpl = fakeFs(
    {
      [FACTS_PATH]: factsFile({
        node: { version: '26.7.0', path: 'node-26.7.0-linux-x64/bin/node' }
      })
    },
    [`${STORE}/node-26.7.0-linux-x64/bin/node`]
  )

  assert.deepEqual(managedRuntimePathEntries(RUNTIME, { fsImpl, pathModule: path.posix, storeDir: STORE }), [
    `${STORE}/node-26.7.0-linux-x64/bin`
  ])
})

test('two installs sharing one store resolve to the same bytes', () => {
  // 44 worktrees cost 44 facts files and ONE copy of node — that is the
  // entire reason the store exists.
  const relative = 'node-26.7.0-linux-x64/bin/node'
  const entries = ['/a/.hermes-runtime', '/b/.hermes-runtime'].map(runtimeDir => {
    const fsImpl = fakeFs(
      { [`${runtimeDir}/runtimes.json`]: factsFile({ node: { version: '26.7.0', path: relative } }) },
      [`${STORE}/${relative}`]
    )

    return managedRuntimePathEntries(runtimeDir, { fsImpl, pathModule: path.posix, storeDir: STORE })
  })

  assert.deepEqual(entries[0], entries[1])
  assert.deepEqual(entries[0], [`${STORE}/node-26.7.0-linux-x64/bin`])
})

test('store-relative pathDirs spread against the store too', () => {
  // A PATH dir must resolve against the same base as the binary, or
  // PortableGit contributes three directories that are not there.
  const entry = 'git-2.53.0.3-win32-x64'
  const fsImpl = fakeFs(
    {
      [FACTS_PATH]: factsFile({
        git: {
          version: '2.53.0.3',
          path: `${entry}/cmd/git.exe`,
          pathDirs: [`${entry}/cmd`, `${entry}/bin`, `${entry}/usr/bin`]
        }
      })
    },
    [`${STORE}/${entry}/cmd/git.exe`]
  )

  assert.deepEqual(managedRuntimePathEntries(RUNTIME, { fsImpl, pathModule: path.posix, storeDir: STORE }), [
    `${STORE}/${entry}/cmd`,
    `${STORE}/${entry}/bin`,
    `${STORE}/${entry}/usr/bin`
  ])
})

test('a self-contained payload is its own store when no storeDir is given', () => {
  // The embedded desktop payload and the Nix bundle stage everything into
  // one directory and cannot write to the machine-wide store. Omitting
  // storeDir has to keep those working unchanged.
  const payload = '/Applications/Hermes.app/Contents/Resources/agent-payload'
  const fsImpl = fakeFs(
    { [`${payload}/runtimes.json`]: factsFile({ node: { version: '26.7.0', path: 'node/bin/node' } }) },
    [`${payload}/node/bin/node`]
  )

  assert.deepEqual(managedRuntimePathEntries(payload, { fsImpl, pathModule: path.posix }), [`${payload}/node/bin`])
})

test('toolStoreDir puts the store beside the Hermes home root', () => {
  assert.equal(toolStoreDir('/home/u/.hermes', { pathModule: path.posix }), '/home/u/.hermes/tools')
})

test('toolStoreDir resolves a profile home to the ROOT store', () => {
  // Tool bytes are a machine fact, not a profile one: two profiles on one
  // machine must share the entry rather than each holding a copy. Mirrors
  // installation.paths.get_tool_store() going through the same
  // normalization.
  assert.equal(toolStoreDir('/home/u/.hermes/profiles/work', { pathModule: path.posix }), '/home/u/.hermes/tools')
})

test('a recorded but vanished binary contributes no PATH entry', () => {
  // Half-deleted runtime dir: never emit a dir whose tool is not there.
  const fsImpl = fakeFs({ [FACTS_PATH]: factsFile({ node: { version: '26.5.1', path: 'node/bin/node' } }) }, [])

  assert.deepEqual(managedRuntimePathEntries(RUNTIME, { fsImpl, pathModule: path.posix }), [])
})

test('missing, malformed, and foreign-schema facts all read as unprovisioned', () => {
  const missing = fakeFs({})
  const malformed = fakeFs({ [FACTS_PATH]: '{not json' })
  const foreign = fakeFs({ [FACTS_PATH]: factsFile({ node: { version: '1', path: 'x' } }, 999) })

  for (const fsImpl of [missing, malformed, foreign]) {
    assert.deepEqual(readRuntimeFacts(RUNTIME, { fsImpl, pathModule: path.posix }), {})
    assert.deepEqual(managedRuntimePathEntries(RUNTIME, { fsImpl, pathModule: path.posix }), [])
  }
})

test('desktop backend PATH leads with managed tools, then venv, then sane entries', () => {
  const fsImpl = fakeFs(
    { [FACTS_PATH]: factsFile({ node: { version: '26.5.1', path: 'node/bin/node' } }) },
    ['/install/.hermes-runtime/node/bin/node']
  )

  const result = buildDesktopBackendPath({
    runtimeDir: RUNTIME,
    venvRoot: '/install/venv',
    currentPath: '/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin',
    platform: 'darwin',
    pathModule: path.posix,
    fsImpl
  })

  const entries = result.split(':')
  assert.deepEqual(entries.slice(0, 2), ['/install/.hermes-runtime/node/bin', '/install/venv/bin'])
  assert.ok(entries.includes('/opt/homebrew/bin'), 'Apple Silicon Homebrew bin is added')
  assert.ok(entries.includes('/usr/local/sbin'), 'missing standard sbin is added')

  for (const expected of POSIX_SANE_PATH_ENTRIES) {
    assert.ok(entries.includes(expected), `${expected} should be present`)
  }
})

test('every managed dir outranks the inherited PATH on both platforms', () => {
  for (const [platform, pathModule, inherited, delimiter, binary, factsPath] of [
    ['darwin', path.posix, '/usr/local/bin:/usr/bin', ':', '/rt/node/bin/node', '/rt/runtimes.json'],
    ['win32', path.win32, 'C:\\Program Files\\nodejs;C:\\Windows\\System32', ';', 'C:\\rt\\node\\node.exe', 'C:\\rt\\runtimes.json']
  ] as const) {
    const runtimeDir = platform === 'win32' ? 'C:\\rt' : '/rt'
    const relative = platform === 'win32' ? 'node/node.exe' : 'node/bin/node'
    const fsImpl = fakeFs({ [factsPath]: factsFile({ node: { version: '26.5.1', path: relative } }) }, [binary])

    const entries = buildDesktopBackendPath({
      runtimeDir,
      venvRoot: null,
      currentPath: inherited,
      platform,
      pathModule,
      fsImpl
    }).split(delimiter)

    const managed = managedRuntimePathEntries(runtimeDir, { fsImpl, pathModule })
    const firstInherited = Math.min(...inherited.split(delimiter).map(entry => entries.indexOf(entry)))

    assert.ok(managed.length > 0, `${platform} should resolve a managed dir`)

    for (const dir of managed) {
      assert.ok(
        entries.indexOf(dir) >= 0 && entries.indexOf(dir) < firstInherited,
        `${dir} must precede the inherited PATH on ${platform}`
      )
    }
  }
})

test('desktop backend PATH preserves first occurrence and avoids duplicates', () => {
  const result = buildDesktopBackendPath({
    runtimeDir: null,
    venvRoot: '/install/venv',
    currentPath: '/opt/homebrew/bin:/usr/bin:/opt/homebrew/bin:/bin',
    platform: 'darwin',
    pathModule: path.posix
  })

  const entries = result.split(':')
  assert.equal(entries.filter(entry => entry === '/opt/homebrew/bin').length, 1)
  assert.ok(
    entries.indexOf('/opt/homebrew/bin') < entries.indexOf('/opt/homebrew/sbin'),
    'existing Homebrew bin keeps its precedence over appended missing sane entries'
  )
})

test('no runtime dir means system tools — a degrade, not a break', () => {
  const result = buildDesktopBackendPath({
    runtimeDir: null,
    venvRoot: null,
    currentPath: '/usr/bin:/bin',
    platform: 'darwin',
    pathModule: path.posix
  })

  assert.ok(result.startsWith('/usr/bin:/bin'))
})

test('buildDesktopBackendEnv extends PYTHONPATH and backend PATH together', () => {
  const fsImpl = fakeFs(
    { [FACTS_PATH]: factsFile({ node: { version: '26.5.1', path: 'node/bin/node' } }) },
    ['/install/.hermes-runtime/node/bin/node']
  )

  const env = buildDesktopBackendEnv({
    hermesHome: '/Users/test/.hermes',
    runtimeDir: RUNTIME,
    pythonPathEntries: ['/repo/hermes-agent'],
    venvRoot: '/install/venv',
    currentEnv: {
      PATH: '/usr/bin:/bin',
      PYTHONPATH: '/existing/pythonpath'
    },
    platform: 'darwin',
    pathModule: path.posix,
    fsImpl
  })

  assert.equal(env.PYTHONPATH, '/repo/hermes-agent:/existing/pythonpath')
  assert.ok(env.PATH.startsWith('/install/.hermes-runtime/node/bin:/install/venv/bin:'))
  assert.ok(env.PATH.includes('/opt/homebrew/bin'))
})

test('buildDesktopBackendEnv forces PYTHONUTF8 unless the user set it explicitly', () => {
  const defaulted = buildDesktopBackendEnv({
    hermesHome: '/Users/test/.hermes',
    currentEnv: { PATH: '/usr/bin' },
    platform: 'darwin',
    pathModule: path.posix
  })

  assert.equal(defaulted.PYTHONUTF8, '1')

  const optedOut = buildDesktopBackendEnv({
    hermesHome: '/Users/test/.hermes',
    currentEnv: { PATH: '/usr/bin', PYTHONUTF8: '0' },
    platform: 'darwin',
    pathModule: path.posix
  })

  assert.equal(optedOut.PYTHONUTF8, '0')
})

test('normalizeHermesHomeRoot maps profile homes back to the global Hermes root', () => {
  assert.equal(
    normalizeHermesHomeRoot('/Users/test/.hermes/profiles/oracle', { pathModule: path.posix }),
    '/Users/test/.hermes'
  )
  assert.equal(
    normalizeHermesHomeRoot('C:\\Users\\test\\AppData\\Local\\hermes\\profiles\\oracle', { pathModule: path.win32 }),
    'C:\\Users\\test\\AppData\\Local\\hermes'
  )
  assert.equal(normalizeHermesHomeRoot('/Users/test/.hermes', { pathModule: path.posix }), '/Users/test/.hermes')
})

test('Windows PATH casing and delimiter are preserved without POSIX sane entries', () => {
  const fsImpl = fakeFs(
    { 'C:\\rt\\runtimes.json': factsFile({ node: { version: '26.5.1', path: 'node/node.exe' } }) },
    ['C:\\rt\\node\\node.exe']
  )

  const env = buildDesktopBackendEnv({
    hermesHome: 'C:\\Users\\test\\AppData\\Local\\hermes',
    runtimeDir: 'C:\\rt',
    pythonPathEntries: ['C:\\repo\\hermes-agent'],
    venvRoot: 'C:\\install\\venv',
    currentEnv: {
      Path: 'C:\\Windows\\System32;C:\\Windows',
      PYTHONPATH: 'C:\\existing\\pythonpath'
    },
    platform: 'win32',
    pathModule: path.win32,
    fsImpl
  })

  assert.equal(pathEnvKey({ Path: 'x' }, 'win32'), 'Path')
  assert.equal(env.PATH, undefined)
  assert.ok(env.Path.startsWith('C:\\rt\\node;'))
  assert.ok(env.Path.includes('\\venv\\Scripts;'))
  assert.ok(env.Path.includes(';C:\\Windows\\System32;C:\\Windows'))

  for (const posixEntry of POSIX_SANE_PATH_ENTRIES) {
    assert.ok(!env.Path.includes(posixEntry), `${posixEntry} must not leak into a Windows PATH`)
  }
})

test('the macOS xcode-select shim is never treated as a real git', () => {
  // /usr/bin/git on a Mac without the Command Line Tools does nothing but
  // pop a modal install dialog. A background app must not invoke it.
  assert.equal(isMacosXcodeShim('/usr/bin/git'), true)
  assert.equal(isMacosXcodeShim('/usr/bin/xcrun'), true)
  assert.equal(isMacosXcodeShim('/opt/homebrew/bin/git'), false)
  assert.equal(isMacosXcodeShim(null), false)
})

test('gitOnPathSkippingShim walks past the shim to a real git', () => {
  const present = new Set(['/usr/bin/git', '/opt/homebrew/bin/git'])

  const found = gitOnPathSkippingShim('/usr/bin:/opt/homebrew/bin', {
    delimiter: ':',
    pathModule: path.posix,
    exists: candidate => present.has(candidate)
  })

  assert.equal(found, '/opt/homebrew/bin/git')
})

test('gitOnPathSkippingShim returns null when only the shim exists', () => {
  // "No git" is the correct answer here: using the shim is worse than
  // reporting the absence, because it hijacks the user's screen.
  const found = gitOnPathSkippingShim('/usr/bin:/somewhere/else', {
    delimiter: ':',
    pathModule: path.posix,
    exists: candidate => candidate === '/usr/bin/git'
  })

  assert.equal(found, null)
})

test('appendUniquePathEntries flattens arrays and strings alike', () => {
  assert.equal(
    appendUniquePathEntries([['/a', '/b'], '/b:/c', null, ''], { delimiter: ':' }),
    '/a:/b:/c'
  )
})
