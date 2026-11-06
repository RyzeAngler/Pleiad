import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pleiad.contracts import JsonObject

from pleiad_weave.capabilities.tools import JSON_OBJECT_ADAPTER, ToolBinding

CSV_TOOL = Path(__file__).resolve().parents[3] / "workspace" / "tools" / "csv_profile.json"


def run_csv_profile(workspace: Path, arguments: JsonObject) -> subprocess.CompletedProcess[str]:
    binding = ToolBinding.model_validate_json(CSV_TOOL.read_bytes())
    Draft202012Validator(binding.input_schema).validate(arguments)
    result = subprocess.run(  # noqa: S603 - Run the fixed repository script with the current test interpreter.
        (sys.executable, str(CSV_TOOL.parent / binding.script)),
        input=json.dumps(arguments),
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode == 0:
        output = JSON_OBJECT_ADAPTER.validate_json(result.stdout)
        Draft202012Validator(binding.output_schema).validate(output)
    return result


def test_csv_profile_preserves_quoted_text_and_counts_empty_cells(tmp_path: Path) -> None:
    (tmp_path / "orders.csv").write_text(
        '\ufeffname,amount,code\n"one, ""quoted""",1.5,001\n\n"two\nlines",,002\n,not-a-number,003\n spaced ,2.5,\n',
        encoding="utf-8",
    )

    result = run_csv_profile(tmp_path, {"path": "orders.csv", "numeric_columns": ["amount"], "sample_size": 2})

    assert result.returncode == 0, result.stderr
    assert JSON_OBJECT_ADAPTER.validate_json(result.stdout) == {
        "path": "orders.csv",
        "row_count": 4,
        "columns": [
            {"name": "name", "empty_count": 1, "samples": ['one, "quoted"', "two\nlines"], "numeric": None},
            {
                "name": "amount",
                "empty_count": 1,
                "samples": ["1.5", "not-a-number"],
                "numeric": {"valid_count": 2, "invalid_count": 1, "minimum": 1.5, "maximum": 2.5, "mean": 2.0},
            },
            {"name": "code", "empty_count": 1, "samples": ["001", "002"], "numeric": None},
        ],
    }


def test_csv_profile_does_not_infer_types_or_normalize_text_and_limits_samples(tmp_path: Path) -> None:
    (tmp_path / "values.csv").write_text(f"code\n001\n \n{'x' * 250}\nNULL\n", encoding="utf-8")

    result = run_csv_profile(tmp_path, {"path": "values.csv"})

    assert result.returncode == 0, result.stderr
    assert JSON_OBJECT_ADAPTER.validate_json(result.stdout) == {
        "path": "values.csv",
        "row_count": 4,
        "columns": [{"name": "code", "empty_count": 0, "samples": ["001", " ", "x" * 200], "numeric": None}],
    }


def test_csv_profile_excludes_nonfinite_numbers_without_overflowing_the_mean(tmp_path: Path) -> None:
    (tmp_path / "values.csv").write_text(
        'amount\nNaN\nsNaN\nInfinity\n-infinity\n1e9999\nbad\n""\n1e308\n1e308\n', encoding="utf-8"
    )

    result = run_csv_profile(tmp_path, {"path": "values.csv", "numeric_columns": ["amount"], "sample_size": 0})

    assert result.returncode == 0, result.stderr
    assert JSON_OBJECT_ADAPTER.validate_json(result.stdout) == {
        "path": "values.csv",
        "row_count": 9,
        "columns": [
            {
                "name": "amount",
                "empty_count": 1,
                "samples": [],
                "numeric": {"valid_count": 2, "invalid_count": 6, "minimum": 1e308, "maximum": 1e308, "mean": 1e308},
            }
        ],
    }


def test_csv_profile_header_only_has_no_numeric_statistics(tmp_path: Path) -> None:
    (tmp_path / "values.csv").write_text("amount\n", encoding="utf-8")

    result = run_csv_profile(tmp_path, {"path": "values.csv", "numeric_columns": ["amount"]})

    assert result.returncode == 0, result.stderr
    assert JSON_OBJECT_ADAPTER.validate_json(result.stdout) == {
        "path": "values.csv",
        "row_count": 0,
        "columns": [
            {
                "name": "amount",
                "empty_count": 0,
                "samples": [],
                "numeric": {"valid_count": 0, "invalid_count": 0, "minimum": None, "maximum": None, "mean": None},
            }
        ],
    }


@pytest.mark.parametrize("escape", ["parent", "absolute", "symlink"])
def test_csv_profile_rejects_paths_outside_the_workspace(tmp_path: Path, escape: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "private.csv"
    outside.write_text("secret\nshould-not-be-returned\n", encoding="utf-8")
    path = "../private.csv"
    if escape == "absolute":
        path = str(outside)
    elif escape == "symlink":
        (workspace / "linked.csv").symlink_to(outside)
        path = "linked.csv"

    result = run_csv_profile(workspace, {"path": path})

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Path escapes the configured workspace" in result.stderr
    assert "should-not-be-returned" not in result.stderr


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("", "CSV must have a non-empty, unique header"),
        ("name,name\na,b\n", "CSV must have a non-empty, unique header"),
        ("name,\na,b\n", "CSV must have a non-empty, unique header"),
        ("name,amount\na\n", "does not match the header column count"),
        ("name,amount\na,1,extra\n", "does not match the header column count"),
        ('name\n"unclosed\n', "unexpected end of data"),
    ],
)
def test_csv_profile_rejects_malformed_records_instead_of_dropping_values(
    tmp_path: Path, content: str, error: str
) -> None:
    (tmp_path / "broken.csv").write_text(content, encoding="utf-8")

    result = run_csv_profile(tmp_path, {"path": "broken.csv"})

    assert result.returncode != 0
    assert result.stdout == ""
    assert error in result.stderr


def test_csv_profile_rejects_an_unknown_numeric_column(tmp_path: Path) -> None:
    (tmp_path / "values.csv").write_text("amount\n1\n", encoding="utf-8")

    result = run_csv_profile(tmp_path, {"path": "values.csv", "numeric_columns": ["missing"]})

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Unknown numeric columns: missing" in result.stderr
