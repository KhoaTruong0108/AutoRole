from __future__ import annotations

from .applications_provider import SQLiteApplicationsProvider
from .detail_format import format_detail_payload
from .view_models import resolve_stage_label


def applications_content(provider: SQLiteApplicationsProvider):
    try:
        from textual.containers import Vertical, VerticalScroll
        from textual.widgets import Checkbox, DataTable, Input, Static
    except ImportError as exc:
        raise RuntimeError("textual package is required for the TUI") from exc

    class ApplicationsWidget(Vertical):
        def __init__(self) -> None:
            super().__init__()
            self._provider = provider

        def compose(self):
            yield Input(placeholder="Search by correlation id, run status, stage, or attempt", id="applications-filter")
            yield Checkbox("Auto-refresh", value=True, id="applications-auto-refresh")
            yield DataTable(id="applications-table")
            with VerticalScroll(id="applications-details-scroll"):
                yield Static(
                    "Select an application context row to inspect details",
                    id="applications-details",
                    markup=False,
                )

        async def on_mount(self) -> None:
            table = self.query_one("#applications-table", DataTable)
            table.cursor_type = "row"
            table.add_columns("Correlation ID", "Run", "Stage", "Attempt", "Updated")
            self.set_interval(5.0, self._schedule_auto_refresh)
            self._schedule_refresh()

        def _schedule_auto_refresh(self) -> None:
            checkbox = self.query_one("#applications-auto-refresh", Checkbox)
            if checkbox.value:
                self._schedule_refresh()

        def _schedule_refresh(self) -> None:
            self.run_worker(self._refresh(), exclusive=True)

        async def _refresh(self) -> None:
            table = self.query_one("#applications-table", DataTable)
            details = self.query_one("#applications-details", Static)
            filter_value = self.query_one("#applications-filter", Input).value

            try:
                rows = await self._provider.list_rows(search=filter_value)
                table.clear(columns=False)
                for row in rows:
                    table.add_row(
                        row.correlation_id,
                        row.run_status or "-",
                        resolve_stage_label(row.current_stage) if row.current_stage else "-",
                        str(row.attempt),
                        row.updated_at,
                        key=row.correlation_id,
                    )
                if not rows:
                    details.update("No application context rows found")
            except Exception as exc:
                table.clear(columns=False)
                details.update(f"Applications error: {exc}")

        async def on_input_changed(self, event: Input.Changed) -> None:
            if event.input.id == "applications-filter":
                self._schedule_refresh()

        async def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
            if event.checkbox.id == "applications-auto-refresh" and event.value:
                self._schedule_refresh()

        async def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
            await self._show_detail(event.row_key)

        async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            await self._show_detail(event.row_key)

        async def _show_detail(self, row_key: object | None) -> None:
            details = self.query_one("#applications-details", Static)
            if row_key is None:
                details.update("Select an application context row to inspect details")
                return

            correlation_id = str(row_key.value)
            payload = await self._provider.get_details(correlation_id)
            if payload is None:
                details.update("Application context not found")
                return

            details.update(format_detail_payload(payload))

    return ApplicationsWidget()
