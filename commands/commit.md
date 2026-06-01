---
description: Push local .md changes to Notion (git commit + push analog)
argument-hint: "[--force <path>...]"
allowed-tools: Read, Write, Edit, Glob, Bash, Task, Workflow, AskUserQuestion, mcp__claude_ai_Notion__notion-fetch, mcp__claude_ai_Notion__notion-update-page, mcp__claude_ai_Notion__notion-create-pages, mcp__claude_ai_Notion__notion-move-pages
---

Push local markdown changes to Notion. Workspace-wide staleness guard; `--force <path>` to override per-file.

Read these references first:
- @${CLAUDE_PLUGIN_ROOT}/references/manifest-schema.md
- @${CLAUDE_PLUGIN_ROOT}/references/path-mapping.md
- @${CLAUDE_PLUGIN_ROOT}/references/conflict-protocol.md
- @${CLAUDE_PLUGIN_ROOT}/references/notion-mcp-cheatsheet.md
- @${CLAUDE_PLUGIN_ROOT}/references/sub-agent-schemas.md

**All deterministic work runs through `nsync.py`.** Main-loop orchestration uses `commit-preflight` / `commit-validate-hunks` / `commit-apply`. Sub-agent per-page work uses `process-fetch`, `process-modified`, `process-postwrite`. Never write inline Python heredocs; never hash, normalize, diff, or extract UUIDs in-context.

Input: `$ARGUMENTS` may contain `--force <path>` (repeatable). Parse them out before the workspace-wide check.

## Steps

1. Walk upward to locate `.nsync/`. Abort if missing.

2. **Preflight.** One Bash call:
   ```sh
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" commit-preflight \
     --sync-root <sync-root> > .nsync/tmp/preflight.json
   ```
   Output JSON has `{modified, new, deleted, rename_candidates, stale_check_list}`. Read it.

3. **Apply rename confirmations.** One AskUserQuestion call with one question per rename candidate. Edit `.nsync/manifest.json` for each `[Y]es`. Skip `[N]o`. Re-run `commit-preflight` only if any rename was applied.

4. **Workspace-wide staleness check via Workflow sub-agents** (skip pages in `--force` set). For `preflight.stale_check_list`, dispatch sub-agents in batches of `NSYNC_READ_BATCH = 4`. Each sub-agent's per-page flow:
   1. `mcp__claude_ai_Notion__notion-fetch`
   2. `Write` the response `text` field to `.nsync/tmp/<page_id>.fetch.txt`
   3. `Bash`: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" process-fetch .nsync/tmp/<page_id>.fetch.txt --page-id <id> --path <path> --has-children {true|false} --out-body .nsync/tmp/<page_id>.body.md`
   (Note: keep the body file — the next phase needs it for hunk-build. Do NOT pass `--delete-fetch` here; the fetch envelope is deleted at end-of-command via the `--cleanup-tmp` flag in `commit-apply`.)

   Aggregate sub-agent records. For each Modified page, compare `record.remote_hash` vs `manifest.remote_hash`. Any page that lost the staleness check (and is not in `--force`) aborts the commit with: "Remote has changes you haven't pulled. Run /nsync:pull first. To override, pass --force <path>."

5. **Validate hunks (dry run).** One Bash call:
   ```sh
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" commit-validate-hunks \
     --sync-root <sync-root> \
     --modified-list <(cat preflight.modified) \
     --bodies-dir .nsync/tmp \
     > .nsync/tmp/hunks.json
   ```
   The orchestrator builds hunks + validates per the line-boundary rule in `notion-mcp-cheatsheet.md` → "Rich-block-safe update". Output JSON has `{pages: [{page_id, path, hunks: [{old_str, new_str, verdict}]}]}` where `verdict` is `ok` / `race_lost` / `ambiguous` / `rich_block_overlap`.

6. **Batched user prompts for non-ok hunks.** Collect every hunk where `verdict != "ok"` across all Modified pages. Build one multi-question `AskUserQuestion` call — one question per non-ok hunk, options:
   - For `ambiguous`: `[F]orce-replace anyway / [S]kip this hunk` (with the file path and hunk line range in the question text).
   - For `rich_block_overlap`: `[F]orce-replace (deletes rich block) / [S]kip this hunk`.
   - For `race_lost`: only `[S]kip` (the orchestrator already classified it as unrecoverable; force-replace would corrupt).
   Build `.nsync/tmp/hunk_decisions.json` shaped `{"hunks": [{page_id, hunk_index, choice}]}`.

   If a hunk's choice is `[S]kip`, mark that file's commit as refused — the file's other hunks still go through if they're `ok`, but the user is warned the file is partially committed. (Per `notion-mcp-cheatsheet.md` recovery rules: hunks within a file are submitted as a single `notion-update-page` call, so a per-hunk skip means dropping the hunk from the array, not refusing the whole file.)

7. **Push Modified pages via Workflow sub-agents.** For each Modified page, one sub-agent. Each sub-agent's flow:
   1. Read the validated hunks for this page from `.nsync/tmp/hunks.json` (filtered by user choices).
   2. `mcp__claude_ai_Notion__notion-update-page` with `command="update_content"`, `content_updates=<hunks>`.
   3. `mcp__claude_ai_Notion__notion-fetch` again (re-fetch).
   4. `Write` the new `text` field to `.nsync/tmp/<page_id>.postwrite.fetch.txt`.
   5. `Bash`: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" process-postwrite .nsync/tmp/<page_id>.postwrite.fetch.txt --page-id <id> --snapshot-out .nsync/snapshots/<id>.md --delete-fetch`
   Returns `{page_id, new_remote_hash}` (a `CommitWriteResult` variant).

8. **New pages** — Create in topological order. Group `preflight.new` by resolved parent_page_id. For each group, one `mcp__claude_ai_Notion__notion-create-pages` call with the shared `parent` + `pages[]` array (titles derived from H1 or slugged filename, bodies = local file content with placeholder child-link lines stripped via `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" strip-childlinks` — one Bash call per file). On success, collect new PageRecord data for `commit-apply`.

9. **Deleted pages** — Batched AskUserQuestion across all `preflight.deleted` entries. One question per page. For non-root entries: options `[O]rphan / [M]anual trash / [R]estore`. For the root entry (`parent_page_id is null`): options `[R]estore local / [E]mpty parent body`. Execute remote operations (`notion-move-pages` for `[O]`, `notion-update-page replace_content` for `[E]`) inline or via a small Workflow for parallelism.

10. **Apply.** One Bash call. Build `.nsync/tmp/write_results.json` from the sub-agent return values + new-page creation responses + deleted-page choices:
    ```json
    {
      "modified": [{"page_id": "...", "path": "...", "pushed": true, "new_remote_hash": "..."}, ...],
      "new": [{"page_id": "...", "path": "...", "parent_page_id": "...", "url": "...", "title": "...", "local_hash": "...", "remote_hash": "..."}, ...],
      "deleted": [{"page_id": "...", "choice": "O|M|R|E"}, ...]
    }
    ```
    Then:
    ```sh
    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" commit-apply \
      --sync-root <sync-root> \
      --write-results .nsync/tmp/write_results.json \
      --cleanup-tmp \
      > .nsync/tmp/apply_report.json
    ```
    `commit-apply` updates the manifest with new remote hashes, inserts new PageRecords, records TrashEntries for deleted pages, runs the **placeholder child-link backfill pass** (resolves placeholders into managed lines, rewrites local files + snapshots), persists the manifest atomically, verifies hashes, and cleans `.nsync/tmp/`.

11. Read `apply_report.json` and print the per-file result summary.

## Output

- **Modified pushed:** `modified_updated` list
- **New created:** `new_added` list
- **Deleted dispositions:** `deleted_trashed` list
- **Placeholders backfilled:** `backfilled` list
- **Errors:** `errors` (per-file failure reasons)
- **Verify mismatches:** `verify_mismatches` (should be empty; non-empty is a bug)

Note any `--force` bypasses with the hash prefixes that differed. Suggest `/nsync:status` to verify.

## Atomicity

`commit-apply` persists the manifest in one atomic rename at the end. Sub-agent failures (one Modified page refused) surface in `errors` but do not abort the batch — the rest commits cleanly. Re-running `/nsync:commit` retries only the still-Modified files.
