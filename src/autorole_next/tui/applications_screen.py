from __future__ import annotations

from .applications_provider import SQLiteApplicationsProvider
from .detail_format import format_detail_payload
from .view_models import resolve_stage_label


def applications_content(provider: SQLiteApplicationsProvider):
    try:
        from textual.containers import Horizontal, Vertical, VerticalScroll
        from textual.widgets import Button, Checkbox, DataTable, Input, Static
    except ImportError as exc:
        raise RuntimeError("textual package is required for the TUI") from exc

    class ApplicationsWidget(Vertical):
        def __init__(self) -> None:
            super().__init__()
            self._provider = provider
            self._selected_correlation_id: str | None = None

        def compose(self):
            yield Input(
                placeholder="Search by correlation id, run status, stage, or attempt",
                id="applications-filter",
            )
            with Horizontal(id="applications-controls"):
                yield Checkbox("Auto-refresh", value=True, id="applications-auto-refresh")
                yield Button("Export Payload", id="applications-export")
                yield Button("Manually Submit", id="applications-manual-submit", variant="error")
            yield DataTable(id="applications-table")
            with VerticalScroll(id="applications-details-scroll"):
                yield Static(
                    "Select an application context row to inspect details",
                    id="applications-details",
                    markup=False,
                )

        async def on_mount(self) -> None:
            table = self.query_one("#applications-table", DataTable)
            export_button = self.query_one("#applications-export", Button)
            manual_submit_button = self.query_one("#applications-manual-submit", Button)
            table.cursor_type = "row"
            table.add_columns("Correlation ID", "Run", "Stage", "Attempt", "Updated")
            export_button.disabled = True
            manual_submit_button.disabled = True
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
            export_button = self.query_one("#applications-export", Button)
            manual_submit_button = self.query_one("#applications-manual-submit", Button)
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
                    self._selected_correlation_id = None
                    export_button.disabled = True
                    manual_submit_button.disabled = True
                    details.update("No application context rows found")
                elif self._selected_correlation_id is not None and not any(
                    row.correlation_id == self._selected_correlation_id for row in rows
                ):
                    self._selected_correlation_id = None
                    export_button.disabled = True
                    manual_submit_button.disabled = True
            except Exception as exc:
                table.clear(columns=False)
                self._selected_correlation_id = None
                export_button.disabled = True
                manual_submit_button.disabled = True
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

        async def on_button_pressed(self, event: Button.Pressed) -> None:
            correlation_id = self._selected_correlation_id
            if not correlation_id:
                return

            if event.button.id == "applications-export":
                export_path = await self._provider.export_payload(correlation_id)
                if export_path is None:
                    self._notify(f"Application context not found for export: {correlation_id}")
                    return

                self._notify(f"Exported payload to {export_path}")
                return

            if event.button.id != "applications-manual-submit":
                return

            success, message = await self._provider.manual_submit_to_concluding(correlation_id)
            self._notify(message)
            if not success:
                return

            await self._refresh()
            await self._show_detail_by_correlation_id(correlation_id)

        async def _show_detail(self, row_key: object | None) -> None:
            details = self.query_one("#applications-details", Static)
            export_button = self.query_one("#applications-export", Button)
            manual_submit_button = self.query_one("#applications-manual-submit", Button)
            if row_key is None:
                self._selected_correlation_id = None
                export_button.disabled = True
                manual_submit_button.disabled = True
                details.update("Select an application context row to inspect details")
                return

            correlation_id = str(row_key.value)
            await self._show_detail_by_correlation_id(correlation_id)

        async def _show_detail_by_correlation_id(self, correlation_id: str) -> None:
            details = self.query_one("#applications-details", Static)
            export_button = self.query_one("#applications-export", Button)
            manual_submit_button = self.query_one("#applications-manual-submit", Button)
            payload = await self._provider.get_details(correlation_id)
            if payload is None:
                self._selected_correlation_id = None
                export_button.disabled = True
                manual_submit_button.disabled = True
                details.update("Application context not found")
                return

            self._selected_correlation_id = correlation_id
            export_button.disabled = False
            manual_submit_button.disabled = not await self._provider.has_pending_form_submission_dlq(correlation_id)
            details.update(format_detail_payload(payload))

        def _notify(self, message: str) -> None:
            notify = getattr(self.app, "notify", None)
            if callable(notify):
                notify(message)

    return ApplicationsWidget()
