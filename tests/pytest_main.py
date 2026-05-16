from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def main() -> int:
    runfiles_root = Path(__file__).resolve().parents[1]
    test_tmp = os.environ.get("TEST_TMPDIR")
    if test_tmp:
        os.environ["HOME"] = test_tmp

    os.chdir(runfiles_root)
    return pytest.main(
        [
            str(runfiles_root / "tests"),
            "-p",
            "no:cacheprovider",
        ]
    )


if __name__ == "__main__":
    sys.exit(main())
