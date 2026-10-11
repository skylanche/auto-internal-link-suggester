#!/usr/bin/env python3
"""
Internal link suggester.

Input : a page URL + your sitemap URL
Output: for each RELEVANT sitemap URL, up to 2 anchor-text suggestions taken from
        body paragraphs only (never headings). Irrelevant URLs are skipped.
        Each suggestion is either:
          - existing_text : a phrase already in the paragraph that can be linked
          - add_text      : a short new phrase/sentence you could add, then link

Setup
-----
    pip install requests beautifulsoup4 anthropic openai
    export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY=...

Usage
-----
    python internal_links.py \
        --page https://example.com/blog/my-post \
        --sitemap https://example.com/sitemap.xml \
        --provider anthropic            # or: openai, nvidia
        [--model MODEL] [--max-candidates 40] [--out suggestions.json]
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import math
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

try:
    import lxml  # noqa: F401
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

UA = {"User-Agent": "InternalLinkSuggester/1.0"}
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".linkcache.json")
MAX_SUGGESTIONS_PER_URL = 2
STOP = set("""a an the and or of to in on for with is are was were be by at as it its this that
from your you we our how what why when which can will not but about into more most""".split())


# ---------- helpers ----------
def fetch(url):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r


def norm(url):
    url, _ = urldefrag(url.strip())
    return url.rstrip("/").lower()


def tokens(text):
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in STOP}


# ---------- sitemap ----------
SKIP_URL = re.compile(r"\.(jpe?g|png|gif|webp|svg|pdf|zip|mp4|css|js|xml)$|/page/\d+/?$|[?&]p(age)?=\d+", re.I)
SITEMAP_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sitemap_cache.json")


def parse_sitemap(url, depth=0):
    r = fetch(url)
    data = r.content
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    root = ET.fromstring(data)
    locs = []
    for e in root.iter():
        ns = e.tag.partition("}")[0]
        if e.tag.endswith("loc") and e.text and "image" not in ns and "video" not in ns:
            locs.append(e.text.strip())
    if root.tag.endswith("sitemapindex") and depth < 3:
        def sub(sm):  # child sitemaps are downloaded in parallel
            try:
                return parse_sitemap(sm, depth + 1)
            except Exception as e:
                print(f"[warn] sitemap {sm}: {e}", file=sys.stderr)
                return []
        urls = []
        with ThreadPoolExecutor(8) as ex:
            for part in ex.map(sub, locs):
                urls += part
        return urls
    return locs


def get_sitemap_urls(url, refresh=False, max_age=6 * 3600):
    """Returns (urls, from_cache). Big sitemaps are cached for 6 hours so repeat runs are instant."""
    try:
        cache = json.load(open(SITEMAP_CACHE))
    except Exception:
        cache = {}
    hit = cache.get(url)
    if hit and not refresh and time.time() - hit["ts"] < max_age:
        return hit["urls"], True
    urls = [u for u in dict.fromkeys(parse_sitemap(url)) if not SKIP_URL.search(u)]
    cache = {k: v for k, v in cache.items() if time.time() - v["ts"] < 24 * 3600}
    cache[url] = {"ts": time.time(), "urls": urls}
    try:
        json.dump(cache, open(SITEMAP_CACHE, "w"))
    except Exception:
        pass
    return urls, False


# ---------- page extraction ----------
HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]


def _flatten(node, base, href=None):
    """Flatten an element into (text, href, is_new_link, highlighted) segments."""
    out = []
    for ch in node.children:
        if isinstance(ch, Comment):
            continue
        if isinstance(ch, NavigableString):
            out.append((str(ch), href, False, False))
        elif isinstance(ch, Tag):
            h = urljoin(base, ch["href"]) if ch.name == "a" and ch.get("href") else href
            out += _flatten(ch, base, h)
    return out


def _clean(segs):
    merged = []
    for t, h, n, hl in segs:
        t = re.sub(r"\s+", " ", t)
        if merged and merged[-1][1] == h:
            merged[-1] = (merged[-1][0] + t, h, n, hl)
        else:
            merged.append((t, h, n, hl))
    if merged:
        merged[0] = (merged[0][0].lstrip(),) + merged[0][1:]
        merged[-1] = (merged[-1][0].rstrip(),) + merged[-1][1:]
    return [m for m in merged if m[0]]


def extract_from_soup(soup, url, with_blocks=True):
    """Returns (title, paragraphs, linked_urls, blocks). `blocks` keeps page order (headings,
    paragraphs, list items) so a document copy can be rebuilt; paragraph blocks carry the same
    'pid' used by the AI suggestions. Only <p> paragraphs are ever offered for linking."""
    for t in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        t.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    linked = {norm(urljoin(url, a["href"])) for a in root.find_all("a", href=True)}
    title = soup.title.get_text(strip=True) if soup.title else ""
    paragraphs, blocks = [], []
    for el in root.find_all(HEADINGS + ["p", "li"]):
        if el.name == "li" and el.find("p"):
            continue
        text = " ".join(el.get_text(" ", strip=True).split())
        if not text:
            continue
        if el.name in HEADINGS:
            blocks.append({"type": "heading", "level": int(el.name[1]), "text": text})
            continue
        pid = None
        if el.name == "p" and len(text.split()) >= 8:
            pid = len(paragraphs)
            link_texts = [a.get_text(" ", strip=True).lower() for a in el.find_all("a")]
            paragraphs.append({"id": pid, "text": text, "linked": link_texts})
        if with_blocks:
            blocks.append({"type": el.name, "pid": pid, "segments": _clean(_flatten(el, url))})
    return title, paragraphs, linked, blocks


def extract_page_full(url):
    return extract_from_soup(BeautifulSoup(fetch(url).text, PARSER), url)


def extract_page(url):
    title, paragraphs, linked, _ = extract_page_full(url)
    return title, paragraphs, linked


# ---------- candidate selection (built for sitemaps with tens of thousands of URLs) ----------
def load_cache():
    try:
        return json.load(open(CACHE_FILE))
    except Exception:
        return {}


def humanize(u):
    return " ".join(re.findall(r"[a-z0-9]+", urlparse(u).path.lower()))


def _stem(w):
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _words(text):
    return [_stem(w) for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in STOP]


def ranking_text(title, blocks, paragraphs):
    """Text used to match against URL slugs: title and headings count extra, then body text."""
    heads = [b["text"] for b in blocks if b["type"] == "heading"]
    return " ".join([title] * 3 + heads * 2 + [p["text"] for p in paragraphs])


def rank_urls(page_text, urls, limit):
    """Pure-Python relevance ranking of every URL's slug against the page (50k URLs in about a second).
    Rare slug words weigh more (IDF); a multi-word slug that appears as a phrase in the page gets a boost."""
    page_tf = Counter(_words(page_text))
    page_norm = " " + " ".join(re.findall(r"[a-z0-9]+", page_text.lower())) + " "
    slugs = {u: set(_words(urlparse(u).path)) for u in urls}
    df = Counter()
    for ws in slugs.values():
        df.update(ws)
    n = max(len(urls), 1)
    scored = []
    for u, ws in slugs.items():
        hit = [w for w in ws if w in page_tf]
        if not hit:
            continue
        score = sum(math.log(1 + n / (1 + df[w])) * (1 + math.log(page_tf[w])) for w in hit) / math.sqrt(len(ws))
        seg = " ".join(re.findall(r"[a-z0-9]+", urlparse(u).path.rstrip("/").split("/")[-1].lower()))
        if len(seg.split()) >= 2 and f" {seg} " in page_norm:
            score *= 2
        scored.append((score, u))
    scored.sort(reverse=True)
    return [u for _, u in scored[:limit]]


def page_meta(url):
    """Title / description / H1 from the first 150 KB only. Returns None on failure (not cached)."""
    try:
        r = requests.get(url, headers=UA, timeout=8, stream=True)
        r.raise_for_status()
        raw = next(r.iter_content(150_000), b"")
        r.close()
        s = BeautifulSoup(raw.decode(r.encoding or "utf-8", errors="replace"), "html.parser")
        d = s.find("meta", attrs={"name": "description"})
        h1 = s.find("h1")
        return {"title": s.title.get_text(strip=True) if s.title else "",
                "description": d.get("content", "") if d else "",
                "h1": h1.get_text(strip=True) if h1 else ""}
    except Exception:
        return None


def build_candidates(page_text, urls, limit, fetch_titles=True, progress=lambda m: None):
    t0 = time.time()
    top = rank_urls(page_text, urls, limit)
    progress(f"Ranked {len(urls):,} URLs by relevance in {time.time() - t0:.1f}s; top {len(top)} selected.")
    cache = load_cache() if fetch_titles else {}
    if fetch_titles and top:
        todo = [u for u in top if u not in cache]
        if todo:
            progress(f"Fetching titles for {len(todo)} pages...")
            with ThreadPoolExecutor(16) as ex:
                for u, m in zip(todo, ex.map(page_meta, todo)):
                    if m:
                        cache[u] = m
            try:
                json.dump(cache, open(CACHE_FILE, "w"))
            except Exception:
                pass
    out = []
    for u in top:
        m = cache.get(u) or {}
        out.append({"url": u, "title": m.get("title") or humanize(u),
                    "description": m.get("description", ""), "h1": m.get("h1", "")})
    return out


# ---------- LLM ----------
class ConfigError(Exception):
    """Missing key / model / URL: not worth retrying."""


class BadOutput(Exception):
    """The model answered, but not with usable JSON."""


class EmptyResponse(BadOutput):
    """The model answered with no text at all."""



SYSTEM = """You are an SEO internal-linking assistant. You get numbered body paragraphs from one
page and a list of candidate target pages from the same site. Return ONLY valid JSON.

Rules:
- Only suggest a target page if it is genuinely topically relevant. Skip irrelevant ones.
- At most 2 suggestions per target URL.
- Anchors must be 2-6 words, descriptive, natural; never generic ("click here", "read more").
- Use paragraph text only (headings are not provided and must never be used).
- type "existing_text": anchor_text MUST appear verbatim (same spelling) in that paragraph.
- type "add_text": when no good phrase exists, propose a short sentence (max 25 words) to add to
  that paragraph that naturally contains the anchor_text. Put it in suggested_sentence.
- Prefer existing_text. Do not reuse the same anchor_text for different target URLs.

Schema:
{"suggestions":[{"target_url":"...","reason":"short",
  "options":[{"type":"existing_text|add_text","paragraph":0,"anchor_text":"...",
              "suggested_sentence":"only for add_text"}]}]}"""


# provider -> (base_url, default model, env var for the key). Model names change often, so they are
# only defaults: override in the UI / --model. Any other OpenAI-compatible service works via "custom".
PRESETS = {
    "openai": ("https://api.openai.com/v1", "gpt-4o", "OPENAI_API_KEY"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "meta/llama-3.1-8b-instruct", "NVIDIA_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct", "OPENROUTER_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "GROQ_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-2.5-flash", "GEMINI_API_KEY"),
    "together": ("https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo", "TOGETHER_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat", "DEEPSEEK_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "mistral-large-latest", "MISTRAL_API_KEY"),
    "ollama": ("http://localhost:11434/v1", "llama3.1", ""),  # local, no key needed
}
PROVIDERS = ["anthropic"] + list(PRESETS) + ["custom"]


def _openai_client(provider, model, api_key, base_url, timeout):
    preset = PRESETS.get(provider, ("", "", ""))
    base_url = (base_url or preset[0] or "").strip()
    model = (model or os.environ.get(f"{provider.upper()}_MODEL") or preset[1] or "").strip()
    if not base_url or not model:
        raise ConfigError("Custom providers need both a Base URL and a Model name.")
    key = api_key or (os.environ.get(preset[2]) if preset[2] else None) or os.environ.get("LLM_API_KEY")
    if not key and provider != "ollama" and "localhost" not in base_url and "127.0.0.1" not in base_url:
        raise ConfigError(f"No API key found for '{provider}'. Add it to your .env file (see .env.example) and restart.")
    from openai import OpenAI
    return OpenAI(base_url=base_url, api_key=key or "none", timeout=timeout, max_retries=0), model, base_url


def call_llm(provider, model, user_prompt, api_key=None, base_url=None, system=None, max_tokens=None, timeout=None):
    """provider: 'anthropic' (native API) or any OpenAI-compatible service (preset or 'custom').
    api_key / base_url / model fall back to presets and environment variables when not given.
    Timeout (seconds) defaults to LLM_TIMEOUT or 60."""
    timeout = timeout or float(os.environ.get("LLM_TIMEOUT", 60))
    system = system or SYSTEM
    max_tokens = max_tokens or int(os.environ.get("LLM_MAX_TOKENS", 4000))
    if provider == "anthropic":
        import anthropic
        c = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY"),
                                timeout=timeout, max_retries=0)
        r = c.messages.create(model=model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"), max_tokens=max_tokens,
                              system=system, messages=[{"role": "user", "content": user_prompt}])
        text = "".join(getattr(b, "text", "") or "" for b in (r.content or []))
        if not text.strip():
            raise EmptyResponse("Model ran out of output tokens before answering (finish_reason=length). This is typical of reasoning/'thinking' models: Try a non-reasoning instruct model, or raise LLM_MAX_TOKENS in .env." if getattr(r, "stop_reason", None) == "max_tokens" else f"Model returned an empty response (stop_reason={getattr(r, 'stop_reason', None)}).")
        return text
    c, model, base_url = _openai_client(provider, model, api_key, base_url, timeout)
    kw = {"response_format": {"type": "json_object"}} if "api.openai.com" in base_url and system is SYSTEM else {}
    for attempt in (0, 1):
        r = c.chat.completions.create(
            model=model, temperature=0.2, max_tokens=max_tokens, **kw,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user_prompt}])
        if not getattr(r, "choices", None):
            raise EmptyResponse("Provider returned no choices (possibly an error or overload). Try another model.")
        ch = r.choices[0]
        text = ch.message.content
        if not text:  # some reasoning models put the answer in a separate field
            for attr in ("reasoning_content", "reasoning"):
                alt = getattr(ch.message, attr, None)
                if alt and "{" in alt:
                    text = alt
                    break
        if text and text.strip():
            return text
        if getattr(ch, "finish_reason", None) == "length" and attempt == 0 and max_tokens < 8000:
            max_tokens = min(max_tokens * 2, 8000)  # reasoning models can burn the budget thinking: try once with more
            continue
        break
    if getattr(ch, "finish_reason", None) == "length":
        raise EmptyResponse("Model ran out of output tokens before answering (finish_reason=length). This is typical of reasoning/'thinking' models: Try a non-reasoning instruct model, or raise LLM_MAX_TOKENS in .env.")
    raise EmptyResponse(f"Model returned an empty response (finish_reason={getattr(ch, 'finish_reason', None)}). Try a different model.")


def test_connection(provider, model=None, api_key=None, base_url=None):
    """Tiny request to check key / model / speed. Returns (ok, message)."""
    t0 = time.time()
    try:
        out = call_llm(provider, model, "Reply with the single word: OK", api_key, base_url,
                       system="You are a connectivity test.", max_tokens=300, timeout=45)
        return True, f"Connected. Model replied in {time.time() - t0:.1f}s ({(out or '').strip()[:20]!r})."
    except EmptyResponse as e:
        if "finish_reason=length" in str(e) or "max_tokens" in str(e):
            return True, ("Connected, but this model spent its whole token budget 'thinking' (a reasoning model). "
                          "It will be slow and may fail; a non-reasoning instruct model is recommended. "
                          f"({time.time() - t0:.1f}s)")
        return False, _explain(e)
    except Exception as e:
        msg = _explain(e)
        if provider != "anthropic" and getattr(e, "status_code", None) == 404:
            try:  # help the user pick a valid model name
                c, _, _ = _openai_client(provider, "x", api_key, base_url, 15)
                ids = sorted(m.id for m in c.models.list())[:40]
                if ids:
                    msg += " Some available models: " + ", ".join(ids)
            except Exception:
                pass
        if "timed out" in msg.lower():
            msg += f" (waited {time.time() - t0:.0f}s). The provider is not answering: try a smaller/faster model, or check your network/VPN."
        return False, msg


def parse_json(text):
    """Pull the JSON out of whatever the model returned (fences, <think> blocks, prose, bare lists)."""
    if not isinstance(text, str) or not text.strip():
        raise EmptyResponse("Model returned an empty response.")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```(?:json)?", "", text)
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    end = max(text.rfind("}"), text.rfind("]"))
    if not starts or end < min(starts):
        raise BadOutput(f"Model returned non-JSON output, starting: {text.strip()[:100]!r}")
    try:
        data = json.loads(text[min(starts): end + 1])
    except json.JSONDecodeError:
        raise BadOutput(f"Model returned incomplete/invalid JSON (maybe cut off), starting: {text.strip()[:100]!r}")
    if isinstance(data, list):
        data = {"suggestions": data}
    if isinstance(data, dict) and "suggestions" not in data and "target_url" in data:
        data = {"suggestions": [data]}
    if not isinstance(data, dict):
        raise BadOutput(f"Unexpected JSON shape: {str(data)[:100]!r}")
    return data


# ---------- validation ----------
def validate(raw, paragraphs, allowed_urls):
    out, used_anchors = [], set()
    for s in raw.get("suggestions", []):
        url = s.get("target_url")
        if url not in allowed_urls:
            continue
        good = []
        for o in s.get("options", []):
            try:
                p = paragraphs[int(o["paragraph"])]
            except Exception:
                continue
            a = (o.get("anchor_text") or "").strip()
            if not a or a.lower() in used_anchors or not 1 <= len(a.split()) <= 8:
                continue
            if any(a.lower() in lt or lt in a.lower() for lt in p["linked"]):
                continue  # overlaps an existing link
            if o.get("type") == "existing_text":
                if a.lower() not in p["text"].lower():
                    continue
            elif o.get("type") == "add_text":
                if a.lower() not in (o.get("suggested_sentence") or "").lower():
                    continue
            else:
                continue
            used_anchors.add(a.lower())
            good.append(o)
            if len(good) == MAX_SUGGESTIONS_PER_URL:
                break
        if good:
            out.append({"target_url": url, "reason": s.get("reason", ""), "options": good})
    return out


def _explain(e):
    code = getattr(e, "status_code", None)
    msg = str(e) or e.__class__.__name__
    if code in (401, 403):
        return f"API key rejected ({code}). Check the key in .env and that it matches the provider."
    if code == 404:
        return f"Model or endpoint not found (404). Check the Model / Base URL fields. {msg[:120]}"
    if code == 429:
        return "Rate limit reached (429). Lower LLM_CONCURRENCY to 1 in .env or wait a minute."
    if code == 400:
        return f"Bad request (400): {msg[:160]}"
    if isinstance(e, BadOutput):
        m = str(e)[:260]
        return m if "Try" in m else m + " Try a larger/different model."
    if isinstance(e, TypeError):
        return f"Unexpected reply format from provider ({msg[:100]}). Try a different model."
    if "timed out" in msg.lower() or "timeout" in e.__class__.__name__.lower():
        return "Request timed out."
    return msg[:200]


def _is_timeout(e):
    return "timed out" in str(e).lower() or "timeout" in e.__class__.__name__.lower()


def _retryable(e):
    if isinstance(e, ConfigError):
        return False
    code = getattr(e, "status_code", None)
    if code is not None:
        return code in (408, 429, 500, 502, 503, 504)
    return True  # timeouts, connection errors, bad JSON


def _ask(call, args, check, tries=3):
    for attempt in range(tries):
        check()
        try:
            return parse_json(call(*args))
        except Exception as e:  # Stop is a BaseException and passes through
            limit = 2 if _is_timeout(e) else tries  # a timed-out provider rarely recovers on retry
            if attempt >= limit - 1 or not _retryable(e):
                raise
            for _ in range((3, 8)[attempt] * 5):  # back off, but stay stoppable
                check()
                time.sleep(0.2)


def suggest_links(title, paragraphs, cands, provider, model=None, api_key=None, base_url=None,
                  chunk_size=15, concurrency=None, call=None, progress=print, check=lambda: None):
    """Ask the AI about the page in parts (chunk_size paragraphs each), several parts at once, with retries.
    A failed part is skipped and the rest are kept; if every part fails the last reason is raised."""
    call = call or call_llm
    concurrency = max(1, concurrency or int(os.environ.get("LLM_CONCURRENCY", 3)))
    chunks = [paragraphs[i:i + chunk_size] for i in range(0, len(paragraphs), chunk_size)]
    cand_text = "\n".join(f"- {c['url']} | {c['title']} | {c['h1']} | {(c['description'] or '')[:150]}" for c in cands)

    def work(chunk):
        prompt = (f"PAGE TITLE: {title}\n\nPARAGRAPHS (suggest links only inside these):\n"
                  + "\n".join(f"[{p['id']}] {p['text']}" for p in chunk)
                  + f"\n\nCANDIDATE TARGET PAGES:\n{cand_text}")
        return _ask(call, (provider, model, prompt, api_key, base_url), check)

    results, errors = {}, []
    progress(f"Asking AI: {len(chunks)} part(s), {concurrency} at a time...")
    ex = ThreadPoolExecutor(concurrency)
    try:
        futs = {ex.submit(work, ch): n for n, ch in enumerate(chunks, 1)}
        pending = set(futs)
        while pending:
            check()
            done, pending = wait(pending, timeout=0.3, return_when=FIRST_COMPLETED)
            for f in done:
                n = futs[f]
                try:
                    results[n] = f.result()
                    progress(f"  part {n}/{len(chunks)} done")
                except Exception as e:
                    errors.append(_explain(e))
                    progress(f"  part {n}/{len(chunks)} failed: {errors[-1]}")
            if errors and not results and len(errors) >= concurrency:  # first wave all failed: stop early
                progress("First requests all failed, so I am stopping early instead of waiting on the rest.")
                pending = set()
                break
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if errors and not results:
        raise RuntimeError(f"AI request failed: {errors[-1]}"
                           + (" Use the Test AI connection button / README troubleshooting." if "timed out" in errors[-1] else ""))
    if errors:
        progress(f"Note: {len(errors)} part(s) failed; showing results from the rest.")

    merged, used = {}, set()
    allowed = {c["url"] for c in cands}
    for n in sorted(results):
        in_chunk = {p["id"] for p in chunks[n - 1]}
        for sug in validate(results[n], paragraphs, allowed):
            m = merged.setdefault(sug["target_url"], {"target_url": sug["target_url"], "reason": sug["reason"], "options": []})
            for o in sug["options"]:
                a = o["anchor_text"].lower()
                if int(o["paragraph"]) not in in_chunk or a in used or len(m["options"]) >= MAX_SUGGESTIONS_PER_URL:
                    continue
                used.add(a)
                m["options"].append(o)
    return [m for m in merged.values() if m["options"]]


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", required=True)
    ap.add_argument("--sitemap", required=True)
    ap.add_argument("--provider", choices=PROVIDERS, default="anthropic")
    ap.add_argument("--model")
    ap.add_argument("--base-url", help="OpenAI-compatible endpoint (for --provider custom); key via LLM_API_KEY env var")
    ap.add_argument("--max-candidates", type=int, default=30)
    ap.add_argument("--no-titles", action="store_true", help="skip fetching candidate page titles (faster, slug-only)")
    ap.add_argument("--refresh-sitemap", action="store_true", help="ignore the 6-hour sitemap cache")
    ap.add_argument("--concurrency", type=int, help="parallel AI requests (default 3, or LLM_CONCURRENCY)")
    ap.add_argument("--out", default="suggestions.json")
    ap.add_argument("--docx", help="also write a Word copy of the page with links placed, e.g. page.docx")
    a = ap.parse_args()

    print("Reading page...")
    title, paragraphs, linked, blocks = extract_page_full(a.page)
    if not paragraphs:
        sys.exit("No paragraph content found on the page.")
    print("Loading sitemap...")
    all_urls, cached = get_sitemap_urls(a.sitemap, a.refresh_sitemap)
    me = norm(a.page)
    urls = [u for u in all_urls if norm(u) != me and norm(u) not in linked]
    print(f"{len(all_urls):,} sitemap URLs ({'cached' if cached else 'downloaded'}); {len(urls):,} after removing self and already-linked pages")
    cands = build_candidates(ranking_text(title, blocks, paragraphs), urls, a.max_candidates, not a.no_titles, print)
    if not cands:
        sys.exit("No relevant candidates found.")

    result = suggest_links(title, paragraphs, cands, a.provider, a.model, base_url=a.base_url, concurrency=a.concurrency)

    json.dump({"page": a.page, "suggestions": result}, open(a.out, "w"), indent=2)
    if not result:
        print("No relevant internal links found.")
    for s in result:
        print(f"\n-> {s['target_url']}\n   Why: {s['reason']}")
        for o in s["options"]:
            if o["type"] == "existing_text":
                print(f"   [para {o['paragraph']}] Link existing text: \"{o['anchor_text']}\"")
            else:
                print(f"   [para {o['paragraph']}] Add: \"{o['suggested_sentence']}\"  (anchor: \"{o['anchor_text']}\")")
    print(f"\nSaved to {a.out}")
    if a.docx:
        from docx_export import build_docx
        open(a.docx, "wb").write(build_docx(title, a.page, blocks, result))
        print(f"Word copy saved to {a.docx}")


if __name__ == "__main__":
    main()
