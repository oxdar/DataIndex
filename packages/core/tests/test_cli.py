from __future__ import annotations

from dataindex.cli import main


def test_extensions_list_works_without_plugins(capsys) -> None:
    assert main(["extensions", "list"]) == 0
    assert capsys.readouterr().out == "No extensions discovered.\n"
