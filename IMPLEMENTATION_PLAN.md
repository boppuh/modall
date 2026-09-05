# Capability Intelligence Exchange — Delivery Roadmap

**Status:** Proposed execution plan  
**Source:** `capability_intelligence_exchange_revised_technical_plan.md`  
**Initial release:** MCP Registry Alpha  
**Planning posture:** Build durable foundations now; activate enterprise machinery when the product threat model requires it

---

## 1. Product decision

Modall is a task-level capability selection and routing product. The long-term proprietary loop is:

```text
task + constraints
  -> eligible capability versions
  -> predicted utility
  -> selected implementation
  -> execution
  -> attributable outcome
  -> better selection
```

The registry is the first release because the router needs trustworthy capability identity, version history, execution, and operational visibility. The registry is platform substrate, not the product moat.

Delivery sequence:

```text
M1  MCP Registry Alpha
    Connect, discover, version, enable, invoke, and inspect MCP tools

M2  Capability Comparison Pilot
    Run realistic iOS-shaped review tasks through multiple capabilities

M3  Routing Closed Alpha
    Route by a static policy, collect outcomes, and shadow task-aware selection

M4  Evidence-Gated Task-Aware Routing
    Activate learned selection only for validated traffic strata
```

The [dedicated Milestone 1 plan](MCP_REGISTRY_ALPHA_IMPLEMENTATION_PLAN.md) governs the first release.

## 2. Planning guardrails

### 2.1 Current release posture

Milestone 1 is a closed operator alpha with:

- pre-provisioned internal users;
- one deployed region;
- public, synthetic, or otherwise non-confidential invocation inputs;
- curated remote MCP servers over Streamable HTTP;
- no contractual enterprise availability or regulated-deletion promise; and
- a modular monolith, worker process, PostgreSQL, and web application.

Private iOS repositories are not processed by external model or MCP providers in Milestone 1. They enter a later controlled pilot only after provider agreements, data handling, and reviewer access are approved.

### 2.2 Controls that are foundational now

These are expensive or unsafe to retrofit and must be implemented in Milestone 1:

1. Every tenant-owned record is workspace-scoped through one repository boundary.
2. Capability versions and their executable bindings are immutable.
3. Runs pin the exact capability and connection versions used.
4. Remote endpoints are hostile: outbound network policy, SSRF defenses, HTTPS, bounded responses, and no invocation redirects are mandatory.
5. Credentials are secret-manager references and never enter ordinary API responses, logs, or database columns.
6. A durable dispatch fence prevents automatic re-execution after an uncertain upstream send.
7. Operator-visible remote text and JSON are bounded, escaped, and inert.
8. State changes produce typed audit events and correlation IDs.
9. Database changes use forward migrations, constraints, and restore-tested backups.

### 2.3 Architecture seams required now

Milestone 1 implements thin interfaces—not full enterprise subsystems—for:

- identity-provider integration;
- workspace authorization;
- content classification and retention metadata;
- secret providers;
- MCP protocol adapters;
- result storage;
- provider policy; and
- audit and telemetry sinks.

Each seam has a narrow contract and a default implementation appropriate to the current alpha. A future implementation may replace it without changing public resource identity or historical run lineage.

### 2.4 Promotion-triggered controls

The following controls remain off the Milestone 1 critical path. Their schemas must not be pre-built speculatively; implement them when the trigger is approved.

| Trigger | Required promotion work |
|---|---|
| Raw private or confidential customer content | Provider data agreements, data-classification policy, approved processing zones, content scanning, per-object envelope encryption, deletion verification, and incident response |
| External multi-tenant customer access | Database row-level security or equivalent independent enforcement, tenant-scoped operational tooling, enterprise identity lifecycle, invitation/admin flows, and isolation testing |
| Regulated or contractually guaranteed deletion | Per-object cryptographic erasure, recovery-key and backup handling, destruction evidence, legal retention holds, and restore verification |
| Large, binary, or multimodal inputs/results | Artifact service, short-lived subject-bound grants, immutable object versions, MIME-specific scanning, archive controls, bounded streaming, and revocation behavior |
| Enterprise authorization-revocation SLA | Qualified identity-source propagation measurement, authorization epochs, cache budgets, stream reauthorization, and fail-closed degraded identity behavior |
| Zero-downtime mixed-version deployments | Expand/contract compatibility roles, old-worker claim guards, contract-version lineage, and tested rollback drain procedures |
| Monetary provider liabilities | Per-credential quota, reservation, settlement, exact currency/rounding ledger, reconciliation, and billing controls |
| Task-aware production routing | Versioned taxonomy/features, benchmark evidence, route replay, task/result deletion cascade, outcome trust, model lineage, and stratum-specific activation gates |

An enterprise control can be pulled forward only with a named requirement, owner, threat, acceptance test, and schedule tradeoff.

## 3. Milestone map

| Milestone | User outcome | Entry condition | Exit condition | Planning range |
|---|---|---|---|---:|
| M1 — Registry Alpha | Internal operators connect, discover, version, invoke, and diagnose curated MCP tools | Stack and protocol decisions approved | Reference journey and release gates pass | 22–28 person-weeks |
| M2 — Comparison Pilot | Reviewers compare multiple capabilities on realistic iOS-shaped tasks | M1 run lineage stable; public/synthetic corpus approved | Reproducible comparison report and reviewer calibration | 12–18 person-weeks |
| M3 — Routing Closed Alpha | Clients receive one selected implementation and can report an outcome | M2 establishes eligible capabilities and a static baseline | Static routing reliable; task-aware policy runs in shadow | 18–26 person-weeks |
| M4 — Task-Aware Activation | Validated traffic strata use task-aware selection | Representative holdout, outcome quality, privacy, and rollback gates pass | Measured lift without violating cost/privacy constraints | Evidence-dependent |

Ranges are planning inputs, not commitments. Each milestone is re-estimated from its accepted task inventory before kickoff.

## 4. Milestone 2 — capability comparison pilot

The first comparison workload is source-level Swift/iOS concurrency pull-request review. It is iOS-shaped because that is the available domain expertise and corpus, not because Modall is an iOS-only product.

### Scope

- public, licensed, synthetic, or explicitly sanitized Swift tasks;
- UIKit, SwiftUI, structured-concurrency, isolation, cancellation, and lifecycle patterns;
- five to eight curated implementations spanning frontier models, smaller specialists, static analysis, and deterministic tooling;
- one normalized review-result schema with findings, severity, location, category, confidence, and evidence;
- blinded primary and secondary review with a qualified adjudicator for disagreements;
- immutable benchmark, implementation, prompt/configuration, result, cost, and grader lineage; and
- a strongest-static-policy baseline for the routing milestone.

### Non-goals

- training or hosting a local model;
- sending raw internal repositories to an unapproved provider;
- claiming globally calibrated success probability;
- public leaderboards or seller claims; and
- activating task-aware production routing from the pilot alone.

### Exit evidence

- the same corpus snapshot reproduces the report;
- reviewer agreement and adjudication load are measured;
- each implementation has sufficient coverage for the claimed task slice;
- cost, latency, and success are reported separately;
- limitations and unrepresented strata are explicit; and
- a go/no-go decision selects the M3 static baseline and shadow candidates.

## 5. Milestone 3 — routing closed alpha

M3 introduces `route_task`, `run_task`, and typed outcome collection over curated implementations. Production starts with the strongest eligible static policy. Task-aware selection runs in shadow until representative evidence supports activation.

Core additions:

- versioned task taxonomy and pre-execution feature schema;
- immutable routing decisions and candidate snapshots;
- provider/model/CLI adapters behind one execution interface;
- per-request policy, price, latency, and privacy eligibility;
- typed human and integration outcomes with invocation lineage;
- deterministic route replay while retained data remains available; and
- shadow evaluation, rollback, and segment-specific activation controls.

Private/internal traffic is a separate processing stratum. It cannot inherit approval from public or synthetic evaluation.

## 6. Architecture direction

Use three deployables through the closed alpha:

```text
Web application
      |
Control-plane API ---- PostgreSQL
      |
Durable worker ------ secret provider / approved remote services
```

Domain modules:

- identity and workspaces;
- registry and immutable capability versions;
- execution and run history;
- evaluation and evidence;
- routing and outcomes;
- policy and retention; and
- audit and operations.

Do not split these modules into independent network services until scale, security isolation, or release ownership requires it.

## 7. Decision and review policy

Architecture review classifies findings into:

- **release blocker:** violates the current threat model, corrupts history, exposes credentials/tenants, or duplicates an external side effect;
- **required seam:** a small interface or data field needed to avoid an expensive migration;
- **promotion control:** valid enterprise work tied to a future trigger; or
- **backlog:** beneficial but not required for the accepted milestone outcome.

Automated review findings do not become requirements automatically. A finding that adds a subsystem, changes the threat model, or moves the delivery range requires an explicit product/engineering decision.

## 8. Program definition of done

For each milestone:

- the user outcome works end to end;
- scope and threat-model assumptions are testable and documented;
- security boundaries appropriate to admitted data are independently reviewed;
- migrations, rollback, telemetry, and incident ownership are exercised;
- estimates match the accepted task inventory;
- deferred controls retain a named activation trigger; and
- follow-on work consumes stable public/domain contracts instead of bypassing them.

