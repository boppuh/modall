# Capability Intelligence Exchange
## Evaluation, Routing, and Marketplace Architecture

**Status:** Revised technical plan  
**Revision:** Route-first architecture  
**Initial vertical:** Coding-agent capabilities  
**Potential wedge:** Swift / iOS engineering tasks  
**Supporting technologies:** MCP/HTTP, TangleML, containerized runtimes, x402/payment adapters

---

# 1. Executive Summary

Build a **performance market and intelligent routing layer for specialized machine capabilities**.

The system evaluates competing machine capabilities against real tasks, predicts which implementation is most likely to succeed under a buyer's constraints, routes execution, collects downstream outcomes, and uses those outcomes to improve future routing.

The marketplace, payment system, and Tangle-powered capability studio are important supporting systems, but they are **not the primary product**.

The primary product is the routing decision:

```text
route_task(task, constraints)
```

rather than:

```text
browse marketplace
    ↓
select provider
    ↓
invoke provider
```

The core long-term proprietary asset is the **capability-performance graph**:

```text
tasks
×
task characteristics
×
capabilities
×
capability versions
×
providers
×
cost
×
latency
×
real-world outcomes
```

As data accumulates, the platform should estimate:

```text
P(success | task, context, capability, version, provider)
```

and route each task to the implementation with the highest expected utility.

The marketplace exists to increase supply. The execution layer exists to generate usage. Usage generates labeled outcome data. Outcome data improves routing. Better routing creates more demand.

That is the core flywheel.

---

# 2. Why the Architecture Changed

A generic paid MCP marketplace is not sufficiently differentiated.

Several infrastructure layers are becoming commodities:

- model-provider routing
- public MCP registries
- semantic discovery of tools/services
- MCP interoperability
- HTTP APIs
- x402 payments
- stablecoin settlement
- container hosting
- generic agent directories

The platform therefore should not depend on owning any of those layers.

Instead, it should own the layer above them:

```text
Task understanding
      ↓
Capability taxonomy
      ↓
Independent evaluation
      ↓
Task-specific performance data
      ↓
Outcome telemetry
      ↓
Success prediction
      ↓
Intelligent routing
```

The strategic position is:

> **The platform decides which machine capability should receive a task.**

OpenRouter, MCP providers, x402 services, external APIs, Tangle workflows, hosted models, and agent services can all be suppliers underneath this layer.

---

# 3. Product Thesis

Agents increasingly have many ways to accomplish the same task.

A coding agent trying to review a Swift pull request might use:

- a frontier reasoning model
- a small fine-tuned Swift model
- a static analyzer
- a specialized concurrency agent
- a multi-agent workflow
- a Tangle graph
- a deterministic compiler/static-analysis pipeline
- an external MCP server
- an external paid API

The hard problem is no longer merely:

> "Can I find a service that claims to do this?"

The hard problem becomes:

> **"Which available implementation is most likely to complete this exact task successfully, within my price, latency, privacy, and quality constraints?"**

That is the product.

---

# 4. Strategic Moat

## 4.1 Weak / Commodity Moats

Do not rely on these for differentiation:

```text
MCP support
x402 support
USDC payments
API gateway
semantic search
public registry
basic marketplace listings
generic price-based routing
generic latency-based routing
```

## 4.2 Moderate Moats

Useful, but not sufficient alone:

```text
creator marketplace
provider integrations
developer SDKs
hosted execution
seller analytics
distribution
```

## 4.3 Strong Moats

Prioritize building:

```text
private benchmark datasets
task ontology
task-specific performance history
real-world outcome labels
routing models
creator reputation
version-specific capability history
benchmark-to-production correlation
```

## 4.4 Network-Effect Moat

```text
More buyers
   ↓
More executions
   ↓
More outcome data
   ↓
Better routing
   ↓
Higher buyer ROI
   ↓
More buyers

More buyers
   ↓
More provider revenue
   ↓
More creators
   ↓
More specialized capabilities
   ↓
Better routing options
   ↓
More buyers
```

Creators want access to demand. Agents want access to the best supply. The router improves as both sides grow.

---

# 5. Core Architectural Principles

1. **Route-first, marketplace-second.**
2. **Capabilities are the atomic unit, not APIs or models.**
3. **Every published capability version is immutable.**
4. **Every routing decision is observable and attributable.**
5. **Benchmark performance and production outcomes are distinct datasets.**
6. **Outcome telemetry is a first-class product requirement.**
7. **Tangle is one capability-production adapter, not the universal runtime.**
8. **Payments are adapters around an internal ledger, not the core architecture.**
9. **V1 routing should be deterministic before introducing ML.**
10. **The initial vertical should be narrow enough to measure real routing lift.**

---

# 6. What Is a Capability?

A **capability** describes an economically meaningful task that one or more implementations can perform.

Examples:

```text
software.code-review.concurrency
software.code-review.security
software.fix.test-failure
software.fix.build-failure
software.generate.unit-tests
finance.sec.extract-guidance
research.company.verify-claims
documents.extract-invoice
```

A capability is **not**:

```text
POST /v1/chat/completions
```

and is **not**:

```text
Claude Sonnet
```

Those are potential implementations or dependencies.

A capability is:

```text
"Review a Swift pull request for concurrency bugs."
```

Multiple implementations compete underneath it.

```text
               software.code-review.concurrency

              /               |               \
             /                |                \
            ↓                 ↓                 ↓

 Fine-tuned Swift 3B    Frontier-model       Multi-stage
       model                agent             workflow

      $0.004              $0.041              $0.095

   78% success          91% success          96% success
```

The router selects among them based on the task and constraints.

---

# 7. Capability Taxonomy

A first-class task ontology is required for meaningful routing.

## 7.1 Initial Software Taxonomy

```text
software
├── code-review
│   ├── correctness
│   ├── security
│   ├── performance
│   ├── concurrency
│   ├── maintainability
│   └── accessibility
│
├── debugging
│   ├── crash
│   ├── build-failure
│   ├── test-failure
│   ├── flaky-test
│   ├── memory
│   ├── concurrency
│   └── rendering
│
├── generation
│   ├── unit-tests
│   ├── integration-tests
│   ├── migration
│   ├── documentation
│   └── implementation
│
├── optimization
│   ├── database-query
│   ├── bundle-size
│   ├── build-time
│   ├── startup-time
│   └── runtime-performance
│
└── operations
    ├── dependency-audit
    ├── vulnerability-audit
    ├── ci-failure
    └── deployment-debug
```

## 7.2 Task Dimensions

A task has more than a taxonomy ID.

```yaml
task:
  type: software.code-review.concurrency
  language: swift
  framework:
    - swiftui
    - uikit
  platform: ios
  change_type: pull_request
  repository_size: medium
  context_requirement: multi_file
  codebase_age: mature
  build_system: xcode
```

Potential dimensions include:

```text
language
framework
platform
repository size
repository age
context size
change size
file count
task difficulty
task ambiguity
domain
tool requirements
runtime requirements
privacy requirement
latency class
risk class
```

These become routing features.

---

# 8. Capability Manifest

Tangle's component specification remains an internal workflow representation when Tangle is used.

The marketplace owns a separate capability-level manifest.

```yaml
apiVersion: intelligence.market/v1alpha1
kind: Capability

metadata:
  slug: swift-concurrency-review
  name: Swift Concurrency Review
  version: 2.4.1
  organization: examplelabs
  visibility: public

taskSupport:
  - task: software.code-review.concurrency
    languages:
      - swift
    frameworks:
      - swiftui
      - uikit
    platforms:
      - ios
      - macos
    repositorySizes:
      - small
      - medium

interface:
  executionMode: async
  inputs:
    type: object
    required:
      - repository_url
      - pull_request
    properties:
      repository_url:
        type: string
      pull_request:
        type: integer
  outputs:
    type: object
    required:
      - findings
    properties:
      findings:
        type: array
      patch:
        type: string
      report_url:
        type: string

source:
  type: tangle
  graphDigest: sha256:8d2f...
  pipelineId: pipe_01J...

runtime:
  backend: hosted_tangle
  timeoutSeconds: 600

pricing:
  model: per_call
  amount: "0.025"
  currency: USD

evaluation:
  suite: swift-concurrency-review-v3
  minimumScore: 0.85

permissions:
  requiredConnections:
    - github.oauth

settlement:
  creatorShareBps: 9000
  platformShareBps: 1000
```

Every published version is immutable.

```text
swift-concurrency-review@2.4.0
swift-concurrency-review@2.4.1
swift-concurrency-review@2.5.0
```

A routing decision always points to an exact version and deployment.

---

# 9. Flagship API: `route_task`

The primary external API should be task-centric.

## 9.1 Route Without Executing

```http
POST /v1/routes
```

Request:

```json
{
  "task": {
    "intent": "review a Swift pull request for concurrency bugs",
    "taxonomy": "software.code-review.concurrency",
    "attributes": {
      "language": "swift",
      "frameworks": ["swiftui"],
      "repository_size": "medium"
    },
    "inputs": {
      "repository": "github://example/project",
      "pull_request": 142
    }
  },
  "constraints": {
    "max_price_usd": 0.05,
    "max_latency_seconds": 90,
    "minimum_success_probability": 0.90
  },
  "policy": "best_value"
}
```

Response:

```json
{
  "routing_decision_id": "route_01K...",
  "selected_capability": {
    "id": "swift-concurrency-review",
    "version": "2.4.1",
    "deployment_id": "deploy_019..."
  },
  "prediction": {
    "success_probability": 0.943,
    "expected_cost_usd": 0.018,
    "expected_latency_seconds": 31
  },
  "alternatives": [
    {
      "capability_id": "general-code-review-agent",
      "version": "8.2.0",
      "success_probability": 0.887,
      "expected_cost_usd": 0.041
    }
  ],
  "reason_codes": [
    "high_swift_concurrency_score",
    "strong_medium_repo_performance",
    "within_latency_budget"
  ]
}
```

## 9.2 Route and Execute

```http
POST /v1/run
```

Developer SDK:

```typescript
const result = await intelligence.run({
  task: "Review this Swift PR for concurrency issues",
  inputs: {
    repository,
    pullRequest
  },
  constraints: {
    maxPrice: 0.05,
    maxLatencySeconds: 90
  }
});
```

The platform handles:

```text
task classification
    ↓
candidate discovery
    ↓
constraint filtering
    ↓
performance prediction
    ↓
provider selection
    ↓
payment / authorization
    ↓
execution
    ↓
result normalization
    ↓
outcome instrumentation
```

---

# 10. Agent-Facing MCP Interface

Do not expose hundreds of marketplace entries as hundreds of MCP tools.

Expose a small set of meta-tools:

```text
route_task
run_task
search_capabilities
get_capability
get_invocation
report_outcome
```

`route_task` is the primary interface.

`search_capabilities` exists for agents or humans that explicitly want to inspect supply.

`report_outcome` is strategically important because it feeds the data flywheel.

---

# 11. High-Level Architecture

```text
                             BUYERS

              Codex / Claude Code / Agents / Apps
                              │
                              │ MCP / SDK / HTTP
                              ▼
┌───────────────────────────────────────────────────────────────┐
│                     INTELLIGENCE GATEWAY                      │
│                                                               │
│ auth │ task parsing │ constraints │ idempotency │ metering   │
└──────────────────────────────┬────────────────────────────────┘
                               │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                         ROUTING PLANE                         │
│                                                               │
│ candidate retrieval                                          │
│ compatibility filtering                                      │
│ success prediction                                           │
│ cost / latency prediction                                    │
│ routing policy                                               │
│ exploration / fallback                                       │
└──────────────────────────────┬────────────────────────────────┘
                               │
                ┌──────────────┼───────────────┐
                │              │               │
                ▼              ▼               ▼
       Hosted Capability   External MCP    Model/API
           Runtime           / HTTP          Router
                │              │               │
                └──────────────┼───────────────┘
                               │
                               ▼
                         Task Result
                               │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                      OUTCOME TELEMETRY                        │
│                                                               │
│ accepted? │ retry? │ fallback? │ tests? │ human correction? │
└──────────────────────────────┬────────────────────────────────┘
                               │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                   CAPABILITY-PERFORMANCE DATA                 │
│                                                               │
│ benchmarks │ production results │ versions │ task features   │
└──────────────────────────────┬────────────────────────────────┘
                               │
                               ▼
                       Better Routing Models


                             SELLERS

                     Capability Creator
                              │
                 ┌────────────┴────────────┐
                 │                         │
                 ▼                         ▼
           External Provider         Capability Studio
                                          │
                                       Tangle
                                          │
                                      Publish
                 └────────────┬────────────┘
                              ▼
                     Capability Registry
                              │
                              ▼
                 Evaluation / Tournament
```

---

# 12. System Planes

The system should be explicitly separated into six planes.

## 12.1 Intelligence Plane

Owns:

- task classification
- taxonomy
- feature extraction
- candidate selection
- success prediction
- utility scoring
- routing
- fallbacks
- exploration

This is the primary proprietary product.

## 12.2 Evaluation Plane

Owns:

- benchmark datasets
- benchmark versioning
- hidden tests
- judges
- tournaments
- pairwise comparisons
- capability performance scores

## 12.3 Outcome Plane

Owns:

- production outcome events
- acceptance signals
- retries
- fallbacks
- test outcomes
- human corrections
- downstream success signals

## 12.4 Control Plane

Owns:

- users
- organizations
- capability metadata
- versions
- provider registration
- permissions
- pricing
- deployments
- creator accounts

## 12.5 Runtime Plane

Owns:

- hosted execution
- Tangle jobs
- pre-warmed containers
- external provider proxying
- artifacts
- async invocation lifecycle

## 12.6 Economic Plane

Owns:

- quotes
- credits
- x402 adapters
- settlement
- internal ledger
- provider balances
- platform fees
- refunds
- payouts

---

# 13. Evaluation Plane

Evaluation should be one of the largest engineering investments.

## 13.1 Benchmark Dataset Model

Every benchmark case should contain:

```text
task taxonomy
task dimensions
input artifacts
expected properties
grading strategy
difficulty
environment
hidden/public status
contamination risk
dataset version
```

Suggested schema:

```text
benchmark_suites
benchmark_suite_versions
benchmark_cases
benchmark_case_assets
benchmark_case_labels
benchmark_runs
benchmark_case_results
benchmark_scores
evaluation_judges
evaluation_judge_versions
```

## 13.2 Evaluation Methods

### Exact / Structural

- exact match
- JSON schema
- expected entities
- expected field values
- invariant validation

### Executable

For software tasks:

- compile succeeds
- unit tests pass
- hidden tests pass
- regression tests remain green
- static analysis passes
- benchmark performance improves

### Pairwise

Compare Capability A versus Capability B on the same hidden task.

### Model-Based

Use a judge model where deterministic evaluation is unavailable.

Model-based grading must record:

```text
judge provider
judge model
judge prompt version
temperature
evaluation timestamp
```

### Human

Use manual review for high-value benchmark subsets and disagreement resolution.

## 13.3 Coding Benchmark Principle

For coding tasks, prefer downstream execution over subjective text quality.

```text
Did the patch compile?
Did the hidden failing test pass?
Did existing tests continue to pass?
Was the root cause fixed?
Did the patch introduce a regression?
How many attempts were needed?
Did a human need to modify the patch?
```

---

# 14. Capability Tournaments

Every serious capability update should challenge current incumbents.

```text
New capability version
        ↓
Determine supported task categories
        ↓
Select hidden benchmark subset
        ↓
Select incumbent capabilities
        ↓
Run identical cases
        ↓
Measure success / cost / latency
        ↓
Compute pairwise performance
        ↓
Update performance frontier
        ↓
Allocate limited exploration traffic
```

Tables:

```text
tournament_runs
tournament_participants
capability_matchups
pairwise_results
ranking_history
performance_frontiers
exploration_allocations
```

Example result:

```text
software.code-review.concurrency

Best quality
  ConcurrencyExpert 2.4
  94.1% success

Best value
  SwiftGuard 1.8
  89.7% success
  $0.009/run

Fastest
  ActorCheck 0.9
  11s median

Best under $0.01
  SwiftGuard 1.8

Current challenger
  SwiftGuard 1.9-rc
```

Creator incentive:

```text
better capability
      ↓
better benchmark
      ↓
better routing position
      ↓
more traffic
      ↓
more revenue
```

---

# 15. Outcome Telemetry

Benchmark data alone is not enough.

The platform must explicitly collect real-world outcomes.

## 15.1 Invocation Lifecycle

```text
Task submitted
      ↓
Router selects capability
      ↓
Capability executes
      ↓
Result returned
      ↓
Agent accepts / rejects
      ↓
Retry?
      ↓
Fallback?
      ↓
Tests run?
      ↓
Human edits?
      ↓
Task completed?
      ↓
Outcome label
```

## 15.2 Outcome Event

```json
{
  "invocation_id": "inv_123",
  "event": "outcome_reported",
  "outcome": {
    "status": "success",
    "agent_accepted": true,
    "human_correction_required": false,
    "fallback_required": false,
    "completion_attempts": 1,
    "tests": {
      "passed": 42,
      "failed": 0
    }
  }
}
```

## 15.3 Data Tables

```text
task_instances
task_feature_snapshots
outcome_events
outcome_labels
human_corrections
fallback_events
retry_events
consumer_feedback
downstream_test_results
agent_acceptance_events
```

## 15.4 Outcome Collection SDK

```typescript
await intelligence.reportOutcome({
  invocationId,
  outcome: "success",
  testsPassed: true,
  humanCorrectionRequired: false
});
```

For coding integrations, automate collection where possible from:

- CI
- test suites
- build systems
- GitHub PR state
- code review state
- deployment state

Do not depend entirely on users manually rating outputs.

---

# 16. Routing Architecture

Routing should mature in stages.

## 16.1 Routing V0 — Deterministic

```text
1. classify task
2. retrieve compatible capabilities
3. filter unsupported inputs/platforms
4. apply permission/privacy constraints
5. filter by maximum price
6. filter by maximum latency
7. filter by minimum benchmark quality
8. rank remaining providers
```

Example scoring:

```python
score = (
    quality_score * 0.45
    + reliability_score * 0.25
    + latency_score * 0.15
    + price_score * 0.15
)
```

Policies:

```text
best_value
best_quality
cheapest
fastest
lowest_risk
preferred_provider
```

## 16.2 Routing V1 — Task-Specific Prediction

```python
predicted_success = model.predict({
    "task_type": task.type,
    "language": task.language,
    "framework": task.framework,
    "repository_size": task.repository_size,
    "context_size": task.context_size,
    "capability_id": capability.id,
    "capability_version": capability.version,
    "provider": deployment.provider
})
```

Then calculate expected utility:

```python
utility = (
    predicted_success * success_value
    - expected_cost
    - latency_penalty
    - failure_penalty
)
```

## 16.3 Routing V2 — Online Learning

Once production traffic is large enough:

- contextual multi-armed bandits
- controlled provider exploration
- task-specific routing models
- user-specific policies
- confidence-aware fallbacks
- ensemble execution
- budget allocation across multiple attempts
- dynamic provider suppression
- drift detection

Example decisions:

```text
Easy low-value task
→ cheap specialist

High-value task
→ best-quality capability

Low-confidence task
→ two inexpensive capabilities + adjudicator

Critical task
→ primary capability + independent validator

Provider degradation detected
→ route away automatically
```

---

# 17. Routing Model Versioning

Routing itself must be versioned.

Each decision stores:

```text
routing_model_version
candidate_set
feature_snapshot
predicted success
predicted cost
predicted latency
selected capability
selection reason
exploration flag
actual outcome
```

Tables:

```text
routing_models
routing_model_versions
routing_feature_snapshots
routing_decisions
routing_candidates
routing_predictions
routing_experiments
routing_outcomes
```

This enables offline replay.

Example question:

> "Would routing-model-v14 have outperformed v13 on last week's 80,000 tasks?"

---

# 18. Capability-Performance Graph

This is the most important data abstraction.

```text
Task Type
   │
   ├── Task Instance
   │      ├── task dimensions
   │      ├── input characteristics
   │      └── outcome
   │
   ├── Capability
   │      ├── Version
   │      │      ├── benchmark results
   │      │      ├── production outcomes
   │      │      └── deployments
   │      │
   │      └── Creator
   │
   └── Routing Decision
          ├── candidates
          ├── predictions
          └── selected implementation
```

Key entities:

```text
task_types
task_dimensions
task_instances
capabilities
capability_versions
capability_task_claims
deployments
benchmark_suites
benchmark_cases
evaluation_results
outcome_labels
routing_models
routing_decisions
capability_matchups
performance_frontiers
creators
```

Over time, the graph should answer:

```text
Which implementation works best:
- for this task?
- in this language?
- at this repository size?
- under this price ceiling?
- under this latency ceiling?
- for this user's risk tolerance?
- when context is incomplete?
- when the first provider fails?
```

---

# 19. Tangle's Revised Role

Tangle remains valuable, but it should be repositioned.

## Tangle Is

```text
one way to:
- build capabilities
- compose components
- execute batch workflows
- train specialized models
- evaluate pipelines
- package reproducible versions
```

## Tangle Is Not

```text
the universal runtime
the marketplace database
the router
the payment system
the task taxonomy
the evaluation authority
the only way to publish capabilities
```

## Supported Capability Sources

The platform should treat these as peers:

```text
Tangle graph
Hosted container
External MCP server
External HTTP API
Serverless function
Fine-tuned model
Model-router-backed agent
Deterministic analyzer
Multi-agent workflow
Human-assisted service
```

Every source is simply an implementation competing for routing traffic.

---

# 20. Capability Studio

The Tangle-powered visual studio should still exist, but move later in the roadmap.

Purpose:

> Give creators a fast way to build new capabilities that can immediately enter marketplace benchmarks and compete for routed traffic.

## Existing Tangle Concepts to Leverage

- container components
- graph components
- typed inputs/outputs
- pipeline composition
- artifacts
- component digests
- reusable pipelines
- execution logs
- Kubernetes execution

## Marketplace Extensions

### Interface

```text
Capability name
Description
Task taxonomy
Input JSON schema
Output JSON schema
Examples
Error schema
Sync / async
```

### Runtime

```text
Hosted batch
Hosted online
External endpoint

CPU
Memory
GPU
Region
Concurrency
Timeout
```

### Commercial

```text
Free
Per-call
Per-second
Per-token
Per-document

Price
Creator payout configuration
```

### Evaluation

```text
Applicable benchmark suites
Required quality threshold
Latency constraints
Test cases
Safety checks
```

### Publishing

```text
Validate
   ↓
Test
   ↓
Security scan
   ↓
Benchmark
   ↓
Tournament
   ↓
Publish
```

---

# 21. Runtime Classes

Not every capability should execute through Tangle.

## Class A — Hosted Batch Capability

Best for:

- repository audits
- document extraction
- research jobs
- dataset generation
- model training
- large scans
- video processing

```text
Router
  ↓
Hosted capability selected
  ↓
Create Tangle/batch job
  ↓
Return invocation ID
  ↓
Execute workflow
  ↓
Store artifacts
  ↓
Return normalized result
```

## Class B — Hosted Online Capability

Best for:

- classification
- embeddings
- small-model inference
- reranking
- lightweight extraction

```text
Router
  ↓
Pre-warmed container
  ↓
Result
```

Tangle may train/package/benchmark it, but does not execute every request.

## Class C — External MCP Provider

```text
Router
  ↓
Runtime gateway
  ↓
External MCP server
  ↓
Result
```

## Class D — External HTTP Provider

```text
Router
  ↓
Runtime gateway
  ↓
External HTTP API
  ↓
Result
```

## Class E — Model Router Provider

```text
Router
  ↓
Capability implementation
  ↓
Model router
  ↓
Model provider
```

This makes existing model routers suppliers rather than direct substitutes.

---

# 22. Capability Publisher

The publisher converts an implementation into a versioned marketplace candidate.

Publish sequence:

```text
1. Validate capability manifest
2. Validate input/output schemas
3. Resolve source artifacts
4. Resolve container or graph digests
5. Scan dependencies/images
6. Verify permissions
7. Execute examples
8. Run baseline benchmarks
9. Run applicable tournament cases
10. Freeze immutable version
11. Register deployment
12. Add to routing candidate index
13. Allocate exploration traffic if qualified
```

States:

```text
draft
validating
security_review
benchmarking
tournament
deploying
private
eligible
published
degraded
suspended
deprecated
```

Precise deployment identity:

```text
capability_id
capability_version
source_digest
container_image_digest
deployment_id
```

---

# 23. Search and Candidate Retrieval

Semantic search is useful, but only as the first stage.

```text
Task
 ↓
Taxonomy classification
 ↓
Structured filters
 ↓
Semantic candidate retrieval
 ↓
Compatibility filter
 ↓
Performance router
```

Start with:

```text
PostgreSQL
pgvector
PostgreSQL full-text search
```

Index:

- capability task claims
- descriptions
- input/output schemas
- languages/frameworks/platforms
- benchmark summaries
- permissions
- pricing
- runtime properties

Search retrieves candidates. It does **not** make the final routing decision.

---

# 24. Control-Plane Services

## Marketplace API

Responsibilities:

- users
- organizations
- creators
- capability metadata
- capability versions
- deployments
- pricing
- provider dashboards
- buyer dashboards
- discovery pages

Suggested stack:

```text
Python
FastAPI
PostgreSQL
SQLAlchemy
```

## Router Service

Responsibilities:

- task feature extraction
- candidate retrieval
- compatibility filtering
- prediction
- policy evaluation
- selection
- fallback planning
- exploration decisions
- route logging

This should be independently deployable.

## Evaluation Service

Responsibilities:

- dataset management
- benchmark scheduling
- isolated execution
- grading
- pairwise tournaments
- score calculation
- benchmark freshness
- regression detection

## Outcome Service

Responsibilities:

- receive production outcome events
- validate provenance
- connect outcomes to task/routing decisions
- aggregate capability statistics
- calculate production performance labels

## Tangle Adapter

Responsibilities:

```text
create/update pipeline
submit run
cancel run
read run status
retrieve artifacts
resolve graph digest
clone run
```

No other service should depend directly on Tangle database internals.

## Runtime Gateway

Responsibilities:

- MCP endpoint
- HTTP endpoint
- authentication
- authorization
- idempotency
- rate limiting
- payment verification
- provider proxying
- timeout enforcement
- result normalization
- traces

## Artifact Service

Use:

```text
S3-compatible object storage
OCI image registry
signed artifact URLs
```

---

# 25. Economic Plane

Payments should not define the product.

## 25.1 Internal Accounting

Maintain a conventional double-entry ledger regardless of payment rail.

Core concepts:

```text
buyer debit
platform receivable
provider payable
platform revenue
refund liability
network fee
payout
```

Tables:

```text
ledger_accounts
ledger_transactions
ledger_entries
quotes
payments
refunds
provider_balances
payouts
```

Do not reconstruct balances from blockchain events on demand.

## 25.2 Payment Adapters

Support adapters such as:

```text
marketplace credits
credit card / invoice
x402
stablecoin
enterprise prepaid balance
```

The router should not care which payment rail is used.

## 25.3 Rollout

### V1

```text
marketplace credits
fixed prices
no blockchain requirement
manual provider settlement if needed
```

### V2

```text
automated billing
provider balances
refunds
spending limits
x402 adapter
stablecoin settlement
```

### Later

```text
component revenue splits
programmable royalties
optional on-chain settlement
```

---

# 26. Component Royalties — Delayed Feature

Component royalties remain strategically interesting, but should not be part of V1.

Potential future example:

```text
ios.pull-request-review

uses:
  Swift AST parser
  concurrency classifier
  code search
  reasoning model
  report generator
```

Before implementing this, the platform needs:

- immutable dependency graphs
- usage attribution
- payout accounting
- refund allocation
- fraud protection
- maximum royalty depth
- circular dependency prevention
- clear IP/licensing rules

Roadmap:

```text
V1
single capability owner

V2
declared component dependency graph

V3
off-chain component revenue splits

V4
optional programmable/on-chain splits
```

---

# 27. Security Architecture

Public seller-provided execution must be treated as hostile.

## Container Isolation

Require:

- no privileged containers
- no Docker socket mounts
- non-root process
- read-only root filesystem
- temporary isolated workspace
- CPU limits
- memory limits
- wall-clock timeout
- process-count limit
- default-deny network egress
- approved-domain allowlists
- syscall restrictions
- hardened sandboxing
- image vulnerability scans
- immutable image digests
- software bill of materials

Potential isolation:

```text
gVisor
Kata Containers
Firecracker
```

## Secrets

Never store raw long-lived secrets in capability YAML.

```yaml
secrets:
  - reference: github.oauth
    mountAs: environment
    name: GITHUB_TOKEN
```

Runtime:

```text
Invocation identity
      ↓
Policy check
      ↓
Short-lived credential
      ↓
Ephemeral injection
      ↓
Execution
      ↓
Credential expiration
```

## Output Security

Add:

- structured-output validation
- secret scanning
- PII detection where appropriate
- max artifact size
- content-type validation
- malware scanning
- signed download URLs
- log redaction

---

# 28. Core Data Model

Minimum meaningful production schema:

```text
users
organizations
organization_members
creators

task_types
task_dimensions
task_instances
task_feature_snapshots

capabilities
capability_versions
capability_task_claims
capability_tags
capability_examples

deployments
provider_endpoints
health_checks

tangle_pipelines
component_versions
graph_versions

invocations
invocation_events
invocation_artifacts
invocation_errors

outcome_events
outcome_labels
human_corrections
fallback_events
retry_events
downstream_test_results

benchmark_suites
benchmark_suite_versions
benchmark_cases
benchmark_case_assets
evaluation_runs
evaluation_case_results
evaluation_scores
evaluation_judges

tournament_runs
tournament_participants
capability_matchups
pairwise_results
ranking_history
performance_frontiers

routing_models
routing_model_versions
routing_feature_snapshots
routing_decisions
routing_candidates
routing_predictions
routing_experiments

quotes
payments
refunds
ledger_accounts
ledger_transactions
ledger_entries
provider_balances
payouts

oauth_connections
secret_references
permission_grants
```

---

# 29. Observability

Use OpenTelemetry-compatible traces.

```text
task submission
  ↓
task classification
  ↓
candidate retrieval
  ↓
candidate filtering
  ↓
routing predictions
  ↓
routing selection
  ↓
quote/payment
  ↓
provider invocation
  ↓
runtime substeps
  ↓
result
  ↓
outcome events
```

Important dimensions:

```text
task taxonomy
capability
version
deployment
routing model version
predicted success
actual success
expected cost
actual cost
expected latency
actual latency
fallback count
retry count
```

---

# 30. Initial Vertical

Do not launch horizontally.

Start with coding-agent capabilities.

Initial taxonomy:

```text
software.code-review.security
software.code-review.correctness
software.code-review.concurrency
software.fix.test-failure
software.fix.flaky-test
software.fix.build-failure
software.optimize.database-query
software.generate.unit-tests
software.resolve.review-comments
software.audit.dependencies
```

## Optional Swift / iOS Wedge

```text
swift.fix-concurrency
swift.explain-crash
swift.fix-retain-cycle
swift.review-pull-request
swift.generate-tests
swift.fix-build
swift.fix-snapshot-test
swift.migrate-api
swift.check-accessibility
swift.optimize-scrolling
```

Advantages:

- clear task categories
- executable validation
- reproducible test environments
- objective pass/fail outcomes
- many possible competing implementations
- relatively easy to build private benchmark suites

---

# 31. First Routing Experiment

Before building a public marketplace, prove the routing thesis.

Choose one task family:

```text
swift.pull-request-review
```

Create 5–10 competing implementations.

Example implementations:

```text
1. Frontier model A
2. Frontier model B
3. Cheap general model
4. Fine-tuned Swift small model
5. Static-analysis workflow
6. Specialized concurrency agent
7. Multi-agent workflow
8. Tangle-composed pipeline
```

Create:

```text
100–500 benchmark tasks
```

Each task should contain:

```text
repository context
PR diff
known issue(s)
hidden expected findings
build/test validation where possible
task metadata
```

Compare:

### Baseline A

Always choose strongest general agent.

### Baseline B

Always choose cheapest valid provider.

### Baseline C

Always choose provider with highest global benchmark average.

### Experimental

Task-aware router.

The company thesis is validated only if the router materially improves metrics such as:

```text
task success rate
successful tasks per dollar
latency-adjusted success
fallback frequency
human correction rate
```

---

# 32. North-Star Metrics

Do not optimize early for:

```text
number of marketplace listings
number of tokens settled
number of wallet transactions
number of MCP servers indexed
```

Primary technical metrics:

```text
routing lift over best static provider
routing lift over cheapest provider
routing lift over strongest general model
task completion rate
cost per successful task
latency per successful task
fallback rate
retry rate
human correction rate
prediction calibration
benchmark-to-production correlation
percentage of tasks with outcome labels
```

## Example Success Case

```text
Strong default agent

Success:                 82%
Average attempt cost:    $0.061
Cost / successful task:  $0.074

Capability router

Success:                 91%
Average attempt cost:    $0.034
Cost / successful task:  $0.037
```

If the router consistently produces a result like this, the product has a compelling reason to exist.

---

# 33. Revised MVP Roadmap

## Phase 0 — Router Experiment

**Goal:** Validate that task-aware routing beats static provider selection.

Build:

- narrow task taxonomy
- benchmark dataset
- 5–10 capability adapters
- evaluation runner
- deterministic routing algorithm
- offline replay/evaluation
- basic routing dashboard

Do **not** build:

- public marketplace
- crypto
- seller onboarding
- visual capability builder
- complex payments

Deliverable:

> Evidence that routing improves successful task completion per dollar.

## Phase 1 — Closed Coding Router

Build:

- `route_task`
- `run_task`
- task classifier
- candidate retrieval
- constraint filtering
- deterministic router
- capability adapter interface
- invocation gateway
- outcome reporting API/SDK
- basic traces
- five to ten curated capabilities
- marketplace credits
- internal dashboard

Initial consumers:

- custom coding agent
- MCP client
- Codex-style workflow
- Claude Code-style workflow
- CI bot

## Phase 2 — Evaluation Platform

Build:

- private benchmark management
- benchmark versioning
- executable graders
- pairwise comparisons
- capability tournaments
- performance frontiers
- regression alerts
- production/benchmark correlation reporting

This strengthens the moat before opening supply.

## Phase 3 — Creator Platform

Build:

- creator accounts
- capability publishing
- immutable versions
- external MCP provider registration
- HTTP provider registration
- provider health checks
- provider analytics
- basic revenue accounting
- exploration traffic
- public capability profile pages

## Phase 4 — Paid Marketplace

Build:

- real billing
- quotes
- provider balances
- automated provider payouts
- refunds
- spending limits
- x402 payment adapter
- optional stablecoin settlement
- public discovery

The marketplace now sits on top of a routing product that already creates demand.

## Phase 5 — Capability Studio

Add the Tangle-powered creator workflow:

```text
Compose
  ↓
Test
  ↓
Benchmark
  ↓
Tournament
  ↓
Deploy
  ↓
Publish
  ↓
Receive routed traffic
```

This turns the platform from an aggregator into a capability-production ecosystem.

## Phase 6 — Learned Routing

Once sufficient real-world outcome data exists:

- train task-specific success predictors
- contextual bandit routing
- provider exploration
- drift detection
- personalized policies
- ensemble routing
- validator capabilities
- automatic fallback planning

---

# 34. Suggested Build Timeline

## Weeks 1–2

- taxonomy
- capability interface
- benchmark harness
- 5 competing implementations
- offline evaluation
- deterministic router

## Weeks 3–4

- `route_task`
- `run_task`
- MCP gateway
- invocation lifecycle
- outcome events
- internal dashboard
- initial benchmark expansion

At the end of Week 4, answer:

> Does routing beat using one default agent?

If no, revise the thesis before building a marketplace.

## Weeks 5–8

If validated:

- private benchmark service
- tournament framework
- more capabilities
- provider health monitoring
- routing experiments
- production telemetry
- first external alpha users

## Months 3–4

- third-party provider publishing
- immutable versions
- seller dashboard
- billing/credits
- public capability profiles
- limited marketplace

## Months 4–6

- Tangle capability studio
- hardened multi-tenant execution
- x402 adapter
- provider payouts
- learned routing experiments
- broader coding taxonomy

---

# 35. Explicitly Exclude From V1

Do not build initially:

- custom blockchain
- platform token
- decentralized execution
- permissionless validators
- tokenized capability ownership
- component royalties
- automatic on-chain revenue splits
- arbitrary unreviewed containers
- general-purpose all-category marketplace
- learned router without sufficient data
- auction pricing
- replacement for MCP
- replacement for Tangle
- hundreds of integrations

The V1 thesis is:

```text
Can task-specific evaluation + routing
choose better machine intelligence
than a static provider choice?
```

Everything else waits.

---

# 36. Recommended Repository Structure

```text
capability-intelligence/
├── apps/
│   ├── control-web/              # Internal + marketplace UI
│   ├── control-api/              # Capability/control plane API
│   ├── intelligence-gateway/     # HTTP + MCP entry point
│   ├── router/                   # Routing engine
│   ├── evaluator/                # Benchmark execution
│   ├── outcome-service/          # Outcome telemetry
│   └── publisher/                # Capability publishing
│
├── services/
│   ├── candidate-search/
│   ├── runtime-gateway/
│   ├── tangle-adapter/
│   ├── artifact-service/
│   ├── payment-service/
│   └── job-service/
│
├── packages/
│   ├── capability-manifest/
│   ├── task-taxonomy/
│   ├── routing-types/
│   ├── evaluation-sdk/
│   ├── typescript-sdk/
│   ├── python-sdk/
│   ├── mcp-server/
│   └── provider-sdk/
│
├── capabilities/
│   ├── frontier-agent-a/
│   ├── frontier-agent-b/
│   ├── swift-small-model/
│   ├── static-analysis/
│   ├── concurrency-agent/
│   └── tangle-workflow/
│
├── benchmarks/
│   ├── swift-concurrency/
│   ├── swift-build-failure/
│   └── pull-request-review/
│
├── tangle/
│   ├── upstream/
│   └── marketplace-extensions/
│
├── manifests/
│   └── examples/
│
└── infrastructure/
    ├── terraform/
    ├── kubernetes/
    └── local/
```

---

# 37. Recommended Initial Stack

## Backend

```text
Python
FastAPI
PostgreSQL
SQLAlchemy
Pydantic
```

## Search

```text
PostgreSQL full-text search
pgvector
```

## Queue / Jobs

Start:

```text
PostgreSQL-backed queue
or
Redis queue
```

Introduce Temporal only if durable orchestration outside Tangle becomes complex enough to justify it.

## Artifacts

```text
S3-compatible object storage
OCI registry
```

## Execution

```text
Docker locally
Kubernetes for hosted batch workloads
hardened sandbox for third-party containers
```

## Observability

```text
OpenTelemetry
structured logs
metrics
distributed traces
```

## SDKs

Prioritize:

```text
TypeScript
Python
MCP
```

---

# 38. Open-Source vs Proprietary Boundary

## Open Source

Potentially open-source:

- task/capability manifest format
- provider SDK
- TypeScript SDK
- Python SDK
- MCP adapter
- Tangle integration
- benchmark runner framework
- local developer environment
- selected public benchmark cases

## Proprietary

Keep proprietary:

- private benchmark datasets
- hidden benchmark cases
- production outcome dataset
- routing models
- task-specific performance histories
- performance graph
- fraud detection
- benchmark contamination detection
- routing exploration strategy
- marketplace economics
- commercial analytics

---

# 39. Competitive Architectural Boundary

The platform should be designed so existing infrastructure becomes supply.

```text
                    CAPABILITY INTELLIGENCE

                   Evaluation + Routing Layer
                           │
          ┌────────────────┼────────────────┐
          │                │                │
          ▼                ▼                ▼
    Model routers      MCP services     Hosted capabilities
          │                │                │
          ▼                ▼                ▼
      LLM APIs         x402 APIs        Tangle / Containers
```

This means:

- an inference router can be an underlying provider
- x402 infrastructure can be a payment/discovery adapter
- cloud infrastructure can host capabilities
- MCP registries can seed provider discovery
- Tangle can manufacture capability supply

The product remains useful regardless of which infrastructure layer wins.

---

# 40. Long-Term End State

The eventual system behaves like an **exchange for machine intelligence**.

An agent submits:

```text
Task:
Review this Swift PR for concurrency bugs.

Constraints:
≤ $0.05
≤ 60 sec
Prefer > 95% success probability
No persistent data retention
```

The platform evaluates the market:

```text
Capability                    Pred Success    Cost    Latency

SwiftGuard 4B                     92%         $0.008    13s
ConcurrencyAgent v7               97%         $0.031    38s
FrontierAgent                     94%         $0.047    29s
MultiAgentReviewer                98%         $0.089    74s
```

The router chooses:

```text
ConcurrencyAgent v7
```

because it satisfies the quality target and maximizes expected utility under the constraints.

The task executes. The resulting tests pass. That production outcome is recorded. The router becomes slightly better for the next comparable task.

At scale:

```text
millions of tasks
      ↓
millions of routing decisions
      ↓
millions of outcome labels
      ↓
task-specific performance graph
      ↓
better success predictions
      ↓
better routing
      ↓
more demand
```

That—not MCP compatibility, stablecoins, or a marketplace catalog—is the core company.

---

# 41. Revised Product Definition

The previous concept was:

> **A marketplace where developers build, publish, monetize, discover, and invoke machine capabilities.**

The revised concept is:

> **A performance market and intelligent routing network that continuously evaluates competing machine capabilities, predicts the best implementation for each task, executes that capability, and learns from the real-world outcome.**

The supporting ecosystem is:

```text
Tangle
  ↓
Capability creation

MCP / HTTP
  ↓
Interoperability

External providers / hosted runtimes
  ↓
Supply

Evaluation system
  ↓
Independent performance data

Router
  ↓
Task-specific provider selection

Marketplace
  ↓
Creator incentives + supply growth

Payments
  ↓
Economic settlement

Outcome telemetry
  ↓
Proprietary learning loop
```

The central technical question for the first month is therefore:

> **Can the system route coding tasks to specialized capabilities in a way that materially improves task completion per dollar over using one strong default agent?**

If the answer is yes, the rest of the marketplace architecture has a reason to exist.

---

# 42. Current Ecosystem Context

The architecture intentionally sits above existing infrastructure:

- **OpenRouter** already provides multi-provider model routing based on operational factors such as price, throughput, latency, reliability, and tool support. The proposed system routes at the *task/capability* layer rather than only the model-provider layer.
- **Coinbase Bazaar** already exposes semantic discovery and invocation of paid x402 endpoints through MCP. The proposed system treats such endpoints as candidate supply rather than relying on discovery/payment itself as the moat.
- **The MCP Registry** already provides standardized public MCP server metadata and a discovery API. It can be used as one supply-ingestion source.
- **TangleML** provides reusable containerized components and graph-based ML pipelines. It is useful as one capability-building and evaluation environment.

Reference documentation:

- https://openrouter.ai/docs/guides/routing/provider-selection
- https://docs.cdp.coinbase.com/api-reference/v2/rest-api/x402-facilitator/bazaar-mcp-server
- https://modelcontextprotocol.io/registry/about
- https://tangleml.com/docs/core-concepts/what-are-components/
