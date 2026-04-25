"""Smoke-test entry point.

Run with ``python -m expense_tracker`` or, after ``pip install -e .``, ``expense``.
This will be replaced by a real CLI in a later commit; for now it just proves
the package is importable and the install plumbing works.
"""

from __future__ import annotations

from . import __version__


def main() -> None:
    print(f"expense_tracker scaffold OK (v{__version__})")


if __name__ == "__main__":
    main()
