from __future__ import annotations

import orjson

from .listings_provider import SQLiteListingsProvider
from .view_models import resolve_stage_label


MAX_DETAIL_CHARS = 20_000


def _format_detail_payload(payload: dict[str, object]) -> str:
    rendered = orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode("utf-8")
    if len(rendered) <= MAX_DETAIL_CHARS:
        return rendered
    return (
        rendered[:MAX_DETAIL_CHARS]
        + "\n\n... detail truncated to "
        + f"{MAX_DETAIL_CHARS} chars (full payload is {len(rendered)} chars)."
    )


def listings_content(provider: SQLiteListingsProvider):
    try:
        from textual.containers import Vertical, VerticalScroll
        from textual.widgets import Checkbox, DataTable, Input, Static
    except ImportError as exc:
        raise RuntimeError("textual package is required for the TUI") from exc

    class ListingsWidget(Vertical):
        def __init__(self) -> None:
            super().__init__()
            self._provider = provider

        def compose(self):
            yield Input(placeholder="Search listings by company, title, platform, stage, or run", id="listings-filter")
            yield Checkbox("Auto-refresh", value=True, id="listings-auto-refresh")
            yield DataTable(id="listings-table")
            with VerticalScroll(id="listings-details-scroll"):
                yield Static("Select a listing to inspect details", id="listings-details", markup=False)

        async def on_mount(self) -> None:
            table = self.query_one("#listings-table", DataTable)
            table.cursor_type = "row"
            table.add_columns("Correlation ID", "Company", "Title", "Platform", "Run", "Stage", "Updated")
            self.set_interval(5.0, self._schedule_auto_refresh)
            self._schedule_refresh()

        def _schedule_auto_refresh(self) -> None:
            checkbox = self.query_one("#listings-auto-refresh", Checkbox)
            if checkbox.value:
                self._schedule_refresh()

        def _schedule_refresh(self) -> None:
            self.run_worker(self._refresh(), exclusive=True)

        async def _refresh(self) -> None:
            table = self.query_one("#listings-table", DataTable)
            details = self.query_one("#listings-details", Static)
            filter_value = self.query_one("#listings-filter", Input).value

            try:
                rows = await self._provider.list_rows(search=filter_value)
                table.clear(columns=False)
                for row in rows:
                    table.add_row(
                        row.correlation_id,
                        row.company_name,
                        row.job_title,
                        row.platform,
                        row.run_status or row.listing_status,
                        resolve_stage_label(row.current_stage) if row.current_stage else "-",
                        row.updated_at,
                        key=row.correlation_id,
                    )
                if not rows:
                    details.update("No listings found")
            except Exception as exc:
                table.clear(columns=False)
                details.update(f"Listings error: {exc}")

        async def on_input_changed(self, event: Input.Changed) -> None:
            if event.input.id == "listings-filter":
                self._schedule_refresh()

        async def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
            if event.checkbox.id == "listings-auto-refresh" and event.value:
                self._schedule_refresh()

        async def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
            await self._show_details(event.row_key)

        async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            await self._show_details(event.row_key)

        async def _show_details(self, row_key: object | None) -> None:
            details = self.query_one("#listings-details", Static)
            if row_key is None:
                details.update("Select a listing to inspect details")
                return

            correlation_id = str(row_key.value)
            payload = await self._provider.get_details(correlation_id)
            if payload is None:
                details.update("Listing not found")
                return

            details.update(_format_detail_payload(payload))

    return ListingsWidget()