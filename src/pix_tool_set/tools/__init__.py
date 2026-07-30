"""Package init: loads every builtin tool module on import."""

from __future__ import annotations

_LOADED = False


def load_builtin_tools() -> None:
    """Import every tool module so the registry is fully populated."""
    global _LOADED
    if _LOADED:
        return
    from . import session_tools  # noqa: F401
    from . import event_tools  # noqa: F401
    from . import frame_tools  # noqa: F401
    from . import texture_tools  # noqa: F401
    from . import shader_tools  # noqa: F401
    from . import pass_binding_tools  # noqa: F401
    from . import timing_tools  # noqa: F401
    from . import replay_value_tools  # noqa: F401
    from . import resource_texture_tools  # noqa: F401
    from . import source_tools  # noqa: F401
    from . import value_tools  # noqa: F401
    from . import geometry_tools  # noqa: F401
    from . import pipeline_tools  # noqa: F401
    from . import resource_tools  # noqa: F401
    from . import export_tools  # noqa: F401
    from . import advanced_tools  # noqa: F401
    from . import performance_tools  # noqa: F401
    from . import diagnostic_tools  # noqa: F401

    _LOADED = True
