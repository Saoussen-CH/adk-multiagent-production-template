# Production Evaluation Architecture

## Overview

This document describes the 5-stage production evaluation architecture for the Customer Support Multi-Agent System. Each stage uses different tools and serves a different purpose in the quality assurance pipeline.

```
┌────────────────────────────────────────────────────────────────────────┐
│                    EVALUATION PIPELINE                                 │
│                                                                        │
│  Stage 1       Stage 2       Stage 3       Stage 4       Stage 5      │
│  ┌─────┐      ┌─────┐      ┌─────┐      ┌─────┐      ┌───────────┐   │
│  │LOCAL│──────│ CI  │──────│STAGE│──────│PROD │──────│  CANARY   │   │
│  │ DEV │      │PIPE │      │EVAL │      │GATE │      │ QUALITY   │   │
│  └─────┘      └─────┘      └─────┘      └─────┘      │  CHECK    │   │
│  ADK local    ADK+pytest   Vertex AI    Vertex AI    └───────────┘   │
│  InMemory     InMemory     Eval Svc     Eval Svc     Sessions API +  │
│  Runner       Runner       (deployed)   (deployed)   agentic rubric  │
│                                                       (scheduled)     │
└────────────────────────────────────────────────────────────────────────┘
```

## Stage Summary

| Stage | Name | Tool | Trigger |
|-------|------|------|---------|
| 1 | Local Development | ADK AgentEvaluator + InMemoryRunner | Manual (`pytest tests/`) |
| 2 | CI Pipeline | ADK AgentEvaluator + pytest | Every PR and push |
| 3 | Post-Deploy Eval | Vertex AI Gen AI Eval Service | After deploy to staging or prod |
| 4 | Production Pre-Canary Eval Gate | Vertex AI Gen AI Eval Service | After shadow deploy to prod, before canary enable |
| 5 | Canary Quality Check | Vertex AI Gen AI Eval Service + Agent Engine Sessions API | Cloud Scheduler (midnight UTC), while a canary is live |

Stages 3 and 4 use `tests/eval_vertex.py` against the live Agent Engine with a **fixed hand-authored dataset**: Stage 3 runs after staging deploy; Stage 4 runs against the prod shadow revision before canary is enabled, and additionally compares against the baseline from the last release that passed this gate (last known-good release, not a rolling window).

Stage 5 is a different kind of check, using `tests/eval_agent_platform.py`'s `canary-check` mode: it pulls **real production conversations** (not a fixed dataset) from both the champion and canary Agent Engines via the Sessions API, and scores them on agentic-behavior rubrics (task success, tool use quality, safety). This exists because a fixed dataset can only tell you the agent still handles the cases someone thought to write down — it can't tell you how the agent is actually doing against the traffic it's serving right now. It also deliberately does not use infra-health signals (HTTP error rate, latency): those confirm the server is up, not that a multi-agent system is behaving correctly, which is a different question for a MAS than for typical web traffic.

**Optional, complementary layer:** Stage 5 only runs while a canary is live. Nothing above continuously watches the standing champion for *slow* drift between releases — see [Ambient Drift Monitoring](#ambient-drift-monitoring-online-monitor--quality-alerts) below for that (Online Monitor + a Terraform-managed Cloud Monitoring alert).

---

## Stage 1: Local Development

**Purpose:** Fast feedback loop during development.

**Tools:** ADK `AgentEvaluator`, `InMemoryRunner`, mocked Firestore backend

**Metrics:** Rouge-1, tool trajectory (exact match on structured args)

**How to run:**
```bash
# Run unit tests with eval
pytest tests/unit/ -v -s

# Run integration tests
pytest tests/integration/ -v -s

# Use fast profile for quick feedback
EVAL_PROFILE=fast pytest tests/unit/ -v -s
```

**Key files:**
- `tests/unit/test_agent_eval_ci.py`: unit-level agent evals
- `tests/integration/test_integration_eval_ci.py`: integration evals
- `tests/conftest.py`: mock setup for Firestore

---

## Stage 2: CI Pipeline

**Purpose:** Automated quality gate on every PR/push.

**Tools:** ADK `AgentEvaluator` + `InMemoryRunner` + pytest

**Metrics:** Vary by profile:
- `fast` (PR): Rouge-1 only: free, fast
- `standard` (push to main / release): + tool trajectory (unit), rubric-based LLM judge (integration)
- `full` (deeper post-deploy checks): + final_response_match_v2

**CI/CD mapping:**
| Event | Profile | Gate |
|-------|---------|------|
| PR to `main` (no agent changes) | `fast` | Must pass to merge |
| PR to `main` (agent code changed) | `standard` | Must pass to merge (auto-detected) |
| Push to `main` (dev deploy) | `standard` | Blocks deployment |
| Tag `v*.*.*-rc.*` (staging) | `standard` | Blocks staging deploy |
| Tag `v*.*.*` (prod release) | `standard` | Post-deploy eval gates canary enable, vs last known-good release baseline |
| Canary quality check | n/a (agentic rubric metrics) | Real-traffic eval drives promote/rollback while a canary is live |

**How to run:**
```bash
# Simulate CI profiles locally
EVAL_PROFILE=fast pytest tests/ -v
EVAL_PROFILE=standard pytest tests/ -v
EVAL_PROFILE=full pytest tests/ -v
```

**Key files:**
- `cloudbuild/pr-checks.yaml`, `cloudbuild/cloudbuild-deploy.yaml`: CI pipeline definitions
- `tests/eval_configs/unit/{fast,standard,full}.json`
- `tests/eval_configs/integration/{fast,standard,full}.json`
- `tests/eval_configs/__init__.py`: profile loader

---

## Stage 3: Staging Deployment Eval

**Purpose:** Evaluate the *deployed* agent (not local code) before promoting to production.

**Tools:** Vertex AI Gen AI Evaluation Service (`client.evals`)

**Metrics:**
- `TOOL_USE_QUALITY`: Did the agent use the right tools with correct parameters?
- `FINAL_RESPONSE_QUALITY`: Is the response accurate and helpful?
- `HALLUCINATION` (full profile): Did the agent fabricate information?
- `SAFETY` (full profile): Is the response safe and appropriate?

**How it works:**
1. Deploy agent to staging Agent Engine
2. Run `eval_vertex.py` against the staging deployment
3. Script sends prompts → collects responses → runs Vertex AI eval metrics
4. HTML report saved locally as `eval-TIMESTAMP.html` (e.g. `eval-20260306-152928.html`)
5. If `GOOGLE_CLOUD_STORAGE_BUCKET` is set: report uploaded to `gs://BUCKET/eval-reports/eval-TIMESTAMP.html`
6. Results logged to Vertex AI Experiments as run `eval-TIMESTAMP`; GCS URI recorded as a param in the run: all three (file, GCS path, experiment run) share the same timestamp for easy correlation
7. If all metrics pass thresholds → promote to production
8. If any metric fails → block promotion, alert team

**How to run:**
```bash
# Standard eval via make (recommended)
make eval-post-deploy ENV=staging

# Standard eval (tool use + response quality) — multi-agent setup
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID \
    --custom-inference

# Full eval (+ hallucination + safety)
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID \
    --custom-inference \
    --profile full

# Save results + debug inference (includes intermediate_events)
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID \
    --custom-inference \
    --output eval_results.json \
    --save-inference inference_debug.json

# Dump SDK intermediate_events format to file (for debugging/format research)
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID \
    --inspect-sdk-events sdk_events.json

# Adjust delay between prompts (default 3s, increase for rate limit issues)
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID \
    --custom-inference \
    --delay 8.0
```

**CLI flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--agent-engine-id` | required | Full resource name of the deployed Agent Engine |
| `--profile` | `standard` | Eval config profile (`standard` or `full`) |
| `--dataset` | `post_deploy_cases.json` | Path to eval dataset JSON |
| `--output` | none | Save results to JSON file |
| `--delay` | `3.0` | Seconds between prompts (rate limit protection) |
| `--custom-inference` | off | Use custom `async_stream_query()` adapter (required for multi-agent AgentTool) |
| `--save-inference` | none | Save raw prompt/response/intermediate_events to JSON for debugging |
| `--inspect-sdk-events` | none | Run SDK inference, dump raw `intermediate_events` to FILE, then exit (format research) |
| `--sdk-inference` | off | Legacy alias for `--custom-inference` |
| `--update-baseline` | none | GCS path to baseline JSON (`gs://...`). Compares composite score; saves updated baseline on pass; exits 1 on regression |
| `--regression-threshold` | `0.10` | Max allowed relative composite score drop vs the last known-good release baseline (release + release-staging gates) |

**Key files:**
- `tests/eval_vertex.py`: main eval script
- `tests/eval_configs/post_deploy/{standard,full}.json`: metric configs
- `tests/post_deploy/datasets/post_deploy_cases.json`: eval dataset (10 cases)
- `tests/post_deploy/dataset_converter.py`: ADK → Vertex AI format converter

### Custom Inference Adapter vs SDK Inference

Pass `--custom-inference` for multi-agent systems that use `AgentTool`. Without it, the SDK's built-in `run_inference()` is used, which fails on AgentTool.

**Why the SDK's `run_inference()` fails for AgentTool:**

The SDK's internal parser extracts the final response with:
```python
resp_item[-1]["content"]["parts"][0]["text"]
```

With `AgentTool`, the conversation flow is:
1. Root agent emits a `function_call` event (delegating to sub-agent)
2. Sub-agent returns a `function_response` event (with result text)
3. Root agent emits a final `text` event (human-readable response)

The SDK stops at event 2 and fails to parse it as text. The custom adapter uses `async_stream_query()` which yields all events, matching how the production backend works.

```
SDK run_inference():   function_call → function_response → STOPS (parse error)
Custom adapter:        function_call → function_response → text response → DONE
Production backend:    function_call → function_response → text response → DONE
```

> `stream_query()` (sync) also has issues: it only yields the first event for AgentTool calls. The custom adapter always uses `async_stream_query()`.

**intermediate_events and TOOL_USE_QUALITY:**

The custom adapter preserves `thought_signature` and `function_call.id` in intermediate events — these fields are required by the `TOOL_USE_QUALITY` rubric evaluator. Stripping them causes the judge to see an empty trajectory and score ~0.02.

**When to use SDK inference (no `--custom-inference`):**
- Single-agent systems that don't use `AgentTool`
- Use `--inspect-sdk-events FILE` to dump what the SDK captures for format comparison

### Resilience Features

The script includes retry logic for common transient failures:

- **Agent Engine 503s:** `async_stream_query()` sometimes returns `503 UNAVAILABLE` (gRPC connection issues). The adapter retries up to 3 times with exponential backoff (5s, 10s).
- **Polling SSL errors:** `get_evaluation_run()` can hit transient SSL/connection errors during the polling loop. Up to 5 consecutive failures are tolerated before giving up.

### Judge Rate Limits

The eval service uses Gemini as an LLM judge to score responses. With 10 items and 2 metrics, that's 20 judge calls which can hit `RESOURCE_EXHAUSTED` rate limits. Items that fail at the judge level show as `failed_items` in the results: they are excluded from scoring, not counted as quality failures.

**Tips to improve judge success rate:**
- Use smaller datasets (3-5 items) for more reliable scoring
- Increase `--delay` between prompts
- Run at off-peak hours

### Prerequisites

Before running post-deploy eval:

1. **Agent Engine deployed** with a valid resource name
2. **Firestore permissions:** The Vertex AI service agent (`service-PROJECT_NUMBER@gcp-sa-aiplatform.iam.gserviceaccount.com`) must have `roles/datastore.user` to access Firestore:
   ```bash
   gcloud projects add-iam-policy-binding PROJECT_ID \
     --member="serviceAccount:service-PROJECT_NUMBER@gcp-sa-aiplatform.iam.gserviceaccount.com" \
     --role="roles/datastore.user"
   ```
3. **GCS bucket** for report upload: set `GOOGLE_CLOUD_STORAGE_BUCKET` in `.env`: the script uploads the HTML report to `gs://BUCKET/eval-reports/eval-TIMESTAMP.html` and records the URI in the Vertex AI Experiments run. Without this, the report is saved locally only and the experiment run will have no `report_gcs_uri` param.
4. **Dataset IDs must match seeded Firestore data**: use real order/invoice IDs (e.g., `ORD-12345`, `ORD-67890`, `INV-2025-001`), not placeholder IDs

### AgentInfo Construction

The eval service requires `AgentInfo` to provide tool declarations and agent instructions. On `google-cloud-aiplatform>=1.160.0` the `AgentInfo` schema changed to a hierarchical `agents: dict[str, AgentConfig]` + `root_agent_id` shape — the old flat `AgentInfo(name=..., instruction=...)` constructor now raises a pydantic `extra_forbidden` validation error.

`AgentInfo.load_from_agent()` — previously broken for this project on older SDK versions (ADK `PreloadMemoryTool` lacked `__globals__`, and `AgentTool`-wrapped sub-agents caused recursive introspection failures) — has been verified to work on `>=1.160.0`. It does not recurse into `AgentTool`-wrapped sub-agents as separate `agents` entries (this codebase uses agents-as-tools, not ADK's native `sub_agents=[...]` hierarchy), but each sub-agent's instruction/description is still captured as a `FunctionDeclaration` in the root's `tools` list, which is enough context for scoring.

Both `tests/eval_vertex.py` and `tests/eval_agent_platform.py` share one helper — `build_agent_info()` in `eval_agent_platform.py` — which tries `load_from_agent()` first and falls back to manually constructing the new schema shape if it ever fails:
```python
try:
    return types.evals.AgentInfo.load_from_agent(agent=root_agent)
except Exception:
    return types.evals.AgentInfo(
        name=root_agent.name,
        root_agent_id=root_agent.name,
        agents={root_agent.name: types.evals.AgentConfig(
            agent_id=root_agent.name,
            agent_type="LlmAgent",
            instruction=root_agent.instruction or "",
        )},
    )
```

`agent_resource_name` is intentionally omitted in both paths: when set, `evaluate()` re-runs its own SDK inference which breaks on `AgentTool` and returns empty responses.

---

## Stage 4: Production Pre-Canary Eval Gate

**Purpose:** Eval gate against the shadow revision before enabling canary traffic. The new revision is deployed with `--no-traffic` — real users are unaffected. If eval fails, the shadow stays at 0% and the canary is never enabled.

**Tools:** Same as Stage 3 (`eval_vertex.py`)

**How it differs from Stage 3:**
- Runs against the shadow revision URL (prod, 0% traffic), not 100% traffic
- Runs with `standard` profile (same as staging)
- If eval fails → pipeline stops, shadow stays at 0%, no canary is enabled, no rollback needed

**How to run:**
```bash
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT/locations/LOCATION/apps/PROD_APP_ID \
    --dataset tests/post_deploy/datasets/post_deploy_cases.json
```

---

## Stage 5: Canary Quality Check

**Purpose:** While a canary is live, continuously verify it's behaving correctly on **real production traffic** — not a fixed dataset — and drive promote/rollback automatically. A fixed dataset (Stages 3/4) only proves the agent still handles cases someone thought to write down; it can't observe how the agent is actually doing against the traffic it's serving right now.

**Tools:** `tests/eval_agent_platform.py`'s `canary-check` mode — Agent Engine Sessions API + Vertex AI Gen AI Eval Service multi-turn rubric metrics, run on a schedule via Cloud Scheduler (or manually).

**How the decision works:**

1. Resolve both the champion and canary Agent Engine resource names from the current Cloud Run traffic split (canary = `sha-*` tag with partial traffic; champion = highest-traffic revision). If no canary is live, no-op.
2. Pull real sessions for **each** engine via `client.agent_engines.sessions` since `--since` (e.g. last 2h), excluding synthetic eval sessions.
3. Score each engine's real conversations on agentic rubric metrics: `MULTI_TURN_TASK_SUCCESS`, `MULTI_TURN_TOOL_USE_QUALITY`, `MULTI_TURN_SAFETY`.
4. Compare canary vs champion scores on that same real-traffic window:
   - Canary within `--relative-threshold` (default 0.10) of champion AND above `--absolute-floor` (default 0.60) → **promote** (exit 0)
   - Canary regressed beyond either threshold → **rollback** (exit 1)
   - Fewer than `--min-sessions` (default 20) real sessions on either engine → **hold**, no decision yet (exit 2)

There is no persistent baseline file here (unlike Stages 3/4) — each run compares canary and champion against each other on the same real traffic window, so there's nothing to go stale or need resetting after a rollback.

**Why not infra-health metrics (error rate, latency)?** Those confirm the server didn't crash — they say nothing about whether a multi-agent system routed correctly, respected a workflow's gates, or hallucinated an answer. That's a materially different question for a MAS than for typical web traffic, so canary promotion here is driven by agentic quality metrics, not SLOs.

**How to run:**
```bash
# Canary quality check (manual trigger)
python tests/eval_agent_platform.py canary-check \
    --canary-engine-id CANARY_ENGINE_ID \
    --champion-engine-id CHAMPION_ENGINE_ID \
    --since 2h \
    --min-sessions 20 \
    --relative-threshold 0.10 \
    --absolute-floor 0.60 \
    --output /workspace/canary_check_result.json
```

---

## Ambient Drift Monitoring (Online Monitor + Quality Alerts)

**Purpose:** Detect *slow* quality drift on the long-lived, already-promoted champion between releases — weeks of shifting user behavior, not a release event. This is a different failure mode than Stage 5: canary-quality-check's job ends the moment a canary is promoted or there's no canary live; nothing else in this pipeline continuously watches the standing champion in between releases. This layer fills that gap.

**Tools:** Gemini Enterprise Agent Platform's **Online Monitors** (continuous eval over live production traces, exported to Cloud Monitoring) + a Terraform-managed **Cloud Monitoring alert policy** (`terraform/modules/core/monitoring.tf`).

**Why this is split into a manual step + a Terraform-managed step:**

Online Monitor *creation* has no API — no Terraform resource, no `gcloud` command, no SDK method (confirmed by SDK introspection: no `OnlineEvaluator`-related methods exist anywhere in `google-cloud-aiplatform`). It's console-only. That also means it can't be automatically re-pointed after a release promotes a new Agent Engine resource to champion — this repo creates a **new** resource on every release (not same-resource revisions), so the monitor needs re-pointing (or recreating) each time. This is the reason it wasn't used for canary promotion itself (see the canary-quality-check design above) — but it's still worth the manual upkeep for slow, ambient drift detection on the champion, which nothing else here does.

The alert policy that watches the monitor's exported metric, however, is a normal Cloud Monitoring resource with a real API — that part **is** Terraform-managed and needs no manual upkeep once created.

**One-time (and post-release) manual setup:**

1. In the Google Cloud console: **Agent Platform → Agents → Evaluation → Online monitors → New monitor**.
2. Select the current champion Agent Engine, choose **All traces** (or a filter), and configure metrics — at minimum **Task Success** (matches the alert policy below); optionally add **Tool Use Quality** and **Safety**.
3. Set a sampling percentage and a max-samples-per-run cap to control eval cost.
4. Telemetry prerequisites are already set on every deploy (`deployment/deploy.py`'s `ENV_VARS`: `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`, `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=EVENT_ONLY`) — no extra deploy-side change needed.
5. **After any release that promotes a new champion**, repeat step 2 pointed at the new resource (or duplicate and edit the existing monitor). There's no notification when this goes stale — it's a manual runbook step, not something CI enforces.

**Terraform-managed alerting (once, per environment):**

```hcl
# terraform/environments/prod/terraform.tfvars
enable_quality_alerts                = true
quality_alert_task_success_threshold = 0.60
quality_alert_notification_channels  = ["projects/YOUR_PROJECT_ID/notificationChannels/YOUR_CHANNEL_ID"]
```

```bash
make sync-tfvars ENV=prod
make infra-up ENV=prod
```

This creates a `google_monitoring_alert_policy` that fires if the Online Monitor's `task_success` score averages below the threshold for 30+ minutes (`aiplatform.googleapis.com/online_evaluator/scores`, `evaluation_metric_name="task_success"`). It's inert — no data, no false alerts — until an Online Monitor actually exists and is exporting scores; enabling it before step 1-4 above is harmless but pointless.

**Notification channels** are a separate, pre-existing Cloud Monitoring concept (email/Slack/Pub-Sub) — create them via **Monitoring → Alerting → Notification channels** in the console (or `google_monitoring_notification_channel` in Terraform, not included here since it typically needs external credentials like a Slack webhook) and pass their resource names into `quality_alert_notification_channels`.

---

## Tool Comparison: ADK AgentEvaluator vs Vertex AI Eval Service

| Feature | ADK AgentEvaluator | Vertex AI Eval Service |
|---------|-------------------|----------------------|
| **Runs against** | Local agent (InMemoryRunner) | Deployed Agent Engine app |
| **Speed** | Fast (in-process) | Slower (network calls) |
| **Cost** | Free (local compute) | Vertex AI pricing |
| **Metrics** | Rouge-1, tool trajectory, response match, rubric judges | TOOL_USE_QUALITY, FINAL_RESPONSE_QUALITY, HALLUCINATION, SAFETY |
| **Use case** | Dev/CI (Stages 1-2) | Post-deploy (Stages 3-4, 6) |
| **Mocking** | Mocked backends | Real backends (Firestore, etc.) |
| **Environment** | Local / CI runner | GCP project with Agent Engine |

**When to use which:**
- **ADK AgentEvaluator**: Development and CI: fast, free, tests agent logic
- **Vertex AI Eval Service**: Post-deployment: tests the full deployed stack including infrastructure, latency, and real data access

---

## Eval Profile System

All stages support the `EVAL_PROFILE` environment variable:

| Profile | Unit Metrics | Integration Metrics | Post-Deploy Metrics |
|---------|-------------|--------------------|--------------------|
| `fast` | Rouge-1 | Rouge-1 | |
| `standard` | Rouge-1 + tool trajectory | Rouge-1 + rubric judge | TOOL_USE_QUALITY + FINAL_RESPONSE_QUALITY |
| `full` | + response match v2 | + response match v2 | + HALLUCINATION + SAFETY |

**CI/CD mapping:**
```
PR            → fast       (quick feedback, free)
Push main     → standard   (balanced quality gate)
Release       → standard   (gates canary enable, vs last known-good baseline)
Post-deploy   → standard   (deployed agent quality)
Canary check  → n/a        (agentic rubric metrics on real traffic, not a profile)
```

---

## Dataset Format

### Post-Deploy Dataset (`post_deploy_cases.json`)

Simple JSON array for the Vertex AI Eval Service:

```json
[
  {
    "prompt": "Where is my order ORD-12345?",
    "reference": "Your order ORD-12345 is currently in transit via FastShip.",
    "expected_tool_use": [
      {"tool_name": "order_agent", "tool_input": {"request": "track order ORD-12345"}}
    ]
  }
]
```

**Important:** The `"prompt"` column name is required: the eval service SDK looks for this exact key when building `EvaluationItemRequest` objects. Using `"request"` instead will cause all items to fail with `INTERNAL` errors.

**Dataset IDs must match seeded Firestore data:**

| Entity | Valid IDs (demo-user-001) |
|--------|--------------------------|
| Orders | `ORD-12345` (In Transit), `ORD-67890` (Delivered, refundable), `ORD-11111` (Delivered, past window) |
| Invoices | `INV-2025-001` (Pending), `INV-2025-002` (Paid), `INV-2024-003` (Paid) |
| Products | `PROD-001` through `PROD-006` |

### ADK EvalSet (`.evalset.json`)

Use the dataset converter to transform ADK evalsets to Vertex AI format:

```python
from tests.post_deploy.dataset_converter import adk_evalset_to_dataframe

df = adk_evalset_to_dataframe("tests/integration/product_agent_handoffs.evalset.json")
```

---

## Thresholds

### Standard Profile
| Metric | Threshold | Rationale |
|--------|-----------|-----------|
| TOOL_USE_QUALITY | 0.5 | Agent uses correct tools for >50% of queries |
| FINAL_RESPONSE_QUALITY | 0.5 | Responses are accurate and helpful >50% of time |

### Full Profile
| Metric | Threshold | Rationale |
|--------|-----------|-----------|
| TOOL_USE_QUALITY | 0.5 | Same as standard |
| FINAL_RESPONSE_QUALITY | 0.5 | Same as standard |
| HALLUCINATION | 0.5 | Agent doesn't fabricate information |
| SAFETY | 0.8 | Higher bar: safety is critical |

Thresholds should be adjusted upward as the agent matures. Start conservative and ratchet up.
