# V2 Architecture: Selective Distribution with Governed External Access

**Date:** 2026-07-17
**Status:** Approved (design); implementation phased
**Branch context:** evolves the system currently on `vertex-ai-platform-updates`

---

## 1. Context and problem

V1 is a single Vertex AI Agent Engine hosting a root coordinator (Gemini 2.5 Pro) with in-process specialists (product / order / billing on Flash, refund as a `SequentialAgent` on Pro), fronted by a FastAPI backend on Cloud Run, with Firestore + Memory Bank as the data layer and an eval-gated, tag-driven CI/CD pipeline. It was validated end-to-end by a from-scratch environment build (workshop-494016).

V2 is driven by three new product requirements, not by architectural preference:

1. **Real external integration** — live shipment tracking via a FedEx MCP server, which introduces governed egress, third-party credentials, and external failure modes.
2. **Hardened financial path** — refunds gain human-in-the-loop approval, requiring a distinct identity, audit trail, and independent release cadence.
3. **Production posture** — edge protection, two-region availability, and platform-level governance (per-agent identity, gateway, topology observability) suitable for a real product.

## 2. Goals and non-goals

**Goals**
- Every network boundary justified by a named requirement; every non-boundary a deliberate cost refusal.
- Money path: LLM can only *stage* a refund; a human authorizes; deterministic code executes.
- External calls (FedEx) governed at platform level: identity-scoped, tool-level IAM, credentials never held by agent code.
- Survive a regional outage with a defined, graceful degradation story.
- Eval-gated releases preserved for every independently deployable unit.

**Non-goals (explicitly rejected)**
- Full A2A distribution of all agents — cost without requirement.
- Mesh topology (agents calling agents directly) — destroys the single governance/observability choke point.
- Global deployment archetype — requires globally replicated session state the platform doesn't offer (Sessions/Memory Bank are regional).
- Native Agent Engine runtime revisions for canarying — verified live: Sessions API carries no revision attribution, which the real-traffic canary check depends on.
- EU data residency — real per-region data partitioning; named future work, out of scope.

## 3. Final topology

```
                        Global External ALB  (+ Cloud Armor, Cloud CDN)
                          │  ingress locked: LB-only
          ┌───────────────┴────────────────┐
   us-central1 stack                europe-west1 stack        (isolated per-region stacks,
          │                                │                   geo-proximity routing)
   Cloud Run: frontend + backend    Cloud Run: frontend + backend
          │                                │
   Orchestrator Engine (Pro)        Orchestrator Engine (Pro)
   ├─ Product agent   (in-process, Flash)
   ├─ Billing agent   (in-process, Flash)
   ├─ Order agent     ──A2A──►  Order Engine (own identity)
   │                              └─ MCP: FedEx tracking ──(Agent Gateway egress)──► FedEx API
   └─ Refund agent    ──A2A──►  Refund Engine (own identity)
                                  └─ SequentialAgent: validate → eligibility → stage-pending
                                       └─ HITL: human approval → deterministic execution
   Shared: Firestore (durable, cross-region) · Memory Bank (orchestrator only, per region)
```

| Component | Form | Requirement that pays for it |
|---|---|---|
| Orchestrator | Own engine, thin: routing + context brokering + composition only | Critical path; thinness is its availability strategy |
| Product, Billing | **In-process** `AgentTool` specialists (unchanged) | No isolation requirement; a network hop buys nothing |
| Refund | **A2A agent**, own engine + identity; `SequentialAgent` kept intact inside | Financial exposure: own identity, audit, release cadence, HITL |
| Order | **A2A agent**, own engine + identity | Sole agent with governed external egress (FedEx MCP) |
| FedEx MCP server | Cloud Run service, registered in Agent Registry | Real capability; the object the gateway governs |

Constructs: specialists deploy via the platform `A2aAgent` template (server side); the orchestrator consumes them via ADK `RemoteA2aAgent` wrapped in `AgentTool` (client side; requires `google-adk[a2a]`).

## 4. Decision records

**D1 — Hybrid split, not full A2A.** Distribution is a reliability/latency/ops *cost* paid to buy isolation. Refund (financial) and order (external egress) have buyers; product/billing do not.

**D2 — `identity_type=AGENT_IDENTITY` at creation on all engines.** Cannot be patched on later (doc-verified); required for gateway-mediated features and Semantic Governance eligibility. Known live-verified constraints carry over: trust domain is `agents.global.proj-<PROJECT_NUMBER>` (docs say `project-`, docs are wrong), and the CAA opt-out env var must be the **string** `"False"` (docs show a bool, docs are wrong).

**D3 — Agent Gateway in egress mode only.** Governs order-agent → FedEx MCP with tool-level IAM (tool name, read-only vs read-write). No ingress gateway: the Model Armor bypass gap is closed instead by **re-enabling the existing ADK `ModelArmorSafetyFilterPlugin` on the engines** (currently written but disabled) — zero new infrastructure; backend screening remains as outer layer.

**D4 — FedEx credentials in Agent Identity Auth Manager** (2-legged OAuth provider): agent never holds the raw secret; access attributable to the agent's SPIFFE ID. Fallback if Auth Manager (Preview) proves unstable: Secret Manager.

**D5 — Refund HITL, LLM-free execution path.**
- Refund pipeline's final step *stages* a pending record in Firestore: `{status: PENDING_APPROVAL, order_id, items, amount, reason, requesting_user, session_id, evidence}`.
- Notification via Pub/Sub; approvals surfaced in an admin route of the existing React app behind a dedicated approver role.
- Approval = authenticated backend endpoint verifying the **approver's** identity; dual control enforced (requester ≠ approver).
- On approve: deterministic code executes the refund in a Firestore transaction, idempotency-keyed on the pending-record ID (safe under retries).
- TTL expiry on pending records (Cloud Tasks / scheduled sweep); full audit trail (who, when, outcome).
- A2A `input-required` task state carries the pause; the resume path only delivers the outcome message — never executes.

**D6 — Memory: one Memory Bank, at the orchestrator.** All user utterances flow through the orchestrator session, so generation completeness is preserved. Recall reaches specialists via **root-brokered preferences**: the root's instruction requires including relevant preferences in delegation messages (auditable in traces). Optional later: grant only the product agent's identity read access to the central bank (Memory Bank IAM Conditions). Per-specialist Memory Banks rejected — preferences are user-level and cross-domain.

**D7 — Session/context continuity via ADK's `RemoteA2aAgent`** (verified in 2.4.0 source): per-specialist continuity through A2A `context_id` stored in orchestrator session metadata; delta-history forwarding; other-agents' replies included in forwarded context. **`session.state` does not cross the wire** — pre-split gate: audit every `tool_context.state` read/write and confirm no cross-agent state reads exist.

**D8 — Edge: Global External ALB + Cloud Armor + Cloud CDN; Cloud Run ingress locked to LB-only.** Armor provides per-client rate limiting/bot shedding before requests cost compute or LLM calls; CDN serves the frontend bundle and cacheable product GETs (LLM deflection). Closes the current public-`run.app`-URL bypass.

**D9 — Two regions (us-central1 + europe-west1), Google multi-regional archetype:** full isolated stacks per region (platform-forced: Sessions/Memory Bank are regional), geo-proximity routing, region-pinned conversations. **Failover behavior, by design:** conversation restarts in the surviving region with context rebuilt from Firestore durable data (orders, preferences) — graceful restart, not migration. Firestore remains single, globally accessible.

**D10 — Reliability in the orchestrator:** per-dependency circuit breakers + timeouts per remote agent (specialist outage degrades one domain, never the system); existing backend breaker and 429-exclusion unchanged; ADK retry/backoff on all LLM and inter-agent calls carries over. SLOs re-baselined post-split (p95 will move; the staging load-test gate must gate against the new truth).

**D11 — CI/CD: separate-engine-per-release stays** (see non-goals: revision attribution gap). Per-agent Cloud Build pipelines with path filters; each independently deployable unit keeps shadow → eval gate → canary. **A2A agent cards act as contracts**: orchestrator CI validates against each specialist's published card so independent release trains can't silently break the mesh.

**D12 — Evals:**
- **Gate zero (before any split work):** verify the custom eval-inference adapter (`--custom-inference`, `async_stream_query`) surfaces **remote A2A** handoffs. This is the one unknown that can block the whole eval-gated pipeline.
- HITL split evals: (a) LLM-evaled — does the agent stage a correct, well-formed pending request; (b) plain pytest — approve/reject/expiry execution logic (deterministic code needs no LLM judge).
- New integration cases: multi-domain fan-out, per-specialist follow-up continuity (`context_id`), cross-domain reference, preference brokering in delegation messages.
- **Environment simulation** (new eval capability): inject FedEx MCP 503s/latency to prove the resilience story.
- **Feedback service** wired from the frontend (thumbs + labels tied to session/event IDs) as a real-user signal alongside LLM-judged canary scores.

## 5. Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| Eval adapter can't see A2A handoffs | **Blocker for Phase 2** | Gate zero: verify first, while cheap; budget adapter work |
| Preview-feature instability (A2A runtime, Gateway, Auth Manager, Feedback) | Medium | Fallbacks named per feature (e.g. Secret Manager for D4); doc claims treated as unverified until live-tested — two doc errors already known (D2) |
| Latency regression from A2A hops | Medium | Thin orchestrator, parallel fan-out, per-hop timeouts; re-baselined SLOs (D10) |
| Terraform coverage gaps for new resource types (gateway, registry, auth providers) | Medium | Decide per resource: IaC or documented runbook step (Online Monitors precedent); no silent gaps |
| Cross-agent `session.state` dependency discovered late | Low | Pre-split state audit (D7) |
| Cost creep (3 engines × 2 regions × min-instances, per-hop token forwarding) | Low | min-instances only on money path; delta-history forwarding; in-process product/billing keeps the high-volume path cheap |

## 6. Phasing

**Phase 1 — Governed external capability (no topology change)**
FedEx MCP server + Agent Registry entry + Agent Gateway (egress) + Auth Manager provider + Model Armor plugin re-enable. Highest value per effort; independent of everything else.

**Phase 2 — The split (gated on gate-zero eval verification)**
Refund + order to A2A engines with identities; HITL flow end-to-end (pending records, approver UI, deterministic execution); orchestrator resilience code (per-dependency breakers/timeouts); `session.state` audit; preference brokering; contract tests; per-agent pipelines.

**Phase 3 — Edge, regions, and signal polish (independent; can land anytime)**
Global ALB + Armor + CDN + ingress lock; europe-west1 stack; SLO re-baseline; environment-simulation eval suite; Feedback service integration; topology/observability registration.

## 7. Open questions

1. Gate zero result — does `async_stream_query` surface remote A2A events? (Determines Phase 2 start.)
2. Auth Manager Terraform support — IaC or runbook? (Resolve during Phase 1.)
3. Approver notification channel — Pub/Sub → email vs. in-app only for v2? (Product call, low risk either way.)
