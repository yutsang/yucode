from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from html import unescape
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from . import RiskLevel, ToolDefinition, ToolSpec

if TYPE_CHECKING:
    from . import ToolRegistry


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
            lambda args: _web_fetch(args),
        ),
        ToolDefinition(
            ToolSpec(
                "web_search",
                "Search the web via DuckDuckGo. Returns a list of {title, url} results. "
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


def _web_fetch(args: dict[str, Any]) -> str:
    raw_url = str(args["url"])
    prompt = str(args.get("prompt", ""))
    url = _normalize_fetch_url(raw_url)
    started = time.monotonic()

    req = urllib.request.Request(url, headers={
        "User-Agent": "yucode-agent/0.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=20) as response:
        body_bytes = response.read()
        final_url = response.url
        status_code = response.status
        content_type = response.headers.get("Content-Type", "")

    duration_ms = int((time.monotonic() - started) * 1000)
    body = body_bytes.decode("utf-8", errors="replace")
    byte_size = len(body_bytes)

    is_html = "html" in content_type.lower() or body.lstrip()[:15].lower().startswith("<!doctype") or body.lstrip()[:15].lower().startswith("<html")
    cleaned = _clean_html(body) if is_html else body.strip()
    summary = _summarize_web_fetch(final_url, prompt, cleaned)

    return json.dumps({
        "url": final_url,
        "status": status_code,
        "content_type": content_type,
        "bytes": byte_size,
        "duration_ms": duration_ms,
        "result": summary,
    }, indent=2)


def _web_search(args: dict[str, Any]) -> str:
    query = urllib.parse.quote_plus(str(args["query"]))
    url = f"https://duckduckgo.com/html/?q={query}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "yucode-agent/0.1",
        "Accept": "text/html",
    })
    with urllib.request.urlopen(req, timeout=20) as response:
        body = response.read().decode("utf-8", errors="replace")

    hits = _extract_ddg_search_hits(body)
    if not hits:
        hits = _extract_generic_link_hits(body)

    allowed_domains: list[str] | None = args.get("allowed_domains")
    blocked_domains: list[str] | None = args.get("blocked_domains")
    if allowed_domains:
        hits = [h for h in hits if any(d in h["url"] for d in allowed_domains)]
    if blocked_domains:
        hits = [h for h in hits if not any(d in h["url"] for d in blocked_domains)]

    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for h in hits:
        if h["url"] not in seen:
            seen.add(h["url"])
            deduped.append(h)

    return json.dumps(deduped[:10], indent=2)


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
