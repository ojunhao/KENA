"""AgentCore client and optional runtime entrypoint; not imported by the local server."""
import json
import uuid
from .models import StrictModel


class DecisionRequest(StrictModel):
    context: dict


class AgentCoreAgent:
    """Delegates planning to an agent hosted on Bedrock AgentCore Runtime. Actions are still re-validated locally."""
    name = "agentcore-runtime"
    def __init__(self, client, runtime_arn):
        self.client, self.runtime_arn = client, runtime_arn

    def respond(self, context):
        result = self.client.invoke_agent_runtime(
            agentRuntimeArn=self.runtime_arn,
            runtimeSessionId="kena-" + context["journey"]["id"] + "-" + uuid.uuid4().hex,
            payload=json.dumps({"context": context}).encode(),
            contentType="application/json", accept="application/json",
        )
        actions = json.loads(result["response"].read()).get("actions")
        return [a for a in actions if isinstance(a, dict)] if isinstance(actions, list) else []


def decide(payload, agent):
    request = DecisionRequest.model_validate(payload)
    if not isinstance(request.context.get("journey"), dict) or request.context.get("trigger") not in {"customer_reply", "no_response"}:
        raise ValueError("Context must include a journey and a known trigger")
    return {"actions": agent.respond(request.context)}


def main():
    """Run only in an approved AWS runtime; SDK supplies /invocations and /ping."""
    import os
    from anthropic import AnthropicBedrockMantle
    from bedrock_agentcore.runtime import BedrockAgentCoreApp
    from .agent import ClaudeAgent
    if os.getenv("KENA_ENABLE_AWS_CALLS") != "explicitly-approved":
        raise RuntimeError("AWS calls disabled; explicit approval required")
    agent = ClaudeAgent(AnthropicBedrockMantle(aws_region=os.environ["AWS_REGION"]), os.getenv("KENA_MODEL", "anthropic.claude-opus-5-5"))
    runtime = BedrockAgentCoreApp()

    @runtime.entrypoint
    def invoke(payload):
        return decide(payload, agent)

    runtime.run()


if __name__ == "__main__":
    main()
