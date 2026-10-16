import json
import sys
from pathlib import Path


def main() -> None:
    """Read JSON stdin and return deterministic workspace text statistics."""
    arguments = json.load(sys.stdin)
    root = Path.cwd().resolve()
    path = (root / arguments["path"]).resolve()
    if not path.is_relative_to(root):
        raise PermissionError("Path escapes the configured workspace")
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    json.dump(
        {
            "path": str(path.relative_to(root)),
            "lines": len(lines),
            "non_empty_lines": sum(bool(line.strip()) for line in lines),
            "words": len(content.split()),
            "characters": len(content),
        },
        sys.stdout,
        ensure_ascii=False,
    )


if __name__ == "__main__":
    main()
