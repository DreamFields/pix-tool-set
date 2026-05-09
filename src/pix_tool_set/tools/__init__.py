from __future__ import annotations

_loaded = False


def load_builtin_tools() -> None:
    global _loaded
    if _loaded:
        return
    from . import placeholder  # noqa: F401
    from . import cpp_export_tools  # noqa: F401
    from . import shader_event_tools  # noqa: F401
    from . import event_analysis_tools  # noqa: F401
    from . import shader_source_tools  # noqa: F401
    from . import resource_history_tools  # noqa: F401
    from . import wpix_export_tools  # noqa: F401

    _loaded = True
