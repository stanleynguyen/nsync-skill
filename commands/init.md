---
description: Initialize a Notion sync root in the current folder
argument-hint: "[notion-parent-url?]"
allowed-tools: Read, Write, Glob, Bash, AskUserQuestion, mcp__claude_ai_Notion__notion-fetch, mcp__claude_ai_Notion__notion-search, mcp__claude_ai_Notion__notion-create-pages
---

Initialize the current working directory as an nsync sync root mirroring a Notion parent page tree.

Read these references first — they hold the canonical rules and you must follow them exactly:
- @${CLAUDE_PLUGIN_ROOT}/references/path-mapping.md
- @${CLAUDE_PLUGIN_ROOT}/references/manifest-schema.md
- @${CLAUDE_PLUGIN_ROOT}/references/notion-mcp-cheatsheet.md

Input: `$1` may contain a Notion parent URL. If absent, ask the user for it via AskUserQuestion.

## Preflight (run in order, fail fast with an actionable message)

1. Walk upward from CWD looking for any existing `.nsync/` directory. If found, abort with: "A sync root already exists at `<path>`. Run /nsync:status there, or pick a different directory."
2. List CWD. Anything other than `.git/` and `.gitignore` counts as existing content. If non-empty, AskUserQuestion with three options:
   - "Initialize anyway and ignore existing files (they won't sync unless they match remote pages)"
   - "Clear and initialize (DELETES every file except .git/)"
   - "Abort" (default)
   On "Clear", list every file and directory that would be removed and require a second confirmation. Never touch `.git/`.
3. Parse the URL. Accept: `notion.so` URLs with a 32-hex page id, `*.notion.site` URLs with the same shape, or a raw UUID (with or without dashes). Reject URLs with `?v=`, `/collection`, or any database-shaped target.
4. Smoke-test Notion connector connectivity: call `mcp__claude_ai_Notion__notion-search` with `{ "query": "_", "page_size": 1 }`. If the tool isn't registered, the user hasn't connected Notion yet — tell them: "Notion connector not installed. Open Claude Code → Settings → Connectors → Notion → Connect, then re-run /nsync:init." If the call returns an auth error, instead tell them their OAuth session has expired and they need to reconnect via the same path.
5. Call `fetch` on the parsed UUID and map the response per `references/notion-mcp-cheatsheet.md` "Notion error mapping" table (404 / 403 / `<data-source>`).

## Initialize

Once preflight passes:

1. Capture parent page metadata (UUID, title, URL).
2. Create `.nsync/` with:
   - `config.json` — `{ schema_version: 1, plugin_version: "0.1.0", parent: { page_id, url, title }, created_at: <RFC3339 UTC> }`
   - `manifest.json` — initial shell with parent populated, `pages: {}`, `trash_log: []`. Sorted-key, two-space-indented.
   - `ignore` — copy the "Default ignore patterns" block from `references/path-mapping.md` verbatim.
   - `snapshots/` — empty directory.
3. Recursively enumerate the parent's sub-page tree via `search` filtered by `page_url=<parent_url>`. Paginate until exhausted.
4. For each discovered page (depth-first):
   - Compute the local path per `references/path-mapping.md` (slug + collision suffix + `index.md` for pages with children).
   - `fetch` the page body. Apply the markdown normalization pipeline; record rich-block presence in `rich_blocks` (type + coarse anchor + summary) per `references/manifest-schema.md`.
   - Write the local `.md` file with the markdown-only body.
   - Write `.nsync/snapshots/<page_id>.md` with the same content.
   - Add a PageRecord with computed `local_hash`, `remote_hash`, `last_synced_at` (now), `has_children`, `local_ignored: false`. (No `last_seen_remote_modified` field in v1 — hashes drive classification.)
5. Persist `manifest.json` (sorted-key, two-space-indented).

## Output

Print a short summary:
- Parent page title and URL
- Count of pages mirrored
- Count of rich blocks detected (preserved, not synced) — grouped by type
- Any pages that failed to fetch, with the underlying error per page

Suggest: "Run /nsync:status to verify the working tree is clean."
