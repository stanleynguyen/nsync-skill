# nsync manifest schema (v1)

## Files in `.nsync/`

| File | Purpose |
|---|---|
| `config.json` | Plugin metadata + parent page identity |
| `manifest.json` | Per-page state — the source of truth |
| `ignore` | Gitignore-syntax patterns (local-side filter for `.md` files only) |
| `snapshots/<page_id>.md` | Last-synced markdown body, one file per tracked page |
| `conflicts/<page_id>.scratch.md` | Transient — written during conflict resolution, removed after |

Recommended `.gitignore` if the sync root lives in git: ignore `.nsync/snapshots/` (large, regeneratable) and `.nsync/conflicts/` (transient). Check in `.nsync/config.json`, `.nsync/manifest.json`, `.nsync/ignore` for team visibility.

## `config.json`

```json
{
  "schema_version": 1,
  "plugin_version": "0.1.0",
  "parent": {
    "page_id": "fb1d8c3a-5e21-4f70-8a23-9c4b6d8e1f02",
    "url": "https://www.notion.so/workspace/My-Docs-fb1d8c3a5e214f708a239c4b6d8e1f02",
    "title": "My Docs"
  },
  "created_at": "2026-05-23T10:00:00Z"
}
```

Written once by `/nsync:init`. Read-only after that — never mutated by other commands.

## `manifest.json` top-level

```json
{
  "schema_version": 1,
  "plugin_version": "0.1.0",
  "parent": { "page_id": "...", "url": "...", "title": "..." },
  "pages": {
    "<page_uuid>": { /* PageRecord */ }
  },
  "trash_log": [ /* TrashEntry */ ]
}
```

`pages` is keyed by UUID, not path — local renames update `path` inside the record without rekeying.

Persistence: sorted-key, two-space-indented JSON. Deterministic so diffs of the manifest itself read cleanly when checked into git.

## `PageRecord`

```json
{
  "path": "engineering/onboarding.md",
  "title": "Onboarding",
  "parent_page_id": "fb1d8c3a-5e21-4f70-8a23-9c4b6d8e1f02",
  "url": "https://www.notion.so/...",
  "last_synced_at": "2026-05-23T10:14:32Z",
  "local_hash": "sha256:9f2b...",
  "remote_hash": "sha256:9f2b...",
  "rich_blocks": [
    { "type": "callout", "anchor": "before:## Setup", "summary": "Yellow callout: 'Heads up'" }
  ],
  "has_children": false,
  "local_ignored": false
}
```

Field semantics:

- **`path`** — relative to sync root, forward slashes. For pages with children, points at `<slug>/index.md`.
- **`title`** — Notion page title at last sync. Cosmetic; manifest UUID key is authoritative.
- **`parent_page_id`** — the page above this one in the Notion tree. Equals top-level `parent.page_id` for direct children of the sync root.
- **`url`** — Notion URL (cache; not authoritative).
- **`last_synced_at`** — RFC3339 UTC. Time of the most recent successful sync operation on this page. Audit metadata only — not used by any classifier in v1.
- **`local_hash`** — SHA-256 over the file's UTF-8 bytes after the markdown normalization pipeline (below).
- **`remote_hash`** — SHA-256 over the markdown-only portion of the remote page body, same pipeline. Rich-block tags and standalone image-block lines are stripped before hashing — Notion-side edits that only touch a callout, embed, image, or page property will NOT register here.
- **`rich_blocks`** — array of `{ type, anchor, summary }` for non-markdown blocks present at last sync. `anchor` is a coarse positional marker: `before:<heading>`, `after:<heading>`, or `end-of-page`. Used for diff annotation and commit-time safety checks.
- **`has_children`** — true if the page has Notion sub-pages. Drives the `index.md` directory layout.
- **`local_ignored`** — true if the page's `path` currently matches a pattern in `.nsync/ignore`. Set during pull/status/commit pre-pass; means the remote is still tracked but no local file should exist.

There is intentionally no `last_seen_remote_modified` field in v1. State classification is driven purely by hash comparison — see `conflict-protocol.md` for the three-state table. v2 may reintroduce a `last_seen_remote_modified` field as an opt-in skip-fetch optimization (cache only; never authoritative for classification).

### Root PageRecord (the sync-root entry)

When the Notion parent page has a non-empty body (per `path-mapping.md` → "Non-empty parent body"), its body is mirrored at `<sync-root>/index.md` and tracked as a normal PageRecord with these specifics:

- **UUID key** equals `config.parent.page_id` — this is the marker that identifies the root entry.
- **`path`** = `"index.md"`.
- **`parent_page_id`** = `null` — sentinel meaning "this is the sync root; no nsync-visible parent". Every other PageRecord has a non-null `parent_page_id`.
- **`has_children`** mirrors whether the parent has Notion sub-pages (almost always `true` in practice).
- All other fields (`title`, `url`, hashes, `last_synced_at`, `rich_blocks`, `local_ignored`) behave identically to non-root PageRecords.

The root entry is subject to the constraints in `path-mapping.md` → "Root-page constraints": no rename, no orphan/trash, not in default ignore. `/nsync:commit`'s Deleted-page flow checks `parent_page_id == null` and routes to a restricted prompt (`[R]estore local` / `[E]mpty parent body`) instead of the normal orphan/manual-trash options.

If the parent body becomes empty over time (user clears it in Notion), the root PageRecord remains tracked; the next `/nsync:pull` overwrites `index.md` with the empty/title-only content as a normal Auto-mergeable remote-only update.

## `TrashEntry`

```json
{
  "page_id": "7c2a4d10-...",
  "path": "old-runbook.md",
  "trashed_at": "2026-05-23T09:55:12Z",
  "trashed_by": "orphaned-to-workspace",
  "title": "Old runbook",
  "last_local_hash": "sha256:a4..."
}
```

`trashed_by` values:

- **`orphaned-to-workspace`** — `notion-move-pages` moved the page to workspace level; it survives there for the user to trash later.
- **`untracked-no-remote-action`** — user picked manual trash; only the PageRecord was removed locally.
- **`remote-trashed`** — the page disappeared from remote search results; the local file was deleted.

Entries are kept indefinitely. Volume stays low because trash events are rare.

## Two-page example

```json
{
  "schema_version": 1,
  "plugin_version": "0.1.0",
  "parent": {
    "page_id": "fb1d8c3a-5e21-4f70-8a23-9c4b6d8e1f02",
    "url": "https://www.notion.so/workspace/Docs-fb1d8c3a5e214f708a239c4b6d8e1f02",
    "title": "Docs"
  },
  "pages": {
    "fb1d8c3a-5e21-4f70-8a23-9c4b6d8e1f02": {
      "path": "index.md",
      "title": "Docs",
      "parent_page_id": null,
      "url": "https://www.notion.so/workspace/Docs-fb1d8c3a5e214f708a239c4b6d8e1f02",
      "last_synced_at": "2026-05-23T10:14:32Z",
      "local_hash": "sha256:b04f...",
      "remote_hash": "sha256:b04f...",
      "rich_blocks": [],
      "has_children": true,
      "local_ignored": false
    },
    "7c2a4d10-3b8e-4a99-9c12-1d5e7f3a8b62": {
      "path": "onboarding.md",
      "title": "Onboarding",
      "parent_page_id": "fb1d8c3a-5e21-4f70-8a23-9c4b6d8e1f02",
      "url": "https://www.notion.so/workspace/Onboarding-7c2a4d103b8e4a999c121d5e7f3a8b62",
      "last_synced_at": "2026-05-23T10:14:32Z",
      "local_hash": "sha256:9f2bc1a8d4...",
      "remote_hash": "sha256:9f2bc1a8d4...",
      "rich_blocks": [],
      "has_children": false,
      "local_ignored": false
    },
    "a3e5c7f2-9d11-4b88-bf3c-2e6a9d4c1057": {
      "path": "engineering/coding-standards.md",
      "title": "Coding standards",
      "parent_page_id": "b8f1c4d2-1234-5678-9abc-def012345678",
      "url": "https://www.notion.so/workspace/Coding-standards-a3e5c7f29d114b88bf3c2e6a9d4c1057",
      "last_synced_at": "2026-05-22T18:02:01Z",
      "local_hash": "sha256:1a4b...",
      "remote_hash": "sha256:cc99...",
      "rich_blocks": [
        { "type": "callout", "anchor": "before:## Linting", "summary": "Info callout: 'Run lint before commit'" }
      ],
      "has_children": false,
      "local_ignored": false
    }
  },
  "trash_log": []
}
```

`coding-standards.md` is in a Remote-newer state — the stored `remote_hash` (`cc99...`) doesn't match what the next pull would compute by re-hashing the live Notion content, while `local_hash` still matches the on-disk file.

## Markdown normalization (hash pipeline)

Apply identically to local file bytes and remote markdown bodies before SHA-256:

1. UTF-8 decode.
2. NFC unicode normalization.
3. Convert all line endings to LF.
4. Strip trailing whitespace from every line.
5. Ensure exactly one trailing newline (no extra blank lines at EOF).
6. For `remote_hash` only: strip enhanced-markdown rich-block tags and their contents. See `notion-mcp-cheatsheet.md` for the full tag list. Image-block lines (markdown-syntax) are stripped here too.
7. **Both `remote_hash` and `local_hash`**: strip every whole-line `<page url ...>...</page>` tag AND every line matching the **child-link regex** OR the **placeholder child-link regex** in `path-mapping.md` → "Child-link lines". This is symmetric on both sides: the `<page url>` strip catches the remote form (what Notion serializes), and the child-link / placeholder strip catches the local forms (what nsync renders into `index.md`, plus user-authored placeholders awaiting commit-time backfill). Together they make child adds, removes, renames, reorders, and pending placeholders invisible to both hashes, so parent pages don't churn when children change.

Both implementations (one for `local_hash`, one for `remote_hash`) must produce byte-identical output on byte-identical inputs. If a discrepancy is suspected during debugging, log both the canonicalized text and the hash alongside the conflict report.
