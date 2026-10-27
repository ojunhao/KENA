# Demo from your laptop

KENA runs on your laptop. Your personal AWS account provides two live services:

- **Claude on Amazon Bedrock**, which runs the conversation, and
- **Amazon SES**, which sends real emails to your inbox.

WhatsApp and RCS go through exactly the same journey code but are delivered to the **customer device emulator** at `/device`, a phone screen styled as WhatsApp or Google Messages (RCS). This avoids WhatsApp Business onboarding and the 5–7 day RCS agent registration. Expected cost: a few dollars of Bedrock usage; SES is fractions of a cent.

```
Dashboard (bank) ──▶ KENA on your laptop ──▶ Claude on Bedrock   (live)
                         │        └────────▶ Amazon SES → inbox   (live)
                         └─▶ WhatsApp / RCS / SMS → /device phone emulator
```

## 1. Laptop (15 min)

1. Install Python 3.12+ and Git.
2. Get the code:

   ```bash
   git clone https://github.com/ojunhao/KENA.git kena && cd kena
   python -m venv .venv
   source .venv/bin/activate                         # Windows: .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   cp .env.example .env                              # Windows: copy .env.example .env
   ```

## 2. AWS credentials and budget (10 min)

1. In your personal AWS account, create an IAM Identity Center or IAM user with `AdministratorAccess` (not the root user). Install the AWS CLI v2 and run `aws configure` with region `ap-southeast-1`; check with `aws sts get-caller-identity`.
2. Billing console → **Budgets** → monthly cost budget of US$10 with an email alert.

## 3. Claude on Bedrock (5 min)

Bedrock console → **Model access** → enable Anthropic **Claude Opus 5.5** (fill in the use-case form if asked). If access isn't granted, set `KENA_MODEL=anthropic.claude-opus-4-8` in `.env`.

## 4. SES (5 min)

SES console → **Identities** → **Create identity** → Email address → click the link SES emails you. Verify the sender and, if different, the customer address. The SES sandbox is fine.

## 5. Fill in `.env`

```ini
AWS_REGION=ap-southeast-1
KENA_LIVE=1
KENA_LIVE_CHANNELS=SES
KENA_AGENT=claude
KENA_MODEL=anthropic.claude-opus-5-5
KENA_CUSTOMER_NAME=Alex
KENA_CUSTOMER_EMAIL=you@example.com
KENA_SES_SENDER=you@example.com
KENA_NO_RESPONSE_SECONDS=90       # 30 while recording
```

Leave the WhatsApp, SMS and SNS lines empty. Comment out `KENA_BASIC_AUTH` if you only use the laptop; nothing is exposed to the internet in this mode.

Check everything:

```bash
python scripts/connect_local.py check
```

You should see ✓ for AWS credentials, Claude on Bedrock and both SES addresses.

## 6. Run

```bash
python -m app.run
```

- Dashboard (bank's intervention desk): http://127.0.0.1:8000. The header reads **LIVE · Email (SES) · OTHERS EMULATED** and **AGENT · CLAUDE ON BEDROCK**.
- Customer phone: http://127.0.0.1:8000/device, or **Customer device ↗** in the dashboard sidebar. It follows the most recent flagged transfer; add `?journey=txn-…` to pin one. To show it on a real phone, open the same URL from a phone on the same Wi-Fi with `python -m app.run --host 0.0.0.0` (this requires `KENA_BASIC_AUTH`).

Put the two windows side by side: dashboard on the left, phone on the right.

## 7. Walk through the journeys

A short follow-up delay makes the no-response path quick to see: set `KENA_NO_RESPONSE_SECONDS=30` and restart `app.run`.

1. **WhatsApp journey.** On the dashboard, choose **WhatsApp**, tick **Active call**, amount `8000`, **Create transfer**. The phone shows the alert with reply buttons, and a security-alert email is sent at the same time.
2. On the phone, type in your own words: "a police officer said my account is linked to money laundering and I must move my savings to a safe account". "KENA Bank is typing…" shows while Claude works; it answers and sends the consent message.
3. Tap **Pause & open case**. The dashboard shows the transfer paused and the case open, the phone shows the confirmation, and a mail notification drops in; the real email with the full conversation arrives in your inbox.
4. **RCS journey.** Choose **RCS**, create another transfer. The phone switches to the Google Messages style with suggested-reply chips. Don't reply: after 30 seconds the agent follows up on another channel (SMS on the phone, or a real email), then escalates to a human with an email record.
5. Finish on the dashboard's audit trail and outcome metrics.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `check` ✗ for Claude | Enable model access, or `KENA_MODEL=anthropic.claude-opus-4-8` |
| Replies get "I want to avoid guessing…" | The Claude call failed and KENA used the rules fallback; the terminal running `app.run` shows the error |
| No email | Both addresses must be verified in SES in the same region as `AWS_REGION`; check spam |
| Header says LOCAL CHANNELS | `KENA_LIVE=1` missing, or `.env` changed without restarting `app.run` |
| Phone shows nothing | It shows flagged transfers only; tick New payee and use an amount of at least S$5,000 |

## Turning WhatsApp or RCS on later

The adapters are implemented and contract-tested. Once a WhatsApp Business Account or an RCS agent is registered, add the channel to `KENA_LIVE_CHANNELS` (for example `SES,WHATSAPP`) and follow [setup-aws.md](setup-aws.md) for the IDs, the SNS topic and the tunnel (`scripts/connect_local.py setup` / `subscribe`).
