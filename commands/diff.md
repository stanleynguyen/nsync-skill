---
description: Show local vs remote diff for tracked Notion pages (git diff analog)
argument-hint: "[path...]"
allowed-tools: Read, Glob, Bash(diff:*), Bash(pwd:*), Bash(python3:*), Task, mcp__claude_ai_Notion__notion-fetch, mcp__claude_ai_Notion__notion-search
---

Show unified diff between local `.md` files and their remote Notion pages.

Read these references first:
- @${CLAUDE_PLUGIN_ROOT}/references/manifest-schema.md
- @${CLAUDE_PLUGIN_ROOT}/references/notion-mcp-cheatsheet.md
- @${CLAUDE_PLUGIN_ROOT}/references/path-mapping.md

**Never hash, normalize, or diff in-context.** Use `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py"` (see `references/manifest-schema.md` → "Compute helper") and keep page bodies out of this command's context per `references/notion-mcp-cheatsheet.md` → "Sub-agent fan-out & context discipline".

Input: `$ARGUMENTS` contains zero or more positional path arguments. Each arg is either a `.md` file or a folder. Mixing is allowed. See "Argument resolution" below.

## Steps

1. Walk upward to locate `.nsync/`. Abort if missing.
2. Load manifest and ignore patterns. Compute the ignore matcher.
3. Capture the user's CWD (use `pwd` via Bash) so paths in `$ARGUMENTS` can be resolved relative to it.
4. Run rename detection in REPORT-ONLY mode — list any candidates but do not apply them or write to the manifest.
5. **Resolve `$ARGUMENTS` into a target set** (see "Argument resolution" below). If `$ARGUMENTS` is empty, target every tracked PageRecord plus any untracked `.md` discovered under the sync root.
6. Recompute `local_hash` for all targets in one shot via `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/nsync.py" hash-batch --mode local`, classify each, and skip Clean ones per step 7. For the non-clean targets, produce the rendered diffs **without holding remote bodies in context**, per `references/notion-mcp-cheatsheet.md` → "Sub-agent fan-out & context discipline": ≥8 targets, dispatch one sub-agent per `NSYNC_READ_BATCH` batch; each fetches its pages and returns the **rendered unified diff text** for its targets (built per step 8). <8 targets, run inline with process-and-discard. Honor the 429 backoff and report progress per "Progress reporting" (e.g. `Diffed 12/40 pages…`). For Added-only targets there is no remote; for Deleted-only targets there is no local — handle the asymmetric diff accordingly.
7. Skip pages whose state is Clean (no diff to show) UNLESS the user named the path explicitly as a file arg — in that case print a `<path>: no changes` line so the user knows the file was found.
8. The rendered unified diff for each non-clean target (built by the sub-agent, or inline for <8 targets):
   - LEFT (local) — the local `.md` file via `nsync.py normalize --mode local`. For Deleted targets, use the last snapshot from `.nsync/snapshots/<page_id>.md`.
   - RIGHT (remote) — the remote markdown-only body via `nsync.py normalize --mode remote`. Rich-block tags MUST be replaced with a single placeholder line per block, formatted exactly as `[rich block: <type>] (not synced)`. Use the manifest's `rich_blocks` entries to label types when possible; fall back to the tag name from the fetched body. For Added targets, the right side is empty (new file).
   - Generate the unified output with `nsync.py diff` (or `diff -u` via Bash) — never by hand.
9. Render targets in natural sort order of their sync-root-relative path (stable across invocations regardless of arg order).

## Argument resolution

Resolve each positional arg in `$ARGUMENTS`:

- **CWD-relative**: resolve against the user's CWD captured in Step 3, then normalize to sync-root-relative using forward slashes. If the resulting path escapes the sync root, warn `<arg>: outside sync root` and skip.
- **`.nsync/` rejection**: any path that lands inside `.nsync/` → reject with `<arg>: cannot diff plugin state` and skip.
- **File arg** (ends in `.md`, case-insensitive): match against PageRecords by `path` AND against `.md` files on disk. Include if either side exists (covers Modified, Remote-newer, Conflict, Added, Deleted). If neither exists, emit `<arg>: no matching tracked file` and skip. **Explicit file args override `.nsync/ignore`** — an ignored file named on the command line is still diffed.
- **Folder arg** (no `.md` suffix OR has trailing `/`): include every PageRecord whose `path` starts with `<arg>/` (sync-root-relative) PLUS every untracked `.md` on disk under that prefix. The folder itself does not need to be a Notion-mapped page; passing `engineering/` matches `engineering/index.md`, `engineering/standards.md`, and any descendants under `engineering/sub/`. If the folder yields zero matches, emit `<arg>: folder has no tracked .md files` and skip. Folder args **do not** override `.nsync/ignore` — to diff an ignored file, name it explicitly.
- **Deduplication**: a target picked up by multiple args (e.g., a folder and an explicit file arg both pointing to the same path) is rendered once.

## Output sections (skip empty ones)

- **Modified (local)** — local hash diverged from manifest, remote unchanged.
- **Remote-newer** — freshly-computed `remote_hash` differs from `manifest.remote_hash` (markdown-only content has changed upstream since last sync).
- **Conflict** — both sides changed since last sync.
- **Added (local)** — `.md` files with no PageRecord (will become new Notion pages on commit). Show the file content as a "new file" diff.
- **Deleted (local)** — PageRecords whose file is missing. Show the last snapshot as the deleted content.
- **Rename suggestions (informational)** — heuristic matches that would be confirmed by status/pull/commit but are not applied here.
- **Pending child-link placeholders** - for each `has_children` file in scope, list placeholder child-link lines (the placeholder regex in `references/path-mapping.md` → "Child-link lines") with their target path and whether each resolves. These become managed links on `/nsync:commit`; unresolvable ones are flagged. Read-only; nothing is rewritten here.
- **No changes (explicit args only)** — paths the user named that resolved Clean. Print each as `<path>: no changes`.

If `$ARGUMENTS` was non-empty, print a one-line summary of resolution warnings at the top (paths skipped + reason), so the user notices typos before scanning the diff.

No writes. Close with a one-liner suggesting `/nsync:commit` or `/nsync:pull` as appropriate.
