---
description: Sync remote Notion changes into the local working tree (git pull --rebase analog)
allowed-tools: Read, Write, Edit, Glob, Bash, AskUserQuestion, mcp__claude_ai_Notion__notion-fetch, mcp__claude_ai_Notion__notion-search
---

Pull remote Notion changes into the local working tree. Behaves like `git pull --rebase`: auto-merge anything that doesn't conflict, prompt per file for genuine conflicts.

Read these references first:
- @${CLAUDE_PLUGIN_ROOT}/references/conflict-protocol.md
- @${CLAUDE_PLUGIN_ROOT}/references/manifest-schema.md
- @${CLAUDE_PLUGIN_ROOT}/references/path-mapping.md
- @${CLAUDE_PLUGIN_ROOT}/references/notion-mcp-cheatsheet.md

## Steps

1. Walk upward to locate `.nsync/`. If absent, abort: "Not a sync root. Run /nsync:init first."
2. Load `.nsync/manifest.json` and `.nsync/ignore`. Compute the ignore matcher (gitignore syntax — shell out to `git check-ignore` if available, else use an inline matcher).
3. Run rename detection per `references/path-mapping.md`. Apply user-confirmed renames to PageRecord paths BEFORE classifying state.
4. Glob `**/*.md` under sync root (scope = `.md` only, excluding `.nsync/` and ignored patterns). Recompute `local_hash` for every tracked file.
5. For each tracked page (skip those with `local_ignored: true`): `fetch` to get current content. Compute fresh `remote_hash` over the markdown-only portion. Classify state per the hash-only table in `references/conflict-protocol.md`.
6. Enumerate the parent's full sub-tree via `search` (paginate). Detect:
   - Remote pages with no matching PageRecord → mirror locally as new files (compute path, write `.md` + snapshot, add PageRecord).
   - Tracked PageRecords whose page is no longer reachable → prompt the symmetric trash flow (delete local? keep as Added on next commit?). Record a TrashEntry on choice.

## Per-state actions

- **Clean** — refresh `last_synced_at` only. No file write, no hash change.
- **Auto-mergeable (local-only)** — leave the file alone. No manifest change (local_hash already matches because it derives from the same content; only refresh `last_synced_at` for audit).
- **Auto-mergeable (remote-only)** — overwrite local file with remote markdown. Refresh `local_hash`, `remote_hash`, snapshot (`.nsync/snapshots/<page_id>.md`), and `last_synced_at`.
- **Conflict** — drive UX per `references/conflict-protocol.md`. The resolution choice determines what `local_hash`, `remote_hash`, and the snapshot become — follow the "Snapshot update after resolution" table in that reference exactly.

## Conflict UX (NEVER inject `<<<<<<<` markers)

For each conflicting page, AskUserQuestion with options `[L]ocal`, `[R]emote`, `[E]dit`, `[S]kip`.

- `[E]dit` writes a scratch buffer to `.nsync/conflicts/<page_id>.scratch.md` containing three labeled sections (LOCAL / REMOTE / MERGED, with MERGED pre-filled as a best-guess merge). Ask the user to edit the MERGED section, confirm, then overwrite the real file with the MERGED content and remove the scratch file.
- After each resolution, ask: "Continue with the next conflict, or stop and finish later?" Partial pulls are allowed; the manifest commits per-file after each resolution lands.

## Atomicity

Update the manifest and snapshot file atomically per page after each operation succeeds. A pull that crashes or is aborted mid-way leaves a consistent state for the pages it already processed.

## Output

Summary by section:
- Auto-merged (remote-only changes pulled): file list
- New local files created from remote-added pages
- Conflicts resolved: file + chosen resolution
- Conflicts skipped: file list (these will reappear on next pull and block commit)
- Remote-trashed handled: file list + disposition

Suggest next step (`/nsync:commit` if local edits exist, `/nsync:status` otherwise).
