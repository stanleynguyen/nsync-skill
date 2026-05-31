# nsync

Claude plugin that treats your Notion workspace like a git repository: pull, edit locally, diff, commit.

## The problem

If you work on Notion docs alongside Claude, you've felt this:

- Every new chat starts with you pasting the same Notion page URLs in. Context that should be ambient becomes a chore.
- When Claude edits a doc, either it lands in Notion immediately (no preview, no rollback), or it sits in a transcript you'll lose the next time the window closes.
- Between sessions, there's no version trail. A subtle hallucination (a fabricated paragraph, a regression on a careful edit) has nothing standing between it and your team's published docs.

`nsync` makes the seam between Claude and Notion behave like git. Pull your tree once, edit locally with the tools you already use, see a real diff before changes go live, and let content hashes catch surprises before they ship.

## What you get

- **Pull once, work locally.** No more pasting Notion URLs every chat. After `/nsync:init`, the sub-tree lives next to your code as plain `.md` files. Claude works on the whole repo at once.
- **Diff before you ship.** `/nsync:diff` is a real dry-run: exact unified diff of what would land in Notion, with rich blocks rendered as placeholders so the markdown change reads clearly. Catch hallucinations before they reach your team.
- **Per-page version tracking.** Content hashes for every page; both-sides-changed lands in an interactive conflict prompt (`[L]ocal / [R]emote / [E]dit-in-scratch / [S]kip`), never a silent overwrite. Pure metadata bumps (someone tweaked a callout color) don't trigger noise.
- **Whole-tree Claude context.** Instead of fetching one Notion page per URL, Claude greps the full sync root in a single read: bigger context, fewer tokens, no losing track in the linked-doc-of-a-linked-doc rabbit hole.

## Mental model

Like `git`, but the remote is a Notion page tree.

```
my-docs/                       ← sync root (your local working dir)
├── .nsync/                    ← like .git/; local state, do not edit
│   ├── config.json            ← parent page identity
│   ├── manifest.json          ← per-page UUID + hashes
│   ├── ignore                 ← which .md files to skip
│   └── snapshots/<id>.md      ← last-synced baselines (drive safe updates)
├── welcome.md                 ← mirror of a Notion page
└── engineering/
    ├── index.md               ← body of "Engineering" page itself
    ├── standards.md
    └── onboarding.md
```

`local_hash` and `remote_hash` in the manifest are your indices. The snapshots are the staging baseline used to compute commits that touch only the markdown, never rich blocks.

## Install

### Prerequisite: Python 3

nsync shells out to a small bundled helper (`scripts/nsync.py`) for the deterministic work (content hashing, markdown normalization, diffing), so the model never does it by hand (slow, and unreliable for hashing). You need **Python 3 on your `PATH`** (`python3 --version` should print 3.x). It's standard-library only: no `pip install`, no virtualenv. macOS and most Linux distros already ship it.

### Prerequisite: connect Notion

The plugin uses Anthropic's built-in Notion connector. Connect it once before installing:

- **Claude Code (CLI / desktop app / IDE extensions):** type `/connectors` in a session (or open Settings → Connectors in the desktop app / IDE extension), select Notion, click Connect, and approve workspace access via OAuth.
- **Claude Cowork (claude.ai/code web app):** open your workspace's Connectors panel, select Notion, click Connect, and approve workspace access via OAuth.

You only do this once per workspace. No environment variables, no integration tokens, no per-page sharing; the OAuth connector handles all of that.

### Claude Code

```bash
claude plugin marketplace add stanleynguyen/nsync-skill
claude plugin install nsync@nsync
```

Restart your session so the slash commands register. Verify with `claude plugin list`; `nsync@nsync` should appear as `enabled`.

### Claude Cowork

1. Open the **Plugins** panel in your Cowork workspace.
2. **Add a marketplace** and paste `stanleynguyen/nsync-skill` (or the full URL `https://github.com/stanleynguyen/nsync-skill`).
3. Once the marketplace appears, **install the `nsync` plugin** from it.
4. Reload the workspace so the slash commands surface in `/help`.

### Local development

If you've cloned the repo locally, point the marketplace at the checked-out path:

```bash
claude plugin marketplace add /path/to/nsync-skill
claude plugin install nsync@nsync
```

## Your first sync

Five-minute walkthrough. Pick a parent page in Notion you're willing to experiment on (or create a throwaway one) and copy its URL.

```bash
mkdir my-docs && cd my-docs
```

```text
/nsync:init https://www.notion.so/workspace/My-Docs-fb1d8c3a5e214f708a239c4b6d8e1f02
```

You'll see the sub-tree mirrored into local `.md` files, plus a `.nsync/` state directory. Pages with sub-pages become folders containing `index.md` (the parent's own body) alongside the children.

Open one of the files in your editor of choice and make a small change.

```text
/nsync:status
```

Shows `Modified (local): welcome.md`. Same vocabulary as `git status`.

```text
/nsync:diff
```

This is the dry-run. Rich blocks (callouts, embeds, images) show up as `[rich block: <type>] (not synced)` placeholders so the markdown delta reads cleanly. If Claude wrote something you don't want, this is where you catch it.

```text
/nsync:commit
```

Push lands as a snippet-level update: only the markdown lines that changed get touched, every rich block stays exactly where the Notion author put it. Reload the page in Notion to confirm.

From here, just edit and commit. Open a Claude session anywhere in `my-docs/` and it has the entire tree available, no link pasting required.

## Commands

| Command                             | What it does                                                                                                                                                                                     |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `/nsync:init [url]`                 | Initialize CWD as a sync root mirroring the given Notion page. Prompts for the URL if omitted.                                                                                                   |
| `/nsync:status`                     | Show modified / added / deleted / remote-newer files. Read-only.                                                                                                                                 |
| `/nsync:diff [path...]`             | Unified diff between local files and Notion. Pass any number of file or folder paths to scope (e.g. `/nsync:diff engineering/ welcome.md`). With no args, diffs every non-clean page. Read-only. |
| `/nsync:pull`                       | Pull remote changes. Auto-merges clean-side updates; prompts per conflict.                                                                                                                       |
| `/nsync:commit [--force <path>...]` | Push local changes to Notion. Refuses if remote has unpulled changes (override with `--force`).                                                                                                  |

Depending on your Claude Code build the slash form may appear as `/nsync:init` or `/init` (with a `(plugin:nsync)` label). Check `/help` after install.

## Workflow patterns

**Iterating on a doc with Claude.** Open a session anywhere in your sync root. Ask Claude to refine `engineering/onboarding.md`. Run `/nsync:diff` to inspect what Claude changed; `/nsync:commit` when you're happy. The doc never leaves a reviewable state.

**Multi-doc refactor.** Renaming a concept across 20 pages? Use `sed -i` or have Claude do the substitution, then `/nsync:diff engineering/` to scope the dry-run to one subtree before pushing. Bulk edits Notion's UI can't do cleanly.

**PR-reviewable doc changes.** Commit `.nsync/manifest.json` and your local tree to git. The diff that lands in your pull request is human-readable markdown, not Notion's opaque block IDs; reviewers see exactly what changed, branch-based experimentation works, and rollback is a `git revert` away.

**Create a sub-page and link it in place.** A new sub-page normally lands as a Notion child block at the page foot. To place the link mid-document instead, create the `.md` and drop a placeholder line where you want it, the managed child-link form minus the page id: `[Roadmap](./roadmap.md) <!-- nsync:child -->`. On `/nsync:commit`, nsync creates the page and backfills the real id in place: no duplicate, no broken link. The same placeholder also positions a link to an already-existing child. Caveat: this sets the **local** position only; Notion still renders the child block at the page foot (in-body reordering on the Notion side is a future `/nsync:mv`).

## How it works

- **Scope:** only `*.md` files participate in sync. Any other extension is invisible to every command.
- **Layout:** Notion sub-pages map to local files, with pages-that-have-children rendered as a folder containing `index.md` (the body of that page itself) plus one `.md` per child.
- **State:** `.nsync/` at the sync root holds `config.json` (parent identity), `manifest.json` (per-page UUID + hashes + rich-block records), `ignore` (gitignore-syntax filter for `.md` files), and `snapshots/<page_id>.md` (last-synced markdown bodies that drive safe commits).
- **Rich blocks preserved.** Callouts, toggles, databases, embeds, equations, images don't round-trip through standard markdown. Rather than mangling them, `nsync` only ever updates the markdown portion of each page via snippet-level `update_content` calls; every rich block stays exactly where the Notion author put it.
- **Built for big trees.** Page reads run in parallel batches, the deterministic work (hash / normalize / diff) runs in the bundled `scripts/nsync.py` helper instead of in-context, and on larger trees the per-page fetch-and-hash work fans out to sub-agents that return only compact records. The upshot: command time tracks how fast Notion responds, not how many pages you have. Long operations report progress at least once a minute so nothing looks hung.

If you check `.nsync/` into git, ignore `.nsync/snapshots/` (large, regeneratable), `.nsync/conflicts/` (transient), and `.nsync/tmp/` (scratch space cleaned up after each run). Keep `.nsync/config.json`, `.nsync/manifest.json`, `.nsync/ignore` checked in for team visibility.

### Default ignore patterns

`/nsync:init` writes a small `.nsync/ignore` with these patterns (because non-`.md` files are already out of scope, this list filters specific markdown files only):

```
README.md
CHANGELOG.md
LICENSE.md
CONTRIBUTING.md
```

Edit `.nsync/ignore` to add your own. Syntax is gitignore-compatible.

## Limitations

- **Page trashing.** Notion's MCP toolkit has no "trash this page" call. On local file delete + commit, you pick:
  - **Orphan to workspace** (default): the page leaves the sync tree but survives at workspace level so you can trash it manually later.
  - **Manual trash:** open the page in Notion, use `···` → "Move to Trash" yourself.
  - **Restore local:** recreate the local file from snapshot; no remote action.
- **Images.** Notion-hosted image URLs in `fetch` responses are signed and expire (~1h). `nsync` deliberately does NOT export `<image>` blocks as local `![]()` markdown; they'd link-rot. Author images with external URLs (GitHub, S3, etc.) for round-trip sync.
- **Databases.** Out of scope. nsync syncs page trees, not databases. The parent URL must point to a regular page.
- **Conflict markers.** `<<<<<<<` / `=======` / `>>>>>>>` are never written into `.md` files. Conflicts resolve interactively (`[L]ocal / [R]emote / [E]dit-in-scratch / [S]kip`).
- **Concurrency.** No lock file in v1. Don't run two `nsync` commands against the same sync root simultaneously.
- **Notion's autolinker.** Bare text in your `.md` files that looks like a filename gets converted by Notion's editor into a markdown link with a placeholder URL: `welcome.md` becomes `[welcome.md](http://welcome.md)` after Notion sees it. Round-trips cleanly but you'll see the autolinked form locally after pull. If it bothers you, escape filename-like substrings (`welcome\.md`) or write them as inline code (`` `welcome.md` ``).

## Contributing

Open repo at `https://github.com/stanleynguyen/nsync-skill`. Issues and PRs welcome.

### Report bugs or request features

File an issue at https://github.com/stanleynguyen/nsync-skill/issues. Minimal reproduction beats prose; the throwaway-page recipe in the end-to-end checklist below works well as a starting template.

### Submit a PR

Open at https://github.com/stanleynguyen/nsync-skill/pulls. One concern per PR, please. For non-trivial design changes, open an issue first.

### End-to-end checklist

Run these against a throwaway Notion parent page before requesting review.

Create a Notion parent page "nsync-test" with three sub-pages:

- "Welcome": three plain paragraphs.
- "Engineering": body + two sub-pages "Standards" and "Onboarding".
- "Notes": a yellow callout, a heading, a paragraph, an inline image.

Make sure the Notion connector is connected (see Install → Prerequisite above), then run through the checklist:

1. `mkdir /tmp/nsync-test && cd /tmp/nsync-test`
2. `/nsync:init <url>`: expect `.nsync/`, `welcome.md`, `engineering/index.md`, `engineering/standards.md`, `engineering/onboarding.md`, `notes.md`.
3. `/nsync:status` → "Working tree clean."
4. Edit `welcome.md`, add a paragraph. `/nsync:diff` shows the addition. `/nsync:commit` pushes it; reload Notion and confirm.
5. In Notion, edit "Standards". Wait a few seconds. `/nsync:status` → `Remote-newer`. `/nsync:pull` → auto-merge.
6. Edit `engineering/onboarding.md` locally AND in Notion differently. `/nsync:pull` → conflict prompt. Pick `[E]dit`, resolve in the scratch buffer, then `/nsync:commit`.
7. Rename `welcome.md` → `intro.md`. `/nsync:status` → rename prompt. Confirm. `/nsync:commit`.
8. In Notion, add a new sub-page "Roadmap". `/nsync:pull` → `roadmap.md` appears locally.
9. Edit `notes.md`'s markdown portion locally. `/nsync:diff` shows only that hunk plus `[rich block: callout] (not synced)`. `/nsync:commit` → push; reload Notion and confirm the callout is still present.
10. Delete `notes.md`. `/nsync:commit` → orphan / manual-trash / restore prompt. Pick orphan.

If all 10 steps behave as expected, your change is ready for review.
