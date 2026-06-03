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

**All deterministic work runs through `nsync.py`.** Main-loop orchestration goes through the `status-scan` subcommand. Sub-agent per-page work goes through the `process-fetch` helper. Never write inline Python heredocs.

## Steps

1. Walk upward to locate `.nsync/`. Abort if missing: "Not a sync root. Run /nsync:init first."

2. **Fetch remote hashes via Workflow sub-agents** (skip `local_ignored: true`). For each tracked PageRecord, dispatch sub-agents in batches of `NSYNC_READ_BATCH = 4` (see `notion-mcp-cheatsheet.md` → "Concurrency"). Each sub-agent's per-page flow is **three tool calls**:
   1. `mcp__claude_ai_Notion__notion-fetch`
   2. Use the `Write` **tool** (not Bash) to save the response's `.text` field to `.nsync/tmp/<page_id>.fetch.txt`. The `.text` value is a raw string with real newlines; pass it directly as `content`. **Forbidden:** Bash `cat > file << 'EOF'` heredoc, `echo "$VAR" >`, or any shell-mediated write — they mangle escapes and produce stable-but-wrong hashes (see `references/sub-agent-body-escape-drift.md`).
   3. `Bash`: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" process-fetch .nsync/tmp/<page_id>.fetch.txt --page-id <id> --path <path> --has-children {true|false} --delete-fetch`
   Each sub-agent returns a `CompactReadRecord` (schema in `references/sub-agent-schemas.md`). Aggregate into `.nsync/tmp/fetch_results.json` shaped `{"records": [...]}`. Report progress per `notion-mcp-cheatsheet.md` → "Progress reporting".

3. **Root-backfill detection (read-only).** If `manifest.pages` lacks an entry for `config.parent.page_id`, the sub-agent fan-out above does not cover the parent. `notion-fetch` it inline, write to `.nsync/tmp/<parent_page_id>.fetch.txt`, and call:
   ```sh
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" process-fetch \
     .nsync/tmp/<parent_page_id>.fetch.txt \
     --page-id <parent_page_id> --has-children true \
     --out-body .nsync/tmp/<parent_page_id>.body.md
   ```
   The body file is what step 4 needs for reachability + the "Pending migration" check. Do NOT write to `<sync-root>/index.md` — status never writes outside `.nsync/tmp/`.

4. **Scan.** One Bash call:
   ```sh
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" status-scan \
     --sync-root <sync-root> \
     --records .nsync/tmp/fetch_results.json \
     --parent-fetch .nsync/tmp/<parent_page_id>.fetch.txt \
     > .nsync/tmp/status.json
   ```
   Output JSON has `{clean, modified_local, modified_remote, conflict, untracked_remote_hash, local_only_md_files, ignored_md_files, tracked_paths_missing, rename_candidates, remote_added, remote_trashed}`. Read it.

5. **Pending migration check.** If a root-backfill body was extracted (step 3) and the body is non-empty per `references/path-mapping.md` → "Non-empty parent body" (run the body through `python3 nsync.py normalize --mode remote` and compare `strip()`'d output to `""` or the title-heading line), surface it in the output.

## Output (skip empty sections)

- **Modified (local)** — `modified_local` page IDs (look up paths from manifest).
- **Added (local)** — `local_only_md_files`; show the destination Notion path they'd attach to.
- **Deleted (local)** — `tracked_paths_missing` (excluding `local_ignored`).
- **Renamed (suggested)** — `rename_candidates`. Unconfirmed; commit/pull will prompt.
- **Conflicts** — `conflict` (both sides changed).
- **Remote-newer** — `modified_remote`.
- **Remote-added** — `remote_added`.
- **Remote-trashed** — `remote_trashed`.
- **Ignored (was tracked)** — `ignored_md_files` that match tracked PageRecord paths.
- **Pending migration** — if step 5 detected a non-empty parent body with no root entry: `Root index.md will be created on next /nsync:pull (parent body has <N chars> of content).`
- **Child links to update** — for each tracked has_children page where `child_link_tags` from sub-agent records differs from the file's existing managed lines (compare in-context using the regex in `references/path-mapping.md` → "Child-link lines" — this is a one-line scan per file, not a heredoc).
- **Pending child-link placeholders** — for each has_children file, scan for placeholder child-link lines per `path-mapping.md`. List unresolved placeholders.

Close with a one-liner suggesting `/nsync:pull` and/or `/nsync:commit` based on findings, or "Working tree clean. All pages in sync." if every section is empty.

**Cleanup:** at command end, `rm /Users/stanley/Documents/Claude/Projects/Superfluid/.nsync/tmp/*` (or your sync-root equivalent) to remove the scratch files written by the sub-agents.
