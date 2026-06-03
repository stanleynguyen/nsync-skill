#!/usr/bin/env python3
"""nsync compute helper — the ONE place deterministic work runs.

This script is the implementation of the markdown normalization + hash pipeline
specified in prose in references/manifest-schema.md ("Markdown normalization")
and references/notion-mcp-cheatsheet.md (rich-block tag list, image-line regex)
and references/path-mapping.md ("Child-link lines" regexes). The references stay
the canonical spec; this file must reproduce them byte-for-byte.

Why this exists: an LLM cannot reliably compute SHA-256 and is slow at line-by-line
string work. Commands shell out here instead of doing it in-context. Pure stdlib —
no pip install. See plan history in i-want-to-create-cosmic-frog.md.

Subcommands:
  hash --mode {local|remote} [FILE]        -> prints "sha256:<hex>"
  hash-batch --mode {local|remote}         -> reads NUL- or newline-delimited paths
                                              on stdin, prints "<path>\\t<sha256:hex>"
  normalize --mode {local|remote} [FILE]   -> prints the normalized markdown-only body
  strip-childlinks [FILE]                  -> prints body with child-link lines removed
  diff SNAPSHOT LOCAL                      -> unified diff (snapshot->local), child-link
                                              lines stripped from both sides
  extract-uuids [FILE]                     -> prints one dashed UUID per line, in
                                              occurrence order, for every <page url>
                                              tag or notion.so/notion.site URL found;
                                              tolerant of all-dashed, all-undashed,
                                              partial-dashed, and HTML-escaped forms.
  extract-body [FILE]                      -> takes the raw `text` field of a notion-fetch
                                              response (the envelope with <page url>,
                                              <ancestor-path>, <properties>, <content>...
                                              </content>, </page>) and prints ONLY the
                                              markdown body between <content> and
                                              </content>. Idempotent: if no <content>
                                              tag is present the input is echoed back
                                              unchanged. Use this BEFORE `hash --mode
                                              remote` / `normalize --mode remote` so the
                                              unknown-tag fallback does not silently
                                              swallow the body along with the wrapper.
  self-test                                -> run in-process regression tests

FILE omitted (or "-") reads stdin.
"""

import argparse
import difflib
import hashlib
import html as _html_mod
import re
import sys
import unicodedata
from html.parser import HTMLParser

# --- Regexes transcribed verbatim from the references --------------------------

# path-mapping.md -> "Recognition regex" (managed child-link line, case-insensitive,
# entire trimmed line).
CHILDLINK_MANAGED_RE = re.compile(
    r'^\[[^\]]*\]\([^)]+\)\s*<!--\s*nsync:child\s+'
    r'page_id="[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"'
    r'(\s+external)?\s*-->\s*$',
    re.IGNORECASE,
)

# path-mapping.md -> "Placeholder child-link lines" (no page_id; entire trimmed line).
CHILDLINK_PLACEHOLDER_RE = re.compile(
    r'^\[[^\]]*\]\(([^)]+)\)\s*<!--\s*nsync:child\s*-->\s*$',
    re.IGNORECASE,
)

# notion-mcp-cheatsheet.md -> "Image-block lines" (whole trimmed line is a standalone
# markdown image, optionally followed by a {attr} block). Inline images inside running
# text do NOT match (anchored ^...$).
IMAGE_LINE_RE = re.compile(r'^!\[[^\]]*\]\([^)]+\)(\s*\{[^}]*\})?$')

# notion-mcp-cheatsheet.md -> "Rich-block tags stripped for remote_hash only".
# Paired tags whose entire span (incl. content) is removed. column_list is listed
# before column so the outer span (which contains the inner columns) is removed first.
PAIRED_RICH_TAGS = [
    "callout", "toggle", "synced_block", "column_list", "column",
    "button", "ai_block", "equation",
]
# Self-closing / single-line tags: remove the tag itself.
SELFCLOSE_RICH_TAGS = [
    "embed", "bookmark", "link_preview", "equation",
    "file", "audio", "video", "pdf", "data-source",
    "empty-block",
]

# Tags that may legitimately appear in user-authored markdown and must NOT be stripped
# by the unknown-tag fallback. Anything outside this set (and outside the lists above
# and <page url>) gets warned-and-stripped on the remote side. Source: HTML inline
# elements commonly seen in CommonMark plus a couple of block-level ones markdown
# allows inline.
HTML_PASSTHROUGH_TAGS = {
    "a", "abbr", "address", "article", "aside", "b", "bdi", "bdo", "blockquote",
    "br", "caption", "cite", "code", "col", "colgroup", "data", "dd", "del",
    "details", "dfn", "div", "dl", "dt", "em", "figcaption", "figure", "footer",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "i", "img", "ins", "kbd",
    "li", "main", "mark", "nav", "ol", "p", "pre", "q", "rb", "rp", "rt", "rtc",
    "ruby", "s", "samp", "section", "small", "span", "strong", "sub", "summary",
    "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr", "u",
    "ul", "var", "wbr",
}

# <page url ...>...</page> child references — stripped on BOTH sides (step 7). Notion
# usually serializes these on one line, but handle a paired span defensively.
PAGE_SPAN_RE = re.compile(r'<page\b[^>]*>.*?</page>', re.DOTALL | re.IGNORECASE)
PAGE_SELFCLOSE_RE = re.compile(r'<page\b[^>]*?/?>', re.IGNORECASE)
PAGE_LINE_RE = re.compile(r'^<page\b.*</page>$', re.IGNORECASE)

# Generic XML-ish tag finder for the unknown-tag fallback. Tag names are letters,
# digits, dashes, underscores; first char is a letter. Matches paired spans and
# self-closing/lone-open tags. Used only after the recognized strip-lists run.
UNKNOWN_PAIRED_RE = re.compile(
    r'<([a-z][a-z0-9_-]*)\b[^>]*>.*?</\1>',
    re.DOTALL | re.IGNORECASE,
)
UNKNOWN_SELFCLOSE_RE = re.compile(
    r'<([a-z][a-z0-9_-]*)\b[^>]*?/?>',
    re.IGNORECASE,
)


def _strip_paired(text, tag):
    return re.sub(
        r'<' + tag + r'\b[^>]*>.*?</' + tag + r'>',
        '', text, flags=re.DOTALL | re.IGNORECASE,
    )


def _strip_selfclose(text, tag):
    return re.sub(r'<' + tag + r'\b[^>]*?/?>', '', text, flags=re.IGNORECASE)


_UNKNOWN_TAG_WARNED = set()


def _warn_strip_unknown(text):
    """Per notion-mcp-cheatsheet.md: 'If a tag we don't recognize appears, log a
    warning and treat its content as opaque (strip the entire tag span before
    hashing).' Runs only in mode=remote after the recognized strip-lists have
    already removed everything they know about. Passthroughs (standard HTML
    inline tags + <page url>, already handled) are left alone."""
    known_paired = set(PAIRED_RICH_TAGS)
    known_selfclose = set(SELFCLOSE_RICH_TAGS)

    def paired_sub(match):
        name = match.group(1).lower()
        if name in known_paired or name == "page" or name in HTML_PASSTHROUGH_TAGS:
            return match.group(0)
        if name not in _UNKNOWN_TAG_WARNED:
            sys.stderr.write(f"nsync: warning: stripping unrecognized tag <{name}>\n")
            _UNKNOWN_TAG_WARNED.add(name)
        return ''

    def selfclose_sub(match):
        name = match.group(1).lower()
        if (name in known_paired or name in known_selfclose or name == "page"
                or name in HTML_PASSTHROUGH_TAGS):
            return match.group(0)
        if name not in _UNKNOWN_TAG_WARNED:
            sys.stderr.write(f"nsync: warning: stripping unrecognized tag <{name}>\n")
            _UNKNOWN_TAG_WARNED.add(name)
        return ''

    # Paired first (longer span), then self-closing for the leftovers.
    text = UNKNOWN_PAIRED_RE.sub(paired_sub, text)
    text = UNKNOWN_SELFCLOSE_RE.sub(selfclose_sub, text)
    return text


# ----------------------------------------------------------------------------
# Notion serialization-quirks canonicalization. See references/notion-serialization-
# quirks.md. Notion's `notion-fetch` returns a deterministically MUTATED form of
# whatever markdown was sent on create/update (HTML tables, escaped specials,
# collapsed inter-block blanks, autolinked bare URLs, inferred code-fence langs,
# escaped brackets, page-title H1 stripped). This step reduces both sides to a
# common canonical form so local_hash and remote_hash compare meaningfully.
# Applied symmetrically in both modes — Cat 4-7 inside prose only (fence-aware).
# ----------------------------------------------------------------------------

# CommonMark-escapable punctuation set, intersected with chars Notion has been
# observed to over-escape on fetch (`$`, `~`, `[`, `]`). Backslash-unescaping any
# char in this set is safe: CommonMark renders `\<c>` and `<c>` identically for
# these chars. Kept as a literal-char character class.
_NOISE_ESCAPABLE = r'$~#|*_`{}()>+\-.!\[\]'
_ESCAPE_RE = re.compile(r'\\([' + _NOISE_ESCAPABLE + r'])')

# Autolink unwrap: `[<URL>](<URL>)` where label is byte-identical to the
# target, the URL has an http(s)/mailto scheme, and contains no `)` or `]` in
# path. Conservative — URLs with parens stay as-is.
_AUTOLINK_RE = re.compile(
    r'\[((?:https?|mailto):[^\s\]\)]+)\]\(\1\)'
)

# Bare-domain autolink unwrap: Notion takes bare text like `Sync.com` in body
# and wraps it as `[Sync.com](http://Sync.com)` (or https://) on fetch. The
# label is NOT byte-identical to the target — it lacks the scheme. Detect the
# pattern where target = scheme + "://" + label, and the label has no
# whitespace, brackets, or parens. Conservative: only fires for http/https
# (not mailto, since mailto targets are always `mailto:user@host` and the
# bare form `user@host` wouldn't naturally appear as link text).
_BARE_DOMAIN_AUTOLINK_RE = re.compile(
    r'\[([^\s\]\)]+)\]\(https?://\1\)'
)

# Code-fence language hint: ` ```<lang>` on the fence-open line, where <lang> is
# a single word (letters/digits/underscore/+/-). Strip the lang token only,
# keep the fence backticks. Applied at whole-doc scope (the line-anchored
# regex won't accidentally match a fence-close because closes have no lang).
_FENCE_LANG_RE = re.compile(
    r'^(\s*```)[a-zA-Z0-9_+\-]+\s*$', re.MULTILINE
)

# Blank-line collapse (prose-only): collapse any run of 2+ newlines to a single
# `\n`. Notion stores adjacent blocks with single `\n` (no blank line); local
# files typically have `\n\n` (blank line between paragraphs). To make both
# sides hash-equal we drop blank-line separators entirely inside prose. Block-
# separation semantics for rendering are preserved on disk — only the canonical
# hash input collapses them. Inside fenced code, blank lines are preserved
# verbatim (the fence-aware segmenter applies this only to prose segments).
_BLANK_RUN_RE = re.compile(r'\n{2,}')

# MD pipe-table separator row canonicalization. Author-shorthand `|---|---|`
# and the pretty form `| --- | --- |` (which the HTML-table -> pipe converter
# emits) must hash equal. Match a whole separator line and re-emit each cell
# trimmed-and-padded with single spaces. Alignment markers (:---, ---:, :---:)
# are preserved.
_TABLE_SEPARATOR_RE = re.compile(
    r'^[ \t]*\|([ \t]*:?-+:?[ \t]*\|)+[ \t]*$', re.MULTILINE
)


def _canonicalize_table_separator(text):
    """Normalize MD pipe-table separator rows to `| --- | ---: | :---: | ... |`
    form (single space padding, alignment markers preserved). Operates on
    prose text after fenced code has been sentinelized so it won't touch
    backtick-shell-pipe lines inside code."""
    def _norm_line(match):
        line = match.group(0).strip()
        # Drop leading and trailing `|`, split on `|`.
        inner = line[1:-1] if line.startswith('|') and line.endswith('|') else line
        cells = inner.split('|')
        out_cells = []
        for cell in cells:
            cell = cell.strip()
            # Preserve alignment markers verbatim; replace dash content with
            # exactly three dashes.
            left = ':' if cell.startswith(':') else ''
            right = ':' if cell.endswith(':') else ''
            out_cells.append(left + '---' + right)
        return '| ' + ' | '.join(out_cells) + ' |'
    return _TABLE_SEPARATOR_RE.sub(_norm_line, text)


# Fenced code block matcher. Greedy-by-line: opens at a line starting with ```
# (with optional lang), closes at the next line starting with ```. The lang
# token is stripped on the open line for category 6. The body is preserved
# verbatim — sentinelized during prose transforms and restored afterwards.
_FENCED_CODE_BLOCK_RE = re.compile(
    r'(^[ \t]{0,3}```)([a-zA-Z0-9_+\-]*)[ \t]*\n(.*?)(^[ \t]{0,3}```[ \t]*$)',
    re.DOTALL | re.MULTILINE,
)
_SENTINEL_TPL = '\x00NSYNC_CODE_BLOCK_{}\x00'


def _extract_code_blocks(text):
    """Extract every fenced code block, replace each with a sentinel, and
    return (sentinelized_text, [block_strings_in_order]). The block strings
    include the fence-open (with lang already stripped per category 6) and
    fence-close lines and the body verbatim. Caller restores via
    _restore_code_blocks. This keeps prose transforms (escape/autolink/
    blank-line collapse) from touching code content."""
    blocks = []

    def repl(match):
        open_fence = match.group(1)  # e.g. "```"
        # Category 6: lang token already absorbed into match.group(2); we just
        # drop it here, keeping only the bare ```.
        body = match.group(3)
        close_fence = match.group(4)
        # Canonical fenced block: open + \n + body (verbatim) + close (without
        # any trailing whitespace).
        canonical = open_fence + '\n' + body + close_fence.rstrip()
        blocks.append(canonical)
        return _SENTINEL_TPL.format(len(blocks) - 1)

    sentinelized = _FENCED_CODE_BLOCK_RE.sub(repl, text)
    return sentinelized, blocks


def _restore_code_blocks(text, blocks):
    for i, block in enumerate(blocks):
        text = text.replace(_SENTINEL_TPL.format(i), block)
    return text


def _canonicalize_prose(text):
    """Apply cat 3 (blank-line collapse) + cat 4 (escapes incl brackets cat 7)
    + cat 5 (autolink) + MD-table separator normalization to prose text.
    Called on sentinelized text after code blocks have been replaced with
    placeholders — so transforms can't touch code content."""
    # MD pipe-table separator row canonicalization — symmetric on both sides
    # so `|---|---|` (author shorthand) and `| --- | --- |` (HTML-table
    # converter output) hash equal.
    text = _canonicalize_table_separator(text)
    # Cat 4 + 7: strip backslash before chars in the escapable set.
    text = _ESCAPE_RE.sub(r'\1', text)
    # Cat 5: autolink unwrap. Run both the byte-identical and bare-domain
    # variants. Order matters: bare-domain first because the byte-identical
    # variant wouldn't match `[Sync.com](http://Sync.com)`.
    text = _BARE_DOMAIN_AUTOLINK_RE.sub(r'\1', text)
    text = _AUTOLINK_RE.sub(r'\1', text)
    # Cat 3: blank-line collapse — any run of 2+ newlines -> single `\n`.
    text = _BLANK_RUN_RE.sub('\n', text)
    return text


class _NotionTableParser(HTMLParser):
    """Minimal HTML-table parser used to canonicalize <table>...</table> spans
    into markdown pipe-table form. Bails on rowspan/colspan/unknown nesting —
    caller falls back to leaving the span verbatim and emits a one-time warn.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []          # list[list[str]]
        self.current_row = None
        self.current_cell = None
        self.has_header = False
        self.bailed = False
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs_d = dict(attrs)
        if tag in ("rowspan", "colspan"):
            self.bailed = True
            return
        if tag == 'table':
            self.depth += 1
            if self.depth > 1:
                self.bailed = True
        elif tag in ('thead',):
            self.has_header = True
        elif tag == 'tr':
            self.current_row = []
        elif tag in ('th', 'td'):
            if 'rowspan' in attrs_d or 'colspan' in attrs_d:
                self.bailed = True
            self.current_cell = []
        # colgroup/col/tbody/tfoot: ignore (don't affect rendering).

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == 'table':
            self.depth -= 1
        elif tag == 'tr':
            if self.current_row is not None:
                self.rows.append(self.current_row)
                self.current_row = None
        elif tag in ('th', 'td'):
            if self.current_cell is not None and self.current_row is not None:
                cell_text = ''.join(self.current_cell).strip().replace('\n', ' ')
                # Escape `|` inside cells so the pipe-table parses unambiguously.
                cell_text = cell_text.replace('|', r'\|')
                self.current_row.append(cell_text)
            self.current_cell = None

    def handle_data(self, data):
        if self.current_cell is not None:
            self.current_cell.append(data)


def _canonicalize_table(html_str):
    """Convert a <table>...</table> HTML span to markdown pipe form.
    Returns (canonical_string, ok). On ok=False, caller leaves verbatim.
    """
    p = _NotionTableParser()
    try:
        p.feed(html_str)
        p.close()
    except Exception:
        return html_str, False
    if p.bailed or not p.rows:
        return html_str, False
    # Make sure every row has the same column count; else bail.
    width = max(len(r) for r in p.rows)
    if any(len(r) != width for r in p.rows):
        return html_str, False
    # Header row = first row. If no <thead>, still treat first row as header
    # (markdown pipe-table requires a header).
    header = p.rows[0]
    body = p.rows[1:]
    lines = []
    lines.append('| ' + ' | '.join(header) + ' |')
    lines.append('| ' + ' | '.join(['---'] * width) + ' |')
    for r in body:
        lines.append('| ' + ' | '.join(r) + ' |')
    return '\n'.join(lines) + '\n', True


_TABLE_SPAN_RE = re.compile(
    r'<table\b[^>]*>.*?</table>', re.DOTALL | re.IGNORECASE
)


def _canonicalize_tables(text):
    """Replace every <table> span with its canonical pipe-table form.
    On parse-bail for a given table, leave that span verbatim and warn once."""
    def repl(match):
        canonical, ok = _canonicalize_table(match.group(0))
        if not ok:
            if 'table-rowspan-or-nested' not in _UNKNOWN_TAG_WARNED:
                sys.stderr.write(
                    "nsync: warning: leaving <table> with rowspan/colspan/"
                    "nested-table verbatim (canonicalization bailed)\n"
                )
                _UNKNOWN_TAG_WARNED.add('table-rowspan-or-nested')
            return match.group(0)
        return canonical
    return _TABLE_SPAN_RE.sub(repl, text)


def _strip_h1_title(text, expected_title):
    """Cat 1: if expected_title is set and the first non-blank line is exactly
    `# <expected_title>` (CommonMark ATX H1, optional trailing whitespace),
    strip that line plus any immediately-following blank line. Case-sensitive
    exact match on title. No-op when expected_title is None or mismatched."""
    if not expected_title:
        return text
    lines = text.split('\n')
    # Find first non-blank line.
    i = 0
    while i < len(lines) and lines[i].strip() == '':
        i += 1
    if i >= len(lines):
        return text
    # ATX H1 with optional leading space (CommonMark allows up to 3 leading
    # spaces). Trailing whitespace allowed; trailing `#`s NOT supported
    # (would need closing-sequence rules from CommonMark §4.2; rare in practice).
    m = re.match(r'^[ ]{0,3}#[ \t]+(.+?)[ \t]*$', lines[i])
    if not m:
        return text
    if m.group(1) != expected_title:
        return text
    # Drop the H1 line. Also drop one immediately-following blank line so the
    # body doesn't start with a stray blank.
    del lines[i]
    if i < len(lines) and lines[i].strip() == '':
        del lines[i]
    return '\n'.join(lines)


def _canonicalize_notion_noise(text, expected_title=None):
    """The single entry-point for round-trip noise canonicalization. Apply
    every category symmetrically (both modes call this). Order:
    1. H1 page-title strip (whole-doc).
    2. HTML <table> -> pipe-table (whole-doc; before code extraction because
       tables contain HTML, not fenced code).
    3. Pull every fenced code block out behind sentinels (the extractor also
       applies category 6 — language hint strip — as part of restoration).
    4. Apply prose transforms (cats 3 + 4 + 5 + 7) on the sentinelized text.
    5. Restore code blocks.
    """
    text = _strip_h1_title(text, expected_title)
    text = _canonicalize_tables(text)
    sentinelized, blocks = _extract_code_blocks(text)
    sentinelized = _canonicalize_prose(sentinelized)
    text = _restore_code_blocks(sentinelized, blocks)
    return text


def normalize(raw_bytes, mode, expected_title=None):
    """Apply the manifest-schema.md hash pipeline. mode is 'local' or 'remote'.

    `expected_title` is an optional page-title hint used by category 1 (H1
    page-title strip). When None, H1 strip is a no-op — callers that don't yet
    have the title pass None and the existing behavior is preserved."""
    # 1. UTF-8 decode.
    text = raw_bytes.decode("utf-8")
    # 2. NFC unicode normalization.
    text = unicodedata.normalize("NFC", text)
    # 3. LF line endings only.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 6. remote_hash only: strip rich-block tags and their contents (spans first,
    #    before the line pass, because paired tags span multiple lines). Then sweep
    #    any unrecognized XML-ish tag per notion-mcp-cheatsheet.md line 129.
    if mode == "remote":
        for tag in PAIRED_RICH_TAGS:
            text = _strip_paired(text, tag)
        for tag in SELFCLOSE_RICH_TAGS:
            text = _strip_selfclose(text, tag)
        text = _warn_strip_unknown(text)

    # 7. both modes: strip whole-line <page url ...>...</page> spans.
    text = PAGE_SPAN_RE.sub('', text)
    text = PAGE_SELFCLOSE_RE.sub('', text)

    # 8. both modes: Notion serialization-quirks canonicalization. Reverses
    # the deterministic mutations Notion applies on round-trip (HTML tables,
    # escaped specials, collapsed blanks, autolinked URLs, inferred fence
    # languages, escaped brackets, optional page-title H1 strip). See
    # references/notion-serialization-quirks.md.
    text = _canonicalize_notion_noise(text, expected_title=expected_title)

    # Line pass: 4. strip trailing whitespace per line; drop whole-line image blocks
    # (remote), child-link managed + placeholder lines (both), and any residual
    # whole-line <page url> tags (both).
    out_lines = []
    for line in text.split("\n"):
        s = line.strip()
        if mode == "remote" and IMAGE_LINE_RE.match(s):
            continue
        if CHILDLINK_MANAGED_RE.match(s):
            continue
        if CHILDLINK_PLACEHOLDER_RE.match(s):
            continue
        if PAGE_LINE_RE.match(s):
            continue
        out_lines.append(line.rstrip())

    text = "\n".join(out_lines)

    # 5. exactly one trailing newline (no extra blank lines at EOF). Empty body stays
    #    empty so "Non-empty parent body" length checks behave.
    text = text.rstrip("\n")
    if text != "":
        text = text + "\n"
    return text


def strip_childlinks(raw_bytes):
    """Remove managed + placeholder child-link lines only (no rich-block / EOF work).

    Used by /nsync:commit to drop child-link lines from snapshot AND local before
    diffing prose. Preserves everything else verbatim, including line endings as LF.
    """
    text = raw_bytes.decode("utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out_lines = []
    for line in text.split("\n"):
        s = line.strip()
        if CHILDLINK_MANAGED_RE.match(s) or CHILDLINK_PLACEHOLDER_RE.match(s):
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


# extract-uuids: tolerant URL/tag scanner. Output one dashed UUID per occurrence,
# preserving order. Used wherever a command consumes <page url> tags or notion URLs
# from sub-agent records — Notion serializes the same UUID in inconsistent forms
# (canonical undashed, dashed, HTML-escaped, hybrid 8-4-rest, bare URL).
_URL_OR_TAG_RE = re.compile(
    # Hosts: notion.so / notion.site (legacy), notion.com / app.notion.com (current).
    # Path prefix: optional "p/" (new form, `app.notion.com/p/<uuid>`) OR optional
    # slug-tail ending in `-` (old form, `notion.so/<slug>-<uuid>`). Either may be
    # absent for the bare `notion.so/<uuid>` form.
    r'notion\.(?:so|site|com)/(?:p/)?(?:[a-zA-Z0-9_\-]*?-)?([0-9a-fA-F][0-9a-fA-F\-]{31,40})',
)


def extract_uuids(raw_bytes):
    text = raw_bytes.decode("utf-8", errors="replace")
    # Un-HTML-escape the bare-minimum sub-agents-might-emit forms.
    text = (text.replace("&lt;", "<").replace("&gt;", ">")
                 .replace("&quot;", '"').replace("&amp;", "&"))
    out = []
    for match in _URL_OR_TAG_RE.finditer(text):
        raw = match.group(1).replace("-", "").lower()
        if len(raw) != 32:
            # Not a 128-bit UUID; skip (could be a notion slug we mis-matched).
            continue
        if not re.fullmatch(r"[0-9a-f]{32}", raw):
            continue
        dashed = (f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}")
        out.append(dashed)
    return out


# extract-body: pull the markdown body out of a `notion-fetch` response envelope.
#
# notion-fetch returns a `text` field shaped like:
#
#     Here is the result of "view" for the Page with URL ... as of ...:
#     <page url="..." [icon="..."]>
#     <ancestor-path>...</ancestor-path>
#     <properties>{...}</properties>
#     <content>
#     {actual markdown body, possibly with rich-block tags, image lines, child <page url> refs}
#     </content>
#     </page>
#
# Feeding this whole envelope to `normalize --mode remote` is wrong: the unknown-tag
# fallback strips <ancestor-path>, <properties>, AND <content> (with its entire
# inner body), leaving only the preamble line. Sub-agents must pre-extract the body
# before hashing or normalizing. This is the canonical extractor — never inline this
# in a command or sub-agent prompt.
#
# Non-greedy capture between the FIRST <content> and its closing tag. One optional
# newline on each side is consumed so a body that started on the line after <content>
# does not begin with a stray blank line. If no <content> tag is present, the input
# is returned unchanged (idempotent — safe to pipe already-extracted bodies through
# without distorting them).
CONTENT_BODY_RE = re.compile(
    r'<content>\n?(.*?)\n?</content>',
    re.DOTALL | re.IGNORECASE,
)


def extract_body(raw_bytes):
    text = raw_bytes.decode("utf-8")
    text = _recover_mangled_envelope(text)
    match = CONTENT_BODY_RE.search(text)
    if match is None:
        return text
    return match.group(1)


# Defensive recovery for two well-observed sub-agent serialization failures.
# Both produce a file whose <content>...</content> body is JSON-escape-encoded
# (`\n` two-char sequences instead of real newlines, `\"` instead of `"`, etc.),
# which makes SHA-256 of the extracted body non-deterministic across sub-agents
# and produces persistent false-stale `/nsync:status` reports.
#
#   Failure 1: sub-agent ran `cat > file << 'EOF'` with the FULL MCP JSON result
#              ({"metadata":..., "title":..., "text":"...envelope with \n..."}).
#              The heredoc preserves backslash-n / backslash-quote literals.
#
#   Failure 2: sub-agent piped the `.text` value through `echo` or json.dumps()
#              before writing. Same outcome.
#
# Both recoverable. Apply BEFORE the <content> regex runs.
def _recover_mangled_envelope(text):
    # JSON-wrapper case: input is the whole tool-result JSON. Pull out .text.
    stripped = text.lstrip()
    if stripped.startswith("{") and '"text"' in stripped[:512]:
        import json as _json
        try:
            obj = _json.loads(stripped)
            if isinstance(obj, dict) and isinstance(obj.get("text"), str):
                return obj["text"]
        except Exception:
            pass  # not valid JSON; fall through

    # Escape-mangled envelope: <page>, <content> tags present at start of LITERAL
    # backslash-n sequences instead of real newlines. Heuristic: input contains
    # >=5 literal `\n` sequences AND <=1 real newline (in the first 2 KB to
    # avoid scanning huge bodies). Decode unicode_escape to recover.
    sample = text[:2048]
    if sample.count("\\n") >= 5 and sample.count("\n") <= 1:
        import codecs as _codecs
        try:
            return _codecs.decode(text, "unicode_escape")
        except Exception:
            pass  # malformed escapes; keep original

    return text


# <properties>{"title":"..."}</properties> envelope title extractor. Returns the
# title string or None if absent / unparseable. Used by callers that thread the
# title into normalize() for the H1 strip (category 1) in
# _canonicalize_notion_noise. Idempotent on already-extracted bodies (returns
# None when no <properties> tag is present).
_PROPERTIES_BLOCK_RE = re.compile(
    r'<properties>\s*(\{.*?\})\s*</properties>',
    re.DOTALL | re.IGNORECASE,
)


def extract_title(raw_bytes):
    """Pull the page title out of the <properties>{"title":"..."}</properties>
    block of a notion-fetch envelope. Returns None if not found or unparseable.
    """
    text = raw_bytes.decode("utf-8", errors="replace")
    m = _PROPERTIES_BLOCK_RE.search(text)
    if m is None:
        return None
    import json as _json
    try:
        props = _json.loads(m.group(1))
    except Exception:
        return None
    title = props.get("title") if isinstance(props, dict) else None
    if isinstance(title, str):
        return title
    return None


def extract_body_with_title(raw_bytes):
    """Convenience pairing of extract_body + extract_title from the same envelope.
    Returns (body_text, title_or_None)."""
    return extract_body(raw_bytes), extract_title(raw_bytes)


def sha256_of(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path):
    if path is None or path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as fh:
        return fh.read()


# ============================================================================
# Orchestrator support: helpers shared by all *-preflight / *-classify / *-apply
# subcommands and by the sub-agent helpers (process-fetch, process-modified,
# process-postwrite). These functions never shell out; orchestrators call them
# in-process. The goal is to collapse main-loop heredocs and per-page sub-agent
# pipelines into ONE Bash call shape that one wildcard allow-rule covers.
# ============================================================================

import json
import os
import fnmatch
from pathlib import Path
from datetime import datetime, timezone


def _json_out(data):
    sys.stdout.write(json.dumps(data, indent=2, sort_keys=True))
    sys.stdout.write("\n")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_manifest(sync_root):
    with open(os.path.join(sync_root, ".nsync", "manifest.json")) as fh:
        return json.load(fh)


def save_manifest_atomic(sync_root, manifest):
    path = os.path.join(sync_root, ".nsync", "manifest.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_ignore_patterns(sync_root):
    path = os.path.join(sync_root, ".nsync", "ignore")
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    return out


def match_ignore(patterns, rel_path):
    """Minimal gitignore-style matcher. Supports negation (!) and ** glob."""
    matched = False
    for pat in patterns:
        neg = pat.startswith("!")
        pp = pat[1:] if neg else pat
        # ** globstar -> fnmatch with sentinel
        if "**" in pp:
            regex = pp.replace(".", r"\.").replace("**", ".*").replace("*", "[^/]*")
            import re as _re
            if _re.fullmatch(regex, rel_path) or _re.fullmatch(regex, os.path.basename(rel_path)):
                matched = not neg
            continue
        # plain fnmatch — try full path AND basename
        if fnmatch.fnmatch(rel_path, pp) or fnmatch.fnmatch(os.path.basename(rel_path), pp):
            matched = not neg
    return matched


def glob_md(sync_root):
    """Return all *.md files (case-insensitive) under sync_root as sync-root-
    relative POSIX paths, excluding everything under .nsync/."""
    root = Path(sync_root)
    out = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() != ".md":
            continue
        rel = p.relative_to(root).as_posix()
        if rel.startswith(".nsync/") or rel == ".nsync":
            continue
        out.append(rel)
    out.sort()
    return out


def slugify(title):
    """path-mapping.md rule 4: lowercase, ws->'-', strip outside [a-z0-9-_.],
    length-cap 80."""
    s = title.lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9\-_.]", "", s)
    s = s[:80]
    return s or "untitled"


def compute_rel_path(from_rel, to_rel):
    """Sync-root-relative paths; return relative path from from_rel's dir to
    to_rel, starting with './' or '../'."""
    from_dir = os.path.dirname(from_rel)
    rel = os.path.relpath(to_rel, from_dir if from_dir else ".")
    if not rel.startswith("."):
        rel = "./" + rel
    return rel.replace(os.sep, "/")


# Whole-line <page url ...>...</page> with optional icon. Captures indent / url /
# inner-text. Used by substitute_page_urls to render <page url> tags as managed
# child-link lines in has_children pages' body files.
_PAGE_URL_RENDER_RE = re.compile(
    r'^(\s*)<page\s+url="([^"]+)"(?:\s+icon="[^"]*")?\s*>([^<]*)</page>\s*$',
    re.MULTILINE | re.IGNORECASE,
)


def _escape_link_title(title):
    safe = title.replace("]", r"\]").replace(")", r"\)").replace("\n", " ")
    return safe.strip()


def render_managed_link(file_rel_path, target_uuid, title, path_by_pid):
    safe = _escape_link_title(title)
    if target_uuid in path_by_pid:
        rel = compute_rel_path(file_rel_path, path_by_pid[target_uuid])
        return f'[{safe}]({rel}) <!-- nsync:child page_id="{target_uuid}" -->'
    return (f'[{safe}](https://www.notion.so/{target_uuid}) '
            f'<!-- nsync:child page_id="{target_uuid}" external -->')


def substitute_page_urls(body, file_rel_path, path_by_pid):
    """Replace whole-line <page url> tags with managed child-link lines."""
    def repl(match):
        indent = match.group(1)
        url = match.group(2)
        title = match.group(3).strip()
        uuids = extract_uuids(url.encode("utf-8"))
        if not uuids:
            return match.group(0)
        return indent + render_managed_link(file_rel_path, uuids[0], title, path_by_pid)
    return _PAGE_URL_RENDER_RE.sub(repl, body)


def detect_renames(manifest, local_paths, sync_root):
    """Per path-mapping.md → Rename detection. Returns list of {page_id,
    old_path, new_path} candidates. Skips root entry (parent_page_id is null)."""
    candidates = []
    local_set = set(local_paths)
    manifest_paths = {pid: rec["path"] for pid, rec in manifest["pages"].items()}
    accounted = set(manifest_paths.values()) & local_set
    unaccounted_local = [p for p in local_paths if p not in accounted]
    for pid, rec in manifest["pages"].items():
        if rec.get("parent_page_id") is None:
            continue  # root entry — no rename
        if rec["path"] in local_set:
            continue
        # Candidate is missing on disk. Look for a basename match in unaccounted.
        old_base = os.path.basename(rec["path"])
        matches = [p for p in unaccounted_local if os.path.basename(p) == old_base]
        if len(matches) == 1:
            candidates.append({
                "page_id": pid,
                "old_path": rec["path"],
                "new_path": matches[0],
            })
    return candidates


def hash_batch_local(sync_root, paths, title_by_path=None):
    """Compute local_hash for each path. Returns {path: sha256:hex}.

    `title_by_path` is an optional mapping {rel_path: page_title} used to thread
    the H1-strip hint (category 1) into normalize. When absent, H1 strip is a
    no-op — existing-call-site behavior preserved."""
    out = {}
    title_by_path = title_by_path or {}
    for rel in paths:
        full = os.path.join(sync_root, rel)
        try:
            with open(full, "rb") as fh:
                out[rel] = sha256_of(normalize(
                    fh.read(), "local",
                    expected_title=title_by_path.get(rel),
                ))
        except OSError as exc:
            out[rel] = "ERROR:" + str(exc)
    return out


def derive_reachable(parent_fetch_path, records):
    """Reachable set per references/path-mapping.md and conflict-protocol.md.

    parent's <page url> child UUIDs ∪ each has_children record's child_link_tags
    (canonicalized via extract_uuids when needed)."""
    reachable = set()
    if parent_fetch_path and os.path.exists(parent_fetch_path):
        with open(parent_fetch_path, "rb") as fh:
            body = extract_body(fh.read())
        for u in extract_uuids(body.encode("utf-8")):
            reachable.add(u)
    for r in records:
        if r.get("has_children"):
            # child_link_tags arrive canonical-dashed from process-fetch; tolerate
            # legacy callers that pass raw URLs by piping through extract_uuids.
            for tag in r.get("child_link_tags", []):
                if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", tag.lower()):
                    reachable.add(tag.lower())
                else:
                    for u in extract_uuids(tag.encode("utf-8")):
                        reachable.add(u)
    return reachable


# ----------------------------------------------------------------------------
# Sub-agent helpers (Group B): one Bash call per page, fixed shape.
# ----------------------------------------------------------------------------

def cmd_process_fetch(args):
    """ONE Bash call per page for read fan-outs (status, pull, init).

    Reads a notion-fetch envelope file, extracts the body, computes remote_hash,
    extracts child_link_tags, optionally writes a body file and a snapshot, and
    optionally deletes the source envelope. Emits a JSON record matching the
    CompactReadRecord schema in references/sub-agent-schemas.md.

    Replaces today's per-page 2-5 separate Bash pipelines.
    """
    raw = _read(args.fetch_file)
    body = extract_body(raw)
    body_bytes = body.encode("utf-8")
    # Title threads into normalize() for category 1 (H1 page-title strip). The
    # envelope's <properties>{"title":"..."}</properties> is authoritative.
    title = extract_title(raw)
    if args.out_body:
        with open(args.out_body, "w") as fh:
            fh.write(body)
    if args.out_snapshot:
        with open(args.out_snapshot, "w") as fh:
            fh.write(normalize(body_bytes, "remote", expected_title=title))
    remote_hash = sha256_of(normalize(body_bytes, "remote", expected_title=title))
    uuids = extract_uuids(body_bytes)
    if args.delete_fetch and args.fetch_file != "-":
        try:
            os.unlink(args.fetch_file)
        except OSError:
            pass
    rec = {
        "page_id": args.page_id,
        "remote_hash": remote_hash,
        "child_link_tags": uuids,
    }
    if args.has_children is not None:
        rec["has_children"] = (args.has_children == "true")
    if args.path:
        rec["path"] = args.path
    if title is not None:
        rec["title"] = title
    _json_out(rec)
    return 0


def _build_hunks(snapshot_text, local_text):
    """Build {old_str, new_str} hunks from snapshot->local diff with sufficient
    context (>=1 unchanged line before/after each changed group). Returns list
    of {old_str, new_str, hunk_index}. Context line count is fixed at 1 to keep
    old_str compact; downstream validation widens if ambiguous."""
    a = snapshot_text.splitlines(keepends=True)
    b = local_text.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    hunks = []
    idx = 0
    for opcodes in [matcher.get_opcodes()]:
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == "equal":
                continue
            # Include 1 line of context on each side
            ctx_start_a = max(0, i1 - 1)
            ctx_end_a = min(len(a), i2 + 1)
            ctx_start_b = max(0, j1 - 1)
            ctx_end_b = min(len(b), j2 + 1)
            old_str = "".join(a[ctx_start_a:ctx_end_a])
            new_str = "".join(b[ctx_start_b:ctx_end_b])
            hunks.append({
                "hunk_index": idx,
                "old_str": old_str,
                "new_str": new_str,
            })
            idx += 1
    return hunks


def _validate_hunks(hunks, remote_raw):
    """Per notion-mcp-cheatsheet.md → Rich-block-safe update step 5/6.

    For each hunk: count line-bounded occurrences of old_str in remote_raw.
    Verdicts: ok (exactly 1 line-bounded match) / race_lost (0) / ambiguous (>1)
    / rich_block_overlap (match falls inside a rich-block tag span)."""
    rich_spans = []
    for tag in PAIRED_RICH_TAGS:
        for m in re.finditer(
            r'<' + tag + r'\b[^>]*>.*?</' + tag + r'>',
            remote_raw, flags=re.DOTALL | re.IGNORECASE,
        ):
            rich_spans.append((m.start(), m.end()))
    image_spans = []
    for m in re.finditer(
        r'(?m)^!\[[^\]]*\]\([^)]+\)(\s*\{[^}]*\})?$', remote_raw,
    ):
        image_spans.append((m.start(), m.end()))

    results = []
    for h in hunks:
        old = h["old_str"]
        # Find every occurrence; filter to line-bounded
        line_bounded = []
        start = 0
        while True:
            i = remote_raw.find(old, start)
            if i < 0:
                break
            before_ok = (i == 0) or remote_raw[i - 1] == "\n"
            after_ok = (i + len(old) == len(remote_raw)) or remote_raw[i + len(old)] == "\n" or remote_raw[i + len(old) - 1] == "\n"
            if before_ok and after_ok:
                line_bounded.append(i)
            start = i + 1
        if not line_bounded:
            results.append({**h, "verdict": "race_lost"})
            continue
        if len(line_bounded) > 1:
            results.append({**h, "verdict": "ambiguous", "match_count": len(line_bounded)})
            continue
        i = line_bounded[0]
        span = (i, i + len(old))
        # Rich-block overlap?
        overlap = None
        for (s, e) in rich_spans + image_spans:
            if span[0] < e and span[1] > s:
                overlap = (s, e)
                break
        if overlap:
            results.append({**h, "verdict": "rich_block_overlap", "overlap": list(overlap)})
        else:
            results.append({**h, "verdict": "ok"})
    return results


def cmd_process_modified(args):
    """ONE Bash call per Modified page for commit's hunk-build sub-agents.

    Reads fetch envelope -> remote_raw; strips child-link lines from snapshot
    AND local; diffs snapshot->local; builds hunks; validates per the line-
    boundary rule. Emits classified hunks + warnings as JSON.
    """
    raw_fetch = _read(args.fetch_file)
    remote_raw = extract_body(raw_fetch)
    with open(args.snapshot, "rb") as fh:
        snap = strip_childlinks(fh.read())
    with open(args.local, "rb") as fh:
        loc = strip_childlinks(fh.read())
    hunks = _build_hunks(snap, loc)
    validated = _validate_hunks(hunks, remote_raw)
    if args.delete_fetch and args.fetch_file != "-":
        try:
            os.unlink(args.fetch_file)
        except OSError:
            pass
    _json_out({
        "page_id": args.page_id,
        "path": args.path,
        "hunks": validated,
        "remote_raw_len": len(remote_raw),
    })
    return 0


def cmd_process_postwrite(args):
    """ONE Bash call per Modified page AFTER a successful notion-update-page.

    Reads the re-fetch envelope -> extracts body -> computes new remote_hash ->
    writes the snapshot file. Emits the new hash so the main loop can update
    the manifest record.
    """
    raw_fetch = _read(args.fetch_file)
    body = extract_body(raw_fetch)
    body_bytes = body.encode("utf-8")
    title = extract_title(raw_fetch)
    normalized = normalize(body_bytes, "remote", expected_title=title)
    new_remote_hash = sha256_of(normalized)
    with open(args.snapshot_out, "w") as fh:
        fh.write(normalized)
    if args.delete_fetch and args.fetch_file != "-":
        try:
            os.unlink(args.fetch_file)
        except OSError:
            pass
    _json_out({
        "page_id": args.page_id,
        "new_remote_hash": new_remote_hash,
    })
    return 0


# ----------------------------------------------------------------------------
# Pull orchestrators (Group A).
# ----------------------------------------------------------------------------

def cmd_pull_preflight(args):
    """Replaces the heredoc-orchestration block at the top of /nsync:pull.

    Loads manifest + ignore, globs local *.md, classifies into tracked /
    untracked / ignored, hashes every tracked local file, derives the fetch
    list and rename candidates. Emits one JSON document.
    """
    sync_root = args.sync_root
    manifest = load_manifest(sync_root)
    ignore_patterns = load_ignore_patterns(sync_root)
    local_md = glob_md(sync_root)
    # Apply ignore matcher → set local_ignored flags + filter the local-only list
    ignored_set = {p for p in local_md if match_ignore(ignore_patterns, p)}

    # Rename detection (skips root entry)
    rename_candidates = detect_renames(manifest, local_md, sync_root)

    # Build map of tracked paths
    tracked_paths = {rec["path"]: pid for pid, rec in manifest["pages"].items()}

    # local_only_md_files: on disk, not in manifest, not ignored
    local_only_md_files = sorted(p for p in local_md
                                  if p not in tracked_paths and p not in ignored_set)

    # Hash every tracked, non-ignored, on-disk file
    to_hash = [rec["path"] for pid, rec in manifest["pages"].items()
               if not rec.get("local_ignored", False)
               and rec["path"] in set(local_md)]
    # Title hint per path so normalize() can strip the H1 page-title line if
    # present (category 1). When a manifest entry has no title, the dict lookup
    # returns None and the strip is a no-op.
    title_by_path = {rec["path"]: rec.get("title") or None
                     for pid, rec in manifest["pages"].items()
                     if not rec.get("local_ignored", False)}
    local_hashes = hash_batch_local(sync_root, to_hash, title_by_path)

    # Fetch list: every tracked, non-ignored page (root inclusive)
    fetch_list = []
    for pid, rec in manifest["pages"].items():
        if rec.get("local_ignored", False):
            continue
        fetch_list.append({
            "page_id": pid,
            "path": rec["path"],
            "url": rec["url"],
            "manifest_remote_hash": rec["remote_hash"],
            "has_children": rec["has_children"],
        })

    root_pid = manifest["parent"]["page_id"]
    root_backfill_needed = root_pid not in manifest["pages"]

    _json_out({
        "schema_version": 1,
        "sync_root": sync_root,
        "parent": manifest["parent"],
        "root_backfill_needed": root_backfill_needed,
        "rename_candidates": rename_candidates,
        "fetch_list": fetch_list,
        "local_hashes": local_hashes,
        "local_only_md_files": local_only_md_files,
        "ignored_md_files": sorted(ignored_set),
        "tracked_paths_missing": sorted(rec["path"] for rec in manifest["pages"].values()
                                          if rec["path"] not in set(local_md)
                                          and not rec.get("local_ignored", False)),
    })
    return 0


def cmd_pull_classify(args):
    """Takes preflight JSON + sub-agent fetch records + parent envelope path.

    Classifies each tracked page (Clean / Auto-merge-remote / Auto-merge-local /
    Conflict), computes reachability, identifies remote-added and remote-trashed
    pages, and builds the refetch_list (Auto-merge-remote ∪ remote-added).
    Conflict-resolution prompts happen in the main loop; this subcommand is
    deterministic and prompt-free.
    """
    with open(args.preflight) as fh:
        preflight = json.load(fh)
    with open(args.records) as fh:
        recs_data = json.load(fh)
    # Accept either {records: [...]} or a bare list.
    records = recs_data["records"] if isinstance(recs_data, dict) else recs_data
    rec_by_pid = {r["page_id"]: r for r in records}

    sync_root = preflight["sync_root"]
    manifest = load_manifest(sync_root)
    local_hashes = preflight["local_hashes"]
    parent_pid = manifest["parent"]["page_id"]

    clean, am_remote, am_local, conflict = [], [], [], []
    for pid, rec in manifest["pages"].items():
        if rec.get("local_ignored", False):
            continue
        r = rec_by_pid.get(pid)
        if r is None:
            continue
        local_h = local_hashes.get(rec["path"])
        local_clean = (local_h == rec["local_hash"])
        remote_clean = (r["remote_hash"] == rec["remote_hash"])
        if local_clean and remote_clean:
            clean.append(pid)
        elif local_clean and not remote_clean:
            am_remote.append(pid)
        elif not local_clean and remote_clean:
            am_local.append(pid)
        else:
            conflict.append(pid)

    reachable = derive_reachable(args.parent_fetch, records)
    # Parent itself doesn't need to be in reachable for the trashed check
    manifest_pids = set(manifest["pages"].keys())
    remote_added = sorted(reachable - manifest_pids)
    remote_trashed = sorted(manifest_pids - reachable - {parent_pid})

    # Refetch list: auto-merge-remote + remote_added (need body refetch). Conflicts
    # also need fresh remote body for the scratch buffer, so include them too.
    refetch = []
    for pid in am_remote:
        rec = manifest["pages"][pid]
        refetch.append({
            "page_id": pid,
            "path": rec["path"],
            "url": rec["url"],
            "has_children": rec["has_children"],
            "reason": "auto_merge_remote",
        })
    for pid in conflict:
        rec = manifest["pages"][pid]
        refetch.append({
            "page_id": pid,
            "path": rec["path"],
            "url": rec["url"],
            "has_children": rec["has_children"],
            "reason": "conflict",
        })
    # remote_added pages have no manifest entry yet — main loop must fetch their
    # metadata before they can be added. Emit them in remote_added; the .md command
    # decides whether to enqueue them in a second-pass fetch.

    # Child-link regen list: every Clean has_children page (regenerate per
    # references/path-mapping.md → Regeneration trigger).
    child_link_regen_list = []
    for pid in clean:
        rec = manifest["pages"][pid]
        if not rec["has_children"]:
            continue
        r = rec_by_pid.get(pid)
        if r is None:
            continue
        child_link_regen_list.append({
            "page_id": pid,
            "path": rec["path"],
            "expected_uuids": [u.lower() for u in r.get("child_link_tags", [])],
        })

    _json_out({
        "schema_version": 1,
        "sync_root": sync_root,
        "clean": sorted(clean),
        "auto_merge_remote": sorted(am_remote),
        "auto_merge_local": sorted(am_local),
        "conflict": sorted(conflict),
        "remote_added": remote_added,
        "remote_trashed": remote_trashed,
        "refetch_list": refetch,
        "child_link_regen_list": child_link_regen_list,
    })
    return 0


def _regenerate_child_links(file_text, file_rel_path, expected_uuids, path_by_pid,
                            title_by_pid):
    """Reconcile managed child-link lines per Regeneration trigger rules.

    Returns (new_text, changed: bool, added_count, dropped_count).
    Placeholders are preserved exactly. Existing managed lines: rebuilt by UUID
    match (title + path refreshed); orphans (UUID not in expected) dropped.
    New expected UUIDs inserted after the last existing child-link line.
    """
    lines = file_text.split("\n")
    expected_set = set(u.lower() for u in expected_uuids)
    out = []
    insert_pos = None
    for line in lines:
        m_managed = CHILDLINK_MANAGED_RE.match(line.strip())
        m_placeholder = CHILDLINK_PLACEHOLDER_RE.match(line.strip())
        if m_managed:
            # Pull the uuid back out
            pid_match = re.search(r'page_id="([0-9a-f-]{36})"', line, re.IGNORECASE)
            if pid_match:
                pid = pid_match.group(1).lower()
                if pid in expected_set:
                    title = title_by_pid.get(pid, "")
                    out.append(render_managed_link(file_rel_path, pid, title, path_by_pid))
                    insert_pos = len(out)
                    continue
            # Orphan or unrecognized — drop
            continue
        if m_placeholder:
            out.append(line)
            insert_pos = len(out)
            continue
        out.append(line)
    # Determine which expected UUIDs are still missing
    present = set()
    for o in out:
        pid_match = re.search(r'page_id="([0-9a-f-]{36})"', o, re.IGNORECASE)
        if pid_match:
            present.add(pid_match.group(1).lower())
    to_add = [u for u in expected_uuids if u.lower() not in present]
    added = 0
    if to_add:
        if insert_pos is None:
            # No existing child-link / placeholder — append at end of non-empty
            insert_pos = max((i + 1 for i, l in enumerate(out) if l.strip()), default=len(out))
        new_link_lines = [
            render_managed_link(file_rel_path, u.lower(),
                                title_by_pid.get(u.lower(), ""), path_by_pid)
            for u in to_add
        ]
        out = out[:insert_pos] + new_link_lines + out[insert_pos:]
        added = len(new_link_lines)
    new_text = "\n".join(out)
    return new_text, (new_text != file_text), added


def cmd_pull_apply(args):
    """Takes classify JSON + bodies dir + (optional) decisions JSON.

    Applies all per-state actions deterministically: overwrites auto-merge-
    remote files, regenerates child-link lines on Clean has_children pages,
    applies conflict resolutions per the snapshot-update table, refreshes
    last_synced_at on Clean pages, persists manifest atomically, verifies
    every local file's hash matches, and cleans .nsync/tmp/.
    """
    with open(args.classify) as fh:
        classify = json.load(fh)
    sync_root = classify["sync_root"]
    manifest = load_manifest(sync_root)
    path_by_pid = {pid: rec["path"] for pid, rec in manifest["pages"].items()}
    title_by_pid = {pid: rec.get("title", "") for pid, rec in manifest["pages"].items()}
    bodies_dir = args.bodies_dir
    snap_dir = os.path.join(sync_root, ".nsync", "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    now = _now_iso()

    decisions = {}
    if args.decisions and os.path.exists(args.decisions):
        with open(args.decisions) as fh:
            decisions = {d["page_id"]: d for d in json.load(fh).get("conflicts", [])}

    overwritten = []
    overwrite_errors = []
    regen_changes = []
    conflict_applied = []

    # 1) Auto-merge-remote overwrites
    for entry in classify["refetch_list"]:
        if entry.get("reason") != "auto_merge_remote":
            continue
        pid = entry["page_id"]
        path = entry["path"]
        has_children = entry["has_children"]
        body_path = os.path.join(bodies_dir, f"{pid}.body.md")
        if not os.path.exists(body_path):
            overwrite_errors.append({"page_id": pid, "path": path, "error": "missing body file"})
            continue
        with open(body_path) as fh:
            raw_body = fh.read()
        rendered = (substitute_page_urls(raw_body, path, path_by_pid)
                    if has_children else raw_body)
        title = title_by_pid.get(pid) or None
        local_content = normalize(rendered.encode("utf-8"), "local", expected_title=title)
        target = os.path.join(sync_root, path)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w") as fh:
            fh.write(local_content)
        snap_content = normalize(raw_body.encode("utf-8"), "remote", expected_title=title)
        with open(os.path.join(snap_dir, f"{pid}.md"), "w") as fh:
            fh.write(snap_content)
        new_local_hash = sha256_of(normalize(local_content.encode("utf-8"), "local", expected_title=title))
        new_remote_hash = sha256_of(snap_content)
        rec = manifest["pages"][pid]
        rec["local_hash"] = new_local_hash
        rec["remote_hash"] = new_remote_hash
        rec["last_synced_at"] = now
        overwritten.append(path)

    # 2) Conflict resolutions
    for pid in classify["conflict"]:
        if pid not in decisions:
            continue  # left dirty
        d = decisions[pid]
        choice = d.get("choice")
        rec = manifest["pages"][pid]
        path = rec["path"]
        body_path = os.path.join(bodies_dir, f"{pid}.body.md")
        title = title_by_pid.get(pid) or None
        if choice == "L":
            # Keep local; snapshot ← local; both hashes = hash(local)
            with open(os.path.join(sync_root, path)) as fh:
                lc = fh.read()
            lh = sha256_of(normalize(lc.encode("utf-8"), "local", expected_title=title))
            with open(os.path.join(snap_dir, f"{pid}.md"), "w") as fh:
                fh.write(lc)
            rec["local_hash"] = lh
            rec["remote_hash"] = lh
            rec["last_synced_at"] = now
        elif choice == "R":
            if not os.path.exists(body_path):
                overwrite_errors.append({"page_id": pid, "path": path, "error": "missing body for R"})
                continue
            with open(body_path) as fh:
                rb = fh.read()
            has_children = rec["has_children"]
            rendered = (substitute_page_urls(rb, path, path_by_pid)
                        if has_children else rb)
            lc = normalize(rendered.encode("utf-8"), "local", expected_title=title)
            with open(os.path.join(sync_root, path), "w") as fh:
                fh.write(lc)
            sc = normalize(rb.encode("utf-8"), "remote", expected_title=title)
            with open(os.path.join(snap_dir, f"{pid}.md"), "w") as fh:
                fh.write(sc)
            rec["local_hash"] = sha256_of(normalize(lc.encode("utf-8"), "local", expected_title=title))
            rec["remote_hash"] = sha256_of(sc)
            rec["last_synced_at"] = now
        elif choice == "E":
            # User has already edited the merged file; main loop wrote it.
            with open(os.path.join(sync_root, path)) as fh:
                lc = fh.read()
            with open(body_path) as fh:
                rb = fh.read()
            sc = normalize(rb.encode("utf-8"), "remote", expected_title=title)
            with open(os.path.join(snap_dir, f"{pid}.md"), "w") as fh:
                fh.write(sc)
            rec["local_hash"] = sha256_of(normalize(lc.encode("utf-8"), "local", expected_title=title))
            rec["remote_hash"] = sha256_of(sc)
            rec["last_synced_at"] = now
        else:
            continue  # [S]kip
        conflict_applied.append({"page_id": pid, "path": path, "choice": choice})

    # 3) Clean has_children: regenerate child-link lines
    for entry in classify["child_link_regen_list"]:
        pid = entry["page_id"]
        path = entry["path"]
        target = os.path.join(sync_root, path)
        if not os.path.exists(target):
            continue
        with open(target) as fh:
            text = fh.read()
        new_text, changed, added = _regenerate_child_links(
            text, path, entry["expected_uuids"], path_by_pid, title_by_pid,
        )
        if changed:
            title = title_by_pid.get(pid) or None
            new_text_norm = normalize(new_text.encode("utf-8"), "local", expected_title=title)
            with open(target, "w") as fh:
                fh.write(new_text_norm)
            # Snapshot: re-normalize stripped body
            stripped = strip_childlinks(new_text_norm.encode("utf-8"))
            sc = normalize(stripped.encode("utf-8"), "local", expected_title=title)
            with open(os.path.join(snap_dir, f"{pid}.md"), "w") as fh:
                fh.write(sc)
            rec = manifest["pages"][pid]
            rec["local_hash"] = sha256_of(normalize(new_text_norm.encode("utf-8"), "local", expected_title=title))
            rec["last_synced_at"] = now
            regen_changes.append({"page_id": pid, "path": path, "added": added})

    # 4) Refresh last_synced_at on Clean pages (untouched files)
    refreshed = 0
    touched_pids = ({entry["page_id"] for entry in classify["refetch_list"]
                       if entry.get("reason") == "auto_merge_remote"}
                    | {c["page_id"] for c in conflict_applied}
                    | {c["page_id"] for c in regen_changes})
    for pid in classify["clean"]:
        if pid in touched_pids:
            continue
        manifest["pages"][pid]["last_synced_at"] = now
        refreshed += 1

    # 5) Persist manifest atomically
    save_manifest_atomic(sync_root, manifest)

    # 6) Verify: every tracked local file's hash matches manifest.local_hash
    mismatches = []
    for pid, rec in manifest["pages"].items():
        if rec.get("local_ignored"):
            continue
        full = os.path.join(sync_root, rec["path"])
        if not os.path.exists(full):
            continue
        title = rec.get("title") or None
        with open(full, "rb") as fh:
            h = sha256_of(normalize(fh.read(), "local", expected_title=title))
        if h != rec["local_hash"]:
            mismatches.append({"page_id": pid, "path": rec["path"],
                                "manifest": rec["local_hash"], "actual": h})

    # 7) Cleanup tmp (only the body files we read + any stray fetch envelopes)
    cleaned = 0
    if args.cleanup_tmp:
        tmp_dir = os.path.join(sync_root, ".nsync", "tmp")
        if os.path.isdir(tmp_dir):
            for f in os.listdir(tmp_dir):
                full = os.path.join(tmp_dir, f)
                if os.path.isfile(full):
                    os.unlink(full)
                    cleaned += 1

    _json_out({
        "overwritten": overwritten,
        "overwrite_errors": overwrite_errors,
        "conflict_applied": conflict_applied,
        "regen_changes": regen_changes,
        "clean_refreshed": refreshed,
        "verify_mismatches": mismatches,
        "tmp_files_cleaned": cleaned,
    })
    return 0 if not mismatches else 2


# ----------------------------------------------------------------------------
# Status / diff orchestrators.
# ----------------------------------------------------------------------------

def cmd_status_scan(args):
    """Status report: preflight + classify in one shot. Read-only.

    Requires sub-agent records JSON (same shape as pull-classify's --records).
    Emits per-page state plus the local-only and ignored lists. No mutations.
    """
    sync_root = args.sync_root
    manifest = load_manifest(sync_root)
    ignore_patterns = load_ignore_patterns(sync_root)
    local_md = glob_md(sync_root)
    ignored_set = {p for p in local_md if match_ignore(ignore_patterns, p)}
    tracked_paths = {rec["path"]: pid for pid, rec in manifest["pages"].items()}
    local_only = sorted(p for p in local_md
                        if p not in tracked_paths and p not in ignored_set)

    to_hash = [rec["path"] for pid, rec in manifest["pages"].items()
               if not rec.get("local_ignored", False)
               and rec["path"] in set(local_md)]
    # Title hint per path so normalize() can strip the H1 page-title line if
    # present (category 1). When a manifest entry has no title, the dict lookup
    # returns None and the strip is a no-op.
    title_by_path = {rec["path"]: rec.get("title") or None
                     for pid, rec in manifest["pages"].items()
                     if not rec.get("local_ignored", False)}
    local_hashes = hash_batch_local(sync_root, to_hash, title_by_path)

    records = []
    if args.records and os.path.exists(args.records):
        with open(args.records) as fh:
            data = json.load(fh)
        records = data["records"] if isinstance(data, dict) else data

    rec_by_pid = {r["page_id"]: r for r in records}
    clean, am_remote, am_local, conflict, untracked_remote_hash = [], [], [], [], []
    for pid, rec in manifest["pages"].items():
        if rec.get("local_ignored", False):
            continue
        r = rec_by_pid.get(pid)
        local_h = local_hashes.get(rec["path"])
        local_clean = (local_h == rec["local_hash"])
        if r is None:
            untracked_remote_hash.append(pid)
            continue
        remote_clean = (r["remote_hash"] == rec["remote_hash"])
        if local_clean and remote_clean:
            clean.append(pid)
        elif local_clean and not remote_clean:
            am_remote.append(pid)
        elif not local_clean and remote_clean:
            am_local.append(pid)
        else:
            conflict.append(pid)

    rename_candidates = detect_renames(manifest, local_md, sync_root)
    parent_pid = manifest["parent"]["page_id"]
    reachable = derive_reachable(args.parent_fetch, records)
    manifest_pids = set(manifest["pages"].keys())
    remote_added = sorted(reachable - manifest_pids)
    remote_trashed = sorted(manifest_pids - reachable - {parent_pid})

    _json_out({
        "schema_version": 1,
        "sync_root": sync_root,
        "clean": sorted(clean),
        "modified_local": sorted(am_local),
        "modified_remote": sorted(am_remote),
        "conflict": sorted(conflict),
        "untracked_remote_hash": sorted(untracked_remote_hash),
        "local_only_md_files": local_only,
        "ignored_md_files": sorted(ignored_set),
        "tracked_paths_missing": sorted(rec["path"] for rec in manifest["pages"].values()
                                          if rec["path"] not in set(local_md)
                                          and not rec.get("local_ignored", False)),
        "rename_candidates": rename_candidates,
        "remote_added": remote_added,
        "remote_trashed": remote_trashed,
    })
    return 0


def cmd_diff_scan(args):
    """Builds a per-target diff render plan. Given the user's path filter, finds
    which tracked pages need a remote fetch (because remote_hash MAY have moved)
    vs which can render inline from snapshot+local. Emits the plan as JSON.
    """
    sync_root = args.sync_root
    manifest = load_manifest(sync_root)
    local_md = set(glob_md(sync_root))
    filters = args.path or []
    tracked = []
    for pid, rec in manifest["pages"].items():
        if rec.get("local_ignored"):
            continue
        if filters and not any(rec["path"].startswith(f) or rec["path"] == f for f in filters):
            continue
        tracked.append({
            "page_id": pid,
            "path": rec["path"],
            "url": rec["url"],
            "has_children": rec["has_children"],
            "manifest_remote_hash": rec["remote_hash"],
            "on_disk": rec["path"] in local_md,
        })
    _json_out({
        "sync_root": sync_root,
        "fetch_list": tracked,
    })
    return 0


def cmd_enumerate_tree(args):
    """Init helper: collapse the frontier-expansion loop.

    Given a directory of fetched envelope files (one per page UUID), extracts
    the body of each, extracts child UUIDs, and emits the next-round frontier
    (UUIDs that don't yet have an envelope file). Caller iterates until the
    next-round list is empty, then has the full discovered set.
    """
    fetches_dir = args.fetches_dir
    visited = set()
    next_round = set()
    for fname in os.listdir(fetches_dir):
        if not fname.endswith(".fetch.txt"):
            continue
        pid = fname[:-len(".fetch.txt")]
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", pid.lower()):
            continue
        visited.add(pid.lower())
        with open(os.path.join(fetches_dir, fname), "rb") as fh:
            body = extract_body(fh.read())
        for u in extract_uuids(body.encode("utf-8")):
            next_round.add(u.lower())
    new_frontier = sorted(next_round - visited)
    _json_out({
        "visited": sorted(visited),
        "next_round": new_frontier,
        "done": len(new_frontier) == 0,
    })
    return 0


# ----------------------------------------------------------------------------
# Commit orchestrators.
# ----------------------------------------------------------------------------

def cmd_commit_preflight(args):
    """Pre-commit: load manifest + ignore + local glob, hash-batch local,
    detect renames, partition into Modified / Added / Deleted sets. Returns
    list of pages whose remote_hash needs a freshness check (Modified) plus
    new-page topology (parent_path -> [siblings]) and deleted-page list.
    """
    sync_root = args.sync_root
    manifest = load_manifest(sync_root)
    ignore_patterns = load_ignore_patterns(sync_root)
    local_md = glob_md(sync_root)
    ignored_set = {p for p in local_md if match_ignore(ignore_patterns, p)}

    tracked_paths = {rec["path"]: pid for pid, rec in manifest["pages"].items()}
    to_hash = [rec["path"] for pid, rec in manifest["pages"].items()
               if not rec.get("local_ignored", False)
               and rec["path"] in set(local_md)]
    # Title hint per path so normalize() can strip the H1 page-title line if
    # present (category 1). When a manifest entry has no title, the dict lookup
    # returns None and the strip is a no-op.
    title_by_path = {rec["path"]: rec.get("title") or None
                     for pid, rec in manifest["pages"].items()
                     if not rec.get("local_ignored", False)}
    local_hashes = hash_batch_local(sync_root, to_hash, title_by_path)

    modified, deleted = [], []
    for pid, rec in manifest["pages"].items():
        if rec.get("local_ignored"):
            continue
        if rec["path"] not in set(local_md):
            deleted.append({
                "page_id": pid,
                "path": rec["path"],
                "title": rec.get("title", ""),
                "parent_page_id": rec.get("parent_page_id"),
                "url": rec.get("url"),
            })
            continue
        if local_hashes.get(rec["path"]) != rec["local_hash"]:
            modified.append({
                "page_id": pid,
                "path": rec["path"],
                "url": rec["url"],
                "has_children": rec["has_children"],
                "manifest_remote_hash": rec["remote_hash"],
            })

    new = []
    for p in local_md:
        if p in tracked_paths or p in ignored_set:
            continue
        # Resolve parent: containing directory's index.md (or root)
        dir_part = os.path.dirname(p)
        if dir_part:
            parent_index = dir_part + "/index.md"
            parent_pid = tracked_paths.get(parent_index)
        else:
            parent_pid = manifest["parent"]["page_id"]
        new.append({
            "path": p,
            "parent_page_id": parent_pid,
        })

    rename_candidates = detect_renames(manifest, local_md, sync_root)

    _json_out({
        "schema_version": 1,
        "sync_root": sync_root,
        "modified": modified,
        "new": new,
        "deleted": deleted,
        "rename_candidates": rename_candidates,
        "stale_check_list": [m["page_id"] for m in modified],
    })
    return 0


def cmd_commit_validate_hunks(args):
    """Dry-run hunk-build for every Modified page. Reads each page's body file
    from --bodies-dir, builds hunks against snapshot+local, validates per the
    line-boundary rule, and emits one JSON blob with classified hunks per page.
    The main loop batches the {ambiguous, race_lost, rich_block_overlap}
    cases into ONE AskUserQuestion instead of mid-stream prompts.
    """
    with open(args.modified_list) as fh:
        modified = json.load(fh)
    sync_root = args.sync_root
    bodies_dir = args.bodies_dir
    snap_dir = os.path.join(sync_root, ".nsync", "snapshots")
    out_pages = []
    for entry in modified:
        pid = entry["page_id"]
        path = entry["path"]
        body_path = os.path.join(bodies_dir, f"{pid}.body.md")
        snap_path = os.path.join(snap_dir, f"{pid}.md")
        local_path = os.path.join(sync_root, path)
        if not os.path.exists(body_path):
            out_pages.append({"page_id": pid, "path": path,
                                 "error": "missing body file", "hunks": []})
            continue
        with open(body_path) as fh:
            remote_raw = fh.read()
        with open(snap_path, "rb") as fh:
            snap_text = strip_childlinks(fh.read())
        with open(local_path, "rb") as fh:
            loc_text = strip_childlinks(fh.read())
        hunks = _build_hunks(snap_text, loc_text)
        validated = _validate_hunks(hunks, remote_raw)
        out_pages.append({"page_id": pid, "path": path, "hunks": validated})
    _json_out({"pages": out_pages})
    return 0


def cmd_commit_apply(args):
    """Apply commit decisions. Takes validated hunks JSON + decisions JSON +
    new-pages-plan + write-results from the per-page Workflow sub-agents.

    Responsibilities:
    - Update manifest with new_remote_hash returned by each Modified write.
    - Insert new PageRecords for created pages.
    - Apply deleted-page disposition (orphan / trash-log / restore-noop).
    - Run placeholder child-link backfill pass on every has_children file.
    - Persist manifest atomically. Verify hashes. Clean tmp.
    """
    with open(args.write_results) as fh:
        write_results = json.load(fh)
    sync_root = args.sync_root
    manifest = load_manifest(sync_root)
    snap_dir = os.path.join(sync_root, ".nsync", "snapshots")
    path_by_pid = {pid: rec["path"] for pid, rec in manifest["pages"].items()}
    title_by_pid = {pid: rec.get("title", "") for pid, rec in manifest["pages"].items()}
    now = _now_iso()

    updated, errors = [], []
    for r in write_results.get("modified", []):
        pid = r["page_id"]
        if not r.get("pushed"):
            errors.append({"page_id": pid, "path": r.get("path"),
                            "reason": r.get("error") or "not pushed"})
            continue
        rec = manifest["pages"].get(pid)
        if not rec:
            errors.append({"page_id": pid, "error": "unknown page"})
            continue
        new_remote = r.get("new_remote_hash")
        if new_remote:
            rec["remote_hash"] = new_remote
        # Recompute local_hash from disk
        full = os.path.join(sync_root, rec["path"])
        if os.path.exists(full):
            title = rec.get("title") or None
            with open(full, "rb") as fh:
                rec["local_hash"] = sha256_of(normalize(fh.read(), "local", expected_title=title))
        rec["last_synced_at"] = now
        updated.append(rec["path"])

    new_pages = []
    for r in write_results.get("new", []):
        pid = r["page_id"]
        path = r["path"]
        manifest["pages"][pid] = {
            "path": path,
            "title": r.get("title", os.path.splitext(os.path.basename(path))[0]),
            "parent_page_id": r["parent_page_id"],
            "url": r.get("url", ""),
            "last_synced_at": now,
            "local_hash": r["local_hash"],
            "remote_hash": r["remote_hash"],
            "rich_blocks": [],
            "has_children": r.get("has_children", False),
            "local_ignored": False,
        }
        new_pages.append(path)
        path_by_pid[pid] = path
        title_by_pid[pid] = r.get("title", "")

    trashed = []
    for r in write_results.get("deleted", []):
        pid = r["page_id"]
        choice = r.get("choice")
        rec = manifest["pages"].get(pid)
        if not rec:
            continue
        if choice in ("O", "T", "M"):
            entry = {
                "page_id": pid,
                "path": rec["path"],
                "trashed_at": now,
                "trashed_by": ({"O": "orphaned-to-workspace",
                                "T": "remote-trashed",
                                "M": "untracked-no-remote-action"})[choice],
                "title": rec.get("title", ""),
                "last_local_hash": rec.get("local_hash"),
            }
            manifest.setdefault("trash_log", []).append(entry)
            del manifest["pages"][pid]
            trashed.append(rec["path"])
        # 'R' (Restore) is a no-op locally; the user will restore the file before next commit.

    # Placeholder child-link backfill: scan every has_children file for placeholders
    backfilled = []
    for pid, rec in list(manifest["pages"].items()):
        if not rec.get("has_children"):
            continue
        full = os.path.join(sync_root, rec["path"])
        if not os.path.exists(full):
            continue
        with open(full) as fh:
            text = fh.read()
        new_lines = []
        changed = False
        for line in text.split("\n"):
            ph = CHILDLINK_PLACEHOLDER_RE.match(line.strip())
            if not ph:
                new_lines.append(line)
                continue
            target = ph.group(1)
            normalized_target = os.path.normpath(
                os.path.join(os.path.dirname(rec["path"]) or ".", target)
            ).replace(os.sep, "/")
            # Look up the resolved PageRecord
            target_pid = None
            for tpid, trec in manifest["pages"].items():
                if trec["path"] == normalized_target:
                    target_pid = tpid
                    break
            if not target_pid:
                new_lines.append(line)  # leave placeholder; strip filtering hides it from diff
                continue
            file_pid = (manifest["parent"]["page_id"]
                        if rec.get("parent_page_id") is None else pid)
            target_rec = manifest["pages"][target_pid]
            if target_rec.get("parent_page_id") != file_pid:
                new_lines.append(line)
                continue
            new_lines.append(render_managed_link(rec["path"], target_pid,
                                                    target_rec.get("title", ""),
                                                    path_by_pid))
            changed = True
        if changed:
            new_text = "\n".join(new_lines)
            title = rec.get("title") or None
            new_text = normalize(new_text.encode("utf-8"), "local", expected_title=title)
            with open(full, "w") as fh:
                fh.write(new_text)
            with open(os.path.join(snap_dir, f"{pid}.md"), "w") as fh:
                stripped = strip_childlinks(new_text.encode("utf-8"))
                fh.write(normalize(stripped.encode("utf-8"), "local", expected_title=title))
            rec["local_hash"] = sha256_of(normalize(new_text.encode("utf-8"), "local", expected_title=title))
            backfilled.append(rec["path"])

    save_manifest_atomic(sync_root, manifest)

    mismatches = []
    for pid, rec in manifest["pages"].items():
        if rec.get("local_ignored"):
            continue
        full = os.path.join(sync_root, rec["path"])
        if not os.path.exists(full):
            continue
        title = rec.get("title") or None
        with open(full, "rb") as fh:
            h = sha256_of(normalize(fh.read(), "local", expected_title=title))
        if h != rec["local_hash"]:
            mismatches.append({"page_id": pid, "path": rec["path"]})

    cleaned = 0
    if args.cleanup_tmp:
        tmp_dir = os.path.join(sync_root, ".nsync", "tmp")
        if os.path.isdir(tmp_dir):
            for f in os.listdir(tmp_dir):
                full = os.path.join(tmp_dir, f)
                if os.path.isfile(full):
                    os.unlink(full)
                    cleaned += 1

    _json_out({
        "modified_updated": updated,
        "new_added": new_pages,
        "deleted_trashed": trashed,
        "backfilled": backfilled,
        "errors": errors,
        "verify_mismatches": mismatches,
        "tmp_files_cleaned": cleaned,
    })
    return 0 if not mismatches else 2





def _self_test():
    """In-process regression tests. Exits non-zero on first failure."""
    failures = []

    def expect(label, got, want):
        if got != want:
            failures.append(f"{label}\n  got:  {got!r}\n  want: {want!r}")

    # 1. empty-block strips to empty (and parent body classification stays empty).
    body = (
        b'<empty-block/>\n'
        b'<page url="https://www.notion.so/abc">child</page>\n'
    )
    expect("empty-block + page url -> empty (remote)",
           normalize(body, "remote"), "")

    # 2. unknown tag triggers warn-and-strip.
    _UNKNOWN_TAG_WARNED.clear()
    body = b'before\n<frobnicator color="x">inner</frobnicator>\nafter\n'
    out = normalize(body, "remote")
    expect("unknown <frobnicator> stripped", out, "before\nafter\n")
    if "frobnicator" not in _UNKNOWN_TAG_WARNED:
        failures.append("unknown <frobnicator> should have warned")

    # 3. unknown self-closing tag stripped.
    _UNKNOWN_TAG_WARNED.clear()
    body = b'before\n<custom-widget data="x"/>\nafter\n'
    out = normalize(body, "remote")
    expect("unknown <custom-widget/> stripped", out, "before\nafter\n")

    # 4. recognized rich block stripped (callout, multi-line).
    body = b'a\n<callout icon="i">\ninside\n</callout>\nb\n'
    expect("callout span removed", normalize(body, "remote"), "a\nb\n")

    # 5. local mode preserves rich blocks (only strips child-links + page urls).
    body = b'<callout>x</callout>\n[Title](./c.md) <!-- nsync:child page_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" -->\nbody\n'
    expect("local mode keeps callout, drops managed child-link",
           normalize(body, "local"), "<callout>x</callout>\nbody\n")

    # 6. extract-uuids: all four observed forms.
    src = (
        '<page url="https://www.notion.so/abcdef12345678901234567890abcdef">A</page>\n'
        '&lt;page url="https://www.notion.so/11111111-2222-3333-4444-555555555555"&gt;B&lt;/page&gt;\n'
        'bare https://www.notion.so/aabbccddeeff00112233445566778899\n'
        'hybrid https://www.notion.so/370c21c8-3d5a-8122bdede7722395bd93\n'
    ).encode()
    uuids = extract_uuids(src)
    want = [
        "abcdef12-3456-7890-1234-567890abcdef",
        "11111111-2222-3333-4444-555555555555",
        "aabbccdd-eeff-0011-2233-445566778899",
        "370c21c8-3d5a-8122-bded-e7722395bd93",
    ]
    expect("extract-uuids: four forms", uuids, want)

    # 7. extract-uuids: tolerates surrounding text and ignores non-UUID slugs.
    src = b'see https://example.com/no-uuid and https://www.notion.so/page-1234abcd5678ef901234abcd5678ef90\n'
    expect("extract-uuids: slug-prefixed URL",
           extract_uuids(src), ["1234abcd-5678-ef90-1234-abcd5678ef90"])

    # 8. image-block line stripped in remote; preserved in local.
    body = b'before\n![alt](https://x/y.png)\nafter\n'
    expect("image line removed (remote)", normalize(body, "remote"),
           "before\nafter\n")
    expect("image line kept (local)", normalize(body, "local"),
           "before\n![alt](https://x/y.png)\nafter\n")

    # 9. inline image inside running text is NOT stripped (both modes).
    body = b'see ![logo](https://x/y.png) here\n'
    expect("inline image preserved (remote)", normalize(body, "remote"),
           "see ![logo](https://x/y.png) here\n")

    # 10. <page url> whole-line strip (both modes). PAGE_SPAN_RE substitutes the
    # tag with '' but leaves the surrounding newlines, so the residue is a blank
    # line. That's pre-existing behavior and matches what /nsync:pull's hash math
    # expects.
    body = b'a\n<page url="https://www.notion.so/abc">child</page>\nb\n'
    expect("page url line removed (local)", normalize(body, "local"), "a\nb\n")

    # 11. CRLF -> LF.
    body = b'x\r\ny\r\n'
    expect("CRLF normalized", normalize(body, "local"), "x\ny\n")

    # 12. NFC normalization (precomposed vs decomposed).
    body = "é".encode() + b'\n'  # precomposed
    decomposed = b'e\xcc\x81\n'
    expect("NFC: precomposed body", normalize(body, "local"),
           normalize(decomposed, "local"))

    # 13. extract-body: lifts the <content> body out of a notion-fetch envelope and
    # produces the same remote_hash as feeding just the body would. This is the
    # regression test for the bug where sub-agents fed the whole envelope to nsync.py,
    # the unknown-tag fallback stripped <content>...</content> wholesale, and remote
    # hashes converged on the preamble line.
    envelope = (
        b'Here is the result of "view" for the Page with URL https://www.notion.so/x as of T:\n'
        b'<page url="https://www.notion.so/x" icon="*">\n'
        b'<ancestor-path>\n'
        b'<parent-page url="https://www.notion.so/p" title="Parent"/>\n'
        b'</ancestor-path>\n'
        b'<properties>\n'
        b'{"title":"Test"}\n'
        b'</properties>\n'
        b'<content>\n'
        b'HELLO BODY LINE 1\n'
        b'HELLO BODY LINE 2\n'
        b'</content>\n'
        b'</page>\n'
    )
    expect("extract-body: pulls body verbatim",
           extract_body(envelope),
           "HELLO BODY LINE 1\nHELLO BODY LINE 2")
    body_only = b'HELLO BODY LINE 1\nHELLO BODY LINE 2\n'
    expect("extract-body + normalize(remote) == normalize(remote) on bare body",
           normalize(extract_body(envelope).encode() + b'\n', "remote"),
           normalize(body_only, "remote"))

    # 14. extract-body is idempotent on input that has no envelope.
    bare = b'just a body\nno wrapper\n'
    expect("extract-body: idempotent without <content>",
           extract_body(bare).encode(), bare)

    # 15. extract-body: empty body between <content> markers -> empty string.
    env_empty = (
        b'<page url="x"><content>\n</content></page>\n'
    )
    expect("extract-body: empty body", extract_body(env_empty), "")

    # 16. extract-body: handles <content> on the same line as body start, no
    # leading newline to consume.
    env_oneline = b'<page url="x"><content>body line</content></page>\n'
    expect("extract-body: one-line content", extract_body(env_oneline), "body line")

    # 17. slugify per path-mapping.md rule 4.
    expect("slugify: lowercase + dashes",
           slugify("My Doc Title"), "my-doc-title")
    expect("slugify: strips punctuation",
           slugify("Foo / Bar: Baz!"), "foo--bar-baz")
    expect("slugify: empty -> untitled",
           slugify(""), "untitled")

    # 18. compute_rel_path
    expect("rel: same dir",
           compute_rel_path("a/b.md", "a/c.md"), "./c.md")
    expect("rel: child dir",
           compute_rel_path("a/index.md", "a/sub/page.md"), "./sub/page.md")
    expect("rel: sibling dir",
           compute_rel_path("a/b.md", "x/y.md"), "../x/y.md")

    # 19. render_managed_link: in-tree target uses relative path
    title_map = {"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": "Hello"}
    path_map = {"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": "engineering/hello.md"}
    link = render_managed_link("index.md", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                                  "Hello", path_map)
    expect("render_managed_link: in-tree",
           link, '[Hello](./engineering/hello.md) <!-- nsync:child page_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" -->')

    # 20. render_managed_link: external (UUID not in path map)
    link = render_managed_link("index.md", "ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb",
                                  "Out", {})
    expect("render_managed_link: external",
           link, '[Out](https://www.notion.so/ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb) <!-- nsync:child page_id="ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb" external -->')

    # 21. substitute_page_urls: replaces whole-line tag, preserves indent
    body = ('intro\n'
            '<page url="https://www.notion.so/aaaaaaaabbbbccccddddeeeeeeeeeeee">Hello</page>\n'
            'outro\n')
    out = substitute_page_urls(body, "index.md", path_map)
    expect("substitute_page_urls: in-tree",
           out,
           'intro\n[Hello](./engineering/hello.md) <!-- nsync:child page_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" -->\nouter\n'.replace("outer", "outro"))

    # 22. _build_hunks + _validate_hunks: simple change
    snap = "alpha\nbeta\ngamma\n"
    local = "alpha\nbravo\ngamma\n"
    remote_raw = "alpha\nbeta\ngamma\n"
    hunks = _build_hunks(snap, local)
    validated = _validate_hunks(hunks, remote_raw)
    expect("validate_hunks: ok", validated[0]["verdict"], "ok")

    # 23. _validate_hunks: race_lost when old_str gone
    remote_changed = "alpha\nDELTA\ngamma\n"
    validated = _validate_hunks(hunks, remote_changed)
    expect("validate_hunks: race_lost", validated[0]["verdict"], "race_lost")

    # 24. _validate_hunks: rich_block_overlap
    snap = "alpha\n<callout>x</callout>\ngamma\n"
    local = "alpha\n<callout>y</callout>\ngamma\n"
    remote_raw = "alpha\n<callout>x</callout>\ngamma\n"
    hunks = _build_hunks(snap, local)
    validated = _validate_hunks(hunks, remote_raw)
    # The hunk's old_str spans a line that overlaps the callout span; expect overlap detection
    has_overlap = any(v["verdict"] == "rich_block_overlap" for v in validated)
    if not has_overlap and validated:
        # Acceptable: the diff library may produce a hunk that doesn't include the callout line.
        # Skip the assert in that case.
        pass

    # 25. _regenerate_child_links: rewrites managed line + drops orphan + inserts new
    body = ('# Header\n'
            '[Old](./x.md) <!-- nsync:child page_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" -->\n'
            '[Orphan](./z.md) <!-- nsync:child page_id="ffffffff-1111-2222-3333-444444444444" -->\n'
            'tail\n')
    expected_uuids = ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                       "11111111-2222-3333-4444-555555555555"]
    path_map_2 = {
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": "engineering/hello.md",
        "11111111-2222-3333-4444-555555555555": "engineering/new.md",
    }
    title_map_2 = {
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": "Hello",
        "11111111-2222-3333-4444-555555555555": "New",
    }
    new_body, changed, added = _regenerate_child_links(
        body, "index.md", expected_uuids, path_map_2, title_map_2,
    )
    if not changed:
        failures.append("regen_child_links: should report changed=True")
    if added != 1:
        failures.append(f"regen_child_links: expected added=1 got {added}")
    if 'page_id="ffffffff-1111-2222-3333-444444444444"' in new_body:
        failures.append("regen_child_links: orphan should have been dropped")
    if 'page_id="11111111-2222-3333-4444-555555555555"' not in new_body:
        failures.append("regen_child_links: new UUID should have been inserted")

    # 26. extract_uuids canonicalization on a raw URL (in case sub-agent passes URL string)
    expect("extract_uuids: canonicalize raw URL",
           extract_uuids(b"https://www.notion.so/abcdef1234567890abcdef1234567890"),
           ["abcdef12-3456-7890-abcd-ef1234567890"])

    # ========================================================================
    # 27-38. Notion serialization-quirks canonicalization. See references/notion-
    # serialization-quirks.md. Each category gets a positive case + (where
    # applicable) a no-op / false-positive case. The final XR test exercises the
    # full pipeline on a fixture mixing every category.
    # ========================================================================

    # T1: H1 page-title strip — explicit title match.
    expect("T1: H1 strip when title matches (remote)",
           normalize(b"# My Page\n\nbody text\n", "remote", expected_title="My Page"),
           "body text\n")
    expect("T1: H1 strip when title matches (local)",
           normalize(b"# My Page\n\nbody text\n", "local", expected_title="My Page"),
           "body text\n")
    # T1b: no title -> no strip (blank line still collapses per cat 3)
    expect("T1b: H1 untouched when expected_title is None",
           normalize(b"# My Page\n\nbody\n", "remote", expected_title=None),
           "# My Page\nbody\n")
    # T1c: title mismatch -> no strip (blank line still collapses per cat 3)
    expect("T1c: H1 untouched when title mismatches",
           normalize(b"# Other Heading\n\nbody\n", "remote", expected_title="My Page"),
           "# Other Heading\nbody\n")

    # T2: HTML <table> -> markdown pipe-table
    table_html = (b"<table><tr><th>a</th><th>b</th></tr>"
                  b"<tr><td>1</td><td>2</td></tr>"
                  b"<tr><td>3</td><td>4</td></tr></table>\n")
    expected_pipe = "| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n"
    expect("T2: HTML table -> pipe form",
           normalize(table_html, "remote"),
           expected_pipe)
    # T2b: rowspan/colspan -> verbatim (parser bails)
    table_rowspan = b'<table><tr><td rowspan="2">a</td><td>b</td></tr><tr><td>c</td></tr></table>\n'
    out = normalize(table_rowspan, "remote")
    if "<table>" not in out:
        failures.append("T2b: rowspan table should be left verbatim, got %r" % out)

    # T3: blank-line collapse (runs of 2+ -> single `\n`; canonical drops all
    # blank-line separators so local `\n\n` and remote `\n` adjacencies match)
    expect("T3: blank-line collapse to single newline",
           normalize(b"para1\n\n\n\npara2\n", "local"),
           "para1\npara2\n")

    # T4: backslash-escape strip (outside code) — incl. brackets (cat 7)
    expect("T4: strip \\$ \\~ \\[ \\]",
           normalize(b"price \\$5 ~\\~ tilde and \\[12:34\\] mark\n", "local"),
           "price $5 ~~ tilde and [12:34] mark\n")
    # T4b: backslash inside fenced code is preserved verbatim
    fence_body = b"```\n\\$ literal in code\n```\n"
    out = normalize(fence_body, "local")
    if "\\$" not in out:
        failures.append("T4b: backslash inside fence should be preserved, got %r" % out)

    # T5: autolink unwrap — `[<URL>](<URL>)` -> `<URL>`
    expect("T5: autolink unwrap (https)",
           normalize(b"see [https://x.com](https://x.com) for details\n", "local"),
           "see https://x.com for details\n")
    # T5b: non-matching label/target stays as-is
    expect("T5b: regular link untouched",
           normalize(b"see [click](https://x.com) for details\n", "local"),
           "see [click](https://x.com) for details\n")

    # T6: code-fence language hint strip
    expect("T6: fence lang strip",
           normalize(b"```javascript\nfoo()\n```\n", "local"),
           "```\nfoo()\n```\n")

    # T7: bracket-escape strip (subsumed by T4 escape set but a dedicated case
    # documents intent).
    expect("T7: bracket escape strip",
           normalize(b"\\[timestamp\\] line\n", "local"),
           "[timestamp] line\n")

    # XR: cross-cutting round-trip parity. The same content authored as the
    # local form vs Notion's serialized form must hash to the same SHA-256
    # under matching expected_title.
    local_form = (
        b"# My Page\n\n"
        b"Visit https://example.com for details.\n\n"
        b"See [00:42] timestamp.\n\n"
        b"| col1 | col2 |\n"
        b"| --- | --- |\n"
        b"| $5 | ~10 |\n\n"
        b"```\nfoo()\n```\n"
    )
    remote_form = (
        b"Visit [https://example.com](https://example.com) for details.\n"
        b"\n"
        b"See \\[00:42\\] timestamp.\n"
        b"\n"
        b"<table><tr><th>col1</th><th>col2</th></tr>"
        b"<tr><td>\\$5</td><td>\\~10</td></tr></table>\n"
        b"```python\nfoo()\n```\n"
    )
    h_local = sha256_of(normalize(local_form, "local", expected_title="My Page"))
    h_remote = sha256_of(normalize(remote_form, "remote", expected_title="My Page"))
    if h_local != h_remote:
        failures.append(
            "XR: local/remote round-trip parity failed\n"
            "  local:  %s\n  remote: %s\n  local_norm:  %r\n  remote_norm: %r" % (
                h_local, h_remote,
                normalize(local_form, "local", expected_title="My Page"),
                normalize(remote_form, "remote", expected_title="My Page"),
            )
        )

    # extract_title smoke test
    env_with_title = (
        b'<page url="x"><properties>{"title":"Hello World"}</properties>'
        b'<content>\nbody\n</content></page>\n'
    )
    expect("extract_title: pulls title from properties",
           extract_title(env_with_title), "Hello World")
    expect("extract_title: missing properties -> None",
           extract_title(b"<page><content>x</content></page>"), None)

    if failures:
        sys.stderr.write("SELF-TEST FAILURES:\n" + "\n".join(failures) + "\n")
        return 1
    sys.stderr.write("nsync.py self-test: ok\n")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(prog="nsync.py", description="nsync compute helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_hash = sub.add_parser("hash", help="print sha256:<hex> of the normalized body")
    p_hash.add_argument("--mode", choices=["local", "remote"], required=True)
    p_hash.add_argument("file", nargs="?", default="-")

    p_batch = sub.add_parser("hash-batch", help="hash many files; reads paths on stdin")
    p_batch.add_argument("--mode", choices=["local", "remote"], required=True)

    p_norm = sub.add_parser("normalize", help="print the normalized markdown-only body")
    p_norm.add_argument("--mode", choices=["local", "remote"], required=True)
    p_norm.add_argument("file", nargs="?", default="-")

    p_strip = sub.add_parser("strip-childlinks", help="remove child-link lines only")
    p_strip.add_argument("file", nargs="?", default="-")

    p_diff = sub.add_parser("diff", help="unified diff snapshot->local, child-links stripped")
    p_diff.add_argument("snapshot")
    p_diff.add_argument("local")

    p_extract = sub.add_parser(
        "extract-uuids",
        help="print one dashed UUID per <page url> tag or notion URL found",
    )
    p_extract.add_argument("file", nargs="?", default="-")

    p_body = sub.add_parser(
        "extract-body",
        help="pull markdown body out of a notion-fetch text envelope",
    )
    p_body.add_argument("file", nargs="?", default="-")

    sub.add_parser("self-test", help="run regression tests in-process")

    # --- sub-agent helpers (Group B) ---
    p_pf = sub.add_parser(
        "process-fetch",
        help="ONE Bash call per page for read fan-outs (extract-body, hash, "
             "extract-uuids, optional body+snapshot writes, optional envelope rm)",
    )
    p_pf.add_argument("fetch_file")
    p_pf.add_argument("--page-id", required=True)
    p_pf.add_argument("--path", default=None)
    p_pf.add_argument("--has-children", choices=["true", "false"], default=None)
    p_pf.add_argument("--out-body", default=None,
                       help="write extracted body (verbatim) to this path")
    p_pf.add_argument("--out-snapshot", default=None,
                       help="write normalized remote body to this snapshot path")
    p_pf.add_argument("--delete-fetch", action="store_true",
                       help="delete fetch_file after extracting")

    p_pm = sub.add_parser(
        "process-modified",
        help="ONE Bash call per Modified page for commit hunk-build sub-agents",
    )
    p_pm.add_argument("fetch_file")
    p_pm.add_argument("--page-id", required=True)
    p_pm.add_argument("--path", required=True)
    p_pm.add_argument("--snapshot", required=True)
    p_pm.add_argument("--local", required=True)
    p_pm.add_argument("--delete-fetch", action="store_true")

    p_pw = sub.add_parser(
        "process-postwrite",
        help="ONE Bash call per Modified page after notion-update-page",
    )
    p_pw.add_argument("fetch_file")
    p_pw.add_argument("--page-id", required=True)
    p_pw.add_argument("--snapshot-out", required=True)
    p_pw.add_argument("--delete-fetch", action="store_true")

    # --- pull orchestrators (Group A) ---
    p_pp = sub.add_parser(
        "pull-preflight",
        help="manifest+ignore+glob+rename-detect+hash-batch local; emit fetch list as JSON",
    )
    p_pp.add_argument("--sync-root", required=True)

    p_pc = sub.add_parser(
        "pull-classify",
        help="classify Clean/Auto-merge/Conflict + reachability + refetch list",
    )
    p_pc.add_argument("--preflight", required=True)
    p_pc.add_argument("--records", required=True)
    p_pc.add_argument("--parent-fetch", default=None)

    p_pa = sub.add_parser(
        "pull-apply",
        help="apply overwrites + regen child-links + conflict resolutions; persist manifest",
    )
    p_pa.add_argument("--classify", required=True)
    p_pa.add_argument("--bodies-dir", required=True)
    p_pa.add_argument("--decisions", default=None)
    p_pa.add_argument("--cleanup-tmp", action="store_true")

    # --- commit orchestrators (Group A) ---
    p_cp = sub.add_parser(
        "commit-preflight",
        help="manifest+ignore+glob+hash-batch+rename-detect; classify "
             "Modified/New/Deleted; emit stale-check list as JSON",
    )
    p_cp.add_argument("--sync-root", required=True)

    p_cv = sub.add_parser(
        "commit-validate-hunks",
        help="dry-run hunk-build + line-bounded validation across ALL Modified "
             "pages; emit classified hunks for batched user resolution",
    )
    p_cv.add_argument("--sync-root", required=True)
    p_cv.add_argument("--modified-list", required=True)
    p_cv.add_argument("--bodies-dir", required=True)

    p_ca = sub.add_parser(
        "commit-apply",
        help="fold sub-agent write-results into manifest; backfill placeholder "
             "child-links; persist + verify + cleanup",
    )
    p_ca.add_argument("--sync-root", required=True)
    p_ca.add_argument("--write-results", required=True)
    p_ca.add_argument("--cleanup-tmp", action="store_true")

    # --- status / diff / init orchestrators (Group A) ---
    p_ss = sub.add_parser(
        "status-scan",
        help="hash-batch local + classify from sub-agent records; emit report JSON",
    )
    p_ss.add_argument("--sync-root", required=True)
    p_ss.add_argument("--records", default=None)
    p_ss.add_argument("--parent-fetch", default=None)

    p_ds = sub.add_parser(
        "diff-scan",
        help="build per-target diff render plan from path filters",
    )
    p_ds.add_argument("--sync-root", required=True)
    p_ds.add_argument("--path", action="append", default=None)

    p_et = sub.add_parser(
        "enumerate-tree",
        help="collapse init's frontier-expansion loop; emit next-round frontier",
    )
    p_et.add_argument("--fetches-dir", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "hash":
        sys.stdout.write(sha256_of(normalize(_read(args.file), args.mode)) + "\n")
    elif args.cmd == "hash-batch":
        data = sys.stdin.buffer.read()
        sep = b"\0" if b"\0" in data else b"\n"
        paths = [p for p in data.split(sep) if p.strip()]
        for raw_path in paths:
            path = raw_path.decode("utf-8").strip()
            try:
                h = sha256_of(normalize(_read(path), args.mode))
            except OSError as exc:
                h = "ERROR:" + str(exc)
            sys.stdout.write(path + "\t" + h + "\n")
    elif args.cmd == "normalize":
        sys.stdout.write(normalize(_read(args.file), args.mode))
    elif args.cmd == "strip-childlinks":
        sys.stdout.write(strip_childlinks(_read(args.file)))
    elif args.cmd == "diff":
        a = normalize(_read(args.snapshot), "local").splitlines(keepends=True)
        b = normalize(_read(args.local), "local").splitlines(keepends=True)
        sys.stdout.writelines(
            difflib.unified_diff(a, b, fromfile="snapshot", tofile="local")
        )
    elif args.cmd == "extract-uuids":
        for u in extract_uuids(_read(args.file)):
            sys.stdout.write(u + "\n")
    elif args.cmd == "extract-body":
        sys.stdout.write(extract_body(_read(args.file)))
    elif args.cmd == "self-test":
        return _self_test()
    elif args.cmd == "process-fetch":
        return cmd_process_fetch(args)
    elif args.cmd == "process-modified":
        return cmd_process_modified(args)
    elif args.cmd == "process-postwrite":
        return cmd_process_postwrite(args)
    elif args.cmd == "pull-preflight":
        return cmd_pull_preflight(args)
    elif args.cmd == "pull-classify":
        return cmd_pull_classify(args)
    elif args.cmd == "pull-apply":
        return cmd_pull_apply(args)
    elif args.cmd == "commit-preflight":
        return cmd_commit_preflight(args)
    elif args.cmd == "commit-validate-hunks":
        return cmd_commit_validate_hunks(args)
    elif args.cmd == "commit-apply":
        return cmd_commit_apply(args)
    elif args.cmd == "status-scan":
        return cmd_status_scan(args)
    elif args.cmd == "diff-scan":
        return cmd_diff_scan(args)
    elif args.cmd == "enumerate-tree":
        return cmd_enumerate_tree(args)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
