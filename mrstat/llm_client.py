"""Client for any OpenAI-compatible `/chat/completions` endpoint (plain HTTP, no vendor SDK).

Works with internal LLM gateways, Azure OpenAI (base_url = .../openai/deployments/<name>,
api_key_header = "api-key", extra_query = {"api-version" = "..."}), vLLM, Ollama
(base_url = http://host:11434/v1), LiteLLM and similar.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from mrstat.config import ConfigError, LLMConfig
from mrstat.http import make_session

log = logging.getLogger(__name__)


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class ChatResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict = field(default_factory=dict)


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        if not cfg.base_url or not cfg.model:
            raise ConfigError("llm.base_url and llm.model must be configured "
                              "(or MRSTAT_LLM_BASE_URL / MRSTAT_LLM_MODEL)")
        self.cfg = cfg
        self.url = cfg.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json", **cfg.extra_headers}
        if cfg.api_key:
            if cfg.api_key_header.lower() == "authorization":
                headers["Authorization"] = f"Bearer {cfg.api_key}"
            else:
                headers[cfg.api_key_header] = cfg.api_key
        self.s = make_session(cfg.verify_ssl, headers)

    @property
    def model(self) -> str:
        return self.cfg.model

    def chat(self, messages: list[dict], *, json_schema: dict | None = None,
             schema_name: str = "result") -> ChatResult:
        c = self.cfg
        body: dict = {"model": c.model, "messages": messages, c.max_tokens_param: c.max_tokens}
        if c.temperature != "omit":
            body["temperature"] = float(c.temperature)
        if json_schema is not None:
            if c.response_format == "json_schema":
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": schema_name, "schema": json_schema, "strict": True}}
            elif c.response_format == "json_object":
                body["response_format"] = {"type": "json_object"}
        body.update(c.extra_body)

        r = self.s.post(self.url, params=c.extra_query or None, json=body, timeout=c.timeout)
        if r.status_code >= 400:
            hint = ""
            if r.status_code == 400 and "response_format" in r.text:
                hint = " (endpoint rejects response_format: set llm.response_format = \"json_object\" or \"none\")"
            raise LLMError(f"LLM endpoint returned {r.status_code}{hint}: {r.text[:500]}",
                           status=r.status_code, body=r.text[:500])
        data = r.json()
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content")
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected LLM response shape: {str(data)[:500]}") from exc
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content:
            reason = choice.get("finish_reason")
            refusal = choice["message"].get("refusal")
            raise LLMError(f"empty LLM response (finish_reason={reason}, refusal={refusal})")
        if choice.get("finish_reason") == "length":
            log.warning("LLM output hit max_tokens (%s); raise llm.max_tokens", c.max_tokens)
        usage = data.get("usage") or {}
        return ChatResult(content, int(usage.get("prompt_tokens") or 0),
                          int(usage.get("completion_tokens") or 0), data)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_object(text: str) -> dict:
    """Parse a JSON object from model output, tolerating code fences and surrounding prose."""
    candidates = [text.strip()]
    candidates += [m.group(1).strip() for m in _FENCE.finditer(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise LLMError(f"no JSON object in LLM output: {text[:300]!r}")
