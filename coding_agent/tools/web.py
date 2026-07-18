from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import uuid
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from . import RiskLevel, ToolDefinition, ToolSpec

if TYPE_CHECKING:
    from . import ToolRegistry

# Parity with Rust web_fetch.rs limits
_MAX_RESPONSE_SIZE = 5 * 1024 * 1024  # 5 MB
_MAX_REDIRECTS = 10
_FETCH_TIMEOUT = 30
_BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"

_log = logging.getLogger("yucode.tools.web")


def _web_fetch_artifact_path(registry: ToolRegistry) -> Path:
    """Return a fresh path for a full-page web_fetch artifact under workspace state."""
    from ..config.settings import state_dir
    out_dir = state_dir(registry.workspace_root) / "web_fetch"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{uuid.uuid4().hex[:12]}.md"


def web_tools(registry: ToolRegistry) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            ToolSpec(
                "web_fetch",
                "Fetch a URL and return its cleaned text content. "
                "Use this AFTER web_search to read the most promising result pages. "
                "Pass a 'prompt' describing what you need so the output is focused. "
                "Works on HTML pages, APIs, and plain text. "
                "Returns: url, status, content_type, bytes, duration_ms, result (cleaned text).",
                {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Full URL to fetch."},
                        "prompt": {"type": "string", "description": "What you are looking for on this page. Helps extract relevant content."},
                    },
                    "required": ["url"],
                },
                "read-only",
                RiskLevel.HIGH,
            ),
            lambda args: _web_fetch(registry, args),
        ),
        ToolDefinition(
            ToolSpec(
                "web_search",
                "Search the web. Uses Brave Search API if BRAVE_API_KEY is set, else "
                "DuckDuckGo HTML; auto-retries with a relaxed query on zero hits. "
                "Returns {results: [{title, url}], _meta: {backends_tried}}. "
                "IMPORTANT: After searching, pick the 1-3 most relevant URLs and use web_fetch "
                "to read their content. Do NOT keep searching endlessly — fetch the best results "
                "to extract the actual data. If the first search doesn't find what you need, "
                "try ONE more refined query, then fetch the best hits.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query. Be specific with names, dates, amounts."},
                        "allowed_domains": {"type": "array", "items": {"type": "string"}, "description": "Only include results from these domains."},
                        "blocked_domains": {"type": "array", "items": {"type": "string"}, "description": "Exclude results from these domains."},
                    },
                    "required": ["query"],
                },
                "read-only",
                RiskLevel.HIGH,
            ),
            lambda args: _web_search(args),
        ),
    ]


def _build_redirect_limited_opener() -> urllib.request.OpenerDirector:
    """Build an opener that enforces _MAX_REDIRECTS."""

    class _LimitedRedirectHandler(urllib.request.HTTPRedirectHandler):
        max_redirections = _MAX_REDIRECTS

    return urllib.request.build_opener(_LimitedRedirectHandler)


def _web_fetch(registry: ToolRegistry, args: dict[str, Any]) -> str:
    raw_url = str(args["url"])
    prompt = str(args.get("prompt", ""))
    url = _normalize_fetch_url(raw_url)
    started = time.monotonic()

    req = urllib.request.Request(url, headers={
        "User-Agent": "yucode-agent/0.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    opener = _build_redirect_limited_opener()
    with opener.open(req, timeout=_FETCH_TIMEOUT) as response:
        body_bytes = response.read(_MAX_RESPONSE_SIZE + 1)
        final_url = response.url
        status_code = response.status
        content_type = response.headers.get("Content-Type", "")

    truncated = len(body_bytes) > _MAX_RESPONSE_SIZE
    if truncated:
        body_bytes = body_bytes[:_MAX_RESPONSE_SIZE]

    duration_ms = int((time.monotonic() - started) * 1000)
    body = body_bytes.decode("utf-8", errors="replace")
    byte_size = len(body_bytes)

    is_html = "html" in content_type.lower() or body.lstrip()[:15].lower().startswith("<!doctype") or body.lstrip()[:15].lower().startswith("<html")
    cleaned = _clean_html(body) if is_html else body.strip()
    summary = _summarize_web_fetch(final_url, prompt, cleaned)

    # The prompt-based extraction above (_summarize_web_fetch) caps its
    # output at a few thousand chars — anything beyond that used to be
    # silently gone. If it dropped content, persist the full cleaned page so
    # it's still recoverable via read_file/grep instead of lost outright.
    artifact_path: Path | None = None
    if len(cleaned) > len(summary):
        artifact_path = _web_fetch_artifact_path(registry)
        artifact_path.write_text(cleaned, encoding="utf-8", errors="replace")
        summary += (
            f"\n\n[Full page content ({len(cleaned):,} chars) saved to: {artifact_path}. "
            "Use read_file with offset/limit, or grep_search on that file, to find "
            "specific details beyond this preview.]"
        )

    result: dict[str, Any] = {
        "url": final_url,
        "status": status_code,
        "content_type": content_type,
        "bytes": byte_size,
        "duration_ms": duration_ms,
        "result": summary,
    }
    if truncated:
        result["truncated"] = True
        result["max_bytes"] = _MAX_RESPONSE_SIZE
    if artifact_path is not None:
        result["artifact_file"] = str(artifact_path)

    return json.dumps(result, indent=2)


def _web_search(args: dict[str, Any]) -> str:
    """Web search with backend fallback chain.

    Order of preference:
      1. Brave Search API (if BRAVE_API_KEY env var is set) — most reliable, current.
      2. DuckDuckGo HTML scraping (default; no key required, but brittle).
      3. DuckDuckGo retry with relaxed query (strip quotes, drop short tokens) — fires only on 0 hits.

    The response is always the JSON-encoded list of {title, url} hits the
    tool spec promises, plus an optional `_meta.fallback` field naming which
    backend ultimately produced the results.
    """
    raw_query = str(args["query"]).strip()
    allowed_domains: list[str] | None = args.get("allowed_domains")
    blocked_domains: list[str] | None = args.get("blocked_domains")

    fallback_path: list[str] = []
    hits: list[dict[str, str]] = []

    # 1) Brave Search API first if configured
    brave_key = os.environ.get("BRAVE_API_KEY", "").strip()
    if brave_key:
        try:
            hits = _brave_search(raw_query, brave_key)
            fallback_path.append("brave")
        except Exception as exc:
            _log.warning("Brave Search failed (%s); falling back to DuckDuckGo", exc)

    # 2) DuckDuckGo HTML (primary if no Brave key, fallback otherwise)
    if not hits:
        hits = _duckduckgo_search(raw_query)
        fallback_path.append("duckduckgo")

    # 3) Zero-hit retry with relaxed query
    if not hits:
        relaxed = _relax_query(raw_query)
        if relaxed and relaxed != raw_query:
            _log.info("0 hits for `%s`; retrying with relaxed `%s`", raw_query, relaxed)
            hits = _duckduckgo_search(relaxed)
            fallback_path.append("duckduckgo_relaxed")

    # Domain filters
    if allowed_domains:
        hits = [h for h in hits if any(d in h["url"] for d in allowed_domains)]
    if blocked_domains:
        hits = [h for h in hits if not any(d in h["url"] for d in blocked_domains)]

    # Dedup by URL, preserve order
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for h in hits:
        if h["url"] not in seen:
            seen.add(h["url"])
            deduped.append(h)

    result: dict[str, Any] = {"results": deduped[:10]}
    if fallback_path:
        result["_meta"] = {"backends_tried": fallback_path}
    if not deduped:
        result["_meta"] = result.get("_meta", {})
        result["_meta"]["hint"] = (
            "No results from any backend. Try a different query, "
            "or set BRAVE_API_KEY for a more reliable search backend."
        )
    return json.dumps(result, indent=2)


def _duckduckgo_search(query: str) -> list[dict[str, str]]:
    """Single-shot DDG HTML scrape. Returns [] on failure rather than raising."""
    if not query.strip():
        return []
    encoded = urllib.parse.quote_plus(query)
    url = f"https://duckduckgo.com/html/?q={encoded}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "yucode-agent/0.1",
        "Accept": "text/html",
    })
    opener = _build_redirect_limited_opener()
    try:
        with opener.open(req, timeout=_FETCH_TIMEOUT) as response:
            body = response.read(_MAX_RESPONSE_SIZE).decode("utf-8", errors="replace")
    except Exception as exc:
        _log.warning("DuckDuckGo fetch failed: %s", exc)
        return []
    hits = _extract_ddg_search_hits(body)
    if not hits:
        hits = _extract_generic_link_hits(body)
    return hits


def _brave_search(query: str, api_key: str) -> list[dict[str, str]]:
    """Brave Search API. Returns [{title, url}] or raises on HTTP error."""
    if not query.strip():
        return []
    encoded = urllib.parse.urlencode({"q": query, "count": 10})
    url = f"{_BRAVE_API_URL}?{encoded}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "yucode-agent/0.1",
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
    })
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as response:
        body = response.read(_MAX_RESPONSE_SIZE).decode("utf-8", errors="replace")
    data = json.loads(body)
    web = (data.get("web") or {}).get("results") or []
    return [
        {"title": str(r.get("title", "")), "url": str(r.get("url", ""))}
        for r in web
        if r.get("url")
    ]


_QUERY_FILLER = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "for", "to", "by",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "and", "or", "but", "with", "this", "that", "these", "those",
    "what", "who", "where", "when", "why", "how",
    "what's", "who's",
})


def _relax_query(query: str) -> str:
    """Drop quotes and short filler words to widen a zero-hit query."""
    cleaned = query.replace('"', " ").replace("'", " ")
    tokens = cleaned.split()
    kept = [t for t in tokens if len(t) > 2 and t.lower() not in _QUERY_FILLER]
    if not kept:
        kept = tokens  # don't strip everything
    return " ".join(kept)


def _clean_html(body: str) -> str:
    text = re.sub(r"<script.*?</script>", "", body, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.S | re.I)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _normalize_fetch_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        return "https" + url[4:]
    return url


def _summarize_web_fetch(url: str, prompt: str, content: str) -> str:
    lower_prompt = prompt.lower()
    compact = re.sub(r"\s+", " ", content).strip()

    if "title" in lower_prompt:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", content, re.I | re.S)
        if title_match:
            return f"Title: {unescape(title_match.group(1)).strip()}"
        return compact[:600]

    if "summary" in lower_prompt or "summarize" in lower_prompt:
        return compact[:2000]

    if "price" in lower_prompt or "transaction" in lower_prompt:
        return compact[:3000]

    return compact[:4000]


def _decode_ddg_redirect(href: str) -> str | None:
    if "duckduckgo.com/l/" in href or "duckduckgo.com%2Fl%2F" in href:
        match = re.search(r"[?&]uddg=([^&]+)", href)
        if match:
            return urllib.parse.unquote(match.group(1))
    if href.startswith("http://") or href.startswith("https://"):
        parsed = urlparse(href)
        if "duckduckgo.com" not in (parsed.hostname or ""):
            return href
    return None


def _extract_ddg_search_hits(html: str) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for match in re.finditer(r'<a[^>]+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', html, re.S | re.I):
        raw_url = unescape(match.group(1))
        title = unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
        decoded_url = _decode_ddg_redirect(raw_url)
        if decoded_url and title:
            hits.append({"title": title, "url": decoded_url})
    if not hits:
        for match in re.finditer(r'<a[^>]+href="([^"]*)"[^>]*class="result__a"[^>]*>(.*?)</a>', html, re.S | re.I):
            raw_url = unescape(match.group(1))
            title = unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
            decoded_url = _decode_ddg_redirect(raw_url)
            if decoded_url and title:
                hits.append({"title": title, "url": decoded_url})
    return hits


def _extract_generic_link_hits(html: str) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for match in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        url = unescape(match.group(1))
        title = unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
        parsed = urlparse(url)
        if "duckduckgo.com" in (parsed.hostname or ""):
            continue
        if title and len(title) > 2:
            hits.append({"title": title, "url": url})
    return hits
