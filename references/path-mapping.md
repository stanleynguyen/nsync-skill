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

After the markdown normalization pipeline (rich-block tags + `<empty-block/>` + unknown-tag fallback + image-block lines + child-page `<page url>` tags stripped — see `notion-mcp-cheatsheet.md`), the residual markdown is the parent's "body content". Treat it as **empty** if any of:

- Length after `strip()` is 0.
- The only remaining content is the title heading line (`# <Parent title>\n`).

Otherwise it is non-empty and the sync root gets an `index.md` per rule #1.

Implementation note: run the check via `python3 nsync.py normalize --mode remote <body>` and compare the output to `""` (after `strip()`) or to the title-heading line. Do NOT pattern-match the input bytes — that misses `<empty-block/>` and any other rich-block residue.

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

### Placeholder child-link lines

A **placeholder child-link line** is the managed form minus the `page_id`. The user authors it where they want a child link to sit, *before* the target page exists in Notion (so no UUID is available yet):

```
[<Title>](<relative-path>) <!-- nsync:child -->
```

Its recognition regex (case-insensitive, entire trimmed line, target path captured) is disjoint from the managed regex above: the managed line carries `page_id="<uuid>"` between `child` and `-->`, while the placeholder has `-->` immediately after `child`:

```
^\[[^\]]*\]\(([^)]+)\)\s*<!--\s*nsync:child\s*-->\s*$
```

A placeholder is resolved into a managed line **only at `/nsync:commit`** (see "Commit-time backfill" below), never at `/nsync:pull`. Until then it is user-owned text. Like managed lines, placeholder lines are stripped from both `local_hash` and `remote_hash` (see `manifest-schema.md` → "Markdown normalization"), so authoring one never marks `index.md` Modified, and `/nsync:status` and `/nsync:diff` surface pending placeholders in their own section instead.

### Auto-managed rule

Child-link lines are **owned by nsync**, not the user:

- They are rewritten on every `/nsync:pull` to match the current set of `<page url>` tags in remote body, in the same order Notion serializes them.
- Local edits to the title text or relative path inside a child-link line are **discarded on next pull** (UUID match drives the rewrite). Document this so users don't waste effort editing them.
- Local insert / delete / reorder of child-link lines does NOT propagate to Notion — the source of truth for the child list is Notion's page tree. (A future `/nsync:mv` could push reorder; out of scope for v1.)
- Both `local_hash` and `remote_hash` strip child-link lines (and whole-line `<page url>` tags) before SHA-256 — see `manifest-schema.md` → "Markdown normalization". Adding, removing, renaming, or reordering a child therefore does NOT move either hash and never triggers the conflict prompt.
- A **placeholder** child-link line (the no-`page_id` form above) is the exception to "owned by nsync": it is user-authored until `/nsync:commit` resolves it. `/nsync:pull` leaves placeholders untouched (see "Regeneration trigger"), and both hashes strip placeholder lines too.

### Regeneration trigger

Runs inside `/nsync:pull` after the three-state classification (see `conflict-protocol.md`) for every PageRecord with `has_children: true`. "Existing" lines are the local lines matching **either** the managed recognition regex or the placeholder regex; "expected" lines are derived from the current `<page url>` tags (in `child_link_tags` on each fetched record — UUIDs always extracted via `python3 nsync.py extract-uuids`, never an inline regex; Notion serializes these URLs in several inconsistent forms, see `notion-mcp-cheatsheet.md` → "Sub-agent fan-out & context discipline"). Behavior:

- **`expected` == existing managed set** byte-for-byte (and no placeholders): no-op.
- **Any existing line present (managed or placeholder), mismatch**: reconcile **managed lines only**: replace each existing managed line by UUID match (updates title/path), insert any newly-added expected lines after the last existing child-link line, and drop orphaned existing **managed** lines whose UUID is no longer in `expected`. **Placeholder lines are never UUID-matched, never orphan-dropped, and never reordered: they are left exactly in place** (resolution is commit-only). Position of the first existing line is preserved.
- **No existing line at all** (neither managed nor placeholder) and `expected` non-empty (migration case for already-init'd folders): if the local file's non-child-link content (both regexes stripped) equals the snapshot's, overwrite with the regenerated body (positions match Notion). If local body has diverged, append all expected lines after the file's last non-empty line and surface a one-time `Added <N> child-link lines to <path>; reposition manually if desired.` message.

The snapshot is overwritten to match. Hashes don't move; no spurious Modified state. Because a placeholder counts as an existing line, the migration-overwrite branch never fires while an uncommitted placeholder is present, so the placeholder survives the pull intact.

### `/nsync:commit` filtering

Before constructing `update_content` hunks, `/nsync:commit` strips every child-link line **and every placeholder child-link line** (both regexes) from BOTH the snapshot AND the local content. Resulting hunks describe only prose edits; Notion's native child-page blocks stay untouched, and an unresolved placeholder is never pushed to Notion as a literal markdown link.

### Commit-time backfill

`/nsync:commit` resolves placeholder child-link lines into managed lines, in a pass that runs **after New-page creation and before the Modified-page diff** (so a just-created page already has a `page_id`, and the resolved managed line is then stripped from the diff). The pass scans **every `has_children` local file, regardless of its commit-set classification**: a parent whose only change is a placeholder hashes Clean (placeholder stripped) and is in neither the New nor Modified set, yet still needs backfill.

For each placeholder line, with its captured target path:

1. **Normalize** the captured path to sync-root-relative (join against the directory of the file being edited; forward slashes).
2. **Resolve** to the PageRecord whose `path` equals it, searching the **union of pages created in this commit ∪ existing tracked pages**. Zero matches → unresolvable (step 5). Resolving against the union (not just this commit's New pages) makes the pass idempotent and safe to resume after an interrupted commit, and lets a placeholder also target an already-existing child.
3. **Identify this file's page_id**: `config.parent.page_id` when the file is the root `index.md` (its own `parent_page_id` is `null`), otherwise the file's own page_id.
4. **Parent guard**: the resolved target's `parent_page_id` must equal this file's page_id. If not (the target lives under a different parent, or is an out-of-tree/external page), treat as unresolvable (step 5), since Notion cannot represent it as a child block of this page.
5. **On success**: rewrite the placeholder in place into the canonical managed line (title and relative path recomputed from the manifest / create response, plus the resolved UUID). **On unresolvable / parent-mismatch**: emit a warning, leave the line as a placeholder (it is stripped from the diff by the filtering rule above, so nothing is pushed as prose), and continue.
6. **Duplicate targets**: if more than one placeholder in the same file resolves to the same target, convert the first (topmost) only, warn listing the rest, and leave the rest as placeholders. Never emit two managed lines for one child.

After rewriting a file's placeholders, **overwrite that page's snapshot to match the new local content** and recompute `local_hash` (it will not move, since both regexes are stripped). This mirrors the pull "Regeneration trigger" snapshot rule and keeps the snapshot consistent with disk.

Backfill changes only the **local** in-place position. The Notion child block stays where `notion-create-pages` appended it (the page foot); nsync v1 has no block-reorder operation. A subsequent `/nsync:pull` preserves the in-place managed line (UUID match, position preserved), so no duplicate appears.

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
