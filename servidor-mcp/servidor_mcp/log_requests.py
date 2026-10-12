"""Registro de cada request MCP no stderr: metodo, id e traceparent do _meta.

Tambem registra as clientCapabilities do _meta e os headers que o transporte
espelha do corpo (MCP-Protocol-Version, Mcp-Method, Mcp-Name).

Fica por fora do app do SDK para registrar tambem o que o SDK recusa antes de
chegar a um handler (por exemplo, _meta incompleto). O corpo e lido uma vez e
reentregue intacto ao app.
"""

from __future__ import annotations

import json
import sys
from typing import Any


class LogRequests:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        partes = []
        while True:
            mensagem = await receive()
            partes.append(mensagem.get("body", b""))
            if not mensagem.get("more_body"):
                break
        corpo = b"".join(partes)
        cabecalhos = {k.decode().lower(): v.decode(errors="replace") for k, v in scope.get("headers", [])}
        _registrar(corpo, cabecalhos)

        entregue = False

        async def receive_de_novo() -> dict:
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        await self.app(scope, receive_de_novo, send)


def _registrar(corpo: bytes, cabecalhos: dict[str, str]) -> None:
    try:
        mensagem = json.loads(corpo)
    except (ValueError, UnicodeDecodeError):
        print("mcp request ilegivel (JSON invalido)", file=sys.stderr, flush=True)
        return
    if not isinstance(mensagem, dict):
        print("mcp request fora do formato JSON-RPC", file=sys.stderr, flush=True)
        return
    params = mensagem.get("params") if isinstance(mensagem.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    alvo = params.get("name") or params.get("uri")
    campos = [
        f"method={mensagem.get('method')}",
        f"id={json.dumps(mensagem.get('id'))}",
        f"traceparent={meta.get('traceparent', '-')}",
    ]
    if alvo:
        campos.append(f"name={alvo}")
    if "requestState" in params:
        campos.append("retry=sim")
    capabilities = meta.get("io.modelcontextprotocol/clientCapabilities")
    campos.append("clientCapabilities=" + (json.dumps(capabilities, separators=(",", ":")) if capabilities is not None else "-"))
    espelhados = [f"{h}={cabecalhos[h.lower()]}" for h in ("MCP-Protocol-Version", "Mcp-Method", "Mcp-Name") if h.lower() in cabecalhos]
    campos.append("headers[" + " ".join(espelhados) + "]")
    print("mcp request " + " ".join(campos), file=sys.stderr, flush=True)
