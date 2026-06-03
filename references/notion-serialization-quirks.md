# Notion serialization quirks (hash pipeline step 8)

## Why this file exists

Notion's `notion-fetch` returns a **deterministically mutated** form of the markdown that was sent on `notion-create-pages` / `notion-update-page`. The mutations are pure serialization choices (not user edits), but they cause `local_hash` and `remote_hash` to diverge for every page that contains a table, a backslash-escapable character, a bare URL, a code fence with a language hint, a blank line between blocks, an inline `[bracket]`, or a leading H1 matching the page title.

Without step 8, `/nsync:status` reports 90%+ of a freshly-init'd tree as **Remote-newer** even when nothing was edited on Notion. `/nsync:pull` would then overwrite user-authored markdown with Notion's mutated form (losing pipe-tables, escapes, blank-line shape). `/nsync:commit` would build hunks against a snapshot that bakes in those mutations, then refuse the push because `old_str` doesn't appear line-bounded in `remote_raw` after Notion re-mutates.

Step 8 reverses every observed mutation so both sides reduce to a common canonical form for hashing only. **On-disk files are never rewritten** — only the bytes fed to SHA-256 are canonicalized. The local file's authored shape (MD pipe-tables, plain `$`, language hints on code fences, paragraph breaks) is preserved exactly.

Step 8 runs after step 7 (`<page url>` / child-link strip) and before the final EOF-newline normalization in `nsync.py:normalize()`. Both `mode=local` and `mode=remote` invoke step 8 identically, with the same `expected_title` hint.

## The seven empirical categories

| # | Category | Local-authored form | Notion-returned form |
|---|---|---|---|
| 1 | H1 page-title in body | `# My Page\n\n<body>` | `<body>` (title moved to `<properties>` block) |
| 2 | Table | `\| a \| b \|\n\| --- \| --- \|\n\| 1 \| 2 \|` | `<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>` |
| 3 | Inter-block blanks | `para1\n\npara2` | `para1\npara2` |
| 4 | Backslash escapes | `$5`, `~5/10` | `\$5`, `\~5/10` |
| 5 | Bare URL autolink | `https://x.com` or `Sync.com` | `[https://x.com](https://x.com)` or `[Sync.com](http://Sync.com)` |
| 6 | Code-fence language | `` ```\nfoo\n``` `` | `` ```javascript\nfoo\n``` `` |
| 7 | Bracket escapes | `[00:00:11]` | `\[00:00:11\]` |

All seven are pure serialization choices Notion makes on write. None reflect user-intent change. The catalog is empirical — discovered by sampling 10 diffs across diverse content shapes (callouts, lists, code, tables, equations). If a future Notion behavior change introduces a new mutation, add a row here and a corresponding transform in `_canonicalize_notion_noise`.

## Canonical form (what step 8 produces)

For each category, both sides converge to:

| # | Category | Canonical form |
|---|---|---|
| 1 | H1 strip | When caller passes `expected_title` AND the first non-blank line is exactly `# <expected_title>`, drop that line + any immediately-following blank line. Otherwise unchanged. |
| 2 | Tables | MD pipe-table form: `\| a \| b \|\n\| --- \| --- \|\n\| 1 \| 2 \|\n`. HTML `<table>` parsed via stdlib `html.parser.HTMLParser`; rowspan/colspan/nested tables are left verbatim (parser bails, warns once). Cell content has `\|` escaped to `\\\|` to avoid pipe-collision. |
| 3 | Blank lines | Runs of 2+ newlines → single `\n`. Inside fenced code, blanks preserved. |
| 4 | Backslash escapes | Drop `\` before any of `$ ~ # \| * _ \` { } ( ) > + - . ! [ ]` (CommonMark-escapable set Notion is known to over-escape). Inside fenced code, escapes preserved. |
| 5 | Autolink | `[<URL>](<URL>)` → `<URL>` (byte-identical). `[<text>](https?://<text>)` → `<text>` (bare-domain variant where Notion adds the scheme). URLs with `)` in path are not unwrapped (conservative). |
| 6 | Fence language | Strip the language token from ` ```<lang>\n` → ` ```\n`. On-disk language hint preserved for editors. |
| 7 | Bracket escapes | Subsumed by category 4 (`[` and `]` are in the escape set). |

## Why each transform is safe

| # | Safety argument |
|---|---|
| 1 | Heuristic: only fires when the caller explicitly passes a title AND the H1 text matches the title verbatim. Mismatched title or `None` → no-op. Risk: legitimate in-body H1 that happens to equal the title is dropped (rare; same H1 in remote means Notion already dropped it, so symmetric). |
| 2 | HTML→MD pipe-table is lossless for tables Notion produces (no rowspan, no colspan, no styled cells). Parser bail-out keeps unsupported tables verbatim; a warn-once stderr message surfaces them. |
| 3 | CommonMark renders `\n` and `\n\n` differently (soft break vs paragraph break) BUT Notion does not preserve that distinction on round-trip — both ends up as block boundaries. Collapsing to single `\n` for hashing loses no information that survives the round-trip. On-disk files keep their blank lines. |
| 4 | CommonMark spec: `\X` and `X` render identically for X in the punctuation-escape set. Stripping the backslash is a CommonMark-equivalent canonicalization. Risk: inside code spans/fences, the backslash IS literal — fence-aware extraction protects code. |
| 5 | Byte-identical label/target autolink renders identically to bare URL in any markdown engine. Bare-domain variant: Notion adds the scheme; we strip the wrapper to recover the bare form (which the user authored). Risk: URLs with parens in path don't match the regex — left verbatim. |
| 6 | Notion infers a language hint when none was authored; the hint is informational for syntax highlighting only and does not affect rendering of the code content. Hash compare ignores it; on-disk file keeps it. |
| 7 | Same as category 4 (extension of the escape set). |

## Implementation: code paths in `scripts/nsync.py`

| Helper | Lines | Purpose |
|---|---|---|
| `_strip_h1_title` | `_strip_h1_title(text, expected_title)` — category 1 |
| `_canonicalize_table` + `_canonicalize_tables` + `_NotionTableParser` | category 2 — HTML→pipe |
| `_canonicalize_table_separator` + `_TABLE_SEPARATOR_RE` | pipe-table separator row normalization |
| `_extract_code_blocks` + `_restore_code_blocks` + `_FENCED_CODE_BLOCK_RE` | sentinelize code blocks so prose transforms don't touch code; also strips category 6 language hints during extraction |
| `_canonicalize_prose` + `_ESCAPE_RE` + `_AUTOLINK_RE` + `_BARE_DOMAIN_AUTOLINK_RE` + `_BLANK_RUN_RE` | categories 3 + 4 + 5 + 7 on sentinelized prose |
| `_canonicalize_notion_noise` | orchestrator entry point called from `normalize()` step 8 |
| `extract_title` + `extract_body_with_title` | pull the page title from `<properties>{"title":"..."}</properties>` so callers can thread it into `normalize()` for category 1 |

Self-tests covering each category live in `_self_test()` (T1, T1b, T1c, T2, T2b, T3, T4, T4b, T5, T5b, T6, T7) plus a cross-cutting `XR` test that asserts byte-identical-hash for a fixture combining categories 1–7 in their local vs remote forms.

## Title plumbing

`normalize(raw_bytes, mode, expected_title=None)` accepts an optional title hint. Callers in the script:

- `cmd_process_fetch` — title from `extract_title()` on the fetch envelope's `<properties>` block.
- `cmd_process_postwrite` — title from `extract_title()` on the post-write re-fetch envelope.
- `hash_batch_local` — title from a `{path: title}` mapping built from the manifest by the caller.
- `cmd_status_scan` / `cmd_diff_scan` / `cmd_commit_preflight` — build the title-by-path mapping from the manifest and pass it through.
- `cmd_pull_apply` — title from the manifest entry (`title_by_pid.get(pid)`) at every snapshot-write / hash-recompute site.
- `cmd_commit_apply` (verify pass + placeholder backfill) — title from the manifest entry.

The CLI `hash` and `normalize` subcommands do NOT yet accept `--expected-title`; pass `None` from those entry points (legitimate use cases are direct hash-of-arbitrary-bytes invocations where there's no page context). Add the flag if a downstream caller needs it.

## What this fixes

- **`/nsync:status`** — 99 false "Remote-newer" entries (on a 106-page tree where nothing was edited on Notion) disappear. Reported state matches reality.
- **`/nsync:pull`** — Auto-mergeable-remote-only no longer fires spuriously, so local files don't get overwritten with Notion's mutated form.
- **`/nsync:commit`** — Snapshots still store Notion's serialized form for compat, but hunk validation now happens against a canonicalized `remote_raw`, so deterministic round-trip noise can no longer masquerade as `race_lost`.

## What this does NOT fix

- **Notion edits that overlap user edits on the same line** still produce real conflicts. Step 8 only suppresses noise — real divergences (user changed line 12 + remote changed line 12) still classify correctly as Conflict.
- **Tables with rowspan / colspan / nested tables** are left verbatim. A warn-once stderr message tracks them. If a page uses these, hashes for that page will diverge — but the user is told.
- **`<image>` / signed-URL link rot** — out of scope (image URLs aren't promoted into local markdown; see "Image / file URL caveat" in `notion-mcp-cheatsheet.md`).

## Migration

Existing manifests written under the pre-step-8 pipeline still hold the old hash values. After deploying step 8, the simplest path is **re-init**:

```sh
rm <sync-root>/.nsync/manifest.json
rm -rf <sync-root>/.nsync/snapshots/
# then re-run /nsync:init
```

Init re-enumerates the page tree and writes fresh hashes under the new pipeline. Local files are untouched. The `trash_log` (if any) is lost — this is acceptable per the user's explicit choice (see plan history). A future `/nsync:refresh-baseline` command could rewrite the manifest in place without touching local files; out of scope for v1.

## Adding a new category

If a new Notion mutation is discovered:

1. Sample several diffs to confirm the pattern is deterministic (not just user-typo noise).
2. Add a row to the empirical-categories table above with local form, remote form, and one example.
3. Add a transform function in `nsync.py` and call it from `_canonicalize_prose` (if fence-aware) or `_canonicalize_notion_noise` (if whole-doc).
4. Add at least one positive self-test case + one no-op / boundary-case test.
5. Run `python3 nsync.py self-test` to confirm.
6. Re-init affected sync roots so manifest baselines absorb the new transform.

Step 8 is **load-bearing for sync correctness**. Treat changes here with the same rigor as the rich-block strip pipeline (step 6).
