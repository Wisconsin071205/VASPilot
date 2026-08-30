"""Web tools: SSRF guard, HTML stripping, search adapters, key resolution."""

from __future__ import annotations

import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import vaspilot.tools.web as web
from vaspilot.core.config import Config
from vaspilot.core.errors import ValidationError
from vaspilot.tools.registry import ToolContext, ToolRegistry


class TestSsrfGuard:
    def test_localhost_refused(self):
        with pytest.raises(ValidationError):
            web.web_fetch("http://127.0.0.1:8000/x")
        with pytest.raises(ValidationError):
            web.web_fetch("http://localhost/docs")

    def test_private_ranges_refused(self):
        for url in ("http://192.168.1.1/", "http://10.0.0.5/",
                    "http://172.16.0.9/", "http://[::1]/"):
            with pytest.raises(ValidationError):
                web.web_fetch(url)

    def test_exotic_port_refused(self):
        with pytest.raises(ValidationError):
            web.web_fetch("http://example.com:9000/")

    def test_non_http_scheme_refused(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x",
                    "gopher://example.com"):
            with pytest.raises(ValidationError):
                web.web_fetch(url)


class TestFetch:
    @pytest.fixture()
    def local_server(self):
        """A real HTTP server; the SSRF guard is bypassed for the test only."""
        pages = {
            "/page": (b"<html><head><style>x{}</style></head><body>"
                      b"<h1>Title</h1><script>evil()</script>"
                      b"<p>Hello&nbsp;VASPilot</p></body></html>", "text/html"),
            "/big": (b"<html><body>" + b"<p>lorem ipsum</p>" * 20000
                     + b"</body></html>", "text/html"),
            "/plain": (b"plain text body", "text/plain"),
        }

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body, ctype = pages.get(self.path, (b"not found", "text/plain"))
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}"
        server.shutdown()

    def _fetch(self, base, path):
        original = web._assert_public_url
        web._assert_public_url = lambda url: web.urllib.parse.urlparse(url)
        try:
            return web.web_fetch(base + path)
        finally:
            web._assert_public_url = original

    def test_html_stripped(self, local_server):
        result = self._fetch(local_server, "/page")
        assert "Hello VASPilot" in result["text"]
        assert "Title" in result["text"]
        assert "evil()" not in result["text"]      # scripts dropped
        assert "x{}" not in result["text"]          # styles dropped

    def test_truncation_flag(self, local_server):
        result = self._fetch(local_server, "/big")
        assert result["truncated"] is True
        assert len(result["text"]) <= web.FETCH_TEXT_CAP

    def test_plain_passthrough(self, local_server):
        result = self._fetch(local_server, "/plain")
        assert result["text"] == "plain text body"


class TestSearch:
    def _patch_post(self, monkeypatch, document):
        monkeypatch.setattr(web, "_post_json", lambda *a, **k: document)

    def test_zhipu_adapter(self, monkeypatch):
        self._patch_post(monkeypatch, {
            "search_result": [
                {"title": "Fe lattice constant", "link": "https://mp.example/fe",
                 "content": "a=2.86 A"},
            ]})
        result = web.web_search("Fe lattice", provider="zhipu",
                                api_key="test-key")
        assert result["provider"] == "zhipu"
        assert result["results"][0]["title"] == "Fe lattice constant"
        assert result["results"][0]["snippet"] == "a=2.86 A"

    def test_bocha_adapter(self, monkeypatch):
        self._patch_post(monkeypatch, {
            "data": {"webPages": {"value": [
                {"name": "NEB tutorial", "url": "https://vasp.example/neb",
                 "snippet": "CI-NEB setup"}]}}})
        result = web.web_search("NEB", provider="bocha", api_key="test-key")
        assert result["results"][0]["url"] == "https://vasp.example/neb"

    def test_missing_key_rejected(self):
        with pytest.raises(ValidationError):
            web.web_search("q", provider="zhipu", api_key="")

    def test_bing_needs_no_key(self):
        assert web._search_bing.__doc__  # keyless adapter exists
        with pytest.raises(ValidationError):
            web.web_search("q", provider="google", api_key="k")

    SERP = """
    <ol id="b_results">
      <li class="b_algo"><h2><a href="https://vasp.example/wiki"
        class="link">VASP INCAR Guide</a></h2>
        <div class="b_caption"><p>ENCUT controls the plane-wave
        cutoff.</p></div></li>
      <li class="b_algo"><h2><a href="/relative/skip">no href</a></h2>
        <p>x</p></li>
      <li class="b_algo"><h2><a
        href="https://example.com/nested">Nested <b>title</b></a></h2>
        <p>snippet two</p></li>
    </ol>"""

    def test_bing_adapter_parses_serp(self, monkeypatch):
        body = self.SERP.encode("utf-8")

        class Headers:
            @staticmethod
            def get_content_charset():
                return "utf-8"

        class Response:
            headers = Headers()

            def read(self, n):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Opener:
            def open(self, req, timeout=None):
                return Response()

        monkeypatch.setattr(web, "_assert_public_url",
                            lambda url: web.urllib.parse.urlparse(url))
        monkeypatch.setattr(web.urllib.request, "build_opener",
                            lambda *a, **k: Opener())
        result = web.web_search("VASP INCAR", provider="bing")
        assert result["provider"] == "bing"
        assert len(result["results"]) == 2
        first = result["results"][0]
        assert first["title"] == "VASP INCAR Guide"
        assert first["url"] == "https://vasp.example/wiki"
        assert "ENCUT" in first["snippet"]
        # nested markup still concatenated into the title
        assert result["results"][1]["title"] == "Nested title"


class TestRegistryWebTools:
    @pytest.fixture()
    def registry(self, config_home, app_with_fake, monkeypatch):
        app, transport = app_with_fake
        monkeypatch.setenv("VASPILOT_WEBSEARCH_KEY", "env-key")
        app.config.set_websearch(provider="zhipu", enabled=True)
        context = ToolContext(config=app.config, client=app.client(),
                              audit=None)
        return ToolRegistry(context)

    def test_web_search_tool(self, registry, monkeypatch):
        monkeypatch.setattr(web, "_post_json",
                            lambda *a, **k: {"search_result": [
                                {"title": "t", "link": "https://x", 
                                 "content": "c"}]})
        result = registry.dispatch("web_search", {"query": "VASP ENCUT"})
        assert result["ok"] is True and len(result["results"]) == 1

    def test_web_search_disabled_rejected(self, registry, monkeypatch):
        registry.context.config.set_websearch(provider="zhipu", enabled=False)
        from vaspilot.core.errors import ValidationError as VE
        with pytest.raises(VE):
            registry.dispatch("web_search", {"query": "x"})
        registry.context.config.set_websearch(provider="zhipu", enabled=True)

    def test_web_fetch_tool_ssrf(self, registry):
        from vaspilot.core.errors import ValidationError as VE
        with pytest.raises(VE):
            registry.dispatch("web_fetch", {"url": "http://127.0.0.1/x"})

    def test_web_tools_are_read_kind(self, registry, monkeypatch):
        monkeypatch.setattr(web, "_post_json",
                            lambda *a, **k: {"search_result": []})
        names = registry.names()
        assert "web_search" in names and "web_fetch" in names
        assert registry.get("web_search").kind == "read"
        # read tools stay available in analysis_only mode
        result = registry.dispatch("web_search", {"query": "anything"},
                                   provider_mode="analysis_only")
        assert result["ok"] is True
