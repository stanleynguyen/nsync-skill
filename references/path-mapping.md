# Path ↔ Notion page mapping

## Scope

Only `*.md` files (case-insensitive) participate in sync. Any other extension is invisible to every command. A `notes.txt` next to `welcome.md` is not deleted, not synced, not surfaced in status — it just doesn't exist as far as nsync is concerned.

## Mapping rules

1. **Sync root** ↔ the Notion parent page configured in `config.json`. The parent itself is not mirrored as a file — only its sub-tree is.

2. **Sub-page with children** ↔ a directory containing `index.md` for its own body, plus one file per child. Example: Notion page "Engineering" with children "Standards" and "Onboarding":

   ```
   engineering/
     index.md           # body of "Engineering"
     standards.md       # child "Standards"
     onboarding.md      # child "Onboarding"
   ```

   `has_children: true` in the PageRecord pins this layout. If the parent page has no body, `index.md` still exists, containing just `# Engineering\n`.

   The `index.md` convention avoids the `engineering.md` + `engineering/` sibling pattern: if a parent had a same-named child, the child would land at `engineering/engineering.md` — same path scheme, visually confusing. `index.md` keeps the parent's body cleanly inside its own folder.

3. **Leaf sub-page** ↔ a single `.md` at the parent directory level. `welcome.md`, `engineering/standards.md`, etc.

4. **Filename = `slug(title) + ".md"`** where slug:
   - Lowercases.
   - Replaces whitespace with `-`.
   - Strips characters outside `[a-z0-9-_.]`.
   - Length-caps at 80 characters.
   - On sibling collision: append `-2`, `-3`, ... ordered by Notion page `created_at` ascending.
   - Once chosen, the path is sticky in the PageRecord. Renaming the page in Notion does NOT auto-rename the local file (v1 — a future `/nsync:mv` could do that).

5. **Edge case — child literally titled "index"** inside a parent that has children: slug → `index.md`, collides with the parent's own `index.md`. Rule #4 kicks in → child becomes `index-2.md`. The manifest UUID key disambiguates either way.

## State lives in the manifest, not in file frontmatter

The `.md` files are plain markdown — no YAML frontmatter holding `page_id` or sync metadata. Reasons:

- Files stay readable for external tools.
- No risk of leaking sync metadata back to Notion on commit.
- The manifest is easy to inspect and rebuild without parsing every file.

The cost is rename detection — addressed below.

## Rename detection

Run at the start of `/nsync:status` (report-only), `/nsync:diff` (report-only), `/nsync:commit` (apply with confirmation), `/nsync:pull` (apply with confirmation).

Algorithm:

1. Glob every `.md` path under the sync root, excluding `.nsync/` and ignored patterns.
2. For each PageRecord whose `path` is missing on disk:
   - Look for an unaccounted `.md` matching by **basename** OR by **current content-hash equality**.
   - Exactly one unambiguous candidate → AskUserQuestion: `Detected rename <old> → <new>. Apply?` On confirm, update the PageRecord's `path`. If the directory portion changed, queue a `notion-move-pages` call for the next commit step (new parent = the page corresponding to the new directory).
   - Multiple candidates or no candidate → leave as `Deleted (local)` + `Added (local)` and let the user resolve.
3. Any `.md` on disk with no matching PageRecord → `Added (local)`. It becomes a new Notion page on commit. Its parent equals the page corresponding to its containing directory; root-level files attach to `config.json.parent.page_id`.

Heuristic limitation: noisy renames + simultaneous content edits can fool the algorithm. The user can always cancel the rename prompt to keep the `Deleted + Added` framing.

## Default ignore patterns

Written by `/nsync:init` into `.nsync/ignore`:

```
# nsync default ignore patterns
# Non-.md files are already out of scope; this list filters specific .md files only.
README.md
CHANGELOG.md
LICENSE.md
CONTRIBUTING.md
```

Patterns use gitignore syntax: line-per-pattern, `#` comments, `!` negation, `**` globstar.

Matching can shell out to `git check-ignore --no-index <path>` if `git` is on PATH; otherwise use a small inline matcher. The choice is an implementation detail.

## `local_ignored` mechanics

A PageRecord whose `path` matches the ignore set gets `local_ignored: true` on the next command's pre-pass. While ignored:

- The local file is not created on pull (even if a remote page exists for that path).
- `/nsync:status` shows the page under "Ignored (was tracked)" so regressions stay visible.
- `/nsync:commit` skips create/update/delete actions for that page.
- `/nsync:pull` still refreshes `remote_hash` (we keep tracking the remote side).

If the user later removes the matching pattern from `.nsync/ignore`, the next command sees `local_ignored` flip back to false. If the local file is missing at that point, the page is treated as `Deleted` and the user gets the standard orphan-or-restore prompt.
