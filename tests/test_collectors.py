"""采集器测试（使用本地 mock RSS）。"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from src.collectors.rss import RSSCollector

_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test</title>
<item><title>Breaking News A</title>
<link>https://example.com/a</link>
<description>Content A</description>
<pubDate>Mon, 05 Aug 2026 10:00:00 GMT</pubDate>
</item>
<item><title>Breaking News B</title>
<link>https://example.com/b</link>
<description>Content B</description>
</item>
</channel></rss>
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.end_headers()
        self.wfile.write(_RSS.encode("utf-8"))

    def log_message(self, *args):  # noqa: ANN002
        pass


class _Server:
    def __init__(self):
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):  # noqa: ANN002
        self.httpd.shutdown()


def test_rss_collector():
    with _Server() as server:
        collector = RSSCollector(name="ap_news", config={"rss_url": f"http://127.0.0.1:{server.port}/rss"})
        msgs = collector.fetch()
    assert len(msgs) == 2
    assert msgs[0].title == "Breaking News A"
    assert msgs[0].url == "https://example.com/a"
    assert msgs[0].source == "rss"
