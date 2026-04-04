from __future__ import annotations

from .. import _snapflow  # noqa: F401  Ensures the workspace SnapFlow source is first on sys.path.
from .applications_provider import build_applications_provider_from_env
from .applications_screen import applications_content
from .listings_provider import build_listings_provider_from_env
from .listings_screen import listings_content


def create_tui_app():
    try:
        from textual.app import App
        from textual.containers import Container
        from textual.widgets import Footer, Header, TabbedContent, TabPane

        from snapflow.tui.screens.dashboard import dashboard_content
        from snapflow.tui.screens.dlq_browser import dlq_browser_content
        from snapflow.tui.screens.queue_depths import queue_depths_content
        from snapflow.tui.screens.run_inspector import run_inspector_content
        from snapflow.tui.screens.stage_monitor import stage_monitor_content
        from snapflow.tui.store_provider import build_stage_monitor_provider_from_env
    except ImportError as exc:
        raise RuntimeError("textual package is required for the TUI") from exc

    class AutoRoleNextTUI(App):
        TITLE = "AutoRole Next TUI"
        SUB_TITLE = "Pipeline runtime and listings"
        DEFAULT_CSS = """
        TabbedContent {
            height: 1fr;
        }

        TabPane {
            height: 1fr;
        }

        #runs-table,
        #dlq-table,
        #in-process-table,
        #listings-table,
        #applications-table {
            height: 30%;
        }

        #run-details,
        #dlq-details,
        #stage-details,
        #listings-details-scroll,
        #applications-details-scroll {
            height: 1fr;
            overflow-x: auto;
            overflow-y: auto;
        }
        """

        def compose(self):
            yield Header()
            with Container():
                with TabbedContent():
                    with TabPane("Stage Monitor"):
                        yield stage_monitor_content(build_stage_monitor_provider_from_env())
                    with TabPane("Listings"):
                        yield listings_content(build_listings_provider_from_env())
                    with TabPane("Applications"):
                        yield applications_content(build_applications_provider_from_env())
                    with TabPane("Dashboard"):
                        yield dashboard_content()
                    with TabPane("Queue Depths"):
                        yield queue_depths_content()
                    with TabPane("Run Inspector"):
                        yield run_inspector_content()
                    with TabPane("DLQ Browser"):
                        yield dlq_browser_content()
            yield Footer()

    return AutoRoleNextTUI()