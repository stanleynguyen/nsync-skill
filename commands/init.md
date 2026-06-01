---
description: Initialize a Notion sync root in the current folder
argument-hint: "[notion-parent-url?]"
allowed-tools: Read, Write, Glob, Bash, Task, Workflow, AskUserQuestion, mcp__claude_ai_Notion__notion-fetch, mcp__claude_ai_Notion__notion-search, mcp__claude_ai_Notion__notion-create-pages
---

Initialize the current working directory as an nsync sync root mirroring a Notion parent page tree.

Read these references first:
- @${CLAUDE_PLUGIN_ROOT}/references/path-mapping.md
- @${CLAUDE_PLUGIN_ROOT}/references/manifest-schema.md
- @${CLAUDE_PLUGIN_ROOT}/references/notion-mcp-cheatsheet.md
- @${CLAUDE_PLUGIN_ROOT}/references/sub-agent-schemas.md

**All deterministic work runs through `nsync.py`.** Sub-agent per-page work uses `process-fetch`. The frontier-expansion loop uses `enumerate-tree`. Never write inline Python heredocs.

Input: `$1` may contain a Notion parent URL. If absent, ask the user for it via AskUserQuestion.

## Preflight (run in order, fail fast)

1. Walk upward from CWD for an existing `.nsync/`. If found, abort with: "A sync root already exists at `<path>`. Run /nsync:status there, or pick a different directory."
2. List CWD. Anything other than `.git/` and `.gitignore` counts as existing content. If non-empty, AskUserQuestion with three options:
   - "Initialize anyway and ignore existing files"
   - "Clear and initialize (DELETES every file except .git/)"
   - "Abort" (default)
   On "Clear", list every file/dir that would be removed and require a second confirmation. Never touch `.git/`.
3. Parse the URL. Accept `notion.so` URLs with a 32-hex page id, `*.notion.site` URLs same shape, or raw UUID (dashed/undashed). Reject `?v=`, `/collection`, database-shaped targets.
4. Smoke-test connectivity: `mcp__claude_ai_Notion__notion-search` with `{query: "_", page_size: 1}`. Map error responses per `notion-mcp-cheatsheet.md` "Init preflight error mapping" table.
5. `mcp__claude_ai_Notion__notion-fetch` the parsed UUID and map per the same table (404 / 403 / `<data-source>`).

## Initialize

Once preflight passes:

1. Capture parent metadata (UUID, title, URL).
2. Create `.nsync/`:
   - `config.json` — `{schema_version: 1, plugin_version: "0.3.0", parent: {page_id, url, title}, created_at: <RFC3339>}`
   - `manifest.json` — initial shell `{schema_version, plugin_version, parent, pages: {}, trash_log: []}`. Sorted-key, two-space-indented.
   - `ignore` — copy "Default ignore patterns" block from `path-mapping.md` verbatim.
   - `snapshots/`, `tmp/` — empty directories.

3. **Enumerate the sub-tree, round-by-round.** Save the parent fetch to `.nsync/tmp/<parent_id>.fetch.txt` (you already have its body cached from preflight step 5; write the response `text` field verbatim). Then loop:
   ```sh
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" enumerate-tree \
     --fetches-dir .nsync/tmp \
     > .nsync/tmp/frontier.json
   ```
   Read `frontier.json`. If `done: true`, the tree is fully enumerated. Otherwise, dispatch a Workflow batch to fetch every UUID in `next_round`: each sub-agent does **one tool call** (notion-fetch) per page and one Write to `.nsync/tmp/<page_id>.fetch.txt`. After the batch lands, re-run `enumerate-tree` — it now sees more cached fetches and emits the next frontier. Report progress per `notion-mcp-cheatsheet.md` → "Progress reporting" each round (e.g. `Enumerating sub-tree… (87 pages so far)`). Hard cap at 500 pages.

4. **Compute the path map.** Read each cached fetch envelope inline (via Read tool), extract title from the `<properties>` block and parent UUID from `<ancestor-path>`, then compute the full UUID→path map per `path-mapping.md` (slug + collision suffix + `index.md` for has_children). Path computation uses only titles + parentage — no body re-fetch needed. Persist the manifest with placeholder PageRecords (path, parent_page_id, title, url, has_children, empty hashes).

5. **Mirror page bodies via Workflow sub-agents.** For each cached fetch, dispatch sub-agents in batches of `NSYNC_READ_BATCH = 4`. Each sub-agent's per-page flow is **one Bash call**:
   ```sh
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" process-fetch \
     .nsync/tmp/<page_id>.fetch.txt \
     --page-id <id> --path <path> --has-children {true|false} \
     --out-body .nsync/tmp/<page_id>.body.md \
     --out-snapshot .nsync/snapshots/<page_id>.md \
     --delete-fetch
   ```
   Sub-agent returns `{page_id, remote_hash, child_link_tags}`.

6. **Apply via `pull-apply`.** The init flow's post-fetch work — substituting `<page url>` → child-link lines for has_children pages, writing local files, computing local_hashes — is identical to `pull-apply`'s remote-only overwrite branch. Build a synthetic classify JSON that lists every fetched page as `auto_merge_remote`:
   ```json
   {
     "sync_root": "<sync-root>",
     "clean": [],
     "auto_merge_remote": [...all pids...],
     "auto_merge_local": [],
     "conflict": [],
     "remote_added": [],
     "remote_trashed": [],
     "refetch_list": [{"page_id": "...", "path": "...", "has_children": ..., "reason": "auto_merge_remote"}, ...],
     "child_link_regen_list": []
   }
   ```
   Then:
   ```sh
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" pull-apply \
     --classify .nsync/tmp/classify.json \
     --bodies-dir .nsync/tmp \
     --cleanup-tmp \
     > .nsync/tmp/apply_report.json
   ```

7. **Root body** — handle the parent's own body. Run `process-fetch` on `.nsync/tmp/<parent_id>.fetch.txt` with `--out-body .nsync/tmp/<parent_id>.body.md`. If the body is non-empty per `path-mapping.md` → "Non-empty parent body" (check by running `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" normalize --mode remote .nsync/tmp/<parent_id>.body.md` and inspecting), include the parent in the classify JSON as a root entry (`page_id: <parent_id>`, `path: "index.md"`, `has_children: true|false`). `pull-apply` writes `index.md` + the root snapshot + the root PageRecord.

## Output

Short summary:
- Parent page title + URL
- Root `index.md` created? (or skipped because parent body was empty)
- Page count mirrored
- Rich-block count by type
- Any pages that failed (page_id + error)

Suggest: "Run /nsync:status to verify the working tree is clean."
