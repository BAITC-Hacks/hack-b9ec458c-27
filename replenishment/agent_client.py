"""Optional OpenAI-compatible Chat Completions adapter; no implicit network calls."""
import os
from urllib.parse import urlsplit

from .agent import ModelReply, ToolCall

LIGHT_MODEL = "gpt-6-luna"
ORDER_MODEL = "gpt-6-sol"


class OpenAIChatModel:
    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    def complete(self, messages, tools) -> ModelReply:
        options = dict(
            model=self.model, messages=messages, tools=tools,
            parallel_tool_calls=False, max_completion_tokens=1200,
        )
        if self.model in (LIGHT_MODEL, ORDER_MODEL):
            # GPT-6 function calling through Chat Completions requires this mode.
            options["reasoning_effort"] = "none"
        response = self.client.chat.completions.create(**options)
        choice = response.choices[0]
        if choice.finish_reason not in ("stop", "tool_calls") or choice.message.refusal:
            raise ValueError("Model did not complete the turn")
        message = choice.message
        return ModelReply(text=message.content or "", calls=[
            ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments)
            for c in message.tool_calls or []
        ])

    def close(self):
        self.client.close()


def model_from_env(*, request_order: bool = False) -> OpenAIChatModel | None:
    """Choose a model for the UI task; require a key and leave .env files untouched."""
    key = os.environ.get("AGENT_API_KEY", "").strip()
    model = (os.environ.get("AGENT_MODEL_ORDER" if request_order else "AGENT_MODEL_LIGHT", "").strip()
             or os.environ.get("AGENT_MODEL", "").strip()
             or (ORDER_MODEL if request_order else LIGHT_MODEL))
    endpoint = os.environ.get("AGENT_BASE_URL", "https://api.openai.com/v1").strip()
    if not key:
        return None
    url = urlsplit(endpoint)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("AGENT_BASE_URL must be an HTTPS API endpoint without credentials or query")
    from openai import OpenAI

    return OpenAIChatModel(OpenAI(api_key=key, base_url=endpoint, timeout=20, max_retries=0), model)
