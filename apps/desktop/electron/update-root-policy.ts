/**
 * Pure policy for which update roots the desktop's git update flow may touch.
 *
 * Mirrors installation/tree.py (is_managed_install_root + the "source"
 * install method): a .git tree at a managed install root is ours to update; a
 * .git tree anywhere else is somebody's working tree, and the update flow
 * would stash local changes and move it to the update branch — so both check
 * and apply refuse it and point at `git pull`. No .git at all is not
 * updatable through git.
 *
 * Extracted from main.ts so the policy is unit testable without booting
 * Electron (main.ts requires('electron') at load).
 */

export type UpdateRootKind = 'managed-checkout' | 'unmanaged-checkout' | 'not-a-git-checkout'

export interface ClassifyUpdateRootDeps {
  /** True when the root has a .git entry (directory or worktree gitfile). */
  isGitCheckout: (root: string) => boolean
  /** Canonical absolute forms of the managed install roots. */
  managedRoots: readonly string[]
  /** Resolve a path to its canonical absolute form (realpath when possible). */
  canonicalize: (p: string) => string
  /** Case-insensitive filesystems (Windows, default macOS) compare lowercased. */
  caseInsensitive?: boolean
}

// The canonical installer-created checkout locations `hermes update` owns:
// the per-user root under HERMES_HOME, and the FHS root-install location
// (install.sh as root on Linux). Harmless to list the FHS path on other
// platforms — nothing resolves there.
export function managedInstallRoots(hermesHome: string, joinPath: (...parts: string[]) => string): string[] {
  return [joinPath(hermesHome, 'hermes-agent'), '/usr/local/lib/hermes-agent']
}

export function classifyUpdateRoot(root: string, deps: ClassifyUpdateRootDeps): UpdateRootKind {
  if (!deps.isGitCheckout(root)) {
    return 'not-a-git-checkout'
  }

  const fold = (p: string) => (deps.caseInsensitive ? deps.canonicalize(p).toLowerCase() : deps.canonicalize(p))
  const resolved = fold(root)

  return deps.managedRoots.some(managed => fold(managed) === resolved) ? 'managed-checkout' : 'unmanaged-checkout'
}

export function unmanagedCheckoutMessage(root: string): string {
  return (
    `This copy of Hermes Desktop is running from a git checkout at ${root}.\n` +
    'Update it by closing Hermes and running `git pull`.'
  )
}
