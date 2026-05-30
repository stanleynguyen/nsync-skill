# nsync — repo guide for Claude

This repo IS a Claude Code plugin. It is not application code. There is no build step, no test runner, no compiled output. The "code" is a set of Markdown command prompts plus reference documents that those prompts read at runtime.

## What this plugin does

`nsync` gives a local directory a git-style relationship with a Notion parent page tree. Slash commands `/nsync:init`, `/nsync:pull`, `/nsync:diff`, `/nsync:commit`, `/nsync:status` use the **built-in Claude Notion connector** to read and write pages while a `.nsync/` state directory (created at sync-root install time) tracks per-page hashes and snapshots in the user's working directory.

Read `README.md` if you want the end-user view. The rest of this file is for working ON the plugin.

## Repo layout

```
.
├── .claude-plugin/
│   ├── plugin.json          # plugin manifest — NO mcpServers (connector-only)
│   └── marketplace.json     # self-marketplace so the dir is install-able
├── commands/                # slash-command prompts (see "Files are prompts" below)
│   ├── init.md
│   ├── pull.md
│   ├── diff.md
│   ├── commit.md
│   └── status.md
├── references/              # canonical specs — commands @-include these
│   ├── manifest-schema.md
│   ├── path-mapping.md
│   ├── conflict-protocol.md
│   └── notion-mcp-cheatsheet.md
├── CLAUDE.md                # this file
└── README.md                # user-facing install + verification
```

## Files are prompts, not user docs

Every `commands/*.md` is a prompt addressed to Claude. When a user invokes `/nsync:init`, the file's body becomes Claude's instructions. So:

- Write in the imperative ("Walk upward to locate `.nsync/`..."), not the descriptive ("This command walks upward...").
- The YAML frontmatter (`description`, `argument-hint`, `allowed-tools`) is real config — don't add fields Claude Code doesn't understand.
- The `allowed-tools` list is enforced — if you add a new MCP call to a command body, also add it to `allowed-tools` or the call will be rejected.
- `references/*.md` files are included via `@${CLAUDE_PLUGIN_ROOT}/references/<name>.md` from command bodies. They are read once per invocation, so keep them tight and self-consistent.

**When you change runtime behavior, change the reference first, then verify the commands link to it.** The references are the spec; the commands are the choreography.

## Load-bearing invariants (do not violate)

These were earned through end-to-end dry-runs against a real Notion workspace. Each one prevents a class of bug.

1. **`.md`-only scope.** Anything else is invisible. `.nsync/ignore` filters specific `.md` files only (e.g., `README.md`), never generic clutter — that's already filtered by extension. See `references/path-mapping.md` rule 0.
2. **Hash-only classification.** The `Clean / Auto-mergeable / Conflict` table in `references/conflict-protocol.md` compares `local_hash` and `remote_hash`. No timestamps participate. `last_seen_remote_modified` is NOT in v1's `PageRecord`. If you find yourself wanting to add a `modifiedTime` check, you don't — the hash compare already handles every case (rich-block edits don't move `remote_hash` because rich-block tags are stripped before hashing).
3. **Snapshot ← remote on `[E]dit` conflict resolve.** This is the entry in `references/conflict-protocol.md` → "Snapshot update after resolution" that took the longest to get right. Without it, the next commit's snapshot→local diff produces `old_str` values that don't exist in `remote_raw`, refuses to push, and forces a re-pull. The `[L]ocal` row has a similar special case ("diff remote→local for the next commit's hunks"). Read that table carefully before touching pull or commit logic.
4. **Line-bounded `old_str` for `update_content`.** `references/notion-mcp-cheatsheet.md` → "Rich-block-safe update" enumerates four constraints — line-bounded, whole-line, surrounding context, unique. A short fragment like `"Read the README."` can substring-match into `"Read the README and the CONTRIBUTING guide."` and corrupt the page. Always validate the surrounding-byte check.
5. **No `<<<<<<<` / `=======` / `>>>>>>>` markers in tracked `.md` files.** Conflict UX is interactive (`[L]ocal / [R]emote / [E]dit-in-scratch / [S]kip`). A skipped marker would push to Notion verbatim and round-trip back as content on the next pull.
6. **Image blocks use markdown syntax, not XML.** Notion's enhanced-markdown serializes `<image>` blocks as `![Caption](URL)` lines, not `<image>` tags. The strip rule lives in `references/notion-mcp-cheatsheet.md` → "Image-block lines" and is regex-driven (`^!\[[^\]]*\]\([^)]+\)(\s*\{[^}]*\})?$`). Inline `![]()` inside running text is preserved.
7. **No MCP supports trashing a page.** Local-delete + commit prompts the user with `[O]rphan to workspace / [M]anual trash / [R]estore local`. `notion-update-data-source` with `in_trash: true` is database-only. Don't try to add a "delete page" shortcut — there isn't one.
8. **Decoupled rename in v1.** Local filename change updates the PageRecord's `path` but does not rename the Notion page title (a future `/nsync:mv` would). If the directory portion of the path changed, queue a `notion-move-pages` to the new parent.
9. **Placeholder child-link backfill keeps the snapshot in sync.** Placeholder child-link lines (`<!-- nsync:child -->`, no id, see `references/path-mapping.md` → "Placeholder child-link lines") are stripped from `local_hash` just like managed lines, and `/nsync:commit`'s backfill pass MUST overwrite the page's snapshot after rewriting them, exactly as pull-regeneration does (invariant #3), or the snapshot diverges from disk. "Pending placeholder" is always derived at runtime; never add a PageRecord field for it (keeps classification hash-only, invariant #2).

## Notion MCP — connector-only

This plugin uses the **Claude built-in Notion connector** exclusively. Tool namespace: `mcp__claude_ai_Notion__*`. Specifically:

- `mcp__claude_ai_Notion__notion-fetch`
- `mcp__claude_ai_Notion__notion-search`
- `mcp__claude_ai_Notion__notion-create-pages`
- `mcp__claude_ai_Notion__notion-update-page` (with `command` set to `update_content` / `replace_content` / `update_properties`)
- `mcp__claude_ai_Notion__notion-move-pages`

`plugin.json` deliberately has **no `mcpServers` block**. The user installs the connector once via Claude Code's Connectors UI; the plugin assumes it is present and surfaces a clear setup-error if not (see `commands/init.md` preflight #4).

**Do not** add `mcp__notion__*` (the `@notionhq/notion-mcp-server` namespace) to `allowed-tools`. That server exposes a different tool surface (REST-style `API-*` names, block-tree JSON instead of enhanced markdown). Supporting it would require a parallel implementation, not just a config tweak. See plan `/Users/stanley/.claude/plans/i-want-to-create-cosmic-frog.md` §M for the rationale.

## Where to look when you need to change something

| If you're changing... | Edit these |
|---|---|
| Manifest schema (PageRecord fields, TrashEntry, hash pipeline) | `references/manifest-schema.md` first, then the JSON examples that consume it |
| Conflict UX or three-state classifier | `references/conflict-protocol.md` first, then `commands/pull.md` and `commands/commit.md` |
| Path layout or rename heuristic | `references/path-mapping.md` first, then `commands/{init,pull,commit,status}.md` |
| Child-link / placeholder line behavior | `references/path-mapping.md` → "Child-link lines" first, then `commands/{commit,pull,status,diff}.md` and the hash pipeline in `manifest-schema.md` + `notion-mcp-cheatsheet.md` |
| Which MCP tool a command calls | `references/notion-mcp-cheatsheet.md` first, then the relevant command's `allowed-tools` |
| User-visible install or behavior | `README.md` |

The plan file at `/Users/stanley/.claude/plans/i-want-to-create-cosmic-frog.md` carries the full design history — sections A–J for the initial design, §K for the `/nsync:diff` arg grammar, §L for the post-dry-run fixes (L1 hash-only, L2 image-strip, L3 conflict snapshot, L4 verification order, L5 line-bounded `old_str`, L6 autolink doc), and §M for the connector-only migration. Read it before making non-trivial design changes.

## Verifying changes

There is no test suite. Validation is:

1. **Plugin validates.**

   ```bash
   claude plugin validate /Users/stanley/Documents/Projects/nsync-skill/
   ```

2. **Grep for known anti-patterns.** All four should return zero matches:

   ```bash
   grep -rn "NOTION_TOKEN" --include="*.md" --include="*.json" . | grep -v "no.*NOTION_TOKEN"
   grep -rn "@notionhq"   --include="*.md" --include="*.json" .
   grep -rn "mcp__notion__" --include="*.md" --include="*.json" .
   grep -rn "last_seen_remote_modified" --include="*.md" --include="*.json" . | grep -v "There is intentionally no\|cache only; never authoritative"
   ```

3. **End-to-end dry-run.** Follow the 10-step verification checklist in `README.md` against a throwaway Notion parent page. The previous two passes (logged in plan §J and §L verification notes) caught all the L-series bugs — a fresh dry-run should now complete without any manual snapshot tweaks or substring-match collisions.

## Style

- Markdown only. No code generation; no scripts.
- Keep references self-consistent with each other — if you update a hash field in `manifest-schema.md`, audit `conflict-protocol.md` and `notion-mcp-cheatsheet.md` for the same field name.
- Imperative voice in command bodies. Descriptive voice in references and README.
- When you introduce a new design decision, append a section to the plan file (§N, §O, ...) rather than retroactively rewriting old sections — the history is load-bearing for understanding why the current design looks the way it does.
