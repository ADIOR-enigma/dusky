# Dusky Update Sync Behavior — Local Changes vs Upstream

Reference notes for how `update_dusky.py` reconciles local work-tree changes with upstream.
Confirms the behavior of editing/deleting scripts (or any tracked file) across updates.

## Core mechanic

- The repo is a **bare** repo in `~/dusky` with the home directory as the **work tree**.
- Every update the Git engine (`GitEngine.execute_phase`, `update_dusky.py:3366`) does:
  1. Fetch upstream into a tracking ref.
  2. **Collision backup** (`_backup_worktree_collisions`, `:2960`): untracked/conflicting
     files that collide with incoming *tracked* paths are moved to `moved_aside_*`.
     Tracked files are skipped here.
  3. **Snapshot** (`_capture_tracked_changes` + `_backup_user_modifications`, `:3064`/`:3096`):
     any local tracked change (modified/deleted) found via `diff-index HEAD` is backed up to
     `your_changes_*` with a `MANIFEST.txt`.
  4. **Reset** (`git reset --hard` to upstream, `:3591`).
  5. **Restore** (`_restore_user_modifications`, `:3252`): replays your local changes,
     deciding per file by comparing its content (OID + mode) at the old HEAD vs the new upstream HEAD.

The decision in step 5 is purely "did upstream change this file between old HEAD and new HEAD?"

## Scenarios

### 1. You deleted a script; upstream did NOT change it
- Stays deleted. `diff-index` reports `D`; reset re-creates the file, then restore hits
  `delete-safe` (new content == old content) and deletes it again (`:3292`).
- Your deletion is permanent and re-applied on every update. No backup copy is made
  (manifest records `status=D has_copy=0`).

### 2. You edited a script; upstream did NOT change it
- Your edit survives. Backed up to `your_changes_*`, reset applies upstream's copy, then
  restore sees new content == old content → `restore` (`:3332`) → your version is put back.
- The `your_changes_*` backup dir is removed after a successful restore.

### 3. You edited a script; upstream ALSO changed it
- **Upstream wins.** Because new content != old content, it goes `merge` (`:3309`):
  your version is saved to `manual_merge_<timestamp>/<path>` and flagged in the TUI
  ("N file(s) need manual merge... Review in: ..."). The work tree keeps upstream's new version.

### 4. You deleted a script; upstream changed it (even slightly)
- **The file comes back** with upstream's new version. `delete-restored` (`:3305`):
  your deletion is overridden because the file changed upstream.

## Missed scenarios worth knowing

- **You added a new untracked script** (not in upstream): untouched — `reset --hard` only
  touches tracked files. It persists. **But** if upstream later adds a file at that exact
  path, your copy is moved aside to `moved_aside_*` and upstream's takes the path (`:2960`).
- **Upstream deletes a script you modified:** your modified copy is restored from backup
  (`not new_oid` → `restore`, `:3287`), so the file *survives* upstream's deletion and
  becomes a permanently untracked local file.
- **Both you and upstream delete it:** stays deleted, no-op (`delete-preserved`).
- **Renaming** = delete + new file: the old path follows rule 1 or 4; the new file is
  untracked and persists.
- **`once` markers** are separate from file state: checksum-based. Editing a `once:content`
  script changes its checksum so it may re-run; `once:sealed` / `once:forever` behave
  differently regardless of file state.
- **`--skip-sync`** bypasses all of the above entirely (no git operations).

## Takeaways

- The two "surprise" behaviors:
  - Rule 4: a trivial upstream change resurrects a locally-deleted file.
  - Upstream-deletes-your-modified-file: your version beats upstream.
- Both are driven by comparing the file's OID at old HEAD vs new upstream HEAD in
  `_restore_user_modifications` (`update_dusky.py:3252`).
