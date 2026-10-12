"""Servidor MCP da Central de Salas: tres tools, um resource e o ciclo de MRTR na reserva."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INVALID_PARAMS,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
)
from pydantic import BaseModel

from servidor_mcp import dominio

NOME = "central-de-salas"
CHAVE_ESCOLHA = "escolha_de_sala"
VERSAO_ESTADO = 1
# Validade do requestState: o boundary do SDK carimba exp = agora + TTL e rejeita depois disso.
TTL_REQUEST_STATE_SEGUNDOS = 600.0
CAPABILITY_FORM = {"elicitation": {"form": {}}}


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


def criar_servidor(segredo: bytes) -> MCPServer:
    mcp = MCPServer(
        NOME,
        version="1.0.0",
        request_state_security=RequestStateSecurity(keys=[segredo], ttl=TTL_REQUEST_STATE_SEGUNDOS),
    )

    @mcp.tool(description="Lista todas as salas com capacidade e recursos.")
    def listar_salas() -> ListaDeSalas:
        return ListaDeSalas(salas=[SalaOut(**vars(s)) for s in dominio.SALAS.values()])

    @mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
    def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
        _, comeco, termino = _validar(sala, inicio, fim)
        encontrados = dominio.conflitos(sala, comeco, termino)
        return Disponibilidade(
            sala=sala,
            livre=not encontrados,
            conflitos=[ConflitoOut(id=r.id, inicio=r.inicio, fim=r.fim, responsavel=r.responsavel) for r in encontrados],
        )

    @mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
    def reservar_sala(
        sala: str, inicio: str, fim: str, responsavel: str, ctx: Context
    ) -> ReservaOut | InputRequiredResult:
        if ctx.request_state is not None:
            return _retomar(ctx)
        return _reservar_ou_perguntar(sala, inicio, fim, responsavel, ctx)

    @mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
                  description="Politica de uso das salas. A primeira linha declara a versao.")
    def politica() -> str:
        return dominio.POLITICA_TEXTO

    return mcp


def _validar(sala: str, inicio: str, fim: str):
    try:
        return dominio.validar(sala, inicio, fim)
    except dominio.RegraVioladaError as e:
        raise ToolError(str(e)) from None


def _reservar_ou_perguntar(
    sala: str, inicio: str, fim: str, responsavel: str, ctx: Context
) -> ReservaOut | InputRequiredResult:
    pedida, comeco, termino = _validar(sala, inicio, fim)
    if not dominio.conflitos(sala, comeco, termino):
        return _reservar(sala, inicio, fim, responsavel)

    opcoes = dominio.alternativas(pedida, comeco, termino)
    if not opcoes:
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
    _exigir_elicitation_form(ctx)

    # Tudo que o retry precisa para reconstruir o pedido viaja aqui. O boundary do SDK
    # sela este texto (AES-256-GCM com a chave de REQUEST_STATE_SECRET, exp e vinculo
    # com a tool e os argumentos) antes de ele sair; nada fica guardado no servidor.
    estado = {
        "v": VERSAO_ESTADO,
        "sala": sala,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
        "alternativas": opcoes,
    }
    pergunta = ElicitRequest(
        params=ElicitRequestFormParams(
            message="A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
            requested_schema={
                "type": "object",
                "properties": {
                    "sala": {
                        "type": "string",
                        "title": "Sala",
                        "description": "Sala alternativa escolhida",
                        "enum": opcoes,
                    }
                },
                "required": ["sala"],
            },
        )
    )
    return InputRequiredResult(input_requests={CHAVE_ESCOLHA: pergunta}, request_state=json.dumps(estado))


def _exigir_elicitation_form(ctx: Context) -> None:
    """A elicitation so sai para quem declarou form mode no _meta deste request."""
    elicitation = ctx.client_capabilities.elicitation if ctx.client_capabilities else None
    # `elicitation: {}` sem modos equivale a form; declarar so url nao vale.
    if elicitation is not None and (elicitation.form is not None or elicitation.url is None):
        return
    raise MCPError(
        code=MISSING_REQUIRED_CLIENT_CAPABILITY,
        message="Client did not declare the form elicitation capability required by reservar_sala",
        data={"requiredCapabilities": CAPABILITY_FORM},
    )


def _retomar(ctx: Context) -> ReservaOut | InputRequiredResult:
    """Retry do MRTR: o boundary ja verificou integridade e validade; o pedido vem so do estado selado."""
    estado: dict[str, Any] = json.loads(ctx.request_state or "")
    if estado.get("v") != VERSAO_ESTADO:
        raise MCPError(code=INVALID_PARAMS, message="Invalid or expired requestState")
    resposta = (ctx.input_responses or {}).get(CHAVE_ESCOLHA)
    if not isinstance(resposta, ElicitResult):
        raise MCPError(code=INVALID_PARAMS, message=f"inputResponses sem a resposta para {CHAVE_ESCOLHA!r}")

    if resposta.action in ("decline", "cancel"):
        return ReservaOut(reservado=False, motivo="recusado")

    escolhida = (resposta.content or {}).get("sala")
    if escolhida not in estado["alternativas"]:
        raise ToolError(f"Escolha invalida: {escolhida} nao esta entre as alternativas oferecidas")

    _, comeco, termino = _validar(escolhida, estado["inicio"], estado["fim"])
    if dominio.conflitos(escolhida, comeco, termino):
        # A alternativa foi ocupada entre a pergunta e o retry: pergunta de novo sobre o pedido original.
        return _reservar_ou_perguntar(estado["sala"], estado["inicio"], estado["fim"], estado["responsavel"], ctx)
    return _reservar(escolhida, estado["inicio"], estado["fim"], estado["responsavel"])


def _reservar(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    reserva = dominio.criar_reserva(sala, inicio, fim, responsavel)
    return ReservaOut(
        reserva=reserva.id,
        sala=reserva.sala,
        inicio=reserva.inicio,
        fim=reserva.fim,
        responsavel=reserva.responsavel,
        politica=dominio.POLITICA_VERSAO,
    )
