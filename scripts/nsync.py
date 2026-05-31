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
  self-test                                -> run in-process regression tests

FILE omitted (or "-") reads stdin.
"""

import argparse
import difflib
import hashlib
import re
import sys
import unicodedata

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


def normalize(raw_bytes, mode):
    """Apply the manifest-schema.md hash pipeline. mode is 'local' or 'remote'."""
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
    r'(?:notion\.so|notion\.site)/(?:[a-zA-Z0-9_\-]*?-)?([0-9a-fA-F][0-9a-fA-F\-]{31,40})',
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


def sha256_of(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path):
    if path is None or path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as fh:
        return fh.read()


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
    expect("unknown <frobnicator> stripped", out, "before\n\nafter\n")
    if "frobnicator" not in _UNKNOWN_TAG_WARNED:
        failures.append("unknown <frobnicator> should have warned")

    # 3. unknown self-closing tag stripped.
    _UNKNOWN_TAG_WARNED.clear()
    body = b'before\n<custom-widget data="x"/>\nafter\n'
    out = normalize(body, "remote")
    expect("unknown <custom-widget/> stripped", out, "before\n\nafter\n")

    # 4. recognized rich block stripped (callout, multi-line).
    body = b'a\n<callout icon="i">\ninside\n</callout>\nb\n'
    expect("callout span removed", normalize(body, "remote"), "a\n\nb\n")

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
    expect("page url line removed (local)", normalize(body, "local"), "a\n\nb\n")

    # 11. CRLF -> LF.
    body = b'x\r\ny\r\n'
    expect("CRLF normalized", normalize(body, "local"), "x\ny\n")

    # 12. NFC normalization (precomposed vs decomposed).
    body = "é".encode() + b'\n'  # precomposed
    decomposed = b'e\xcc\x81\n'
    expect("NFC: precomposed body", normalize(body, "local"),
           normalize(decomposed, "local"))

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

    sub.add_parser("self-test", help="run regression tests in-process")

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
    elif args.cmd == "self-test":
        return _self_test()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
