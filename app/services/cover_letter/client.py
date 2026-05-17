"""
Тонкий клиент к DeepSeek поверх OpenAI-совместимого SDK.

DeepSeek поддерживает OpenAI-совместимый интерфейс на base_url
https://api.deepseek.com. Поэтому используем штатный openai-клиент.

Context Caching включён по умолчанию для всех запросов и не требует
никаких флагов. Метрику кеша возвращаем пользователю явно через поля
`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` из response.usage.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from openai import OpenAI


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"  # non-thinking режим deepseek-v4-flash


@dataclass
class CacheStats:
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int

    @property
    def hit_ratio(self) -> float:
        total = self.cache_hit_tokens + self.cache_miss_tokens
        return self.cache_hit_tokens / total if total else 0.0


@dataclass
class ChatResult:
    content: str
    stats: CacheStats


class DeepSeekClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is not set. Put it in env or pass explicitly."
            )
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url or os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
            timeout=timeout,
        )
        self._model = model or os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL)

    @property
    def model(self) -> str:
        return self._model

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.4,
        max_tokens: int | None = None,
    ) -> ChatResult:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choice = resp.choices[0]
        content = (choice.message.content or "").strip()

        usage = resp.usage
        # У DeepSeek в usage добавлены поля prompt_cache_hit_tokens
        # и prompt_cache_miss_tokens. SDK их не типизирует, читаем
        # через model_dump / getattr, чтобы не падать на чужих провайдерах.
        usage_dict = usage.model_dump() if usage is not None else {}
        stats = CacheStats(
            prompt_tokens=int(usage_dict.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage_dict.get("completion_tokens", 0) or 0),
            cache_hit_tokens=int(usage_dict.get("prompt_cache_hit_tokens", 0) or 0),
            cache_miss_tokens=int(usage_dict.get("prompt_cache_miss_tokens", 0) or 0),
        )
        return ChatResult(content=content, stats=stats)
