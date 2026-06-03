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

**All deterministic work runs through `nsync.py`.** Main-loop orchestration goes through the `pull-preflight` / `pull-classify` / `pull-apply` subcommands. Sub-agent per-page work goes through the `process-fetch` helper. Never write inline Python heredocs in this command; never hash, normalize, diff, or extract UUIDs directly in-context. The single allow-rule `Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py:*)` covers every Bash call this command makes.

## Steps

1. **Locate `.nsync/`.** Walk upward from the working directory. If absent, abort: "Not a sync root. Run /nsync:init first."

2. **Preflight.** One Bash call:
   ```sh
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" pull-preflight \
     --sync-root <sync-root> > .nsync/tmp/preflight.json
   ```
   This loads the manifest + ignore patterns, globs `**/*.md`, runs rename detection (skipping the root entry per `path-mapping.md` → "Root-page constraints"), hashes every tracked file, and builds the fetch list. Read the resulting JSON via the Read tool — it has fields `{schema_version, sync_root, parent, root_backfill_needed, rename_candidates, fetch_list, local_hashes, local_only_md_files, ignored_md_files, tracked_paths_missing}`.

3. **Apply rename confirmations.** For each entry in `rename_candidates`, AskUserQuestion `[Y]es / [N]o`. On `Y`, update the PageRecord's `path` (edit `manifest.json` via the Read+Write tools, or run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py pull-preflight ...` again after the manual edit). On `N`, the candidate becomes `Deleted (local)` + `Added (local)` per `path-mapping.md`. Re-run `pull-preflight` only if any rename was applied (regenerates `fetch_list` + `local_hashes` against the new paths).

4. **Root backfill** (only if `preflight.root_backfill_needed` is true). `notion-fetch` the parent page; write the full `text` field to `.nsync/tmp/<parent_page_id>.fetch.txt` via the Write tool. Run via Bash:
   ```sh
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" process-fetch \
     .nsync/tmp/<parent_page_id>.fetch.txt \
     --page-id <parent_page_id> --path index.md --has-children true \
     --out-body .nsync/tmp/<parent_page_id>.body.md
   ```
   If the extracted body is empty per `path-mapping.md` → "Non-empty parent body", skip silently. Otherwise create the root PageRecord (the substitution + write happens in step 7 via `pull-apply`, so just add the manifest entry here with placeholder hashes — `pull-apply` will overwrite them).

5. **Fetch every tracked page's remote_hash via Workflow sub-agents** (skip `local_ignored: true`). Build `preflight.fetch_list`-derived batches of size `NSYNC_READ_BATCH = 4` (see `notion-mcp-cheatsheet.md` → "Concurrency"). Each sub-agent's per-page flow is **three tool calls**:
   1. `mcp__claude_ai_Notion__notion-fetch`
   2. Use the `Write` **tool** (not Bash) to save the response's `.text` field to `.nsync/tmp/<page_id>.fetch.txt`. The `.text` value is a raw string with real newlines; pass it directly as `content`. **Forbidden:** Bash `cat > file << 'EOF'` heredoc, `echo "$VAR" >`, or any shell-mediated write — they mangle escapes and produce stable-but-wrong hashes (see `references/sub-agent-body-escape-drift.md`).
   3. `Bash`: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" process-fetch .nsync/tmp/<page_id>.fetch.txt --page-id <id> --path <path> --has-children {true|false} --delete-fetch`
   Each sub-agent returns a structured object validated against the `CompactReadRecord` schema in `references/sub-agent-schemas.md`. Aggregate all returned records into one JSON file (`.nsync/tmp/fetch_results.json`) shaped `{"records": [...]}`. Report progress per `notion-mcp-cheatsheet.md` → "Progress reporting".

6. **Classify.** One Bash call:
   ```sh
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" pull-classify \
     --preflight .nsync/tmp/preflight.json \
     --records .nsync/tmp/fetch_results.json \
     --parent-fetch .nsync/tmp/<parent_page_id>.fetch.txt \
     > .nsync/tmp/classify.json
   ```
   Output JSON has `{clean, auto_merge_remote, auto_merge_local, conflict, remote_added, remote_trashed, refetch_list, child_link_regen_list}`. Read it. The `refetch_list` is exactly the set of pages whose body the next phase needs (Auto-merge-remote ∪ Conflict pages).

7. **Conflict prompts (one batched AskUserQuestion).** If `classify.conflict` is non-empty, ask the user with one multi-question `AskUserQuestion` call — one question per conflicting file, options `[L]ocal / [R]emote / [E]dit / [S]kip`, per `conflict-protocol.md`. Build `.nsync/tmp/decisions.json` shaped `{"conflicts": [{page_id, choice, ...}]}`. For `[E]dit` choices, write the scratch buffer to `.nsync/conflicts/<page_id>.scratch.md` (per `conflict-protocol.md` → "Scratch buffer format"), wait for the user to edit, then confirm; once confirmed, copy the MERGED section over the real file before invoking `pull-apply`.

8. **Refetch bodies for `classify.refetch_list`.** Same sub-agent pattern as step 5, but with one tweak: pass `--out-body .nsync/tmp/<page_id>.body.md` and `--delete-fetch` so each sub-agent's `process-fetch` leaves a body file behind for `pull-apply` to consume. Sub-agents return `PageOk` records (just `{page_id, ok}`) — main loop only needs to know all bodies landed.

9. **Apply.** One Bash call:
   ```sh
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" pull-apply \
     --classify .nsync/tmp/classify.json \
     --bodies-dir .nsync/tmp \
     --decisions .nsync/tmp/decisions.json \
     --cleanup-tmp \
     > .nsync/tmp/apply_report.json
   ```
   `pull-apply` substitutes `<page url>` → child-link lines for has_children pages, normalizes, writes local files + snapshots, applies conflict resolutions per the snapshot-update table in `conflict-protocol.md`, regenerates child-link lines on Clean has_children pages, refreshes `last_synced_at`, persists the manifest atomically (`.tmp` → fsync → rename), verifies every local file's hash matches the manifest, and cleans up `.nsync/tmp/`.

10. **Read the apply report and print the summary.** The JSON contains `{overwritten, overwrite_errors, conflict_applied, regen_changes, clean_refreshed, verify_mismatches, tmp_files_cleaned}`.

## Output

Print one section per relevant bucket from the apply report:
- **Auto-merged (remote-only changes pulled):** `overwritten` file list
- **New local files created from remote-added pages:** from the second-pass body mirror, if any
- **Child-link lines updated:** `regen_changes` per `index.md` with `added` count
- **Conflicts resolved:** `conflict_applied` (page + chosen resolution)
- **Conflicts skipped:** entries in `classify.conflict` not present in `conflict_applied`
- **Remote-trashed handled:** from the trash flow on `classify.remote_trashed`

Suggest next step (`/nsync:commit` if local edits exist, `/nsync:status` otherwise).

## Atomicity

`pull-apply` persists the manifest in one atomic rename at the end of its run. If interrupted mid-orchestration, the manifest reflects only the state at the previous successful persist (the prior pull's terminal state). Re-running `/nsync:pull` resumes cleanly. Sub-agent body files in `.nsync/tmp/` are scratch — `pull-apply --cleanup-tmp` removes them; a crash leaves them harmless for next run's `process-fetch` to overwrite.
