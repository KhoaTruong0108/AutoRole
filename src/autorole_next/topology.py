from __future__ import annotations

from .executors.llm_applying import LlmApplyingExecutor
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
from .gates.llm_applying import LlmApplyingGate
from .gates.packaging import PackagingGate
from .gates.scoring import ScoringGate
from .gates.session import SessionGate
from .gates.tailoring import TailoringGate
from .stage_ids import CONCLUDING, FIELD_COMPLETER, FORM_SCRAPER, FORM_SUBMISSION, LLM_APPLYING, PACKAGING, SCORING, SESSION, TAILORING
from .store import AutoRoleStoreAdapter


def build_topology(store: AutoRoleStoreAdapter, stage_timeout_ms: dict[str, int] | None = None) -> Topology:
    _t = stage_timeout_ms or {}

    def _timeout(stage_id: str) -> dict[str, int]:
        ms = _t.get(stage_id)
        return {"timeout_ms": ms} if ms is not None else {}

    ScoringExecutor.configure_store(store)
    TailoringExecutor.configure_store(store)
    PackagingExecutor.configure_store(store)
    SessionExecutor.configure_store(store)
    FormSubmissionExecutor.configure_store(store)
    LlmApplyingExecutor.configure_store(store)
    ConcludingExecutor.configure_store(store)
    return Topology(
        stages=[
            StageNode(id=SCORING, executor=ScoringExecutor, gate=ScoringGate(), **_timeout(SCORING)),
            StageNode(id=TAILORING, executor=TailoringExecutor, gate=TailoringGate(), **_timeout(TAILORING)),
            StageNode(id=PACKAGING, executor=PackagingExecutor, gate=PackagingGate(), **_timeout(PACKAGING)),
            StageNode(id=SESSION, executor=SessionExecutor, gate=SessionGate(), **_timeout(SESSION)),
            StageNode(id=FORM_SCRAPER, executor=FormScraperExecutor, gate=FormScraperGate(), **_timeout(FORM_SCRAPER)),
            StageNode(id=FIELD_COMPLETER, executor=FieldCompleterExecutor, gate=FieldCompleterGate(), **_timeout(FIELD_COMPLETER)),
            StageNode(id=FORM_SUBMISSION, executor=FormSubmissionExecutor, gate=FormSubmissionGate(), **_timeout(FORM_SUBMISSION)),
            # StageNode(id=LLM_APPLYING, executor=LlmApplyingExecutor, gate=LlmApplyingGate(), **_timeout(LLM_APPLYING)),
            StageNode(id=CONCLUDING, executor=ConcludingExecutor, **_timeout(CONCLUDING)),
        ],
        queue_backend=SQLiteQueueAdapter(store.path),
        store_backend=store,
    )
