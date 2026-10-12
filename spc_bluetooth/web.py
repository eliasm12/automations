"""Small Home Assistant ingress page for readings and log downloads."""
from datetime import datetime, timezone
from functools import partial
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from threading import Thread
from urllib.parse import urlsplit


def allowed_file(name):
    return bool(re.fullmatch(r"(?:latest\.json|inventory\.json|last-command\.json|metrics-\d{4}-\d{2}-\d{2}\.csv|raw-\d{4}-\d{2}-\d{2}\.jsonl)", name))


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, directory, **kwargs):
        self.directory = directory
        super().__init__(*args, **kwargs)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            try:
                result = json.loads((self.directory / "latest.json").read_text())
            except (OSError, ValueError):
                result = {"status": "starting", "metrics": {}}
            age = ""
            if result.get("timestamp"):
                seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(result["timestamp"])).total_seconds()
                age = f" ({max(0, int(seconds))} seconds ago)"
            rows = "".join(f"<tr><td>{escape(k)}</td><td>{escape(str(v))}</td></tr>"
                           for k, v in result.get("metrics", {}).items())
            errors = "".join(f"<li>{escape(e)}</li>" for e in result.get("errors", []))
            links = "".join(f'<li><a href="files/{f.name}">{f.name}</a></li>'
                            for f in sorted(self.directory.iterdir(), reverse=True)
                            if allowed_file(f.name))
            body = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><meta http-equiv="refresh" content="20">
<title>SPC inverter</title><style>body{{font:16px system-ui;max-width:850px;margin:32px auto;padding:0 20px}}
td{{padding:8px;border-bottom:1px solid #aaa}}a{{color:#1576c4}}li{{margin:8px 0}}</style>
<h1>SPC inverter readings</h1><p>Status: <b>{escape(result['status'])}</b></p>
<p>Last attempt: {escape(result.get('timestamp', 'Waiting'))}{age}</p>
<p>Experimental decoder: compare these measurements with WatchPower before relying on them.</p>
<table>{rows}</table><ul>{errors}</ul><h2>Downloads</h2><ul>{links}</ul></html>"""
            self.respond(body.encode(), "text/html; charset=utf-8")
        elif path.startswith("/files/") and allowed_file(path[7:]):
            try:
                file = self.directory / path[7:]
                data = file.read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.respond(data, "application/octet-stream", file.name)
        elif path == "/api/latest":
            try:
                self.respond((self.directory / "latest.json").read_bytes(), "application/json")
            except OSError:
                self.send_error(503)
        else:
            self.send_error(404)

    def respond(self, body, content_type, filename=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_server(directory, port=8099):
    server = ThreadingHTTPServer(("0.0.0.0", port), partial(Handler, directory=directory))
    Thread(target=server.serve_forever, daemon=True).start()
    return server
