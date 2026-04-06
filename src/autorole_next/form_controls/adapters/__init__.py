from __future__ import annotations

from autorole_next.form_controls.adapters.ashby import AshbyAdapter
from autorole_next.form_controls.adapters.base import ATSAdapter
from autorole_next.form_controls.adapters.generic import GenericAdapter
from autorole_next.form_controls.adapters.greenhouse import GreenhouseAdapter
from autorole_next.form_controls.adapters.lever import LeverAdapter
from autorole_next.form_controls.adapters.workday import WorkdayAdapter

ADAPTER_REGISTRY: dict[str, type[ATSAdapter]] = {
	"greenhouse": GreenhouseAdapter,
	"workday": WorkdayAdapter,
	"lever": LeverAdapter,
	"ashby": AshbyAdapter,
	"generic": GenericAdapter,
}


def get_adapter(platform_id: str) -> ATSAdapter:
	adapter_cls = ADAPTER_REGISTRY.get(platform_id, GenericAdapter)
	return adapter_cls()


__all__ = [
	"ADAPTER_REGISTRY",
	"get_adapter",
	"ATSAdapter",
	"AshbyAdapter",
	"GenericAdapter",
	"GreenhouseAdapter",
	"LeverAdapter",
	"WorkdayAdapter",
]

