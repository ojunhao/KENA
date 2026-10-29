# Going live on AWS: WhatsApp, SES, Claude on Bedrock

This takes a personal AWS account from nothing to a deployed KENA that talks to your own phone over WhatsApp. Allow about two hours, most of it waiting on Meta and CloudFront. Use one region throughout; the examples use **ap-southeast-1 (Singapore)**.

What you end up with:

```
Your phone (WhatsApp) ⇄ AWS End User Messaging Social ─▶ SNS topic ─▶ https://<id>.cloudfront.net/webhooks/sns
                                                                         │
                     KENA on EC2 ──▶ Claude on Amazon Bedrock (tool use) ─┘
                        │ └──────▶ SES (formal email record)
                        └────────▶ End User Messaging SMS (optional fallback)
```

## 1. AWS account and CLI

1. Create or sign in to your AWS account. Turn on MFA for the root user and create an IAM user or IAM Identity Center user with `AdministratorAccess` for the hackathon.
2. Install the AWS CLI and run `aws configure` (or `aws configure sso`). Set the default region to `ap-southeast-1`.
3. Check: `aws sts get-caller-identity`.

## 2. Claude on Amazon Bedrock

1. In the Bedrock console (ap-southeast-1) open **Model access** and enable Anthropic **Claude Opus 5.5**. Singapore serves Claude through the global endpoint.
2. If Opus 5.5 access is not granted on your account yet, deploy with `--model anthropic.claude-opus-4-8` or `--model anthropic.claude-sonnet-5`, which are open to all Bedrock customers.
3. Optional local check, from the repository with the virtualenv active:

```bash
python -c "from anthropic import AnthropicBedrockMantle as C; r=C(aws_region='ap-southeast-1').messages.create(model='anthropic.claude-opus-5-5',max_tokens=200,messages=[{'role':'user','content':'Say hi'}]); print(r.content[-1].text)"
```

## 3. WhatsApp Business Account (WABA) through AWS End User Messaging Social

You need a phone number that is **not** already on WhatsApp and that can receive an SMS or voice call for the one-time verification. A virtual VoIP number works (this is the route teammates used). Your personal WhatsApp will be the *customer* phone.

1. Console → **AWS End User Messaging** → **Social messaging** → **Add WhatsApp phone number**. This launches Meta's embedded signup.
2. Log in with Facebook and create a **Meta Business portfolio**. Any website URL is accepted for the business profile; a placeholder site is fine for a sandbox.
3. Create a new **WhatsApp Business Account**, add the virtual number, choose **SMS** verification and enter the OTP it receives.
4. Grant AWS End User Messaging Social access when Meta asks, then finish in the AWS console. Leave the event destination empty for now; the deploy script sets it.
5. Find the IDs KENA needs:

```bash
python scripts/deploy_aws.py --list-whatsapp
# WABA My Bank Demo: --waba-id waba-0123...      (Meta id 1234...)
#   phone +65 8xxx xxxx: --wa-phone-number-id phone-number-id-abcd...
```

### Business-initiated messages

WhatsApp only lets a business message someone freely inside a 24-hour window that the customer opened. For the demo you have two options:

- **Quickest:** from your personal WhatsApp, send "Hi" to the business number shortly before you demo. KENA's alerts, reply buttons and consent buttons then work for 24 hours. KENA ignores that "Hi" if there is no open alert.
- **Proper:** in WhatsApp Manager create a **Utility** template named `kena_transfer_alert` (language English) and pass `--wa-template kena_transfer_alert`. Use this body and quick-reply buttons so KENA can recognise the taps:

  > KENA security check: we noticed a {{1}} transfer to {{2}} that is still pending. Did you make this transfer? Reply here in your own words. We will never ask for your password or OTP.
  >
  > Buttons: `Yes, it was me` · `No, it wasn't me` · `Someone told me to`

  KENA fills `{{1}}` with the amount and `{{2}}` with the payee. Templates are usually approved within minutes to hours.

If an alert goes out outside the window without a template, WhatsApp reports it as failed and KENA automatically re-sends it by SMS (when SMS is configured) and shows the failure on the dashboard.

## 4. Amazon SES (email record)

New accounts are in the SES sandbox, which can only send to verified addresses. That is enough for the demo.

1. SES console → **Identities** → **Create identity** → Email address. Verify the address you will send from (for example your own Gmail).
2. Verify the customer address too, if it is different.
3. Pass both as `--ses-sender` and `--email`.

## 5. SMS (optional)

SMS sending needs an origination identity and, for most countries including Singapore, sender registration. It is optional: KENA works with WhatsApp and email only, and SMS appears in the channel list when configured. If you have a number or sender ID in End User Messaging SMS, pass its ARN or ID as `--sms-identity` and add your phone as a verified destination while in the SMS sandbox. Two-way SMS replies can publish to the same SNS topic.

## 6. Deploy

From the repository root, after committing your changes (the script deploys `HEAD`):

```bash
python -m pip install -r requirements.txt
python scripts/deploy_aws.py \
  --phone +6591234567 --name "Alex" \
  --email you@example.com --ses-sender you@example.com \
  --wa-phone-number-id phone-number-id-abcd... --waba-id waba-0123... \
  --password 'pick-a-dashboard-password'
```

The script uploads the source to a private S3 bucket, creates the `kena` CloudFormation stack (`infra/kena.yaml`), waits for the HTTPS URL, subscribes the SNS webhook, and points your WABA's events at the topic. The first run takes 10–20 minutes because of CloudFront. Later runs reuse the password and only roll the code, typically in under two minutes:

```bash
git commit -am "Tweak prompts" && python scripts/deploy_aws.py --phone +6591234567
```

Open the printed URL and log in as `kena` with your password. Share that URL, with a separate password, with anyone who needs access.

Costs: the `t3.micro` instance and its public IPv4 address are about US$3 a week (CloudFront, SNS, SES and S3 stay within free allowances at demo volume); Claude on Bedrock is roughly US$0.05–0.10 per customer reply on Opus 5.5, and WhatsApp messages are cents. To keep the total near US$10:

- Develop locally with the rules agent and simulated channels (free); switch on `--agent claude` and live channels only when you need them.
- Deploy only for the judging window (6–12 Nov), then delete the stack.
- Set an AWS Budgets alert, and ask the organisers or your AWS partner team whether hackathon credits are available.

Remove everything with `aws cloudformation delete-stack --stack-name kena`, then delete the `kena-deploy-…` bucket.

## 7. Run locally against live channels (no deploy)

Useful while iterating on prompts. You need a public HTTPS URL for SNS; a quick tunnel works:

```bash
cp .env.example .env    # fill in the values, then:
set -a; source .env; set +a
python -m app.run --agent claude --live          # http://127.0.0.1:8000
cloudflared tunnel --url http://localhost:8000   # prints https://<random>.trycloudflare.com
aws sns subscribe --topic-arn "$KENA_SNS_TOPIC_ARN" --protocol https \
  --notification-endpoint https://<random>.trycloudflare.com/webhooks/sns
```

For this you need an SNS topic, which the stack creates; or create one yourself and attach the topic policy from `infra/kena.yaml` so End User Messaging can publish to it.

## 8. Walk through the journey

1. On the dashboard choose **WhatsApp**, tick **Active call**, enter `8000` and **Create transfer**. Your phone receives the alert with three buttons.
2. Reply in your own words, for example "a man from the police said my account is linked to money laundering and I must move my savings". Claude looks up the government-impersonation playbook, answers, and sends the fixed consent message with a **Pause & open case** button.
3. Tap it. The transfer pauses, a case opens, WhatsApp confirms, and the SES email arrives with the whole conversation.
4. To show orchestration, create another transfer and don't reply. After `--no-response-seconds` (90 by default) KENA follows up on another channel, then escalates to a human with an email record.

Troubleshooting: on the instance, use **SSM Session Manager** and run `journalctl -u kena -f`. Most issues are a WABA phone-number ID typo, the 24-hour window, an unverified SES address, or Bedrock model access.

## Serverless deployment (Lambda + CloudFront + WAF)

For accounts that block public IPs, default VPCs or open security groups, deploy KENA without any servers:

```bash
python -m pip install -r requirements.txt
python scripts/deploy_lambda.py --password 'choose-a-password'
```

The script builds a Linux package for Python 3.12 and creates, in us-east-1:

| Resource | Purpose |
| --- | --- |
| DynamoDB table `kena-journeys` | Journey state, on-demand billing |
| IAM role `kena-lambda` | DynamoDB on that table, Bedrock inference, CloudWatch Logs |
| Lambda function `kena` with a Function URL | The FastAPI app via Mangum; it rejects any request without CloudFront's secret origin header |
| AWS WAF web ACL `kena-waf` | Per-IP rate limit, Amazon IP reputation list, known bad inputs, common rule set |
| CloudFront distribution | HTTPS URL, no caching, WAF attached; judges sign in on KENA's /login page (user `kena`) |

Channels are emulated and Claude on Bedrock is live (enable model access first). Dashboard polling drives the no-response follow-ups, since Lambda has no background worker. Run the script again to deploy new code; settings and the password are kept. `python scripts/deploy_lambda.py --delete` removes the distribution, function and table.

Costs are small: Lambda, DynamoDB and CloudFront are per request, and AWS WAF is about US$5 a month for the web ACL plus US$1 per rule, prorated.
