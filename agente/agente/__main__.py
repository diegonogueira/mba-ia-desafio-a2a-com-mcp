"""Sobe o agente: Agent Card e endpoint A2A em http://localhost:7300 por padrao."""

from __future__ import annotations

import os

import uvicorn

from agente.a2a import criar_app


def main() -> None:
    host = os.environ.get("AGENTE_HOST", "127.0.0.1")
    porta = int(os.environ.get("AGENTE_PORT", "7300"))
    url_mcp = os.environ.get("MCP_URL", "http://localhost:7301/mcp")
    url_a2a = os.environ.get("AGENTE_URL_PUBLICA", f"http://localhost:{porta}/a2a")
    uvicorn.run(criar_app(url_mcp, url_a2a), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
