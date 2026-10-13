# KENA — Knowledge-Enabled Notification Agent

A working local scam-intervention MVP for the AWS CDS Hackathon, by team **Scamurais**. All customer, payee, risk and banking data is synthetic. Default mode makes **zero AWS calls** and sends no real messages.

## Run locally

Python 3.12+ is required. The browser frontend has no build step or Node dependency.

```bash
cd kena
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Windows PowerShell: activate with `.\.venv\Scripts\Activate.ps1` instead of `source`; all remaining Python commands are the same. If activation is blocked, use `.\.venv\Scripts\python.exe` directly. Open **http://127.0.0.1:8000**. Interactive API reference: **http://127.0.0.1:8000/docs**.

## Demonstrate the complete journey

1. Click **Simulate S$8,000 transfer**. The new payee, unusual device and recent-payee signals produce a 100/100 synthetic risk score; KENA queues RCS outreach.
2. Click **Why flagged?**, or type that question, to inspect the context-based explanation.
3. Reply **I did not make this transfer**. KENA proposes a pause and scam-review case; the transfer is still pending.
4. Click **Confirm pause & open case**. The server validates the current customer, token, expiry, policy, transfer state and both approved actions. Mock `pause_transfer` and `create_scam_case` execute atomically.
5. Watch the queue, metrics, case card and audit trail update. An RCS confirmation and SES follow-up appear in the communication outbox, clearly marked **simulated**.
6. Check **Simulate RCS unavailable → SMS** before creating another transfer to demonstrate the SMS route. Try cancellation, verification, an ambiguous reply or repeated confirmation to inspect guardrail behavior.

The local agent supports common explicit replies, not unrestricted language understanding. Ambiguous wording results in clarification, never automatic banking action. Verification leaves funds pending for review; KENA cannot execute or release funds.

CLI demo, while the server is running:

```bash
python scripts/demo.py
```

SQLite is stored in `data/kena.db`; restart preserves journeys. Set `KENA_DB` to an alternate database path for a separate sandbox. Dashboard polling interval is 2.5 seconds. No destructive reset endpoint exists.

## Tests

```bash
python -m pytest -q
python scripts/verify_local.py
```

Optional real-browser test (Node 22+; server already running):

```bash
npm install
npx playwright install chromium
npm run test:browser
```

Set `KENA_TEST_URL` for a non-default local port. Browser checks create synthetic journeys without resetting existing data. AWS contract tests use Botocore stubs and fake test credentials; no network calls, recipients or cloud account are required.

`python scripts/verify_local.py --browser` starts an isolated temporary local server, runs the CLI journey and browser checks, then stops its own server. Queued simulated messages are recovered at application startup; failed sends are not retried. `requirements-lock.txt` captures all Python dependency versions used for verification.

## Containers

```bash
docker compose up --build
```

Compose binds only localhost and persists SQLite in a named volume. The container runs as a non-root user. Docker files are supplied but were not built in the development environment because Docker is unavailable. This local demo has no production authentication and must not be publicly exposed.

## Modules

| Component | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI event, dashboard and customer routes; static UI |
| `app/service.py` | Journey state machine, consent policy enforcement, atomic mock actions and outbox |
| `app/store.py` | SQLite serialized writes and durable journey aggregates |
| `app/agent.py` | Synthetic risk rules, local reply classifier, optional Bedrock Converse classifier |
| `app/agentcore.py` | AgentCore client and optional SDK runtime decision entrypoint |
| `app/messaging.py` | Local simulator plus explicit AWS End User Messaging / SES adapters |
| `app/static/` | Responsive intervention desk and customer simulator |
| `tests/` | Journey, concurrency, failure and AWS SDK contract checks |

## AWS integration status

AWS communication contracts are central to the journey, but local delivery is simulated. The AWS adapter uses `pinpoint-sms-voice-v2.send_text_message` with either an SMS origination identity or an RCS agent identity, and `sesv2.send_email` for follow-up. Provider **accepted** does not mean **delivered**. Local RCS fallback is a simulated pre-send choice, not a tested AWS carrier fallback.

Bedrock and AgentCore adapters are implemented and stub-tested. The default HTTP app is local rules + simulated communications. The optional launcher can use an approved model backend while keeping messaging simulated:

```bash
# Only after approval of paid AWS model/runtime calls and account setup.
# Use the standard AWS credential chain / IAM role; never put credentials in source.
export AWS_REGION='<approved-region>'
export KENA_MODEL_ID='<approved-model-or-inference-profile-id>'
python -m app.run --agent bedrock --enable-aws-calls

# Alternative: a previously approved and deployed AgentCore runtime.
export KENA_AGENTCORE_RUNTIME_ARN='<approved-runtime-arn>'
python -m app.run --agent agentcore --enable-aws-calls
```

No AWS live calls, resource creation, deployment or publication were performed. Real CDS wiring still requires approved test destinations, origination/brand registration, verified SES identities, inbound routing, identity/session binding, delivery receipts and an asynchronous outbox worker. The optional AgentCore entrypoint requires the `bedrock-agentcore` SDK, which is not part of the tested base requirements. See [architecture](docs/architecture.md), [implementation plan](docs/plan.md), [progress](docs/progress.md) and [API examples](docs/api.md).

## Guardrail boundaries

- Only `pause_transfer` and `create_scam_case` are allowed; tool authority stays outside model output.
- Consent is bound to customer, transaction, current proposal, five-minute expiry, both actions and policy version. A simple "yes" cannot execute tools.
- Banking state, case, command receipt, audit and outgoing messages commit together. Replayed IDs with changed payloads are rejected.
- A cancelled or replaced proposal cannot be confirmed. Amounts above the S$100,000 demo policy cap cannot be protected automatically.
- Event schemas accept only the built-in demo customer/payees, no phone/email/account fields. Conversation text is persisted: do not enter real customer data or secrets. Real PII filtering and retention controls are future work.
- `X-Demo-Customer` is a synthetic identity assertion, **not authentication**. Dashboard and simulator share a local trust boundary. Production identity, role separation, access control, rate limiting and secure inbound callbacks are not implemented.
- Failed sends are recorded without rolling back protection or blindly retrying. External bank/API atomicity and exactly-once message delivery are not claimed by this MVP.
