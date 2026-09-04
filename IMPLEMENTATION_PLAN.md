# Capability Intelligence Exchange — Implementation Plan

**Status:** Proposed execution plan

**Source:** `capability_intelligence_exchange_revised_technical_plan.md`

**Planning posture:** Staff-level implementation scope

**Initial platform release:** MCP Registry Alpha (`MCP_REGISTRY_ALPHA_IMPLEMENTATION_PLAN.md`)

**First evaluation workload after the registry foundation:** Swift/iOS concurrency pull-request review

**Primary delivery posture:** Build the registry and execution foundation first, then add comparison, outcomes, and task-aware routing

---

## 1. Executive decision

### Sequencing update — September 3, 2026

The first implementation milestone is now the closed **MCP Registry Alpha** defined in `MCP_REGISTRY_ALPHA_IMPLEMENTATION_PLAN.md`. It delivers protocol-neutral capability identity, MCP server registration and live discovery, immutable tool versions, safe invocation, a run ledger, and a basic operator UI. It precedes the evaluation/router work below and supplies reusable control-plane and execution foundations for it.

The registry is not treated as the product moat or as a replacement for the official MCP Registry. Routing evidence and attributable outcomes remain the long-term differentiators. The immediate sequencing is:

```text
M1 Registry Alpha
  -> M2 capability comparison and human feedback
  -> M3 task-aware routing and first reference vertical
```

Sections below retain the detailed evaluation and router plan for those follow-on milestones. Where their original Phase 0/Phase 1 sequence conflicts with the dedicated Milestone 1 plan, the Milestone 1 plan governs the first release.

After the Registry Alpha foundation, build the intelligence layer as two concurrent workstreams with separate release gates:

1. **Evaluation and router calibration:** a reproducible benchmark that ranks capabilities, establishes quality/cost/latency evidence, and determines when task-aware selection is safe to activate.
2. **Productization:** a closed routing API built from day one that executes curated capabilities, captures trustworthy outcomes, and can later support third-party supply.

Do not start by implementing the six logical planes as independently deployed services. For Phase 0 and the closed alpha, use a modular monolith plus workers, PostgreSQL, and object storage. Preserve domain boundaries in code and extract services only when scale, security isolation, or independent release cadence requires it.

The immediate post-registry deliverable is a closed capability comparison and routing loop with an evidence-backed selection policy. The calibration question is:

> Can pre-execution task features select a capability that improves review success over the strongest static default on unseen tasks without an unacceptable increase in cost per successful review?

The answer must come from a preregistered, held-out evaluation. If task-aware routing does not yet beat the strongest static reviewer, ship the closed alpha with the strongest eligible static quality policy, keep task-aware selection in shadow mode, and use production outcomes to improve it. Calibration controls policy activation; it does not block the platform foundation.

### Evidence update — September 3, 2026

External evidence is sufficient to retire the category-level question of whether specialized models can outperform frontier generalists. A [Shopify product-review slide shared by Tobi Lütke](https://x.com/tobi/status/2094808564355191249) reports a fine-tuned 0.8B model scoring 84.6 against GPT-5.6 Sol xhigh at 83.0 on a specialized buyer-profile task, while reducing its system prompt from 9.1K to 1.1K tokens and increasing throughput from 2M to 72M profiles per day. [Shopify's published Flow case study](https://shopify.engineering/fine-tuning-agent-shopify-flow) separately reports a fine-tuned agent that is 2.2x faster, 68% cheaper, and more accurate than the closed model it replaced, with a production feedback flywheel.

This evidence validates the strategic premise: narrow capabilities plus proprietary evaluation and outcome data can outperform general defaults. It does not remove the need to calibrate capability selection for Swift/iOS PR review or to validate the production outcome loop. Those are now treated as product quality and rollout concerns, not existential company-thesis gates.

---

## 2. Product and architecture understanding

The product is a task-level decision system, not a model router or an MCP catalog. Its proprietary loop is:

```text
Task features
  -> eligible capability versions
  -> predicted utility
  -> immutable routing decision
  -> execution
  -> attributable outcome
  -> version-specific performance history
  -> better routing
```

The implementation must preserve five invariants:

1. A capability version and its executable source are immutable.
2. A routing decision records every candidate, filter, score, input feature, and model/policy version used.
3. Benchmark results and production outcomes remain separate and carry provenance.
4. A production outcome is never attached without authorization and invocation lineage.
5. Features used to route are available before execution; ground-truth or post-execution data cannot leak into routing evaluation.

Supporting systems—provider adapters, hosted runtimes, Tangle, payments, creator tools, and public discovery—exist to supply or exercise the router. They are not prerequisites for calibrating its first policy. MCP registration and invocation are intentionally implemented first as the platform substrate described in the dedicated Registry Alpha plan; that sequencing does not make catalog breadth the product moat.

---

## 3. Required changes to the source plan

| Area | Implementation decision |
|---|---|
| Initial task | Use one canonical task ID: `software.code-review.concurrency`, constrained to Swift. Do not create a parallel `swift.*` taxonomy branch. Language and platform are task dimensions. |
| Swift execution | Include realistic UIKit/SwiftUI and iOS-shaped source in Phase 0. Make source-level review the authoritative task, so an Xcode build is not required for the routing gate. Keep a buildable Swift Package Manager subset for deterministic tooling and treat optional Xcode/macOS validation as a separately reported stratum. macOS execution cannot be treated like the proposed Linux container runtime. |
| Review grading | Use seeded or independently labeled concurrency defects with normalized finding spans and categories. Compile/test grading applies only when a capability returns a patch; it cannot by itself grade review text. |
| Success probability | V0 may return an empirical benchmark estimate and uncertainty, not a calibrated `success_probability`. Use the latter only after calibration is measured on held-out production-like data. |
| Service topology | Implement logical planes as modules in three deployables: API, worker, and web dashboard. Do not create 12–15 network services in V1. |
| Search | Use taxonomy and structured PostgreSQL filters first. Add full-text search when creator supply expands; add pgvector only after measuring structured-retrieval recall. |
| Queue | Use a PostgreSQL-backed durable job table for the experiment and alpha. Adopt Redis or Temporal only when measured throughput or orchestration needs justify it. |
| Tangle | Treat Tangle as a post-calibration challenger and optional adapter. It is not part of the initial eight-candidate matrix or on the critical path for the closed alpha. |
| Payments | Use free alpha quotas or non-cash test credits. A double-entry ledger becomes mandatory when credits have monetary value or provider liabilities exist. |
| Hosted execution | Curated platform-owned adapters only through Phase 1. Do not accept arbitrary seller containers until isolation, scanning, policy, and incident response are production-ready. |
| Learned routing | Require enough labeled, representative production outcomes and a shadow evaluation before enabling ML or online exploration. Calendar phase alone is not an entry criterion. |

---

## 4. Planning assumptions

This plan assumes:

- A core team of four engineers for Phase 0 and Phase 1: two backend/platform, one evaluation/data, and one product/full-stack. A Swift domain expert is available at least part time.
- The project owner will serve as primary Swift concurrency reviewer, and a second qualified iOS engineer is available for blinded calibration and adjudication.
- A product owner can resolve taxonomy, user-policy, and go/no-go decisions within one business day.
- The initial calibration corpus uses non-confidential public, licensed, synthetic, or explicitly sanitized cases so every candidate is eligible. Raw internally owned repositories form a separate controlled-infrastructure stratum.
- No controlled GPU infrastructure or approved private-code model-provider agreement is currently available. Phase 0 therefore does not execute model-based capabilities against raw private repositories.
- Eight curated capability implementations can be invoked through model APIs, command-line tools, or local adapters.
- Secrets for external model providers are available through a managed secret store in deployed environments and local environment injection during development.
- The first alpha is single-region and has no contractual enterprise availability requirement.
- PostgreSQL and S3-compatible object storage are managed services in non-local environments.
- All time estimates are elapsed engineering time for the assumed team, not commitments. A team of one or two should expect roughly two to three times the elapsed duration.

---

## 5. Scope and stage gates

### Phase 0: evaluation foundation and router calibration

**In scope**

- One task family and controlled task dimensions
- Versioned benchmark corpus and hidden holdout
- Five to eight curated capabilities
- Common input/output contracts
- Reproducible evaluation runner and graders
- Deterministic task-aware routing
- Static baselines and offline replay
- Cost, latency, reliability, and outcome capture
- Internal experiment report/dashboard

**Out of scope**

- Public APIs, marketplace, creator onboarding, billing, payouts
- Arbitrary external providers or containers
- General semantic discovery
- Tangle Studio
- Production learning or contextual bandits
- Private customer repositories

### Phase 1: closed coding router

**In scope**

- Authenticated `route_task` and `run_task` interfaces over HTTP and MCP
- Curated provider execution
- Asynchronous invocation lifecycle
- Automatic and explicit outcome reporting
- Idempotency, quotas, audit trails, traces, and operational dashboards
- Shadow and canary routing policies
- A small group of design partners

**Out of scope**

- Self-service seller publishing
- Cash-equivalent credits, provider settlement, or crypto
- Unreviewed containers
- Learned routing in the live path

### Gate G0 — corpus readiness

Proceed to the full experiment only when:

- every case has source/license provenance and a reproducible repository snapshot;
- every private/internal case has data-owner approval, a classification, a retention policy, and an explicit provider-processing policy;
- the corpus includes clean controls, single-defect PRs, and multi-defect or cross-file PRs;
- the primary domain reviewer labels every case, while a second qualified reviewer independently labels a stratified 20% sample and every disputed high/critical finding;
- every defect label includes a frozen mechanism ID, required causal/impact facts, and disqualifying contradictions that can distinguish a correct explanation from a category/span guess;
- measured inter-rater agreement reaches at least 0.80 Cohen's kappa on the fixed rating matrix below before label freeze; otherwise refine the rubric and expand dual review;
- train, validation, and hidden test groups are split by a frozen composite `correlation_cluster_id` that preserves both source-repository and controlled-mutation/archetype lineage dependence;
- the harness reproduces the same deterministic grading result in at least 98% of reruns;
- every candidate used for authoritative comparison has an immutable platform-controlled version or an attested remote implementation revision; unverified mutable remotes are excluded from G1 evidence;
- no candidate capability has received hidden labels or hidden-case artifacts.

Before any split, build `correlation_cluster_id` as the connected component of a graph whose cases are linked when they share a source repository or the same controlled-mutation/archetype lineage, including a seeded template or near-duplicate variant applied across repositories. A broad defect-family label alone is not a lineage edge. Freeze repository and lineage IDs plus the resulting components before allocation; every component stays in one split and is the resampling unit for G1 sizing and activation. If a giant component or too few independent components makes either interval underpowered or imprecise, expand/rebalance the corpus or remain in shadow mode.

For the kappa gate, the rating units are fixed before either reviewer labels the stratified sample: every sampled case crossed with every canonical defect family in the frozen taxonomy, including an explicit `other` family. Each reviewer independently assigns exactly one binary category, `present` or `absent`, to every case×family unit; a clean case is therefore all absent. Compute the primary Cohen's kappa once over the flattened matrix of those common units and report per-family kappas when both categories occur. Every concrete finding must map to one family and carry the revision-aware location from Section 7.3. An unrecognized but asserted finding maps to `other`/present rather than disappearing; a finding asserted by only one reviewer is a present/absent disagreement. Multiple same-family findings do not create extra kappa units: count, span, severity, and category-alias agreement are reported separately with deterministic bipartite span matching, and every count/location disagreement for high or critical findings enters adjudication before label freeze. The protocol versions the family map, unit matrix, alias rules, unmatched treatment, and span tolerance before annotation.

### Gate G1 — task-aware policy activation

G1 is an activation decision for one immutable `evaluation_stratum_id`, not a global router approval. That identifier freezes at least data classification, source-ownership/trust stratum (`universal` versus `private_internal`), task segment, provider-processing policy, and candidate-eligibility universe. A passing holdout authorizes task-aware selection only for request strata explicitly represented by that holdout and preregistered before reveal; the initial universal, non-confidential G1 can never authorize private/internal traffic. Unknown, changed, or unvalidated strata use their strongest eligible static policy while task-aware decisions remain shadow-only. Each additional stratum needs its own fresh representative holdout and a preregistered slice of the system-wide G1 sequential alpha budget; evidence, alpha spend, and revealed components are never pooled across strata. Routing feature flags and decisions persist the exact approved `evaluation_stratum_id` and gate evidence version.

Enable task-aware selection for an `evaluation_stratum_id` only when, on its fresh representative G1 activation holdout, the quality-first policy produces at least 5 percentage points higher task success than the strongest static capability eligible throughout that stratum and its cost per successful task is no more than 20% higher. For each policy, cost per successful task is total platform-attributed primary-attempt cost across all holdout tasks, including costs from unsuccessful tasks, divided by the number of successful primary tasks. Activation requires both an observed task-aware/static cost ratio no greater than 1.20 and a one-sided upper confidence bound at confidence `1-alpha^C_t` for preregistered attempt `t` (never below 95%) no greater than 1.20 under the same paired `correlation_cluster_id` bootstrap. A zero-success arm in the observed holdout fails the gate.

Bootstrap boundary draws are retained, never silently omitted and never treated as a one-draw veto. For the observed sample and each draw, define the task-aware/static ratio on the extended nonnegative reals: a draw with zero task-aware successes is `+infinity`; a draw with zero static successes but at least one task-aware success is `0`; if both arms have zero successes it is `+infinity`. When both arms have successes but the static cost per success is zero, the ratio is `1` if task-aware cost per success is also zero and `+infinity` otherwise. The upper bound is the preregistered empirical `1-alpha^C_t` percentile of all finite and infinite draw values for activation attempt `t`. Fix the bootstrap seed, draw count, quantile convention, and Monte Carlo error rule before revealing results; use exact correlation-cluster resample enumeration when tractable, and otherwise declare the gate inconclusive unless the conservative Monte Carlo error bound leaves the upper limit at or below 1.20. Isolated boundary draws therefore contribute their tail probability rather than vetoing activation based on whether any one draw occurred.

The paired task outcome is the preregistered primary-attempt estimand defined in Section 8.4; diagnostic repeats never change it. A paired cluster bootstrap that resamples frozen `correlation_cluster_id` components and preserves all paired task outcomes and costs within each sampled component must also show a two-sided interval at confidence `1-alpha^S_t` that excludes no improvement in task success. Before freezing or executing that holdout, preregister the success and cost-ratio estimators, charge attribution and currency normalization, observed and resampled boundary handling, confidence-bound construction, and a joint simulation-based sizing analysis using those exact estimands and resampling procedures, the observed composite-cluster size distribution, a conservative calibration-derived bound on candidate discordance, and the joint pilot distribution of success and cost. The default planning alternative is a true eight-percentage-point success lift and a true cost ratio of 1.10; changing either value requires a documented rationale before any activation data is revealed, and the success alternative must remain strictly above five points while the cost alternative remains strictly below 1.20. For attempt `t`, choose the smallest sample whose conservative simulation shows at least 90% probability of satisfying the complete gate simultaneously: observed lift at least five points, the `1-alpha^S_t` interval excluding zero, nonzero successes in both arms, observed cost ratio at most 1.20, and the `1-alpha^C_t` upper bound at most 1.20. Require the one-sided Monte Carlo lower confidence bound for that joint pass probability to be at least 90%, or exact enumeration when tractable. No 90% power claim is made when the true lift is exactly the five-point observed cutoff; under a centered estimator that condition alone passes only about half the time. Before any result is revealed, an undersized holdout may be expanded and re-frozen; after reveal, it may not be extended or pooled. This holdout contains no case—and no correlation component linked to a case—whose candidate outputs or labels were previously revealed.

Treat G1 activation across all `evaluation_stratum_id` values as one system-wide family of sequential decisions with two co-primary requirements. Before the first activation run, preregister the allowed strata, maximum attempts per stratum, and allocations `alpha^S_{s,t}` and `alpha^C_{s,t}` whose combined sum across both requirement types, every stratum `s`, and every attempt `t` is at most 0.05; every allocation is therefore at most 0.05, so each attempt-specific confidence level is at least 95%. A later stratum can test only with unused preregistered alpha or a new explicitly versioned release family that does not retroactively authorize prior traffic. Because any false activation must reject at least one true quality or cost null, the combined Bonferroni budget controls the probability of any false activation across mixed nulls, repeated looks, and strata at 0.05. Freeze the exact stratum definition, router/policy/candidate versions, and a wholly fresh set of representative correlation components for each attempt. As soon as any candidate output, label, aggregate, or interim statistic from an attempt is revealed, permanently mark all of its components `g1_revealed`; they can be reported or used for later training with provenance but never pooled into or reused by any activation claim. A failed or inconclusive attempt can proceed only with the relevant remaining preregistered alpha, a newly frozen policy version where changed, and an entirely fresh holdout sized for the tighter bounds. When the assigned attempt limit or applicable combined alpha budget is exhausted, that stratum's task-aware routing remains in shadow mode.

A value-oriented policy should still be reported as secondary analysis, including whether it achieves at least 20% lower cost per successful task while remaining non-inferior within a 2 percentage-point success margin. It cannot substitute for the quality-first activation gate. Report all baselines and all attempted policy variants, including failures. In every stratum where G1 has not passed—including private/internal strata after only the universal gate passes—the closed alpha uses that stratum's strongest eligible static quality policy while task-aware decisions run in shadow mode.

### Gate G2 — closed-alpha readiness

Proceed to design partners only when:

- tenant authorization—including the Registry Alpha end-to-end 60-second IdP propagation/lookup/cache freshness budget and authoritative cache bypass for privileged actions—idempotency, audit logs, data retention, and secret handling have passed review;
- the invocation SLO and recovery tests in Section 15 pass in staging;
- concurrent dispatch proves every fence atomically owns sufficient worst-case quota/budget reservation, and indeterminate liabilities remain reserved until reconciled;
- queued-run tests prove quote expiry or provider price-version change is detected under the fence locks before reservation/send and requires fresh user-approved routing;
- artifact-upload qualification proves credential-bearing targets use only the sealed, short-lived non-recording transfer path, the backend supplies a bounded in-flight-write/visibility contract that fits the 15-minute cleanup SLO, cleanup cannot attest deletion while a late write may still materialize, and unsupported backends or MCP hosts fail closed;
- at least 90% of alpha-eligible invocations can receive an automatic or attributable explicit outcome;
- route decisions are replayable from stored feature and candidate snapshots;
- provider disablement and policy rollback complete without a redeploy.

Any alpha use of private repositories additionally requires either controlled model infrastructure or an approved provider agreement covering no training, zero retention, security, and incident obligations. Satisfying that infrastructure/contract gate permits eligible static execution and shadow evaluation only; task-aware live selection for private/internal requests also requires a separate representative private/internal G1 pass under the scoped rule above.

### Gate G3 — creator platform readiness

Open supply only after the closed alpha demonstrates sustained buyer demand, useful outcome density, and operational stability. Minimum evidence should include eight consecutive weeks of usage, at least 5,000 attributable invocations, and no unresolved critical isolation or cross-tenant findings.

### Gate G4 — learned-routing readiness

Enable learned routing in shadow mode only when each trained segment has adequate support, temporal holdout performance exceeds V0, predictions are calibrated, and drift/rollback controls exist. A practical starting threshold is 10,000 high-confidence production outcomes overall and at least 500 outcomes in every segment used directly by the model; the data review may raise these thresholds.

---

## 6. Delivery architecture

### 6.1 Phase 0 topology

```mermaid
flowchart LR
    CLI["Experiment CLI"] --> Eval["Evaluation runner"]
    Eval --> Queue["PostgreSQL job queue"]
    Queue --> Worker["Evaluation workers"]
    Worker --> Adapters["Curated capability adapters"]
    Worker --> Grader["Deterministic graders"]
    Worker --> Objects["Object storage"]
    Eval --> DB["PostgreSQL"]
    Grader --> DB
    DB --> Router["Offline V0 router/replay"]
    Router --> Report["Experiment report"]
```

The evaluation runner executes every eligible capability against the same versioned task inputs. The router consumes completed results for train/validation groups and is evaluated against unseen test-group outcomes. There is no production request path in Phase 0.

### 6.2 Phase 1 topology

```mermaid
flowchart LR
    Client["HTTP / MCP / SDK client"] --> API["Intelligence API"]
    API --> Router["Router module"]
    Router --> DB[(PostgreSQL)]
    API --> Jobs["Durable job table"]
    Jobs --> Worker["Invocation worker"]
    Worker --> Provider["Curated provider adapters"]
    Worker --> Artifacts["Object storage"]
    Worker --> DB
    CI["CI / GitHub integration"] --> Outcome["Outcome ingestion module"]
    Outcome --> DB
    API --> OTel["OpenTelemetry collector"]
    Worker --> OTel
    Admin["Internal web console"] --> API
```

Deployables:

- `api`: authentication, route and public run endpoints, control-plane administration, outcome ingestion.
- `worker`: benchmark and production invocation jobs with separate queues and concurrency limits.
- `web`: experiment results and internal operations console.

Logical modules:

- `taxonomy`
- `capabilities`
- `routing`
- `evaluation`
- `runs` (the internal Invocation aggregate over the Registry run ledger)
- `outcomes`
- `providers`
- `artifacts`
- `identity`
- `telemetry`

Modules communicate through typed in-process interfaces and persisted domain events. They must not query another module's tables through ad hoc joins from application code. This preserves future extraction without paying distributed-system costs now.

### 6.3 Extraction triggers

Create an independent network service only when one of these is true:

- it needs a different trust boundary, such as untrusted execution;
- its scaling unit differs by more than an order of magnitude from the API;
- it needs an independent deployment or availability target;
- it has a distinct data-governance boundary;
- measured contention cannot be fixed with worker pools, indexes, or database partitioning.

The likely first extractions are the runtime sandbox and evaluation workers. The router should remain a versioned library/module until independent scaling or release cadence is demonstrated.

### 6.4 Initial repository structure

```text
modall/
├── apps/
│   ├── api/                       # FastAPI application
│   ├── worker/                    # Evaluation and invocation workers
│   └── web/                       # Internal console
├── packages/
│   ├── domain/                    # IDs, enums, domain events, state machines
│   ├── taxonomy/                  # Versioned task ontology and feature extraction
│   ├── manifests/                 # Capability manifest schema and validation
│   ├── routing/                   # Candidate filtering, policies, replay
│   ├── evaluation/                # Suites, graders, statistics
│   ├── providers/                 # Adapter protocol and curated adapters
│   ├── persistence/               # SQLAlchemy models, repositories, migrations
│   ├── telemetry/                 # OTel and structured logging
│   ├── python-sdk/
│   ├── typescript-sdk/
│   └── mcp-server/
├── benchmarks/
│   ├── public/                    # Shareable case definitions
│   └── tooling/                   # Corpus build and validation tools
├── capabilities/                  # Curated adapter configurations/manifests
├── schemas/                       # JSON Schema and generated OpenAPI artifacts
├── infrastructure/
│   ├── local/
│   ├── terraform/
│   └── kubernetes/                # Add when needed; not required for Phase 0
├── docs/
│   ├── adr/
│   ├── runbooks/
│   └── experiments/
└── tests/
    ├── contract/
    ├── integration/
    ├── replay/
    └── end_to_end/
```

Use Python, FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL, and OpenTelemetry as proposed. Use object storage for repository bundles and large results; never place large source archives or raw model transcripts directly in PostgreSQL.

---

## 7. Canonical domain contracts

### 7.1 Identity and versioning

Use sortable opaque IDs, such as UUIDv7 or ULID, for records. Human-readable slugs are mutable aliases and must not be foreign keys.

A routable implementation identity is the tuple:

```text
capability_id
capability_version_id
execution_binding_kind
server_connection_version_id XOR deployment_version_id
source_digest
adapter_version
```

Publishing freezes the capability version, schemas, task claims, price policy, required permissions, and source digest. A `deployment` is a stable operational identity; each immutable `deployment_version` freezes the provider/model revision or local CLI/static-analysis implementation, adapter version, environment, policy, and source/image digest used by an attempt. Operational deployment fields such as current health, capacity, and active version may change, but changes are audited. MCP implementations instead bind to the existing immutable `server_connection_version`; one run/attempt can never carry both target kinds.

### 7.2 Task contract

```json
{
  "taxonomy": "software.code-review.concurrency",
  "taxonomy_version": "1.0.0",
  "intent": "Review this Swift change for concurrency defects",
  "attributes": {
    "language": "swift",
    "platform": "ios",
    "change_size": "medium",
    "context_requirement": "multi_file",
    "toolchain": "swift-6.x",
    "data_classification": "confidential",
    "allowed_execution_zones": ["controlled", "approved_zero_retention"]
  },
  "inputs": {
    "repository_snapshot_uri": "artifact://...",
    "base_revision": "...",
    "head_revision": "...",
    "diff_uri": "artifact://..."
  }
}
```

Persist both the normalized task and the original request. Feature extraction produces an immutable `task_feature_snapshot` tied to an extractor version.

### 7.3 Capability input and output

Every Phase 0 capability receives an immutable repository snapshot, diff, task metadata, execution budget, and trace context. GitHub URLs are never the authoritative benchmark input because their content can change.

The normalized review output is:

```json
{
  "findings": [
    {
      "category": "data_race",
      "severity": "high",
      "revision": "head",
      "diff_side": "right",
      "path": "Sources/App/Store.swift",
      "start_line": 42,
      "end_line": 48,
      "mechanism": "Unsynchronized mutation races with the detached task read.",
      "message": "...",
      "confidence": 0.91
    }
  ],
  "summary": "...",
  "patch": null
}
```

Every finding location and bounded causal `mechanism` explanation is mandatory and revision-aware. `revision` is `base` or `head` and resolves to the corresponding immutable task revision; `diff_side` is respectively `left` or `right`, and `path` plus line coordinates are interpreted in that tree. Deleted-line findings use `base`/`left`; added-line findings use `head`/`right`. Renames and moves use the path on the declared side. Labels use the same convention and also freeze a mechanism ID, required causal/impact facts, and disqualifying contradictions. Adapters that cannot produce a valid location or substantive mechanism return invalid output rather than guessing.

Adapter-specific raw output first enters the Registry Alpha quarantine and classification path. Retain it as an encrypted restricted artifact only when policy allows; ordinary APIs expose only the validated normalized output or a safe quarantine placeholder. Normalization failures count against reliability and are not silently repaired by the platform unless the repair step is declared as part of the capability version.

### 7.4 Routing decision

A routing decision is append-only and includes:

- request and workspace policy IDs;
- taxonomy and feature-extractor versions;
- all discovered candidates;
- filter pass/fail results and reason codes;
- expected quality, cost, latency, and uncertainty for each eligible candidate;
- routing policy and router version;
- selected exact implementation and deterministic tie-break input;
- exploration flag and experiment assignment;
- quote/estimate expiry plus every candidate's immutable provider price-version ID and worst-case reservation vector;
- correlation and trace IDs.

The V0 API response calls its quality field `benchmark_success_estimate` and includes `sample_size`, `confidence_interval`, and `estimate_kind`. Rename it to `success_probability` only after a calibration gate.

### 7.5 Invocation lifecycle

```text
Invocation
accepted -> queued -> preparing -> running
accepted | queued | preparing -> cancelled
accepted | queued | preparing -> execution_failed
running -> fallback_queued -> running
fallback_queued -> cancelled
fallback_queued -> execution_failed | execution_timed_out | indeterminate
running -> execution_succeeded | execution_failed | execution_timed_out
running -> cancelled | indeterminate

Attempt
created -> dispatch_fenced -> awaiting_result
created -> cancelled | failed
dispatch_fenced | awaiting_result -> succeeded | failed | timed_out | cancelled | indeterminate

Outcome (orthogonal to invocation execution state)
not_expected
pending -> finalized | unavailable
```

These Invocation states are internal orchestration phases, not additions to the public `Run.status` enum. The shared API preserves the Registry Alpha enum and projects internal state as follows:

| Internal Invocation state | Public `Run.status` |
|---|---|
| `accepted`, `queued`, `preparing` | `queued` |
| `running`, `fallback_queued` | `running` |
| `execution_succeeded` | `succeeded` |
| `execution_failed` | `failed` |
| `execution_timed_out` | `timed_out` |
| `cancelled` | `cancelled` |
| `indeterminate` | `indeterminate` |

List filters and terminal checks operate on this public projection. Phase 1 detail is exposed only through additive optional reason/phase metadata and the forward-compatible event envelope; it never leaks a new value into the closed Alpha status enum or makes status regress from `running` to `queued` during fallback.

Every transition is an append-only event guarded by an allowed-transition table. The current state is a materialized projection. Duplicate worker delivery must be safe.

The pre-running transition to `execution_failed`, and any corresponding `created -> failed` attempt transition, is legal only for a definitive pre-dispatch failure while no attempt has a dispatch fence, including request-key activation failure, completed security erasure, expired quote, or rejected reservation/eligibility. Security reclassification cannot take that edge until the inherited content and fingerprint states are attested `erased` and non-comparable. Any possibility that a provider send occurred uses the running/attempt evidence rules and may require `indeterminate`; it never uses this shortcut.

An attempt failure or timeout does not by itself authorize fallback. The orchestrator may enter `fallback_queued` and create a child attempt linked by `parent_attempt_id` only when durable evidence proves the prior attempt did not execute, or when every candidate in the fallback chain honors the same tested end-to-end idempotency key for the external operation. A failure response alone is not proof of non-execution. Any post-fence timeout or lost response with uncertain acceptance moves the invocation to `indeterminate` with `reconciliation_required`; it never creates a fallback attempt. Recheck the execution-disposition guard and candidate eligibility after entering `fallback_queued` and before creating the child attempt. If no eligible candidate remains or the chain is exhausted, transition to `execution_failed` or `execution_timed_out` according to the last definitive attempt and record `fallback_unavailable` or `fallback_exhausted`; if the prior disposition has become unknown, transition to `indeterminate` with reconciliation required. No path may remain terminally parked in `fallback_queued`.

Execution status and outcome status are separate projections. Execution reaches a terminal state regardless of whether downstream outcome evidence arrives. At execution completion, an eligible invocation receives an outcome record with `pending` plus a fixed `evidence_due_at`; it transitions to `finalized` when adequate evidence arrives or `unavailable` when the deadline expires. An ineligible invocation receives terminal `not_expected`. Late evidence creates a superseding outcome version without reopening execution state.

Before any provider network send, persist `dispatch_fenced` in the same transaction that records the attempt ownership. Record an evidence-backed execution disposition of `not_executed`, `executed`, or `unknown` separately from transport status. A recovered `created` attempt is safe to dispatch. A recovered `dispatch_fenced` or `awaiting_result` attempt without a durable terminal response has disposition `unknown` and must not be sent again or fall back: persist any available provider receipt and move it to `indeterminate` with `reconciliation_required`. This deliberately prefers manual reconciliation to duplicating an external side effect.

Cancellation is a request event, not proof of execution state. A `created` attempt may become `cancelled` before its dispatch fence. After fencing, emit `cancellation_requested` and propagate best effort, but allow `cancelled` only when definitive provider evidence proves that execution did not occur or was fully rolled back. Otherwise keep awaiting a definitive terminal response; an unsupported request, lost acknowledgement, or deadline with uncertain external execution becomes `indeterminate` with `reconciliation_required`. The parent invocation can be `cancelled` only when every attempt has a definitive non-execution/cancelled disposition.

### 7.6 Outcome contract and truth hierarchy

Outcome evidence is ranked:

1. Reproducible hidden tests, build, or static checks tied to the returned artifact
2. Signed CI/GitHub integration events
3. Downstream automation with invocation correlation
4. Buyer or agent explicit report
5. Passive signals such as no retry

Store evidence separately from the derived label. An outcome label contains labeler/version, confidence, evidence IDs, timestamp, and supersession lineage. Conflicting evidence produces `disputed`, not a silent overwrite. Provider self-reports cannot create high-confidence success labels.

---

## 8. Evaluation and router-calibration specification

### 8.1 Hypothesis

Swift concurrency-review implementations have heterogeneous performance across observable task characteristics, and a deterministic router using only pre-execution features can exploit that heterogeneity on unseen tasks.

### 8.2 Budget-calibration pilot

Before producing the official train/validation/test matrix, run every candidate on a separate set of 20 representative non-confidential PRs under generous emergency safety limits. These PRs are permanently excluded from official benchmark splits and routing-lift calculations. Raw internal PRs cannot enter this pilot because no third-party provider is currently approved to process private source.

The pilot measures complete end-to-end cost, latency, context volume, output volume, tool usage, timeout behavior, and successful-review rates. Use it to establish:

- the global maximum allowed cost per review;
- the global wall-clock deadline;
- controlled-cohort token and tool budgets;
- capability-specific concurrency and resource limits;
- output and artifact size limits;
- the definition of a billable/chargeable failed attempt.

For the hidden experiment, set cost and latency ceilings to the observed p95 of successful pilot runs for the strongest-quality candidate plus 20% headroom, unless that would exceed a separately approved safety ceiling. Apply the resulting global ceilings to every candidate. Record exclusions caused by the ceilings as failures or ineligibility according to the preregistered protocol; do not silently increase limits after viewing hidden results.

### 8.3 Corpus

Start with a 75–100-case universally eligible, non-confidential calibration suite to calibrate graders, candidate behavior, feature extraction, and the static production policy. This suite has its own grouped split:

- 60% training
- 20% validation
- 20% locked calibration test

The calibration test remains hidden until the initial report, then is permanently marked revealed and cannot contribute to the G1 activation holdout.

Continue expanding to a separate activation benchmark while the router alpha is built, using 200–300 cases as the first authoring tranche rather than a fixed cap. Before allocating groups, preregister the complete G1 joint-pass simulation defined in the gate: use the exact paired composite-cluster bootstrap, planned `correlation_cluster_id` distribution, conservative calibration-derived discordance, joint pilot success/cost behavior, default eight-point/1.10 planning alternative, maximum activation attempts, and separate success/cost alpha-spending schedules. For each attempt, choose a wholly fresh holdout whose conservative joint-pass probability has a one-sided Monte Carlo lower bound of at least 90% under that attempt's allocations. Freeze its newly added clustered cases and labels before final policy selection; do not run candidates on them or reveal any artifact until the static baseline and task-aware policy are frozen. An undersized attempt may expand beyond 300 total cases only while every component remains hidden. At the first reveal, permanently mark the attempt's components `g1_revealed`; they may enter a later training/reporting pool with provenance but can never be extended, pooled into a later attempt, or reused for activation. No activation component may connect to any previously revealed case. An underpowered, cost-imprecise, or joint-underpowered attempt cannot activate task-aware routing.

Split by the frozen composite correlation components, not by individual diff, repository alone, or archetype alone, so same-repository and cross-repository lineage variants cannot cross groups. Keep the test labels encrypted or access-controlled from capability authors and router development.

Each case contains:

- immutable source snapshot and license/provenance;
- immutable `source_repository_id`, nullable controlled-mutation/archetype-lineage ID, frozen `correlation_cluster_id`, and cluster-graph version;
- base/head revisions and canonical diff;
- zero or more labeled concurrency defects plus an explicit `clean` or `defective` case kind;
- allowed category aliases and span tolerances;
- case-specific false-positive limit, with clean cases defaulting to zero;
- task dimensions computable before execution;
- pinned Swift toolchain and environment image/runner label;
- public setup metadata and private grading metadata;
- expected runtime class and resource limits;
- contamination and duplication notes.

Use realistic iOS-shaped repositories and PRs, including UIKit and SwiftUI code. Source-level defect detection is the primary evaluation path and must be reproducible without compiling the app. Maintain a buildable Swift Package Manager subset for analyzers that benefit from compiler context. Any Xcode build/test evidence runs on pinned macOS images as a separate stratum and is reported independently from the primary routing gate.

Target this case composition:

- 25% clean PRs with no labeled concurrency defect;
- 45% single-defect PRs;
- 30% multi-defect, cross-file, or context-dependent PRs.

The clean controls are mandatory because a reviewer that flags every concurrency pattern must not score well. Defect families should include actor isolation, `Sendable` violations, unsafe shared state, task cancellation, continuation misuse, main-actor violations, reentrancy, and detached-task misuse.

Use two separate data strata.

**Primary universal-eligibility corpus**

- 55% licensed public iOS repositories and controlled mutations of them;
- 30% purpose-built realistic iOS fixtures;
- 15% defect patterns re-authored from internal experience only when a data owner confirms that the resulting code is non-confidential and cannot reconstruct the original source.

Every candidate in both cohorts must be eligible for every case in this corpus. The initial Gate G1 is calculated only on a fresh activation holdout whose frozen `evaluation_stratum_id` is universal, non-confidential, and matches the represented task/provider/candidate scope. The initial 75–100 cases support calibration and shadow routing; their revealed test cases never count as unseen G1 evidence. The expanded, adequately powered activation benchmark can authorize live task-aware routing only for matching universal requests.

**Private generalization corpus**

- during Phase 0, inventory and label approximately 20 representative historical PRs from internally owned repositories to validate taxonomy and grading realism;
- do not send their code, diffs, embeddings, or derived source artifacts to third-party model providers;
- CPU-only deterministic tooling may be exercised inside controlled machines for harness validation, but those results are not evidence of routing lift;
- after initial calibration and once secure model execution becomes available, expand toward 50–100 verified historical PRs and controlled mutations;
- execute the expanded corpus solely with eligible controlled or contractually approved capabilities;
- report results separately and never aggregate them into the universal candidate comparison;
- keep private/internal live requests on their strongest eligible static policy and task-aware shadowing until a wholly fresh, representative private/internal activation holdout passes its own scoped G1 family.

Source mix and defect composition are independent dimensions. Private snapshots remain denied to every third-party provider because no processing agreement is currently in place. If approved no-training and zero-retention agreements are established later, eligibility changes apply only to new evaluation runs and never rewrite historical candidate sets.

### 8.4 Initial capability set

Test two explicit cohorts under the same task input and normalized finding-output contracts.

**Cohort A — controlled model baselines**

1. OpenAI `gpt-5.6-sol` through the Responses API
2. Anthropic `claude-opus-5` through the Messages API
3. Google `gemini-3.8-flash` through the Gemini API

OpenAI and Anthropic API access and billing are confirmed. Gemini API access must be established before the budget-calibration pilot. These selections are frozen as of September 2, 2026, subject only to an availability check before pilot execution. If a selected model becomes unavailable, choose and document a replacement before any official matrix run; never substitute a model silently or midway through a benchmark version.

These three candidates use the same semantic system prompt, context assembly, tool contract, retry policy, maximum output, and output normalizer. Provider wrappers may translate message and tool schemas but cannot add hidden prompting or repair stages. Use a preregistered provider-native reasoning tier and omit sampling parameters that a provider does not support; do not pretend unlike reasoning controls are numerically equivalent. Enforce the same pilot-derived cost and wall-clock ceilings and record actual input, reasoning, cached, and output usage where exposed. This cohort measures model-selection lift while minimizing workflow confounding.

**Cohort B — differentiated capabilities**

4. Codex CLI reviewer with pinned client, configuration, tools, and model selection
5. Claude Code reviewer with pinned client, configuration, tools, and model selection
6. Deterministic SwiftSyntax-based concurrency analyzer
7. SwiftSyntax/static-analysis plus model hybrid reviewer
8. Specialized Swift-concurrency review agent

Differentiated candidates may use distinct prompts, tools, stages, and runtimes, but must receive equivalent authoritative task inputs and return the shared normalized schema. Their declared version includes every workflow dependency. Total end-to-end cost and latency include all internal stages and model calls.

Grok, Muse, multi-agent orchestration, and Tangle are explicitly deferred from the initial matrix. They may enter as post-calibration challengers through the same adapter contract and evaluation process; none is a dependency of the closed alpha.

The primary router selects across both cohorts. The strongest static baseline is the best single candidate from either cohort, selected on the applicable validation data and frozen before each hidden test. The experiment report must also show Cohort A alone, Cohort B alone, and the combined set so model-routing lift and full capability-routing lift are distinguishable.

The preceding comparison and its initial G1 authorization apply only to the universal-eligibility corpus. Model-based routing on the private generalization corpus is deferred until controlled GPU infrastructure or approved provider processing is available. Those prerequisites do not transfer universal G1 evidence: private/internal task-aware activation still requires a separately sized, fresh, representative holdout with a preregistered slice of the system-wide alpha family. CPU-only deterministic runs are harness diagnostics, not a substitute for a multi-candidate routing experiment.

Pin model identifiers, prompts, tool definitions, supported sampling/reasoning configuration or explicit omission, maximum output, adapter code, and provider routing settings. An official comparison candidate must expose one immutable model revision or provider-attested deployment revision that remains identical across its entire randomized matrix. A provider alias plus timestamps is insufficient: such a candidate is diagnostic-only and excluded from static-baseline selection, routing training, G1, and official cost/quality comparisons. If an attested revision changes mid-matrix, invalidate that candidate's results for the benchmark version and rerun its full matrix only after freezing a new candidate version; never combine revisions in one estimate.

Before execution, assign every candidate/task pair a primary attempt slot and randomize matrix execution order. G1, policy comparisons, task success, and gated cost/latency use exactly that primary attempt: success is the deterministic grader result for the slot, while timeout, failed, invalid, or missing execution counts as failure under the preregistered exclusion rules and its incurred cost remains charged. A policy's per-task outcome is the primary result of the capability it selected; when two policies select the same capability they reuse the same primary result. This produces exactly one paired outcome and one cost/latency observation per policy/task.

Run stochastic capabilities in two additional diagnostic slots, for three attempts total; deterministic capabilities may remain at one after the pilot verifies their determinism. Diagnostic repeats estimate within-capability variance and reliability using nested models, but they never vote, average, replace a failed primary, change policy selection, or enter the G1 activation estimand or its cost denominator. Report diagnostic execution cost separately. The protocol freezes slot assignment, failure handling, and aggregation before hidden execution, and the power simulation uses this same primary-attempt Bernoulli estimand.

### 8.5 Grading

Match findings to labels using category compatibility, declared base/head revision and diff side, file identity in that revision, configured line-span overlap, and semantic correctness against the label's frozen mechanism checklist. Reject inconsistent revision/side pairs before scoring. Location/category creates only a provisional pair; it earns label credit only after its explanation asserts the required causal/impact facts and contains no disqualifying contradiction. Record:

- weighted defect recall;
- precision and false-positive count;
- critical-defect recall;
- schema validity;
- tool/runtime failure;
- wall-clock latency;
- provider-reported and platform-calculated cost;
- optional patch build/test result.

A task-level binary success should be fixed before the hidden run. Defective and clean cases use separate, total definitions so recall is never evaluated with a zero denominator.

For a defective case:

```text
all critical defects found
AND weighted recall >= 0.80
AND no more than the case-specific false-positive limit
AND output schema valid
AND no execution failure
```

For a clean case:

```text
false-positive count <= the case-specific limit (default 0)
AND output schema valid
AND no execution failure
```

A false positive is a reportable candidate finding that does not receive both structural and semantic match credit after the frozen matching and blinded adjudication rules below. A broad-category/span overlap with a vacuous, causally wrong, or contradictory mechanism is a false positive and leaves the label missed. Weighted and critical-defect recall are recorded as not applicable for clean cases, not as zero or one. Aggregate recall is calculated over defective cases; the overall task-success rate includes both clean and defective cases.

Before hidden execution, freeze the per-label semantic checklist, explanation-completeness threshold, reviewer form, tie rule, and one-to-one pairing algorithm. Strip candidate/provider/policy identity from every provisional match and mix in blinded correct, vacuous, and category-correct-but-mechanism-wrong controls. For calibration, the primary reviewer evaluates every provisional pair and the second reviewer independently covers the preregistered stratified sample plus every high/critical or uncertain pair. For a G1 activation attempt, two qualified reviewers independently judge every provisional pair and every substantive unmatched finding; disagreement receives no credit until a third blinded adjudicator resolves it. No learned semantic judge may award activation credit unless separately validated and preregistered, and human reviewers remain blinded to aggregate policy results.

Send every substantive unmatched finding, including findings in nominally clean cases, to a label-gap queue before final scoring. Define `substantive` before hidden evaluation using candidate-blind minimum severity, confidence, location validity, and explanation-completeness rules. Strip candidate, provider, policy, and aggregate-result identity; mix findings with blinded negative controls; and have two qualified reviewers independently decide whether each is a genuine rubric-covered defect using only the frozen source and rubric. A rejected finding becomes a false positive. For calibration/training data, a genuine omitted defect marks the case `label_incomplete`; never patch frozen labels in place, and publish a new benchmark version with the added label before uniformly rescoring immutable outputs or rerunning candidates.

For any G1 activation attempt whose candidate output has been revealed, one genuine omitted defect or unresolved material label dispute makes the entire attempt inconclusive—not merely that case. Immediately mark every component in that attempt `g1_revealed`, retire the complete holdout from activation, and prohibit subset exclusion, remaining-sample power recomputation, extension, pooling, or corrected-output rescoring for any activation claim. The revealed attempt remains available only for transparent reporting or later training with provenance. After correcting and freezing a new benchmark version, a later attempt must consume its preregistered remaining alpha allocation and use a wholly fresh representative holdout with no connected component from any revealed attempt. Report queue frequency, controls, decisions, whole-attempt retirements, and version lineage.

The project owner is the primary Swift concurrency reviewer and labels every case against the frozen rubric. During pre-freeze label authoring and calibration, a second qualified reviewer—blinded to candidate identity and the primary label—covers a stratified 20% sample across clean/single/complex cases and defect families plus every disputed high/critical finding. During G1 scoring, that reviewer instead covers every provisional semantic match and substantive unmatched finding as required above. A third blinded reviewer resolves material disagreement; after reveal, an unresolved label dispute retires the whole G1 attempt. The primary reviewer cannot unilaterally break ties after seeing candidate outputs.

### 8.6 V0 routing

Candidate filtering is hard and ordered:

1. exact taxonomy support;
2. compatible language, platform, toolchain, input, and output schemas;
3. permission and data-handling compatibility;
4. healthy deployment and capacity;
5. price and latency ceilings;
6. minimum evidence threshold.

For sparse feature buckets, estimate quality with empirical-Bayes shrinkage toward the capability's global result rather than using raw small-sample rates. Phase 0 uses lexicographic quality-first ranking:

```text
1. reject candidates below the validation-derived critical-recall floor;
2. reject candidates above the validation-derived false-positive ceiling;
3. rank by uncertainty-adjusted task-specific quality estimate;
4. when candidates are within a preregistered quality-equivalence band, prefer lower cost;
5. use latency and deterministic implementation identity as final tie-breakers.
```

Cost cannot compensate for a material quality deficit. Normalize cost and latency only for the equivalence-band tie-break and for secondary `best_value` analysis. That secondary policy may rank using:

```text
utility = quality_weight * quality_estimate
        - cost_weight * normalized_cost
        - latency_weight * normalized_latency
        - risk_weight * uncertainty_penalty
```

Quality floors, uncertainty penalties, the equivalence band, secondary-policy weights, priors, minimum sample sizes, missing-value behavior, and tie-breaking are configuration under an immutable router version. The final test run may not tune them.

Required baselines:

- strongest static capability selected on validation data;
- cheapest eligible capability;
- highest global validation success rate;
- oracle upper bound that selects the successful candidate after observing results—reported only to measure routing headroom;
- random eligible candidate as a diagnostic.

### 8.7 Analysis

Report:

- task success and paired difference versus every baseline;
- cost per successful task for every policy, the observed task-aware/static ratio, and its one-sided composite-correlation-cluster bootstrap upper bound at the preregistered attempt-specific confidence level;
- latency per successful task;
- capability failure and invalid-output rates;
- selection share by capability and feature segment;
- controlled-model, differentiated-capability, and combined-cohort routing results;
- an ablation showing the incremental lift from adding differentiated capabilities to the controlled model set;
- universal and private-stratum results reported separately, with candidate eligibility and live/static/shadow authorization made explicit;
- `evaluation_stratum_id`, G1 attempt ID, above-threshold planning alternative, simulated complete-gate joint pass probability and Monte Carlo bound, stratum-specific maximum attempts, success/cost alpha allocations and cumulative spend, frozen policy/candidate versions, retired `g1_revealed` component IDs, paired bootstrap success interval, and cost-ratio upper bound grouped by frozen `correlation_cluster_id` using exactly one primary outcome and attributed cost per policy/task, plus separately labeled nested-repeat variability intervals that cannot affect activation;
- sensitivity to policy weights and missing features;
- oracle headroom;
- train/validation/test divergence;
- all excluded or disputed cases with reasons.

The final report is generated from committed result snapshots and a versioned analysis program. A second engineer must reproduce it from scratch.

### 8.8 Phase 0 work breakdown

| Epic | Deliverable | Acceptance criteria | Owner profile |
|---|---|---|---|
| P0-01 Protocol | Preregistered calibration and stratum-scoped activation gate | Immutable classification/source/task/provider/candidate `evaluation_stratum_id`; metrics, composite clusters, rating units, pairing, cost/UCB, boundary rules, per-stratum attempts within system-wide alpha schedules, permanent post-reveal retirement, and statistics approved before hidden runs | Staff/data |
| P0-02 Taxonomy | Versioned task and feature schema | JSON Schema validation; feature provenance; no post-outcome fields | Backend/domain |
| P0-03 Corpus | 75–100-case calibration suite plus fresh per-attempt representative activation holdouts whose first universal tranche is 200–300 cases and final size follows the complete joint-gate simulation | Labels freeze causal-mechanism checklists; stratum/component graph is audited; any post-reveal label gap retires the whole attempt; each holdout has >=90% conservative joint-pass probability and cannot authorize unrepresented/private traffic | Swift/evaluation |
| P0-04 Harness | Durable execution and restricted artifact capture | Resumable, idempotent matrix runs; exclusive exact MCP-connection/provider-deployment binding; evaluation-only visibility domain enforced below the repository layer; hidden inputs/outputs absent from ordinary run/artifact APIs; preregistered primary/diagnostic attempt slots; pinned environment; per-attempt cost/latency | Platform |
| P0-04A Budget pilot | Separate 20-PR candidate matrix and frozen limits | Pilot cases excluded from official splits; cost/latency/resource ceilings approved before official matrix | Platform/product |
| P0-05 Adapter SDK | Common adapter protocol and eight candidates | Official candidates attest one revision across the full matrix; aliases are diagnostic-only; contract suite requires revision-aware findings and safe normalized outputs | Backend/AI |
| P0-06 Graders | Structural pairing, blinded mechanism-correctness scoring, and label-gap adjudication | Golden tests reject vacuous/wrong-mechanism category/span matches; every G1 provisional match is dual reviewed; a genuine post-reveal gap retires the full holdout; clean/lineage/false-positive rules and rerun agreement >=98% pass | Evaluation |
| P0-07 Router V0 | Filter/rank/reason implementation | Deterministic replay from snapshots; no hidden data access | Backend/data |
| P0-08 Analysis | Baseline comparison, joint power, and stratum-scoped sequential confidence bounds | Reproducible report; sizing simulates the complete gate with a >=90% Monte Carlo lower bound; activation uses the same paired bootstrap and cross-stratum alpha registry; no component/evidence is reused across attempts or strata | Data/full-stack |
| P0-09 Policy decision | Written per-stratum static-versus-task-aware release review | Exact activated strata/evidence, static/shadow fallbacks for every other stratum, limitations, and policy recommendation signed off | Tech/product leads |

### 8.9 Post-registry Phase 0 schedule

This schedule begins only after the MCP Registry Alpha release gate passes. With the assumed follow-on team, target three elapsed weeks for initial calibration while the post-registry Phase 1 routing foundations start in parallel:

- **Post-registry Week 1:** ADRs, contracts, corpus rubric, harness skeleton, extensions to the existing API/persistence foundation
- **Post-registry Week 2:** first adapters and graders, 20-PR budget pilot, frozen execution limits, first 40–50 cases
- **Post-registry Week 3:** all eight adapters, 75–100-case initial matrix, strongest static policy, router shadow policy, reproducible report

After Week 3, activation-benchmark authoring and shadow evaluation continue as an evaluation workstream alongside productization. Treat 200–300 universal cases as an initial tranche and, before any reveal, run the preregistered complete joint-gate simulation for that attempt's alpha allocations and above-threshold planning alternative; expand only while the holdout is untouched until the conservative joint-pass probability reaches 90%. Pass every G1 threshold on one wholly fresh attempt before task-aware selection controls live traffic in that exact `evaluation_stratum_id`. A revealed attempt is permanently retired from activation, and any allowed later attempt consumes that stratum's remaining preregistered alpha and uses new correlation components. Private/internal and any other unrepresented strata remain static/shadow-only pending their own holdout; do not reuse or pool evidence across strata or weaken clustering, power, cost uncertainty, label quality, or reproducibility requirements.

---

## 9. Phase 1 closed-alpha implementation

Begin Phase 1 routing foundations in post-registry Week 1 rather than waiting for G1. This work reuses the Registry Alpha identity, capability, invocation, artifact, job, and audit modules. Target router closed-alpha readiness eight to ten weeks after the Registry Alpha gate, with evaluation and routing streams running in parallel. A stratum-specific G1 determines whether matching requests use task-aware selection or their strongest eligible static policy; G2 determines whether the alpha is operationally safe to release.

### 9.1 API and identity

Phase 1 inherits the Registry Alpha authorization-freshness contract without resetting any timer: the conservative provider propagation bound `P`, qualified-source lookup allowance `R`, server snapshot TTL/poll interval `L`, and safety margin `M` must satisfy `P + R + L + M <= 60 seconds`. Privileged mutations, artifact grant mint/redemption, and every `read_artifact` chunk synchronously bypass the shared authorization cache and require `P + R + M <= 60 seconds`; timeout or incomplete source data fails closed. Server-authored snapshot expiry propagates to clients so browser caching cannot add another freshness interval.

Endpoints:

- `POST /v1/artifact-uploads` — authorize metadata and return one dedicated short-lived `EphemeralUploadTarget`, scoped to a unique create-only object key or storage version
- `POST /v1/artifact-uploads/{id}/complete` — under the upload-row lock, require server time strictly before `expires_at`, close the upload, verify the exact storage version's ephemeral client integrity proof, size, content type, and scan status, then persist kind-appropriate integrity metadata and mint the authoritative `artifact://` URI; an expired `open` row is atomically moved to `cleanup_pending` and returns no artifact even if the sweeper is late
- `GET /v1/artifacts/{id}` — return authorized metadata, readiness, exact immutable storage-version identity, and a clean-content digest only when policy permits, never a restricted plaintext fingerprint or ambient object-store credential
- `POST /v1/artifacts/{id}/access-grants` — authorize the current subject, workspace, classification, and retention state, then mint the sealed Alpha `ArtifactAccessGrantToken` bound to that subject and exact artifact version; ordinary replay storage retains only a non-secret reference/HMAC verifier, while the encrypted token envelope remains replayable through exact grant expiry and becomes logically unreadable then
- `POST /v1/artifact-access-grants/{grant_id}/revoke` — append the inherited idempotent grant-revocation event and advance its active/revoked/expired projection; revoke suppresses token replay immediately and schedules envelope erasure
- `GET /v1/artifacts/{id}/content` — require ordinary authentication plus the subject/workspace/version/authorization-epoch-bound grant in a redacted header and, on every redemption, reauthorize current membership/role, epoch, visibility, classification, retention, scan, and integrity before streaming through the isolated no-store path
- `POST /v1/routes` — create and persist a route-only decision
- `POST /v1/routed-runs` — atomically route, authorize, create the canonical Run resource, enqueue, and return `202 Accepted`
- `GET /v1/runs/{id}` — current state and normalized result metadata for either a direct Registry run or routed run
- `POST /v1/runs/{id}/cancel` — best-effort cancellation
- `POST /v1/runs/{id}/outcomes` — attributable outcome evidence
- `GET /v1/capabilities/{id}/versions/{version}` — exact public/authorized metadata

Generate OpenAPI and SDK types from one schema source. Require an `Idempotency-Key` for mutating client calls. After bounded input scanning, retain the workspace-keyed HMAC tombstone used to locate that client key for the workspace lifetime. Ordinary non-content mutations may retain a separately domain-separated workspace-keyed request fingerprint while replay is supported; run arguments instead use the Registry Alpha unique per-run erasable fingerprint subkey. While a clean ordinary response exists, the same key/fingerprint returns it. Once run content is reclassified, its encryption and fingerprint subkeys are destroyed, active state becomes a generic non-comparable no-replay tombstone, and every same-key request fails without testing candidate plaintext; the lookup HMAC alone prevents re-execution. Every credential-bearing response inherits Registry Alpha's recoverable envelope protocol: durably prepare the encrypted fixed-expiry envelope before database commit, make it logically readable through the credential's exact immutable expiry but never after it, atomically persist only its non-secret resource/reference plus an activation outbox with the domain mutation, then idempotently promote and return the same envelope after commit. The backing store may delete later but never earlier. Rollback/uniqueness-loser envelopes remain inaccessible and expire, while a committed missing/prematurely evicted envelope fails closed without reminting. After response/envelope expiry, replay fails with `idempotency_replay_expired`; a different comparable fingerprint conflicts, and a non-comparable run tombstone always rejects, so delayed retry never executes again. Lookup tries current and retained retired idempotency-key versions; older lookup keys remain encrypted/non-retirable until protected workspaces are hard-deleted, while destroyed per-run fingerprint keys are never retained. Scope API credentials to workspace, environment, action, and optional spending/quota policy. All errors use stable machine codes and correlation IDs.

`EphemeralUploadTarget` is a sealed outbound secret-capability type, not a `SafeUrl` or an ordinary URL-valued API field. It contains an upload ID, method, credential-bearing target, required headers or form fields, expiry, maximum bytes, content/checksum constraints, and no read/list authority. The service mints it only from a configured and validated object-store origin, for an exact non-overwritable key/version, with a maximum five-minute TTL and no redirects. It appears only in the authorized creation response or a same-key/same-fingerprint idempotency replay, always with `Cache-Control: no-store`; its encrypted exact-lifetime replay envelope uses the inherited prepare/database-commit/promotion protocol, lives only in the dedicated secret store, remains available through the target's immutable expiry, becomes logically unreadable at that instant, and leaves a non-secret tombstone so later replay fails closed without reminting. Physical deletion may finish later but may never occur earlier. A backend qualifies only if it supplies a conservative maximum post-expiry in-flight materialization window `W` plus read-after-write/list visibility bound `C`, supports an expiry write fence that rejects new starts, and guarantees `W + C` plus the cleanup retry allowance fits the 15-minute deletion SLO. The fence may be an object-store-side cancellable upload session or a bounded upload gateway that durably registers each active write lease before accepting bytes and enforces a hard transfer deadline; an ordinary presigned PUT with no cancellable session, enforceable duration, or bounded visibility does not qualify.

Ordinary durable records retain only the opaque upload ID, safe origin identifier, never-reused key, expected/provider-assigned version scope, lifecycle state, expiry, qualified `W`/`C`, write-fence/active-lease evidence, cleanup attempts, quiescence deadline, and deletion attestation. A provider event or service-side HEAD restricted to that key records the actual version/generation even when the client never calls completion; the create-only policy permits at most one successful object version under the key. Completion and expiry cleanup lock the same upload row and compare database server time to `expires_at`. Completion may finalize only an `open` row with `now < expires_at`; if `now >= expires_at`, it atomically performs or preserves `open -> cleanup_pending`, enqueues/continues cleanup, and returns `upload_expired` without publishing an artifact. The sweeper uses the same transition, makes the upload permanently non-completable, activates the provider-side write fence, and aborts multipart state. It then repeatedly observes and deletes the exact version/key while any registered write lease exists and through at least `expires_at + W + C`; an object that becomes visible during this interval is deleted and restarts the bounded consistency verification. The row reaches `deleted` only after the write fence is attested, every lease/request is closed or past its enforced deadline, the quiescence deadline has passed, and a final provider-consistent absence/deletion check succeeds. A late provider event after terminal attestation is an invariant breach that triggers immediate delete plus an incident, not an ignored orphan. Object-store failure or uncertain quiescence leaves `cleanup_pending`, retries with alerting, and retains metadata until deletion is proven. A committed finalized artifact can never enter this cleanup path. Cleanup starts at target expiry and backend qualification ensures terminal deletion remains within the 15-minute SLO. Application, proxy, CDN, browser, and MCP-host telemetry/body capture is disabled or credential-redacted; object-store audit configuration records only approved safe identifiers and redacts query, form, and header credential fields. Browser clients pass the object directly to a non-navigating upload primitive with no ambient cookies or referrer, an exact-origin `connect-src` allowlist, and immediate in-memory disposal. MCP clients may receive it only through a host-supported non-recording secret-result channel wired directly to the transfer primitive; if that capability is unavailable, `create_artifact_upload` fails closed and the client must use the authenticated HTTP/SDK upload path. Together with the inherited `ArtifactAccessGrantToken`, it is one of only two credential-bearing response types; the upload credential can never enter a generic URL parser, DOM, model transcript, resource-link/result surface, application database, audit payload, error, history, analytics, log, or trace.

`Run` is the sole public production execution resource and maps one-to-one to the internal Invocation aggregate. Registry Alpha's plural `/v1/runs` family remains backward compatible for direct capability execution; Phase 1 adds `/v1/routed-runs` only as a creation command and returns the same Run schema and ID. Phase 0 evaluation executions reuse the physical run/attempt/event ledger and status schema but are not public Run resources: the ordinary `/v1/runs` list/get/event/cancel/outcome paths and artifact grant/read paths deny `evaluation_restricted` rows below the repository layer, including to ordinary workspace Admins. Dedicated internal evaluation APIs use separate service identities and explicit campaign/stage-scoped `EvaluationRunner`, `EvaluationReviewer`, or `EvaluationOperator` grants; adapter-development identities are incompatible with those grants, and hidden-holdout assets/results remain inaccessible except to the runner and preregistered blinded reviewers. The previously planned singular `/v1/run` and `/v1/invocations/*` paths are replaced before implementation and never ship as aliases. Generated OpenAPI/SDK mappings are `createRun` for direct execution, `createRoutedRun` for routed execution, and `getRun`, `cancelRun`, and `createRunOutcome` for the shared public resource paths. The MCP meta-tool name `get_invocation` is a protocol compatibility name that calls `getRun`; it does not create a second HTTP resource. Contract tests reject accidental legacy routes, compile both existing direct-run and new routed-run clients against the checked schema, and prove evaluation IDs cannot be enumerated or dereferenced through them.

The shared `Run.status` representation remains exactly `queued|running|succeeded|failed|timed_out|cancelled|indeterminate` for direct, evaluation, and routed rows, while evaluation rows remain internal-only under the visibility boundary above. Phase 1 internal states use the projection in Section 7.5, and public run-event types use the Alpha forward-compatible known-or-unknown string representation so new routed/fallback details cannot break an already generated Alpha client.

Routing and run requests accept only finalized, unexpired artifacts owned by the same workspace and allowed by the request/provider data policy. Each upload uses a unique non-overwritable object key or versioned-bucket write. Completion closes the upload, verifies an ephemeral client integrity proof and length against the exact storage version, detects archive expansion/path traversal, and applies the Registry Alpha malware/type plus per-MIME metadata/embedded-content extraction, all-page/frame render/OCR, secret/PII classification, and complete-coverage policy; a type without a complete tested profile remains quarantined. Clean artifacts persist a content digest; encrypted restricted artifacts persist a ciphertext integrity checksum plus a purpose-separated versioned HMAC plaintext fingerprint whose key remains in the secret manager, never an ordinary plaintext digest. Consumption reauthorizes the artifact and reads only that pinned version, verifying its kind-appropriate integrity value; a still-valid upload credential cannot replace finalized content. HTTP/SDK clients upload through the `EphemeralUploadTarget`; the MCP facade exposes both `create_artifact_upload` and `complete_artifact_upload`, with target delivery allowed only through the non-recording transfer channel above and completion returning the authoritative `artifact://` URI so an eligible MCP control-plane client needs no REST credential. Large or retained non-text results use the same immutable artifact and authorized-access contract, so the console never reads object storage directly.

Result classification also inherits the Registry Alpha independent-output rule: `effective_output_classification` is the maximum of the request/input class, immutable connection or deployment minimum output class, and detector/content-profile class. Detector-clean proprietary output from a capability with confidential data access therefore remains restricted and can never receive an ordinary digest or inline/public result representation; missing/ambiguous minimum output class blocks dispatch.

For result consumption without REST credentials, `read_artifact(artifact_uri, offset, max_bytes)` uses the authenticated MCP session to obtain a qualified current authorization snapshot and recheck subject, workspace membership/role, authorization epoch, artifact visibility, classification, retention, scan status, and exact immutable version/kind-appropriate integrity state on every call. It returns at most 256 KiB per chunk with total length, detected content type, a clean-content digest only when policy permits, and the next offset; restricted plaintext fingerprints remain internal. Safe text/JSON uses typed content, and only explicitly allowlisted bytes that completed their full content-aware profile use base64. Repeated bounded calls can consume a large result, while stale or incomplete authorization, active, encrypted/uninspectable, unknown, unsupported, type-mismatched, coverage-incomplete, quarantined, expired, cross-workspace, integrity-failed, or policy-forbidden content fails closed. The tool never returns an ambient object-store URL or credential.

Acceptance criteria:

- identical idempotent requests produce one route/invocation;
- cross-workspace access tests fail closed;
- unfinalized, expired, overwritten, wrong-version, integrity-mismatched, unsafe, and cross-workspace artifacts are rejected before routing, enqueue, viewing, or download;
- an incomplete upload becomes permanently non-completable at expiry and its exact object version or multipart state is verified deleted within 15 minutes; completion checks database server time under the same row lock, so an expired `open` row enters/stays on cleanup even when completion beats a delayed sweeper; completion/cleanup and late-materialization races have one safe winner, and cleanup failures remain retryable, visible, and alerted without touching finalized artifacts;
- a currently authorized subject can view safe text/JSON or download other content only through an active, unexpired subject/workspace/version/epoch-bound grant and required isolation headers; an individual grant-revocation event/projection or post-mint access/policy/retention change denies token replay and bytes immediately on the next authoritative check;
- an eligible MCP control-plane client can create an upload, transfer bytes through a host-enforced non-recording channel to its bounded `EphemeralUploadTarget`, call `complete_artifact_upload`, and use the returned authoritative URI without REST credentials; an ineligible host fails closed without disclosing the target;
- an MCP-only result client can consume safe text/JSON or bounded base64 chunks through `read_artifact` using only its MCP session, with the same authorization and integrity decisions as HTTP retrieval;
- p95 route-only latency is under 300 ms with 100 curated versions, excluding external task-artifact upload;
- compatibility and constraint failures expose safe reason codes without private provider data.

### 9.2 Routing service module

Implement taxonomy classification, feature extraction, structured candidate retrieval, hard filters, V0 policy ranking, reason codes, and fallback planning. Explicit taxonomy supplied by an authorized client takes precedence; free-text classification returns confidence and may require clarification rather than inventing a task type.

Acceptance criteria:

- every response references an immutable router version and feature snapshot;
- every response records the current immutable `evaluation_stratum_id`, selected gate-evidence version if any, and `task_aware|static|shadow_only` disposition; task-aware selection is impossible unless the request exactly matches a passing stratum;
- route replay returns the same selection from the same candidate/health snapshot;
- no-candidate and all-candidates-unhealthy paths are tested;
- provider disable, workspace denylist, stratum change, and policy rollback take effect without deployment and fail task-aware traffic back to the strongest currently eligible static policy.

### 9.3 Invocation and provider runtime

Use curated HTTP/model/CLI adapters behind one async interface. Separate benchmark and production worker pools. Enforce per-provider concurrency, circuit breaking, retry budgets, absolute deadlines, and result-size limits. Before canonical request persistence, apply the Registry Alpha bounded fail-closed input secret/PII scan; raw credentials are accepted only through opaque adapter secret bindings. Persist retained clean arguments only through the inherited unique per-run external hierarchy with purpose-separated encryption/fingerprint subkeys and ciphertext/keyed envelope; queues, replay state, caches, WAL, replicas, and backups never receive plaintext or key material. For every initial or fallback fence, decrypt once into the inherited single-use `ScannedArgumentLease`, re-scan and bind its exact per-run-keyed fingerprint/current policy in the fence, retain that same in-memory lease and serialization buffers until the transport reports the full request body written or definitive zero-byte failure, and then wipe them; never decrypt again after fencing. A partial write or post-fence crash follows no-retry indeterminate recovery. If a newer policy rejects previously queued content, inherit the Alpha erasure state machine: immediately suppress reads/dispatch/comparison, enter `erasure_pending`, verify irreversible destruction of both subkeys, replace active content/fingerprint state with a generic non-comparable tombstone, purge caches/references, and only then finalize rejection; historical keyed bits in WAL/backups remain untestable, and delay/failure remains quarantined/non-comparable/non-dispatchable. A persisted route is historical selection evidence, not continuing dispatch authorization.

Phase 1 extends the Alpha shared lock-plan helper while preserving the relative order of every common class. After an API idempotency lock when applicable, coordinated transactions acquire: (1) provider current-price selectors; (2) stable server-connection or deployment rows; (3) exact/current target configuration and MCP discovery pointers/snapshots; (4) capability-status projections; (5) artifact rows; (6) the current input-scan policy; (7) workspace/provider/data/destination policy rows; (8) quota/budget accounts ordered by scope, currency, and unit; and (9) run, attempt, job, and reservation rows in that order. IDs within a class are sorted. Fence workers release lease/job locks before starting this transaction; price, disable, artifact, policy, and quota mutations use only the same relevant subsequence. Reverse-order input and opposite worker/control start tests fail on any timeout, deadlock, raw coordinated lock outside the helper, or order inversion.

The immutable route quote and execution ceilings determine an approved worst-case reservation vector across every enforced dimension, including normalized currency, calls, tokens/tool usage, and workspace/provider limits. In the transaction that creates the initial dispatch fence—and again for every fallback child fence—the extended helper locks the quoted price selector, execution target/configuration, capability, artifacts, scan policy, applicable policies, quota/budget accounts, and attempt/reservation state in the order above. Recheck server time is strictly before quote expiry and the provider's applicable current immutable price-version ID still equals the quoted version. An expired quote or changed price terminates the undispatched attempt with stable `quote_expired` or `price_version_changed`, sends nothing, and requires a new route/quote plus user confirmation; the worker may not silently reprice or extend expiry. Only a fresh quote proceeds to check each limit dimension net of outstanding reservations, append a unique `usage_reservation` for the exact attempt/fence, atomically move the whole worst-case vector from available to reserved, and write the dispatch fence. No complete reservation means no fence and no provider send; concurrent attempts therefore cannot all pass a check against the same unreserved balance. If any other eligibility or reservation check fails, terminate the still-undispatched invocation with stable `dispatch_eligibility_revoked` or `quota_budget_unavailable` evidence.

Provider reconciliation is the only path from reserved liability to a terminal accounting disposition. Definitive actual usage commits that amount and releases the proven remainder; definitive non-execution releases the full reservation. An uncertain fenced attempt retains its worst-case reservation while `indeterminate` until provider evidence or manual reconciliation resolves it; an age policy may escalate or block new work but cannot release an amount that might still be billed. A fallback child gets a new reservation only under the remaining immutable run ceiling and only after the prior attempt is proven not executed and its reservation is released, unless a tested end-to-end provider idempotency contract gives one shared charge identity and reservation lineage. Any provider usage above the reserved ceiling is a fail-closed billing incident that blocks further dispatch for the affected account until reconciled. Record execution disposition separately from transport status. Fallback is allowed only when durable evidence proves the preceding attempt did not execute, or when all candidates share a tested end-to-end idempotency contract for the external operation. A fenced attempt with uncertain provider acceptance becomes `indeterminate` and can neither retry nor fall back. Every result passes the Registry Alpha quarantine, classification, redaction, and artifact policy before ordinary persistence or display; scanner failure fails closed.

Acceptance criteria:

- worker termination before the dispatch fence safely requeues; termination at or after the fence without a durable result produces `indeterminate` plus `reconciliation_required` rather than an automatic repeat;
- argument-bearing initial and fallback attempts consume the exact pre-fence-scanned `ScannedArgumentLease` once, never decrypt after fencing, retain/wipe it through full-body write or proven zero-byte failure, and classify partial write or post-fence crash as indeterminate rather than reconstructing the lease;
- disabling the selected deployment/capability, revoking workspace/provider policy, or invalidating an artifact after enqueue but before the initial fence produces `dispatch_eligibility_revoked` and zero provider sends;
- a sensitive/scanner-failed provider input creates no persisted request or dispatch, while a scan-policy change before a fence forces a fresh decision, immediately suppresses request reads, dispatch, and fingerprint comparison, and reaches terminal rejection only after attested destruction of both per-run subkeys makes historical ciphertext and low-entropy fingerprint enumeration unrecoverable and installs a generic no-replay tombstone, with zero provider sends;
- fault injection covers termination before send, after send, after provider receipt, and before response persistence;
- deadline and cancellation propagate where the provider supports them; post-fence cancellation becomes `cancelled` only with definitive non-execution/rollback evidence and otherwise remains awaiting or becomes `indeterminate`;
- circuit breaker removes a degraded deployment from new candidate snapshots, and the fence transaction blocks a stale queued selection after degradation;
- permitted fallback creates a new child attempt linked to the original invocation and route; fault tests prove that post-fence timeout, lost response, and ambiguous failure paths never fall back, and that no-candidate, exhausted-chain, and guard-race paths leave `fallback_queued` through the specified terminal edge.
- a barrier test starts more concurrently eligible attempts than the remaining limit permits and proves the fence-plus-worst-case-reservation transaction admits only the bounded subset; no negative available balance or unreserved fenced attempt is possible;
- fake-clock and price-update races prove a queued run whose quote expires or whose current provider price version changes cannot reserve, fence, or send and cannot be silently repriced;
- reconciliation tests prove successful usage commits actual cost and releases only the remainder, definitive non-execution releases all, indeterminate execution keeps the worst-case liability, and fallback cannot reserve beyond the immutable run ceiling.

### 9.4 Outcome collection

Ship Python and TypeScript `reportOutcome` support plus one signed CI/GitHub integration. Validate that the reporter owns the invocation or holds a scoped integration credential. Derive labels asynchronously from evidence.

Acceptance criteria:

- duplicate outcome evidence is deduplicated;
- conflicting evidence is retained and marks the label disputed;
- manual reports cannot overwrite deterministic CI evidence;
- outcome coverage and confidence distribution are visible by client and capability.

### 9.5 Operations console

Build only internal workflows needed to operate the alpha:

- invocation search and timeline;
- route candidates, filters, and reason codes;
- provider health and disable control;
- benchmark and production performance kept in separate views;
- outcome coverage and dispute queue;
- workspace quotas and access;
- router/capability version rollback.

### 9.6 Phase 1 epics

| Epic | Scope | Depends on | Exit condition |
|---|---|---|---|
| P1-01 Identity | Workspaces, workspace memberships, API keys, RBAC, audit log | Registry Alpha identity | Authorization matrix proves the inherited end-to-end 60-second budget, derived cache expiry, and privileged cache bypass |
| P1-01A Artifacts | Non-overwritable fenced ingestion, expiry-driven quiescent abandoned-upload cleanup, completion/scanning, immutable artifact URI, and event-revocable sealed `ArtifactAccessGrantToken` | Identity, object storage | Backend proves bounded `W`/`C` and expiry fencing; completion rejects expired `open` rows under lock; cleanup waits for quiescence and attests absence within 15 minutes; individual grant revocation suppresses replay/redemption; finalized artifacts survive cleanup; overwrite/expiry/wrong-version tests fail closed |
| P1-02 Routing API | `/routes`, schemas, idempotency, stratum-scoped activation evidence | P0 router, P1-01A | Replayable decision records exact `evaluation_stratum_id`; unvalidated/changed/private strata cannot consume a universal activation flag |
| P1-03 Run API | `/routed-runs`, shared `/runs` status projection, exclusive execution binding, in-place ledger migration, job creation | Identity, artifacts, jobs | An Alpha client deserializes every routed lifecycle state through the unchanged public status enum and forward-compatible event envelope; every run/attempt has exactly one MCP-connection or provider/local deployment version; one ledger serves all kinds |
| P1-04 Runtime | Input scanning/encryption/content-and-fingerprint cryptographic erasure, adapter pools, quote/price freshness, dispatch eligibility, atomic quota/budget reservation, deadlines, fallback, circuit breakers | P0 adapters | Newly rejected queued content becomes non-comparable and terminal only after both per-run subkeys are destroyed; every fence rechecks current active/closed health, has a live unchanged price version, and reserves one worst-case amount in the same transaction; expiry/repricing sends nothing and concurrency cannot overspend |
| P1-05 Outcomes | Evidence API, SDKs, CI integration, label derivation | Invocation lineage | >=90% expected alpha coverage in staging trial |
| P1-06 MCP | Nine meta-tools—`route_task`, `run_task`, `search_capabilities`, `get_capability`, `get_invocation`, `report_outcome`, `create_artifact_upload`, `complete_artifact_upload`, and `read_artifact`—mapped to API/services | Stable HTTP and artifact contracts | A host with a tested non-recording secret-result channel transfers the target directly to the upload primitive; unsupported hosts fail closed, and result reads use only bounded MCP chunks; contract/auth/leakage tests pass |
| P1-07 Console | Operations and experiment views | Core APIs | On-call can diagnose/disable/replay without SQL |
| P1-08 Telemetry | Traces, logs, metrics, usage-reservation and cost reconciliation | All request paths | Correlated trace/span-link chain covers route, reservation/fence, terminal execution, reconciliation, and later outcome; stale liabilities alert |
| P1-09 Security | Threat model, retention, secret flow, dependency scans | Runtime/API | G2 security checks pass |
| P1-10 Alpha rollout | Shadow, canary, design partners | All above | G2 approved and runbook exercised |

---

## 10. Later roadmap

Durations below begin only after the preceding gate.

### Phase 2 — evaluation platform, 6–8 weeks

Build suite and case administration, immutable benchmark versions, scheduled regression runs, executable graders, judge registry, tournaments, pairwise results, performance frontiers, and benchmark-to-production correlation reports.

Exit criteria:

- a new curated capability version can be evaluated and compared without code changes;
- hidden assets are access-separated from providers and general operators;
- benchmark regression alerts have measured false-positive behavior;
- production and benchmark results can be compared by task segment without merging their labels.

### Phase 3 — creator platform, 8–10 weeks

Build creator identity, draft manifests, validation, immutable publishing, external HTTP/MCP registration, deployment health, security review workflow, analytics, eligibility states, and limited exploration allocation.

Keep execution allowlisted. Publication does not imply routing eligibility.

Exit criteria:

- the publisher state machine is transactional and recoverable;
- every routable version has provenance, benchmark evidence, health, permissions, and a kill switch;
- gaming, self-dealing, benchmark leakage, and abusive pricing have an initial policy and audit controls;
- support can suspend a creator, version, or deployment independently.

### Phase 4 — paid marketplace, 8–12 weeks

Build quotes, monetary credits, double-entry ledger, usage reconciliation, refunds, provider payable balances, spending controls, tax/compliance integrations, payouts, and later a payment-rail adapter such as x402.

Payment adapters post balanced ledger transactions; blockchain or card events are evidence, not the source of computed balances.

Exit criteria:

- every invocation charge reconciles to quote, actual usage, refund state, and provider liability;
- ledger invariants and replay pass independent review;
- negative-balance, partial-failure, duplicate-webhook, chargeback, and payout-reversal paths are tested;
- required legal, tax, sanctions, and money-movement reviews are complete.

### Phase 5 — capability studio, 8–12 weeks

Integrate Tangle for compose/test/benchmark/deploy/publish. Use the existing publisher, evaluation, identity, and deployment contracts. Tangle-specific identifiers remain inside the adapter boundary.

Exit criteria:

- a graph digest resolves to an immutable capability source;
- Studio-created and externally hosted capabilities enter the same evaluation and eligibility pipeline;
- failure or replacement of Tangle does not invalidate marketplace domain records.

### Phase 6 — learned routing, data-gated

Implement offline feature pipelines, point-in-time-correct training data, model registry, calibration, temporal evaluation, shadow inference, drift detection, fairness/concentration analysis, guarded exploration, and rollback.

Roll out in this order:

1. shadow predictions with V0 still selecting;
2. analyst comparison and calibration monitoring;
3. 1% canary on low-risk tasks;
4. gradual segment-by-segment exposure;
5. bounded contextual exploration;
6. optional ensembles and validator capabilities.

Never train directly from mutable production tables. Materialize versioned datasets with label cutoff time, feature availability time, inclusion policy, and lineage.

---

## 11. Data implementation

The released Registry Alpha schema is the migration baseline, not a parallel subsystem. Every follow-on migration extends its tenant, capability, job, run, attempt, event, artifact, idempotency, and audit rows in place; the names below distinguish genuinely new tables from extensions to that existing authority.

### 11.1 Phase 0 tables and extensions

- `task_types`, `task_type_versions`
- `benchmark_suites`, `benchmark_suite_versions`
- `evaluation_strata` plus `benchmark_cases`, `benchmark_case_versions`, and `benchmark_case_assets`, including frozen classification/source-ownership/task/provider-policy/candidate-eligibility scope, repository/archetype-lineage graph provenance, `correlation_cluster_id`, and irreversible `g1_revealed_at`/attempt provenance
- `g1_activation_attempts`, `g1_alpha_allocations`, and immutable attempt-component assignments keyed by exact `evaluation_stratum_id`, recording per-stratum maximum-attempt policy, separate success/cost alpha spend, frozen policy/candidate versions, reveal status, and terminal decision
- `capability_task_claims` plus additive evaluation metadata on the existing `capabilities` and `capability_versions` rows
- `deployments`, immutable `deployment_versions` including required minimum output classification, immutable `provider_price_versions`, and `deployment_health_snapshots`; routing candidates freeze the exact deployment/classification, price version, quote expiry, and worst-case reservation vector
- `evaluation_visibility_domains` and campaign/stage-scoped `evaluation_access_grants`, with mutually exclusive runner/reviewer/operator versus adapter-developer roles and immutable access/audit provenance
- `evaluation_runs`, `evaluation_attempts`, `evaluation_results`, `grader_results`; after the execution-binding and visibility migration below, Phase 0 writes evaluation executions as `run_kind=evaluation`, `run_visibility_scope=evaluation_restricted`, an exact visibility-domain ID, and an exact immutable `deployment_version_id`, then references those authoritative `runs`/`run_attempts` rows rather than duplicating dispatch state
- `routing_models`, `routing_model_versions`
- `routing_decisions`, `routing_candidates`, `routing_feature_snapshots`, with every live decision bound to the exact evaluated stratum and approved gate-evidence version or explicitly marked static/shadow-only
- visibility-inheriting references to the existing immutable `artifacts` rows; no evaluation-specific artifact store, and ordinary artifact metadata/grant/content paths cannot authorize an evaluation-restricted reference

### 11.2 Phase 1 additions and in-place run-ledger migration

- `api_credentials`; the existing `users`, `workspaces`, and `workspace_memberships` remain canonical
- `permission_grants`, `workspace_policies`
- `task_instances`
- the additive `routed` value for the existing `run_kind` constraint, nullable `routing_decision_id`/`task_instance_id`, and routing lineage on existing `runs`
- additive exclusive execution-target binding, parent-attempt, execution-disposition, and fallback lineage on existing `run_attempts`; each initial/fallback attempt points to exactly one immutable `server_connection_version` or `deployment_version`
- new routed/fallback event detail strings in existing `run_events`; the public known-or-unknown event wrapper remains compatible, and event ordering/projection replay remain one stream per run
- `run_artifacts` as a visibility-domain- and role-qualified link to existing immutable `artifacts`; `artifact_uploads` retains only opaque upload ID, safe configured-origin ID, immutable key/version scope, constraints, `open|completed|cleanup_pending|deleted` state, expiry, qualified `W`/`C`, write-fence/active-lease evidence, cleanup lease/attempts, quiescence deadline, and deletion attestation—never an `EphemeralUploadTarget` credential; upload-target and canonical `artifact_access_grants` rows store only non-secret metadata/versioned HMAC verifiers, inherited append-only grant events/current projections provide `active|revoked|expired`, and inherited idempotency/outbox rows reference prepared/committed exact-lifetime encrypted envelopes and retain only non-secret tombstones after logical expiry/erasure
- append-only `usage_reservations` linked one-to-one with a dispatch fence/attempt, plus reservation/commit/release/reconciliation events and workspace/provider quota-budget account balances
- `outcome_evidence`, `outcome_labels`, `outcome_label_history`
- `provider_usage_records`
- extensions to existing `idempotency_records` and `audit_events`; no second idempotency or audit ledger

The Phase 0 rolling migration first adds nullable `run_kind`, `run_visibility_scope`, `evaluation_visibility_domain_id`, `execution_binding_kind`, and `deployment_version_id` to existing `runs` and `run_attempts`. Before any evaluation row exists, deploy database row-level policies and constrained database roles: the ordinary API role can select/mutate only `workspace` rows and their visibility-inheriting events/artifacts, while internal evaluation roles require an explicit campaign/stage grant; null or unknown visibility is denied except inside the bounded backfill role. New readers/workers then treat null legacy kinds as direct MCP only during backfill, leave unknown kinds non-dispatchable/non-readable, and backfill Registry rows with `run_kind=direct`, `run_visibility_scope=workspace`, and `execution_binding_kind=mcp_connection`. Retire every old application/database role that can bypass the visibility policy and every writer/reader that assumes non-null `server_connection_version_id`, then enforce non-null visibility plus the constraint that `evaluation` requires `evaluation_restricted` and an evaluation domain while `direct` requires `workspace` and no evaluation domain. Relax the standalone binding constraint and validate the execution XOR: exactly one of `server_connection_version_id` and `deployment_version_id` is set and agrees with `execution_binding_kind=mcp_connection|deployment`. Only after those policies, grants, backfills, compatibility tests, and constraints pass may the `evaluation` kind and evaluation writers be enabled; model, CLI, and static-analysis executions use an immutable `deployment_version_id` and never fabricate an MCP connection.

Phase 1 uses the same expand/contract sequence to add nullable routing columns and the `routed` allowed value; routed creation writes `run_kind=routed`, `run_visibility_scope=workspace`, its routing foreign key, and its initial exclusive target into the same transaction/ledger, while every fallback attempt records its own exact target and inherits visibility. Existing run IDs, attempts, events, artifacts, lifecycle projections, idempotency records, and API results are never copied or renamed. Shared maintenance repositories require an explicit visibility domain; public query/cancel/event/artifact repositories are workspace-only, and evaluation repositories are campaign/stage-grant-only rather than relying on an optional caller filter. Rollback first disables new evaluation/routed creation and their workers; down-level binaries run only with the ordinary database role, so row-level policy keeps evaluation rows invisible and immutable rather than misclassifying or exposing them. Columns, policies, roles, or allowed values are not removed until the compatibility window and rollback evidence close.

### 11.3 Data rules

- All mutable entities use optimistic concurrency or explicit state-transition locks.
- Event and decision tables are append-only; corrections supersede prior records.
- Every run/attempt has one exact execution binding enforced by a database XOR constraint: an immutable MCP `server_connection_version_id` or immutable provider/local `deployment_version_id`, never both or neither.
- Every execution binding supplies an immutable minimum output classification. The run records input, binding-minimum, detector/profile, and effective classifications, with constraints requiring the effective class to be their lattice maximum; restricted output cannot have an ordinary digest or inline representation.
- Upload completion and expiry cleanup use one locked monotonic state machine and database server time. `open -> completed` is legal only when `now < expires_at`; an expired completion instead performs/preserves `open -> cleanup_pending`, permanently forbids completion, activates the qualified write fence, aborts multipart state, and repeatedly deletes only the exact key/version. `cleanup_pending -> deleted` is legal only after all active write leases close or their hard deadline passes, `expires_at + W + C` has elapsed, and provider-consistent absence is attested; a late materialization restarts deletion/verification. Cleanup failures or uncertain quiescence retry and alert, and no transition can move a completed upload into deletion.
- Artifact-grant mint, revoke, expiry, replay, and redemption use the inherited append-only event/current-projection authority. Individual revocation atomically advances to `revoked`, suppresses credential-envelope replay before asynchronous erasure, and every later redemption requires `active`; a bearer token or delayed cleanup cannot override that projection.
- Run, event, result, and artifact visibility inherits one non-null domain. PostgreSQL row-level policy plus separate database roles—not an optional repository predicate—prevents ordinary API identities and workspace roles from enumerating, reading, cancelling, granting, or downloading `evaluation_restricted` data; internal evaluation access requires a compatible campaign/stage grant, and adapter-development roles cannot hold one.
- A run's initial attempt must match its stored initial binding; every fallback child records its own binding, references an eligible candidate in the immutable route/fallback snapshot, and cannot rewrite the parent run's historical selection.
- Internal run phases project through the fixed Alpha public status enum; database/API constraints reject any unmapped phase, and public list filters use only the projected value.
- G1 reveal state and alpha spend are monotonic and stratum-scoped: a revealed component can never be assigned to another activation attempt, an attempt cannot claim more than its preregistered allocation or that stratum family's remaining budget, and no activation evidence or flag authorizes a different classification/source/provider/candidate stratum.
- Dispatch fencing locks the relevant quota/budget accounts and atomically moves the approved worst-case amount from available to reserved; reconciliation commits actual usage and releases only the proven remainder through append-only reservation events.
- A fence can reference only an unexpired quote whose immutable price version still matches the locked current selector; repricing appends a new version and never mutates the version a historical route recorded.
- Timestamps are UTC and server-assigned for security/audit events.
- Money is stored as integer minor units plus currency, never floating point.
- Raw request, source, model transcript, and output retention is separate from operational metadata retention.
- Retained run arguments inherit the Registry Alpha per-run external key hierarchy: only ciphertext/keyed bits/references may enter durable storage, queues, caches, WAL, replicas, or backups. Security reclassification monotonically enters `erasure_pending`, blocks reads/dispatch/fingerprint comparison, and becomes `erased` only after verified irreversible destruction of encryption and fingerprint subkeys, generic no-compare tombstoning, and cache cleanup; delayed destruction remains quarantined and non-comparable.
- Sensitive artifact access uses audited, short-lived grants bound to the authenticated subject, workspace, authorization epoch, and exact immutable artifact version; every redemption rechecks current membership/role, visibility, classification, retention, scan, and integrity, so possession does not bypass or outlive ordinary authorization.
- Mutating-call idempotency retains a minimal workspace-HMAC idempotency-key tombstone/key version until workspace hard deletion even after replay expires. Ordinary mutation fingerprints may retain separate workspace-key versions; content-bearing run fingerprints use only the erasable per-run subkey and become generic non-comparable tombstones after reclassification, so lookup still prevents execution without retaining a low-entropy oracle. Credential-bearing responses use encrypted envelopes plus non-secret resource/grant references; the envelope is available through the credential's exact expiry, never readable after it, and never expires prematurely. Retired lookup/comparison keys remain only while their permitted live records require them; missing key material fails closed, and destroyed per-run keys are not retained.
- JSONB is appropriate for immutable snapshots and provider-specific metadata; fields used in constraints, joins, or policy are normalized and indexed.
- Schema migrations are forward-compatible during rolling deployment and tested against production-sized fixtures.

---

## 12. Security and privacy plan

The initial threat model must cover malicious repository content, prompt injection in source code, provider data exfiltration, poisoned outputs, stolen OAuth tokens, cross-tenant artifact access, replayed outcome events, and cost-amplification attacks.

### Phase 0 controls

- public, synthetic, or data-owner-approved internal fixtures only;
- encrypted private snapshots with access logs and explicit retention/deletion dates;
- third-party processing of private source denied at repository level because no provider agreement is currently approved;
- no named provider allowlists until approved no-training and zero-retention terms are established;
- no model-based private-repository execution until controlled GPU infrastructure or an approved provider agreement exists;
- hard router filtering by data classification, execution zone, and provider policy;
- allowlisted providers and adapters;
- no third-party containers;
- least-privilege provider keys;
- default-deny tool access with a capability-version allowlist for every file, process, shell, and network tool;
- outbound traffic restricted to declared provider and tool destinations through an enforced allowlist;
- provider credentials isolated from tool-capable CLI subprocesses and never inherited by child processes unless the exact adapter contract requires a named, least-privilege secret;
- isolated working directories, resource and process limits, and no ambient host credentials;
- dependency and secret scanning in CI;
- hidden-case inputs, labels, candidate outputs, run events, and artifacts use the evaluation-restricted visibility domain; PostgreSQL policies and campaign/stage grants separate runner/blinded-reviewer access from adapter development and ordinary workspace administration, and every human access is audited.

### Phase 1 controls

- short-lived repository credentials scoped to required repositories;
- encryption in transit and at rest;
- artifact authorization independent of grant or signed-URL possession, with subject/workspace binding, event-backed individual grant revocation, and safe isolated response headers;
- log and trace redaction with tests;
- request, artifact, and output size limits;
- sealed `EphemeralUploadTarget` delivery with five-minute maximum expiry, exact create-only non-overwritable key/version and size/type/checksum scope, no redirects/read/list authority, configured-origin minting, a qualified provider/gateway write fence plus bounded post-expiry materialization/visibility window, no-store/non-recording transfer, query/form/header redaction, only exact-lifetime encrypted secret-store replay with no premature eviction, and no application-database, transcript, history, referrer, DOM, audit, error, log, trace, or analytics capture;
- sealed `ArtifactAccessGrantToken` mint/replay with a subject/artifact-version binding, versioned HMAC verifier, append-only revoke/expire events plus active projection, immediate replay suppression on revoke, exact-lifetime encrypted secret-store envelope with no premature eviction, ordinary-authentication requirement, redacted header redemption, and no full token in application/idempotency rows, audit, telemetry, browser surfaces, or caches;
- inherited bounded typed/scanned control fields ensure names, tags, notes, and audit reasons cannot carry secrets/PII into domain rows or append-only audit storage;
- ephemeral client integrity-proof/length verification, clean digest versus restricted keyed-fingerprint/ciphertext-integrity persistence, archive traversal/expansion protection, malware/type scanning, and versioned per-MIME metadata/embedded-content extraction plus all-page/frame render/OCR secret/PII classification with complete coverage before finalization/routing;
- outbound destination allowlists for workers;
- signed integration webhooks with replay protection;
- workspace-level data retention and deletion jobs;
- per-credential rate, concurrency, and spend/quota controls;
- prompt-injection-aware agent instructions and separation of code data from platform control messages.

### Before third-party hosted execution

Require a dedicated sandbox boundary, non-root/read-only images, seccomp, default-deny egress, resource and process limits, immutable image digests, SBOMs, vulnerability/malware scans, ephemeral workspaces, short-lived secrets, and incident containment. Select gVisor, Kata, Firecracker, or a managed sandbox through a measured security and operability evaluation; do not make that choice during Phase 0.

---

## 13. Test strategy

### Unit

- manifest and API schema validation;
- taxonomy and feature extraction;
- deterministic correlation-component construction and leakage checks across repository and cross-repository mutation/archetype lineages;
- filter reason codes and deterministic tie-breaking;
- state transitions;
- structural grader matching, frozen mechanism checklists, semantic-credit/tie rules, and thresholds;
- monetary/cost calculations;
- quota/budget reservation, reconciliation, and conservative indeterminate-liability calculations;
- outcome-label derivation.

### Contract

- every provider adapter passes one shared success, invalid output, timeout, cancellation, and idempotency suite;
- official-matrix admission rejects alias-only models, and an attested mid-matrix revision change invalidates rather than mixes that candidate's results;
- matrix protocol tests prove primary-slot failures cannot be replaced or outvoted by diagnostic repeats and produce one paired policy/task outcome;
- blinded grading fixtures prove a location/category overlap receives no credit for a vacuous, wrong, or contradictory mechanism; every G1 provisional match receives two independent blinded decisions, and controls/ties follow the frozen rule;
- label-gap fixtures prove a genuine or unresolved material gap after any activation output is revealed retires every component in that attempt, never salvages a subset or recomputes power, and permits only a new benchmark version plus wholly fresh holdout under remaining alpha;
- generated Python, TypeScript, and MCP interfaces match OpenAPI semantics, including sealed upload-target/access-grant handling, upload completion, unsupported-host failure, and bounded `read_artifact` parity;
- a client generated from the Registry Alpha schema deserializes list/get/event responses for every public Phase 1 state; public status never leaves the Alpha enum, unknown event detail strings remain readable, and evaluation-restricted IDs remain nonexistent through ordinary list/get/event/cancel/outcome/artifact operations;
- persisted domain events validate against versioned schemas.

### Integration

- PostgreSQL transactions and migrations;
- G1 sizing/analysis fixtures prove repository-only and archetype-only resampling are rejected, the frozen composite cluster preserves every linked case, attempt-specific confidence uses preregistered alpha, the complete quality/cost rule is jointly simulated with a >=90% Monte Carlo lower bound, an exact-five-point alternative is not mislabeled 90%-powered, and a post-reveal label gap/dispute retires the entire attempt with no case deletion, subset salvage, repowering, extension, reuse, or pooling; a passing universal/non-confidential gate activates only matching requests, while private/internal, changed-policy, changed-candidate-universe, and unknown strata remain static/shadow until their own representative G1 passes;
- upgrades from a populated Registry Alpha ledger preserve direct run IDs/events/attempts, backfill their MCP binding and workspace visibility, install ordinary-versus-evaluation database roles/RLS before enabling writers, retire bypass-capable old roles/readers, safely introduce direct/evaluation/routed kinds, reject invalid kind/visibility/domain/binding combinations, and prove ordinary APIs plus down-level binaries cannot enumerate/read/cancel or mint artifact access for evaluation rows while authorized internal maintenance and rollback still work without parallel ledgers or fabricated MCP connections;
- object upload/download authorization plus fake-clock abandoned-upload cleanup: completion arriving after `expires_at` while the row is still `open` atomically enters cleanup and returns no artifact; a single-request write accepted immediately before expiry and materialized after the first absence check cannot reach an orphaned terminal state; cleanup fences new writes, waits through active leases and `expires_at + W + C`, repeatedly deletes late objects, survives worker failure, and reaches `deleted` within 15 minutes only after consistent quiescence, while a pre-expiry completion/expiry barrier protects finalized artifacts;
- allowlisted image/PDF/archive fixtures with visible, metadata, OCR, or embedded secrets/PII follow restricted/discard policy, while encrypted, unsupported, truncated, timed-out, or coverage-incomplete content remains quarantined;
- detector-clean proprietary outputs from restricted connection/deployment fixtures remain restricted under the maximum-class lattice and cannot acquire an ordinary digest or inline/public representation; missing minimum output classification blocks dispatch;
- concurrent dispatch fences cannot over-reserve a workspace/provider quota or budget; terminal actual usage commits and releases the remainder, definitive non-execution releases all, and indeterminate execution retains the conservative liability until reconciled;
- fake-clock expiry and concurrent provider-price-update tests prove stale quoted runs create neither reservation nor fence and require a new confirmed route;
- reverse-ordered IDs and opposite fence/control start orders prove Phase 1 price, execution-target, artifact, policy, quota, and run/reservation locks preserve the shared class order without timeout or deadlock;
- idempotency replay lookup across HMAC-key rotation and fail-closed behavior when a retired key required for lookup is unavailable;
- durable job recovery;
- provider circuit-breaker and health-generation barriers prove every initial and fallback fence rechecks the locked current target as `active`, breaker `closed`, and dispatch-eligible, and a breaker opened after admission produces zero provider sends;
- webhook verification and deduplication;
- OpenTelemetry propagation.

### Replay and evaluation

- golden route decisions replay exactly;
- historical candidate sets can be scored by a new router without mutating history;
- benchmark reports reproduce from a clean database and artifact snapshot;
- temporal training/evaluation queries reject future leakage.
- ordinary Viewer/Operator/Admin/API and down-level-reader matrices cannot enumerate or dereference evaluation run, event, result, case, or artifact IDs; campaign/stage grants expose only the intended slice to runner/blinded-reviewer/operator roles, adapter-developer combinations are rejected, and access attempts are audited.

### End to end

- artifact upload, completion, authoritative URI issuance, authorized viewer/download grants, and rejection of unsafe, mutable/overwritten, wrong-version, integrity-mismatched, unfinalized, expired, or cross-workspace artifacts;
- artifact grants are carried only in redacted headers and never appear in URLs, access logs, traces, history, or referrers;
- access-grant and upload-target mint/replay crash and fake-clock matrices prove the encrypted envelope is durably prepared before the domain/idempotency/outbox commit, same-key recovery promotes/replays exactly that credential at `expires_at - epsilon`, the envelope becomes logically unreadable at expiry, backing storage never evicts it prematurely, rollback/uniqueness orphans expire inaccessible, and missing committed or expired envelopes fail without reminting; ordinary rows retain only HMAC verifiers/non-secret references;
- fake-clock IdP propagation and lookup races prove cached authorization plus frontend display expires within the single inherited 60-second budget, and mutations, grant mint/redemption, and each `read_artifact` chunk bypass the shared cache and fail closed on source timeout/incompleteness;
- individual grant revocation advances the event-backed projection, suppresses mint replay, and denies every later redemption even if envelope erasure is delayed; membership/role removal, authorization-epoch change, visibility/classification/retention-policy revocation, quarantine, or integrity-state change likewise denies artifact bytes with an otherwise valid grant;
- HTTP upload targets are returned only on authorized creation or matching idempotency replay with no-store; tests prove exact-lifetime replay availability at `expires_at - epsilon`, immediate logical denial at expiry, no premature envelope eviction, and credential redaction/disabled capture across app, proxy, CDN, object-store audit, logs, traces, browser navigation/referrers/history/DOM, errors, and analytics, while redirect, expiry, overwrite, over-size, wrong-type/checksum, and broader-authority attempts fail closed; completion after server-side expiry returns no artifact even before the sweeper runs, a pre-expiry single PUT that materializes after cleanup's first absence check is fenced/observed/deleted before quiescent terminal attestation, and uncompleted exact object versions remain non-completable and delete within the 15-minute SLO across crash/retry/completion races;
- an MCP host with the required non-recording secret-result channel transfers `EphemeralUploadTarget` directly to its upload primitive and completes without REST credentials; a host without that channel receives no target and fails closed;
- `read_artifact` returns authorized, integrity-checked bounded chunks only after complete content-aware classification and rejects active, encrypted/uninspectable, unknown, unsupported, type-mismatched, coverage-incomplete, quarantined, expired, or cross-workspace results without exposing restricted plaintext fingerprints;
- route-only success and no-candidate paths;
- run through terminal result and outcome;
- every routed internal phase projects monotonically through the stable Alpha `Run.status` values, including `fallback_queued -> running` and definitive pre-dispatch `execution_failed -> failed`; security-erasure failure cannot become terminal before attestation and no pre-dispatch failure edge is legal after a fence;
- retry, timeout, evidence-gated fallback, ambiguous post-fence execution, cancellation, and provider degradation;
- same-key delayed mutation replay after full response expiry is rejected without execution;
- deployment/capability disable, policy revocation, and artifact invalidation between enqueue and the initial dispatch fence each terminate without a provider send;
- quote expiry and provider repricing between enqueue and any initial/fallback fence terminate without reservation or provider send and never silently alter the accepted price;
- sensitive/scanner-failed input leaves no persisted request, content-derived audit value, or provider send; scan-policy changes before either an initial or fallback fence force a fresh decision, suppress all encrypted request reads, dispatch, and fingerprint comparisons, and finalize only after attested destruction of both per-run subkeys proves restored database/WAL/replica/backup ciphertext undecryptable and retained keyed fingerprints unusable for low-entropy enumeration, with only a generic no-replay tombstone remaining;
- workspace isolation;
- version disable and router rollback.

### Non-functional

- API and worker load tests;
- job-backlog recovery;
- fault injection for provider errors, database failover, and object-store latency;
- static analysis, dependency scanning, secret scanning, and periodic penetration testing;
- cost-budget tests that prevent unbounded retry or ensemble execution and barrier tests proving the fence and worst-case reservation are one atomic linearization point.

No merge is releasable with failing migration, contract, replay, or workspace-isolation tests.

---

## 14. Observability and experiment integrity

One correlation chain must connect the following stages. Propagate trace context through short asynchronous work and use span links plus immutable invocation/outcome IDs for evidence that arrives after the original trace lifetime; do not hold a span open while awaiting downstream outcome evidence.

```text
request -> classification -> features -> candidates -> filters -> selection
        -> quote/estimate -> job -> provider attempt -> normalization
        -> result -> outcome evidence -> derived label
```

Required metrics:

- route latency and candidate counts;
- filter counts by reason;
- selection counts by bounded environment, task-family, provider-family, and experiment-cohort dimensions;
- predicted/estimated versus actual cost and latency;
- quota/budget available, reserved, committed, released, and indeterminate-liability amounts plus reservation age and reconciliation lag;
- provider-family success, timeout, schema failure, and circuit state;
- task success, cost per success, retry, fallback, and correction;
- outcome coverage, evidence type, confidence, age, and dispute rate;
- benchmark versus production performance by task segment;
- policy concentration and exploration allocation;
- artifact bytes, incomplete-upload cleanup age/failures, and retention backlog.

Metric labels come from an explicit bounded enum allowlist with per-metric series budgets. Do not place raw source, prompts, outputs, secrets, workspace/customer identifiers, capability/deployment/router version IDs, or other high-cardinality values in metric labels. Exact workspace and version attribution belongs in access-controlled operational tables and restricted logs/traces, linked by opaque correlation IDs rather than exported as dimensions.

---

## 15. Reliability targets and runbooks

Phase 1 alpha targets:

- route-only API availability: 99.5% monthly;
- p95 route-only latency: <300 ms for 100 curated capability versions;
- accepted jobs durably recorded: 99.99%;
- terminal invocation status eventually recorded: 99.9%;
- duplicate billable provider attempt caused by platform retry: <0.1%;
- outcome ingestion availability: 99.5%;
- routing-decision replay completeness: 100% for retained decisions.

Create and exercise runbooks for:

- provider outage or latency spike;
- incorrect capability version or manifest;
- bad router rollout;
- growing job backlog;
- outcome webhook replay/forgery;
- artifact exposure, abandoned-upload cleanup failure, or secret in logs;
- runaway spend;
- stuck or indeterminate quota/budget reservations;
- database migration failure;
- hidden benchmark leakage.
- universal G1 success followed by a private/internal request proves the live selector still uses the strongest eligible static policy while persisting a shadow task-aware decision; only a separate passing private/internal gate can enable its task-aware flag.

Capability disable, deployment disable, and router rollback must be separate controls. A route already issued must retain its historical meaning after any rollback.

---

## 16. Rollout plan

1. **Offline calibration:** establish the strongest static policy and validate the initial harness/corpus.
2. **Internal route shadowing:** record what task-aware V0 would select while the static policy controls execution.
3. **Internal route-only:** expose explanations and alternatives to internal users; humans may override selection.
4. **Internal execution:** route curated, non-sensitive tasks with the static policy until their exact universal/non-confidential stratum passes G1, then canary task-aware selection only there.
5. **Design-partner shadowing:** collect feature distributions and eligibility without executing.
6. **Design-partner canary:** enable low-risk repositories and strict quotas for 5% of eligible tasks using the strongest static policy; task-aware stays shadow-only until that private/internal stratum passes its own G1.
7. **Closed alpha:** expand by workspace and task segment when success, cost, outcome coverage, incident metrics, and the exact stratum-specific activation evidence remain within bounds.

Use feature flags at workspace, immutable `evaluation_stratum_id`, task type, policy, capability version, and deployment levels. A task-aware flag must reference one passing gate-evidence version and match the request's current stratum exactly; otherwise selection fails to static/shadow. Every rollout step has an owner, start/end time, comparison cohort, abort threshold, and rollback procedure.

---

## 17. Delivery dependencies and estimates

The two initial workstreams converge at the closed alpha:

```text
Evaluation: contracts -> initial corpus -> static ranking -> shadow router -> G1 activation

Product:    API/runtime -> outcome capture -> security/reliability -> G2 closed alpha

After alpha: production outcomes -> G3 creator supply -> economics -> Studio/learning
```

The closed alpha does not depend on G1 because it can use the strongest eligible static quality policy. Evaluation still cannot be skipped: it establishes the initial ranking, safe constraints, regression detection, and evidence needed to activate task-aware routing. Creator, payment, and learned-routing investments remain gated by observed demand, outcome quality, and operational readiness.

Rough order-of-magnitude estimates for the assumed team and scope:

| Phase | Elapsed time | Engineering effort | Confidence | Main uncertainty |
|---|---:|---:|---|---|
| 0 — Initial calibration | 3 weeks, overlapping Phase 1 | 10–12 person-weeks | Medium | Corpus authoring and grading agreement |
| 1 — Closed alpha | 8–10 weeks from kickoff | 28–36 person-weeks | Medium | Provider reliability and outcome integration |
| 2 — Evaluation platform | 6–8 weeks | 22–30 person-weeks | Low/medium | Judge and tournament workflow complexity |
| 3 — Creator platform | 8–10 weeks | 30–40 person-weeks | Low | Security review and provider variability |
| 4 — Paid marketplace | 8–12 weeks | 34–50 person-weeks plus legal/finance | Low | Compliance, reconciliation, and payout rails |
| 5 — Capability Studio | 8–12 weeks | 26–40 person-weeks | Low | Tangle integration depth and UX scope |
| 6 — Learned routing | 8–12 weeks for first canary, then ongoing | 30–45 person-weeks initially | Low | Label quality, segment support, and drift |

These ranges exclude time waiting on external compliance, payment-provider, Apple/macOS infrastructure, or design-partner approvals. Re-estimate at every gate using observed throughput and incident data. Do not present the multi-quarter estimates as one committed launch date.

---

## 18. Staffing and ownership

### Phase 0/1 team

- **Technical lead/staff engineer:** architecture, routing protocol, ADRs, cross-stream integration, gate recommendation
- **Evaluation/data engineer:** corpus, grader validity, statistics, replay analysis
- **Backend/platform engineer:** persistence, jobs, provider adapters, runtime reliability
- **Product/full-stack engineer:** API/SDK integration, MCP, operations console
- **Project owner / primary Swift reviewer:** defect taxonomy, fixture authoring, labels, and grading-rubric ownership
- **Part-time secondary Swift reviewer:** blinded 20% calibration sample, all disputed high/critical findings, and adjudication support
- **Part-time security reviewer:** Phase 1 threat model and G2 review

One directly responsible individual owns each epic and each go/no-go gate. Product decides value tradeoffs; engineering owns measurement integrity, reliability, security, and whether evidence supports the gate.

Plan approximately 25–50 hours of primary-reviewer effort for the initial 75–100 cases and 100–150 hours cumulatively as the corpus grows toward 300, separate from repository preparation and mutation authoring. Reserve approximately 10–15 hours of secondary review for initial calibration and 25–40 hours cumulatively. Measure actual review throughput on the first ten gold cases and revise the corpus schedule at the Day 10 checkpoint.

### Later team growth

- Evaluation Platform can become its own team after Phase 1.
- Runtime/Security must become a dedicated ownership area before third-party containers.
- Marketplace/Economics needs backend, finance operations, legal/compliance, and security ownership before real money.
- Routing/Data owns feature pipelines and ML only after G4.

---

## 19. Open-source, proprietary, and data boundaries

Keep the repository private through initial calibration so the team can change contracts quickly and ensure hidden evaluation assets never enter public history.

Candidates for later open-source extraction:

- task and capability manifest schemas;
- provider adapter protocol and conformance tests;
- Python and TypeScript SDKs;
- MCP server facade;
- local development stack;
- benchmark runner framework and selected public cases;
- Tangle adapter after its contract stabilizes.

Keep private:

- hidden case assets and labels;
- corpus provenance details that expose hidden cases;
- production task inputs, outputs, outcomes, and feature snapshots;
- routing estimates, policies, models, and exploration allocation;
- benchmark-to-production correlations;
- provider fraud, gaming, and contamination signals;
- commercial performance and economic analytics.

Public packages must not import private packages or require private data to run their contract tests. Add a CI check for dependency direction and a secret/history scan before any repository is made public. Publishing a runner must not publish the authoritative hidden suite, scoring thresholds, or access credentials.

---

## 20. Principal risks

| Risk | Early signal | Mitigation |
|---|---|---|
| No exploitable capability heterogeneity in the initial corpus | Oracle headroom is small | Ship the static quality policy, keep task-aware routing in shadow, revise task segments/features/candidates, and continue collecting outcomes |
| Router overfits or repeated G1 looks create a false activation | Validation gains disappear on composite-cluster test or a revealed holdout is extended/reused | Frozen components, preregistered maximum attempts and alpha spending, irreversible reveal retirement, wholly fresh per-attempt holdouts, versioned analysis |
| Review labels are subjective | Low reviewer agreement and high adjudication | Narrow defect definitions; prefer seeded defects; report ambiguity |
| Swift/iOS infrastructure blocks reproducibility | Xcode/macOS queue and version failures | Make source review authoritative; retain an SPM subset; keep optional Xcode evidence in a separate macOS stratum and budget |
| Provider/model drift invalidates results | Same version changes over time | Record timestamp/config, monitor sentinels, rerun anchors, flag unpinned sources |
| Outcome data is sparse or biased | Reports concentrate among failures or one client | CI integrations, evidence confidence, missingness dashboards, no naive training |
| Benchmark gaming/contamination | Sudden benchmark lift without production lift | Hidden cases, rotating suites, provenance, production correlation, audits |
| Cold-start capabilities cannot compete | Incumbents receive all traffic | Shrinkage, minimum evidence, controlled exploration after safety threshold |
| Prompt injection or data exfiltration | Unexpected tools/network destinations | Treat repo as untrusted data, strict tool policy, egress controls, redaction |
| Microservice overhead slows proof | Contract and deployment work exceeds experiment work | Modular monolith and explicit extraction triggers |
| Cost variance or stale pricing breaks user constraints | Provider charge exceeds estimate or quoted price version changes while queued | Immutable price versions, quote-expiry/current-price recheck under fence locks, atomic worst-case reservation, hard token/time budgets, reconciliation, abort thresholds |
| Marketplace fraud/self-dealing | Synthetic outcomes or circular traffic | Provenance, confidence weighting, anomaly detection, payout holds |
| Payment/compliance scope dominates | Engineering blocked on money movement | Delay real settlement until buyer demand and accounting requirements are demonstrated |

---

## 21. Architecture decision records required before implementation

Create and approve these ADRs in Week 1:

1. Canonical task taxonomy and versioning policy
2. Calibration success definition and task-aware activation gate
3. Source-level iOS review versus optional Xcode/macOS validation boundary
4. Immutable capability/deployment identity
5. Benchmark artifact and hidden-label access model
6. Provider adapter and normalized review-output contract
7. PostgreSQL durable-job semantics and idempotency
8. V0 score normalization, shrinkage, and tie-breaking
9. Outcome evidence hierarchy and label supersession
10. Data classification, retention, and deletion
11. Authentication and workspace isolation for the alpha
12. Build-versus-buy decision for sandboxing before third-party execution

ADRs 1–9 block authoritative benchmark runs and task-aware activation, but not API/runtime scaffolding. ADRs 10–11 block the closed alpha. ADR 12 blocks hosted third-party publishing, not earlier phases.

---

## 22. Definition of done

### Initial calibration is done when

- the protocol and code are versioned;
- the initial 75–100-case corpus passes the applicable G0 quality checks;
- the complete candidate matrix is captured with provenance;
- the hidden result is reproducible by a second engineer;
- the strongest static quality policy and task-aware shadow policy are versioned;
- a written policy recommendation and corpus-expansion plan are approved.

### Phase 1 is done when

- an authorized HTTP client, or an MCP host with a non-recording upload-target channel, can ingest and complete a repository snapshot and diff and receive finalized workspace-scoped `artifact://` identifiers pinned to immutable object versions; an unsupported MCP host fails closed without receiving credentials;
- an MCP-only result client can retrieve permitted text/JSON or bounded binary chunks through `read_artifact` using its MCP session, while HTTP clients use subject-bound short-lived access and neither path receives object-store credentials;
- an authorized HTTP or MCP client can route and execute a supported task idempotently;
- direct and routed executions share the upgraded Registry `runs`/attempt/event ledger and canonical public Run resource with no parallel invocation tables;
- every direct, evaluation, routed, and fallback attempt preserves one exact database-enforced MCP-connection or provider/local deployment-version binding;
- the exact decision, version, attempts, costs, artifacts, and evidence are traceable;
- evidence-gated fallback and provider disablement work under fault injection, while uncertain fenced execution never falls back;
- outcome coverage and trust are measurable;
- security, isolation, replay, migration, and SLO tests pass;
- the closed-alpha rollout and rollback runbooks have been exercised.

### The broader platform is not done merely when features exist

Creator, payment, Studio, and learned-routing phases are complete only when their stated evidence gates and operational controls pass. Listing count, integration count, or payment volume are not substitutes for routing lift and task outcomes.

---

## 23. First ten working days after Registry Alpha

This schedule starts only after Milestone 1 satisfies its release definition of done. Registry implementation follows `MCP_REGISTRY_ALPHA_IMPLEMENTATION_PLAN.md`; no corpus, model-adapter, or routing work below is on the Milestone 1 critical path.

### Days 1–2

- Assign epic owners and approve planning assumptions.
- Confirm the allowed UIKit/SwiftUI frameworks, minimum deployment targets, and optional macOS validation subset.
- Inventory internal repositories, assign data owners, and record provider-processing and retention policies before snapshotting code.
- Write ADRs 1–4.
- Freeze the Phase 0 task contract and finding schema.

### Days 3–5

- Preregister the quality-first objective, default eight-point/1.10 joint planning alternative or pre-data rationale for a stricter alternative, complete-gate joint simulation and Monte Carlo precision rule, maximum G1 attempts, separate success/cost alpha-spending schedules, irreversible post-reveal component retirement, observed thresholds, zero-success handling, baselines, frozen correlation-cluster graph/split, and statistics.
- Extend the Registry Alpha workspace, CI, PostgreSQL, object storage, migrations, and test conventions for evaluation data.
- Implement manifest/task schemas and the provider adapter contract.
- Produce the first ten gold benchmark cases and grader golden tests.

### Days 6–8

- Extend the existing job leasing, heartbeat, retry-policy, and artifact modules for benchmark execution.
- Integrate the strong-default and low-cost model capabilities.
- Add static-analysis and hybrid adapter skeletons.
- Run reproducibility and reviewer-agreement checks on the pilot corpus.

### Days 9–10

- Execute an initial ten-case by three-capability harness dry run; these cases cannot enter the official corpus.
- Review invalid outputs, grader disagreements, cost attribution, latency, context usage, and trace completeness.
- Approve or revise the corpus production rubric.
- Re-estimate Phase 0 using measured case-authoring and execution throughput.

The Day 10 review is the first schedule checkpoint. It should change estimates if the corpus, runner environment, or candidate adapters are materially harder than assumed; it must not weaken the evidence standard to preserve a date.

---

## 24. Source-plan traceability

| Source concern | Implementation coverage |
|---|---|
| Intelligence and routing plane | Sections 6–9, 11, 14, and 16 |
| Evaluation plane and tournaments | Sections 5, 8, 10, 13, and 14 |
| Outcome plane and performance graph | Sections 2, 7.4–7.6, 9.4, 11, and 14 |
| Control plane and publisher | Sections 6, 9.1, 9.5, and Phase 3 in Section 10 |
| Runtime classes, gateway, and artifacts | Sections 6, 7.3, 9.3, 11, 12, and 15 |
| Economic plane and royalties | Required deferral in Section 3 and Phase 4 in Section 10; component royalties remain outside this plan until the ledger and dependency-attribution prerequisites exist |
| Capability taxonomy and manifest | Sections 3, 7.1–7.3, 8.3, and ADRs in Section 21 |
| MCP, HTTP, and SDK interfaces | Sections 9.1 and 9.6 |
| Tangle and Capability Studio | Phase 5 in Section 10 |
| Search and candidate retrieval | Sections 3, 6, 8.6, and 9.2 |
| Security, secrets, and hostile execution | Sections 5, 12, 13, and 15 |
| Observability and routing versioning | Sections 7.4, 13, 14, and 15 |
| Initial vertical and first experiment | Sections 3, 5, and 8 |
| Repository and technology stack | Sections 6.4 and 11 |
| Open-source/proprietary boundary | Section 19 |
| Marketplace and creator incentives | Phases 3–5 in Section 10 |
| Learned routing and online exploration | G4 in Section 5 and Phase 6 in Section 10 |

This traceability does not imply every source feature is approved for implementation. It records where each concern is either implemented, gated, or explicitly deferred.
