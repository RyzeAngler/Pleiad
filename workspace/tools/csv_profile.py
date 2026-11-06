import csv
import json
import math
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TypedDict


class NumericSummary(TypedDict):
    """Describe finite numbers in an explicitly selected column."""

    valid_count: int
    invalid_count: int
    minimum: float | None
    maximum: float | None
    mean: float | None


@dataclass(slots=True)
class NumericProfile:
    """Accumulate finite numeric values without retaining every row."""

    valid_count: int = 0
    invalid_count: int = 0
    minimum: float | None = None
    maximum: float | None = None
    total: Decimal = field(default_factory=Decimal)

    def observe(self, value: str) -> None:
        """Count a non-empty cell as a finite number or an invalid value."""
        try:
            decimal = Decimal(value)
        except InvalidOperation:
            self.invalid_count += 1
            return
        if not decimal.is_finite():
            self.invalid_count += 1
            return
        number = float(decimal)
        if not math.isfinite(number):
            self.invalid_count += 1
            return
        self.valid_count += 1
        self.minimum = number if self.minimum is None else min(self.minimum, number)
        self.maximum = number if self.maximum is None else max(self.maximum, number)
        self.total += decimal

    def summary(self) -> NumericSummary:
        """Return numeric statistics; an empty column has no extrema or mean."""
        return {
            "valid_count": self.valid_count,
            "invalid_count": self.invalid_count,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": float(self.total / self.valid_count) if self.valid_count else None,
        }


def main() -> None:
    """Read host-validated JSON stdin and profile a workspace UTF-8 CSV file."""
    arguments = json.load(sys.stdin)
    root = Path.cwd().resolve()
    path = (root / arguments["path"]).resolve()
    if not path.is_relative_to(root):
        raise PermissionError("Path escapes the configured workspace")
    numeric_columns = set(arguments.get("numeric_columns", []))
    sample_size = arguments.get("sample_size", 3)
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, strict=True)
        header = next(reader, None)
        if not header or any(not name.strip() for name in header) or len(set(header)) != len(header):
            raise ValueError("CSV must have a non-empty, unique header for every column")
        unknown_columns = numeric_columns.difference(header)
        if unknown_columns:
            raise ValueError(f"Unknown numeric columns: {', '.join(sorted(unknown_columns))}")
        empty_counts = [0] * len(header)
        samples: list[list[str]] = [[] for name in header]
        numbers = {name: NumericProfile() for name in header if name in numeric_columns}
        row_count = 0
        for row in reader:
            if not row:
                continue
            if len(row) != len(header):
                raise ValueError(f"CSV record ending at line {reader.line_num} does not match the header column count")
            row_count += 1
            for index, value in enumerate(row):
                if value == "":
                    empty_counts[index] += 1
                    continue
                if len(samples[index]) < sample_size:
                    samples[index].append(value[:200])
                name = header[index]
                if name in numbers:
                    numbers[name].observe(value)
    json.dump(
        {
            "path": str(path.relative_to(root)),
            "row_count": row_count,
            "columns": [
                {
                    "name": name,
                    "empty_count": empty_counts[index],
                    "samples": samples[index],
                    "numeric": numbers[name].summary() if name in numbers else None,
                }
                for index, name in enumerate(header)
            ],
        },
        sys.stdout,
        ensure_ascii=False,
        allow_nan=False,
    )


if __name__ == "__main__":
    main()
