"""Deploy KENA to your AWS account: package source to S3, create/update the CloudFormation stack,
roll the instance, subscribe the SNS webhook, and (optionally) point your WhatsApp Business Account at it.

    python scripts/deploy_aws.py --list-whatsapp            # find your WABA id and phone-number id
    python scripts/deploy_aws.py --phone +6591234567 --email you@example.com --ses-sender you@example.com \
        --wa-phone-number-id phone-number-id-abc --waba-id waba-abc --password 'choose-a-password'

Uses your standard AWS credentials (aws configure / SSO). Creates billable resources: one EC2 instance,
a CloudFront distribution, an S3 bucket, an SNS topic. Delete with:  aws cloudformation delete-stack --stack-name kena
"""
import argparse
import io
import os
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = {".git", ".venv", "node_modules", "data", "__pycache__", ".pytest_cache"}


def package():
    """Tracked files at HEAD when this is a Git checkout, otherwise the working tree minus local state."""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        if subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip():
            print("! Uncommitted changes are not deployed; commit them first to include them.")
        data = subprocess.run(["git", "archive", "--format=tar.gz", "HEAD"], cwd=ROOT, capture_output=True, check=True).stdout
        return f"kena-{sha}.tar.gz", data
    except (OSError, subprocess.CalledProcessError):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for path in ROOT.rglob("*"):
                if path.is_file() and not EXCLUDE.intersection(path.relative_to(ROOT).parts):
                    archive.add(path, arcname=str(path.relative_to(ROOT)))
        return f"kena-{int(time.time())}.tar.gz", buffer.getvalue()


def ensure_bucket(s3, name, region):
    try:
        s3.head_bucket(Bucket=name)
    except ClientError:
        config = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
        s3.create_bucket(Bucket=name, **config)
        s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})


def stack_outputs(cfn, name):
    stack = cfn.describe_stacks(StackName=name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def stack_exists(cfn, name):
    try:
        return cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"] != "REVIEW_IN_PROGRESS"
    except ClientError:
        return False


def deploy_stack(cfn, name, parameters, exists):
    """Create or update the stack. On update, parameters left as None keep their previous values."""
    template = (ROOT / "infra" / "kena.yaml").read_text()
    params = [{"ParameterKey": k, "ParameterValue": str(v)} if v is not None else {"ParameterKey": k, "UsePreviousValue": True}
              for k, v in parameters.items() if v is not None or exists]
    kwargs = {"StackName": name, "TemplateBody": template, "Parameters": params, "Capabilities": ["CAPABILITY_IAM"]}
    if exists:
        try:
            cfn.update_stack(**kwargs)
        except ClientError as error:
            if "No updates are to be performed" not in str(error):
                raise
            return
        print("… updating stack", name)
        cfn.get_waiter("stack_update_complete").wait(StackName=name, WaiterConfig={"Delay": 15, "MaxAttempts": 120})
        return
    print("… creating stack", name, "(CloudFront takes 5-15 minutes)")
    cfn.create_stack(**kwargs, OnFailure="ROLLBACK")
    cfn.get_waiter("stack_create_complete").wait(StackName=name, WaiterConfig={"Delay": 20, "MaxAttempts": 120})


def roll_instance(ssm, instance_id):
    command = ssm.send_command(InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
                               Parameters={"commands": ["/usr/local/bin/kena-deploy"]})["Command"]["CommandId"]
    for _ in range(60):
        time.sleep(5)
        try:
            result = ssm.get_command_invocation(CommandId=command, InstanceId=instance_id)
        except ClientError:
            continue
        if result["Status"] in {"Success", "Failed", "Cancelled", "TimedOut"}:
            if result["Status"] != "Success":
                sys.exit("Instance update failed:\n" + result.get("StandardErrorContent", "")[-2000:])
            return
    sys.exit("Timed out waiting for the instance update")


def wait_healthy(url):
    for _ in range(90):
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=10) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(10)
    sys.exit(f"{url}/healthz did not become healthy; check the instance with SSM Session Manager (journalctl -u kena)")


def list_whatsapp(social):
    accounts = social.list_linked_whatsapp_business_accounts()["linkedAccounts"]
    if not accounts:
        print("No linked WhatsApp Business Accounts. Follow docs/setup-aws.md step 3 first.")
    for account in accounts:
        print(f"WABA {account['wabaName']}: --waba-id {account['id']}  (Meta id {account['wabaId']}, {account['registrationStatus']})")
        detail = social.get_linked_whatsapp_business_account(id=account["id"])["account"]
        for number in detail.get("phoneNumbers", []):
            print(f"  phone {number.get('displayPhoneNumber')}: --wa-phone-number-id {number['phoneNumberId']}")
        for destination in account.get("eventDestinations", []):
            print(f"  events -> {destination['eventDestinationArn']}")


def main():
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default=env("AWS_REGION", "ap-southeast-1"))
    parser.add_argument("--stack", default="kena")
    parser.add_argument("--list-whatsapp", action="store_true", help="Print linked WhatsApp accounts and phone-number ids, then exit")
    parser.add_argument("--phone", default=env("KENA_CUSTOMER_PHONE"), help="Test customer's WhatsApp number, E.164 (required on first deploy)")
    parser.add_argument("--email", default=env("KENA_CUSTOMER_EMAIL"))
    parser.add_argument("--name", default=env("KENA_CUSTOMER_NAME"))
    parser.add_argument("--ses-sender", default=env("KENA_SES_SENDER"))
    parser.add_argument("--wa-phone-number-id", default=env("KENA_WA_PHONE_NUMBER_ID"))
    parser.add_argument("--wa-template", default=env("KENA_WA_TEMPLATE"))
    parser.add_argument("--waba-id", default=env("KENA_WABA_ID"), help="AWS id of the linked WABA (waba-...), to route its events to KENA")
    parser.add_argument("--sms-identity", default=env("KENA_SMS_IDENTITY"))
    parser.add_argument("--agent", default=env("KENA_AGENT"), choices=["claude", "local"])
    parser.add_argument("--model", default=env("KENA_MODEL"))
    parser.add_argument("--instance-type", default=env("KENA_INSTANCE_TYPE"), help="EC2 size (template default t3.micro)")
    parser.add_argument("--no-response-seconds", type=int, default=env("KENA_NO_RESPONSE_SECONDS"))
    parser.add_argument("--password", default=env("KENA_DASHBOARD_PASSWORD"), help="Dashboard password (user 'kena'); reused if omitted on update")
    parser.add_argument("--simulated", action="store_true", help="Deploy without sending real messages")
    parser.add_argument("--live", action="store_true", help="Switch a simulated deployment back to live messaging")
    args = parser.parse_args()

    session = boto3.session.Session(region_name=args.region)
    if args.list_whatsapp:
        return list_whatsapp(session.client("socialmessaging"))
    account = session.client("sts").get_caller_identity()["Account"]
    cfn, s3 = session.client("cloudformation"), session.client("s3")
    bucket = f"{args.stack}-deploy-{account}-{args.region}"
    ensure_bucket(s3, bucket, args.region)
    key, data = package()
    s3.put_object(Bucket=bucket, Key=key, Body=data)
    print(f"✓ uploaded s3://{bucket}/{key}")

    exists = stack_exists(cfn, args.stack)
    if not exists and not (args.phone and args.password):
        parser.error("--phone and --password are required on first deploy")
    # Unspecified values keep their previous setting on update (or the template default on create).
    parameters = {"ArtifactBucket": bucket, "ArtifactKey": key, "Live": "0" if args.simulated else "1" if args.live or not exists else None,
                  "Agent": args.agent, "Model": args.model, "CustomerName": args.name, "CustomerPhone": args.phone,
                  "CustomerEmail": args.email, "SesSender": args.ses_sender, "WhatsAppPhoneNumberId": args.wa_phone_number_id,
                  "WhatsAppTemplate": args.wa_template, "SmsIdentity": args.sms_identity,
                  "NoResponseSeconds": args.no_response_seconds, "InstanceType": args.instance_type, "DashboardPassword": args.password}
    deploy_stack(cfn, args.stack, parameters, exists)
    outputs = stack_outputs(cfn, args.stack)
    if exists:
        print("… rolling instance to the new version")
        roll_instance(session.client("ssm"), outputs["InstanceId"])
    wait_healthy(outputs["Url"])
    print("✓ healthy:", outputs["Url"])

    sns = session.client("sns")
    subscribed = any(s["Endpoint"] == outputs["WebhookUrl"] for s in
                     sns.list_subscriptions_by_topic(TopicArn=outputs["EventsTopicArn"])["Subscriptions"])
    if not subscribed:
        sns.subscribe(TopicArn=outputs["EventsTopicArn"], Protocol="https", Endpoint=outputs["WebhookUrl"], ReturnSubscriptionArn=True)
        print("✓ SNS webhook subscribed (KENA confirms it automatically)")
    if args.waba_id:
        session.client("socialmessaging").put_whatsapp_business_account_event_destinations(
            id=args.waba_id, eventDestinations=[{"eventDestinationArn": outputs["EventsTopicArn"]}])
        print("✓ WhatsApp events routed to", outputs["EventsTopicArn"])
    print(f"\nDashboard: {outputs['Url']}  (user 'kena')\nWebhook:   {outputs['WebhookUrl']}\nTopic:     {outputs['EventsTopicArn']}")


if __name__ == "__main__":
    main()
