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
   - **If the page has children** (`has_children: true`): walk the fetched body line by line. For each whole-line `<page url url="<href>"[ icon="..."]>Title</page>` tag, substitute the line with a child-link line per `references/path-mapping.md` → "Child-link lines" → "Format". The substitution requires knowing the target's local path, so process pages depth-first (children before parents) so the manifest already contains the relevant PageRecords. For `<page url>` tags whose UUID is NOT yet in the manifest (race/ordering edge case — shouldn't happen with depth-first), render with the `external` flag and the full Notion URL; the line will be promoted to a relative link on the next `/nsync:pull`.
   - Write the local `.md` file with the resulting markdown.
   - Write `.nsync/snapshots/<page_id>.md` with the same content.
   - Add a PageRecord with computed `local_hash`, `remote_hash`, `last_synced_at` (now), `has_children`, `local_ignored: false`. Both hashes are computed per the pipeline in `references/manifest-schema.md`, which strips child-link lines and `<page url>` tags — so they're identical regardless of whether the file contains rendered child-link lines or none. (No `last_seen_remote_modified` field in v1 — hashes drive classification.)
5. **Mirror the parent body if non-empty.** `fetch` the parent page itself. Apply the markdown normalization pipeline (rich-block tags + image-block lines stripped — but NOT child-link lines; those are rendered, not removed). Decide per `references/path-mapping.md` → "Non-empty parent body":
   - **Empty** (zero-length after strip, or only a single `# <Title>` line): skip — no `index.md`, no root PageRecord. Note this in the output summary as "Parent body empty — no root index.md created".
   - **Non-empty**: substitute each whole-line `<page url>` tag in the parent body with a child-link line (relative path from sync root; manifest has been populated by step 4). Write the result to `<sync-root>/index.md`. Write `.nsync/snapshots/<parent_page_id>.md` with the same content. Add the **root PageRecord** keyed by `config.parent.page_id` with `path: "index.md"`, `parent_page_id: null`, `has_children: true` (assuming the search in step 3 found any sub-page; else `false`), `local_hash` and `remote_hash` computed over the normalized content per the pipeline, `last_synced_at` (now), `local_ignored: false`, and any detected `rich_blocks`.
6. Persist `manifest.json` (sorted-key, two-space-indented).

## Output

Print a short summary:
- Parent page title and URL
- Whether the root `index.md` was created (and why not, if skipped — empty parent body)
- Count of pages mirrored (excluding the root entry to avoid double counting; or call them out separately)
- Count of rich blocks detected (preserved, not synced) — grouped by type
- Any pages that failed to fetch, with the underlying error per page

Suggest: "Run /nsync:status to verify the working tree is clean."
