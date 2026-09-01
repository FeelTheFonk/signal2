#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Signal2 — Veille IA autonome pour le salon #llm du serveur Discord MASSRACE.

Surveille en continu toutes les sorties de modèles d'IA (Anthropic, OpenAI,
Google, Meta, Mistral, DeepSeek, Qwen/Alibaba, Z.ai/Zhipu, Moonshot/Kimi, xAI,
Black Forest Labs, NVIDIA, Microsoft...) via 4 couches redondantes :

  1. Canaux officiels (RSS + pages news)  — annonces éditeur
  2. Hugging Face (API publique)           — nouveaux poids ouverts par org
  3. GitHub (API évènements d'org)         — releases / tags
  4. Hacker News (API Algolia)             — buzz fort = filet de sécurité

Modes :
  --poll          : un cycle de veille (envoie les nouveautés sur Discord)
  --loop [MIN]    : cycles répétés avec pause de MIN minutes (déf. 15)
  --test          : cycle à blanc, rien n'est envoyé
  --digest [DAYS] : publie un digest rétrospectif des N derniers jours
  --launch-msg    : publie le message de présentation du système

Stdlib uniquement (aucune dépendance à installer).
État : signal2_state.json · Journal : signal2.log
"""

import argparse
import email.utils
import hashlib
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Chemins & constantes
# ---------------------------------------------------------------------------

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "signal2_config.json")
STATE_PATH = os.path.join(BASE, "signal2_state.json")
LOG_PATH = os.path.join(BASE, "signal2.log")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Signal2-Veille/1.0")

UTC = timezone.utc

DEFAULT_CONFIG = {
    # Le webhook Discord est fourni via la variable d'environnement
    # SIGNAL2_WEBHOOK (secret GitHub Actions) ou signal2_config.json (local,
    # gitignored). Il ne doit JAMAIS être commité dans ce fichier (repo public).
    "webhook_url": "",
    "webhook_name": "Signal2 · Veille IA",
    # Couche 1 — canaux officiels. "filter": strong = filtre par mots-clés,
    # off = tout publier (chaîne news déjà spécialisée).
    "official_sources": [
        {"id": "openai-rss",   "vendor": "OpenAI",    "kind": "rss",
         "url": "https://openai.com/news/rss.xml", "filter": "strong"},
        {"id": "mistral-rss",  "vendor": "Mistral AI", "kind": "rss",
         "url": "https://mistral.ai/news/rss", "filter": "strong"},
        {"id": "qwen-rss",     "vendor": "Qwen (Alibaba)", "kind": "rss",
         "url": "https://qwenlm.github.io/blog/index.xml", "filter": "off"},
        {"id": "google-rss",   "vendor": "Google DeepMind", "kind": "rss",
         "url": "https://blog.google/technology/google-deepmind/rss/", "filter": "strong"},
        {"id": "anthropic",    "vendor": "Anthropic", "kind": "links",
         "url": "https://www.anthropic.com/news",
         "link_prefix": "/news/", "filter": "off", "fetch_dates": True},
        {"id": "xai",          "vendor": "xAI", "kind": "links",
         "url": "https://x.ai/news",
         "link_prefix": "/news/", "filter": "off", "fetch_dates": True},
        {"id": "deepseek",     "vendor": "DeepSeek", "kind": "links",
         "url": "https://api-docs.deepseek.com/news/",
         "link_prefix": "/news/", "filter": "off", "date_in_url": r"/news/news(\d{2})(\d{2})(\d{2})$"}
    ],
    # Couche 2 — Hugging Face : nouveaux modèles publics par organisation.
    "hf_orgs": ["deepseek-ai", "Qwen", "zai-org", "moonshotai", "meta-llama",
                "mistralai", "openai", "xai-org", "black-forest-labs",
                "google", "nvidia", "microsoft"],
    # Couche 3 — GitHub : évènements publics (releases) par organisation.
    "github_orgs": ["deepseek-ai", "QwenLM", "zai-org", "moonshotai",
                    "meta-llama", "mistralai", "openai", "google",
                    "black-forest-labs", "xai-org"],
    # Couche 4 — Hacker News : filet de sécurité pour tout ce qui échapperait
    # aux couches officielles (nouveaux acteurs, surprises type "Fable 5.1").
    "hn_queries": ["anthropic", "claude", "fable", "openai", "gpt", "gemini",
                   "deepseek", "qwen", "llama", "grok", "kimi", "glm",
                   "mistral", "flux", "moonshot", "z.ai", "open weights"],
    "hn_min_points_poll": 40,
    "hn_min_points_digest": 80,
    "hn_window_hours": 48,
    # Publication
    "max_items_per_cycle": 30,
    "embeds_per_message": 10
}

# Mots-clés de sortie de modèle / annonce technique (filtre "strong")
MODEL_NAMES = [
    "gpt", "chatgpt", "claude", "fable", "mythos", "opus", "sonnet", "haiku",
    "gemini", "gemma", "imagen", "veo", "grok", "llama", "qwen", "deepseek",
    "glm", "kimi", "mistral", "codestral", "magistral", "voxtral", "ministral",
    "pixtral", "flux", "sora", "codex", "o1", "o3", "o4", "operator",
    "daybreak", "nemotron", "phi-", "copilot", "jamba", "command-r", "ernie",
    "hunyuan", "abab", "aya", "solar-", "gemma"
]
RELEASE_ACTIONS = [
    "introduc", "launch", "releas", "announc", "unveil", "debut",
    "now available", "available", "preview", "upgrad", "price", "cost",
    "context window", "faster", "speed", "benchmark", "frontier", "state-of-the-art"
]
STRONG_PHRASES = [
    "open weight", "open-sourc", "open sourc", "weights", "research preview",
    "new model", "latest model", "next model", "frontier model"
]
HN_TITLE_RX = re.compile(
    r"(launch|releas|introduc|announc|unveil|debut|open.?sourc|weight|"
    r"new model|frontier|benchmark|state.of.the.art|drops?|ships?|out now)",
    re.I)
# Titre contenant un nom de modèle + version (ex. "Fable 5.1", "GPT-5.6",
# "GLM-5.3", "Kimi K3") : signal de sortie même sans verbe d'annonce.
HN_MODEL_VERSION_RX = re.compile(
    r"(claude|fable|mythos|gpt|glm|kimi|deepseek|qwen|llama|grok|gemini|"
    r"mistral|opus|sonnet|gemma|sora|flux|o[134])[\s-]*v?\d+(\.\d+)?", re.I)
HN_EXCLUDE_RX = re.compile(r"^(ask hn|show hn|tell hn)\b|hiring|who is hiring", re.I)

# Détection éditeur par nom — ordre = priorité
VENDOR_PATTERNS = [
    ("Anthropic",       r"\bclaude|\bfable|\bmythos|\bopus|\bsonnet|\bhaiku|anthropic"),
    ("OpenAI",          r"openai|\bgpt|\bchatgpt|\bsora\b|\bcodex|\bo[134]\b|\boperator|davinci|whisper"),
    ("Google DeepMind", r"gemini|gemma|imagen|\bveo\b|deepmind|googleness|notebooklm"),
    ("DeepSeek",        r"deepseek"),
    ("Qwen (Alibaba)",  r"qwen|alibaba|tongyi|wan2"),
    ("Meta AI",         r"llama|\bmeta\b|code llama"),
    ("Z.ai (Zhipu)",    r"\bglm|z\.?ai|zhipu|chatglm"),
    ("Moonshot AI",     r"kimi|moonshot"),
    ("Mistral AI",      r"mistral|codestral|magistral|voxtral|ministral|pixtral|shieldstral"),
    ("xAI",             r"\bgrok\b|\bxai\b|grokkit"),
    ("Black Forest Labs", r"flux|black.?forest|\bbfl\b"),
    ("NVIDIA",          r"nvidia|nemotron|\blnm\b"),
    ("Microsoft",       r"microsoft|\bphi-|copilot|mai-1|majidoma"),
    ("AI21",            r"ai21|jamba"),
    ("Cohere",          r"cohere|command-?r|aya"),
    ("Reka",            r"\breka\b"),
    ("MiniMax",         r"minimax|abab"),
    ("01.AI",           r"yi-|01\.ai"),
    ("Tencent",         r"hunyuan|tencent"),
    ("Baidu",           r"ernie|baidu"),
    ("Upstage",         r"solar-|upstage"),
]

VENDOR_STYLE = {
    "Anthropic":         0xD97757, "OpenAI":           0x10A37F,
    "Google DeepMind":   0x4285F4, "DeepSeek":         0x4D6BFE,
    "Qwen (Alibaba)":    0xFF6A00, "Meta AI":          0x0866FF,
    "Z.ai (Zhipu)":      0x2F6BFF, "Moonshot AI":      0x8B5CF6,
    "Mistral AI":        0xFA500F, "xAI":              0x9AA0A6,
    "Black Forest Labs": 0x5865F2, "NVIDIA":           0x76B900,
    "Microsoft":         0x00A4EF, "_default":         0x5865F2,
}
VENDOR_EMOJI = {
    "Anthropic": "🟠", "OpenAI": "🟢", "Google DeepMind": "🔵", "DeepSeek": "🔷",
    "Qwen (Alibaba)": "🟠", "Meta AI": "🔵", "Z.ai (Zhipu)": "🌐",
    "Moonshot AI": "🟣", "Mistral AI": "🟠", "xAI": "⚪",
    "Black Forest Labs": "🎨", "NVIDIA": "💚", "Microsoft": "🔹",
}

MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"]

# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def http_get(url, headers=None, timeout=25, retries=2):
    """GET avec retries ; renvoie le corps (str) ou lève une Exception."""
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001 — robustesse veille
            last_err = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise last_err


def http_post_json(url, payload):
    """POST JSON (webhook Discord) ; gère 429 Retry-After ; renvoie True/False."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read().decode()).get("retry_after", 2))
                except Exception:  # noqa: BLE001
                    wait = 2
                time.sleep(min(wait, 15) + 0.5)
                continue
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            log(f"ERREUR POST webhook {e.code}: {body}")
            return False
        except Exception as e:  # noqa: BLE001
            log(f"ERREUR POST webhook: {e}")
            if attempt == 3:
                return False
            time.sleep(3)
    return False


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def save_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def item_key(src, ident):
    return hashlib.sha1(f"{src}|{ident}".encode("utf-8")).hexdigest()


def detect_vendor(text):
    t = text.lower()
    for vendor, pat in VENDOR_PATTERNS:
        if re.search(pat, t):
            return vendor
    return None


def passes_filter(title, mode):
    """Filtre anti-bruit pour les canaux officiels généralistes.

    Garde : (nom de modèle + verbe d'annonce) OU phrase de sortie explicite
    (poids ouverts, research preview, nouveau modèle...).
    """
    if mode == "off":
        return True
    t = htmllib.unescape(title).lower()
    has_name = any(tok in t for tok in MODEL_NAMES)
    has_action = any(tok in t for tok in RELEASE_ACTIONS)
    if has_name and has_action:
        return True
    return any(p in t for p in STRONG_PHRASES)


def too_old(date, days=7):
    """True si l'item date de plus de `days` jours (fenêtre d'annonce)."""
    return bool(date) and date < datetime.now(UTC) - timedelta(days=days)


def fmt_date(dt):
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (f"{dt.day} {MOIS_FR[dt.month - 1]} {dt.year} "
            f"à {dt.strftime('%H:%M')} UTC")


def parse_http_date(s):
    try:
        return email.utils.parsedate_to_datetime(s).astimezone(UTC)
    except Exception:  # noqa: BLE001
        return None


def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
    except Exception:  # noqa: BLE001
        return None

# ---------------------------------------------------------------------------
# Couche 1 — canaux officiels (RSS + pages news HTML)
# ---------------------------------------------------------------------------

def fetch_rss(source):
    """RSS 2.0 / Atom -> liste d'items {id,title,url,date}."""
    xml = http_get(source["url"])
    items = []
    root = ET.fromstring(re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)",
                                "&amp;", xml))
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        date = parse_http_date(it.findtext("pubDate") or "")
        if title and link:
            items.append({"id": link, "title": title, "url": link, "date": date})
    if not items:  # Atom
        ns = "{http://www.w3.org/2005/Atom}"
        for it in root.iter(f"{ns}entry"):
            title = (it.findtext(f"{ns}title") or "").strip()
            link = ""
            for l in it.findall(f"{ns}link"):
                if l.get("href"):
                    link = l.get("href")
                    if l.get("rel") in (None, "alternate"):
                        break
            date = parse_iso(it.findtext(f"{ns}updated") or "")
            if title and link:
                items.append({"id": link, "title": title, "url": link, "date": date})
    return items


DATE_META_RX = re.compile(
    r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2}[^"]*)"|'
    r'property="article:published_time"\s+content="(\d{4}-\d{2}-\d{2}[^"]*)"|'
    r'<time[^>]+datetime="(\d{4}-\d{2}-\d{2}[^"]*)"',
    re.I)


def fetch_article_date(url):
    """Date de publication d'une page d'annonce (best effort)."""
    try:
        html = http_get(url, timeout=15, retries=1)
        m = DATE_META_RX.search(html)
        if m:
            for g in m.groups():
                if g:
                    return parse_iso(g)
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch_links_page(source):
    """Page news HTML -> items {id,title,url,date?} (dates résolues à la volée)."""
    html = http_get(source["url"])
    base = urllib.parse.urlsplit(source["url"])
    origin = f"{base.scheme}://{base.netloc}"
    prefix = source.get("link_prefix", "/news/")
    rx = re.compile(r'href="(' + re.escape(prefix) + r'[a-zA-Z0-9_\-/.]+)"')
    seen, items = set(), []
    for m in rx.finditer(html):
        path = m.group(1).split("?")[0].rstrip("/")
        if not path or path == prefix.rstrip("/"):
            continue
        url = origin + path if path.startswith("/") else path
        if url in seen:
            continue
        seen.add(url)
        slug = path.rsplit("/", 1)[-1]
        title = htmllib.unescape(slug.replace("-", " ")).strip()
        date = None
        if source.get("date_in_url"):
            dm = re.search(source["date_in_url"], path)
            if dm:
                y, mo, d = dm.groups()
                try:
                    date = datetime(2000 + int(y), int(mo), int(d), tzinfo=UTC)
                except ValueError:
                    pass
        items.append({"id": url, "title": title, "url": url, "date": date,
                      "slug": slug})
    return items

# ---------------------------------------------------------------------------
# Couche 2 — Hugging Face
# ---------------------------------------------------------------------------

def fetch_hf_org(org):
    url = (f"https://huggingface.co/api/models?author={urllib.parse.quote(org)}"
           f"&sort=createdAt&direction=-1&limit=10")
    data = json.loads(http_get(url))
    items = []
    for m in data:
        created = parse_iso(m.get("createdAt") or "")
        tags = m.get("tags") or []
        lic = next((t[8:] for t in tags if t.startswith("license:")), "")
        items.append({
            "id": m["id"], "title": m["id"], "url": f"https://huggingface.co/{m['id']}",
            "date": created, "downloads": m.get("downloads", 0),
            "likes": m.get("likes", 0),
            "pipeline": m.get("pipeline_tag") or "model",
            "license": lic,
        })
    return items

# ---------------------------------------------------------------------------
# Couche 3 — GitHub (évènements release)
# ---------------------------------------------------------------------------

def fetch_github_org(org, etag):
    """ReleaseEvent publics d'une org. Renvoie (items, nouvel_etag|None)."""
    url = f"https://api.github.com/orgs/{org}/events?per_page=100"
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            new_etag = resp.headers.get("ETag")
            events = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return [], "__unchanged__"
        raise
    items = []
    for ev in events:
        if ev.get("type") != "ReleaseEvent":
            continue
        rel = ev.get("payload", {}).get("release") or {}
        html_url = rel.get("html_url") or f"https://github.com/{ev['repo']['name']}/releases"
        name = rel.get("name") or rel.get("tag_name") or ev["repo"]["name"]
        date = parse_iso(rel.get("published_at") or ev.get("created_at") or "")
        items.append({"id": f"gh-{ev['id']}",
                      "title": f"{ev['repo']['name']} — {name}".strip(" —"),
                      "url": html_url, "date": date,
                      "repo": ev["repo"]["name"]})
    return items, new_etag

# ---------------------------------------------------------------------------
# Couche 4 — Hacker News (Algolia)
# ---------------------------------------------------------------------------

def fetch_hn(cfg, hours_window, min_points):
    cutoff = int((datetime.now(UTC) - timedelta(hours=hours_window)).timestamp())
    merged = {}
    for q in cfg["hn_queries"]:
        url = ("https://hn.algolia.com/api/v1/search_by_date?"
               + urllib.parse.urlencode({
                   "query": q, "tags": "story", "hitsPerPage": 8,
                   "numericFilters": f"created_at_i>{cutoff},points>{min_points}"}))
        try:
            data = json.loads(http_get(url))
        except Exception as e:  # noqa: BLE001
            log(f"HN query '{q}' échec: {e}")
            continue
        for h in data.get("hits", []):
            t = h.get("title") or ""
            if HN_EXCLUDE_RX.search(t):
                continue
            if not (HN_TITLE_RX.search(t) or HN_MODEL_VERSION_RX.search(t)):
                continue
            if h["objectID"] not in merged:
                merged[h["objectID"]] = {
                    "id": f"hn-{h['objectID']}", "title": t,
                    "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                    "hn_url": f"https://news.ycombinator.com/item?id={h['objectID']}",
                    "date": parse_iso(h.get("created_at") or ""),
                    "points": h.get("points", 0),
                    "comments": h.get("num_comments", 0),
                    "matched_query": q,
                }
    # Dédoublonnage des soumissions multiples d'une même actualité :
    # on regroupe par titre normalisé et on ne garde que la plus forte.
    best = {}
    for it in merged.values():
        cluster = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:40]
        cur = best.get(cluster)
        if not cur or it["points"] > cur["points"]:
            best[cluster] = it
    return list(best.values())

# ---------------------------------------------------------------------------
# Construction des items normalisés + embeds Discord
# ---------------------------------------------------------------------------

def normalize_official(source, raw):
    vendor = detect_vendor(raw["title"]) or source["vendor"]
    is_model = passes_filter(raw["title"], "strong")
    return {
        "key": item_key("official", raw["id"]),
        "source": "official",
        "source_id": source["id"],
        "vendor": vendor,
        "type": "release" if is_model else "news",
        "title": htmllib.unescape(raw["title"]),
        "url": raw["url"],
        "date": raw["date"],
    }


def build_embed(it):
    vendor = it.get("vendor") or detect_vendor(it["title"]) or "Indéterminé"
    color = VENDOR_STYLE.get(vendor, VENDOR_STYLE["_default"])
    emoji = VENDOR_EMOJI.get(vendor, "✨")
    src = it["source"]

    if src == "official":
        if it["type"] == "release":
            desc = "🚀 **Sortie / annonce officielle** (modèle ou capacité majeure)"
        else:
            desc = "📰 Annonce officielle éditeur"
        fields = [{"name": "Éditeur", "value": f"{emoji} {vendor}", "inline": True},
                  {"name": "Date", "value": fmt_date(it.get("date")), "inline": True},
                  {"name": "Source", "value": it["source_id"], "inline": True}]
        title, url = it["title"], it["url"]

    elif src == "hf":
        desc = "📦 **Poids ouverts publiés** sur Hugging Face"
        fields = [{"name": "Éditeur", "value": f"{emoji} {vendor}", "inline": True},
                  {"name": "Date", "value": fmt_date(it.get("date")), "inline": True},
                  {"name": "Type", "value": it.get("pipeline", "model"), "inline": True},
                  {"name": "Téléchargements (30j)",
                   "value": f"{it.get('downloads', 0):,}".replace(",", " "),
                   "inline": True}]
        if it.get("license"):
            fields.append({"name": "Licence", "value": it["license"], "inline": True})
        title, url = it["title"], it["url"]

    elif src == "github":
        desc = "🏷️ **Release GitHub** publiée"
        fields = [{"name": "Éditeur", "value": f"{emoji} {vendor}", "inline": True},
                  {"name": "Date", "value": fmt_date(it.get("date")), "inline": True},
                  {"name": "Repo", "value": f"`{it.get('repo','')}`", "inline": True}]
        title, url = it["title"], it["url"]

    else:  # hn
        desc = "📡 **Forte résonance communautaire** (Hacker News)"
        fields = [{"name": "Éditeur probable", "value": f"{emoji} {vendor}", "inline": True},
                  {"name": "Score", "value": f"⬆️ {it.get('points',0)} · 💬 {it.get('comments',0)}",
                   "inline": True},
                  {"name": "Date", "value": fmt_date(it.get("date")), "inline": True}]
        title, url = it["title"], it["hn_url"]

    return {
        "title": title[:250],
        "url": url,
        "description": desc,
        "color": color,
        "fields": fields,
        "footer": {"text": "Signal2 · Veille modèles IA · MASSRACE"},
        "timestamp": (it.get("date") or datetime.now(UTC)).isoformat()
        if it.get("date") else datetime.now(UTC).isoformat(),
    }


def send_items(cfg, items, header=None):
    """Envoie les items (embeds) par lots ; renvoie le nb réellement envoyés."""
    items = sorted(items, key=lambda x: x.get("date") or datetime.now(UTC), reverse=True)
    embeds = [build_embed(i) for i in items]
    sent = 0
    batches = [embeds[i:i + cfg["embeds_per_message"]]
               for i in range(0, len(embeds), cfg["embeds_per_message"])]
    for bi, batch in enumerate(batches):
        payload = {"username": cfg["webhook_name"], "embeds": batch}
        if bi == 0 and header:
            payload["content"] = header
        if http_post_json(cfg["webhook_url"], payload):
            sent += len(batch)
        time.sleep(1.5)
    return sent

# ---------------------------------------------------------------------------
# Cycle de veille
# ---------------------------------------------------------------------------

def load_state():
    st = load_json(STATE_PATH, None)
    if not isinstance(st, dict):
        st = {"seen": {}, "github_etags": {}}
    st.setdefault("seen", {})
    st.setdefault("github_etags", {})
    return st


def prune_seen(state, days=90):
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}


def run_cycle(cfg, dry_run=False):
    state = load_state()
    seen = state["seen"]
    fresh, errors = [], 0
    now_iso = datetime.now(UTC).isoformat()
    date_budget = 40  # nb max de fetchs de date d'article par cycle

    # -- Couche 1 : officiels
    for source in cfg["official_sources"]:
        try:
            if source["kind"] == "rss":
                raws = fetch_rss(source)
            else:
                raws = fetch_links_page(source)
            for raw in raws:
                if not passes_filter(raw["title"], source.get("filter", "strong")):
                    continue
                it = normalize_official(source, raw)
                if it["key"] in seen:
                    continue
                # date à la volée pour les pages HTML sans date
                if not it.get("date") and source.get("fetch_dates") and date_budget > 0:
                    date_budget -= 1
                    it["date"] = fetch_article_date(it["url"]) or datetime.now(UTC)
                # hors fenêtre d'annonce : capitalisé, jamais reposté
                if too_old(it.get("date")):
                    seen[it["key"]] = now_iso
                    continue
                fresh.append(it)
        except Exception as e:  # noqa: BLE001
            errors += 1
            log(f"source {source['id']} échec: {e}")

    # -- Couche 2 : Hugging Face
    for org in cfg["hf_orgs"]:
        try:
            for raw in fetch_hf_org(org):
                key = item_key("hf", raw["id"])
                if key in seen:
                    continue
                if too_old(raw.get("date")):
                    seen[key] = now_iso
                    continue
                fresh.append({"key": key, "source": "hf", "vendor": org,
                              "type": "weights", "title": raw["title"],
                              "url": raw["url"], "date": raw["date"],
                              "downloads": raw["downloads"],
                              "pipeline": raw["pipeline"],
                              "license": raw["license"]})
        except Exception as e:  # noqa: BLE001
            errors += 1
            log(f"HF {org} échec: {e}")

    # -- Couche 3 : GitHub
    for org in cfg["github_orgs"]:
        try:
            items, etag = fetch_github_org(org, state["github_etags"].get(org))
            if etag != "__unchanged__":
                state["github_etags"][org] = etag
                for raw in items:
                    key = item_key("gh", raw["id"])
                    if key in seen:
                        continue
                    if too_old(raw.get("date")):
                        seen[key] = now_iso
                        continue
                    fresh.append({"key": key, "source": "github",
                                  "vendor": detect_vendor(raw["title"]) or org,
                                  "type": "release", "title": raw["title"],
                                  "url": raw["url"], "date": raw["date"],
                                  "repo": raw["repo"]})
        except Exception as e:  # noqa: BLE001
            errors += 1
            log(f"GitHub {org} échec: {e}")

    # -- Couche 4 : Hacker News
    try:
        for raw in fetch_hn(cfg, cfg["hn_window_hours"], cfg["hn_min_points_poll"]):
            key = item_key(raw["id"], raw["id"])
            if key in seen:
                continue
            raw["key"] = key
            raw["vendor"] = detect_vendor(raw["title"])
            if not raw["vendor"]:
                continue  # hors périmètre éditeurs IA
            fresh.append(raw)
    except Exception as e:  # noqa: BLE001
        errors += 1
        log(f"HN échec: {e}")

    # Anti-déluge : au premier lancement ou après une longue coupure, on
    # capitalise sans inonder le salon.
    overflow = 0
    if not dry_run and len(fresh) > cfg["max_items_per_cycle"]:
        fresh.sort(key=lambda x: x.get("date") or datetime.now(UTC), reverse=True)
        dropped = fresh[cfg["max_items_per_cycle"]:]
        overflow = len(dropped)
        fresh = fresh[:cfg["max_items_per_cycle"]]

    if dry_run:
        log(f"[TEST] {len(fresh)} item(s) nouveaux détectés · {errors} source(s) en erreur")
        for it in fresh[:40]:
            print(f"  - [{it['source']}] {it.get('vendor','?')} | {it['title'][:80]}")
        return fresh

    sent = 0
    if fresh:
        releases = [i for i in fresh if i.get("type") == "release"]
        others = [i for i in fresh if i.get("type") != "release"]
        if releases:
            send_items(cfg, releases,
                       header="🚨 **Nouvelle(s) sortie(s) / annonce(s) IA détectée(s)**")
            sent += len(releases)
        if others:
            send_items(cfg, others)
            sent += len(others)

    now_iso = datetime.now(UTC).isoformat()
    for it in fresh:
        seen[it["key"]] = now_iso
    if overflow:
        for it in dropped:
            seen[it["key"]] = now_iso
    prune_seen(state)
    save_json_atomic(STATE_PATH, state)
    log(f"Cycle terminé : {sent} envoyé(s), {overflow} capitalisé(s), {errors} erreur(s) source")
    return fresh

# ---------------------------------------------------------------------------
# Digest rétrospectif + message de lancement
# ---------------------------------------------------------------------------

def run_digest(cfg, days=7):
    state = load_state()
    seen = state["seen"]
    cutoff = datetime.now(UTC) - timedelta(days=days)
    collected = []

    for source in cfg["official_sources"]:
        try:
            raws = fetch_rss(source) if source["kind"] == "rss" else fetch_links_page(source)
            for raw in raws:
                if not passes_filter(raw["title"], source.get("filter", "strong")):
                    continue
                d = raw.get("date")
                if not d and source.get("fetch_dates"):
                    d = fetch_article_date(raw["url"])
                if d and d >= cutoff:
                    raw["date"] = d
                    collected.append(normalize_official(source, raw))
        except Exception as e:  # noqa: BLE001
            log(f"digest source {source['id']} échec: {e}")

    for org in cfg["hf_orgs"]:
        try:
            for raw in fetch_hf_org(org):
                if raw.get("date") and raw["date"] >= cutoff:
                    collected.append({
                        "key": item_key("hf", raw["id"]), "source": "hf",
                        "vendor": org, "type": "weights", "title": raw["title"],
                        "url": raw["url"], "date": raw["date"],
                        "downloads": raw["downloads"], "pipeline": raw["pipeline"],
                        "license": raw["license"]})
        except Exception as e:  # noqa: BLE001
            log(f"digest HF {org} échec: {e}")

    try:
        for raw in fetch_hn(cfg, days * 24, cfg["hn_min_points_digest"]):
            if raw.get("date") and raw["date"] >= cutoff and raw.get("vendor"):
                raw["key"] = item_key(raw["id"], raw["id"])
                collected.append(raw)
    except Exception as e:  # noqa: BLE001
        log(f"digest HN échec: {e}")

    # Dédoublonnage par clé, tri : sorties/models d'abord puis par date
    uniq = {}
    for it in collected:
        if it["key"] not in uniq:
            uniq[it["key"]] = it
    items = sorted(uniq.values(),
                   key=lambda x: ((0 if x.get("type") == "release" else
                                   1 if x["source"] == "hf" else 2),
                                  -(x.get("date") or datetime.now(UTC)).timestamp()))

    log(f"[DIGEST {days}j] {len(items)} item(s) collecté(s)")
    # Tous les items collectés sont marqués vus (rien n'est reperdu), mais on
    # limite le volume publié d'un coup pour garder le salon lisible.
    published = items
    truncated = 0
    if len(items) > 60:
        published = items[:60]
        truncated = len(items) - 60
    header = (f"🧾 **Signal2 — Digest des {days} derniers jours** "
              f"(rattrapage initial, {len(published)} éléments"
              + (f", {truncated} mineurs capitalisés" if truncated else "")
              + ")")
    sent = send_items(cfg, published, header=header)
    now_iso = datetime.now(UTC).isoformat()
    for it in items:
        seen[it["key"]] = now_iso
    save_json_atomic(STATE_PATH, state)
    return items


def send_launch_message(cfg):
    embed = {
        "title": "🛰️ Signal2 — Veille IA désormais en ligne",
        "description":
            "Veille active et autonome sur **toutes les sorties de modèles d'IA**. "
            "Vérification automatique toutes les **15 minutes**, 24h/24.\n\n"
            "**Couverture éditeurs** : Anthropic · OpenAI · Google DeepMind · Meta · "
            "DeepSeek · Qwen/Alibaba · Z.ai (GLM) · Moonshot/Kimi · Mistral · xAI · "
            "Black Forest Labs · NVIDIA · Microsoft — et tout nouvel acteur majeur.\n\n"
            "**4 couches de détection (aucune omission)** :\n"
            "1️⃣ Annonces officielles (RSS & news éditeurs)\n"
            "2️⃣ Publications de poids ouverts (Hugging Face)\n"
            "3️⃣ Releases GitHub (10 organisations suivies)\n"
            "4️⃣ Buzz communautaire fort (Hacker News) — filet de sécurité\n\n"
            "**Légende** : 🚀 sortie majeure · 📦 poids ouverts · 🏷️ release GitHub · "
            "📰 annonce · 📡 buzz",
        "color": 0x5865F2,
        "fields": [
            {"name": "Priorité", "value": "Les sorties de modèles (🚀) sont toujours signalées en tête et en premier.", "inline": False},
            {"name": "Statut", "value": "✅ Système opérationnel — publication autonome", "inline": False},
        ],
        "footer": {"text": "Signal2 · Veille modèles IA · MASSRACE"},
        "timestamp": datetime.now(UTC).isoformat(),
    }
    ok = http_post_json(cfg["webhook_url"],
                        {"username": cfg["webhook_name"], "embeds": [embed]})
    log(f"Message de lancement {'envoyé' if ok else 'EN ÉCHEC'}")
    return ok

# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(description="Signal2 — veille IA MASSRACE")
    ap.add_argument("--poll", action="store_true", help="un cycle de veille")
    ap.add_argument("--loop", nargs="?", type=int, const=15, metavar="MIN",
                    help="cycles répétés (minutes, défaut 15)")
    ap.add_argument("--test", action="store_true", help="cycle à blanc")
    ap.add_argument("--digest", nargs="?", type=int, const=7, metavar="JOURS",
                    help="digest rétrospectif")
    ap.add_argument("--launch-msg", action="store_true", help="message de lancement")
    args = ap.parse_args()

    cfg = dict(DEFAULT_CONFIG)
    file_cfg = load_json(CONFIG_PATH, {})
    if isinstance(file_cfg, dict):
        cfg.update(file_cfg)
    # Le secret (webhook Discord) prime toujours sur tout le reste :
    # env SIGNAL2_WEBHOOK (GitHub Actions secret / local).
    env_wh = os.environ.get("SIGNAL2_WEBHOOK", "").strip()
    if env_wh:
        cfg["webhook_url"] = env_wh

    if not cfg.get("webhook_url"):
        log("ERREUR: webhook_url manquant (SIGNAL2_WEBHOOK ou config)")
        sys.exit(1)

    if args.launch_msg:
        send_launch_message(cfg)
    if args.digest is not None:
        run_digest(cfg, args.digest)
    if args.test:
        run_cycle(cfg, dry_run=True)
    elif args.poll:
        run_cycle(cfg)
    elif args.loop is not None:
        interval = max(5, args.loop) * 60
        log(f"Boucle continue démarrée (intervalle {args.loop} min)")
        while True:
            try:
                run_cycle(cfg)
            except Exception as e:  # noqa: BLE001
                log(f"Erreur cycle (boucle): {e}")
            time.sleep(interval)
    if not any([args.launch_msg, args.digest is not None, args.test, args.poll,
                args.loop is not None]):
        ap.print_help()


if __name__ == "__main__":
    main()
