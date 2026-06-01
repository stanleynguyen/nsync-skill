# Notion MCP cheatsheet

## Tool surface

This plugin uses **Claude's built-in Notion connector exclusively**. Tools are surfaced under the `mcp__claude_ai_Notion__*` namespace once the user has connected Notion via Claude Code's (or Claude Cowork's) Connectors UI. No bundled MCP server, no `NOTION_TOKEN` env var, no per-page integration sharing — the OAuth connector handles all of that.

| Operation | Tool |
|---|---|
| Fetch page by URL/UUID | `mcp__claude_ai_Notion__notion-fetch` |
| Search pages (parent-filtered, paginated) | `mcp__claude_ai_Notion__notion-search` with `page_url` set to the parent |
| Create child pages with markdown body | `mcp__claude_ai_Notion__notion-create-pages` |
| Update page content (snippet replace) | `mcp__claude_ai_Notion__notion-update-page` with `command: "update_content"` |
| Replace page content wholesale (fallback) | `mcp__claude_ai_Notion__notion-update-page` with `command: "replace_content"` |
| Update page properties (title, etc.) | `mcp__claude_ai_Notion__notion-update-page` with `command: "update_properties"` |
| Move page to a new parent (incl. workspace-orphan) | `mcp__claude_ai_Notion__notion-move-pages` |

If a future version needs to run against a self-hosted or bundled Notion MCP, the snippet-based `update_content` and markdown-aware `notion-fetch` surface assumed throughout this document must be replicated — otherwise §G's rich-block-safe update strategy degrades.

## Concurrency, batching & rate limits

Commands are slow when per-page connector round-trips run one-at-a-time. They don't have to. This section is the single source of truth for how nsync schedules Notion calls; commands reference it instead of restating numbers.

**`NSYNC_READ_BATCH = 4`** — the canonical read-concurrency constant. It is defined HERE and nowhere else. Commands say "in batches of `NSYNC_READ_BATCH` (see notion-mcp-cheatsheet.md → Concurrency)"; they never restate the number.

### Rate ceiling

The built-in connector proxies to the Notion API, which allows an **average of ~3 requests/second per connection** (2700 requests / 15 minutes). Short bursts above the average are tolerated. Over-limit calls return **HTTP 429 with a `Retry-After` header** (integer seconds). `NSYNC_READ_BATCH = 4` is deliberately conservative — it sits just above the sustained average to exploit burst allowance without churning on 429s.

### Reads parallelize, writes serialize

In Claude Code, read-only tool calls emitted **in a single assistant message** run concurrently; write tools are serialized. nsync follows that split:

- **Parallel reads.** `notion-fetch` has no batch parameter (single `id` per call; "make multiple calls to fetch multiple entities"). To parallelize a per-page body-fetch loop, **emit up to `NSYNC_READ_BATCH` `notion-fetch` calls in one assistant message, await the whole batch, then emit the next batch.** Process each result (compute `remote_hash`, classify) as the batch lands. This applies to every "for each tracked page: `fetch`" loop in pull / status / diff / commit-staleness, and to init's per-page body fetches.
- **`notion-search` is NOT a tree-listing API.** It is a semantic search with an undocumented relevance ceiling — pagination does NOT make it exhaustive. On a 96-page tree with `page_url=<parent>` and `query="."`, full pagination returned 15 pages, not 96. Never rely on `search` to enumerate "what pages exist under a parent." Derive reachability instead from the parent fetch's `<page url>` children unioned with every `has_children: true` page's freshly-fetched `child_link_tags` (the data is already in flight for the per-page hash step). `notion-search` remains valid for genuinely semantic lookups (e.g., finding a candidate page by user-supplied query text), but no command in v1 uses it for tree enumeration.
- **Serial writes.** `notion-update-page` and `notion-move-pages` stay one call per page. Do NOT batch them into a single assistant message expecting concurrency — Claude Code serializes writes and Notion would 429. The write-side optimization is hunk-batching (all hunks for one page in a single `content_updates[]` array), which is unchanged.

### Batched creates (the one write that DOES batch)

`notion-create-pages` accepts a `pages[]` array (max 100) where **all pages share one parent**. When creating multiple New pages:

1. Group New files by their resolved parent `page_id`.
2. Submit each group as **one `notion-create-pages` call** with every sibling in `pages[]`.
3. Preserve topological order **across groups** — a parent's group must land (and yield `page_id`s) before any group whose parent is one of those just-created pages. Within a group, order `pages[]` so an `index.md` precedes siblings that resolve to it.

This collapses N create-calls into (number of distinct parents) calls.

### 429 backoff

On a 429: read `Retry-After`, wait that many seconds, retry only the failed call. If 429s recur within the same command, drop the effective batch size to 2 for the remainder of the run. Never let a 429 corrupt state — a failed fetch just re-runs; a failed write leaves the manifest unpersisted for that page (atomicity rules already cover this).

### Progress reporting (never go silent)

Long operations must surface progress so the user is never left wondering whether the command hung. The rule: **emit a progress line at least once per minute, and never run silently for more than ~60 seconds.** In practice, report at the natural checkpoints — they fire well under a minute on any real tree:

- **Batched read loops** (the per-page `notion-fetch` loops in pull / status / diff / init / commit-staleness): print one line **after each batch lands**, e.g. `Fetched 12/40 pages…`. Include the running count and the total.
- **Write loops** (commit's New-page create groups, Modified-page updates, Deleted/Renamed moves): print a line **per group / per page** as each Notion write succeeds, e.g. `Created 3 pages under engineering/ (8/15 new pages)…` or `Pushed roadmap.md (4/9 modified)…`.
- **Search pagination**: if enumerating a large tree takes more than a page or two, print `Enumerating sub-tree… (87 pages so far)` every few pages.
- **Backoff waits**: when honoring a 429 `Retry-After`, say so — `Rate-limited by Notion; waiting 5s before retrying…` — so the pause looks intentional, not hung.

Keep each line short and single-line (a running counter the user can watch tick up), not a paragraph. The final per-command output summary is unchanged; these are interim heartbeats during the work.

### Sub-agent fan-out & context discipline

The other half of the slowness was the main command holding every fetched page body in context — and re-processing all of it on every subsequent tool-call turn (cost scales with context size). The fix: **page bodies must never accumulate in the main command's context.** Deterministic work runs in `scripts/nsync.py` (see `manifest-schema.md` → "Compute helper"); the bulky read-and-hash work runs in sub-agents that return only compact records.

**Return shapes are schema-validated.** Three distinct sub-agent return types are in flight across the plugin — `CompactReadRecord` (status / pull / init / diff read paths), `CommitWriteResult` (commit Modified writes), and `DiffTextRecord` (diff render). Each is specified as a JSON Schema in `references/sub-agent-schemas.md`. Commands reference the schema by name; sub-agents return data through it; the dispatcher validates at the tool-call layer. Prose-only field lists (the v0 contract) were retired after a 24-agent run produced five different return shapes — see plan history in `i-want-to-create-cosmic-frog.md`.

**Dispatch tool.** For the ≥8-pages branch, sub-agents MUST be dispatched via the `Workflow` tool's `agent(prompt, { schema, label, phase, agentType })` form — the schema arg is what forces structured output. Plain `Agent`-tool calls are allowed only for the <8 inline branch (where the main loop owns the fetches and there is no schema to enforce). For read fan-outs set `agentType: 'Explore'`; for commit Modified writes omit `agentType` (default workflow agent — needs `notion-update-page` permissions).

**Fan-out threshold.** Sub-agents have spawn overhead, so scale to the work:

- **< 8 pages**: run inline (no sub-agents). **Process-and-discard**: fetch a batch, write each response's full `text` field verbatim to `.nsync/tmp/<page_id>.fetch.txt`, run it through the extraction pipeline (below) to produce `.nsync/tmp/<page_id>.remote.md`, hash the body with `nsync.py hash --mode remote`, record the compact result, delete both temp files, and do NOT carry the body forward in later reasoning.
- **≥ 8 pages**: dispatch via `Workflow` — one sub-agent per `NSYNC_READ_BATCH`-sized batch for reads (sub-agent returns `BatchReadRecords`), one per page for commit Modified writes (sub-agent returns `CommitWriteResult`). Bounded by the workflow concurrency cap. Each sub-agent fetches, runs the extraction pipeline below, runs `nsync.py`, returns the schema-validated record, and its context (with the bodies) is discarded on return.

Either path keeps the main loop holding only compact records, the manifest, and small local files — never tens of full page bodies. `.nsync/tmp/` is scratch space; clean it up at command end.

**Mandatory `extract-body` gate (the canonical fetch→hash pipeline).** `notion-fetch` returns its markdown body wrapped inside an envelope (`<page url>`, `<ancestor-path>`, `<properties>`, `<content>…</content>`, `</page>`). Feeding the envelope directly to `hash --mode remote` or `normalize --mode remote` silently strips the body — the unknown-tag fallback removes the entire `<content>…</content>` span, and every page hashes to the preamble line. This caused mass false "stale" reports in plugin_version 0.1.x. The fix is: **always run `nsync.py extract-body` first.** Sub-agent prompts MUST instruct exactly this two-stage pipeline — never let the sub-agent invent its own body-extraction logic, because empirically different sub-agents will trim, dedent, drop the preamble, or include the wrapper in subtly different ways, producing five different hashes for the same page.

The canonical commands (for both inline `<8` and Workflow `≥8` paths):

```sh
# 1. write the full notion-fetch text field verbatim — DO NOT trim or reformat
echo "$NOTION_FETCH_TEXT_FIELD" > .nsync/tmp/<page_id>.fetch.txt

# 2. extract the body, then hash; or extract and write a normalized snapshot
python3 nsync.py extract-body .nsync/tmp/<page_id>.fetch.txt \
  | python3 nsync.py hash --mode remote                      # → sha256:… for remote_hash

python3 nsync.py extract-body .nsync/tmp/<page_id>.fetch.txt \
  | python3 nsync.py normalize --mode remote \
  > .nsync/snapshots/<page_id>.md                            # snapshot write

# 3. clean up scratch
rm .nsync/tmp/<page_id>.fetch.txt
```

`extract-body` is idempotent — piping it on a body that was already extracted (or a local file) returns the input unchanged. So sub-agents never need to detect "is this an envelope?" themselves: always extract, always hash. See `manifest-schema.md` → "Fetch-envelope extraction (`extract-body`)" for the full spec.

**Note on `child_link_tags`.** Notion serializes `<page url>` URLs inconsistently — canonical (undashed 32-hex), HTML-escaped, bare URL, and the hybrid 8-4-rest-no-dashes form have all been observed in a single run. Never write inline URL→UUID regex in commands; pipe the tag list through `python3 nsync.py extract-uuids` (`manifest-schema.md` → "Compute helper") which is tolerant of all four forms and always emits canonical dashed UUIDs.

## Markdown normalization (hash pipeline)

Both `local_hash` and `remote_hash` are SHA-256 over content processed through:

1. UTF-8 decode.
2. NFC unicode normalization.
3. LF line endings only.
4. Strip trailing whitespace per line.
5. Single trailing newline (no extra blank lines at EOF).
6. For `remote_hash` only: strip every enhanced-markdown rich-block tag and its contents (see tag list below).
7. **Both sides**: strip whole-line `<page url ...>...</page>` tags AND whole-line nsync child-link lines AND whole-line placeholder child-link lines (both regexes below). The `<page url>` strip catches what Notion serializes; the child-link / placeholder strip catches what nsync renders locally (managed lines plus user-authored placeholders awaiting commit-time backfill). Together they ensure child adds/removes/renames/reorders and pending placeholders never move either hash.

### Child-link line regex (case-insensitive, anchored)

Managed line (carries a resolved `page_id`):

```
^\[[^\]]*\]\([^)]+\)\s*<!--\s*nsync:child\s+page_id="[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"(\s+external)?\s*-->\s*$
```

Placeholder line (no `page_id`; target path captured; awaits commit-time backfill):

```
^\[[^\]]*\]\(([^)]+)\)\s*<!--\s*nsync:child\s*-->\s*$
```

The two are disjoint (the managed line carries `page_id="<uuid>"` before `-->`; the placeholder has `-->` immediately after `child`) and **both** are stripped by step 7 above. Lines matching the managed regex are nsync-managed; see `path-mapping.md` → "Child-link lines" for format, position semantics, the auto-managed rule, and "Commit-time backfill" for how placeholders resolve. `<page url>` is the input form Notion gives via `notion-fetch`; child-link is the local rendered form written by `/nsync:init` and regenerated by `/nsync:pull`.

## Rich-block tags stripped for `remote_hash` only

These appear in the enhanced-markdown serialization from `fetch` and must be stripped before computing the markdown-only hash:

- `<callout>...</callout>`
- `<toggle>...</toggle>`
- `<embed ...>`, `<bookmark ...>`, `<link_preview ...>`
- `<equation ...>` / `<equation>...</equation>`
- `<file ...>`, `<audio ...>`, `<video ...>`, `<pdf ...>`
- `<synced_block>...</synced_block>`
- `<column_list>...</column_list>` and inner `<column>...</column>`
- `<button>...</button>`
- `<ai_block>...</ai_block>`
- `<empty-block/>` (Notion's serialization of an empty paragraph block — present whenever a page body is "empty" in the UI but holds a single zero-content block)
- `<page url="...">` (child-page references — handled by their own PageRecord)
- `<data-source url="...">` (databases — out of scope)

If a tag we don't recognize appears, log a warning and treat its content as opaque (strip the entire tag span before hashing). `scripts/nsync.py` implements this fallback via `_warn_strip_unknown` — every unknown tag name produces one stderr warning per process, then is removed. The whitelist of HTML-passthrough inline tags (`<b>`, `<em>`, `<span>`, etc.) is in `nsync.py:HTML_PASSTHROUGH_TAGS`; the unknown-tag fallback skips those.

### Image-block lines (markdown syntax, not XML)

Verified empirically: Notion serializes image blocks as standalone markdown image lines, NOT as `<image>` XML tags. A line whose entire trimmed content matches the regex `^!\[[^\]]*\]\([^)]+\)(\s*\{[^}]*\})?$` is an image block and must be stripped for `remote_hash` computation.

Examples that are stripped:

- `![Test image](https://upload.wikimedia.org/...)` — standalone image-block line
- `![](https://prod-files-secure.s3...) {color="gray_bg"}` — image block with color attr

Examples that are PRESERVED (inline markdown image inside running text — round-trips fine):

- `See ![logo](https://example.com/logo.png) for the brand mark.` — text both before and after the `![]()` on the same line

Track image blocks in the PageRecord's `rich_blocks` as entries of `type: "image"` with anchors like `after:## Linting` so `/nsync:diff` can annotate them with `[rich block: image] (not synced)`.

## Rich-block-safe update via `update_content`

The `/nsync:commit` strategy (snapshot-diff → snippet replace):

1. `.nsync/snapshots/<page_id>.md` — markdown-only body at last sync.
2. The local `.md` file — current state, markdown only.
3. Diff snapshot → local via `python3 "$CLAUDE_PLUGIN_ROOT/scripts/nsync.py" diff <snapshot> <local>` (it strips child-link lines from both sides first). Do NOT diff in-context.
4. `fetch` the page to get `remote_raw` (with rich-block tags).
5. For each hunk, build an `{ old_str, new_str }` pair. `old_str` MUST satisfy ALL of these constraints (verified by the dry-run — a substring-match without line boundaries can corrupt pages by replacing a fragment inside an unrelated line):
   1. **Line-bounded**: `old_str` begins at a line boundary (preceded by `\n` or position 0) and ends at a line boundary (followed by `\n` or end-of-string) within `remote_raw`.
   2. **Whole-line coverage**: contains the entire line on which the snapshot→local change starts AND the entire line on which it ends. Never a fragment of either edge line.
   3. **Surrounding context**: at least one full preceding line of unchanged context AND one full following line of unchanged context, where they exist (i.e., not at start/end of file).
   4. **Unique within `remote_raw`**: exactly one occurrence under the line-bounded match.
6. Validate against the constraints above:
   - Locate every occurrence of `old_str` in `remote_raw`. For each, confirm the character immediately before is `\n` (or it's position 0) and immediately after is `\n` (or end-of-string). Reject the hunk if any occurrence is mid-line OR if more than one full-line-bounded match exists.
   - If zero matches, the file lost a race (remote changed since the staleness check). Refuse the file and suggest re-pull.
   - If multiple matches even after the line-boundary filter, surface the ambiguity to the user with a suggestion to add more context lines.
   - No rich-block tag or image-block line falls inside any `old_str` span. If one does, prompt the user: `[F]orce-replace (deletes the rich block) / [S]kip`.
7. Submit all hunks for the file in a single `notion-update-page` call with `command="update_content"` and `content_updates=<array>`.
8. On success: `fetch` again, recompute `remote_hash` via `nsync.py hash --mode remote`, overwrite the snapshot with the new local content, refresh `last_synced_at`.

## Trash gap (no MCP supports trashing a page)

The available connector toolkit has no operation to trash a regular page. `notion-update-data-source` with `in_trash: true` applies to databases / data sources only, not pages.

Workarounds in priority order:

1. **Orphan to workspace** — `notion-move-pages` with `new_parent: { type: "workspace" }`. Page survives at workspace level; user can trash later in Notion UI. TrashEntry `trashed_by: "orphaned-to-workspace"`. This is the default in the `/nsync:commit` prompt for deleted-local pages.
2. **Manual trash** — print the page URL, instruct the user to use the `···` menu → "Move to Trash" in Notion. TrashEntry `trashed_by: "untracked-no-remote-action"`.
3. **Remote-trashed detection** — a tracked page that no longer appears in `search` results under the parent is treated as remote-trashed. Prompt to delete the local file or keep it as `Added` on next commit. TrashEntry `trashed_by: "remote-trashed"`.

## Init preflight error mapping

| Symptom | Cause | User-facing message |
|---|---|---|
| HTTP 404 from `fetch` | Page deleted or URL wrong | "URL malformed or page deleted. Verify the URL in Notion." |
| HTTP 403 from `fetch` | Connector lacks access to the page | "Your connected Notion account can't see this page. Sign in to Notion as the right user, or grant access to this page from notion.so, then re-run /nsync:init." |
| Response includes `<data-source>` tag | URL points at a database | "URL points to a database. nsync syncs page trees only; provide a regular page URL." |
| `mcp__claude_ai_Notion__*` tool not registered | Notion connector not installed in Claude Code / Cowork | "Notion connector not installed. Open Claude Code → Settings → Connectors → Notion → Connect, then re-run /nsync:init." |
| Connector call returns auth error | OAuth expired | "Your Notion OAuth session has expired. Reconnect via Settings → Connectors → Notion, then re-run /nsync:init." |

## Pagination

`search` returns `next_page_token` when more results exist. Always paginate to exhaustion when enumerating sub-trees, with a reasonable safety cap (e.g., 500 pages) to avoid runaway loops.

## Image / file URL caveat

Notion-hosted image/file URLs returned in `<image>` and `<file>` tags are signed and expire (typically ~1 hour). The plugin deliberately does NOT promote `<image>` tags into local `![]()` markdown — they would link-rot the moment the user opens the file later. User-authored `![]()` references with external URLs round-trip cleanly through markdown-only normalization.
