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
3. **Backfill root `index.md` if missing.** Check whether `manifest.pages` has an entry keyed by `config.parent.page_id`. If absent:
   - `fetch` the parent page. Apply the markdown normalization pipeline (rich-block tags + image-block lines stripped; `<page url>` tags are SUBSTITUTED with child-link lines, not stripped, per `references/path-mapping.md` → "Child-link lines"). For `<page url>` whose UUID is in the manifest, render with the relative path. For UUIDs not in the manifest (children not yet enumerated), defer the substitution to step 7's regeneration pass.
   - If the resulting body is **empty** per `references/path-mapping.md` → "Non-empty parent body" (zero-length after strip, or only the `# <Title>` line): skip silently. No file, no PageRecord.
   - If **non-empty**: write `<sync-root>/index.md` with the substituted body. Write `.nsync/snapshots/<parent_page_id>.md` with the same content. Add the root PageRecord (`path: "index.md"`, `parent_page_id: null`, `has_children: true` if the parent has sub-pages else `false`, hashes computed over the normalized content, `last_synced_at` now, `local_ignored: false`, `rich_blocks` from detection). Persist the manifest. Surface in the output summary as `Root index.md created (backfill)`.
4. Run rename detection per `references/path-mapping.md`. Apply user-confirmed renames to PageRecord paths BEFORE classifying state. (Rename detection already skips PageRecords with `parent_page_id: null` per the algorithm in `path-mapping.md`.)
5. Glob `**/*.md` under sync root (scope = `.md` only, excluding `.nsync/` and ignored patterns). Recompute `local_hash` for every tracked file.
6. For each tracked page (skip those with `local_ignored: true`): `fetch` to get current content. Compute fresh `remote_hash` over the markdown-only portion. Classify state per the hash-only table in `references/conflict-protocol.md`.
7. Enumerate the parent's full sub-tree via `search` (paginate). Detect:
   - Remote pages with no matching PageRecord → mirror locally as new files (compute path, write `.md` + snapshot, add PageRecord).
   - Tracked PageRecords whose page is no longer reachable → prompt the symmetric trash flow (delete local? keep as Added on next commit?). Record a TrashEntry on choice.
8. **Regenerate child-link lines** for every PageRecord with `has_children: true` (including the root). For each:
   - Use the already-fetched `remote_raw` from step 6.
   - Extract the ordered list of whole-line `<page url url="<href>"[ icon="..."]>Title</page>` tags → **expected** child-link lines (look up each target's local path in the manifest; render `external` if the UUID is absent).
   - Scan the current local file for **existing** child-link lines via the regex in `references/path-mapping.md` → "Child-link lines" → "Recognition regex".
   - Apply the "Regeneration trigger" rules in `path-mapping.md`:
     - `expected == existing` byte-for-byte → no-op.
     - `existing` non-empty, mismatch → replace each existing line by UUID match (rewrites title text and target path from current manifest data); insert any newly-added expected lines after the last existing child-link line; drop any orphaned existing lines whose UUID is no longer in `expected`. The position of the first existing line stays put.
     - `existing` empty, `expected` non-empty (migration case) → if local non-child-link content equals snapshot non-child-link content (no user prose edits), overwrite the local file with `remote_raw` after substituting `<page url>` tags with child-link lines (positions match Notion). Otherwise append all expected lines after the file's last non-empty line and surface `Added <N> child-link lines to <path>; reposition manually if desired.`
   - Overwrite the snapshot to match the new local file. Recompute `local_hash` and `remote_hash` per the pipeline; the strip rules make these stable so the manifest hashes are unchanged in steady state. Persist if anything moved.

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
- Child-link lines updated: list each affected `index.md` with the delta (added/removed/renamed)
- Conflicts resolved: file + chosen resolution
- Conflicts skipped: file list (these will reappear on next pull and block commit)
- Remote-trashed handled: file list + disposition

Suggest next step (`/nsync:commit` if local edits exist, `/nsync:status` otherwise).
