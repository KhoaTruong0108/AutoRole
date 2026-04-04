from __future__ import annotations

from ._snapflow import SQLiteQueueAdapter, StageNode, Topology
from .executors.concluding import ConcludingExecutor
from .executors.field_completer import FieldCompleterExecutor
from .executors.form_scraper import FormScraperExecutor
from .executors.form_submission import FormSubmissionExecutor
from .executors.packaging import PackagingExecutor
from .executors.scoring import ScoringExecutor
from .executors.session import SessionExecutor
from .executors.tailoring import TailoringExecutor
from .gates.field_completer import FieldCompleterGate
from .gates.form_scraper import FormScraperGate
from .gates.form_submission import FormSubmissionGate
from .gates.packaging import PackagingGate
from .gates.scoring import ScoringGate
from .gates.session import SessionGate
from .gates.tailoring import TailoringGate
from .stage_ids import CONCLUDING, FIELD_COMPLETER, FORM_SCRAPER, FORM_SUBMISSION, PACKAGING, SCORING, SESSION, TAILORING
from .store import AutoRoleStoreAdapter


def build_topology(store: AutoRoleStoreAdapter) -> Topology:
    ScoringExecutor.configure_store(store)
    TailoringExecutor.configure_store(store)
    PackagingExecutor.configure_store(store)
    SessionExecutor.configure_store(store)
    FormSubmissionExecutor.configure_store(store)
    ConcludingExecutor.configure_store(store)
    return Topology(
        stages=[
            StageNode(
                id=SCORING,
                executor=ScoringExecutor,
                gate=ScoringGate(),
            ),
            StageNode(
                id=TAILORING,
                executor=TailoringExecutor,
                gate=TailoringGate(),
            ),
            StageNode(
                id=PACKAGING,
                executor=PackagingExecutor,
                gate=PackagingGate(),
            ),
            StageNode(
                id=SESSION,
                executor=SessionExecutor,
                gate=SessionGate(),
            ),
            StageNode(
                id=FORM_SCRAPER,
                executor=FormScraperExecutor,
                gate=FormScraperGate(),
            ),
            StageNode(
                id=FIELD_COMPLETER,
                executor=FieldCompleterExecutor,
                gate=FieldCompleterGate(),
            ),
            StageNode(
                id=FORM_SUBMISSION,
                executor=FormSubmissionExecutor,
                gate=FormSubmissionGate(),
            ),
            StageNode(
                id=CONCLUDING,
                executor=ConcludingExecutor,
            ),
        ],
        queue_backend=SQLiteQueueAdapter(store.path),
        store_backend=store,
    )
