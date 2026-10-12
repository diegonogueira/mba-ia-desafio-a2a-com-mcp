"""Sobe o servidor MCP em Streamable HTTP: http://localhost:7301/mcp por padrao."""

from __future__ import annotations

import os
import sys

import uvicorn

from servidor_mcp.log_requests import LogRequests
from servidor_mcp.servidor import criar_servidor

MINIMO_BYTES = 32


def ler_segredo() -> bytes:
    """REQUEST_STATE_SECRET em hex (secrets.token_hex(32)), com pelo menos 32 bytes."""
    bruto = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    if not bruto:
        sys.exit("REQUEST_STATE_SECRET nao definido. Gere com: python3 -c \"import secrets; print(secrets.token_hex(32))\"")
    try:
        segredo = bytes.fromhex(bruto)
    except ValueError:
        sys.exit("REQUEST_STATE_SECRET precisa ser hexadecimal, como o gerado por secrets.token_hex(32)")
    if len(segredo) < MINIMO_BYTES:
        sys.exit(f"REQUEST_STATE_SECRET precisa ter pelo menos {MINIMO_BYTES} bytes (64 caracteres hex)")
    return segredo


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    porta = int(os.environ.get("MCP_PORT", "7301"))
    servidor = criar_servidor(ler_segredo())
    app = servidor.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host=host)
    uvicorn.run(LogRequests(app), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
