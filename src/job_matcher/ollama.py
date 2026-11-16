"""A small client for Ollama, which runs language models on this computer.

Only talks to the local Ollama server (127.0.0.1 by default): postings and the CV never
leave the machine. Free; Ollama and its models are downloaded once.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:11434"
CONTEXT_TOKENS = 8192  # how much the model reads at once; postings average ~1,100 tokens


class OllamaError(Exception):
    """Ollama answered, but not with something usable."""


class OllamaUnavailable(OllamaError):
    """Ollama isn't running, or isn't reachable at the configured address."""


class ModelMissing(OllamaError):
    """The model hasn't been downloaded (ollama pull)."""


class TooLong(OllamaError):
    """The input filled the model's context, so Ollama would have cut part of it."""


@dataclass(frozen=True)
class Reply:
    content: str  # the model's answer (JSON text when a schema was given)
    prompt_tokens: int
    output_tokens: int
    seconds: float


class OllamaClient:
    def __init__(self, http: httpx.Client, base_url: str = DEFAULT_URL,
                 context_tokens: int = CONTEXT_TOKENS) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.context_tokens = context_tokens

    def chat(self, model: str, system: str, user: str, schema: dict[str, Any]) -> Reply:
        """One question, answered in the shape `schema` describes."""
        started = time.perf_counter()
        try:
            response = self.http.post(f"{self.base_url}/api/chat", json={
                "model": model,
                "stream": False,
                "think": False,  # no visible reasoning: slower, and not needed for reading
                "format": schema,
                "options": {"temperature": 0, "num_ctx": self.context_tokens},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            })
        except httpx.ConnectError as exc:
            raise OllamaUnavailable(
                f"can't reach Ollama at {self.base_url}; start the Ollama app") from exc
        except httpx.TimeoutException as exc:
            raise OllamaError(f"Ollama took too long to answer: {exc}") from exc
        seconds = time.perf_counter() - started

        if response.status_code == 404:
            raise ModelMissing(f"model {model!r} isn't downloaded; run: ollama pull {model}")
        if response.status_code != 200:
            raise OllamaError(f"HTTP {response.status_code}: {response.text[:200]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise OllamaError("Ollama's answer isn't JSON") from exc

        prompt_tokens = int(data.get("prompt_eval_count") or 0)
        if prompt_tokens >= self.context_tokens:
            raise TooLong(f"the input is {prompt_tokens}+ tokens, over the "
                          f"{self.context_tokens}-token limit")
        return Reply(
            content=(data.get("message") or {}).get("content") or "",
            prompt_tokens=prompt_tokens,
            output_tokens=int(data.get("eval_count") or 0),
            seconds=seconds,
        )
