# KENA — Knowledge-Enabled Notification Agent

An agentic scam-intervention service for retail banks, built for the AWS CDS Agentic AI Partner Hackathon 2026 by team **Scamurais** (HCLTech).

When the bank's risk engine flags a pending transfer, KENA reaches the customer on **WhatsApp** (or RCS/SMS), holds a real conversation with them using **Claude on Amazon Bedrock**, recognises the scam pattern from a knowledge playbook, and asks for explicit one-tap consent to **pause the transfer and open a scam case**. It follows up on another channel if the customer goes quiet, escalates to a human when needed, and sends a formal **SES** email that carries the whole cross-channel conversation.

All customer, payee and banking data is synthetic, and banking actions are mocked. With no configuration KENA runs fully locally with simulated channels and a rules-based agent. Set a few environment variables and the same code sends real messages through AWS End User Messaging and SES.

## What the agent decides, and what it can't

| Claude decides (tool use) | The server enforces |
| --- | --- |
| What to say, in the customer's language | No links, phone numbers, credential requests or promises of refunds (`app/policy.py`) |
| Which scam typology fits (`search_scam_playbook`) | Only `pause_transfer` and `create_scam_case` exist, and only after consent |
| When to ask for protection (`propose_protection`) | Consent wording is fixed server text; only the WhatsApp/app button carries consent, never a typed "yes" |
| When the customer is genuine (`record_customer_verified`) | Five-minute consent bound to customer, transfer, proposal and policy version |
| When to escalate (`escalate_to_human`) and which channel to follow up on | Amount cap, transfer state and channel availability re-checked before anything is committed |

Every action is planned against a snapshot, re-validated against the current state, and committed atomically with the audit trail and outbox.

## Architecture

![KENA architecture on AWS](docs/architecture.png)

Deployed with `infra/kena.yaml`: EC2 behind a CloudFront HTTPS URL, an SNS topic for channel events, and IAM limited to WhatsApp, SMS, SES, Bedrock and its own config. See [architecture](docs/architecture.md).

## Run locally (no AWS)

Python 3.12+.

```bash
git clone https://github.com/ojunhao/KENA.git kena && cd kena
python -m venv .venv && source .venv/bin/activate     # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m app.run                                     # http://127.0.0.1:8000
```

Open the customer phone emulator at http://127.0.0.1:8000/device (or **Customer device ↗** in the sidebar) next to the dashboard. It renders WhatsApp, RCS (Google Messages) or SMS, and its buttons and composer act as the customer.

1. Choose **WhatsApp**, keep **New payee**, tick **Active call**, and click **Create transfer** (S$8,000 scores 90).
2. Click **Not my transfer** or type your own reply. KENA proposes protection; the transfer is still pending.
3. Click **Confirm pause & open case**. The transfer pauses atomically, a case opens, and WhatsApp and SES confirmations appear in the outbox.
4. Leave another transfer unanswered: after 90 seconds (`KENA_NO_RESPONSE_SECONDS`) KENA follows up on SMS, then escalates with an email record.

To use Claude while keeping channels simulated: `AWS_REGION=ap-southeast-1 python -m app.run --agent claude` (needs AWS credentials and Bedrock model access).

## Go live

**From your laptop:** follow **[docs/setup-local-live.md](docs/setup-local-live.md)**. Claude on Bedrock and SES run live from your AWS account; `KENA_LIVE_CHANNELS=SES` sends WhatsApp, RCS and SMS to the device emulator through the same code path. `python scripts/connect_local.py check` verifies the setup. Any channel can be switched to live once its registration is done.

**Hosted on AWS:** follow **[docs/setup-aws.md](docs/setup-aws.md)**, then one command creates an EC2 + CloudFront stack:

```bash
python scripts/deploy_aws.py --phone +6591234567 --email you@example.com --ses-sender you@example.com \
  --wa-phone-number-id phone-number-id-... --waba-id waba-... --password '...'
```

## Configuration

| Variable | Purpose |
| --- | --- |
| `KENA_LIVE=1` | Send through AWS. Without it every channel is simulated. |
| `KENA_LIVE_CHANNELS` | Optional subset sent through AWS, e.g. `SES`; the rest go to the `/device` emulator |
| `KENA_ALERT_EMAIL` | `1` (default) also emails the customer when a transfer is flagged |
| `KENA_AGENT` | `local` (default), `claude` (Bedrock), or `agentcore` (with `KENA_AGENTCORE_RUNTIME_ARN`) |
| `KENA_MODEL`, `KENA_EFFORT` | Bedrock model ID (default `anthropic.claude-opus-5-5`) and optional effort level |
| `KENA_CUSTOMER_PHONE`, `KENA_CUSTOMER_EMAIL`, `KENA_CUSTOMER_NAME` | The demo customer's real contact details; inbound messages are matched on the phone number |
| `KENA_WA_PHONE_NUMBER_ID`, `KENA_WA_TEMPLATE` | WhatsApp origination number and optional approved template |
| `KENA_SMS_IDENTITY`, `KENA_RCS_IDENTITY`, `KENA_SES_SENDER` | Other channels; unset channels are simply unavailable to the agent |
| `KENA_SNS_TOPIC_ARN` | Only signed SNS messages from this topic are accepted at `/webhooks/sns` |
| `KENA_BASIC_AUTH` | `user:password` for the dashboard; required on a public interface |
| `KENA_NO_RESPONSE_SECONDS`, `KENA_DB` | Follow-up delay and SQLite path |

See `.env.example`.

## Tests

```bash
python -m pytest -q                        # 109 tests; AWS calls are stubbed, no network
python scripts/verify_local.py --browser   # isolated server + CLI demo + Chromium checks (Node 22+, npm install)
```

Coverage includes the original journey, consent, concurrency and deletion invariants, plus WhatsApp payload contracts (text, interactive buttons, templates), SNS signature verification and subscription, inbound replies and button taps, stale or replayed consent, delivery receipts, WhatsApp-to-SMS failover, no-response follow-up and escalation, Claude's tool loop with policy feedback and refusal handling, agent text screening and dashboard authentication.

## Modules

| Component | Responsibility |
| --- | --- |
| `app/service.py` | Journey state machine, two-phase agent planning, consent, atomic mock actions, outbox, receipts, failover, no-response orchestration |
| `app/agent.py` | Risk rules, Claude on Bedrock tool loop (`ClaudeAgent`), deterministic `LocalAgent` fallback |
| `app/policy.py` | Action authorisation and screening of agent-written text |
| `app/playbook.py` | Scam typology knowledge the agent retrieves |
| `app/messaging.py` | WhatsApp (End User Messaging Social), SMS/RCS, SES adapters and the simulator |
| `app/inbound.py` | SNS signature verification; WhatsApp and two-way SMS event parsing |
| `app/main.py`, `app/run.py`, `app/config.py` | HTTP API, webhook, background worker, dashboard auth, environment settings |
| `app/agentcore.py` | Optional Bedrock AgentCore Runtime client and entrypoint |
| `scripts/connect_local.py` | Wires your AWS account to a laptop-hosted KENA (SNS topic, WhatsApp events, tunnel subscription, readiness check) |
| `infra/kena.yaml`, `scripts/deploy_aws.py` | CloudFormation stack and deploy script |

## Boundaries

- Banking tools are mocked; KENA never executes, releases, refunds or reverses funds, and verification leaves funds pending for review.
- Inbound identity is "message came from the enrolled phone number", which is fine for a demo, not for production. Real deployments need authenticated in-app consent.
- `X-Demo-Customer` on the web simulator is an identity assertion, not authentication. Protect any public deployment with `KENA_BASIC_AUTH`.
- Conversation text is stored in SQLite. Don't enter real customer data or secrets.
- A provider's "accepted" isn't "delivered"; KENA shows delivered/read only when WhatsApp reports it. Failed sends are re-routed once to SMS when configured, never blindly retried.
- Risk scores, the S$100,000 cap and the five-minute consent window are demo choices, not regulatory requirements.
