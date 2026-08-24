"""
Multi-provider LLM client abstraction. Supports OpenAI-compatible providers:
- Groq, Cerebras, NVIDIA NIM, Google AI Studio (cloud)
- Ollama (local, Phase 5)

A single openai.OpenAI client with swapped base_url handles all of them.
Per-model TPM budgets are tracked separately since Cerebras (60K) ≠ Groq (8K).
"""

import os
import urllib.error
import urllib.request
from functools import lru_cache
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI

# Load .env here too: this module is used standalone (verify_key.py, tests),
# not only via the entry point, so it can't rely on someone else loading it.
load_dotenv()


# Provider configurations: base_url, env var name for API key, free tier TPM limit
PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "tpm_limit": 8000,
        "description": "Groq (6-12K TPM per model, 30 RPM)",
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "key_env": "CEREBRAS_API_KEY",
        "tpm_limit": 60000,
        "description": "Cerebras (60K TPM, 30 RPM) — preferred for Coder",
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "key_env": "NVIDIA_API_KEY",
        "tpm_limit": 40000,  # estimate; credit-based, no strict TPM
        "description": "NVIDIA NIM (40 RPM credit-based)",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": "GEMINI_API_KEY",
        # Free tier is token-generous but request-poor (15 RPM / 1500 RPD).
        # Conservative floor across Flash/Pro; RPM is the real constraint here,
        # not TPM.
        "tpm_limit": 250000,
        "description": "Google AI Studio (15 RPM, 1500 RPD)",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        # Free (":free"-suffixed) models are capped by requests/day, not TPM.
        "tpm_limit": 30000,
        "description": "OpenRouter (aggregator; :free models, ~50 req/day)",
    },
    "github": {
        # GitHub Models — OpenAI-compatible, authed with a GitHub PAT.
        "base_url": "https://models.github.ai/inference",
        "key_env": "GITHUB_API_KEY",
        "tpm_limit": 8000,
        "description": "GitHub Models (free tier, PAT-authed)",
    },
    "sambanova": {
        "base_url": "https://api.sambanova.ai/v1",
        "key_env": "SAMBANOVA_CLOUD_API_KEY",
        "tpm_limit": 30000,
        "description": "SambaNova Cloud (fast Llama inference)",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "key_env": "MISTRAL_API_KEY",
        "tpm_limit": 30000,
        "description": "Mistral La Plateforme (free tier)",
    },
    # NOTE: Cloudflare Workers AI is deliberately NOT registered, despite
    # CLOUDFLARE_API_KEY being set. Its OpenAI-compatible endpoint is
    #   https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1
    # and there's no CLOUDFLARE_ACCOUNT_ID in .env. Registering it with a
    # blank/guessed account id would give a provider that reports as
    # "available" and then fails on every call — the exact listing-vs-serving
    # trap that already cost two debugging rounds with Cerebras and NVIDIA.
    # Add CLOUDFLARE_ACCOUNT_ID to .env and this can be enabled properly.
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "key_env": None,  # Ollama runs locally, no key needed
        "tpm_limit": 1000000,  # effectively unlimited; localhost
        "description": "Ollama (local, offline) — Phase 5",
    },
}


@lru_cache(maxsize=8)
def get_client(provider: str, api_key: Optional[str] = None) -> OpenAI:
    """
    Get or create a cached OpenAI client for the given provider.
    If api_key is not provided, reads from the environment.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}. Available: {list(PROVIDERS.keys())}")

    cfg = PROVIDERS[provider]
    key = api_key or os.environ.get(cfg["key_env"], "")

    # Ollama doesn't need a key
    if provider == "ollama":
        key = "dummy"  # openai.OpenAI requires a key, but it's never used for local

    if not key:
        raise ValueError(
            f"Provider '{provider}' requires {cfg['key_env']} to be set. "
            f"See .env.example for instructions."
        )

    return OpenAI(api_key=key, base_url=cfg["base_url"])


def ollama_is_running(timeout_sec: float = 0.5) -> bool:
    """True only if a local Ollama server actually answers.

    Constructing an OpenAI client does NOT open a connection, so a try/except
    around it always succeeds and reports Ollama available even when it isn't.
    This makes a real request instead.
    """
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=timeout_sec) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def available_providers() -> dict[str, dict]:
    """
    Returns the providers that are actually usable right now: cloud providers
    with a key set, plus Ollama only if it's genuinely serving on localhost.
    Used for startup reporting and graceful degradation.
    """
    available = {}
    for name, cfg in PROVIDERS.items():
        if name == "ollama":
            if ollama_is_running():
                available[name] = cfg
        else:
            key_env = cfg["key_env"]
            if key_env and os.environ.get(key_env, "").strip():
                available[name] = cfg

    return available


def split_ref(model_ref: str) -> tuple[str, str]:
    """
    Parse a model reference like 'cerebras:llama-3.3-70b' into (provider, model_id).
    If no provider is specified, default to 'groq' for backward compatibility.
    """
    if ":" in model_ref:
        provider, model = model_ref.split(":", 1)
        return provider, model
    return "groq", model_ref


def get_tpm_limit(provider: str) -> int:
    """Get the free-tier TPM limit for a provider."""
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")
    return PROVIDERS[provider]["tpm_limit"]
