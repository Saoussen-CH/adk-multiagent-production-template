"""Customer Support Multi-Agent System.

`root_agent` is exported lazily (PEP 562 module ``__getattr__``) rather than
imported at package-import time. Both of these still work unchanged::

    from customer_support_mas import root_agent
    import customer_support_mas; customer_support_mas.root_agent

but importing any *leaf* module no longer drags in the whole agent graph.
That matters because the FastAPI backend (``backend/app/main.py`` and
``backend/app/refund_approvals.py``) imports
``customer_support_mas.rate_limiting`` and
``customer_support_mas.providers.registry``: it talks to a *deployed* Agent
Engine, never to an in-process agent, so constructing every ``LlmAgent`` on
its cold start would be pure waste — and would hard-fail, since
``customer_support_mas/config.py`` raises at import time when
``GOOGLE_CLOUD_PROJECT`` is unset and the backend has its own project
config path (``backend/app/config.py``).
"""

__all__ = ["root_agent"]


def __getattr__(name: str):
    if name == "root_agent":
        from customer_support_mas.main import root_agent

        return root_agent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *__all__])
