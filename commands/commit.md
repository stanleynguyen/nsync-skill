---
description: Push local .md changes to Notion (git commit + push analog)
argument-hint: "[--force <path>...]"
allowed-tools: Read, Write, Edit, Glob, Bash(diff:*), AskUserQuestion, mcp__claude_ai_Notion__notion-fetch, mcp__claude_ai_Notion__notion-search, mcp__claude_ai_Notion__notion-update-page, mcp__claude_ai_Notion__notion-create-pages, mcp__claude_ai_Notion__notion-move-pages
---

Push local markdown changes to Notion. Workspace-wide staleness guard; `--force <path>` to override per-file.

Read these references first — they hold the rich-block-safe update strategy and the trash-gap workaround:
- @${CLAUDE_PLUGIN_ROOT}/references/manifest-schema.md
- @${CLAUDE_PLUGIN_ROOT}/references/path-mapping.md
- @${CLAUDE_PLUGIN_ROOT}/references/conflict-protocol.md
- @${CLAUDE_PLUGIN_ROOT}/references/notion-mcp-cheatsheet.md

Input: `$ARGUMENTS` may contain `--force <path>` (repeatable). Parse them out before the workspace-wide check.

## Steps

1. Walk upward to locate `.nsync/`. Abort if missing.
2. Load manifest + ignore. Apply ignore matcher. Parse `--force` paths from arguments.
3. Recompute `local_hash` for every tracked file. Run rename detection (apply confirmed renames to PageRecord paths now — these will be pushed as `notion-move-pages` calls in step 7 if the directory portion changed). Rename detection skips any PageRecord with `parent_page_id: null` (the root entry — its path is fixed at `index.md` and cannot be renamed in v1).
4. For every tracked page, `fetch` to get current content. Compute fresh `remote_hash` (markdown-only, with rich-block tags + standalone image-block lines stripped per `references/notion-mcp-cheatsheet.md`).
5. **Workspace-wide staleness check**: for every tracked page NOT in the `--force` set, verify `remote_hash == manifest.remote_hash`. If any page fails this check, abort with: "Remote has changes you haven't pulled. Run /nsync:pull first. To override specific files, pass `--force <path>` for each." When listing the offending pages, show the manifest's stored `remote_hash` prefix vs the freshly-computed one so the user sees what diverged.
6. Compute the commit set:
   - **Modified** — file exists, `local_hash != manifest.local_hash`, PageRecord exists.
   - **New** — `.md` file exists with no matching PageRecord and not ignored.
   - **Deleted** — PageRecord exists, file missing, not `local_ignored`.
   - **Renamed** — PageRecord path changed during step 3 and directory portion differs from the old parent_page_id's location.

## Modified pages — rich-block-safe update via `update_content`

For each Modified page:

1. Read the local `.md` file and the snapshot at `.nsync/snapshots/<page_id>.md`.
1a. **Strip child-link lines from both** before diffing. Use the recognition regex from `references/path-mapping.md` → "Child-link lines". These lines are nsync-managed: the user does not own them, Notion does not consume them, and pushing them via `update_content` would create plain markdown links that conflict with Notion's native child-page blocks. Reordering, renaming, inserting, or deleting child-link lines locally produces ZERO hunks — out of scope for v1. (A future `/nsync:mv` could push child reorders via the Notion API.)
2. Diff snapshot → local (both with child-link lines stripped) to produce hunks (use `diff -u` via Bash when helpful).
3. Use the already-fetched `remote_raw` (with rich-block tags) from step 4.
4. For each hunk, construct an `{ old_str, new_str }` pair following the strict line-boundary rule in `references/notion-mcp-cheatsheet.md` ("Rich-block-safe update via `update_content`" → step 5). Briefly: `old_str` must begin and end at line boundaries within `remote_raw`, cover the entire start and end lines of the change, include at least one full unchanged line of leading and trailing context where available, and be uniquely line-bounded-matchable in `remote_raw`. A short fragment like `"Read the README."` is NEVER acceptable as `old_str` — it can substring-match inside a longer line like `"Read the README and the CONTRIBUTING guide."` and corrupt the page.
   Validation rejects the hunk (and refuses the file's commit, with an actionable message) if:
   - `old_str` doesn't appear under a full-line-bounded match → race detected; re-pull this file.
   - More than one line-bounded match exists → ambiguous; ask the user to enlarge the context manually.
   - A rich-block tag or image-block line falls inside the `old_str` span → prompt `[F]orce-replace (deletes the rich block) / [S]kip`.
5. Submit all hunks for the file in a single `notion-update-page` call with `command="update_content"` and `content_updates=<array>`.
6. On success: `fetch` the page again, recompute `remote_hash`, overwrite `.nsync/snapshots/<page_id>.md` with the new local content, refresh `last_synced_at` in the PageRecord.

## New pages

For each New `.md`:

1. Determine the parent Notion page: look up the page whose path corresponds to the new file's containing directory (e.g., `engineering/roadmap.md` → parent = page at `engineering/index.md`). Root-level files attach to `config.json.parent.page_id`.
2. Call `notion-create-pages` with parent + title (derived from the H1 of the file or the slugged filename) + the local markdown body.
3. On success, add a PageRecord (UUID from the response), write the snapshot file, persist manifest.

## Deleted pages

For each Deleted PageRecord, branch on whether it is the **root entry** (`parent_page_id == null`, key equals `config.parent.page_id`).

**Non-root** Deleted PageRecord — AskUserQuestion with three options:

- `[O]rphan to workspace` (default) — `notion-move-pages` with `new_parent: { type: "workspace" }`. The page survives at workspace level; the user can trash later in Notion. Record TrashEntry `trashed_by: "orphaned-to-workspace"`. Remove the PageRecord.
- `[M]anual trash` — print the Notion URL and instruct the user to use the `···` → "Move to Trash" in Notion UI. Record TrashEntry `trashed_by: "untracked-no-remote-action"`. Remove the PageRecord.
- `[R]estore local` — recreate the local file from `.nsync/snapshots/<page_id>.md`. No remote action; no PageRecord change.

**Root** Deleted PageRecord — the parent Notion page cannot be trashed or orphaned (it is the sync target itself). AskUserQuestion with two options only:

- `[R]estore local` (default) — recreate `index.md` from `.nsync/snapshots/<parent_page_id>.md`. No remote action; PageRecord unchanged.
- `[E]mpty parent body` — call `notion-update-page` with `command: "replace_content"` and an empty body. Overwrite the snapshot with empty content. Recompute `local_hash` and `remote_hash` (both will be the hash of empty/normalized content). Keep the PageRecord; refresh `last_synced_at`. The user can later create a fresh `index.md` to repopulate the parent body.

Never offer orphan or manual-trash for the root entry.

## Renamed pages

For renames where the directory portion changed (different parent), call `notion-move-pages` with the new parent's page_id (look up by path). Update `parent_page_id` in the PageRecord. Local filename changes alone do NOT change the Notion page title in v1 (decoupled — a future `/nsync:mv` command would do that).

## Atomicity

Persist the manifest after every successful Notion operation, so a crash mid-commit leaves a consistent state for the operations that already landed.

## Output

Per-file result summary: Modified pushed / New created / Deleted disposition / Renamed moved. Note any `--force` bypasses with the manifest-vs-remote hash prefixes that differed. Suggest `/nsync:status` to verify.
