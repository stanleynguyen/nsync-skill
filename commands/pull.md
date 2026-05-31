---
description: Sync remote Notion changes into the local working tree (git pull --rebase analog)
allowed-tools: Read, Write, Edit, Glob, Bash, Task, Workflow, AskUserQuestion, mcp__claude_ai_Notion__notion-fetch
---

Pull remote Notion changes into the local working tree. Behaves like `git pull --rebase`: auto-merge anything that doesn't conflict, prompt per file for genuine conflicts.

Read these references first:
- @${CLAUDE_PLUGIN_ROOT}/references/conflict-protocol.md
- @${CLAUDE_PLUGIN_ROOT}/references/manifest-schema.md
- @${CLAUDE_PLUGIN_ROOT}/references/path-mapping.md
- @${CLAUDE_PLUGIN_ROOT}/references/notion-mcp-cheatsheet.md
- @${CLAUDE_PLUGIN_ROOT}/references/sub-agent-schemas.md

**Never hash, normalize, diff, or extract UUIDs in-context.** Run every hash / normalization / UUID extraction through `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py"` (see `references/manifest-schema.md` → "Compute helper"), and keep page bodies out of this command's context per `references/notion-mcp-cheatsheet.md` → "Sub-agent fan-out & context discipline".

## Steps

1. Walk upward to locate `.nsync/`. If absent, abort: "Not a sync root. Run /nsync:init first."
2. Load `.nsync/manifest.json` and `.nsync/ignore`. Compute the ignore matcher (gitignore syntax — shell out to `git check-ignore` if available, else use an inline matcher).
3. **Backfill root `index.md` if missing.** Check whether `manifest.pages` has an entry keyed by `config.parent.page_id`. If absent:
   - `fetch` the parent page; cache body to `.nsync/tmp/<parent_page_id>.remote.md`. Apply the markdown normalization pipeline via `nsync.py normalize --mode remote` (rich-block tags + `<empty-block/>` + unrecognized-tag fallback + image-block lines stripped; `<page url>` tags are SUBSTITUTED with child-link lines, not stripped, per `references/path-mapping.md` → "Child-link lines"). UUIDs for child-link substitution come from `nsync.py extract-uuids` (never an inline regex). For UUIDs in the manifest, render with the relative path. For UUIDs not in the manifest (children not yet enumerated), defer the substitution to step 7's regeneration pass.
   - If the resulting body is **empty** per `references/path-mapping.md` → "Non-empty parent body" (zero-length after `strip()`, or only the `# <Title>` line): skip silently. No file, no PageRecord. (`<empty-block/>` is now stripped by the pipeline, so a Notion parent that holds only that block correctly hashes empty.)
   - If **non-empty**: write `<sync-root>/index.md` with the substituted body. Write `.nsync/snapshots/<parent_page_id>.md` with the same content. Add the root PageRecord (`path: "index.md"`, `parent_page_id: null`, `has_children: true` if the parent has sub-pages else `false`, hashes computed over the normalized content, `last_synced_at` now, `local_ignored: false`, `rich_blocks` from detection). Persist the manifest. Surface in the output summary as `Root index.md created (backfill)`.
4. Run rename detection per `references/path-mapping.md`. Apply user-confirmed renames to PageRecord paths BEFORE classifying state. (Rename detection already skips PageRecords with `parent_page_id: null` per the algorithm in `path-mapping.md`.)
5. Glob `**/*.md` under sync root (scope = `.md` only, excluding `.nsync/` and ignored patterns). Recompute `local_hash` for every tracked file in one shot via `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" hash-batch --mode local`.
6. Get every tracked page's fresh `remote_hash` **without holding bodies in context**, per `references/notion-mcp-cheatsheet.md` → "Sub-agent fan-out & context discipline" (skip `local_ignored: true`). ≥8 pages: dispatch via `Workflow` — one sub-agent per `NSYNC_READ_BATCH` batch, each returning `BatchReadRecords` validated against `references/sub-agent-schemas.md`. Inside each sub-agent: `notion-fetch` each page (emit all fetches in one assistant message → parallel), run `nsync.py hash --mode remote`, run `nsync.py extract-uuids` over the body to canonicalize the `<page url>` URLs into `child_link_tags`. <8 pages: run inline with process-and-discard. Honor the 429 backoff and report progress per "Progress reporting" (e.g. `Hashed 12/40 pages…`). Classify each page's state from its returned `remote_hash` per the hash-only table in `references/conflict-protocol.md`. The main loop keeps only the compact records — for the few pages that need a full remote body downstream (Auto-mergeable remote-only overwrite in "Per-state actions", and conflict resolution), re-fetch that single page on demand.
7. Derive **reachability** from the data already in hand — never call `notion-search` for tree enumeration (it is relevance-ranked, not exhaustive; see `references/notion-mcp-cheatsheet.md` → "`notion-search` is NOT a tree-listing API"). `reachable = { parent's <page url> child UUIDs (from step 3's parent fetch — re-fetch if the manifest already had a root entry so step 3 was a no-op) } ∪ { each has_children record's child_link_tags UUIDs (from step 6) }`. Run every `<page url>` source through `python3 nsync.py extract-uuids` so hybrid / escaped / bare-URL forms all canonicalize. Detect:
   - **Remote-added** = `reachable − manifest_pids` → mirror locally as new files (compute path, fetch body, write `.md` + snapshot, add PageRecord). Their bodies were not in the per-page hash fan-out; fetch each on demand here, but if more than 8 land in this list dispatch a second `Workflow` batch using the same `BatchReadRecords` schema.
   - **Remote-trashed** = `manifest_pids − reachable − { config.parent.page_id }` → prompt the symmetric trash flow (delete local? keep as Added on next commit?). Record a `TrashEntry` with `trashed_by: "remote-trashed"` on choice. The root entry is special-cased and never trashed (`manifest-schema.md` → "Root PageRecord").
8. **Regenerate child-link lines** for every PageRecord with `has_children: true` (including the root). Report progress per `references/notion-mcp-cheatsheet.md` → "Progress reporting" if this spans many files (e.g. `Regenerated child links: 5/18 index files…`). For each:
   - Use the page's `child_link_tags` from its step-6 compact record (the ordered whole-line `<page url ...>` tags) — no body re-fetch needed. Run the tag list through `python3 nsync.py extract-uuids` so hybrid / escaped / bare URLs all yield canonical dashed UUIDs.
   - Map those UUIDs to **expected** child-link lines (look up each target's local path in the manifest; render `external` if the UUID is absent).
   - Scan the current local file for **existing** lines matching **either** the managed recognition regex or the placeholder regex in `references/path-mapping.md` → "Child-link lines".
   - Apply the "Regeneration trigger" rules in `path-mapping.md` (reconcile **managed** lines only; leave **placeholder** lines exactly in place (they resolve at `/nsync:commit`, never here):
     - `expected` == existing managed set byte-for-byte (and no placeholders) → no-op.
     - any existing line present (managed or placeholder), mismatch → replace each existing **managed** line by UUID match (rewrites title text and target path from current manifest data); insert any newly-added expected lines after the last existing child-link line; drop orphaned existing **managed** lines whose UUID is no longer in `expected`. Placeholder lines are never UUID-matched, orphan-dropped, or reordered. The position of the first existing line stays put.
     - no existing line at all (neither managed nor placeholder), `expected` non-empty (migration case) → if local non-child-link content (both regexes stripped) equals snapshot non-child-link content (no user prose edits), overwrite the local file with `remote_raw` after substituting `<page url>` tags with child-link lines (positions match Notion). Otherwise append all expected lines after the file's last non-empty line and surface `Added <N> child-link lines to <path>; reposition manually if desired.`
   - Overwrite the snapshot to match the new local file. Recompute `local_hash` (and `remote_hash` if a body was re-fetched) via `nsync.py hash`; the strip rules make these stable so the manifest hashes are unchanged in steady state. Persist if anything moved.

## Per-state actions

- **Clean** — refresh `last_synced_at` only. No file write, no hash change.
- **Auto-mergeable (local-only)** — leave the file alone. No manifest change (local_hash already matches because it derives from the same content; only refresh `last_synced_at` for audit).
- **Auto-mergeable (remote-only)** — re-fetch this single page's body (it was not retained in step 6), overwrite the local file with the normalized remote markdown (`nsync.py normalize --mode remote`). Refresh `local_hash`, `remote_hash` (`nsync.py hash`), snapshot (`.nsync/snapshots/<page_id>.md`), and `last_synced_at`.
- **Conflict** — drive UX per `references/conflict-protocol.md`. The resolution choice determines what `local_hash`, `remote_hash`, and the snapshot become — follow the "Snapshot update after resolution" table in that reference exactly.

## Conflict UX (NEVER inject `<<<<<<<` markers)

For each conflicting page, AskUserQuestion with options `[L]ocal`, `[R]emote`, `[E]dit`, `[S]kip`.

- `[E]dit` writes a scratch buffer to `.nsync/conflicts/<page_id>.scratch.md` containing three labeled sections (LOCAL / REMOTE / MERGED, with MERGED pre-filled as a best-guess merge). Ask the user to edit the MERGED section, confirm, then overwrite the real file with the MERGED content and remove the scratch file.
- After each resolution, ask: "Continue with the next conflict, or stop and finish later?" Partial pulls are allowed; the manifest commits per-file after each resolution lands.

## Atomicity

Persist the manifest **once per batch/phase** (after each fetch batch's records are folded in, after the child-link regeneration pass, and after each conflict resolution), not after every single page. Snapshot files are written as their pages are processed. A pull that crashes or is aborted mid-way leaves a consistent state for the batches it already persisted. Clean up `.nsync/tmp/` at command end.

## Output

Summary by section:
- Auto-merged (remote-only changes pulled): file list
- New local files created from remote-added pages
- Child-link lines updated: list each affected `index.md` with the delta (added/removed/renamed)
- Conflicts resolved: file + chosen resolution
- Conflicts skipped: file list (these will reappear on next pull and block commit)
- Remote-trashed handled: file list + disposition

Suggest next step (`/nsync:commit` if local edits exist, `/nsync:status` otherwise).
