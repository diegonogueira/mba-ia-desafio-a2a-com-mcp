"""Leitura do texto fixo que chega por A2A. Sem LLM: so regra."""

from __future__ import annotations

import re

CAMPOS_PEDIDO = ("sala", "inicio", "fim", "responsavel")
_PEDIDO = re.compile(
    r"^\s*reservar\s+sala=(?P<sala>\S+)\s+inicio=(?P<inicio>\S+)\s+fim=(?P<fim>\S+)\s+responsavel=(?P<responsavel>.+?)\s*$"
)
_ESCOLHA = re.compile(r"^\s*escolha=(?P<valor>\S+)\s*$")
RECUSAR = "recusar"

FORMATO_PEDIDO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"


def ler_pedido(texto: str) -> dict[str, str] | None:
    casou = _PEDIDO.match(texto)
    return {campo: casou.group(campo) for campo in CAMPOS_PEDIDO} if casou else None


def ler_escolha(texto: str) -> str | None:
    casou = _ESCOLHA.match(texto)
    return casou.group("valor") if casou else None
