from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from .._snapflow import PipelineSeeder, RunStatus, StateContext
from ..payloads import (
    ExplorationInput,
    ListingPayload,
    ListingSeed,
    SeedRunPayload,
    SeededRun,
    canonical_listing_key,
    correlation_id_for_listing,
)
from ..stage_ids import SCORING
from ..store import AutoRoleStoreAdapter

SearchDiscovery = Callable[[dict[str, Any]], Awaitable[Iterable[ListingSeed | ListingPayload | dict[str, Any]]]]
JobUrlResolver = Callable[[str, str | None], Awaitable[ListingSeed | ListingPayload | dict[str, Any]]]


class ExploringSeeder(PipelineSeeder[dict[str, Any]]):
    def __init__(
        self,
        runner,
        store: AutoRoleStoreAdapter,
        *,
        search_discovery: SearchDiscovery | None = None,
        job_url_resolver: JobUrlResolver | None = None,
    ) -> None:
        super().__init__(runner)
        self._store = store
        self._search_discovery = search_discovery
        self._job_url_resolver = job_url_resolver

    def build_data(self, **kwargs: Any) -> dict[str, Any]:
        return dict(kwargs)

    async def seed(self, request: ExplorationInput) -> list[SeededRun]:
        discovered = await self._discover(request)
        seen_canonical_keys: set[str] = set()
        seeded_runs: list[SeededRun] = []

        for seed in discovered:
            canonical_key = canonical_listing_key(seed.listing)
            if canonical_key in seen_canonical_keys:
                continue
            seen_canonical_keys.add(canonical_key)
            seeded_runs.append(await self._seed_one(seed, request.metadata))

        return seeded_runs

    async def _discover(self, request: ExplorationInput) -> list[ListingSeed]:
        if request.job_url.strip():
            return [await self._resolve_job_url(request.job_url, request.platform_hint)]

        if request.job_urls_file.strip():
            return [
                await self._resolve_job_url(job_url, request.platform_hint)
                for job_url in _load_job_urls_file(request.job_urls_file)
            ]

        if self._search_discovery is None:
            raise ValueError("search discovery is not configured")

        results = await self._search_discovery(dict(request.search_filters))
        return [_coerce_listing_seed(item, default_source_name="search") for item in results]

    async def _resolve_job_url(self, job_url: str, platform_hint: str | None) -> ListingSeed:
        if self._job_url_resolver is None:
            raise ValueError("job URL resolution is not configured")
        result = await self._job_url_resolver(job_url, platform_hint)
        return _coerce_listing_seed(result, default_source_name="manual-url")

    async def _seed_one(self, seed: ListingSeed, metadata: dict[str, Any]) -> SeededRun:
        correlation_id = correlation_id_for_listing(seed.listing)
        canonical_key = canonical_listing_key(seed.listing)
        created, listing_row = await self._store.claim_listing_seed(correlation_id, seed)
        if not created:
            return SeededRun(
                correlation_id=str(listing_row["correlation_id"]),
                canonical_key=str(listing_row["canonical_key"]),
                status="duplicate",
                source_name=str(listing_row.get("source_name") or seed.source_name),
            )

        payload = SeedRunPayload(
            listing=seed.listing,
            source_name=seed.source_name,
            source_metadata=seed.source_metadata,
            discovered_at=seed.discovered_at,
            canonical_key=canonical_key,
        )
        run_metadata = {
            **metadata,
            "source_name": seed.source_name,
            "source_metadata": seed.source_metadata,
            "canonical_key": canonical_key,
            "discovered_at": seed.discovered_at.isoformat(),
        }
        await self._enqueue_seeded_run(correlation_id, payload, run_metadata)
        return SeededRun(
            correlation_id=correlation_id,
            canonical_key=canonical_key,
            status="seeded",
            source_name=seed.source_name,
        )

    async def _enqueue_seeded_run(
        self,
        correlation_id: str,
        payload: SeedRunPayload,
        metadata: dict[str, Any],
    ) -> None:
        topology = getattr(self.runner, "_topology", None)
        if topology is None:
            raise RuntimeError("PipelineRunner topology is unavailable")

        context = StateContext[dict[str, Any]](
            correlation_id=correlation_id,
            current_stage=SCORING,
            data=payload.model_dump(mode="json"),
            metadata=metadata,
        )
        await topology.store_backend.save_context(context)
        await topology.store_backend.create_run(correlation_id, metadata)
        await topology.store_backend.set_run_status(correlation_id, RunStatus.RUNNING)
        await topology.queue_backend.put(SCORING, correlation_id)


def _coerce_listing_seed(item: ListingSeed | ListingPayload | dict[str, Any], default_source_name: str) -> ListingSeed:
    if isinstance(item, ListingSeed):
        return item
    if isinstance(item, ListingPayload):
        return ListingSeed(listing=item, source_name=default_source_name)
    if "listing" in item:
        payload = dict(item)
        payload.setdefault("source_name", default_source_name)
        return ListingSeed.model_validate(payload)
    return ListingSeed(listing=ListingPayload.model_validate(item), source_name=default_source_name)


def _load_job_urls_file(job_urls_file: str) -> list[str]:
    path = Path(job_urls_file).expanduser()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [str(item).strip() for item in payload if str(item).strip()]
    if isinstance(payload, dict) and isinstance(payload.get("job_urls"), list):
        return [str(item).strip() for item in payload["job_urls"] if str(item).strip()]
    raise ValueError("job URLs file must contain a JSON list or an object with a job_urls list")