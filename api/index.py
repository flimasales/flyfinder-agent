"""
Handler Vercel — serve a página HTML e POST /atualizar refaz a busca.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from buscar_passagens import gerar_pagina_completa, viagem_from_env  # noqa: E402

_cache: dict[str, str] = {}


def _norm_path(raw: str) -> str:
    p = raw.split("?")[0].rstrip("/") or "/"
    if p.startswith("/api"):
        p = p[4:] or "/"
    return p


def _atualizar_html() -> str:
    html = gerar_pagina_completa()
    _cache["html"] = html
    return html


class handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[vercel] {fmt % args}\n")

    def _send(self, code: int, body: bytes,
              ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = _norm_path(self.path)
        if path in ("/", "/index.html"):
            body = _cache.get("html") or _atualizar_html()
            self._send(200, body.encode("utf-8"))
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"nao encontrado", "text/plain")

    def do_POST(self):  # noqa: N802
        path = _norm_path(self.path)
        if path == "/atualizar":
            try:
                _atualizar_html()
                self._send(200, b'{"ok":true}', "application/json")
            except Exception as e:
                self._send(
                    500,
                    json.dumps({"ok": False, "erro": str(e)}).encode(),
                    "application/json",
                )
        else:
            self._send(404, b"nao encontrado", "text/plain")
