# Conflict protocol

## Three states per tracked page

Determined during `/nsync:pull`, `/nsync:status`, `/nsync:diff`, `/nsync:commit`.

| State | Condition |
|---|---|
| **Clean** | `local_hash == manifest.local_hash` AND `remote_hash == manifest.remote_hash` |
| **Auto-mergeable (local-only)** | `local_hash != manifest.local_hash` AND `remote_hash == manifest.remote_hash` |
| **Auto-mergeable (remote-only)** | `local_hash == manifest.local_hash` AND `remote_hash != manifest.remote_hash` |
| **Conflict** | `local_hash != manifest.local_hash` AND `remote_hash != manifest.remote_hash` (both sides changed) |

`remote_hash` is computed over markdown-only content (rich-block tags + standalone image-block lines stripped — see `notion-mcp-cheatsheet.md`). Notion-side edits that only touch a callout, embed, image, comment, or page property therefore do NOT move `remote_hash` and never trigger conflict. v1 uses hash compare exclusively; no timestamps participate in classification.

## Per-state actions during `/nsync:pull`

| State | Action |
|---|---|
| Clean | Refresh `last_synced_at`. No file write, no hash change. |
| Auto-mergeable (local-only) | Leave file untouched. Refresh `last_synced_at` only. |
| Auto-mergeable (remote-only) | Overwrite local file with remote markdown. Update `local_hash`, `remote_hash`, snapshot. Refresh `last_synced_at`. |
| Conflict | Surface to user (below). |

## Conflict UX

For each conflicting page, drive AskUserQuestion with these options:

- **`[L]ocal`** — keep local file as-is. The user is overruling the remote change; treat local as the new merged baseline.
- **`[R]emote`** — overwrite local file with remote markdown. The user is discarding their local edits; treat remote as the new merged baseline.
- **`[E]dit`** — write a scratch buffer to `.nsync/conflicts/<page_id>.scratch.md` containing three labeled sections (LOCAL / REMOTE / MERGED, with MERGED pre-filled as a best-guess merge). Ask the user to edit the MERGED section, confirm, then overwrite the real file with the MERGED contents and remove the scratch. The user's merged content is the new baseline; remote is the reference point for the next commit.
- **`[S]kip`** — leave file dirty. Manifest unchanged. Conflict reappears on next pull. Blocks `/nsync:commit` of that file until resolved.

**Never write `<<<<<<<` / `=======` / `>>>>>>>` markers into a tracked `.md` file.** Reason: if the user skips and forgets, the next `/nsync:commit` would push the markers verbatim to Notion, where they render as plain code, and a follow-up pull would parse them back as content.

Multi-file conflicts process serially. After each resolution, ask "Continue with the next conflict, or stop and finish later?" — partial pulls are allowed; the manifest commits per-file after each resolution lands, so abort/resume is safe.

### Snapshot update after resolution

What `.nsync/snapshots/<page_id>.md`, `local_hash`, and `remote_hash` become after each choice. This is load-bearing — without the correct snapshot update, the next `/nsync:commit` produces hunks whose `old_str` doesn't appear in the current `remote_raw`, forcing a refuse-and-re-pull cycle.

| Choice | Local file | Snapshot after | local_hash / remote_hash after | Next commit |
|---|---|---|---|---|
| `[L]ocal` | unchanged | ← local content | both = `hash(local)` | will push local→remote; commit's snapshot→local diff is empty for THIS page, but the commit still needs to push because manifest.remote_hash now equals local even though Notion's actual content is different. Treat this case specially: build hunks by diffing **remote markdown → local** (one shot), validate per the line-boundary rule, and push. |
| `[R]emote` | overwritten with remote markdown | ← remote markdown | both = `hash(remote)` | nothing to push for this page |
| `[E]dit` | overwritten with MERGED | ← **remote markdown** | `local_hash = hash(merged)`, `remote_hash = hash(remote)` | commit's snapshot→local diff produces hunks describing the merged-vs-remote delta; `old_str` values land in `remote_raw` cleanly |
| `[S]kip` | unchanged | **unchanged** | unchanged in manifest | commit of this file remains blocked; conflict resurfaces on next `/nsync:pull` |

The `[E]dit` row is the critical one — without setting snapshot = remote, hunks reference the pre-conflict state and never match. The `[L]ocal` row's "diff remote→local" special case mirrors how a git `--theirs` resolution still has to push a new tree object: the manifest claims convergence but the remote hasn't been updated yet.

## Scratch buffer format

When `[E]dit` is chosen, write the following to `.nsync/conflicts/<page_id>.scratch.md`:

```
# CONFLICT — <page title>
# Path: <relative path>
# Local hash:  <short prefix>    Last synced: <RFC3339>
# Remote hash: <short prefix>    Fetched at:  <RFC3339>
#
# Edit the MERGED section below. When you save and confirm, MERGED becomes the
# new file content (LOCAL and REMOTE sections are stripped before writing).
# Do NOT remove the "<!-- MERGED -->" / "<!-- /MERGED -->" markers.

<!-- LOCAL -->
<local file content>
<!-- /LOCAL -->

<!-- REMOTE -->
<remote markdown content, rich-block tags replaced with [rich block: <type>] placeholders>
<!-- /REMOTE -->

<!-- MERGED -->
<best-guess merge — e.g., concatenate hunks where unambiguous, leave gaps as [TODO: resolve] for the user>
<!-- /MERGED -->
```

The LOCAL and REMOTE sections are reference-only and must be discarded when finalizing. Only the contents inside the MERGED markers become the new file.

## Detecting conflicts that resolve themselves

If `remote_hash != manifest.remote_hash` but the remote markdown is byte-identical to the local file after normalization, the "conflict" is illusory — both sides converged on the same content. Treat as Clean and silently update both hashes.
