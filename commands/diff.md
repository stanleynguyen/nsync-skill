---
description: Show local vs remote diff for tracked Notion pages (git diff analog)
argument-hint: "[path...]"
allowed-tools: Read, Glob, Bash, Task, Workflow, mcp__claude_ai_Notion__notion-fetch
---

Show unified diff between local `.md` files and their remote Notion pages.

Read these references first:
- @${CLAUDE_PLUGIN_ROOT}/references/manifest-schema.md
- @${CLAUDE_PLUGIN_ROOT}/references/notion-mcp-cheatsheet.md
- @${CLAUDE_PLUGIN_ROOT}/references/path-mapping.md
- @${CLAUDE_PLUGIN_ROOT}/references/sub-agent-schemas.md

**All deterministic work runs through `nsync.py`.** Main-loop orchestration goes through the `diff-scan` subcommand. Sub-agent per-page work goes through the `process-fetch` helper (mode `read-normalize-snapshot` writes the normalized remote body to a body file the main loop can diff against the local `.md`). Never write inline Python heredocs.

Input: `$ARGUMENTS` contains zero or more positional path arguments. Each arg is either a `.md` file or a folder. Mixing is allowed.

## Steps

1. Walk upward to locate `.nsync/`. Abort if missing.

2. **Build the target plan.** One Bash call:
   ```sh
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" diff-scan \
     --sync-root <sync-root> \
     [--path <arg1> --path <arg2> ...] \
     > .nsync/tmp/diff_plan.json
   ```
   The `--path` flag accepts the user's positional args (CWD-relative paths resolved by the calling assistant before invocation, normalized to sync-root-relative). With no `--path`, `diff-scan` returns every tracked PageRecord plus an `on_disk` flag.

3. **Fetch remote bodies for non-clean targets via Workflow sub-agents.** Each sub-agent's per-page flow is **three tool calls**:
   1. `mcp__claude_ai_Notion__notion-fetch`
   2. Use the `Write` **tool** (not Bash) to save the response's `.text` field to `.nsync/tmp/<page_id>.fetch.txt`. The `.text` value is a raw string with real newlines; pass it directly as `content`. **Forbidden:** Bash `cat > file << 'EOF'` heredoc, `echo "$VAR" >`, or any shell-mediated write — they mangle escapes and produce stable-but-wrong hashes (see `references/sub-agent-body-escape-drift.md`).
   3. `Bash`: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" process-fetch .nsync/tmp/<page_id>.fetch.txt --page-id <id> --path <path> --has-children {true|false} --out-body .nsync/tmp/<page_id>.body.md --delete-fetch`

   The `--out-body` flag writes the extracted body for the main loop to diff against the local file. Sub-agents return `{page_id, remote_hash, child_link_tags}` — the main loop reads the bodies from disk, not from sub-agent return values, so per-page context stays clean.

4. **Render diffs.** For each target in the plan, run:
   ```sh
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" diff \
     .nsync/tmp/<page_id>.body.md \
     <sync-root>/<path>
   ```
   (Or, equivalently, diff the snapshot vs local for the Modified-local case — `diff-scan`'s plan tells you which path each target needs.)

   For has_children pages, the body has `<page url>` tags; `nsync.py diff` strips child-link lines automatically via its built-in pre-processing.

5. **Rich-block placeholders.** For each diff, replace any stripped rich-block tag span with a single placeholder line `[rich block: <type>] (not synced)` per the existing convention. Use the manifest's `rich_blocks` entries when available. (`process-fetch` writes the raw body so rich-block tags are still visible to the diff renderer; in the future, a `diff-scan --render` mode could absorb this step into the orchestrator.)

6. Render targets in natural sort order of their sync-root-relative path.

## Output sections (skip empty ones)

- **Modified (local)** — local hash diverged from manifest, remote unchanged.
- **Remote-newer** — freshly-computed `remote_hash` differs from `manifest.remote_hash`.
- **Conflict** — both sides changed since last sync.
- **Added (local)** — `.md` files with no PageRecord. Show file content as a "new file" diff.
- **Deleted (local)** — PageRecords whose file is missing. Show snapshot as deleted content.
- **Rename suggestions (informational)** — heuristic matches.
- **Pending child-link placeholders** — for each has_children file in scope, list placeholder child-link lines (regex in `references/path-mapping.md` → "Child-link lines") with target path and resolution status.
- **No changes (explicit args only)** — paths the user named that resolved Clean. Print each as `<path>: no changes`.

If `$ARGUMENTS` was non-empty, print a one-line summary of resolution warnings at the top.

**Cleanup:** at command end, `rm /Users/stanley/Documents/Claude/Projects/Superfluid/.nsync/tmp/*` (or sync-root equivalent).

No writes. Close with a one-liner suggesting `/nsync:commit` or `/nsync:pull` as appropriate.
