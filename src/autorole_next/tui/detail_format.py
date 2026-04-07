from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import orjson


MAX_DETAIL_CHARS = 20_000


def format_detail_payload(payload: dict[str, object]) -> str:
    rendered = orjson.dumps(payload, option=orjson.OPT_INDENT_2, default=_orjson_default).decode("utf-8")
    if len(rendered) <= MAX_DETAIL_CHARS:
        return rendered
    return (
        rendered[:MAX_DETAIL_CHARS]
        + "\n\n... detail truncated to "
        + f"{MAX_DETAIL_CHARS} chars (full payload is {len(rendered)} chars)."
    )


def _orjson_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, set):
        return sorted(_orjson_default(item) for item in value)
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:
            return repr(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return repr(value)
    if hasattr(value, "__dict__"):
        try:
            return dict(value.__dict__)
        except Exception:
            return repr(value)
    return repr(value)