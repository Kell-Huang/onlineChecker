"""Run the whole test suite.

    python tests/run_tests.py            # everything
    python tests/run_tests.py tests/test_core.py -k report

Thin wrapper around pytest that sets the two environment variables Qt needs
on a headless Windows box: the offscreen platform, and a font directory.
Without ``QT_QPA_FONTDIR`` the offscreen platform has an empty font database
and every glyph renders as a box.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

import pytest  # noqa: E402  (import after the environment is prepared)


def main() -> int:
    args = sys.argv[1:] or ["tests"]
    return pytest.main(["-q", *args])


if __name__ == "__main__":
    sys.exit(main())
