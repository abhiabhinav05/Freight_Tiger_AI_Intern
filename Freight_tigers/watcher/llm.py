"""Optional LLM client for any OpenAI-compatible chat endpoint (Ollama, Groq, vLLM, ...).

Standard library only. Every call runs at temperature 0 with a fixed seed, and every
response is cached on disk by a hash of (model, prompt), so reruns replay the same text.
Token usage is recorded per call so the cost log can report one full run honestly.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class Usage:
    live_calls: int = 0
    cached_calls: int = 0
    input_tokens: int = 0       # summed over every call this run, live or replayed from cache
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def calls(self) -> int:
        return self.live_calls + self.cached_calls


class LLMClient:
    def __init__(self, base_url: str, model: str, api_key: str, cache_path: Path,
                 price_in_per_m: float = 0.0, price_out_per_m: float = 0.0, timeout: float = 120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.cache_path = cache_path
        self.price_in_per_m = price_in_per_m
        self.price_out_per_m = price_out_per_m
        self.timeout = timeout
        self.usage = Usage()
        self._cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    @classmethod
    def from_env(cls, cache_path: Path) -> "LLMClient":
        return cls(
            base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),  # Ollama default
            model=os.environ.get("LLM_MODEL", "llama3.1:8b"),
            api_key=os.environ.get("LLM_API_KEY", ""),
            cache_path=cache_path,
            price_in_per_m=float(os.environ.get("LLM_PRICE_IN_PER_M", "0")),
            price_out_per_m=float(os.environ.get("LLM_PRICE_OUT_PER_M", "0")),
        )

    @property
    def cost_usd(self) -> float:
        return (self.usage.input_tokens * self.price_in_per_m
                + self.usage.output_tokens * self.price_out_per_m) / 1e6

    def chat(self, system: str, user: str, max_tokens: int = 300) -> str:
        key = hashlib.sha256(json.dumps([self.model, system, user]).encode()).hexdigest()
        if key in self._cache:
            hit = self._cache[key]
            self.usage.cached_calls += 1
        else:
            hit = self._post(system, user, max_tokens)
            self.usage.live_calls += 1
            self._cache[key] = hit
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache, indent=1, sort_keys=True), encoding="utf-8")
        self.usage.input_tokens += hit["input_tokens"]
        self.usage.output_tokens += hit["output_tokens"]
        return hit["text"]

    def _post(self, system: str, user: str, max_tokens: int) -> dict:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0, "seed": 0, "max_tokens": max_tokens,
        }).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(f"{self.base_url}/chat/completions", body, headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise LLMUnavailable(f"LLM endpoint {self.base_url} failed: {e}") from e
        usage = data.get("usage") or {}
        return {
            "text": data["choices"][0]["message"]["content"].strip(),
            "input_tokens": int(usage.get("prompt_tokens", 0)),
            "output_tokens": int(usage.get("completion_tokens", 0)),
        }
