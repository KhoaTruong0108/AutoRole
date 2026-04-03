from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .. import _snapflow  # noqa: F401  Ensures the workspace SnapFlow source is first on sys.path.
from .app import create_tui_app


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the autorole_next TUI.")
    parser.add_argument(
        "--db",
        default=os.getenv("SNAPFLOW_TUI_DB", "pipeline.db"),
        help="SQLite database path for the SnapFlow TUI.",
    )
    return parser


def launch_tui(db_path: str | Path) -> None:
    if not sys.stdin.isatty():
        raise RuntimeError(
            "stdin is not a TTY. Launch the TUI with 'python -m autorole_next.tui.run --db <path>' "
            "instead of feeding Python through a here-doc or pipe."
        )

    resolved_db_path = Path(db_path).expanduser().resolve()
    os.environ["SNAPFLOW_TUI_DB"] = str(resolved_db_path)
    create_tui_app().run()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        launch_tui(args.db)
    except RuntimeError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())