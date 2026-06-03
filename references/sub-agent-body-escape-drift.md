# Sub-agent body-escape drift (the source of stable false-stale reports)

## Symptom

After a successful `/nsync:commit`, the next `/nsync:status` (or another `/nsync:commit`) reports a subset of pages as having drifted `remote_hash` even though nothing on Notion has changed. The drift is intermittent across pages in one run, varies across runs, and persists indefinitely — re-running `/nsync:status` does NOT converge.

Observed in a real run on a 19-page tree:
- 9 of 19 pages reported `modified_remote` after a commit. Same pages clean across two consecutive status fetches some runs, drifted across others.
- 4 of 16 modified pages failed the initial `/nsync:commit` push with `Notion API validation_error (400): No matches found for old_str`. Inspecting those pages' hunks JSON showed `old_str` values containing literal `\n` (backslash-n, two characters) instead of real newlines.

## Root cause

`/nsync:commit` step 4.2 and the equivalent steps in pull / status / diff / init each instruct a Workflow sub-agent to:

> Write the response's `text` field verbatim to `.nsync/tmp/<page_id>.fetch.txt`.

The intended path: call the `Write` **tool** with `content=<the .text string>`. The `Write` tool writes the string raw, byte-for-byte, with real newlines.

The failure path that empirically emerges in ~20-25% of sub-agents (varies by batch, by Workflow run, by model temperature):

```sh
cat > /Users/.../365c21c8-3d5a-8198-ae9b-ecc8103ccbcb.fetch.txt << 'EOF'
{"metadata":{"type":"page"},"title":"🧭 PM Curriculum...","url":"...","text":"Here is the result of \"view\"...\n<page url=\"...\">\n<content>\n...real markdown body with \n escapes inside the JSON string..."}
EOF
```

Two things go wrong simultaneously here:

1. **Whole-JSON dump.** The sub-agent wrote the ENTIRE MCP tool result (`{"metadata":..., "text":"..."}`), not just the `.text` field. The envelope nsync.py expects starts with the preamble line `Here is the result of "view"...` — what it gets starts with `{"metadata":`.
2. **JSON escape sequences preserved.** Inside the JSON string value, newlines are encoded as `\n` (backslash + n, two characters), quotes as `\"`, backslashes as `\\`. Bash heredoc with `'EOF'` quoting passes them through literally. So the resulting file has *no real newlines inside the body content* — every line break is the two-character sequence `\n`.

Then `extract-body`'s regex `<content>\n?(.*?)\n?</content>` still matches (because `<content>` and `</content>` appear in the JSON-escaped text as literal `<content>` / `</content>`, unescaped), but the captured body has `\n` literals throughout. The body hashes deterministically — to a hash that nobody else computes the same way, because the next sub-agent might use `Write` correctly. So `manifest.remote_hash` (computed by one sub-agent) and `status`-time `remote_hash` (computed by a different sub-agent) disagree even though the actual Notion content is identical.

Why intermittent: each sub-agent independently chooses between `Write` tool and Bash heredoc. The choice correlates with batch size, prompt phrasing, and which earlier exemplars the model saw. Empirically about 1 in 4 batches reaches for Bash.

Why stable per-page: once a page's manifest hash was recorded by a sub-agent that mangled its body file, that hash is now baked in. Until something rewrites the manifest, the false-stale report repeats.

## Recovery (already in `nsync.py extract-body`)

`extract-body` defensively recovers from both failure modes BEFORE the `<content>` regex runs. See `_recover_mangled_envelope` in `scripts/nsync.py`:

1. **JSON-wrapper case.** If the input starts with `{` and contains `"text"` in the first 512 bytes, parse it as JSON and use `.text`.
2. **Escape-mangled case.** If the first 2 KB contain ≥5 literal `\n` sequences AND ≤1 real newline, decode the whole input through `codecs.decode(text, 'unicode_escape')`.

Both are idempotent on clean envelopes:
- Clean envelopes don't start with `{` (they start with `Here is the result of "view"...`).
- Clean envelopes have many real newlines and zero literal `\n` two-char sequences (Notion does not return JSON-encoded markdown).

So the recovery is a safety net for sub-agent variance, applied automatically and silently. The clean path is unchanged.

## Prevention (prompt hardening)

The recovery in `extract-body` is a backstop, not a license to write mangled files. Command prompts (`commands/commit.md`, `commands/pull.md`, `commands/status.md`, `commands/diff.md`, `commands/init.md`) now spell out the rule:

> Use the `Write` **tool** (not Bash) to save the response's `.text` field to `.nsync/tmp/<page_id>.fetch.txt`. The `.text` value is a raw string with real newlines; pass it directly as `content`. **Forbidden:** Bash `cat > file << 'EOF'` heredoc, `echo "$VAR" >`, or any shell-mediated write.

And `references/notion-mcp-cheatsheet.md` → "Sub-agent fan-out & context discipline" now shows pseudocode for the `Write` tool call instead of a `sh` block (the previous `sh` block was the bug magnet — sub-agents pattern-matched the bash example and reached for heredoc).

## How to detect a mangled fetch file by eye

If `.nsync/tmp/<page_id>.fetch.txt` looks like one long line, or starts with `{"metadata":` or `{"text":` instead of `Here is the result of "view"...`, the sub-agent fumbled. The current `extract-body` recovers transparently; a future debugging session may want to log a stderr warning when recovery kicks in.

## Related historical surprises

- `notion-fetch` returning the same envelope twice in a row for the same page produced identical text bytes (verified empirically with same-second timestamps in both responses). The drift was NOT Notion non-determinism — it was sub-agent serialization variance.
- A separate-but-co-occurring bug produced `remote_trashed: 19` false positives in `/nsync:status` on the same 19-page tree: `extract_uuids`'s regex only matched `notion.so` / `notion.site` hosts, but Notion silently migrated `<page url>` child references to the `app.notion.com/p/<uuid>` form. Every UUID extraction came back empty, breaking reachability. Fixed by widening the regex to accept `notion.com` / `app.notion.com` and the `/p/` path prefix. See the `_URL_OR_TAG_RE` regex in `scripts/nsync.py` and `references/notion-mcp-cheatsheet.md` → "Note on `child_link_tags`".

## Test coverage

`scripts/nsync.py` `_recover_mangled_envelope` is exercised by four inline tests (clean envelope, JSON-wrapper, escape-mangled, bare body) — see the smoke test in `references/sub-agent-body-escape-drift.md` history (this file). When the test infrastructure grows up, move them into a proper `tests/` dir.
