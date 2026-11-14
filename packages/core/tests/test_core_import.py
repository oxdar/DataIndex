from __future__ import annotations

import subprocess
import sys


def test_core_import_does_not_import_optional_extensions() -> None:
    script = """
import builtins
original_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name.startswith("dataindex_ext_"):
        raise AssertionError(f"optional extension imported: {name}")
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
import dataindex
assert dataindex.__version__ == "0.1.0"
from pathlib import Path
notice = Path(dataindex.__file__).with_name("THIRD_PARTY_NOTICES.md")
assert notice.is_file()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
