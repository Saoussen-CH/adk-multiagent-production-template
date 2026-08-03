"""Every session an eval or smoke test creates must carry a tenant_id.

Final-review finding C3. There is no default/implicit tenant: every tool
resolves its provider via `get_tenant_id(tool_context)`, which hard-raises
`MissingTenantError` when `state["tenant_id"]` is absent. Three places
created sessions without one and nothing noticed:

  - `tests/smoke/test_smoke.py` POSTed to /api/chat with no `tenant_id`,
    now a required `ChatRequest` field → HTTP 422 against every `assert
    r.status_code == 200`. These smoke tests gate the eval step in
    `cloudbuild/release.yaml` and `release-staging.yaml`, so this failed
    the prod and staging release pipelines at the smoke gate.
  - `tests/eval_vertex.py` created ADK sessions with `state={}`.
  - the recorded `.test.json` / `.evalset.json` datasets had
    `session_input.state` of null/{}, degrading `make test-unit` and
    `make test-integration`; `tests/generate_eval_dataset.py` and
    `tests/generate_integration_evalset.py` would have regenerated them
    that way forever.

These are data/structure assertions, so they run in the free tool-test tier
rather than only being caught by an eval run that costs LLM calls.
"""

import ast
import glob
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

EVAL_DATASETS = sorted(
    glob.glob(str(REPO_ROOT / "tests/unit/*.test.json")) + glob.glob(str(REPO_ROOT / "tests/integration/*.evalset.json"))
)

# Files whose ADK session creation must always pass a tenant_id in state.
SESSION_CREATING_SOURCES = [
    "tests/eval_vertex.py",
    "tests/generate_eval_dataset.py",
    "tests/generate_integration_evalset.py",
]

SESSION_CREATING_CALLS = {"create_session", "async_create_session", "SessionInput"}


def test_eval_dataset_glob_is_not_empty():
    """Guard against this whole module silently passing on zero files."""
    assert len(EVAL_DATASETS) >= 15, EVAL_DATASETS


def test_every_recorded_eval_case_has_a_tenant_id_in_session_state():
    missing = []
    for path in EVAL_DATASETS:
        with open(path) as fh:
            data = json.load(fh)
        for case in data.get("eval_cases", []):
            state = (case.get("session_input") or {}).get("state") or {}
            if not state.get("tenant_id"):
                missing.append(f"{Path(path).name}::{case.get('eval_id')}")
    assert missing == [], (
        "these recorded eval sessions would hit MissingTenantError on replay: " + ", ".join(missing)
    )


def test_recorded_eval_cases_all_use_the_same_tenant():
    """A mixed-tenant dataset would be a silent isolation bug, not a feature —
    these datasets simulate one merchant's conversations."""
    tenants = set()
    for path in EVAL_DATASETS:
        with open(path) as fh:
            data = json.load(fh)
        for case in data.get("eval_cases", []):
            tenants.add((case.get("session_input") or {}).get("state", {}).get("tenant_id"))
    assert tenants == {"acme-electronics"}, tenants


def test_session_creating_scripts_always_pass_tenant_state():
    """AST-level: no create_session/SessionInput call may omit `state`."""
    offenders = []
    for relative_path in SESSION_CREATING_SOURCES:
        source_path = REPO_ROOT / relative_path
        tree = ast.parse(source_path.read_text(), filename=str(source_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in SESSION_CREATING_CALLS:
                continue
            keywords = {kw.arg for kw in node.keywords}
            if "state" not in keywords:
                offenders.append(f"{relative_path}:{node.lineno} {name}()")
    assert offenders == [], "session created without tenant state: " + ", ".join(offenders)


def test_smoke_tests_send_tenant_id_on_every_chat_request():
    """tenant_id is a required ChatRequest field — a body without it is a 422,
    and these smoke tests gate the release pipelines."""
    source = (REPO_ROOT / "tests/smoke/test_smoke.py").read_text()
    tree = ast.parse(source)

    chat_bodies = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "post"):
            continue
        target = ast.get_source_segment(source, node.args[0]) if node.args else ""
        if "/api/chat" not in (target or ""):
            continue
        chat_bodies += 1
        body = next((kw.value for kw in node.keywords if kw.arg == "json"), None)
        assert isinstance(body, ast.Dict), f"unexpected /api/chat body at line {node.lineno}"
        keys = {k.value for k in body.keys if isinstance(k, ast.Constant)}
        assert "tenant_id" in keys, f"/api/chat POST at line {node.lineno} has no tenant_id — this is a 422"

    assert chat_bodies >= 4, f"expected the smoke suite to exercise /api/chat, found {chat_bodies} calls"
