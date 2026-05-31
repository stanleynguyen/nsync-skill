# nsync sub-agent return schemas (v1)

Three distinct sub-agent return shapes flow through the plugin. Each MUST be enforced by JSON Schema when dispatching via `Workflow` + `agent({schema})`. Prose-only fan-out (plain `Agent` tool calls without schemas) was deprecated after a 24-agent run produced five different return shapes, four fabricated hashes, and two no-write failures — `references/notion-mcp-cheatsheet.md` → "Sub-agent fan-out & context discipline" makes `Workflow`-with-schema the only sanctioned form for the ≥8-pages branch.

The schemas below are JSON Schema draft 2020-12. Each is referenced by name from the per-command files (`commands/status.md`, `commands/pull.md`, `commands/init.md`, `commands/commit.md`, `commands/diff.md`). Keep field names byte-identical to what's defined here — every downstream aggregator assumes them.

## `CompactReadRecord`

Returned by **status / pull / init / diff** read fan-outs (one per fetched page).
Wrap as `BatchReadRecords` (below) when a sub-agent handles a batch.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "CompactReadRecord",
  "type": "object",
  "properties": {
    "page_id":  { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
    "path":     { "type": "string" },
    "remote_hash": { "type": "string", "pattern": "^sha256:[0-9a-f]{64}$" },
    "manifest_remote_hash": { "type": "string" },
    "has_children": { "type": "boolean" },
    "child_link_tags": {
      "type": "array",
      "items": { "type": "string" }
    },
    "rich_blocks": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "type": { "type": "string" },
          "anchor": { "type": "string" },
          "summary": { "type": "string" }
        },
        "required": ["type", "anchor"],
        "additionalProperties": false
      }
    }
  },
  "required": ["page_id", "path", "remote_hash", "has_children", "child_link_tags"],
  "additionalProperties": false
}
```

### `BatchReadRecords`

Sub-agent that handles a `NSYNC_READ_BATCH`-sized batch returns this wrapper.
The aggregator concatenates `records` arrays across all sub-agents.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "BatchReadRecords",
  "type": "object",
  "properties": {
    "records": {
      "type": "array",
      "items": { "$ref": "#/$defs/CompactReadRecord" }
    }
  },
  "required": ["records"],
  "additionalProperties": false
}
```

### Field notes

- **`child_link_tags`** — whole-line `<page url ...>...</page>` tags from the body, in the order Notion serializes them. May arrive HTML-escaped, with hybrid-dashed UUIDs, or as bare URLs depending on the upstream sub-agent. **Always pass through `python3 nsync.py extract-uuids`** (`references/path-mapping.md` → "Recognition regex") before comparing UUIDs.
- **`manifest_remote_hash`** — copied verbatim from the manifest at dispatch time. Lets the aggregator compare without re-reading the manifest. Omit only if the page isn't tracked yet (init's recursion).
- **`rich_blocks`** — keep this small: type + anchor are enough for diff annotation. `summary` is optional and human-readable.

## `CommitWriteResult`

Returned by **commit Modified-page** sub-agents — one per page that gets pushed.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "CommitWriteResult",
  "type": "object",
  "properties": {
    "page_id":         { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
    "path":            { "type": "string" },
    "pushed":          { "type": "boolean" },
    "new_remote_hash": { "type": ["string", "null"], "pattern": "^sha256:[0-9a-f]{64}$|^$" },
    "warnings":        { "type": "array", "items": { "type": "string" } },
    "error":           { "type": ["string", "null"] }
  },
  "required": ["page_id", "path", "pushed", "warnings"],
  "additionalProperties": false
}
```

### Field notes

- **`pushed: false`** ↔ the file was skipped (e.g. validation refused a hunk per `references/notion-mcp-cheatsheet.md` → "Rich-block-safe update" step 6, or `[F]orce-replace/[S]kip` chose skip). `error` carries the reason; `new_remote_hash` MUST be `null` or `""`. Skipping is **per file** — the rest of the commit batch continues. See `commands/commit.md` → "Modified-page recovery".
- **`pushed: true`** ↔ the page was updated and re-fetched. `new_remote_hash` is the recomputed `remote_hash` for the post-write body; the main loop writes it back into the manifest.

## `DiffTextRecord`

Returned by **diff** sub-agents — one per page in the diff scope.
Replaces the v0 raw-string return (which silently masked partial failures).

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "DiffTextRecord",
  "type": "object",
  "properties": {
    "page_id":   { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
    "path":      { "type": "string" },
    "diff_text": { "type": "string" },
    "ok":        { "type": "boolean" },
    "error":     { "type": ["string", "null"] }
  },
  "required": ["page_id", "path", "diff_text", "ok"],
  "additionalProperties": false
}
```

### Field notes

- **`ok: false`** ↔ the sub-agent could not produce a diff for this page (fetch failed, normalization threw, etc.). `diff_text` is the empty string or a one-line stderr-style marker; `error` describes the cause. The diff command's main loop renders `# <path>: <error>` instead of attempting to print partial output.
- **`diff_text`** — unified diff with `--- snapshot` / `+++ local` headers, already stripped of managed + placeholder child-link lines on both sides (per `commands/diff.md` and the cheatsheet's "Sub-agent fan-out & context discipline").

## Dispatch idiom

The canonical Workflow snippet (cf. `commands/status.md` → step 5):

```js
import { CompactReadRecord, BatchReadRecords } from './sub-agent-schemas.md'  // conceptual

const batches = chunk(pages, NSYNC_READ_BATCH)   // 4-page slices
const results = await parallel(batches.map(b => () =>
  agent(buildPrompt(b), {
    schema: BatchReadRecords,
    label: `read:${b[0].page_id.slice(0,8)}`,
    phase: 'Fetch',
    agentType: 'Explore',
  })
))
const records = results.filter(Boolean).flatMap(r => r.records)
```

`Explore` is the right `agentType` for reads — it has `Bash` + every `mcp__claude_ai_Notion__*` MCP tool, can run `nsync.py` via `Bash`, and cannot write project files, which is correct for read-only fan-out. `Write`/`Edit` are not needed because compact records are returned through the schema, not written to disk.

For commit's per-page Modified writes, override to the default workflow agent type (no `agentType` field) so the sub-agent can call `notion-update-page`.
