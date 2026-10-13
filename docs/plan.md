# Implementation plan

## Milestone 1 — Foundation (complete)

Isolated Git repository, Python environment, modular FastAPI application, synthetic input schema, architecture and policy design. No unrelated project modified.

## Milestone 2 — Local vertical slice (complete)

Synthetic S$8,000 event → risk context → simulated RCS/SMS outreach → conversational dispute → explicit consent → mock pause + scam case → live dashboard → simulated SES confirmation. SQLite persistence, idempotency and local audit included.

## Milestone 3 — Verification and AWS seams (complete locally)

50 passing backend/SDK contract tests, a passing live CLI demo, passing Chromium browser tests and desktop visual review, optional Bedrock and AgentCore decision adapters, explicit AWS communication adapter and Docker scaffold. Queued-simulator startup recovery is also implemented. AWS adapters are not live-tested. Docker image is not built here.

## Milestone 4 — Resilience and trusted cloud integration (next)

1. Extend implemented simulator restart recovery into an asynchronous cloud outbox worker with explicit failure/unknown states. Never blindly retry provider-accepted sends.
2. Replace demo identity with trusted customer authentication and operator roles. Protect inbound notifications with provider verification, replay protection and event schemas.
3. Add channel delivery callbacks, actual RCS↔SMS routing and SES outcomes. Prove real send/receive on approved test devices after account registration.
4. Evaluate Bedrock interpretation on negation, pressure, quoted instructions and multilingual replies. Add model guardrails/redaction and robust human handoff. Tool consent remains deterministic.
5. Run the decision entrypoint in AgentCore and validate session handling, IAM boundaries and observability. Broader orchestration can move there after local invariants are preserved.
6. Implement DynamoDB conditional state transitions and resumable idempotent mock-bank tool adapters for distributed deployment.
7. Add explicit no-response deadlines and human escalation; real review queue assignment, retention/redaction and audit access controls.

## Milestone 5 — Hackathon rehearsal (pending)

Rehearse one approved live communications path plus an unmistakably labelled simulator fallback. Add a short presenter script and failure-path demo, validate the deployed dashboard on laptop/mobile and package evidence of actual delivery. Provisioning/deployment/publication and paid AWS usage require project-owner approval first.

## Deferred scope

Real customer/bank data, payment execution, refunds/recovery, production risk modelling, WhatsApp onboarding, regulatory claims and real-bank APIs. The attachment's statistics were not independently validated and are not used as application claims.
