"""
Handler Vercel — serve a página HTML e POST /atualizar refaz a busca.

A URL aceita `?classe=premium` ou `?classe=executiva` para alternar
entre as classes configuradas em VIAGEM_CLASSE (CSV).
"""
from __future__ import annotations

import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from buscar_passagens import (  # noqa: E402
        _classes_from_env,
        gerar_pagina_completa,
        viagem_from_env,
    )
    _IMPORT_ERROR: str | None = None
except Exception:  # pragma: no cover
    _IMPORT_ERROR = traceback.format_exc()

_cache: dict[str, str] = {}


def _pagina_erro(titulo: str, detalhe: str) -> str:
    safe = detalhe.replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Erro</title>"
        "<style>body{font-family:system-ui;background:#0f172a;color:#e2e8f0;"
        "padding:40px;max-width:900px;margin:0 auto}"
        "pre{background:#1e293b;padding:16px;border-radius:8px;"
        "overflow:auto;font-size:13px;line-height:1.5}"
        "h1{color:#fbbf24}</style></head><body>"
        f"<h1>{titulo}</h1><pre>{safe}</pre>"
        "<p style='color:#94a3b8'>Confira em Settings → Environment "
        "Variables: <code>VIAGEM_TRECHOS</code> (ex.: "
        "<code>SAO-IBZ:16/07/2026;CDG-SAO:01/08/2026;BRU-SAO:01/08/2026</code>), "
        "<code>VIAGEM_CLASSE</code>, <code>VIAGEM_MAX_ESCALAS</code>. "
        "Após alterar, rode <code>POST /atualizar</code> ou redeploy.</p>"
        "</body></html>"
    )


def _norm_path(raw: str) -> str:
    p = raw.split("?")[0].rstrip("/") or "/"
    if p.startswith("/api"):
        p = p[4:] or "/"
    return p


def _classe_da_url(raw: str) -> str | None:
    """Lê `?classe=...` da URL e retorna a classe válida correspondente.
    Devolve None quando ausente/inválida — `gerar_pagina_completa`
    aplica o default (primeira de VIAGEM_CLASSE)."""
    try:
        qs = parse_qs(urlparse(raw).query)
    except Exception:
        return None
    valor = (qs.get("classe") or [None])[0]
    if not valor:
        return None
    valor = valor.strip().lower()
    try:
        validas = _classes_from_env()
    except Exception:
        return valor
    return valor if valor in validas else None


def _atualizar_html(classe: str | None = None) -> str:
    if _IMPORT_ERROR:
        raise RuntimeError(_IMPORT_ERROR)
    html = gerar_pagina_completa(classe=classe)
    chave = f"html:{classe or '_default'}"
    _cache[chave] = html
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
            classe = _classe_da_url(self.path)
            chave = f"html:{classe or '_default'}"
            try:
                body = _cache.get(chave) or _atualizar_html(classe)
                self._send(200, body.encode("utf-8"))
            except Exception:
                pag = _pagina_erro(
                    "Erro ao gerar a página",
                    traceback.format_exc(),
                )
                self._send(500, pag.encode("utf-8"))
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"nao encontrado", "text/plain")

    def do_POST(self):  # noqa: N802
        path = _norm_path(self.path)
        if path == "/atualizar":
            try:
                classe = _classe_da_url(self.path)
                _atualizar_html(classe)
                self._send(200, b'{"ok":true}', "application/json")
            except Exception:
                self._send(
                    500,
                    json.dumps(
                        {"ok": False, "erro": traceback.format_exc()}
                    ).encode("utf-8"),
                    "application/json",
                )
        else:
            self._send(404, b"nao encontrado", "text/plain")
