---
description: Show local vs remote sync status (git status analog)
allowed-tools: Read, Glob, Bash, Task, Workflow, mcp__claude_ai_Notion__notion-fetch
---

Show working-tree status vs remote Notion. Read-only; never writes.

Read these references first:
- @${CLAUDE_PLUGIN_ROOT}/references/manifest-schema.md
- @${CLAUDE_PLUGIN_ROOT}/references/path-mapping.md
- @${CLAUDE_PLUGIN_ROOT}/references/conflict-protocol.md
- @${CLAUDE_PLUGIN_ROOT}/references/notion-mcp-cheatsheet.md
- @${CLAUDE_PLUGIN_ROOT}/references/sub-agent-schemas.md

**Never hash, normalize, or diff in-context.** Run every hash / normalization / UUID extraction through `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py"` (see `references/manifest-schema.md` → "Compute helper"). Keep page bodies out of this command's context per `references/notion-mcp-cheatsheet.md` → "Sub-agent fan-out & context discipline".

## Steps

1. Walk upward to locate `.nsync/`. Abort if missing: "Not a sync root. Run /nsync:init first."
2. Load `.nsync/manifest.json` and `.nsync/ignore`. Compute the ignore matcher.
2a. **Read-only root-backfill check.** If `manifest.pages` has no entry keyed by `config.parent.page_id`, `fetch` the parent page, write the `<content>` body to `.nsync/tmp/parent.remote.md`, then run `python3 nsync.py normalize --mode remote .nsync/tmp/parent.remote.md` and check whether the normalized output (after `strip()`) is empty or equal to the title-heading line — per `references/path-mapping.md` → "Non-empty parent body". If non-empty, remember this for the output section "Pending migration". Do NOT write any files outside `.nsync/tmp/` — this is status, not pull. (The `<empty-block/>` Notion serialization for an empty paragraph is now stripped by `--mode remote`; status will NOT falsely flag a Pending migration on a parent whose body holds only that block.)
3. Glob `**/*.md` under sync root, excluding `.nsync/` and ignored patterns. Scope is `.md` only — never look at other extensions.
4. Recompute `local_hash` for every glob result in one shot: pipe the paths into `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" hash-batch --mode local` and read back the `path<TAB>sha256:...` lines. Cross-reference against PageRecords:
   - Local file exists + PageRecord exists + hashes match → Clean.
   - Local file exists + PageRecord exists + hashes differ → Modified.
   - Local file exists + no PageRecord → Added (candidate).
   - PageRecord exists + local file missing + not `local_ignored` → Deleted (candidate). Run rename detection per `references/path-mapping.md` BUT do NOT apply — surface as a Renamed candidate.
   - PageRecord with `local_ignored: true` → "Ignored (was tracked)".
5. Get every tracked page's fresh `remote_hash` **without holding bodies in context**, per `references/notion-mcp-cheatsheet.md` → "Sub-agent fan-out & context discipline" (skip `local_ignored: true`). ≥8 pages: dispatch via `Workflow` — one sub-agent per `NSYNC_READ_BATCH` batch, each returning a `BatchReadRecords` validated by the schema in `references/sub-agent-schemas.md`. Inside each sub-agent: `notion-fetch` each page in its batch (emit all fetches in one assistant message → parallel), write each body to `.nsync/tmp/<page_id>.remote.md`, run `nsync.py hash --mode remote`, run `nsync.py extract-uuids` over the `<page url>` tags to capture them in canonical dashed form, return the schema-validated batch record, and discard the bodies on return. <8 pages: run inline with process-and-discard. Honor the 429 backoff and report progress per "Progress reporting" (e.g. `Hashed 12/40 pages…`). The main loop keeps only the compact records. Any page whose returned `remote_hash != manifest.remote_hash` is **Remote-newer**. Pure metadata bumps (comments, property toggles, rich-block-only edits) do NOT move the markdown-only hash and so are NOT reported as remote changes.
6. Derive **reachability** from the data already in hand — never call `notion-search` for tree enumeration (it is relevance-ranked, not exhaustive; see `references/notion-mcp-cheatsheet.md` → "`notion-search` is NOT a tree-listing API"):
   - `reachable = { parent's <page url> child UUIDs (from step 2a's fetch) } ∪ { each has_children record's child_link_tags UUIDs (from step 5) }`. Pipe every `<page url>` source through `python3 nsync.py extract-uuids` so hybrid / escaped / bare-URL forms all canonicalize to dashed UUIDs.
   - **Remote-added** = `reachable − manifest_pids`.
   - **Remote-trashed** = `manifest_pids − reachable − { config.parent.page_id }` (the root entry is special-cased and never trashed; see `manifest-schema.md` → "Root PageRecord").

## Output (skip empty sections)

- **Modified (local)** — file list with old hash → new hash short prefix.
- **Added (local)** — untracked `.md` files; show the destination Notion path they'd attach to.
- **Deleted (local)** — PageRecords missing their files (excluding `local_ignored`).
- **Renamed (suggested)** — heuristic matches `old → new`. Tell the user these are unconfirmed; commit/pull will prompt.
- **Conflicts** — files where both sides changed (resolution required before commit).
- **Remote-newer** — pages with confirmed remote markdown changes since last pull.
- **Remote-added** — remote pages not yet mirrored locally.
- **Remote-trashed** — pages no longer reachable upstream.
- **Ignored (was tracked)** — pages flagged `local_ignored: true`.
- **Pending migration** — if step 2a flagged a missing root entry with non-empty parent body, print: `Root index.md will be created on next /nsync:pull (parent body has <N chars> of content).`
- **Child links to update** — for each tracked PageRecord with `has_children: true`, compare the **expected** child-link line set (derived from the `child_link_tags` in the step-5 compact records, with UUIDs canonicalized via `python3 nsync.py extract-uuids`, plus manifest path lookups) against the **existing** child-link lines in the local `index.md` (matched via the recognition regex in `references/path-mapping.md` → "Child-link lines"). List any file whose expected ≠ existing, with a one-line summary like `<path>: +<n_added> / -<n_removed> / ~<n_renamed>`. Do NOT modify anything — `/nsync:pull` will reconcile.
- **Pending child-link placeholders** - for each `has_children` file, scan for placeholder child-link lines (the placeholder regex in `references/path-mapping.md` → "Child-link lines"). List each file with its placeholder target path(s) and whether each currently resolves: to a tracked page or to a `.md` staged as Added (will become a managed link on `/nsync:commit`), or unresolvable (typo / out-of-tree / wrong parent). Show this section whenever any placeholder exists, even if every other section is empty. Placeholders are stripped from `local_hash`, so they never show as Modified.

Close with a one-liner suggesting `/nsync:pull` and/or `/nsync:commit` based on findings, or "Working tree clean. All pages in sync." if every section is empty.
