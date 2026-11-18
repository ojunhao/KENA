"""Deploy KENA's decision agent to Amazon Bedrock AgentCore Runtime (direct code deployment, no Docker).

    python scripts/deploy_agentcore.py                    # create or update the runtime, then test it
    python scripts/deploy_agentcore.py --use-from-lambda  # also point the serverless KENA (scripts/deploy_lambda.py) at it

The runtime hosts the same Claude-on-Bedrock tool loop the app uses (app/agentcore.py). The app keeps
the journey state, policy gate and consent; the runtime only plans actions, which the app re-validates.
"""
import argparse
import io
import json
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
REGION = "us-east-1"
RUNTIME_NAME = "kena_scam_intervention"
ROLE_NAME = "kena-agentcore-runtime"
PACKAGES = ["bedrock-agentcore==1.26.0", "anthropic[bedrock]==1.15.0"]


def build_package():
    """Linux arm64 (Graviton) wheels for Python 3.12, KENA's app package and the entry point."""
    with tempfile.TemporaryDirectory() as target:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", target, "--platform", "manylinux2014_aarch64",
                        "--implementation", "cp", "--python-version", "3.12", "--only-binary=:all:", *PACKAGES], check=True)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in Path(target).rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    archive.write(path, path.relative_to(target))
            for path in (ROOT / "app").rglob("*.py"):
                if "__pycache__" not in path.parts:
                    archive.write(path, path.relative_to(ROOT))
            archive.write(ROOT / "kena_agentcore.py", "kena_agentcore.py")
        return buffer.getvalue()


def ensure_bucket(s3, name):
    try:
        s3.head_bucket(Bucket=name)
    except ClientError:
        s3.create_bucket(Bucket=name)
        s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})


def ensure_role(iam, account, bucket):
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
             "Action": "sts:AssumeRole", "Condition": {"StringEquals": {"aws:SourceAccount": account},
             "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{account}:*"}}}]}
    try:
        arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        created = False
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust), Tags=[{"Key": "project", "Value": "kena"}])["Role"]["Arn"]
        created = True
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="kena-agentcore", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["bedrock-mantle:CreateInference", "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{bucket}/*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams", "logs:DescribeLogGroups"],
         "Resource": f"arn:aws:logs:{REGION}:{account}:log-group:/aws/bedrock-agentcore/*"},
        {"Effect": "Allow", "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords"], "Resource": "*"},
        {"Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*", "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}},
    ]}))
    if created:
        time.sleep(12)  # IAM propagation
    return arn


def find_runtime(control):
    token = None
    while True:
        page = control.list_agent_runtimes(**({"nextToken": token} if token else {}))
        for runtime in page.get("agentRuntimes", []):
            if runtime["agentRuntimeName"] == RUNTIME_NAME:
                return runtime
        token = page.get("nextToken")
        if not token:
            return None


def deploy_runtime(control, role_arn, bucket, key, model):
    artifact = {"codeConfiguration": {"code": {"s3": {"bucket": bucket, "prefix": key}}, "runtime": "PYTHON_3_12", "entryPoint": ["kena_agentcore.py"]}}
    settings = {"agentRuntimeArtifact": artifact, "roleArn": role_arn, "networkConfiguration": {"networkMode": "PUBLIC"},
                "protocolConfiguration": {"serverProtocol": "HTTP"},
                "environmentVariables": {"KENA_ENABLE_AWS_CALLS": "explicitly-approved", "KENA_MODEL": model},
                "description": "KENA scam-intervention agent: Claude on Amazon Bedrock plans the next step (playbook lookup, message, "
                               "propose protection, verify, escalate); the KENA service re-validates every action."}
    existing = find_runtime(control)
    if existing:
        result = control.update_agent_runtime(agentRuntimeId=existing["agentRuntimeId"], **settings)
    else:
        result = control.create_agent_runtime(agentRuntimeName=RUNTIME_NAME, tags={"project": "kena"}, **settings)
    runtime_id = result["agentRuntimeId"]
    for _ in range(90):
        current = control.get_agent_runtime(agentRuntimeId=runtime_id)
        if current["status"] == "READY":
            return current["agentRuntimeArn"], current.get("agentRuntimeVersion")
        if current["status"].endswith("FAILED"):
            sys.exit(f"Runtime {current['status']}: {current.get('failureReason')}")
        time.sleep(10)
    sys.exit("Timed out waiting for the runtime to become READY")


def smoke_test(client, runtime_arn):
    """One real decision: a coached-transfer reply should lead the agent to propose protection."""
    context = {"trigger": "customer_reply", "text": "a police officer told me to move my savings to a safe account",
               "channels": ["SES", "WHATSAPP"], "customer_name": "Alex",
               "journey": {"id": "txn-smoketest001", "event": {"amount_cents": 800000, "payee": "New payee • 4821", "currency": "SGD"},
                           "transaction_status": "pending", "journey_status": "awaiting_customer", "channel": "WHATSAPP", "case": None,
                           "risk": {"level": "HIGH", "score": 90, "signals": ["Transfer at least S$5,000", "First transfer to this payee", "Active phone call during transfer"]},
                           "messages": [], "call_context": {"state": "active"}}}
    for attempt in range(3):
        try:
            result = client.invoke_agent_runtime(agentRuntimeArn=runtime_arn, runtimeSessionId="kena-deploy-smoke-test-" + uuid.uuid4().hex,
                                                 payload=json.dumps({"context": context}).encode(), contentType="application/json", accept="application/json")
            actions = json.loads(result["response"].read()).get("actions", [])
            return [a.get("type") for a in actions]
        except ClientError as error:
            print(f"  test attempt {attempt + 1} failed: {error.response['Error'].get('Message', error)}")
            time.sleep(15)
    return None


def use_from_lambda(session, runtime_arn):
    lam, iam = session.client("lambda"), session.client("iam")
    iam.put_role_policy(RoleName="kena-lambda", PolicyName="kena-invoke-agentcore", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime", "Resource": [runtime_arn, runtime_arn + "/*"]}]}))
    env = lam.get_function_configuration(FunctionName="kena")["Environment"]["Variables"]
    env.update({"KENA_AGENT": "agentcore", "KENA_AGENTCORE_RUNTIME_ARN": runtime_arn})
    lam.update_function_configuration(FunctionName="kena", Environment={"Variables": env})
    lam.get_waiter("function_updated_v2").wait(FunctionName="kena")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="anthropic.claude-opus-5-5")
    parser.add_argument("--use-from-lambda", action="store_true", help="Switch the deployed KENA Lambda to call this runtime (only if the test passes)")
    args = parser.parse_args()
    session = boto3.session.Session(region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    print("Account:", account)
    print("… building arm64 package")
    code = build_package()
    print(f"✓ package {len(code) / 1e6:.1f} MB")
    bucket, key = f"kena-agentcore-{account}-{REGION}", f"code/kena-agentcore-{int(time.time())}.zip"
    s3 = session.client("s3")
    ensure_bucket(s3, bucket)
    s3.put_object(Bucket=bucket, Key=key, Body=code)
    print(f"✓ uploaded s3://{bucket}/{key}")
    role_arn = ensure_role(session.client("iam"), account, bucket)
    print("✓ IAM role", ROLE_NAME)
    print("… creating/updating AgentCore runtime (a few minutes)")
    runtime_arn, version = deploy_runtime(session.client("bedrock-agentcore-control"), role_arn, bucket, key, args.model)
    print(f"✓ runtime READY: {runtime_arn} (version {version})")
    actions = smoke_test(session.client("bedrock-agentcore"), runtime_arn)
    if actions is None:
        sys.exit("✗ test invocation failed; check CloudWatch logs under /aws/bedrock-agentcore/runtimes/")
    print("✓ test decision:", actions)
    if args.use_from_lambda:
        if "propose_protection" not in actions:
            sys.exit("✗ not switching the Lambda: the test decision did not propose protection")
        use_from_lambda(session, runtime_arn)
        print("✓ KENA Lambda now plans through the AgentCore runtime")
    print(f"\nAgentCore runtime: {RUNTIME_NAME}\nARN: {runtime_arn}\nConsole: Amazon Bedrock AgentCore → Agents (us-east-1)")


if __name__ == "__main__":
    main()
