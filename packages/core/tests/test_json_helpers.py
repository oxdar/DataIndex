from __future__ import annotations

import json
from collections import OrderedDict
from datetime import date
from decimal import Decimal
from types import MappingProxyType

import pytest

from dataindex._json import MAX_JSON_DEPTH, locator_json, plain_json
from dataindex.models import NativeLocator


def nested_dicts(levels: int) -> dict[str, object]:
    value: object = 1
    for _ in range(levels):
        value = {"a": value}
    assert isinstance(value, dict)
    return value


def nested_lists(levels: int) -> list[object]:
    value: object = 1
    for _ in range(levels):
        value = [value]
    assert isinstance(value, list)
    return value


@pytest.mark.parametrize(
    "value",
    [None, True, False, 0, -7, 2**70, 1.5, -0.0, "", "text", "\u00fcn\u00ef\u00e7\u00f6de"],
)
def test_scalars_pass_through_unchanged(value: object) -> None:
    result = plain_json(value)

    assert result == value
    assert type(result) is type(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("wrap", [lambda v: v, lambda v: {"k": v}, lambda v: [1, (v,)]])
def test_non_finite_numbers_are_rejected_wherever_they_appear(value: float, wrap: object) -> None:
    with pytest.raises(ValueError, match="must be finite"):
        plain_json(wrap(value))  # type: ignore[operator]


def test_mappings_become_plain_dicts_with_string_keys() -> None:
    result = plain_json(
        {
            "a": 1,
            2: "two",
            "ordered": OrderedDict(x=1),
            "proxy": MappingProxyType({"y": [1]}),
        }
    )

    assert result == {"a": 1, "2": "two", "ordered": {"x": 1}, "proxy": {"y": [1]}}
    assert type(result) is dict
    assert type(result["ordered"]) is dict  # type: ignore[index]
    assert type(result["proxy"]) is dict  # type: ignore[index]


def test_lists_and_tuples_become_lists() -> None:
    result = plain_json({"a": (1, (2, 3)), "b": [(4,), []], "c": ()})

    assert result == {"a": [1, [2, 3]], "b": [[4], []], "c": []}
    assert type(result["a"]) is list  # type: ignore[index]
    assert type(result["a"][1]) is list  # type: ignore[index]


def test_the_result_is_a_copy() -> None:
    source = {"items": [1, {"deep": [2]}]}

    result = plain_json(source)
    source["items"].append(3)  # type: ignore[union-attr]
    source["items"][1]["deep"].append(9)  # type: ignore[index]

    assert result == {"items": [1, {"deep": [2]}]}


def test_the_result_is_json_serializable() -> None:
    result = plain_json({"a": [1, 2.5, None, True, "x", {"b": (1,)}], 3: "n"})

    assert json.loads(json.dumps(result)) == result


@pytest.mark.parametrize(
    ("value", "type_name"),
    [
        ({1, 2}, "set"),
        (frozenset({1}), "frozenset"),
        (b"bytes", "bytes"),
        (bytearray(b"bytes"), "bytearray"),
        (object(), "object"),
        (Decimal("1.5"), "Decimal"),
        (date(2024, 1, 1), "date"),
        (1 + 2j, "complex"),
        (range(3), "range"),
        (lambda: 1, "function"),
    ],
)
def test_unsupported_types_are_rejected_by_type_name(value: object, type_name: str) -> None:
    with pytest.raises(ValueError, match=f"unsupported value type {type_name}$"):
        plain_json(value)


def test_one_unsupported_value_rejects_the_whole_structure() -> None:
    with pytest.raises(ValueError, match="unsupported value type set"):
        plain_json({"fine": [1, 2], "bad": {"inner": [{1}]}})


def test_the_error_names_the_type_but_never_the_value() -> None:
    class Credential:
        def __repr__(self) -> str:
            return "TOP-SECRET-TOKEN"

        __str__ = __repr__

    with pytest.raises(ValueError) as caught:
        plain_json({"auth": Credential()})

    assert "Credential" in str(caught.value)
    assert "TOP-SECRET-TOKEN" not in str(caught.value)


@pytest.mark.parametrize("build", [nested_dicts, nested_lists], ids=["dicts", "lists"])
def test_nesting_is_limited(build: object) -> None:
    allowed = build(MAX_JSON_DEPTH)  # type: ignore[operator]
    assert plain_json(allowed) == allowed

    with pytest.raises(ValueError, match="nested too deeply"):
        plain_json(build(MAX_JSON_DEPTH + 1))  # type: ignore[operator]


def test_self_referencing_values_fail_with_a_value_error_instead_of_recursing() -> None:
    cycle_list: list[object] = []
    cycle_list.append(cycle_list)
    cycle_dict: dict[str, object] = {}
    cycle_dict["self"] = cycle_dict

    with pytest.raises(ValueError, match="nested too deeply"):
        plain_json(cycle_list)
    with pytest.raises(ValueError, match="nested too deeply"):
        plain_json(cycle_dict)


def test_wide_structures_are_not_limited() -> None:
    wide = {f"k{index}": [index] for index in range(5_000)}

    assert plain_json(wide) == wide


def test_locator_json_has_scheme_version_and_plain_value() -> None:
    locator = NativeLocator("text-lines", 2, {"line_start": 1, "line_end": 9, "path": {"k": [1, 2]}})

    result = locator_json(locator)

    assert result == {
        "scheme": "text-lines",
        "version": 2,
        "value": {"line_start": 1, "line_end": 9, "path": {"k": [1, 2]}},
    }
    assert json.loads(json.dumps(result)) == result


def test_locator_json_copies_the_locator_value() -> None:
    locator = NativeLocator("sqlite-row", 1, {"table": "orders", "rowid": 7})

    result = locator_json(locator)
    result["value"]["table"] = "changed"  # type: ignore[index]

    assert locator.value["table"] == "orders"


def test_locator_json_accepts_read_only_mapping_values() -> None:
    locator = NativeLocator("sqlite-row", 1, MappingProxyType({"table": "orders"}))

    result = locator_json(locator)

    assert type(result["value"]) is dict
    assert result["value"] == {"table": "orders"}
