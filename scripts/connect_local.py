"""Connect your AWS account to a KENA running on your laptop. No hosting is created.

    python scripts/connect_local.py list                       # WABA and phone-number ids
    python scripts/connect_local.py setup                      # SNS topic + WhatsApp event routing
    python scripts/connect_local.py check                      # Bedrock, SES (and WhatsApp, SNS when live) readiness
    python scripts/connect_local.py test-whatsapp              # send yourself a WhatsApp message
    python scripts/connect_local.py test-email                 # send yourself a test email through SES
    python scripts/connect_local.py subscribe --url https://<tunnel>.trycloudflare.com

Reads .env (see .env.example) and your standard AWS credentials (aws configure).
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import boto3  # noqa: E402
from app.config import load_env_file  # noqa: E402

TOPIC_NAME = "kena-local-events"


def env(name):
    value = os.environ.get(name, "").strip()
    return value or None


def require(name):
    value = env(name)
    if not value:
        sys.exit(f"Set {name} in .env first.")
    return value


def topic_policy(topic_arn, account):
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "Owner", "Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{account}:root"},
         "Action": ["sns:Publish", "sns:Subscribe", "sns:GetTopicAttributes", "sns:SetTopicAttributes", "sns:ListSubscriptionsByTopic"],
         "Resource": topic_arn},
        {"Sid": "EndUserMessagingPublish", "Effect": "Allow",
         "Principal": {"Service": ["social-messaging.amazonaws.com", "sms-voice.amazonaws.com"]},
         "Action": "sns:Publish", "Resource": topic_arn, "Condition": {"StringEquals": {"aws:SourceAccount": account}}},
    ]}


def cmd_list(session, args):
    social = session.client("socialmessaging")
    accounts = social.list_linked_whatsapp_business_accounts()["linkedAccounts"]
    if not accounts:
        print("No linked WhatsApp Business Accounts in", session.region_name, "- complete step 3 of docs/setup-local-live.md.")
    for account in accounts:
        print(f"WABA {account['wabaName']} ({account['registrationStatus']})\n  KENA_WABA_ID={account['id']}")
        for number in social.get_linked_whatsapp_business_account(id=account["id"])["account"].get("phoneNumbers", []):
            print(f"  {number.get('displayPhoneNumber')}: KENA_WA_PHONE_NUMBER_ID={number['phoneNumberId']}")
        for destination in account.get("eventDestinations", []):
            print(f"  events -> {destination['eventDestinationArn']}")


def cmd_setup(session, args):
    account = session.client("sts").get_caller_identity()["Account"]
    sns = session.client("sns")
    topic = sns.create_topic(Name=TOPIC_NAME)["TopicArn"]  # idempotent
    sns.set_topic_attributes(TopicArn=topic, AttributeName="Policy", AttributeValue=json.dumps(topic_policy(topic, account)))
    print("✓ SNS topic ready:", topic)
    waba = args.waba_id or env("KENA_WABA_ID")
    if waba:
        session.client("socialmessaging").put_whatsapp_business_account_event_destinations(
            id=waba, eventDestinations=[{"eventDestinationArn": topic}])
        print("✓ WhatsApp events from", waba, "now go to the topic")
    else:
        print("! Set KENA_WABA_ID in .env (see `list`) and run setup again to route WhatsApp events.")
    print(f"\nPut this in .env:\nKENA_SNS_TOPIC_ARN={topic}")


def cmd_subscribe(session, args):
    topic = require("KENA_SNS_TOPIC_ARN")
    base = args.url.rstrip("/")
    endpoint = base + "/webhooks/sns"
    try:
        urllib.request.urlopen(base + "/healthz", timeout=10).read()
    except OSError as error:
        sys.exit(f"{base}/healthz is not reachable ({error}). Start `python -m app.run` and the tunnel first.")
    sns = session.client("sns")
    for sub in sns.list_subscriptions_by_topic(TopicArn=topic)["Subscriptions"]:
        # Old tunnel URLs die when the tunnel restarts; remove them so SNS stops retrying.
        if sub["Protocol"] in {"http", "https"} and sub["Endpoint"] != endpoint and sub["SubscriptionArn"].startswith("arn:"):
            sns.unsubscribe(SubscriptionArn=sub["SubscriptionArn"])
            print("- removed old endpoint", sub["Endpoint"])
    sns.subscribe(TopicArn=topic, Protocol="https", Endpoint=endpoint, ReturnSubscriptionArn=True)
    for _ in range(20):
        subs = sns.list_subscriptions_by_topic(TopicArn=topic)["Subscriptions"]
        if any(s["Endpoint"] == endpoint and s["SubscriptionArn"].startswith("arn:") for s in subs):
            print("✓ SNS is delivering to", endpoint)
            return
        time.sleep(2)
    sys.exit("Subscription not confirmed yet. Check that KENA_SNS_TOPIC_ARN in .env matches and the app log shows the webhook call.")


def cmd_test_whatsapp(session, args):
    from app.messaging import whatsapp_payload
    payload = whatsapp_payload({"body": "KENA test message. If you can read this, WhatsApp sending works."}, require("KENA_CUSTOMER_PHONE"))
    result = session.client("socialmessaging").send_whatsapp_message(
        originationPhoneNumberId=require("KENA_WA_PHONE_NUMBER_ID"), message=json.dumps(payload).encode(),
        metaApiVersion=env("KENA_META_API_VERSION") or "v20.0")
    print("✓ accepted by AWS:", result["messageId"])
    print("  Nothing on your phone? Send 'Hi' to the business number first (24-hour window) or use an approved template.")


def cmd_test_email(session, args):
    sender, recipient = require("KENA_SES_SENDER"), require("KENA_CUSTOMER_EMAIL")
    ses = session.client("sesv2")
    account = ses.get_account()
    print(f"SES in {session.region_name}: sending {'enabled' if account['SendingEnabled'] else 'DISABLED'}, "
          f"{'production' if account['ProductionAccessEnabled'] else 'sandbox (sender and recipient must both be verified)'}")
    for address in {sender, recipient}:
        try:
            identity = ses.get_email_identity(EmailIdentity=address)
            print(f"  {address}: {'verified' if identity['VerifiedForSendingStatus'] else 'NOT verified - click the link SES emailed you'}")
        except ses.exceptions.NotFoundException:
            print(f"  {address}: NOT an identity in this account/region - create it in the SES console")
    try:
        result = ses.send_email(FromEmailAddress=sender, Destination={"ToAddresses": [recipient]},
                                Content={"Simple": {"Subject": {"Data": "KENA test email"},
                                                    "Body": {"Text": {"Data": "If you can read this, KENA can send email through Amazon SES."}}}})
    except Exception as error:
        sys.exit(f"✗ SES rejected the email: {error}")
    print(f"✓ SES accepted the email (MessageId {result['MessageId']}) from {sender} to {recipient}.")
    print("  Not in the inbox within a minute? Check Junk/Spam. Mail sent through SES 'from' a company or webmail address")
    print("  can fail that domain's DMARC check and be junked or dropped; try the same address as sender and recipient.")


def cmd_check(session, args):
    ok = True

    def report(name, passed, detail=""):
        nonlocal ok
        ok = ok and passed
        print(("✓ " if passed else "✗ ") + name + (f": {detail}" if detail else ""))

    try:
        report("AWS credentials", True, session.client("sts").get_caller_identity()["Arn"])
    except Exception as error:
        report("AWS credentials", False, str(error))
        sys.exit(1)
    from app.config import Settings
    modes = Settings.from_env().channel_modes({"WHATSAPP", "SES"})
    whatsapp_live = modes["WHATSAPP"] == "live"
    if modes["SES"] != "live":
        report("KENA_LIVE", False, "set KENA_LIVE=1 (and KENA_LIVE_CHANNELS=SES) so email is really sent")
    if (env("KENA_AGENT") or "local") == "claude":
        try:
            from anthropic import AnthropicBedrockMantle
            model = env("KENA_MODEL") or "anthropic.claude-opus-5-5"
            AnthropicBedrockMantle(aws_region=session.region_name).messages.create(
                model=model, max_tokens=300, messages=[{"role": "user", "content": "Reply with the word ready."}])
            report("Claude on Bedrock", True, model)
        except Exception as error:
            report("Claude on Bedrock", False, f"{error}. Enable model access, or set KENA_MODEL=anthropic.claude-opus-4-8")
    if not whatsapp_live:
        print("– WhatsApp: shown on the device emulator (KENA_LIVE_CHANNELS); skipping WhatsApp and SNS checks")
    else:
        number = env("KENA_WA_PHONE_NUMBER_ID")
        try:
            account = session.client("socialmessaging").get_linked_whatsapp_business_account(id=require("KENA_WABA_ID"))["account"]
            ids = [n["phoneNumberId"] for n in account.get("phoneNumbers", [])]
            report("WhatsApp phone number", number in ids, number if number in ids else f"{number} not in {ids}")
            destinations = [d["eventDestinationArn"] for d in account.get("eventDestinations", [])]
            report("WhatsApp events -> SNS", env("KENA_SNS_TOPIC_ARN") in destinations, ", ".join(destinations) or "none; run setup")
        except SystemExit:
            report("WhatsApp account", False, "KENA_WABA_ID missing")
        except Exception as error:
            report("WhatsApp account", False, str(error))
    ses = session.client("sesv2")
    for name in ("KENA_SES_SENDER", "KENA_CUSTOMER_EMAIL"):
        address = env(name)
        if not address:
            report(name, False, "not set")
            continue
        try:
            report(f"SES {address}", ses.get_email_identity(EmailIdentity=address)["VerifiedForSendingStatus"], "verified")
        except Exception as error:
            report(f"SES {address}", False, f"not verified ({type(error).__name__})")
    topic = env("KENA_SNS_TOPIC_ARN")
    if not whatsapp_live:
        pass
    elif topic:
        subs = session.client("sns").list_subscriptions_by_topic(TopicArn=topic)["Subscriptions"]
        live = [s["Endpoint"] for s in subs if s["SubscriptionArn"].startswith("arn:")]
        report("SNS webhook subscription", bool(live), ", ".join(live) or "none yet; run subscribe after starting the tunnel")
    else:
        report("KENA_SNS_TOPIC_ARN", False, "not set; run setup")
    print("\nAll set." if ok else "\nFix the ✗ items above.")


def main():
    load_env_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["list", "setup", "check", "test-whatsapp", "test-email", "subscribe"])
    parser.add_argument("--url", help="Public tunnel URL, for subscribe")
    parser.add_argument("--waba-id", help="Overrides KENA_WABA_ID, for setup")
    parser.add_argument("--region", default=env("AWS_REGION") or "ap-southeast-1")
    args = parser.parse_args()
    if args.command == "subscribe" and not args.url:
        parser.error("subscribe needs --url")
    session = boto3.session.Session(region_name=args.region)
    {"list": cmd_list, "setup": cmd_setup, "check": cmd_check, "test-whatsapp": cmd_test_whatsapp, "test-email": cmd_test_email, "subscribe": cmd_subscribe}[args.command](session, args)


if __name__ == "__main__":
    main()
