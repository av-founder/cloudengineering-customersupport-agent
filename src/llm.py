"""LLM access layer: Groq (primary) -> OpenRouter (fallback) with Tenacity retries, a circuit breaker, JSON parsing and a disk cache."""
import asyncio
import json
import logging
import math
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import groq
import openai
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config import Settings, sha256

log = logging.getLogger(__name__)

# Input:  none
# What:   Error families: network/5xx errors get two fast retries, 429s fail over at once, 400s retry without JSON mode,
#         401/402/403/404 (bad key, no credits, no access, retired model) switch that provider off for the rest of the run
# Output: tuples of exception classes
TRANSIENT = (openai.APIConnectionError, openai.InternalServerError, groq.APIConnectionError, groq.InternalServerError)
RATE_LIMITED = (openai.RateLimitError, groq.RateLimitError)
BAD_REQUEST = (openai.BadRequestError, groq.BadRequestError)
FATAL_STATUS = frozenset({401, 402, 403, 404})


class LLMUnavailableError(RuntimeError):
    """Every configured provider failed or is rate limited; callers must escalate deterministically."""


@dataclass
class LLMResult:
    data: dict[str, Any]
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False


@dataclass
class _Provider:
    name: str
    client: Any
    model: str
    failures: int = 0
    open_until: float = 0.0


# Input:  text (str | None) – raw model reply, possibly wrapped in prose or ``` fences
# What:   Pulls out the outermost {...} block and parses it
# Output: dict – parsed JSON object (raises ValueError when there is none)
def extract_json(text: str | None) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("model reply contained no JSON object")
    data = json.loads(m[0])
    if not isinstance(data, dict):
        raise ValueError("model reply JSON is not an object")
    return data


# Input:  e (Exception) – an HTTP 429 error
# What:   Reads the provider's Retry-After header, defaulting to 5 s and capping at 60 s
# Output: float – seconds to keep that provider's circuit open
def _retry_after(e: Exception) -> float:
    try:
        return min(float(e.response.headers.get("retry-after", 5)), 60.0)
    except (AttributeError, TypeError, ValueError):
        return 5.0


# Input:  st (TicketState) – current state; results (list[LLMResult]) – calls made by the current node
# What:   Adds this node's LLM calls, tokens and provider/model names to the running per-ticket totals
# Output: dict – TicketState updates
def usage_update(st: Any, results: list[LLMResult]) -> dict[str, Any]:
    if not results:
        return {}
    merge = lambda old, new: ",".join(dict.fromkeys([x for x in old.split(",") if x and x != "none"] + new)) or "none"
    return {
        "llm_calls": st.llm_calls + len(results),
        "prompt_tokens": st.prompt_tokens + sum(r.prompt_tokens for r in results),
        "completion_tokens": st.completion_tokens + sum(r.completion_tokens for r in results),
        "model_provider": merge(st.model_provider, [r.provider for r in results])[:64],
        "model_identifier": merge(st.model_identifier, [r.model for r in results])[:128],
    }


class _DiskCache:
    # Input:  path (Path) – local SQLite file under storage/ (git-ignored)
    # What:   Remembers identical earlier LLM requests so re-runs are free, fast and reproducible
    # Output: get/put helpers
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db, self.lock = sqlite3.connect(path, check_same_thread=False), threading.Lock()
        self.db.execute("CREATE TABLE IF NOT EXISTS llm_cache (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        self.hits = 0

    def get(self, k: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT v FROM llm_cache WHERE k = ?", (k,)).fetchone()
        self.hits += bool(row)
        return json.loads(row[0]) if row else None

    def put(self, k: str, v: dict) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO llm_cache (k, v) VALUES (?, ?)", (k, json.dumps(v)))
            self.db.commit()


class LLMClient:
    # Input:  s (Settings) – API keys and model names; max_rounds (int) – passes over the provider chain before giving up
    # What:   Provider-failover chat client that always returns parsed JSON or raises LLMUnavailableError
    # Output: LLMClient
    def __init__(self, s: Settings, max_rounds: int = 4):
        hdrs = {"HTTP-Referer": "https://cloudserve.io", "X-Title": "CloudServe Triage"}
        self.providers = [p for p in (
            _Provider("groq", groq.AsyncGroq(api_key=s.groq_api_key, timeout=s.llm_timeout_s, max_retries=0), s.primary_model) if s.groq_api_key else None,
            _Provider("openrouter", openai.AsyncOpenAI(api_key=s.openrouter_api_key, base_url=s.openrouter_base_url, timeout=s.llm_timeout_s,
                                                       max_retries=0, default_headers=hdrs), s.fallback_model) if s.openrouter_api_key else None,
        ) if p]
        self.cache = _DiskCache(s.llm_cache_path) if s.llm_cache_enabled else None
        self.reasoning = {"reasoning_effort": s.llm_reasoning_effort} if s.llm_reasoning_effort else {}
        self.headroom = s.llm_reasoning_headroom_tokens if self.reasoning else 0
        self.max_rounds, self.backoff_seconds, self.failovers, self.calls = max_rounds, 0.0, 0, 0

    @property
    def available(self) -> bool:
        return bool(self.providers)

    @property
    def chain(self) -> str:
        return " -> ".join(f"{p.name}:{p.model}" for p in self.providers) or "none"

    @property
    def cache_hits(self) -> int:
        return self.cache.hits if self.cache else 0

    async def complete_json(self, messages: list[dict], *, temperature: float, max_tokens: int, sample: int = 0) -> LLMResult:
        # Input:  messages (list[dict]) chat turns; temperature (float); max_tokens (int); sample (int) self-consistency draw index
        # What:   Serves identical requests from cache; otherwise tries each provider in order, failing over on errors or
        #         rate limits, and backs off only when every provider is unavailable (time is recorded for the run report)
        # Output: LLMResult – parsed JSON plus provider, model and token usage
        if not self.providers:
            raise LLMUnavailableError("no LLM provider configured (set GROQ_API_KEY and/or OPENROUTER_API_KEY)")
        key = sha256(json.dumps([[p.model for p in self.providers], messages, temperature, max_tokens, sample], sort_keys=True))
        if self.cache and (hit := self.cache.get(key)):
            return LLMResult(**hit, cached=True)
        last: Exception | None = None
        for rnd in range(self.max_rounds):
            for p in [p for p in self.providers if p.open_until <= time.monotonic()]:
                try:
                    res = await self._call(p, messages, temperature, max_tokens, sample)
                except RATE_LIMITED as e:
                    last, p.open_until = e, time.monotonic() + _retry_after(e)
                except Exception as e:  # permanent faults disable the provider; anything else counts towards its circuit breaker
                    last = e
                    if getattr(e, "status_code", None) in FATAL_STATUS:
                        p.open_until = math.inf
                        log.error("LLM provider %s disabled for this run (HTTP %s): check the provider's access, account balance and that model '%s' exists",
                                  p.name, e.status_code, p.model)
                    else:
                        p.failures += 1
                        if p.failures >= 3:
                            p.open_until, p.failures = time.monotonic() + 30.0, 0
                else:
                    p.failures = 0
                    if self.cache:
                        self.cache.put(key, {k: v for k, v in asdict(res).items() if k != "cached"})
                    return res
                self.failovers += 1
                log.warning("LLM provider %s failed (%s); failing over", p.name, type(last).__name__)
            if rnd == self.max_rounds - 1 or all(math.isinf(p.open_until) for p in self.providers):
                break
            soonest = min(p.open_until for p in self.providers) - time.monotonic()
            wait = min(30.0, max(2.0 ** rnd, soonest))
            self.backoff_seconds += wait
            await asyncio.sleep(wait)
        raise LLMUnavailableError(f"all LLM providers failed: {type(last).__name__}: {last}")

    async def _call(self, p: _Provider, messages: list[dict], temperature: float, max_tokens: int, sample: int) -> LLMResult:
        # Input:  p (_Provider) and the request fields
        # What:   One provider call with up to two fast retries (0.2 s, 0.4 s) on network/5xx errors. It asks for JSON mode plus
        #         low reasoning effort (with extra token headroom for the hidden reasoning) and repeats once with plain
        #         parameters if the provider rejects them
        # Output: LLMResult
        for extra in ({"response_format": {"type": "json_object"}} | self.reasoning, {}):
            try:
                async for attempt in AsyncRetrying(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.2, max=0.4),
                                                   retry=retry_if_exception_type(TRANSIENT), reraise=True):
                    with attempt:
                        r = await p.client.chat.completions.create(model=p.model, messages=messages, temperature=temperature,
                                                                   max_tokens=max_tokens + self.headroom, seed=1234 + sample, **extra)
                break
            except BAD_REQUEST:
                if not extra:
                    raise
        self.calls += 1
        u = getattr(r, "usage", None)
        return LLMResult(extract_json(r.choices[0].message.content), p.name, p.model,
                         getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)
