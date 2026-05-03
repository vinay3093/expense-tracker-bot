#!/usr/bin/env python3
"""Rebuild docs/HANDBOOK.docx from docs/HANDBOOK.md.

Run this whenever the handbook changes so the downloadable Word
version stays in sync.

Usage:
    python scripts/build_handbook.py

Requires `pypandoc-binary` (already a dev dependency). The Word file
is regenerated from the markdown with a table of contents and standard
heading styles.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "HANDBOOK.md"
TARGET = ROOT / "docs" / "HANDBOOK.docx"


def main() -> int:
    if not SOURCE.exists():
        print(f"error: {SOURCE} not found", file=sys.stderr)
        return 1

    try:
        import pypandoc
    except ImportError:
        print(
            "error: pypandoc not installed.\n"
            "       run: pip install pypandoc-binary",
            file=sys.stderr,
        )
        return 1

    pypandoc.convert_file(
        str(SOURCE),
        "docx",
        outputfile=str(TARGET),
        extra_args=["--toc", "--toc-depth=3", "--standalone"],
    )

    src_kb = SOURCE.stat().st_size / 1024
    out_kb = TARGET.stat().st_size / 1024
    print(f"built {TARGET.name} ({out_kb:.1f} KB) from {SOURCE.name} ({src_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
