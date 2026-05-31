---
description: Push local .md changes to Notion (git commit + push analog)
argument-hint: "[--force <path>...]"
allowed-tools: Read, Write, Edit, Glob, Bash(diff:*), Bash(python3:*), Task, Workflow, AskUserQuestion, mcp__claude_ai_Notion__notion-fetch, mcp__claude_ai_Notion__notion-update-page, mcp__claude_ai_Notion__notion-create-pages, mcp__claude_ai_Notion__notion-move-pages
---

Push local markdown changes to Notion. Workspace-wide staleness guard; `--force <path>` to override per-file.

Read these references first — they hold the rich-block-safe update strategy and the trash-gap workaround:
- @${CLAUDE_PLUGIN_ROOT}/references/manifest-schema.md
- @${CLAUDE_PLUGIN_ROOT}/references/path-mapping.md
- @${CLAUDE_PLUGIN_ROOT}/references/conflict-protocol.md
- @${CLAUDE_PLUGIN_ROOT}/references/notion-mcp-cheatsheet.md
- @${CLAUDE_PLUGIN_ROOT}/references/sub-agent-schemas.md

**Never hash, normalize, strip child-links, diff, or extract UUIDs in-context.** Run all of it through `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py"` (see `references/manifest-schema.md` → "Compute helper"), and keep page bodies out of this command's context per `references/notion-mcp-cheatsheet.md` → "Sub-agent fan-out & context discipline".

Input: `$ARGUMENTS` may contain `--force <path>` (repeatable). Parse them out before the workspace-wide check.

## Steps

1. Walk upward to locate `.nsync/`. Abort if missing.
2. Load manifest + ignore. Apply ignore matcher. Parse `--force` paths from arguments.
3. Recompute `local_hash` for every tracked file in one shot via `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" hash-batch --mode local`. Run rename detection (apply confirmed renames to PageRecord paths now — these will be pushed as `notion-move-pages` calls in step 7 if the directory portion changed). Rename detection skips any PageRecord with `parent_page_id: null` (the root entry — its path is fixed at `index.md` and cannot be renamed in v1).
4. Get every tracked page's fresh `remote_hash` for the staleness check **without holding bodies in context**, per `references/notion-mcp-cheatsheet.md` → "Sub-agent fan-out & context discipline". ≥8 pages: dispatch via `Workflow` — one sub-agent per `NSYNC_READ_BATCH` batch, each returning `BatchReadRecords` validated against `references/sub-agent-schemas.md`. <8 pages: run inline with process-and-discard. Honor the 429 backoff and report progress per "Progress reporting" (e.g. `Checked 12/40 pages…`). Do NOT retain `remote_raw` here — the Modified-page step re-fetches `remote_raw` inside its own per-page sub-agent (that is where `old_str` validation needs it).
5. **Workspace-wide staleness check**: for every tracked page NOT in the `--force` set, verify `remote_hash == manifest.remote_hash`. If any page fails this check, abort with: "Remote has changes you haven't pulled. Run /nsync:pull first. To override specific files, pass `--force <path>` for each." When listing the offending pages, show the manifest's stored `remote_hash` prefix vs the freshly-computed one so the user sees what diverged.
6. Compute the commit set:
   - **Modified** — file exists, `local_hash != manifest.local_hash`, PageRecord exists.
   - **New** — `.md` file exists with no matching PageRecord and not ignored.
   - **Deleted** — PageRecord exists, file missing, not `local_ignored`.
   - **Renamed** — PageRecord path changed during step 3 and directory portion differs from the old parent_page_id's location.

   The **Backfill pass** (see "Backfill child-link placeholders" below) is separate from this commit set: it scans every `has_children` file for placeholder child-link lines regardless of classification, because a parent whose only edit is a placeholder hashes Clean (placeholder stripped from `local_hash`) and lands in neither New nor Modified.

## Modified pages — rich-block-safe update via `update_content`

Handle Modified pages with the **per-page sub-agent pattern** (see `references/notion-mcp-cheatsheet.md` → "Sub-agent fan-out & context discipline"): ≥8 Modified pages → dispatch via `Workflow` with one sub-agent per page (no `agentType` override — needs `notion-update-page` permissions); <8 → inline. Each runs the full chain below and returns a `CommitWriteResult` (`references/sub-agent-schemas.md`) — `remote_raw` never enters the main context. Report progress per "Progress reporting" as each page is pushed (e.g. `Pushed roadmap.md (4/9 modified)…`). The per-page chain:

1. The local `.md` file and the snapshot at `.nsync/snapshots/<page_id>.md`.
1a. **Strip child-link lines AND placeholder child-link lines from both** before diffing, via `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" strip-childlinks`. These lines are nsync-managed (placeholders are resolved by the Backfill pass above): the user does not own them, Notion does not consume them, and pushing them via `update_content` would create plain markdown links that conflict with Notion's native child-page blocks. Reordering, renaming, inserting, or deleting child-link lines locally produces ZERO hunks, out of scope for v1. (A future `/nsync:mv` could push child reorders via the Notion API.)
2. Diff snapshot → local via `nsync.py diff <snapshot> <local>` (it strips child-link lines from both sides) to produce hunks. Never diff in-context.
3. `fetch` this page to get `remote_raw` (with rich-block tags) inside the sub-agent.
4. For each hunk, construct an `{ old_str, new_str }` pair following the strict line-boundary rule in `references/notion-mcp-cheatsheet.md` ("Rich-block-safe update via `update_content`" → step 5). Briefly: `old_str` must begin and end at line boundaries within `remote_raw`, cover the entire start and end lines of the change, include at least one full unchanged line of leading and trailing context where available, and be uniquely line-bounded-matchable in `remote_raw`. A short fragment like `"Read the README."` is NEVER acceptable as `old_str` — it can substring-match inside a longer line like `"Read the README and the CONTRIBUTING guide."` and corrupt the page.
   Validation rejects the hunk (and refuses **this file's** commit, with an actionable message) if:
   - `old_str` doesn't appear under a full-line-bounded match → race detected; re-pull this file.
   - More than one line-bounded match exists → ambiguous; ask the user to enlarge the context manually.
   - A rich-block tag or image-block line falls inside the `old_str` span → prompt `[F]orce-replace (deletes the rich block) / [S]kip`.

   **Recovery is per-file, not per-batch.** A refused file returns `CommitWriteResult { pushed: false, new_remote_hash: null, error: "<reason>" }`. The main loop continues processing the remaining Modified pages; the failed file is surfaced in the output summary so the user can re-pull just that one. The commit batch never aborts because a single page lost a race or had an ambiguous hunk.

5. Submit all hunks for the file in a single `notion-update-page` call with `command="update_content"` and `content_updates=<array>`.
6. On success: `fetch` the page again, recompute `remote_hash` via `nsync.py hash --mode remote`, overwrite `.nsync/snapshots/<page_id>.md` with the new local content, refresh `last_synced_at` in the PageRecord. Return `CommitWriteResult { pushed: true, new_remote_hash: "sha256:…", warnings: [] }`. The main loop persists the manifest once per batch.

## New pages

Create New pages in **topological order**: a directory's `index.md` before any sibling/child `.md` whose parent resolves to it (a new folder's parent page must exist before its children are created, and before the Backfill pass needs its `page_id`). **Batch siblings that share a parent into one call** per `references/notion-mcp-cheatsheet.md` → "Concurrency, batching & rate limits" → "Batched creates":

1. Determine each New file's parent Notion page: look up the page whose path corresponds to the file's containing directory (e.g., `engineering/roadmap.md` → parent = page at `engineering/index.md`). Root-level files attach to `config.json.parent.page_id`.
2. **Group New files by resolved parent `page_id`**, then process groups in topological order — a parent's own group must land (yielding its `page_id`) before any group whose parent is one of those just-created pages. Within a group, order `pages[]` so an `index.md` precedes siblings that resolve to it.
3. For each group, submit **one `notion-create-pages` call** with the shared parent and a `pages[]` entry per file (each with title — derived from the H1 of the file or the slugged filename — and the local markdown body). `pages[]` holds up to 100; split oversized groups into successive calls.
4. On success, map each returned UUID back to its file, add a PageRecord per file, write each snapshot, and persist the manifest once per group (atomicity preserved — checkpoint per group instead of per file). Report progress per `references/notion-mcp-cheatsheet.md` → "Progress reporting" after each group lands (e.g. `Created 3 pages under engineering/ (8/15 new)…`) so creation never goes silent.

## Backfill child-link placeholders

After the New-page batch completes (every New page now has a `page_id`) and **before** the Modified-page diff, resolve placeholder child-link lines into managed lines. This runs after confirmed renames (step 3), so path resolution sees post-rename paths.

Scan **every `has_children` local file** for placeholder lines (the placeholder regex in `references/path-mapping.md` → "Child-link lines"), regardless of whether the file is in the commit set: a parent whose only change is a placeholder hashes Clean. For each placeholder, apply the resolution algorithm, parent guard, duplicate handling, and snapshot-overwrite rule in `references/path-mapping.md` → "Commit-time backfill":

- Resolve the captured target path (normalized sync-root-relative) against the union of pages created in this commit ∪ existing tracked pages; apply the parent guard (resolved target's `parent_page_id` must equal this file's page_id, where the root `index.md`'s page_id is `config.parent.page_id`).
- On success, rewrite the placeholder in place to the canonical managed line, then **overwrite that page's snapshot to match** the new local content and persist the manifest (`local_hash` will not move, since both regexes are stripped).
- Unresolvable / parent-mismatched placeholders are left in place with a warning; they are stripped from the Modified diff, so they never push to Notion as prose.
- If multiple placeholders in one file resolve to the same target, convert the first and warn on the rest.

This pass adds no Notion call: it uses the `page_id` from the New-page `notion-create-pages` responses (or the existing manifest) and writes only local files. The Notion child block stays at the page foot; only the local in-place position is set.

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

For renames where the directory portion changed (different parent), call `notion-move-pages` with the new parent's page_id (look up by path). Update `parent_page_id` in the PageRecord. If many pages move, report progress per `references/notion-mcp-cheatsheet.md` → "Progress reporting" (e.g. `Moved 3/7 renamed pages…`) so the move loop never goes silent. Local filename changes alone do NOT change the Notion page title in v1 (decoupled — a future `/nsync:mv` command would do that).

## Atomicity

Persist the manifest **once per batch/group/phase** (per create-group, per Modified-page batch, after the backfill pass, after the deleted/renamed phase), not after every single page, so a crash mid-commit leaves a consistent state for the batches that already landed. Clean up `.nsync/tmp/` at command end.

## Output

Per-file result summary: Modified pushed / New created / Deleted disposition / Renamed moved. Note any `--force` bypasses with the manifest-vs-remote hash prefixes that differed. Suggest `/nsync:status` to verify.
