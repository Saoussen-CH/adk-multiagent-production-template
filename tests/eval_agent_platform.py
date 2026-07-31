"""
Agent Platform Quality Flywheel — Multi-Turn Synthetic Evaluation
==================================================================

Implements the new Gemini Enterprise Agent Platform "Quality Flywheel":
generate synthetic multi-turn user-simulation scenarios, run them against
the agent, score with multi-turn autoraters, and auto-cluster failures.

This is DIFFERENT from tests/eval_vertex.py, and does not replace it:
  - eval_vertex.py: deterministic regression testing against a fixed,
    hand-curated golden dataset (Stage 3-5 of docs/EVALUATION.md). Keep
    using it for CI/CD gating — promote/rollback decisions should stay
    based on a reproducible, reviewed dataset, not LLM-generated scenarios.
  - eval_agent_platform.py (this file): synthetic, LLM-generated multi-turn
    scenarios plus automated loss-cluster failure analysis. Use it to
    discover test cases and failure patterns you didn't think to write by
    hand, and to spot-check the deployed agent's quality on a schedule —
    the closest thing to "online" monitoring that has an actual API today.
    (Online Monitors themselves are console-only in this Preview release —
    there is no SDK/Terraform/gcloud surface for creating one; verified by
    introspecting the installed SDK and cross-checking both the "Continuous
    evaluation with online monitors" and "Run offline evaluations" docs,
    neither of which contain a single API code sample, only console steps.)

Three modes:
  local         — runs the agent in-process (no deployment needed). This is
                  also the place to check whether the SDK's run_inference()
                  now captures multi-agent AgentTool trajectories correctly —
                  the exact bug tests/eval_vertex.py's run_custom_inference()
                  was built to work around. This path drives the LOCAL ADK
                  agent object directly through ADK's own runner rather than
                  a remote stream_query() call, so it is not guaranteed to
                  hit the same bug, but it is not guaranteed to avoid it
                  either — inspect the conversation_history in the output
                  for tool calls from product_agent/order_agent/
                  billing_agent/the refund workflow (not just the root
                  agent's first delegation) to confirm.
  cloud         — triggers create_evaluation_run() against the deployed
                  Agent Engine resource with SYNTHETIC scenarios (the "Cloud
                  Agent Eval" flow from Google's reference notebook) and
                  polls until done. Good for periodic (e.g. weekly)
                  diagnostic/coverage-expansion runs, not a promotion gate.
                  --from-real-sessions (score real captured sessions instead
                  of synthetic ones, so the run is console-visible) is
                  CONFIRMED NOT TO WORK server-side — every item fails with
                  an opaque INTERNAL error regardless of user_simulator_config
                  — see run_cloud_evaluation()'s docstring for the full
                  investigation and why this is architectural (real sessions
                  already have responses; create_evaluation_run exists to
                  produce responses via simulation, not consume ones that
                  already exist). Use canary-check for real-session scoring
                  that actually works — it just won't show up in the console.
  canary-check  — compares a canary and champion Agent Engine resource on
                  REAL production traffic (pulled via the Sessions API —
                  client.agent_engines.sessions.list()/.events.list(), which
                  has a genuine per-resource API unlike Online Monitors),
                  scored on agentic behavior (MULTI_TURN_TASK_SUCCESS,
                  MULTI_TURN_TOOL_USE_QUALITY, MULTI_TURN_SAFETY) rather than
                  infra health (latency/error rate tell you the server
                  didn't crash, not whether the agent behaved correctly).
                  This is the intended replacement for cloudbuild-nightly's
                  old fixed-dataset regression re-run, used to drive the
                  canary promote/rollback decision during the bake window.

Requires google-cloud-aiplatform>=1.160.0. generate_conversation_scenarios,
generate_loss_clusters, and the MULTI_TURN_TASK_SUCCESS /
MULTI_TURN_TOOL_USE_QUALITY / MULTI_TURN_TRAJECTORY_QUALITY rubric metrics
do not exist in older versions (verified empirically: absent in 1.136.0,
present in 1.160.0). The Sessions API (client.agent_engines.sessions) and
the real-session-to-EvalCase conversion used by canary-check have been
verified structurally (schema, method signatures) but not against a live
project with real traffic — see session_to_conversation_history()'s
docstring for the first place to check if scores look wrong.

Usage:
    # Local: generate scenarios, run against the in-process agent, score, cluster failures
    python tests/eval_agent_platform.py local --count 5 --max-turn 3 --output results/local_eval.json

    # Cloud: same, but against the deployed Agent Engine (periodic diagnostic, not a gate)
    python tests/eval_agent_platform.py cloud --agent-engine-id <resource_name> \\
        --count 5 --max-turn 3 --dest gs://your-bucket/eval-runs --output results/cloud_eval.json

    # Canary check: real-traffic comparison for the promote/rollback decision
    python tests/eval_agent_platform.py canary-check \\
        --canary-engine-id <canary_resource_name> --champion-engine-id <champion_resource_name> \\
        --since 2h --min-sessions 20 --output results/canary_check.json
    # exit 0 = promote, 1 = rollback, 2 = hold (not enough real sessions yet)

Prerequisites:
    - GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION env vars set
    - google-cloud-aiplatform[adk,evaluation]>=1.160.0 installed
    - For cloud/canary-check: the agent already deployed (see deployment/deploy.py)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import vertexai
from dotenv import load_dotenv
from vertexai import Client, types

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_COUNT = 5
DEFAULT_MAX_TURN = 5
DEFAULT_QPS = 5.0

DEFAULT_GENERATION_INSTRUCTION = (
    "Generate scenarios covering the four support domains: product search, "
    "order tracking, billing/invoices, and refund requests (including the "
    "refund eligibility pre-check before the user is asked for a reason)."
)
DEFAULT_ENVIRONMENT_CONTEXT = (
    "The user is an authenticated customer of an e-commerce electronics/office "
    "furniture store. They may have existing orders, invoices, and past purchases."
)

# Core multi-turn metrics for agentic evaluation (Google's reference notebook default set).
DEFAULT_METRICS = [
    types.RubricMetric.MULTI_TURN_TASK_SUCCESS,
    types.RubricMetric.MULTI_TURN_TOOL_USE_QUALITY,
    types.RubricMetric.MULTI_TURN_TRAJECTORY_QUALITY,
]

# Loss-cluster analysis is only supported for these two metrics per Google's
# reference notebook — passing others does not yield useful clusters.
LOSS_CLUSTER_METRIC = types.RubricMetric.MULTI_TURN_TOOL_USE_QUALITY
LOSS_CLUSTER_METRICS = [types.RubricMetric.MULTI_TURN_TASK_SUCCESS, types.RubricMetric.MULTI_TURN_TOOL_USE_QUALITY]

TERMINAL_STATES = ("SUCCEEDED", "FAILED", "CANCELLED")

# Sessions started by the eval harness itself (see customer_support_mas/callbacks.py's
# auto_save_to_memory) are excluded from real-traffic sampling — they're synthetic,
# not real user conversations.
EVAL_SESSION_PREFIX = "___eval___session___"

# Canary-vs-champion promotion metrics: real agentic behavior, not infra health.
# SAFETY is included because it's cheap reference-free coverage on real traffic;
# TASK_SUCCESS/TOOL_USE_QUALITY are the two metrics generate_loss_clusters supports.
CANARY_CHECK_METRICS = [
    types.RubricMetric.MULTI_TURN_TASK_SUCCESS,
    types.RubricMetric.MULTI_TURN_TOOL_USE_QUALITY,
    types.RubricMetric.MULTI_TURN_SAFETY,
]

DEFAULT_MIN_SESSIONS = 20
DEFAULT_MAX_SESSIONS = 100
DEFAULT_RELATIVE_THRESHOLD = 0.10  # canary must not be >10% worse than champion, per metric
DEFAULT_ABSOLUTE_FLOOR = 0.60  # hard floor regardless of how champion is doing


def parse_since(value: str) -> datetime:
    """Parse a --since value: either 'Nh'/'Nd' (relative) or an ISO8601 timestamp."""
    match = re.fullmatch(r"(\d+)([hd])", value.strip().lower())
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        delta = timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
        return datetime.now(timezone.utc) - delta
    return datetime.fromisoformat(value)


def init_client() -> tuple[Client, str, str]:
    """Initialize the Agent Platform SDK client."""
    load_dotenv()
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    if not project_id:
        logger.error("GOOGLE_CLOUD_PROJECT environment variable is required")
        sys.exit(1)

    vertexai.init(project=project_id, location=location)
    client = Client(project=project_id, location=location)
    logger.info("Initialized Agent Platform client: project=%s location=%s", project_id, location)
    return client, project_id, location


def build_agent_info():
    """Build AgentInfo for the root agent.

    docs/EVALUATION.md documents AgentInfo.load_from_agent() as broken for
    this project on the old SDK (PreloadMemoryTool lacks __globals__,
    AgentTool-wrapped sub-agents cause recursive introspection failures) —
    that's why tests/eval_vertex.py builds AgentInfo manually with just
    name+instruction.

    Verified empirically on google-cloud-aiplatform>=1.160.0: the AgentInfo
    schema itself changed (now `agents: dict[str, AgentConfig]` +
    `root_agent_id`, replacing the old flat `name`/`instruction` fields —
    the old manual-construction call now raises a pydantic
    "extra_forbidden" error). load_from_agent() no longer crashes on this
    agent. It does NOT recurse into AgentTool-wrapped sub-agents as separate
    `agents` entries (this codebase uses agents-as-tools, not ADK's native
    `sub_agents=[...]` hierarchy, so `sub_agents` comes back empty) — but
    each sub-agent's full instruction/description IS captured as a
    FunctionDeclaration in the root's `tools` list (AgentTool exposes the
    wrapped agent as a callable tool), which is enough context for scenario
    generation and multi-turn scoring to reason about what the agent can do.
    """
    from customer_support_mas.agents.root.agent import root_agent

    try:
        return types.evals.AgentInfo.load_from_agent(agent=root_agent)
    except Exception as e:
        logger.warning("AgentInfo.load_from_agent() failed (%s) — falling back to a minimal AgentInfo", e)
        return types.evals.AgentInfo(
            name=root_agent.name,
            root_agent_id=root_agent.name,
            agents={
                root_agent.name: types.evals.AgentConfig(
                    agent_id=root_agent.name,
                    agent_type="LlmAgent",
                    instruction=root_agent.instruction or "",
                )
            },
        )


def generate_scenarios(
    client: Client,
    agent_info,
    count: int = DEFAULT_COUNT,
    generation_instruction: str = DEFAULT_GENERATION_INSTRUCTION,
    environment_context: str = DEFAULT_ENVIRONMENT_CONTEXT,
):
    """Step 1: synthesize multi-turn conversation scenarios from the agent's own instructions."""
    logger.info("Generating %d synthetic conversation scenarios...", count)
    eval_dataset = client.evals.generate_conversation_scenarios(
        agent_info=agent_info,
        config={
            "count": count,
            "generation_instruction": generation_instruction,
            "environment_context": environment_context,
        },
        # The scenario-generation model is a preview model only available in
        # the 'global' region — verified live: without this, every call
        # fails with INVALID_ARGUMENT regardless of GOOGLE_CLOUD_LOCATION,
        # since this project's location (us-central1) isn't 'global'.
        allow_cross_region_model=True,
    )
    n = len(eval_dataset.eval_dataset_df) if eval_dataset.eval_dataset_df is not None else 0
    logger.info("Generated %d scenario(s)", n)
    return eval_dataset


def run_local_inference(client: Client, eval_dataset, max_turn: int = DEFAULT_MAX_TURN):
    """Step 2 (local mode): drive the in-process agent through a multi-turn user simulator.

    Passes the actual ADK agent object (not a deployed resource name), so
    this runs through ADK's own runner rather than a remote stream_query()
    call. Inspect the result for sub-agent tool calls beyond the first
    delegation to confirm whether this sidesteps the AgentTool trajectory
    bug that run_custom_inference() in eval_vertex.py works around.
    """
    from customer_support_mas.agents.root.agent import root_agent

    logger.info("Running local multi-turn inference (max_turn=%d)...", max_turn)
    return client.evals.run_inference(
        agent=root_agent,
        src=eval_dataset,
        config={"user_simulator_config": {"max_turn": max_turn}},
    )


def evaluate_dataset(client: Client, dataset_with_trace, metrics=None, qps: float = DEFAULT_QPS):
    """Step 3: score captured traces with multi-turn autoraters."""
    metrics = metrics or DEFAULT_METRICS
    logger.info("Evaluating with metrics: %s", [str(m) for m in metrics])
    return client.evals.evaluate(
        dataset=dataset_with_trace,
        metrics=metrics,
        config={"evaluation_service_qps": qps},
    )


def analyze_loss_clusters(client: Client, eval_result, metric=LOSS_CLUSTER_METRIC):
    """Step 4: automatically group failed evaluations into semantic loss clusters."""
    logger.info("Generating loss clusters for metric=%s...", metric)
    result = client.evals.generate_loss_clusters(eval_result=eval_result, metric=metric)
    n = len(result.results or [])
    logger.info("Loss analysis complete: %d cluster result(s)", n)
    return result


def run_cloud_evaluation(
    client: Client,
    agent_resource_name: str,
    agent_info,
    eval_dataset,
    dest: str,
    metrics=None,
    max_turn: int = DEFAULT_MAX_TURN,
    loss_analysis_metrics=None,
    run_inference: bool = True,
):
    """Cloud mode: trigger a remote evaluation run against the deployed Agent Engine.

    With run_inference=True (synthetic scenarios): orchestrates NEW agent
    execution via user simulation, scoring, and loss analysis server-side in
    one call. Suitable for a scheduled/nightly diagnostic run.

    With run_inference=False (real sessions, already-completed conversations):
    CONFIRMED BROKEN server-side as of this SDK version, not just unverified.
    Two things were tried, both live against a real deployed engine:
      1. Omitting `agent` entirely → clean 400 INVALID_ARGUMENT: "Required
         field is not set" on inference_configs[...].agent_run_config.
         agent_resource. So `agent` must always be passed regardless.
      2. Passing `agent` + `user_simulator_config={"max_turn": 0}` (as well
         as omitting user_simulator_config entirely) → the request is
         ACCEPTED (no validation error), the run starts, but every single
         evaluation item fails identically with an opaque
         "INTERNAL: Internal error occurred." — no further diagnostic detail
         from the API. Same failure with or without an explicit max_turn=0,
         ruling out "field present vs absent" as the cause.
    This turns out to be architectural, not just an undocumented flag
    interaction. Per Google's own "Simulate agent behavior" reference, the
    canonical flow is two separate steps: (1) generate_conversation_scenarios
    produces prompts + a conversation PLAN with no responses yet, (2)
    client.evals.run_inference(agent=..., config={"user_simulator_config":
    ...}) is what actually drives the simulated conversation and produces
    responses. create_evaluation_run()'s agent/user_simulator_config fields
    exist to collapse exactly those two steps (plus scoring) into one call —
    for scenarios that have no responses yet. Real sessions already have
    real responses; there is no simulation step left to run, so handing them
    to this pipeline was the wrong tool architecturally, not a missing
    parameter — which is consistent with an opaque INTERNAL error regardless
    of how user_simulator_config was set.

    For real-session scoring that actually works today, use
    evaluate_real_traffic() / client.evals.evaluate() instead (what
    canary-check uses) — it has no console-visible EvaluationRun artifact,
    but it returns real, correct scores, because it's designed to score
    data that already has responses rather than to also produce them.
    Kept run_inference=False here as a documented dead end rather than
    removed, so this isn't rediscovered from scratch next time; revisit only
    if Google adds a "score existing traces/sessions" entry point to
    create_evaluation_run() itself.
    """
    metrics = metrics or DEFAULT_METRICS
    loss_analysis_metrics = loss_analysis_metrics if loss_analysis_metrics is not None else LOSS_CLUSTER_METRICS

    logger.info("Triggering cloud evaluation run against %s (run_inference=%s)...", agent_resource_name, run_inference)
    kwargs = dict(
        agent=agent_resource_name,
        agent_info=agent_info,
        dataset=eval_dataset,
        metrics=metrics,
        dest=dest,
        loss_analysis_metrics=loss_analysis_metrics,
    )
    kwargs["user_simulator_config"] = {"max_turn": max_turn if run_inference else 0}
    return client.evals.create_evaluation_run(**kwargs)


def poll_evaluation_run(client: Client, evaluation_run, poll_interval_seconds: float = 10.0):
    """Poll a cloud evaluation run until it reaches a terminal state."""
    start_time = time.time()
    current = client.evals.get_evaluation_run(name=evaluation_run.name)

    while current.state not in TERMINAL_STATES:
        elapsed = int(time.time() - start_time)
        logger.info("Evaluation run %s still running (state=%s, elapsed=%ds)...", evaluation_run.name, current.state, elapsed)
        time.sleep(poll_interval_seconds)
        current = client.evals.get_evaluation_run(name=evaluation_run.name)

    logger.info("Evaluation run finished in %ds with state=%s", int(time.time() - start_time), current.state)
    return client.evals.get_evaluation_run(name=evaluation_run.name, include_evaluation_items=True)


def fetch_real_sessions(client: Client, agent_resource_name: str, since: datetime, max_sessions: int = DEFAULT_MAX_SESSIONS):
    """Fetch real (non-eval) sessions for a deployed Agent Engine resource, created since a given time.

    Filters server-side via the `filter` config string where possible, and
    always re-checks client-side too (create_time comparison, eval-session
    exclusion) since the exact filter grammar accepted by this endpoint
    hasn't been verified against a live project.
    """
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    sessions = []
    try:
        iterator = client.agent_engines.sessions.list(
            name=agent_resource_name,
            config={"filter": f'create_time>"{since_str}"'},
        )
    except Exception as e:
        logger.warning("Server-side session filter failed (%s) — listing all and filtering client-side", e)
        iterator = client.agent_engines.sessions.list(name=agent_resource_name)

    for session in iterator:
        session_id = (session.name or "").rsplit("/", 1)[-1]
        if session_id.startswith(EVAL_SESSION_PREFIX):
            continue
        if session.create_time and session.create_time < since:
            continue
        sessions.append(session)
        if len(sessions) >= max_sessions:
            break

    logger.info("Fetched %d real session(s) for %s since %s", len(sessions), agent_resource_name, since_str)
    return sessions


def session_to_conversation_history(client: Client, session) -> list:
    """Convert a real Session's events into a list of types.evals.Message (conversation_history)."""
    events = list(client.agent_engines.sessions.events.list(name=session.name))
    messages = []
    for i, evt in enumerate(events):
        if evt.content is None:
            continue
        messages.append(
            types.evals.Message(
                turn_id=str(i),
                content=evt.content,
                creation_timestamp=evt.timestamp,
                author=evt.author,
            )
        )
    return messages


def build_eval_dataset_from_real_sessions(client: Client, agent_resource_name: str, agent_info, since: datetime, max_sessions: int):
    """Build an EvaluationDataset of real conversations for a deployed Agent Engine resource.

    One EvalCase per real session, populated via conversation_history — this
    is the field the multi-turn rubric metrics are designed to read (per the
    EvalCase schema), as opposed to a single prompt/response pair. Not
    live-verified against a real project; if evaluate() doesn't score these
    correctly, the conversation_history construction here is the first place
    to check.
    """
    sessions = fetch_real_sessions(client, agent_resource_name, since=since, max_sessions=max_sessions)
    eval_cases = []
    for session in sessions:
        history = session_to_conversation_history(client, session)
        if len(history) < 2:  # need at least one user turn + one agent turn
            continue
        eval_cases.append(
            types.EvalCase(
                eval_case_id=(session.name or "").rsplit("/", 1)[-1],
                conversation_history=history,
                agent_info=agent_info,
            )
        )
    logger.info("Built %d eval case(s) from real sessions (skipped %d too-short)", len(eval_cases), len(sessions) - len(eval_cases))
    return types.EvaluationDataset(eval_cases=eval_cases), len(sessions)


def evaluate_real_traffic(
    client: Client,
    agent_resource_name: str,
    agent_info,
    since: datetime,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    metrics=None,
    qps: float = DEFAULT_QPS,
):
    """Fetch real sessions for one deployed resource and score them with agentic metrics.

    Returns (summary_metrics_dict, num_real_sessions_found).
    """
    metrics = metrics or CANARY_CHECK_METRICS
    dataset, num_sessions = build_eval_dataset_from_real_sessions(client, agent_resource_name, agent_info, since, max_sessions)
    if not dataset.eval_cases:
        return {}, num_sessions
    eval_result = client.evals.evaluate(dataset=dataset, metrics=metrics, config={"evaluation_service_qps": qps})
    return summarize_result(eval_result), num_sessions


def compare_canary_to_champion(
    canary_summary: dict,
    champion_summary: dict,
    relative_threshold: float = DEFAULT_RELATIVE_THRESHOLD,
    absolute_floor: float = DEFAULT_ABSOLUTE_FLOOR,
) -> tuple[bool, list[str]]:
    """Compare canary's real-traffic agentic scores against champion's, per metric.

    A metric fails if EITHER:
      - canary's score is below the absolute floor (regardless of champion), or
      - canary is more than `relative_threshold` relatively worse than champion
        over the same window (protects against blaming the canary for an
        ambient incident that's hitting both revisions equally).
    """
    reasons = []
    for metric_name, canary_stats in canary_summary.items():
        canary_score = canary_stats.get("mean_score")
        if canary_score is None:
            continue

        if canary_score < absolute_floor:
            reasons.append(f"{metric_name}: canary={canary_score:.3f} below absolute floor {absolute_floor}")
            continue

        champion_stats = champion_summary.get(metric_name)
        champion_score = champion_stats.get("mean_score") if champion_stats else None
        if champion_score is None or champion_score <= 0:
            continue  # nothing to compare against — absolute floor already checked above

        relative_drop = (champion_score - canary_score) / champion_score
        if relative_drop > relative_threshold:
            reasons.append(
                f"{metric_name}: champion={champion_score:.3f} -> canary={canary_score:.3f} "
                f"(relative drop {relative_drop:.1%} > threshold {relative_threshold:.1%})"
            )

    return (len(reasons) == 0), reasons


def summarize_result(eval_result) -> dict:
    """Extract a plain-dict summary from an EvaluationResult (mirrors eval_vertex.py's check_thresholds parsing)."""
    summary = {}
    summary_list = getattr(eval_result, "summary_metrics", None) or []
    for agg in summary_list:
        metric_name = getattr(agg, "metric_name", None) or "unknown_metric"
        mean_score = getattr(agg, "mean_score", None)
        num_total = getattr(agg, "num_cases_total", None)
        num_error = getattr(agg, "num_cases_error", None)
        summary[metric_name] = {
            "mean_score": round(float(mean_score), 4) if mean_score is not None else None,
            "num_cases_total": num_total,
            "num_cases_error": num_error,
        }
    return summary


def summarize_loss_clusters(loss_clusters) -> list:
    """Extract a plain-list summary from a GenerateLossClustersResponse.

    The response nests one level deeper than a flat cluster list: each
    LossAnalysisResult carries the metric it was computed for plus its own
    `clusters` list, and each LossCluster's human-readable name/description
    live under `taxonomy_entry`, with case count under `item_count` (not
    `num_cases`) — verified against the real SDK types after an earlier
    version of this function (using getattr for metric/cluster_name/
    description/num_cases directly on each result) silently produced an
    all-None summary despite the underlying API call succeeding.
    """
    out = []
    for result in loss_clusters.results or []:
        metric = result.config.metric if result.config else None
        for cluster in result.clusters or []:
            taxonomy = cluster.taxonomy_entry
            out.append(
                {
                    "metric": metric,
                    "cluster_id": cluster.cluster_id,
                    "l1_category": taxonomy.l1_category if taxonomy else None,
                    "l2_category": taxonomy.l2_category if taxonomy else None,
                    "description": taxonomy.description if taxonomy else None,
                    "num_cases": cluster.item_count,
                }
            )
    return out


def run_canary_check(args):
    """Compare canary vs champion on REAL traffic, scored on agentic behavior (not infra health).

    Exit codes (consumed by the calling Cloud Build step):
      0 — promote: canary's real-session scores hold up against champion's
      1 — rollback: canary regressed on real traffic
      2 — hold: not enough real sessions yet to make a confident decision;
          extend the bake window and re-check later, don't guess
    """
    client, _, _ = init_client()
    agent_info = build_agent_info()
    since = parse_since(args.since)

    champion_summary, champion_n = evaluate_real_traffic(
        client, args.champion_engine_id, agent_info, since=since, max_sessions=args.max_sessions, qps=args.qps
    )
    canary_summary, canary_n = evaluate_real_traffic(
        client, args.canary_engine_id, agent_info, since=since, max_sessions=args.max_sessions, qps=args.qps
    )

    output = {
        "mode": "canary-check",
        "since": since.isoformat(),
        "champion_engine_id": args.champion_engine_id,
        "canary_engine_id": args.canary_engine_id,
        "champion_sessions_found": champion_n,
        "canary_sessions_found": canary_n,
        "champion_summary": champion_summary,
        "canary_summary": canary_summary,
    }

    if canary_n < args.min_sessions or champion_n < args.min_sessions:
        output["decision"] = "hold"
        output["reasons"] = [
            f"Not enough real sessions yet: canary={canary_n}, champion={champion_n}, "
            f"minimum required={args.min_sessions}. Extend the bake window and re-check later."
        ]
        print(json.dumps(output, indent=2, default=str))
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(json.dumps(output, indent=2, default=str))
        sys.exit(2)

    passed, reasons = compare_canary_to_champion(
        canary_summary, champion_summary, relative_threshold=args.relative_threshold, absolute_floor=args.absolute_floor
    )
    output["decision"] = "promote" if passed else "rollback"
    output["reasons"] = reasons
    print(json.dumps(output, indent=2, default=str))

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(output, indent=2, default=str))
        logger.info("Results written to %s", args.output)

    sys.exit(0 if passed else 1)


def run_local(args):
    client, _, _ = init_client()
    agent_info = build_agent_info()

    eval_dataset = generate_scenarios(
        client,
        agent_info,
        count=args.count,
        generation_instruction=args.generation_instruction,
        environment_context=args.environment_context,
    )
    dataset_with_trace = run_local_inference(client, eval_dataset, max_turn=args.max_turn)
    eval_result = evaluate_dataset(client, dataset_with_trace, qps=args.qps)
    loss_clusters = analyze_loss_clusters(client, eval_result)

    output = {
        "mode": "local",
        "summary_metrics": summarize_result(eval_result),
        "loss_clusters": summarize_loss_clusters(loss_clusters),
    }
    print(json.dumps(output, indent=2, default=str))

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(output, indent=2, default=str))
        logger.info("Results written to %s", args.output)

    return output


def run_cloud(args):
    client, _, _ = init_client()
    agent_info = build_agent_info()

    num_real_sessions = None
    if args.from_real_sessions:
        eval_dataset, num_real_sessions = build_eval_dataset_from_real_sessions(
            client,
            agent_resource_name=args.agent_engine_id,
            agent_info=agent_info,
            since=parse_since(args.since),
            max_sessions=args.max_sessions,
        )
        if num_real_sessions == 0:
            logger.error("No real sessions found since %s for %s — nothing to evaluate", args.since, args.agent_engine_id)
            sys.exit(1)
    else:
        eval_dataset = generate_scenarios(
            client,
            agent_info,
            count=args.count,
            generation_instruction=args.generation_instruction,
            environment_context=args.environment_context,
        )

    evaluation_run = run_cloud_evaluation(
        client,
        agent_resource_name=args.agent_engine_id,
        agent_info=agent_info,
        eval_dataset=eval_dataset,
        dest=args.dest,
        max_turn=args.max_turn,
        run_inference=not args.from_real_sessions,
    )
    final_run = poll_evaluation_run(client, evaluation_run, poll_interval_seconds=args.poll_interval)

    output = {
        "mode": "cloud",
        "dataset_source": "real_sessions" if args.from_real_sessions else "synthetic",
        "num_real_sessions": num_real_sessions,
        "agent_engine_id": args.agent_engine_id,
        "run_name": final_run.name,
        "state": str(final_run.state),
        "summary_metrics": summarize_result(final_run.evaluation_item_results) if final_run.evaluation_item_results else {},
    }
    print(json.dumps(output, indent=2, default=str))

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(output, indent=2, default=str))
        logger.info("Results written to %s", args.output)

    if final_run.state == "FAILED":
        logger.error("Cloud evaluation run failed: %s", final_run.error)
        sys.exit(1)

    return output


def main():
    parser = argparse.ArgumentParser(description="Agent Platform Quality Flywheel evaluation")
    subparsers = parser.add_subparsers(dest="action", required=True)

    common_args = argparse.ArgumentParser(add_help=False)
    common_args.add_argument("--count", type=int, default=DEFAULT_COUNT, help="Number of synthetic scenarios to generate")
    common_args.add_argument("--max-turn", type=int, default=DEFAULT_MAX_TURN, help="Max conversation turns per scenario")
    common_args.add_argument("--generation-instruction", type=str, default=DEFAULT_GENERATION_INSTRUCTION)
    common_args.add_argument("--environment-context", type=str, default=DEFAULT_ENVIRONMENT_CONTEXT)
    common_args.add_argument("--output", type=str, default=None, help="Path to write JSON results")

    local_parser = subparsers.add_parser("local", parents=[common_args], help="Evaluate the in-process agent")
    local_parser.add_argument("--qps", type=float, default=DEFAULT_QPS, help="Evaluation service QPS throttle")

    cloud_parser = subparsers.add_parser("cloud", parents=[common_args], help="Evaluate the deployed Agent Engine")
    cloud_parser.add_argument("--agent-engine-id", type=str, required=True, help="Deployed Agent Engine resource name")
    cloud_parser.add_argument("--dest", type=str, required=True, help="GCS path for evaluation run results")
    cloud_parser.add_argument("--poll-interval", type=float, default=10.0, help="Seconds between run-status polls")
    cloud_parser.add_argument(
        "--from-real-sessions",
        action="store_true",
        help="CONFIRMED BROKEN server-side (opaque INTERNAL error, live-verified) — "
        "kept only so this isn't rediscovered from scratch; see run_cloud_evaluation()'s "
        "docstring. Use `canary-check` for real-session scoring that actually works.",
    )
    cloud_parser.add_argument(
        "--since", type=str, default="2h", help="With --from-real-sessions: look at real sessions created since this long ago (e.g. '2h', '24h') or an ISO8601 timestamp"
    )
    cloud_parser.add_argument(
        "--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS, help="With --from-real-sessions: max real sessions to sample"
    )

    canary_parser = subparsers.add_parser(
        "canary-check",
        help="Compare canary vs champion on real traffic, scored on agentic behavior (for canary promotion decisions)",
    )
    canary_parser.add_argument("--canary-engine-id", type=str, required=True, help="Canary Agent Engine resource name")
    canary_parser.add_argument("--champion-engine-id", type=str, required=True, help="Champion Agent Engine resource name")
    canary_parser.add_argument(
        "--since", type=str, default="2h", help="Look at real sessions created since this long ago (e.g. '2h', '24h') or an ISO8601 timestamp"
    )
    canary_parser.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS, help="Max real sessions to sample per engine")
    canary_parser.add_argument(
        "--min-sessions", type=int, default=DEFAULT_MIN_SESSIONS, help="Minimum real sessions required per engine before deciding (else hold)"
    )
    canary_parser.add_argument("--relative-threshold", type=float, default=DEFAULT_RELATIVE_THRESHOLD)
    canary_parser.add_argument("--absolute-floor", type=float, default=DEFAULT_ABSOLUTE_FLOOR)
    canary_parser.add_argument("--qps", type=float, default=DEFAULT_QPS, help="Evaluation service QPS throttle")
    canary_parser.add_argument("--output", type=str, default=None, help="Path to write JSON results")

    args = parser.parse_args()

    if args.action == "local":
        run_local(args)
    elif args.action == "cloud":
        run_cloud(args)
    elif args.action == "canary-check":
        run_canary_check(args)


if __name__ == "__main__":
    main()
