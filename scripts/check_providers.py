"""LLM provider health check: python -m scripts.check_providers

Makes one tiny live JSON call per configured provider and reports OpenRouter's credit balance, with an actionable message per
problem. Keys are never printed (only their last 4 characters, so you can tell which key is loaded).
Exit code 0 when at least one provider works, 1 otherwise.
"""
import asyncio
import sys

import httpx

from src.config import get_settings
from src.llm import LLMClient, LLMUnavailableError


# Input:  none – keys and models come from .env / the environment
# What:   Checks every provider separately (no retries or backoff) and looks up the OpenRouter account's credits
# Output: int – process exit code
async def check() -> int:
    s = get_settings()
    client, working = LLMClient(s.model_copy(update={"llm_cache_enabled": False}), max_rounds=1), 0
    if not client.providers:
        print("FAIL  no provider configured: set GROQ_API_KEY and/or OPENROUTER_API_KEY in .env")
        return 1
    keys = {"groq": s.groq_api_key.strip(), "openrouter": s.openrouter_api_key.strip()}
    for p in list(client.providers):
        client.providers = [p]
        try:
            await client.complete_json([{"role": "user", "content": 'Reply with the JSON object {"ok": true}'}], temperature=0.0, max_tokens=50)
            print(f"OK    {p.name:10s} key ...{keys[p.name][-4:]} model {p.model}")
            working += 1
        except LLMUnavailableError as e:
            print(f"FAIL  {p.name:10s} key ...{keys[p.name][-4:]} model {p.model}: {str(e)[:150]}")
    if keys["openrouter"]:
        r = httpx.get("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {keys['openrouter']}"}, timeout=20)
        d = r.json().get("data", {}) if r.status_code == 200 else {}
        hint = "" if d.get("total_credits") else "  <- this key's account has no credits: create the key inside the account/organisation you topped up"
        print(f"INFO  openrouter account credits: {d.get('total_credits')} (used {d.get('total_usage')}){hint}")
    return 0 if working else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(check()))
