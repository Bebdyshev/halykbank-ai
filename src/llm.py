#!/usr/bin/env python3
"""Multi-provider LLM wrapper.

- Primary: OpenAI (gpt-5 = STRONG, gpt-5-mini = CHEAP), structured outputs via
  response_format json_schema.
- Fallback: Gemini flash pool (free tier) when OpenAI fails.
- Content-addressed disk cache: every call cached under
  artifacts/llm_cache/{sha256(model+prompt+images+schema)}.json - re-runs are
  free and deterministic.
- Spend counter: artifacts/llm_spend.json accumulates tokens/cost per model so
  the $ budget is observable at any moment.
"""
import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CACHE = REPO / "artifacts" / "llm_cache"
SPEND = REPO / "artifacts" / "llm_spend.json"

# Provider selection: LLM_PROVIDER in .env/env = "openai" (default) | "deepseek".
# Vision calls always go to OpenAI (DeepSeek has no vision); everything else
# follows the selected provider. Cache keys include the model name, so switching
# providers never poisons cached results.
def _provider() -> str:
    return (os.environ.get("LLM_PROVIDER")
            or _env_file_get("LLM_PROVIDER") or "openai").lower()


def _env_file_get(key: str):
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return None


_TIERS = {
    "openai": {"strong": "gpt-5", "cheap": "gpt-5-mini"},
    "deepseek": {"strong": "deepseek-v4-pro", "cheap": "deepseek-v4-flash"},
}
STRONG = _TIERS[_provider()]["strong"]
CHEAP = _TIERS[_provider()]["cheap"]
VISION = "gpt-5"  # deepseek has no vision models

# $ per 1M tokens (input, output); deepseek v4 figures are estimates
PRICES = {
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.40),
    "deepseek-v4-pro": (0.60, 2.40),
    "deepseek-v4-flash": (0.10, 0.40),
}

GEMINI_POOL = [
    "gemini-3.6-flash", "gemini-3.5-flash", "gemini-flash-latest",
    "gemini-2.5-flash", "gemini-2.0-flash", "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite", "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite", "gemini-2.0-flash-lite",
]

_lock = threading.Lock()
_openai_client = None
_gemini_client = None
_gemini_exhausted = set()


def _env(key: str):
    val = os.environ.get(key)
    if not val and (REPO / ".env").exists():
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith(f"{key}="):
                val = line.split("=", 1)[1].strip()
    return val


_clients = {}


def _client_for(model: str):
    """OpenAI-compatible client for the model's provider."""
    from openai import OpenAI
    if model.startswith("deepseek"):
        if "deepseek" not in _clients:
            _clients["deepseek"] = OpenAI(
                api_key=_env("DEEPSEEK_API_KEY"),
                base_url="https://api.deepseek.com",
                timeout=180.0, max_retries=2)
        return _clients["deepseek"]
    if "openai" not in _clients:
        _clients["openai"] = OpenAI(api_key=_env("OPENAI_API_KEY"),
                                    timeout=180.0, max_retries=2)
    return _clients["openai"]


def _gemini():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        _gemini_client = genai.Client(api_key=_env("GEMINI_API_KEY"))
    return _gemini_client


def _record_spend(model: str, tokens_in: int, tokens_out: int):
    with _lock:
        spend = json.loads(SPEND.read_text()) if SPEND.exists() else {}
        m = spend.setdefault(model, {"in": 0, "out": 0, "usd": 0.0, "calls": 0})
        m["in"] += tokens_in
        m["out"] += tokens_out
        m["calls"] += 1
        pin, pout = PRICES.get(model, (0.0, 0.0))
        m["usd"] = round(m["in"] / 1e6 * pin + m["out"] / 1e6 * pout, 4)
        spend["total_usd"] = round(
            sum(v["usd"] for k, v in spend.items() if isinstance(v, dict)), 4)
        SPEND.write_text(json.dumps(spend, indent=1))


def total_spend() -> float:
    if not SPEND.exists():
        return 0.0
    return json.loads(SPEND.read_text()).get("total_usd", 0.0)


def _cache_key(model: str, prompt: str, img_hash: str, schema) -> Path:
    h = hashlib.sha256(
        f"{model}\x00{prompt}\x00{img_hash}\x00{json.dumps(schema or {}, sort_keys=True)}".encode()
    ).hexdigest()
    return CACHE / f"{h}.json"


def _call_openai(model, prompt, images, schema, reasoning_effort):
    if images:
        model = VISION  # only OpenAI serves vision
    is_deepseek = model.startswith("deepseek")

    content = []
    for p in images or []:
        b64 = base64.b64encode(Path(p).read_bytes()).decode()
        mime = "image/png" if str(p).endswith(".png") else "image/jpeg"
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"}})
    text_part = prompt
    if schema and is_deepseek:
        # deepseek: json_object mode + schema stated in the prompt
        text_part += ("\n\nRespond with a single JSON object matching this "
                      "JSON Schema exactly:\n" + json.dumps(schema))
    content.append({"type": "text", "text": text_part})

    kwargs = {}
    if reasoning_effort and not is_deepseek:
        kwargs["reasoning_effort"] = reasoning_effort
    if schema:
        kwargs["response_format"] = (
            {"type": "json_object"} if is_deepseek else
            {"type": "json_schema",
             "json_schema": {"name": "result", "schema": schema, "strict": False}})
    resp = _client_for(model).chat.completions.create(
        model=model, messages=[{"role": "user", "content": content}], **kwargs)
    u = resp.usage
    _record_spend(model, u.prompt_tokens, u.completion_tokens)
    text = resp.choices[0].message.content
    return json.loads(text) if schema else text


def _gemini_safe_schema(node):
    """Gemini's response_schema rejects JSON-Schema type unions like
    ["string","null"] - rewrite them to a single type + nullable."""
    if isinstance(node, list):
        return [_gemini_safe_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k == "type" and isinstance(v, list):
            non_null = [t for t in v if t != "null"]
            out["type"] = non_null[0] if non_null else "string"
            if "null" in v:
                out["nullable"] = True
        else:
            out[k] = _gemini_safe_schema(v)
    return out


def _call_gemini(prompt, images, schema):
    from google.genai import types
    schema = _gemini_safe_schema(schema) if schema else schema
    parts = []
    for p in images or []:
        mime = "image/png" if str(p).endswith(".png") else "image/jpeg"
        parts.append(types.Part.from_bytes(data=Path(p).read_bytes(), mime_type=mime))
    parts.append(types.Part.from_text(text=prompt))
    config = types.GenerateContentConfig(temperature=0.0)
    if schema:
        config.response_mime_type = "application/json"
        config.response_schema = schema

    last = None
    for m in [g for g in GEMINI_POOL if g not in _gemini_exhausted]:
        for attempt in range(3):
            try:
                resp = _gemini().models.generate_content(
                    model=m,
                    contents=[types.Content(role="user", parts=parts)],
                    config=config)
                return (json.loads(resp.text) if schema else resp.text), m
            except Exception as e:  # noqa: BLE001
                last = e
                msg = str(e)
                if "PerDay" in msg:
                    _gemini_exhausted.add(m)
                    break
                if any(c in msg for c in ("429", "RESOURCE_EXHAUSTED", "503", "500")):
                    time.sleep(2 ** attempt * 3)
                    continue
                break
    raise RuntimeError(f"gemini pool failed: {last}")


def generate(prompt, model: str = STRONG, schema: dict = None, images: list = None,
             reasoning_effort: str = None, use_cache: bool = True,
             max_retries: int = 3, provider: str = "openai"):
    """Text (+optional images) -> parsed JSON (if schema) or text.

    provider: "openai" (default, falls back to gemini) or "gemini" (free tier).
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    img_hash = "|".join(
        hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in images or [])
    key = _cache_key(model, prompt, img_hash, schema)
    if use_cache and key.exists():
        return json.loads(key.read_text())["result"]

    last = None
    if provider == "openai":
        for attempt in range(max_retries):
            try:
                result = _call_openai(model, prompt, images, schema, reasoning_effort)
                key.write_text(json.dumps(
                    {"model": model, "result": result}, ensure_ascii=False))
                return result
            except Exception as e:  # noqa: BLE001
                last = e
                msg = str(e)
                if any(c in msg for c in ("429", "500", "503", "overloaded",
                                          "Connection", "timeout", "Timeout")):
                    time.sleep(2 ** attempt * 3)
                    continue
                if "json" in msg.lower() and attempt < max_retries - 1:
                    continue
                break
        print(f"  [llm] openai {model} failed ({str(last)[:90]}); trying gemini")

    result, served_by = _call_gemini(prompt, images, schema)
    key.write_text(json.dumps(
        {"model": f"gemini:{served_by}", "result": result}, ensure_ascii=False))
    return result
