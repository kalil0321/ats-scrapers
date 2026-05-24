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

HTML_BLOCK_RE = re.compile(
    r"<(?:p|div|ul|ol|li|h[1-6]|br|table|tr|td|a|strong|em|b|i|span|section|article|hr|blockquote)\b",
    re.IGNORECASE,
)
HTML_ANY_TAG_RE = re.compile(r"<[a-z][a-z0-9]*\b[^>]*>", re.IGNORECASE)
HTML_ENTITY_RE = re.compile(r"&(?:nbsp|amp|lt|gt|quot|#\d+|[a-z]{2,8});", re.IGNORECASE)
BLANK_RUN_RE = re.compile(r"\n{3,}")
WS_RUN_RE = re.compile(r"\s+")


def normalize_one(s):
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
        return (BLANK_RUN_RE.sub("\n\n", out).strip() or None)
    if HTML_ANY_TAG_RE.search(s):
        out = re.sub(r"<[^>]+>", "", s)
        out = html.unescape(out)
        return (WS_RUN_RE.sub(" ", out).strip() or None)
    if HTML_ENTITY_RE.search(s):
        return (html.unescape(s).strip() or None)
    return s


def _normalize_descs(descs):
    """Worker: list[str|None] → list[str|None]."""
    return [normalize_one(d) for d in descs]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("csv_path", type=Path)
    p.add_argument("-j", "--workers", type=int, default=max(1, multiprocessing.cpu_count() - 1))
    p.add_argument("--chunk", type=int, default=2000)
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

    tmp = tempfile.NamedTemporaryFile(
        "w", newline="", delete=False,
        dir=args.csv_path.parent,
        prefix=f".{args.csv_path.name}.normalizing.",
    )

    pool = multiprocessing.Pool(args.workers)

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
    finally:
        pool.close()
        pool.join()
        tmp.close()

    Path(tmp.name).replace(args.csv_path)
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
    for row, new_desc in zip(rows, normalized):
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
