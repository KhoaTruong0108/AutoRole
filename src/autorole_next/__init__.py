__all__ = ["build_runner", "build_store", "build_topology"]


def __getattr__(name: str):
	if name in __all__:
		from .app import build_runner, build_store, build_topology

		exports = {
			"build_runner": build_runner,
			"build_store": build_store,
			"build_topology": build_topology,
		}
		return exports[name]
	raise AttributeError(name)