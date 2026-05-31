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
]

# <page url ...>...</page> child references — stripped on BOTH sides (step 7). Notion
# usually serializes these on one line, but handle a paired span defensively.
PAGE_SPAN_RE = re.compile(r'<page\b[^>]*>.*?</page>', re.DOTALL | re.IGNORECASE)
PAGE_SELFCLOSE_RE = re.compile(r'<page\b[^>]*?/?>', re.IGNORECASE)
PAGE_LINE_RE = re.compile(r'^<page\b.*</page>$', re.IGNORECASE)


def _strip_paired(text, tag):
    return re.sub(
        r'<' + tag + r'\b[^>]*>.*?</' + tag + r'>',
        '', text, flags=re.DOTALL | re.IGNORECASE,
    )


def _strip_selfclose(text, tag):
    return re.sub(r'<' + tag + r'\b[^>]*?/?>', '', text, flags=re.IGNORECASE)


def normalize(raw_bytes, mode):
    """Apply the manifest-schema.md hash pipeline. mode is 'local' or 'remote'."""
    # 1. UTF-8 decode.
    text = raw_bytes.decode("utf-8")
    # 2. NFC unicode normalization.
    text = unicodedata.normalize("NFC", text)
    # 3. LF line endings only.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 6. remote_hash only: strip rich-block tags and their contents (spans first,
    #    before the line pass, because paired tags span multiple lines).
    if mode == "remote":
        for tag in PAIRED_RICH_TAGS:
            text = _strip_paired(text, tag)
        for tag in SELFCLOSE_RICH_TAGS:
            text = _strip_selfclose(text, tag)

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


def sha256_of(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path):
    if path is None or path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as fh:
        return fh.read()


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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
