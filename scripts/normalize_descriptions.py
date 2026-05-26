#!/usr/bin/env python3
"""Normalize the `description` column of a jobhive jobs.csv in place,
streaming chunk-by-chunk so memory stays bounded.

Workflow:
  - Stream-read input CSV row by row
  - Buffer rows into chunks (default 2000)
  - Dispatch each chunk to a worker pool for parallel normalization
  - Stream-write to a temp file
  - On EOF: atomic rename temp → input
"""
from __future__ import annotations

import argparse
import csv
import html
import multiprocessing
import re
import sys
import tempfile
import time
from pathlib import Path

_MD = None
def _md_lazy():
    global _MD
    if _MD is None:
        from markdownify import markdownify as md
        _MD = md
    return _MD


_LINKIFY = None
def _linkify_lazy():
    """linkify-it-py is the canonical URL/email/IP detector and the same
    library markdown-it-py uses for its linkify plugin. Lazy-import so the
    worker subprocess only pays the import cost once per process."""
    global _LINKIFY
    if _LINKIFY is None:
        from linkify_it import LinkifyIt
        from linkify_it.tlds import TLDS
        instance = LinkifyIt()
        instance.tlds(TLDS)  # full ICANN/IANA TLD set
        _LINKIFY = instance
    return _LINKIFY


def autolink(text: str) -> str:
    """Wrap bare URLs and emails in CommonMark autolink syntax (``<url>``).

    Markdownify converts real ``<a href=…>`` anchors to ``[text](url)``
    already; this pass picks up the URLs that were rendered as plain
    text in the source (typical in plain-text job postings: "apply at
    https://acme.com/jobs"). We skip URLs that are already part of a
    markdown link, an existing autolink, or an image — splicing inside
    those would double-wrap.

    Email addresses get ``<addr@host>`` which most markdown renderers
    auto-link as ``mailto:`` links.
    """
    if not text:
        return text
    linkify = _linkify_lazy()
    matches = linkify.match(text)
    if not matches:
        return text
    # Walk matches in reverse so earlier indices stay valid as we splice.
    out = text
    for m in reversed(matches):
        start, end = m.index, m.last_index
        # Skip if already inside an existing markdown link or autolink
        # by checking the chars immediately around the match.
        before = out[max(0, start - 2):start]
        after = out[end:end + 2]
        if before.endswith("](") or before.endswith("<") or after.startswith(">"):
            continue
        # Skip the markdown image syntax ``![alt](url)`` too.
        if before.endswith("!"):
            continue
        # ``m.url`` is linkify-it's normalized form: emails get a
        # ``mailto:`` prefix automatically. Using it as the autolink
        # body would corrupt the visible text (``foo@bar.com`` would
        # render as ``mailto:foo@bar.com`` in viewers that don't
        # collapse the scheme). The raw substring in ``out`` is what
        # the source wrote, so use that and rely on CommonMark/GFM to
        # apply ``mailto:`` at render time.
        original = out[start:end]
        replacement = f"<{original}>"
        out = out[:start] + replacement + out[end:]
    return out


HTML_BLOCK_RE = re.compile(
    r"<(?:p|div|ul|ol|li|h[1-6]|br|table|tr|td|a|strong|em|b|i|span|section|article|hr|blockquote)\b",
    re.IGNORECASE,
)
HTML_ANY_TAG_RE = re.compile(r"<[a-z][a-z0-9]*\b[^>]*>", re.IGNORECASE)
HTML_ENTITY_RE = re.compile(r"&(?:nbsp|amp|lt|gt|quot|#\d+|[a-z]{2,8});", re.IGNORECASE)
BLANK_RUN_RE = re.compile(r"\n{3,}")
WS_RUN_RE = re.compile(r"\s+")


def normalize_one(s):
    """Pipeline: route to markdownify / strip-unescape / fast-path, then
    apply autolink to whatever ended up in the output. Bare URLs and
    emails that survived earlier branches become CommonMark autolinks.
    """
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    if HTML_BLOCK_RE.search(s):
        try:
            out = _md_lazy()(
                s, heading_style="ATX", strip=["script", "style"],
                bullets="-", escape_underscores=False, wrap=False,
            )
        except Exception:
            out = re.sub(r"<[^>]+>", "", s)
            out = html.unescape(out)
        out = BLANK_RUN_RE.sub("\n\n", out).strip()
        return autolink(out) or None
    if HTML_ANY_TAG_RE.search(s):
        out = re.sub(r"<[^>]+>", "", s)
        out = html.unescape(out)
        out = WS_RUN_RE.sub(" ", out).strip()
        return autolink(out) or None
    if HTML_ENTITY_RE.search(s):
        out = html.unescape(s).strip()
        return autolink(out) or None
    return autolink(s)


def _normalize_descs(descs):
    """Worker: list[str|None] → list[str|None]."""
    return [normalize_one(d) for d in descs]


def _positive_int(value: str) -> int:
    """argparse type that rejects 0 and negative values. Used for
    ``--chunk`` and ``-j`` because both feed into denominators in the
    progress logger and worker fan-out — a non-positive value would
    raise ZeroDivisionError or spawn 0 workers."""
    try:
        ivalue = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected integer, got {value!r}") from exc
    if ivalue < 1:
        raise argparse.ArgumentTypeError(
            f"must be >= 1 (got {ivalue})"
        )
    return ivalue


def main():
    p = argparse.ArgumentParser()
    p.add_argument("csv_path", type=Path)
    p.add_argument("-j", "--workers", type=_positive_int,
                   default=max(1, multiprocessing.cpu_count() - 1))
    p.add_argument("--chunk", type=_positive_int, default=2000)
    p.add_argument("--column", default="description")
    args = p.parse_args()

    if not args.csv_path.exists():
        print(f"missing {args.csv_path}", file=sys.stderr)
        return 1

    csv.field_size_limit(sys.maxsize)
    print(f"normalize {args.csv_path} (-j {args.workers}, chunk={args.chunk}, column={args.column})", flush=True)
    t0 = time.time()
    counts = {"unchanged": 0, "shrunk": 0, "nulled": 0, "grew": 0, "newly_set": 0}
    total = 0

    # The streaming-write design needs the handle to live past the
    # open() call; we close + atomically rename in the finally below.
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
        "w", newline="", delete=False,
        dir=args.csv_path.parent,
        prefix=f".{args.csv_path.name}.normalizing.",
    )
    tmp_path = Path(tmp.name)

    pool = multiprocessing.Pool(args.workers)

    success = False
    try:
        with args.csv_path.open(newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            if args.column not in fieldnames:
                print(f"column '{args.column}' missing", file=sys.stderr)
                return 2
            writer = csv.DictWriter(tmp, fieldnames=fieldnames)
            writer.writeheader()

            buffer_rows = []
            for row in reader:
                buffer_rows.append(row)
                if len(buffer_rows) >= args.chunk:
                    _process_chunk(buffer_rows, args.column, pool, writer, counts)
                    total += len(buffer_rows)
                    buffer_rows = []
                    if total % (args.chunk * 10) == 0:
                        elapsed = time.time() - t0
                        rate = total / max(elapsed, 0.001)
                        print(f"  {total:,} rows · {rate:,.0f}/s · "
                              f"unchanged={counts['unchanged']:,} shrunk={counts['shrunk']:,} "
                              f"grew={counts['grew']:,} nulled={counts['nulled']:,}",
                              flush=True)

            if buffer_rows:
                _process_chunk(buffer_rows, args.column, pool, writer, counts)
                total += len(buffer_rows)
            success = True
    finally:
        pool.close()
        pool.join()
        tmp.close()
        # Always clean up the half-written temp on any non-happy path
        # (missing column, exception in a worker, ctrl-c). The atomic
        # rename below only fires on the success path, so leaving the
        # file behind here would litter the workday/ directory with
        # ``.jobs.csv.normalizing.*`` orphans across reruns.
        if not success:
            tmp_path.unlink(missing_ok=True)

    tmp_path.replace(args.csv_path)
    elapsed = time.time() - t0
    print(f"DONE total={total:,} in {elapsed:.1f}s · {counts}", flush=True)
    return 0


def _process_chunk(rows, column, pool, writer, counts):
    """Normalize the column for `rows` (in-place) and write them out."""
    descs = [r.get(column) for r in rows]
    # Process the descs through the pool. We split into N sub-batches for
    # parallelism within this chunk.
    n_workers = pool._processes
    sub_size = max(1, len(descs) // n_workers + 1)
    sub_batches = [descs[i:i+sub_size] for i in range(0, len(descs), sub_size)]
    results = pool.map(_normalize_descs, sub_batches)
    # Flatten
    normalized = [d for sub in results for d in sub]
    for row, new_desc in zip(rows, normalized, strict=True):
        old = row.get(column)
        if new_desc == old:
            counts["unchanged"] += 1
        elif new_desc is None:
            counts["nulled"] += 1
        elif old is None or old == "":
            counts["newly_set"] += 1
        elif len(new_desc) < len(old):
            counts["shrunk"] += 1
        else:
            counts["grew"] += 1
        row[column] = new_desc or ""
        writer.writerow(row)


if __name__ == "__main__":
    sys.exit(main())
