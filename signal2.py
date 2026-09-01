#!/usr/bin/env python3
"""Signal2 — autonomous AI model-release intelligence for GitHub Actions + Discord.

Design goals:
- stdlib only, zero external runtime dependency;
- GitHub Actions as the only host;
- evidence-first aggregation across independent public sources;
- durable outbox: an event is marked delivered only after Discord confirms it;
- catch-up polling instead of relying on scheduler punctuality;
- fail-open for detection (one broken source never aborts the whole cycle).

The system cannot mathematically guarantee exactly-once delivery with a remote HTTP
webhook and Git-backed state. It deliberately chooses at-least-once semantics:
a rare duplicate after an ambiguous crash is preferable to silently losing an alert.
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

UTC = timezone.utc
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = BASE_DIR / "signal2_state.json"
USER_AGENT = "Signal2/2.0 (+https://github.com/FeelTheFonk/signal2)"

POLL_LOOKBACK_HOURS = 48
STATE_RETENTION_DAYS = 120
POLL_DELIVERY_LIMIT = 30
HTTP_TIMEOUT = 20
MAX_HTTP_RETRIES = 3

OFFICIAL_FEEDS = (
    {"id": "openai-news", "vendor": "OpenAI", "url": "https://openai.com/news/rss.xml", "filter": True},
    {"id": "mistral-news", "vendor": "Mistral AI", "url": "https://mistral.ai/news/rss", "filter": True},
    {"id": "qwen-blog", "vendor": "Qwen (Alibaba)", "url": "https://qwenlm.github.io/blog/index.xml", "filter": True},
    {"id": "google-deepmind", "vendor": "Google", "url": "https://blog.google/technology/google-deepmind/rss/", "filter": True},
)

# Focused first-party model publishers. Broad/noisy organizations are still
# collected, but non-core Microsoft/NVIDIA uploads are discovery evidence rather
# than authoritative releases unless independently corroborated.
HF_ORGS = (
    "deepseek-ai",
    "Qwen",
    "zai-org",
    "moonshotai",
    "meta-llama",
    "mistralai",
    "openai",
    "black-forest-labs",
    "nvidia",
    "microsoft",
)

HN_QUERIES = (
    "Claude model release",
    "OpenAI model release",
    "Gemini model release",
    "DeepSeek model release",
    "Qwen model release",
    "Llama model release",
    "Mistral model release",
    "Kimi model release",
    "GLM model release",
    "FLUX model release",
    "new open weights model",
)

SLUG_TO_VENDOR = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
    "google-deepmind": "Google",
    "deepmind": "Google",
    "deepseek": "DeepSeek",
    "deepseek-ai": "DeepSeek",
    "qwen": "Qwen (Alibaba)",
    "qwenlm": "Qwen (Alibaba)",
    "alibaba": "Qwen (Alibaba)",
    "meta": "Meta AI",
    "meta-llama": "Meta AI",
    "zai": "Z.ai (Zhipu)",
    "zai-org": "Z.ai (Zhipu)",
    "zhipu": "Z.ai (Zhipu)",
    "moonshotai": "Moonshot AI",
    "moonshot": "Moonshot AI",
    "mistral": "Mistral AI",
    "mistralai": "Mistral AI",
    "black-forest-labs": "Black Forest Labs",
    "bfl": "Black Forest Labs",
    "nvidia": "NVIDIA",
    "microsoft": "Microsoft",
    "cohere": "Cohere",
    "ai21": "AI21 Labs",
    "minimax": "MiniMax",
    "baidu": "Baidu",
    "tencent": "Tencent",
    "01-ai": "01.AI",
    "reka-ai": "Reka AI",
    "reka": "Reka AI",
    "upstage": "Upstage",
}

VENDOR_TO_SLUG = {
    "Anthropic": "anthropic",
    "OpenAI": "openai",
    "Google": "google",
    "DeepSeek": "deepseek",
    "Qwen (Alibaba)": "qwen",
    "Meta AI": "meta",
    "Z.ai (Zhipu)": "zai",
    "Moonshot AI": "moonshotai",
    "Mistral AI": "mistral",
    "Black Forest Labs": "black-forest-labs",
    "NVIDIA": "nvidia",
    "Microsoft": "microsoft",
    "Cohere": "cohere",
    "AI21 Labs": "ai21",
    "MiniMax": "minimax",
    "Baidu": "baidu",
    "Tencent": "tencent",
    "01.AI": "01-ai",
    "Reka AI": "reka",
    "Upstage": "upstage",
}

# Hosting/routing providers that are not model creators. Their catalog entries are
# accepted only when the underlying creator can be resolved from a qualified model
# id or from the model name.
AGGREGATOR_PROVIDERS = {
    "openrouter", "groq", "togetherai", "together", "fireworks", "deepinfra",
    "amazon-bedrock", "bedrock", "azure", "azure-openai", "vertex", "cloudflare",
    "cerebras", "baseten", "replicate", "fal", "perplexity",
}


VENDOR_PATTERNS = (
    ("Anthropic", r"\banthropic\b|\bclaude\b|\bfable\b|\bmythos\b|\bopus\b|\bsonnet\b|\bhaiku\b"),
    ("OpenAI", r"\bopenai\b|\bchatgpt\b|\bgpt[- .]?\d|\bsora\b|\bcodex\b|\bo[134][-. ]?\d*\b"),
    ("Google", r"\bgoogle\b|\bdeepmind\b|\bgemini\b|\bgemma\b|\bimagen\b|\bveo\b"),
    ("DeepSeek", r"\bdeepseek\b"),
    ("Qwen (Alibaba)", r"\bqwen\b|\balibaba\b|\btongyi\b|\bwan[- .]?\d"),
    ("Meta AI", r"\bmeta\b|\bllama\b"),
    ("Z.ai (Zhipu)", r"\bz\.?ai\b|\bzhipu\b|\bglm[- .]?\d"),
    ("Moonshot AI", r"\bmoonshot\b|\bkimi\b"),
    ("Mistral AI", r"\bmistral\b|\bcodestral\b|\bmagistral\b|\bvoxtral\b|\bministral\b|\bpixtral\b"),
    ("Black Forest Labs", r"\bblack forest\b|\bflux(?:\.|\b)"),
    ("NVIDIA", r"\bnvidia\b|\bnemotron\b"),
    ("Microsoft", r"\bmicrosoft\b|\bphi[- .]?\d|\bmai[- .]?\d"),
    ("Cohere", r"\bcohere\b|\bcommand[- ]?r\b|\baya\b"),
    ("AI21 Labs", r"\bai21\b|\bjamba\b"),
    ("MiniMax", r"\bminimax\b|\babab\b"),
    ("Baidu", r"\bbaidu\b|\bernie\b"),
    ("Tencent", r"\btencent\b|\bhunyuan\b"),
    ("01.AI", r"\b01\.ai\b|\byi[- .]?\d"),
    ("Reka AI", r"\breka\b"),
    ("Upstage", r"\bupstage\b|\bsolar[- .]?\d"),
)

MODEL_HINT_RE = re.compile(
    r"\b(gpt|claude|fable|mythos|opus|sonnet|haiku|gemini|gemma|imagen|veo|deepseek|qwen|"
    r"llama|glm|kimi|mistral|codestral|magistral|voxtral|ministral|pixtral|"
    r"flux|sora|codex|nemotron|phi|jamba|command[- ]?r|aya|ernie|hunyuan|"
    r"new model|frontier model|open weights?|open[- ]source model)\b",
    re.IGNORECASE,
)
RELEASE_RE = re.compile(
    r"\b(introduc(?:e|es|ed|ing)?|launch(?:e[ds]?|ing)?|releas(?:e[ds]?|ing)?|"
    r"announc(?:e[ds]?|ing)?|unveil(?:ed|s|ing)?|debut(?:s|ed|ing)?|ships?|"
    r"now available|available today|generally available|ga release|public preview|"
    r"research preview|open weights?|open[- ]source)\b",
    re.IGNORECASE,
)
VERSION_RE = re.compile(r"(?:^|[- ._/])v?\d+(?:\.\d+){0,3}(?:[-._a-z0-9]+)?", re.IGNORECASE)

SOURCE_LABELS = {
    "official": "Official",
    "deepseek": "DeepSeek changelog",
    "huggingface": "Hugging Face",
    "models.dev": "models.dev",
    "openrouter": "OpenRouter",
    "hn": "Hacker News",
}

SOURCE_COLORS = {
    "Anthropic": 0xD97757,
    "OpenAI": 0x10A37F,
    "Google": 0x4285F4,
    "DeepSeek": 0x4D6BFE,
    "Qwen (Alibaba)": 0xFF6A00,
    "Meta AI": 0x0866FF,
    "Mistral AI": 0xFA500F,
    "NVIDIA": 0x76B900,
    "Microsoft": 0x00A4EF,
}


def log(message: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            if re.fullmatch(r"\d{4}-\d{2}", text):
                text += "-01"
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = email.utils.parsedate_to_datetime(text)
            except (TypeError, ValueError, OverflowError):
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_link_next(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(","):
        if re.search(r'rel\s*=\s*["\']?next["\']?', part, re.IGNORECASE):
            match = re.search(r"<([^>]+)>", part)
            if match:
                return match.group(1)
    return None


def header_value(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    if hasattr(headers, "get"):
        value = headers.get(name)
        if value is not None:
            return str(value)
        value = headers.get(name.lower())
        if value is not None:
            return str(value)
    for key, value in dict(headers).items():
        if str(key).lower() == name.lower():
            return str(value)
    return None


def clean_text(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return re.sub(r"\s+", " ", value).strip()


def looks_like_release(title: str) -> bool:
    text = clean_text(title)
    if not text:
        return False
    if MODEL_HINT_RE.search(text) and RELEASE_RE.search(text):
        return True
    # Model + explicit version is a strong release-shaped signal even when a title
    # omits the usual launch verb (e.g. "GPT-6.1").
    return bool(MODEL_HINT_RE.search(text) and VERSION_RE.search(text) and len(text.split()) <= 4)


def detect_vendor(text: str) -> str | None:
    lowered = clean_text(text).lower()
    # Explicit project requirement inherited from the current repository: xAI/Grok
    # coverage is excluded entirely.
    if re.search(r"\bxai\b|\bx\.ai\b|\bgrok\b", lowered):
        return None
    for vendor, pattern in VENDOR_PATTERNS:
        if re.search(pattern, lowered, re.IGNORECASE):
            return vendor
    return None


def vendor_from_slug(slug: str | None) -> str | None:
    if not slug:
        return None
    key = slug.strip().lower().replace("_", "-")
    if key in {"xai", "x-ai"}:
        return None
    return SLUG_TO_VENDOR.get(key) or key.replace("-", " ").title()


DISPLAY_VENDOR_ALIASES = {
    "Zhipuai": "Z.ai (Zhipu)",
    "Inclusionai": "InclusionAI",
}


def display_vendor(vendor: str | None) -> str:
    value = vendor or "Unknown"
    return DISPLAY_VENDOR_ALIASES.get(value, value)


def extract_model_id(title: str, vendor: str | None = None) -> str | None:
    text = clean_text(title)
    suffix = r"(?:[- .](?:pro|flash|mini|nano|lite|preview|exp|experimental|max|fast|vision|reasoner|coder|instruct|chat|thinking)){0,4}"
    patterns = (
        rf"\bGPT[- .]?\d+(?:\.\d+)*{suffix}",
        rf"\bClaude\s+(?:Opus|Sonnet|Haiku)?\s*\d+(?:\.\d+)*{suffix}",
        rf"\bGemini\s+\d+(?:\.\d+)*{suffix}",
        rf"\bGemma\s+\d+(?:\.\d+)*{suffix}",
        r"\bDeepSeek[- .][A-Za-z0-9._-]+",
        r"\bQwen[A-Za-z0-9._-]*\d[A-Za-z0-9._-]*",
        rf"\bLlama\s+\d+(?:\.\d+)*{suffix}",
        rf"\bGLM[- .]?\d+(?:\.\d+)*{suffix}",
        r"\bKimi[- .]?[A-Za-z0-9._-]*\d[A-Za-z0-9._-]*",
        r"\b(?:Mistral|Codestral|Magistral|Voxtral|Ministral|Pixtral)[- .]?[A-Za-z0-9._-]*\d[A-Za-z0-9._-]*",
        r"\bFLUX(?:\.|[- ])?[A-Za-z0-9._-]*\d[A-Za-z0-9._-]*",
        rf"\b(?:Veo|Imagen|Sora|Nemotron|Phi)[- .]?\d+(?:\.\d+)*{suffix}",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0).strip(" .,:;–—")
    return None


def normalize_model_token(value: str) -> str:
    token = html.unescape(value).strip().lower()
    token = token.split(":", 1)[0]  # OpenRouter variants (:free, :thinking, ...)
    token = token.replace("_", "-").replace("/", "-")
    token = re.sub(r"\b(anthropic|openai|google|deepmind|deepseek-ai|deepseek|qwenlm|qwen|"
                   r"meta-llama|meta|mistralai|mistral|zai-org|moonshotai|black-forest-labs|"
                   r"nvidia|microsoft)\b[- ]*", "", token)
    token = re.sub(r"\b(introducing|introduce|released?|launch(?:ed)?|announced?|now|available|model)\b", " ", token)
    token = re.sub(r"\b(free|beta)\b$", "", token)
    token = re.sub(r"[-_. ]+", "-", token).strip("-")
    return token


def make_candidate(
    source: str,
    vendor: str | None,
    title: str,
    url: str,
    date: datetime | None,
    model_id: str | None,
    trust: int,
    *,
    kind: str = "release",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    vendor = vendor or detect_vendor(title)
    inferred = model_id or extract_model_id(title, vendor)
    return {
        "source": source,
        "vendor": vendor,
        "title": clean_text(title),
        "url": url,
        "date": parse_datetime(date),
        "model_id": inferred,
        "trust": int(trust),
        "kind": kind,
        "metadata": metadata or {},
    }


def canonical_model_key(candidate: dict[str, Any]) -> str:
    model_id = candidate.get("model_id") or extract_model_id(candidate.get("title", ""), candidate.get("vendor"))
    if model_id:
        return normalize_model_token(str(model_id))
    return normalize_model_token(candidate.get("title", ""))[:120]


def merge_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clusters: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        if not item.get("vendor") or not item.get("title") or not item.get("url"):
            continue
        vendor_key = re.sub(r"[^a-z0-9]+", "-", item["vendor"].lower()).strip("-")
        model_key = canonical_model_key(item)
        if not model_key:
            continue
        cluster_id = f"{vendor_key}:{model_key}"
        clusters.setdefault(cluster_id, []).append(item)

    events: list[dict[str, Any]] = []
    for cluster_id, group in clusters.items():
        # Exact URL/source duplicates first.
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for item in group:
            unique[(item["source"], item["url"])] = item
        group = list(unique.values())
        group.sort(key=lambda x: (x.get("trust", 0), x.get("date") or datetime.min.replace(tzinfo=UTC)), reverse=True)
        primary = group[0]
        dates = [x["date"] for x in group if x.get("date")]
        event_date = min(dates) if dates else datetime.now(UTC)
        sources = {x["source"] for x in group}
        max_trust = max(x["trust"] for x in group)
        if max_trust >= 95:
            confidence = "official"
        elif max_trust >= 80:
            confidence = "confirmed"
        elif max_trust >= 60 and len(sources) >= 2:
            confidence = "corroborated"
        else:
            confidence = "discovery"
        event_key = "evt_" + hashlib.sha256(cluster_id.encode("utf-8")).hexdigest()[:32]
        evidence = [
            {
                "source": x["source"],
                "url": x["url"],
                "title": x["title"],
                "trust": x["trust"],
                "date": x["date"].isoformat() if x.get("date") else None,
            }
            for x in group
        ]
        events.append(
            {
                "key": event_key,
                "cluster_id": cluster_id,
                "vendor": primary["vendor"],
                "model_id": primary.get("model_id") or canonical_model_key(primary),
                "title": primary["title"],
                "url": primary["url"],
                "date": event_date.isoformat(),
                "kind": primary.get("kind", "release"),
                "confidence": confidence,
                "max_trust": max_trust,
                "evidence": evidence,
            }
        )
    events.sort(key=lambda e: parse_datetime(e["date"]) or datetime.min.replace(tzinfo=UTC), reverse=True)
    return events


def should_publish(event: dict[str, Any]) -> bool:
    if event.get("max_trust", 0) >= 80:
        return True
    sources = {x.get("source") for x in event.get("evidence", [])}
    return event.get("max_trust", 0) >= 60 and len(sources) >= 2


def filter_window(candidates: list[dict[str, Any]], cutoff: datetime, *, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    kept = []
    for item in candidates:
        dt = item.get("date")
        if dt is None:
            if item.get("trust", 0) >= 90:
                kept.append(item)
            continue
        dt = parse_datetime(dt)
        if dt and cutoff <= dt <= now + timedelta(days=1):
            item["date"] = dt
            kept.append(item)
    return kept


def parse_feed(data: bytes) -> list[dict[str, Any]]:
    # Repair only the most common invalid bare ampersands while preserving entities.
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
    root = ET.fromstring(text)
    out: list[dict[str, Any]] = []

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    for node in root.iter():
        if local(node.tag) not in {"item", "entry"}:
            continue
        title = ""
        url = ""
        date_value = None
        for child in list(node):
            name = local(child.tag)
            if name == "title" and not title:
                title = "".join(child.itertext()).strip()
            elif name == "link" and not url:
                url = (child.get("href") or (child.text or "")).strip()
            elif name in {"pubdate", "published", "updated", "date"} and date_value is None:
                date_value = (child.text or "").strip()
        if title and url:
            out.append({"title": clean_text(title), "url": url, "date": parse_datetime(date_value)})
    return out


def parse_modelsdev(payload: Any) -> list[dict[str, Any]]:
    records: list[tuple[str | None, str, dict[str, Any]]] = []
    if isinstance(payload, list):
        for row in payload:
            if isinstance(row, dict) and row.get("id"):
                records.append((None, str(row["id"]), row))
    elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
        for row in payload["data"]:
            if isinstance(row, dict) and row.get("id"):
                records.append((None, str(row["id"]), row))
    elif isinstance(payload, dict) and any("/" in str(key) and isinstance(value, dict) for key, value in payload.items()):
        for model_id, row in payload.items():
            if "/" in str(model_id) and isinstance(row, dict):
                records.append((None, str(model_id), row))
    elif isinstance(payload, dict) and isinstance(payload.get("models"), dict):
        # Provider-agnostic/lab-shaped format used by newer models.dev exports.
        for lab, models in payload["models"].items():
            if not isinstance(models, dict):
                continue
            for model_id, row in models.items():
                if isinstance(row, dict):
                    records.append((str(lab), str(model_id), row))
    elif isinstance(payload, dict):
        # Main API shape: {provider_id: {models: {model_id: {...}}}}.
        for provider, provider_data in payload.items():
            if not isinstance(provider_data, dict) or not isinstance(provider_data.get("models"), dict):
                continue
            for model_id, row in provider_data["models"].items():
                if isinstance(row, dict):
                    records.append((str(provider), str(model_id), row))

    out = []
    seen = set()
    for provider, raw_model_id, row in records:
        raw_model_id = raw_model_id.strip("/")
        name = clean_text(str(row.get("name") or raw_model_id.split("/")[-1]))

        if "/" in raw_model_id:
            # Aggregator catalogs commonly retain the creator namespace here.
            model_id = raw_model_id
            creator_slug = raw_model_id.split("/", 1)[0]
            vendor = vendor_from_slug(creator_slug)
        else:
            provider_slug = (provider or "").strip().lower().replace("_", "-")
            if provider_slug in AGGREGATOR_PROVIDERS:
                vendor = detect_vendor(name)
                creator_slug = VENDOR_TO_SLUG.get(vendor or "")
                if not vendor or not creator_slug:
                    continue
                model_id = f"{creator_slug}/{raw_model_id}"
            else:
                creator_slug = provider_slug or raw_model_id.split("/", 1)[0]
                vendor = vendor_from_slug(creator_slug)
                if not creator_slug:
                    continue
                model_id = f"{creator_slug}/{raw_model_id}" if provider else raw_model_id

        if vendor is None or model_id in seen:
            continue
        seen.add(model_id)
        release_date = parse_datetime(row.get("release_date") or row.get("created") or row.get("created_at"))
        out.append(
            {
                "model_id": model_id,
                "vendor": vendor,
                "title": name,
                "date": release_date,
                "url": f"https://models.dev/models/{urllib.parse.quote(model_id, safe='/')}",
                "open_weights": bool(row.get("open_weights", False)),
            }
        )
    return out


def parse_openrouter(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("canonical_slug") or row.get("id") or "").strip()
        if not model_id or "/" not in model_id:
            continue
        vendor = vendor_from_slug(model_id.split("/", 1)[0])
        if vendor is None:
            continue
        name = clean_text(str(row.get("name") or model_id.split("/", 1)[1]))
        out.append(
            {
                "model_id": model_id,
                "vendor": vendor,
                "title": name,
                "date": parse_datetime(row.get("created")),
                "url": "https://openrouter.ai/" + urllib.parse.quote(model_id, safe="/.-"),
            }
        )
    return out


def parse_sitemap(data: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(data.decode("utf-8", errors="replace"))

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    rows: list[dict[str, Any]] = []
    for node in root.iter():
        if local(node.tag) != "url":
            continue
        loc = None
        lastmod = None
        for child in list(node):
            name = local(child.tag)
            if name == "loc":
                loc = clean_text(child.text or "")
            elif name == "lastmod":
                lastmod = parse_datetime(child.text or "")
        if loc:
            rows.append({"url": loc, "lastmod": lastmod})
    return rows


def title_from_url_slug(url: str) -> str:
    slug = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    return re.sub(r"[-_]+", " ", slug).strip().title()


def parse_deepseek_news(data: bytes) -> list[dict[str, Any]]:
    text = data.decode("utf-8", errors="replace")
    rx = re.compile(
        r'<a\b[^>]*href=["\'](?P<href>/news/news(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})/?)["\'][^>]*>(?P<title>.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    out = []
    seen = set()
    for match in rx.finditer(text):
        href = match.group("href").rstrip("/") + "/"
        if href in seen:
            continue
        seen.add(href)
        try:
            dt = datetime(2000 + int(match.group("y")), int(match.group("m")), int(match.group("d")), tzinfo=UTC)
        except ValueError:
            continue
        title = clean_text(match.group("title")) or f"DeepSeek release {dt.date().isoformat()}"
        out.append(
            {
                "model_id": extract_model_id(title, "DeepSeek"),
                "vendor": "DeepSeek",
                "title": title,
                "date": dt,
                "url": "https://api-docs.deepseek.com" + href,
            }
        )
    return out


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = HTTP_TIMEOUT,
    retries: int = MAX_HTTP_RETRIES,
) -> tuple[bytes, Any, int]:
    merged_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        merged_headers.update(headers)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=data, method=method, headers=merged_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(), response.headers, int(response.status)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 and attempt < retries:
                retry_after = exc.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else 2.0 * (attempt + 1)
                except ValueError:
                    wait = 2.0 * (attempt + 1)
                time.sleep(min(wait, 30.0))
                continue
            if 500 <= exc.code < 600 and attempt < retries:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= retries:
                raise
            time.sleep(min(2 ** attempt, 8))
    assert last_error is not None
    raise last_error


def http_get_bytes(url: str, **kwargs: Any) -> tuple[bytes, Any]:
    body, headers, _ = _request(url, **kwargs)
    return body, headers


def http_get_json(url: str, **kwargs: Any) -> tuple[Any, Any]:
    body, headers = http_get_bytes(url, headers={"Accept": "application/json", **kwargs.pop("headers", {})}, **kwargs)
    return json.loads(body.decode("utf-8", errors="strict")), headers


def collect_anthropic(
    cutoff: datetime,
    *,
    http_get_bytes: Callable[..., tuple[bytes, Any]] = http_get_bytes,
) -> list[dict[str, Any]]:
    body, _ = http_get_bytes("https://www.anthropic.com/sitemap.xml")
    out = []
    for row in parse_sitemap(body):
        url = row["url"]
        dt = row.get("lastmod")
        if "/news/" not in urllib.parse.urlsplit(url).path or not dt or dt < cutoff:
            continue
        title = title_from_url_slug(url)
        if not looks_like_release(title):
            continue
        out.append(
            make_candidate(
                "official", "Anthropic", title, url, dt, None, 100,
                metadata={"feed": "anthropic-sitemap"},
            )
        )
    return out


def collect_official_feed(source: dict[str, Any], cutoff: datetime) -> list[dict[str, Any]]:
    body, _ = http_get_bytes(source["url"])
    items = []
    for row in parse_feed(body):
        if source.get("filter") and not looks_like_release(row["title"]):
            continue
        item = make_candidate(
            "official",
            source["vendor"],
            row["title"],
            row["url"],
            row["date"],
            None,
            100,
            metadata={"feed": source["id"]},
        )
        if item["date"] is None or item["date"] >= cutoff:
            items.append(item)
    return items


def collect_deepseek(cutoff: datetime) -> list[dict[str, Any]]:
    body, _ = http_get_bytes("https://api-docs.deepseek.com/news/")
    return [
        make_candidate("deepseek", row["vendor"], row["title"], row["url"], row["date"], row["model_id"], 100)
        for row in parse_deepseek_news(body)
        if row["date"] >= cutoff and looks_like_release(row["title"])
    ]


HF_BROAD_ORG_CORE_MODEL_PATTERNS = {
    "microsoft": re.compile(r"^(?:phi[- .]?\d|mai[- .]?\d)", re.IGNORECASE),
    "nvidia": re.compile(r"(?:nemotron|nvlm|cosmos)", re.IGNORECASE),
}


def hf_trust_for_model(org: str, model_name: str) -> int:
    """Trust first-party HF uploads, but demote noisy broad organizations.

    Microsoft and NVIDIA publish many research checkpoints, conversions and
    derived artifacts. Their core model families remain authoritative; other
    uploads are discovery evidence and require independent corroboration.
    """
    pattern = HF_BROAD_ORG_CORE_MODEL_PATTERNS.get(org.lower())
    if pattern is None:
        return 90
    return 90 if pattern.search(model_name) else 55


def collect_hf_org(
    org: str,
    cutoff: datetime,
    *,
    http_get_json: Callable[..., tuple[Any, Any]] = http_get_json,
) -> list[dict[str, Any]]:
    url = "https://huggingface.co/api/models?" + urllib.parse.urlencode(
        {"author": org, "sort": "createdAt", "direction": "-1", "limit": "100"}
    )
    out: list[dict[str, Any]] = []
    visited = set()
    for _ in range(100):  # corruption/loop guard, not a data cap in normal operation
        if not url or url in visited:
            break
        visited.add(url)
        rows, headers = http_get_json(url)
        if not isinstance(rows, list) or not rows:
            break
        page_dates = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            created = parse_datetime(row.get("createdAt"))
            if created:
                page_dates.append(created)
            if not created or created < cutoff:
                continue
            tags = row.get("tags") or []
            license_id = next((str(t)[8:] for t in tags if str(t).startswith("license:")), None)
            vendor = vendor_from_slug(org)
            if vendor is None:
                continue
            out.append(
                make_candidate(
                    "huggingface",
                    vendor,
                    str(row["id"]).split("/", 1)[-1],
                    "https://huggingface.co/" + str(row["id"]),
                    created,
                    str(row["id"]),
                    hf_trust_for_model(org, str(row["id"]).split("/", 1)[-1]),
                    kind="open_weights",
                    metadata={
                        "pipeline": row.get("pipeline_tag"),
                        "license": license_id,
                        "downloads": row.get("downloads"),
                    },
                )
            )
        if page_dates and min(page_dates) < cutoff:
            break
        url = parse_link_next(header_value(headers, "Link"))
    return out


def collect_modelsdev(
    cutoff: datetime,
    *,
    http_get_json: Callable[..., tuple[Any, Any]] = http_get_json,
) -> list[dict[str, Any]]:
    payload, _ = http_get_json("https://models.dev/models.json")
    out = []
    for row in parse_modelsdev(payload):
        if row["date"] and row["date"] >= cutoff:
            out.append(
                make_candidate(
                    "models.dev",
                    row["vendor"],
                    row["title"],
                    row["url"],
                    row["date"],
                    row["model_id"],
                    85,
                    kind="open_weights" if row.get("open_weights") else "release",
                )
            )
    return out


def collect_openrouter(cutoff: datetime) -> list[dict[str, Any]]:
    url = "https://openrouter.ai/api/v1/models?" + urllib.parse.urlencode({"sort": "newest", "output_modalities": "all"})
    payload, _ = http_get_json(url)
    out = []
    for row in parse_openrouter(payload):
        if row["date"] and row["date"] >= cutoff:
            out.append(
                make_candidate(
                    "openrouter",
                    row["vendor"],
                    row["title"],
                    row["url"],
                    row["date"],
                    row["model_id"],
                    65,
                    kind="availability",
                )
            )
    return out


def collect_hn(cutoff: datetime) -> list[dict[str, Any]]:
    out = []
    seen = set()
    cutoff_epoch = int(cutoff.timestamp())
    for query in HN_QUERIES:
        params = urllib.parse.urlencode(
            {
                "query": query,
                "tags": "story",
                "hitsPerPage": 8,
                "numericFilters": f"created_at_i>{cutoff_epoch},points>30",
            }
        )
        try:
            payload, _ = http_get_json("https://hn.algolia.com/api/v1/search_by_date?" + params, retries=1)
        except Exception as exc:  # secondary source: continue per query
            log(f"HN query failed ({query}): {exc}")
            continue
        for row in payload.get("hits", []) if isinstance(payload, dict) else []:
            if not isinstance(row, dict):
                continue
            object_id = str(row.get("objectID") or "")
            title = clean_text(str(row.get("title") or ""))
            if not object_id or object_id in seen or not looks_like_release(title):
                continue
            seen.add(object_id)
            vendor = detect_vendor(title)
            if vendor is None:
                continue
            out.append(
                make_candidate(
                    "hn",
                    vendor,
                    title,
                    row.get("url") or f"https://news.ycombinator.com/item?id={object_id}",
                    parse_datetime(row.get("created_at")),
                    None,
                    25,
                    metadata={"points": row.get("points", 0), "comments": row.get("num_comments", 0)},
                )
            )
    return out


def collect_all(cutoff: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    jobs: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = []
    for source in OFFICIAL_FEEDS:
        jobs.append((source["id"], lambda source=source: collect_official_feed(source, cutoff)))
    jobs.append(("anthropic", lambda: collect_anthropic(cutoff)))
    jobs.append(("deepseek", lambda: collect_deepseek(cutoff)))
    jobs.append(("models.dev", lambda: collect_modelsdev(cutoff)))
    jobs.append(("openrouter", lambda: collect_openrouter(cutoff)))
    jobs.append(("hacker-news", lambda: collect_hn(cutoff)))
    for org in HF_ORGS:
        jobs.append((f"hf:{org}", lambda org=org: collect_hf_org(org, cutoff)))

    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(12, len(jobs))) as executor:
        futures = {executor.submit(fn): name for name, fn in jobs}
        for future in as_completed(futures):
            name = futures[future]
            try:
                rows = future.result()
                candidates.extend(rows)
                log(f"{name}: {len(rows)} candidate(s)")
            except Exception as exc:
                message = f"{name}: {type(exc).__name__}: {exc}"
                errors.append(message)
                log(f"SOURCE ERROR — {message}")
    return filter_window(candidates, cutoff), errors


def new_state() -> dict[str, Any]:
    return {"version": 3, "pending": {}, "delivered": {}, "meta": {}}


def load_state(path: os.PathLike[str] | str = DEFAULT_STATE_PATH) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return new_state()
    if not isinstance(state, dict) or state.get("version") != 3:
        return new_state()
    if not isinstance(state.get("pending"), dict) or not isinstance(state.get("delivered"), dict):
        return new_state()
    if not isinstance(state.get("meta"), dict):
        state["meta"] = {}
    return state


def save_state(path: os.PathLike[str] | str, state: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def touch_heartbeat(
    state: dict[str, Any], *, now: datetime | None = None, interval_days: int = 30
) -> bool:
    """Touch durable state infrequently so public-repo schedules stay active.

    GitHub disables scheduled workflows in public repositories after 60 days with
    no repository activity. A monthly state-only commit stays comfortably inside
    that window without creating per-poll commit noise.
    """
    now = now or datetime.now(UTC)
    meta = state.setdefault("meta", {})
    previous = parse_datetime(meta.get("last_heartbeat_at"))
    if previous and now - previous < timedelta(days=interval_days):
        return False
    meta["last_heartbeat_at"] = now.isoformat()
    return True


def prune_delivered(state: dict[str, Any], *, now: datetime | None = None, days: int = STATE_RETENTION_DAYS) -> None:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    kept = {}
    for key, record in state.get("delivered", {}).items():
        dt = parse_datetime(record.get("at") if isinstance(record, dict) else None)
        if dt and dt >= cutoff:
            kept[key] = record
    state["delivered"] = kept


def enqueue_events(state: dict[str, Any], events: list[dict[str, Any]]) -> int:
    added = 0
    for event in events:
        key = event["key"]
        if not should_publish(event):
            continue
        if key in state["delivered"] or key in state["pending"]:
            continue
        state["pending"][key] = {
            "event": event,
            "attempts": 0,
            "last_error": None,
            "queued_at": datetime.now(UTC).isoformat(),
        }
        added += 1
    return added


def format_date(value: Any) -> str:
    dt = parse_datetime(value)
    return dt.strftime("%Y-%m-%d · %H:%M UTC") if dt else "unknown"


def discord_payload(event: dict[str, Any], *, username: str = "Signal2 · AI Release Intelligence") -> dict[str, Any]:
    evidence = sorted(event.get("evidence", []), key=lambda x: x.get("trust", 0), reverse=True)
    evidence_lines = []
    for row in evidence[:6]:
        label = SOURCE_LABELS.get(row.get("source"), row.get("source", "source"))
        url = str(row.get("url") or "")
        evidence_lines.append(f"[{label}]({url})")
    if len(evidence) > 6:
        evidence_lines.append(f"+{len(evidence) - 6} additional source(s)")

    kind_label = {
        "release": "MODEL RELEASE",
        "open_weights": "OPEN WEIGHTS",
        "availability": "MODEL AVAILABILITY",
    }.get(event.get("kind"), "MODEL RELEASE")
    confidence_label = {
        "official": "Official",
        "confirmed": "Confirmed",
        "corroborated": "Corroborated",
        "discovery": "Discovery",
    }.get(event.get("confidence"), "Confirmed")
    title = clean_text(str(event.get("title") or event.get("model_id") or "AI model release"))[:256]
    model_id = clean_text(str(event.get("model_id") or "—"))
    embed = {
        "title": title,
        "url": event.get("url"),
        "description": f"**{kind_label}** · {confidence_label}",
        "color": SOURCE_COLORS.get(display_vendor(event.get("vendor")), 0x5865F2),
        "fields": [
            {"name": "Vendor", "value": display_vendor(event.get("vendor"))[:1024], "inline": True},
            {"name": "Model", "value": f"`{model_id[:1000]}`", "inline": True},
            {"name": "Published", "value": format_date(event.get("date")), "inline": True},
            {"name": "Evidence", "value": " · ".join(evidence_lines)[:1024] or "—", "inline": False},
        ],
        "footer": {"text": "Signal2 · autonomous AI model release intelligence"},
        "timestamp": (parse_datetime(event.get("date")) or datetime.now(UTC)).isoformat(),
    }
    return {
        "username": username,
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }


class DiscordSender:
    def __init__(self, webhook_url: str, *, username: str = "Signal2 · AI Release Intelligence") -> None:
        webhook_url = webhook_url.strip()
        if not webhook_url.startswith("https://"):
            raise ValueError("Discord webhook must be an https:// URL")
        parts = urllib.parse.urlsplit(webhook_url)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        query = [(k, v) for k, v in query if k != "wait"] + [("wait", "true")]
        self.url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment))
        self.username = username

    def send(self, event: dict[str, Any]) -> tuple[bool, str | None, str | None]:
        payload = discord_payload(event, username=self.username)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for attempt in range(5):
            request = urllib.request.Request(
                self.url,
                data=data,
                method="POST",
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read()
                    if 200 <= response.status < 300:
                        message_id = None
                        if body:
                            try:
                                obj = json.loads(body.decode("utf-8"))
                                message_id = str(obj.get("id")) if isinstance(obj, dict) and obj.get("id") else None
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                pass
                        return True, message_id, None
                    return False, None, f"HTTP {response.status}"
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                if exc.code == 429 and attempt < 4:
                    wait = None
                    try:
                        payload_429 = json.loads(raw.decode("utf-8"))
                        wait = float(payload_429.get("retry_after"))
                    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
                        pass
                    if wait is None:
                        try:
                            wait = float(exc.headers.get("Retry-After", "2"))
                        except ValueError:
                            wait = 2.0
                    time.sleep(min(max(wait, 0.2), 60.0))
                    continue
                if 500 <= exc.code < 600 and attempt < 4:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                detail = clean_text(raw.decode("utf-8", errors="replace"))[:300]
                return False, None, f"HTTP {exc.code}: {detail}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < 4:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                return False, None, f"{type(exc).__name__}: {exc}"
        return False, None, "delivery retries exhausted"


def deliver_outbox(
    state: dict[str, Any],
    sender: Any,
    *,
    limit: int | None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    records = list(state.get("pending", {}).items())
    records.sort(
        key=lambda kv: parse_datetime(kv[1].get("event", {}).get("date")) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    if limit is not None:
        records = records[: max(0, limit)]
    delivered = failed = 0
    for key, record in records:
        event = record["event"]
        ok, message_id, error = sender.send(event)
        if ok:
            state["delivered"][key] = {
                "at": datetime.now(UTC).isoformat(),
                "message_id": message_id,
                "title": event.get("title"),
            }
            state["pending"].pop(key, None)
            delivered += 1
            sleep_fn(0.35)
        else:
            record["attempts"] = int(record.get("attempts", 0)) + 1
            record["last_error"] = error or "unknown delivery failure"
            record["last_attempt_at"] = datetime.now(UTC).isoformat()
            failed += 1
    return {"delivered": delivered, "failed": failed, "remaining": len(state.get("pending", {}))}


def run_status(*, delivery_failures: int, source_errors: list[str]) -> int:
    """Return a non-zero process status for any operational degradation."""
    return 1 if delivery_failures or source_errors else 0


def print_report(events: list[dict[str, Any]], errors: list[str], cutoff: datetime) -> None:
    print(f"\nSignal2 dry-run · since {cutoff.isoformat()} · {len(events)} publishable event(s)")
    for event in events:
        sources = ", ".join(sorted({SOURCE_LABELS.get(x["source"], x["source"]) for x in event["evidence"]}))
        print(f"- {format_date(event['date'])} | {display_vendor(event['vendor'])} | {event['title']} | {event['confidence']} | {sources}")
        print(f"  {event['url']}")
    if errors:
        print(f"\n{len(errors)} source error(s):")
        for error in errors:
            print(f"- {error}")


def execute(mode: str, *, days: int, publish: bool, state_path: Path) -> int:
    now = datetime.now(UTC)
    cutoff = now - (timedelta(days=days) if mode == "digest" else timedelta(hours=POLL_LOOKBACK_HOURS))
    candidates, errors = collect_all(cutoff)
    events = [event for event in merge_candidates(candidates) if should_publish(event)]

    if not publish:
        print_report(events, errors, cutoff)
        return 0

    webhook = os.environ.get("SIGNAL2_WEBHOOK", "").strip()
    if not webhook:
        log("FATAL — SIGNAL2_WEBHOOK is required with --publish")
        return 2

    state = load_state(state_path)
    prune_delivered(state, now=now)
    touch_heartbeat(state, now=now)
    added = enqueue_events(state, events)
    # Persist the outbox before delivery at process level. The GitHub workflow then
    # commits the final state even if delivery fails.
    save_state(state_path, state)
    log(f"outbox: {added} new, {len(state['pending'])} pending")

    sender = DiscordSender(webhook)
    limit = None if mode == "digest" else POLL_DELIVERY_LIMIT
    result = deliver_outbox(state, sender, limit=limit)
    save_state(state_path, state)
    log(
        f"delivery: {result['delivered']} confirmed, {result['failed']} failed, "
        f"{result['remaining']} pending · {len(errors)} source error(s)"
    )
    # State is persisted before returning non-zero so the next scheduled cycle can
    # reconcile automatically while Actions makes the degradation immediately visible.
    return run_status(delivery_failures=result["failed"], source_errors=errors)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Signal2 — autonomous AI model release intelligence")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--poll", action="store_true", help=f"scan the last {POLL_LOOKBACK_HOURS} hours")
    mode.add_argument("--digest", nargs="?", type=int, const=7, metavar="DAYS", help="scan a retrospective window (default: 7 days)")
    parser.add_argument("--publish", action="store_true", help="publish through SIGNAL2_WEBHOOK and update durable state")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="state file path")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    if args.digest is not None:
        if args.digest < 1 or args.digest > 30:
            raise SystemExit("--digest DAYS must be between 1 and 30")
        return execute("digest", days=args.digest, publish=args.publish, state_path=Path(args.state))
    return execute("poll", days=0, publish=args.publish, state_path=Path(args.state))


if __name__ == "__main__":
    raise SystemExit(main())
