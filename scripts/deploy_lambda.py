"""Serverless deployment: AWS Lambda (Function URL) behind Amazon CloudFront with AWS WAF, state in DynamoDB.

No EC2, VPC, public IP or security group is used, so it fits locked-down landing-zone accounts.

    python scripts/deploy_lambda.py --password 'choose-a-password'            # first deploy
    python scripts/deploy_lambda.py                                            # redeploy code, keep settings

Channels are emulated by default (the /device page plays the customer); Claude on Amazon Bedrock is live.
Remove with: python scripts/deploy_lambda.py --delete
"""
import argparse
import io
import json
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
NAME = "kena"
RUNTIME = "python3.12"
PACKAGES = ["fastapi==0.143.0", "anthropic[bedrock]==1.15.0", "cryptography==50.0.2", "mangum==0.19.0", "boto3==1.43.112"]
CACHING_ALL_VIEWER_EXCEPT_HOST = "b689b0a8-53d0-40ab-baf2-68738e2966ac"


def build_package():
    """Linux x86_64 wheels for the Lambda runtime, plus the app package."""
    with tempfile.TemporaryDirectory() as target:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", target, "--platform", "manylinux2014_x86_64",
                        "--implementation", "cp", "--python-version", "3.12", "--only-binary=:all:", *PACKAGES], check=True)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in Path(target).rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    archive.write(path, path.relative_to(target))
            for path in (ROOT / "app").rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    archive.write(path, path.relative_to(ROOT))
        return buffer.getvalue()


def ensure_table(dynamodb, table):
    try:
        dynamodb.describe_table(TableName=table)
    except dynamodb.exceptions.ResourceNotFoundException:
        dynamodb.create_table(TableName=table, BillingMode="PAY_PER_REQUEST", AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                              KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}], Tags=[{"Key": "project", "Value": NAME}])
        dynamodb.get_waiter("table_exists").wait(TableName=table)
    return dynamodb.describe_table(TableName=table)["Table"]["TableArn"]


def ensure_role(iam, table_arn):
    role_name = f"{NAME}-lambda"
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        created = False
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps(trust), Tags=[{"Key": "project", "Value": NAME}])["Role"]["Arn"]
        created = True
    iam.attach_role_policy(RoleName=role_name, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    iam.put_role_policy(RoleName=role_name, PolicyName=f"{NAME}-runtime", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Scan"], "Resource": table_arn},
        {"Effect": "Allow", "Action": ["bedrock-mantle:CreateInference", "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["ses:SendEmail"], "Resource": "*"},
    ]}))
    if created:
        time.sleep(12)  # IAM propagation before Lambda can assume the new role
    return arn


def ensure_function(lam, role_arn, code, env):
    try:
        current = lam.get_function_configuration(FunctionName=NAME)
        env = {**current.get("Environment", {}).get("Variables", {}), **{k: v for k, v in env.items() if v is not None}}
        lam.update_function_code(FunctionName=NAME, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=NAME)
        lam.update_function_configuration(FunctionName=NAME, Environment={"Variables": env}, Timeout=90, MemorySize=1024)
    except lam.exceptions.ResourceNotFoundException:
        if not env.get("KENA_BASIC_AUTH"):
            sys.exit("--password is required on first deploy")
        lam.create_function(FunctionName=NAME, Runtime=RUNTIME, Role=role_arn, Handler="app.lambda_handler.handler", Code={"ZipFile": code},
                            Timeout=90, MemorySize=1024, Architectures=["x86_64"], Environment={"Variables": {k: v for k, v in env.items() if v is not None}},
                            Tags={"project": NAME})
    lam.get_waiter("function_updated_v2").wait(FunctionName=NAME)
    return lam.get_function_configuration(FunctionName=NAME)["Environment"]["Variables"]


def ensure_url(lam):
    try:
        url = lam.get_function_url_config(FunctionName=NAME)["FunctionUrl"]
    except lam.exceptions.ResourceNotFoundException:
        url = lam.create_function_url_config(FunctionName=NAME, AuthType="NONE")["FunctionUrl"]
    # The app itself rejects requests without CloudFront's secret origin header.
    for statement, extra in (("url", {"Action": "lambda:InvokeFunctionUrl", "FunctionUrlAuthType": "NONE"}),
                             ("invoke", {"Action": "lambda:InvokeFunction", "InvokedViaFunctionUrl": True})):
        try:
            lam.add_permission(FunctionName=NAME, StatementId=f"{NAME}-public-{statement}", Principal="*", **extra)
        except lam.exceptions.ResourceConflictException:
            pass
    return url.split("//", 1)[1].rstrip("/")


def ensure_waf(waf):
    name = f"{NAME}-waf"
    for acl in waf.list_web_acls(Scope="CLOUDFRONT", Limit=100)["WebACLs"]:
        if acl["Name"] == name:
            return acl["ARN"]

    def managed(priority, rule):
        return {"Name": rule, "Priority": priority, "OverrideAction": {"None": {}},
                "Statement": {"ManagedRuleGroupStatement": {"VendorName": "AWS", "Name": rule}},
                "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": rule}}
    rules = [
        {"Name": "rate-limit-per-ip", "Priority": 0, "Action": {"Block": {}},
         "Statement": {"RateBasedStatement": {"Limit": 3000, "AggregateKeyType": "IP"}},
         "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": "rate-limit-per-ip"}},
        managed(1, "AWSManagedRulesAmazonIpReputationList"),
        managed(2, "AWSManagedRulesKnownBadInputsRuleSet"),
        managed(3, "AWSManagedRulesCommonRuleSet"),
    ]
    return waf.create_web_acl(Name=name, Scope="CLOUDFRONT", DefaultAction={"Allow": {}}, Rules=rules,
                              VisibilityConfig={"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": name},
                              Tags=[{"Key": "project", "Value": NAME}])["Summary"]["ARN"]


def ensure_cache_policy(cloudfront):
    """No caching in practice (TTL 0) while letting the Authorization header reach the origin for basic auth."""
    name = f"{NAME}-no-cache-with-auth"
    for item in cloudfront.list_cache_policies(Type="custom").get("CachePolicyList", {}).get("Items", []):
        if item["CachePolicy"]["CachePolicyConfig"]["Name"] == name:
            return item["CachePolicy"]["Id"]
    config = {"Name": name, "MinTTL": 0, "DefaultTTL": 0, "MaxTTL": 1, "ParametersInCacheKeyAndForwardedToOrigin": {
        "EnableAcceptEncodingGzip": True, "EnableAcceptEncodingBrotli": True,
        "HeadersConfig": {"HeaderBehavior": "whitelist", "Headers": {"Quantity": 2, "Items": ["Authorization", "X-Demo-Customer"]}},
        "CookiesConfig": {"CookieBehavior": "none"}, "QueryStringsConfig": {"QueryStringBehavior": "all"}}}
    return cloudfront.create_cache_policy(CachePolicyConfig=config)["CachePolicy"]["Id"]


def ensure_distribution(cloudfront, origin_host, secret, cache_policy, waf_arn):
    comment = f"{NAME}-serverless"
    for item in cloudfront.list_distributions().get("DistributionList", {}).get("Items", []):
        if item.get("Comment") == comment:
            return item["DomainName"]
    config = {
        "CallerReference": f"{NAME}-{int(time.time())}", "Comment": comment, "Enabled": True, "PriceClass": "PriceClass_100", "WebACLId": waf_arn,
        "Origins": {"Quantity": 1, "Items": [{"Id": "lambda", "DomainName": origin_host,
            "CustomHeaders": {"Quantity": 1, "Items": [{"HeaderName": "X-Kena-Origin", "HeaderValue": secret}]},
            "CustomOriginConfig": {"HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "https-only", "OriginReadTimeout": 60,
                                   "OriginKeepaliveTimeout": 5, "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]}}}]},
        "DefaultCacheBehavior": {"TargetOriginId": "lambda", "ViewerProtocolPolicy": "redirect-to-https", "Compress": True,
            "CachePolicyId": cache_policy, "OriginRequestPolicyId": CACHING_ALL_VIEWER_EXCEPT_HOST,
            "AllowedMethods": {"Quantity": 7, "Items": ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"],
                               "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}}},
    }
    return cloudfront.create_distribution_with_tags(DistributionConfigWithTags={
        "DistributionConfig": config, "Tags": {"Items": [{"Key": "project", "Value": NAME}]}})["Distribution"]["DomainName"]


def wait_ready(url):
    for _ in range(90):
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=10) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(10)
    return False


def delete(session):
    """Remove everything this script created (the CloudFront distribution must be disabled first)."""
    cloudfront, lam = session.client("cloudfront"), session.client("lambda")
    for item in cloudfront.list_distributions().get("DistributionList", {}).get("Items", []):
        if item.get("Comment") == f"{NAME}-serverless":
            current = cloudfront.get_distribution_config(Id=item["Id"])
            if current["DistributionConfig"]["Enabled"]:
                current["DistributionConfig"]["Enabled"] = False
                cloudfront.update_distribution(Id=item["Id"], IfMatch=current["ETag"], DistributionConfig=current["DistributionConfig"])
                print("… disabling CloudFront (about 5 minutes)")
                cloudfront.get_waiter("distribution_deployed").wait(Id=item["Id"])
            cloudfront.delete_distribution(Id=item["Id"], IfMatch=cloudfront.get_distribution_config(Id=item["Id"])["ETag"])
    for call in (lambda: lam.delete_function(FunctionName=NAME),
                 lambda: session.client("dynamodb").delete_table(TableName=f"{NAME}-journeys")):
        try:
            call()
        except ClientError as error:
            print("-", error.response["Error"]["Message"])
    print("Deleted. Remaining (free when unused): WAF web ACL, cache policy and IAM role named kena-*; remove them in the console if you like.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--password", help="Dashboard password for user 'kena' (required on first deploy)")
    parser.add_argument("--model", default=None, help="Bedrock model id (default anthropic.claude-opus-5-5)")
    parser.add_argument("--agent", choices=["claude", "local"], default=None)
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    session = boto3.session.Session(region_name="us-east-1")  # CloudFront-scoped WAF lives in us-east-1
    if args.delete:
        return delete(session)
    print("Account:", session.client("sts").get_caller_identity()["Account"])
    print("… building Lambda package")
    code = build_package()
    print(f"✓ package {len(code) / 1e6:.1f} MB")
    table_arn = ensure_table(session.client("dynamodb"), f"{NAME}-journeys")
    print("✓ DynamoDB table")
    role_arn = ensure_role(session.client("iam"), table_arn)
    print("✓ IAM role")
    env = {"KENA_DYNAMO_TABLE": f"{NAME}-journeys", "KENA_AGENT": args.agent or None, "KENA_MODEL": args.model, "KENA_LIVE": "0",
           "KENA_TICK_ON_REQUEST": "1", "KENA_WORKER_INTERVAL_SECONDS": "5",
           "KENA_BASIC_AUTH": f"kena:{args.password}" if args.password else None}
    lam = session.client("lambda")
    try:
        existing = lam.get_function_configuration(FunctionName=NAME).get("Environment", {}).get("Variables", {})
    except lam.exceptions.ResourceNotFoundException:
        existing = {}
        env.update({"KENA_AGENT": env["KENA_AGENT"] or "claude", "KENA_MODEL": env["KENA_MODEL"] or "anthropic.claude-opus-5-5",
                    "KENA_CUSTOMER_NAME": "Alex", "KENA_NO_RESPONSE_SECONDS": "90"})
    env["KENA_ORIGIN_SECRET"] = existing.get("KENA_ORIGIN_SECRET") or secrets.token_urlsafe(32)
    variables = ensure_function(lam, role_arn, code, env)
    origin = ensure_url(lam)
    print("✓ Lambda function and URL")
    waf_arn = ensure_waf(session.client("wafv2"))
    print("✓ WAF web ACL")
    cloudfront = session.client("cloudfront")
    domain = ensure_distribution(cloudfront, origin, variables["KENA_ORIGIN_SECRET"], ensure_cache_policy(cloudfront), waf_arn)
    url = "https://" + domain
    print("✓ CloudFront distribution:", url, "(first deploy takes about 5-10 minutes to go live)")
    print("✓ live" if wait_ready(url) else "! not reachable yet; try again in a few minutes")
    print(f"\nDashboard: {url}\nCustomer phone: {url}/device\nSign in as 'kena' with your password.")


if __name__ == "__main__":
    main()
