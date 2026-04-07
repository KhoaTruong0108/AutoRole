from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autorole_next.tui.detail_format import format_detail_payload


@dataclass
class _WeirdValue:
    label: str


def test_format_detail_payload_handles_non_json_values() -> None:
    rendered = format_detail_payload(
        {
            "context": {
                "path": Path("logs/form_scraper/output.json"),
                "raw_bytes": b"hello",
                "weird": _WeirdValue(label="field"),
            }
        }
    )

    assert '"path": "logs/form_scraper/output.json"' in rendered
    assert '"raw_bytes": "hello"' in rendered
    assert '"label": "field"' in rendered


def test_format_detail_payload_truncates_large_payloads() -> None:
    rendered = format_detail_payload({"value": "x" * 25_000})

    assert "detail truncated to 20000 chars" in rendered