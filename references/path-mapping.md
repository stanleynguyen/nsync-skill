# Path ↔ Notion page mapping

## Scope

Only `*.md` files (case-insensitive) participate in sync. Any other extension is invisible to every command. A `notes.txt` next to `welcome.md` is not deleted, not synced, not surfaced in status — it just doesn't exist as far as nsync is concerned.

## Mapping rules

1. **Sync root** ↔ the Notion parent page configured in `config.json`. The parent's *body* is mirrored at `<sync-root>/index.md` **only when that body is non-empty** (see "Non-empty parent body" below). When mirrored, it is tracked as a regular PageRecord keyed by `config.parent.page_id` with `parent_page_id: null` (the sentinel marking the sync root — distinguishes it from every other PageRecord). When the parent body is empty (e.g., a pure navigation page with only sub-pages), no `index.md` is created and no root PageRecord exists in the manifest. The sub-tree below the parent is always mirrored, parent body or not. **Any inline `<page url>` child references in the parent body are rendered as auto-managed child-link lines at the same position** — see "Child-link lines" below.

2. **Sub-page with children** ↔ a directory containing `index.md` for its own body, plus one file per child. Inline `<page url>` child references in the parent body are rendered as auto-managed child-link lines at the same position (see "Child-link lines" below). Example: Notion page "Engineering" with children "Standards" and "Onboarding":

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

5. **Edge case — child literally titled "index"** inside a parent that has children: slug → `index.md`, collides with the parent's own `index.md`. Rule #4 kicks in → child becomes `index-2.md`. The manifest UUID key disambiguates either way. This applies equally to a direct child of the sync root titled "index": it collides with the root `index.md` (when present) and is suffixed to `index-2.md`.

## Non-empty parent body

After the markdown normalization pipeline (rich-block tags + image-block lines + child-page `<page url>` tags stripped — see `notion-mcp-cheatsheet.md`), the residual markdown is the parent's "body content". Treat it as **empty** if any of:

- Length after `strip()` is 0.
- The only remaining content is the title heading line (`# <Parent title>\n`).

Otherwise it is non-empty and the sync root gets an `index.md` per rule #1.

## Root-page constraints

The PageRecord whose UUID key equals `config.parent.page_id` is the **root entry**. It carries these special-case rules everywhere:

- **No rename.** `index.md`'s path is fixed. Rename detection (below) skips any PageRecord whose `parent_page_id` is `null`.
- **No orphan or trash.** The Notion parent must continue to exist. `/nsync:commit`'s Deleted-page flow gives the root a restricted prompt (`[R]estore local` / `[E]mpty parent body`) — never the orphan or manual-trash options.
- **No automatic ignore.** `index.md` is not in the default `.nsync/ignore`. Users may add it manually to suppress; standard `local_ignored` mechanics then apply.

## Child-link lines

For any page with `has_children: true` (root + every sub-page-with-children), inline `<page url>` references in the parent body are rendered as auto-managed **child-link lines** in the local `index.md`, at the same line position they appear in the Notion body.

### Format

One line per `<page url>` tag, this canonical form:

```
[<Title>](<relative-path-or-notion-URL>) <!-- nsync:child page_id="<uuid>"[ external] -->
```

- `<Title>` — the inner text of the `<page url>` tag. Backslash-escape `]` and `)` inside the title; replace `\n` with a single space.
- `<relative-path-or-notion-URL>` — for child pages tracked in the manifest, the path relative to the file being written (e.g., `./prd--autopilot-pm/index.md` from the sync root). For pages outside the sync tree, the full `https://www.notion.so/<uuid>` URL with the ` external` flag set.
- `page_id="<uuid>"` — the dashed-UUID of the child page. **The stable identifier** — survives title and path changes.
- ` external` — present only when the linked page has no PageRecord (out-of-sync-tree target).

### Recognition regex

A line is an nsync-managed child link iff its entire trimmed content matches (case-insensitive):

```
^\[[^\]]*\]\([^)]+\)\s*<!--\s*nsync:child\s+page_id="[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"(\s+external)?\s*-->\s*$
```

### Auto-managed rule

Child-link lines are **owned by nsync**, not the user:

- They are rewritten on every `/nsync:pull` to match the current set of `<page url>` tags in remote body, in the same order Notion serializes them.
- Local edits to the title text or relative path inside a child-link line are **discarded on next pull** (UUID match drives the rewrite). Document this so users don't waste effort editing them.
- Local insert / delete / reorder of child-link lines does NOT propagate to Notion — the source of truth for the child list is Notion's page tree. (A future `/nsync:mv` could push reorder; out of scope for v1.)
- Both `local_hash` and `remote_hash` strip child-link lines (and whole-line `<page url>` tags) before SHA-256 — see `manifest-schema.md` → "Markdown normalization". Adding, removing, renaming, or reordering a child therefore does NOT move either hash and never triggers the conflict prompt.

### Regeneration trigger

Runs inside `/nsync:pull` after the three-state classification (see `conflict-protocol.md`) for every PageRecord with `has_children: true`. Behavior:

- **`expected == existing`** byte-for-byte: no-op.
- **`existing` non-empty, mismatch**: replace each existing line by UUID match (updates title/path); insert any newly-added expected lines after the last existing child-link line; drop any orphaned existing lines whose UUID is no longer in `expected`. Position of the first existing line is preserved.
- **`existing` empty, `expected` non-empty** (migration case for already-init'd folders): if the local file's non-child-link content equals the snapshot's, overwrite with the regenerated body (positions match Notion). If local body has diverged, append all expected lines after the file's last non-empty line and surface a one-time `Added <N> child-link lines to <path>; reposition manually if desired.` message.

The snapshot is overwritten to match. Hashes don't move; no spurious Modified state.

### `/nsync:commit` filtering

Before constructing `update_content` hunks, `/nsync:commit` strips every child-link line from BOTH the snapshot AND the local content. Resulting hunks describe only prose edits; Notion's native child-page blocks stay untouched.

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
2. For each PageRecord whose `path` is missing on disk **and whose `parent_page_id` is not `null`** (skip the root entry — its path is fixed):
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
