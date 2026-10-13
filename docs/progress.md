# Development progress — 13 October 2026

## Version 0.2 update

Editable SGD amounts with exact-cent handling; a compact HCLTech-inspired blue/purple interface without the marketing hero/subtexts; per-transfer deletion with confirmation, audit retention and replay prevention; simulated active-call context adding 25 risk points and targeted RCS/SMS outreach. Android-state normalization and observation freshness are implemented as isolated future-integration seams; native device access is not implemented.

**79 tests pass** including the original suite plus call-risk uplift, threshold crossing, no action without consent, unknown/missing/invalid call state, rejected call-content fields, Android normalization, freshness, arbitrary amount cents, deletion, invalidated consent and restart persistence. Updated live CLI and Chromium checks pass for exact-cent form entry, active-call outreach, protection, SMS/SES, delete cancellation, selected/last-transfer deletion, empty-state reset, HTML escaping and mobile layout. Desktop and mobile screenshots were inspected internally. Current source is maintained in the private `ojunhao/KENA` repository.

The following sections describe the original 0.1 delivery; its 50-test count and no-publication statement are historical.

## Delivered

- Isolated `kena` Git repository on `main`, self-contained Python application and responsive browser UI.
- S$8,000 synthetic-transfer journey: risk → RCS/SMS simulation → explanation/dispute → explicit confirmation → mock pause + scam case → dashboard + SES simulation.
- Versioned tool policy, five-minute transaction/customer-bound consent, no tool execution on ambiguous wording or unconfirmed dispute. Above-cap disputes route to human-review state without proposing unavailable automatic protection.
- Atomic mock state/case/outbox persistence, event and command fingerprints, concurrent confirmation protection, operator token redaction and queued-simulator restart recovery.
- Optional Bedrock Converse / AgentCore decision clients, AgentCore SDK entrypoint and AWS CDS transport adapters. No paid services activated.
- Setup, architecture, API, implementation plan, CLI demonstration, isolated verification harness, dependency lock and container scaffold.

## Verified

**50 tests passed** using Python 3.12.14. Coverage includes happy path, duplicate events/replies/confirmations, replay payload conflicts, concurrent confirmations, expired/replaced/cancelled consent, wrong customer, cross-journey tokens, action allow list, invalid amounts, low risk, policy amount cap, persistence, restart recovery, failed-send behavior and model/transport errors. AWS SDK request contracts for SMS/RCS, SES, Bedrock and AgentCore were tested with Botocore stubs; no AWS requests were sent.

The live HTTP CLI demonstration completed the S$8,000 sequence against an isolated Uvicorn process. Chromium browser checks passed for protection, SES outbox, audit, SMS routing, cancellation, HTML escaping and 390px mobile layout, with zero JavaScript errors. Desktop screenshot was visually inspected internally. A packaged Chromium binary was used because the normal Playwright browser download failed; it is a verification dependency, not bundled with KENA.

One third-party deprecation warning remains: the installed Starlette TestClient warns about future migration from HTTPX to HTTPX2. All current tests pass; production requests do not use TestClient.

## Boundaries and remaining work

The default agent is a deterministic local reply classifier, not a live LLM. CDS delivery and all bank actions are simulated. Bedrock/AgentCore adapters are implemented and stub-tested; AgentCore deployment and broader cloud orchestration are pending. SES has not sent real mail. The Docker scaffold has not been built because no Docker executable was available.

Next implementation priorities: trusted customer/operator identity and secure inbound routing; asynchronous cloud outbox and delivery reconciliation; approved live RCS/SMS/SES testing; AgentCore runtime integration; DynamoDB state; real human-review assignment and no-response escalation; language evaluation and PII/retention controls. An HTTP app must not be exposed publicly in its current demo-identity mode.

No code was published, no AWS resources deployed, no real customer or financial records used, no unrelated project changed. The downloadable archive excludes virtual environments, local databases, browser binaries and temporary screenshots. It contains source, documentation, tests and initialized local Git history.
