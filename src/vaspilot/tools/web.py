"""Web access for the agent: search adapters and a hardened URL fetcher.

- web_search: provider adapters (zhipu web-search, bocha, and a keyless
  Bing SERP reader). The API key comes from ``VASPILOT_WEBSEARCH_KEY``
  or the DPAPI vault — never logged; bing needs none.
- web_fetch: plain http(s) GET with SSRF protection (no private/loopback
  targets, no exotic ports), HTML stripped to readable text, size-capped.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from ..core.errors import ValidationError

FETCH_TIMEOUT = 10
FETCH_BODY_CAP = 512 * 1024   # raw bytes read from the wire
FETCH_TEXT_CAP = 50 * 1024    # text returned to the model
ALLOWED_PORTS = (80, 443, 8080, 8443)

_SEARCH_PROVIDERS = ("zhipu", "bocha", "bing")


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "head", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4",
                   "h5", "h6", "section", "article", "table"):
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.chunks.append(data)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = "".join(parser.chunks)
    text = text.replace("\xa0", " ")  # &nbsp; from convert_charrefs
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _assert_public_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValidationError("web_fetch allows only http/https URLs")
    host = parsed.hostname or ""
    if not host:
        raise ValidationError("URL has no host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError(f"invalid port: {exc}") from exc
    if port is not None and port not in ALLOWED_PORTS:
        raise ValidationError(
            f"port {port} is not allowed (allowed: {ALLOWED_PORTS})")
    # resolve and refuse anything that is not a public address
    try:
        infos = socket.getaddrinfo(host, port or (443 if parsed.scheme ==
                                                  "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValidationError(f"cannot resolve host {host!r}: {exc}") from exc
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            continue
        if (not ip.is_global) or ip.is_private or ip.is_loopback or \
                ip.is_link_local or ip.is_reserved:
            raise ValidationError(
                f"web_fetch refuses non-public targets ({host} -> {ip})")
    return parsed


def web_fetch(url: str) -> dict[str, Any]:
    if not url or len(url) > 2048:
        raise ValidationError("url is required (max 2048 chars)")
    parsed = _assert_public_url(url.strip())
    request = urllib.request.Request(
        parsed.geturl(),
        headers={"User-Agent": "Mozilla/5.0 (VASPilot research agent)",
                 "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"})
    # connect directly: a proxy would fetch on our behalf and make the
    # public-target check above meaningless
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=FETCH_TIMEOUT) as response:
        raw = response.read(FETCH_BODY_CAP + 1)
        truncated = len(raw) > FETCH_BODY_CAP
        raw = raw[:FETCH_BODY_CAP]
        charset = response.headers.get_content_charset() or "utf-8"
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    content_type = response.headers.get("Content-Type", "")  # type: ignore[union-attr]
    if "html" in content_type.lower():
        text = _html_to_text(text)
    if len(text) > FETCH_TEXT_CAP:
        text = text[:FETCH_TEXT_CAP]
        truncated = True
    return {"url": parsed.geturl(), "content_type": content_type,
            "text": text, "truncated": truncated}


def web_search(query: str, *, provider: str, api_key: str = "") -> dict[str, Any]:
    query = str(query or "").strip()
    if not query:
        raise ValidationError("query is required")
    if len(query) > 400:
        raise ValidationError("query too long (max 400 chars)")
    if provider not in _SEARCH_PROVIDERS:
        raise ValidationError(
            f"web-search provider must be one of {_SEARCH_PROVIDERS}")
    if provider != "bing" and not api_key:
        raise ValidationError(
            "no web-search API key configured (set VASPILOT_WEBSEARCH_KEY "
            "or save one in the UI settings)")
    if provider == "zhipu":
        return _search_zhipu(query, api_key)
    if provider == "bocha":
        return _search_bocha(query, api_key)
    return _search_bing(query)


def _post_json(url: str, payload: dict, api_key: str,
               timeout: int = 15) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _search_zhipu(query: str, api_key: str) -> dict[str, Any]:
    document = _post_json("https://open.bigmodel.cn/api/paas/v4/web_search",
                          {"search_engine": "search-pro",
                           "search_query": query}, api_key)
    results = []
    for item in (document.get("search_result") or
                 document.get("data", {}).get("search_result") or [])[:10]:
        if isinstance(item, dict):
            results.append({
                "title": str(item.get("title", ""))[:200],
                "url": str(item.get("link") or item.get("url") or "")[:500],
                "snippet": str(item.get("content") or
                               item.get("snippet") or "")[:400],
            })
    return {"provider": "zhipu", "query": query, "results": results}


def _search_bocha(query: str, api_key: str) -> dict[str, Any]:
    document = _post_json("https://api.bochaai.com/v1/web-search",
                          {"query": query, "summary": True,
                           "count": 10}, api_key)
    pages = ((document.get("data") or {}).get("webPages") or {})
    results = []
    for item in (pages.get("value") or [])[:10]:
        if isinstance(item, dict):
            results.append({
                "title": str(item.get("name", ""))[:200],
                "url": str(item.get("url") or "")[:500],
                "snippet": str(item.get("snippet") or
                               item.get("summary") or "")[:400],
            })
    return {"provider": "bocha", "query": query, "results": results}


class _BingSerpParser(HTMLParser):
    """Extract organic results from bing.com SERP ``li.b_algo`` blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._in_algo = False
        self._li_depth = 0
        self._h2_depth = 0
        self._href = ""
        self._title: list[str] = []
        self._title_done = False
        self._p_depth = 0
        self._snippet: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = str(a.get("class") or "").split()
        if tag == "li" and "b_algo" in classes and not self._in_algo:
            self._in_algo = True
            self._li_depth = 0
            self._h2_depth = 0
            self._href = ""
            self._title = []
            self._title_done = False
            self._p_depth = 0
            self._snippet = []
            return
        if not self._in_algo:
            return
        if tag == "li":
            self._li_depth += 1
        elif tag == "h2":
            self._h2_depth += 1
        elif tag == "a" and self._h2_depth and not self._title_done:
            href = str(a.get("href") or "")
            if href.startswith("http"):
                self._href = href
        elif tag == "p" and self._title_done:
            self._p_depth += 1

    def handle_endtag(self, tag):
        if not self._in_algo:
            return
        if tag == "h2" and self._h2_depth:
            self._h2_depth -= 1
            if not self._h2_depth:
                self._title_done = True
        elif tag == "p" and self._p_depth:
            self._p_depth -= 1
        elif tag == "li":
            if self._li_depth:
                self._li_depth -= 1
            else:
                title = "".join(self._title).strip()
                if title and self._href:
                    self.results.append({
                        "title": title[:200], "url": self._href[:500],
                        "snippet": "".join(self._snippet).strip()[:400]})
                self._in_algo = False

    def handle_data(self, data):
        if not self._in_algo:
            return
        if self._h2_depth:
            self._title.append(data)
        elif self._p_depth:
            self._snippet.append(data)


def _search_bing(query: str) -> dict[str, Any]:
    """Keyless Bing web results: fetch the SERP with the same hardened
    direct-connection fetcher and parse the organic blocks. Layout-driven,
    so a Bing redesign may reduce yield; the paid adapters stay the
    high-reliability option."""
    url = ("https://www.bing.com/search?" +
           urllib.parse.urlencode({"q": query, "mkt": "zh-CN",
                                   "count": "10"}))
    parsed = _assert_public_url(url)
    request = urllib.request.Request(
        parsed.geturl(),
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 "
                 "Safari/537.36",
                 "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
                 "Accept": "text/html,application/xhtml+xml,*/*;q=0.5"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=15) as response:
        raw = response.read(FETCH_BODY_CAP)
        charset = response.headers.get_content_charset() or "utf-8"
    parser = _BingSerpParser()
    try:
        parser.feed(raw.decode(charset, errors="replace"))
    except Exception:
        pass
    return {"provider": "bing", "query": query,
            "results": parser.results[:10]}
