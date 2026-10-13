"""AgentCore client and optional runtime entrypoint; not imported by the local server."""
import json
import uuid
from .agent import BedrockAgent, INTENTS
from .models import StrictModel
from pydantic import Field


class DecisionRequest(StrictModel):
    text: str = Field(min_length=1, max_length=1000)
    context: dict


class AgentCoreAgent:
    name = "agentcore-runtime"
    def __init__(self, client, runtime_arn):
        self.client, self.runtime_arn = client, runtime_arn

    def classify(self, text, context):
        result = self.client.invoke_agent_runtime(
            agentRuntimeArn=self.runtime_arn,
            runtimeSessionId=str(uuid.uuid4()),
            payload=json.dumps({"text": text, "context": context}).encode(),
            contentType="application/json", accept="application/json",
        )
        decision = json.loads(result["response"].read())
        return decision["intent"] if decision.get("intent") in INTENTS else "uncertain"


def decide(payload, agent):
    request = DecisionRequest.model_validate(payload)
    return {"intent": agent.classify(request.text, request.context)}


def main():
    """Run only in an approved AWS runtime; SDK supplies /invocations and /ping."""
    import os
    import boto3
    from bedrock_agentcore.runtime import BedrockAgentCoreApp
    if os.getenv("KENA_ENABLE_AWS_CALLS") != "explicitly-approved":
        raise RuntimeError("AWS calls disabled; explicit approval required")
    model_id = os.environ["KENA_MODEL_ID"]
    agent = BedrockAgent(boto3.client("bedrock-runtime", region_name=os.environ["AWS_REGION"]), model_id)
    runtime = BedrockAgentCoreApp()

    @runtime.entrypoint
    def invoke(payload):
        return decide(payload, agent)

    runtime.run()


if __name__ == "__main__":
    main()
