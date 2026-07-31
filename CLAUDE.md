# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A production-template customer support multi-agent system on GCP: Google ADK agents deployed to Vertex AI Agent Engine, a FastAPI backend + React frontend on Cloud Run, Firestore (with vector search) as the data layer, and Terraform + Cloud Build for infra and CI/CD. The Python package is `customer_support_mas/` (the Makefile's `lint`/`format` targets reference a stale `customer_support_agent/` path — lint manually with `ruff check customer_support_mas/ --ignore=E501`, which is what CI actually runs).

## Commands

Dependency management is `uv` (`uv sync --frozen --group dev`; `make install` does this plus pre-commit). Python entry points run as `uv run python`, usually with `PYTHONPATH=.`.

```bash
make test-tools                      # pure-Python tool tests, mocked Firestore, no LLM (free/fast)
make test-unit EVAL_PROFILE=fast     # single-agent AgentEvaluator eval (fast|standard|full)
make test-integration TEST=name      # multi-agent handoff eval; TEST= filters via pytest -k
make test                            # all three
uv run pytest tests/unit/test_tools.py -k test_name -v   # single test directly

make eval-post-deploy ENV=staging    # score the deployed Agent Engine (tests/eval_vertex.py)
make eval-flywheel-local             # synthetic multi-turn eval + loss clusters, in-process agent
make eval-flywheel-cloud ENV=dev DEST=gs://...   # same against deployed engine
make nightly                         # trigger canary quality check (real-traffic eval vs champion)

make deploy-agent-engine ENV=dev     # deploy/update Agent Engine (deployment/deploy.py)
make seed-db ENV=dev                 # Firestore fixtures; then add-embeddings, vector-index
make infra-up ENV=dev                # terraform init + apply (first time: bootstrap-tfstate ENV=dev)
make sync-tfvars ENV=dev             # push local tfvars to GCS so Cloud Build sees them
```

Environment config lives in `.env` / `.env.dev` / `.env.staging` / `.env.prod` (gitignored); `ENV=<env>` on make targets selects the file. `customer_support_mas/config.py` raises at import if `GOOGLE_CLOUD_PROJECT` is unset, so any script importing the package needs the env loaded. `GOOGLE_GENAI_USE_VERTEXAI=True` is required for all Vertex AI paths.

## Architecture

**Agent layer** (`customer_support_mas/`): a root coordinator agent (Gemini 2.5 Pro) exposes specialists as tools via ADK's `AgentTool` — product, order, billing agents (Gemini 2.5 Flash, low temperature) — plus a refund `SequentialAgent` (Pro) of three narrow sub-agents (validate → eligibility → process). `process_refund` is deliberately unreachable outside that pipeline; refund safety is structural, not prompt-based. Per-agent model/temperature comes from `config.py`'s `AGENT_CONFIGS`. Order/billing tools are wrapped in ownership-check decorators (`auth.py`) that verify `customer_id` against the authenticated user before the tool body runs. Product search is RAG (text-embedding-004 via `services/rag_search.py`) with automatic keyword-search fallback.

**Backend** (`backend/app/`): FastAPI proxy on Cloud Run. `agent_client.py` calls the deployed Agent Engine via the **root resource name** with retry/backoff (429/5xx) plus a circuit breaker (5 failures → fail fast 60s; rate-limit errors deliberately do NOT trip it). Model Armor screening happens here in the backend — the ADK-level `ModelArmorSafetyFilterPlugin` exists but is intentionally disabled in `deployment/deploy.py`.

**Deployment** (`deployment/deploy.py`): update-if-exists keyed on Agent Engine display name; uses the new-style `vertexai.Client(...).agent_engines` SDK. Three things in there were learned by live debugging — don't remove them casually:
- `staging_bucket` must be in the create/update config dict (SDK errors without it).
- `google-adk>=2.4.0` is pinned in `REQUIREMENTS` because the remote build otherwise resolves an older ADK than the one that pickled the agent (`'LlmAgent' object has no attribute 'mode'`).
- Agent Identity (`identity_type=AGENT_IDENTITY`) requires both the IAM grant in `terraform/modules/core/iam.tf` (principalSet trust domain is `agents.global.proj-<PROJECT_NUMBER>...` — `proj-`, not `project-` as Google's docs claim) and the env var `GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES: "False"` (string, not bool) to opt out of Context-Aware Access mTLS token binding, without which every Firestore/Logging call 401s regardless of IAM.

**Evaluation** (5 stages, see `docs/EVALUATION.md`): tool tests → unit eval → integration eval (all against mocks, profile-gated by `EVAL_PROFILE`: fast = Rouge-1 only, standard adds tool-trajectory/rubric judge, full adds everything) → post-deploy eval (`tests/eval_vertex.py`) → real-traffic canary check (`tests/eval_agent_platform.py canary-check`). Two hard-won constraints:
- The eval SDK's built-in inference silently breaks on `AgentTool` multi-agent handoffs; `tests/eval_vertex.py --custom-inference` drives the engine via `async_stream_query()` instead (the sync variant only yields the first event). CI always uses `--custom-inference`.
- `create_evaluation_run()` (console-visible eval runs) architecturally cannot score pre-recorded real sessions — it exists to drive new simulated inference. Scoring real traffic uses `client.evals.evaluate()` (no console artifact). This is documented in `tests/eval_agent_platform.py`; don't retry the dead end.

**CI/CD** (`cloudbuild/`, see `docs/CI_CD.md`): single `main` branch, promotion by git tag. Push to main → dev deploy; `v*-rc.*` → staging (+ Locust load test); `v*.*.*` → prod via `release.yaml`: new Agent Engine per release → Cloud Run shadow revision (`--no-traffic`) → smoke test → eval gate (absolute thresholds + regression vs a baseline that only updates on pass) → 10% canary. The canary quality check (`canary-quality-check.yaml`, scheduled or `make nightly`) pulls real sessions for canary and champion via the Sessions API and compares agentic-quality scores (absolute floor + relative-to-champion). Note: the Sessions API carries no revision attribution — canary/champion separation works only because each release is a separate Agent Engine resource; Agent Engine's native runtimeRevisions/traffic-split feature (v1beta1) was evaluated and can't support this gate.

**Terraform** (`terraform/`): shared `modules/core` (apis, iam, infrastructure, cicd, model_armor, monitoring, secrets) instantiated per env under `environments/{dev,staging,prod}`. State and tfvars live in GCS (`<project>-tf-state`); Cloud Build reads tfvars from GCS, so run `make sync-tfvars` after local tfvars edits. IAM uses additive `google_project_iam_member` only.

## Reference docs mirror

`refs/` (gitignored) is a local markdown mirror of Google's Gemini Enterprise Agent Platform docs (Build/Govern/Optimize/Scale). Check it before web searches for Agent Engine/eval/Memory Bank/Agent Identity questions. Raw markdown for any live docs page is available at `<page-url>.md.txt`. Caveat: Google's own source has a bug where hyperlinked identifiers inside code blocks become doc-reference URLs — the mirror has been cleaned, but fresh fetches need the same fix. These features are largely Preview-stage; treat doc claims as unverified until tested against a live deployment (several were confirmed wrong this way).
