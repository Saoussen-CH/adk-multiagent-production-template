"""Regression tests for the backend's dependency on `customer_support_mas`.

Final-review finding C1: `backend/app/main.py` imports
`customer_support_mas.rate_limiting` and `backend/app/refund_approvals.py`
imports `customer_support_mas.providers.registry`, but nothing declared the
package as a backend dependency and `backend/Dockerfile` copied only
`backend/app` into the image — so uvicorn died with `ModuleNotFoundError`
on startup in every deployed environment.

Two guarantees are asserted here, because fixing only one still leaves the
container broken:

1.  **Packaging** — the dependency is declared and the Dockerfile ships the
    source, so the module exists at runtime.
2.  **Import weight** — importing the modules the backend actually uses must
    NOT drag in `customer_support_mas.main` / `customer_support_mas.config`
    / the agent graph. `config.py` raises at import time when
    `GOOGLE_CLOUD_PROJECT` is unset (the backend has its own project config
    path), and the backend talks to a *deployed* Agent Engine, never an
    in-process agent, so building every `LlmAgent` on cold start would be
    both wasteful and fatal.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# The exact import chain backend/app/{main,refund_approvals}.py rely on.
BACKEND_IMPORTS = [
    "customer_support_mas.rate_limiting",
    "customer_support_mas.providers.registry",
]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text()


# =============================================================================
# 1. Packaging
# =============================================================================


def test_backend_declares_customer_support_mas_dependency():
    """Without this the Cloud Run image has no customer_support_mas at all."""
    pyproject = _read("backend/pyproject.toml")
    assert "customer-support-mas" in pyproject, (
        "backend/pyproject.toml must declare customer-support-mas — "
        "backend/app/main.py imports customer_support_mas.rate_limiting"
    )
    assert "customer-support-mas = { workspace = true }" in pyproject, (
        "the dependency must resolve to the workspace root package, not PyPI"
    )


def test_backend_dockerfile_copies_the_package_source():
    """`uv sync --no-editable` builds customer_support_mas into a wheel, which
    needs its source present in the deps stage."""
    dockerfile = _read("backend/Dockerfile")
    assert "COPY customer_support_mas" in dockerfile, (
        "backend/Dockerfile must copy the customer_support_mas source into the "
        "dependency-install stage, or the wheel build has nothing to package"
    )


def test_ci_installs_workspace_member_dependencies():
    """`uv sync --group dev` alone does not install the backend workspace
    member's deps (fastapi, email-validator, ...), so CI steps that run
    tests importing backend.app.main need --all-packages (finding I8)."""
    for pipeline in ("cloudbuild/release.yaml", "cloudbuild/release-staging.yaml", "cloudbuild/cloudbuild-deploy.yaml"):
        contents = _read(pipeline)
        if "test_admin_refund_endpoints.py" not in contents:
            continue
        assert "uv sync --frozen --all-packages --group dev" in contents, (
            f"{pipeline} runs tests importing backend.app.main but syncs without --all-packages"
        )


# =============================================================================
# 2. Import weight
# =============================================================================


def test_backend_import_chain_does_not_build_the_agent_graph():
    """Run in a clean subprocess with GOOGLE_CLOUD_PROJECT unset: the backend's
    imports must succeed and must not pull in config.py / main.py / any agent."""
    script = (
        "import sys\n"
        + "".join(f"import {module}\n" for module in BACKEND_IMPORTS)
        + "leaked = sorted(\n"
        "    m for m in sys.modules\n"
        "    if m in ('customer_support_mas.main', 'customer_support_mas.config')\n"
        "    or m.startswith('customer_support_mas.agents')\n"
        ")\n"
        "print(';'.join(leaked))\n"
    )

    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO_ROOT),
        # Deliberately NOT setting GOOGLE_CLOUD_PROJECT — customer_support_mas/
        # config.py raises at import time without it, which is exactly the
        # failure mode this test pins down.
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )

    assert result.returncode == 0, (
        "the backend's customer_support_mas imports must work without "
        f"GOOGLE_CLOUD_PROJECT set:\n{result.stderr[-3000:]}"
    )
    leaked = [name for name in result.stdout.strip().split(";") if name]
    assert leaked == [], (
        "importing the backend's modules pulled in the agent graph / config: "
        f"{leaked}. Keep customer_support_mas/__init__.py's root_agent export lazy."
    )


def test_root_agent_is_still_importable_from_the_package_root():
    """The lazy export must not break `from customer_support_mas import root_agent`
    (deployment/deploy.py and ADK agent discovery rely on the package's public
    import surface)."""
    import customer_support_mas

    assert "root_agent" in dir(customer_support_mas)
    assert customer_support_mas.root_agent is not None

    try:
        customer_support_mas.does_not_exist
    except AttributeError:
        pass
    else:  # pragma: no cover - only reached if __getattr__ regresses
        raise AssertionError("module __getattr__ must still raise AttributeError for unknown names")
