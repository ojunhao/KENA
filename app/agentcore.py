"""AgentCore client and optional runtime entrypoint; not imported by the local server."""
import json
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
            # One runtime session per journey, so a conversation reuses a warm session (ids need 33+ characters).
            runtimeSessionId=f"kena-agent-session-{context['journey']['id']}".ljust(33, "0"),
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


def build_runtime_app(agent):
    """AgentCore Runtime HTTP app: the SDK serves POST /invocations and GET /ping on port 8080."""
    from bedrock_agentcore.runtime import BedrockAgentCoreApp
    runtime = BedrockAgentCoreApp()

    @runtime.entrypoint
    def invoke(payload):
        return decide(payload, agent)

    return runtime


def main():
    """Run only in an approved AWS runtime (deployed by scripts/deploy_agentcore.py)."""
    import os
    from anthropic import AnthropicBedrockMantle
    from .agent import ClaudeAgent
    if os.getenv("KENA_ENABLE_AWS_CALLS") != "explicitly-approved":
        raise RuntimeError("AWS calls disabled; explicit approval required")
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    agent = ClaudeAgent(AnthropicBedrockMantle(aws_region=region), os.getenv("KENA_MODEL", "anthropic.claude-opus-5-5"))
    build_runtime_app(agent).run()


if __name__ == "__main__":
    main()
