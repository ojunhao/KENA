"""Optional model-backed local launch. Messaging stays simulated."""
import argparse
import os
import uvicorn
from .main import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", choices=["local", "bedrock", "agentcore"], default="local")
    parser.add_argument("--enable-aws-calls", action="store_true", help="Use only after the project owner approves paid model/runtime calls")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    agent = None
    if args.agent != "local":
        if not args.enable_aws_calls:
            parser.error("AWS calls disabled. Explicit approval and --enable-aws-calls required.")
        import boto3
        from botocore.config import Config
        region = os.environ["AWS_REGION"]
        config = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 1})
        if args.agent == "bedrock":
            from .agent import BedrockAgent
            agent = BedrockAgent(boto3.client("bedrock-runtime", region_name=region, config=config), os.environ["KENA_MODEL_ID"])
        else:
            from .agentcore import AgentCoreAgent
            agent = AgentCoreAgent(boto3.client("bedrock-agentcore", region_name=region, config=config), os.environ["KENA_AGENTCORE_RUNTIME_ARN"])
    uvicorn.run(create_app(agent=agent), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
