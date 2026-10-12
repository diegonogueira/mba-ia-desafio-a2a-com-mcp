"""A ponte: o agente como host MCP por dentro, costurado a Task do A2A por fora.

Nenhuma regra de sala mora aqui. Conflito, politica e alternativas sao decisao
do servidor MCP; o agente so traduz protocolo:

- input_required do MCP  -> Task em TASK_STATE_INPUT_REQUIRED (`_aplicar`)
- escolha do cliente A2A -> retry do tools/call com id novo, inputResponses e o
  requestState ecoado sem modificacao (`continuar`)
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import types
from contextlib import AsyncExitStack
from typing import Any

from mcp import Client
from mcp.client.session import ClientSession
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CONNECTION_CLOSED,
    CallToolResult,
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitResult,
    FormElicitationCapability,
    Implementation,
    InputRequiredResult,
    TextContent,
)

from agente import pedido as formato
from agente.tarefas import CANCELED, COMPLETED, FAILED, INPUT_REQUIRED, WORKING, Pausa, Task, novo_id

PROTOCOLO_MCP = "2026-07-28"
TOOL_RESERVA = "reservar_sala"
URI_POLITICA = "politica://uso"


def _somente_form(self: ClientSession, version: str) -> ClientCapabilities:
    return ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))


class ClienteMcp(Client):
    """Client do SDK declarando exatamente `{"elicitation": {"form": {}}}` no _meta de todo request.

    O SDK so declara elicitation quando recebe um callback, e ai declara form e url
    e passa a responder a elicitation sozinho. Aqui nao ha callback: o agente le o
    input_required cru e devolve a pergunta ao cliente A2A.
    """

    async def _build_session(self, exit_stack: AsyncExitStack) -> ClientSession:
        sessao = await super()._build_session(exit_stack)
        sessao._build_capabilities = types.MethodType(_somente_form, sessao)  # type: ignore[method-assign]
        return sessao


def _meta(task: Task) -> dict[str, Any]:
    """O mesmo trace-id da chamada A2A em todo request MCP da Task, com span-id novo."""
    return {"traceparent": f"00-{task.trace_id}-{secrets.token_hex(8)}-01"}


def _texto(resultado: CallToolResult) -> str:
    return " ".join(b.text for b in resultado.content if isinstance(b, TextContent))


def linha_alternativas(alternativas: list[str]) -> str:
    return "alternativas: " + ", ".join(alternativas)


class Ponte:
    """Mantem um Client MCP vivo entre as chamadas e faz a traducao MCP <-> Task."""

    def __init__(self, url_mcp: str) -> None:
        self._url_mcp = url_mcp
        self._cliente: ClienteMcp | None = None
        self._pronto = asyncio.Event()
        self._caiu = asyncio.Event()
        self._supervisor: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Ponte:
        self._supervisor = asyncio.create_task(self._manter_cliente())
        await self._pronto.wait()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._supervisor is not None:
            self._supervisor.cancel()
            await asyncio.gather(self._supervisor, return_exceptions=True)

    async def _manter_cliente(self) -> None:
        """Dono do ciclo de vida do Client: se a conexao com o servidor MCP cair, abre outro."""
        while True:
            self._caiu.clear()
            cliente = ClienteMcp(
                self._url_mcp,
                mode=PROTOCOLO_MCP,
                client_info=Implementation(name="agente-central-de-salas", version="1.0.0"),
            )
            try:
                async with cliente:
                    self._cliente = cliente
                    self._pronto.set()
                    await self._caiu.wait()
            except Exception as e:  # o transporte do SDK encerra o Client quando a conexao falha
                print(f"agente: cliente MCP encerrado ({type(e).__name__})", file=sys.stderr, flush=True)
            finally:
                self._pronto.clear()
            print("agente: abrindo um novo cliente MCP", file=sys.stderr, flush=True)

    async def _mcp(self) -> ClienteMcp:
        await self._pronto.wait()
        assert self._cliente is not None
        return self._cliente

    def _falha_mcp(self, task: Task, erro: MCPError) -> None:
        if erro.error.code == CONNECTION_CLOSED:
            self._caiu.set()
        task.mudar(FAILED, erro.error.message)

    async def iniciar(self, task: Task, texto: str) -> None:
        task.mudar(WORKING)
        argumentos = formato.ler_pedido(texto)
        if argumentos is None:
            task.mudar(FAILED, f"Pedido fora do formato: {formato.FORMATO_PEDIDO}")
            return
        try:
            cliente = await self._mcp()
            # Descoberta em runtime: a tool precisa estar no tools/list deste servidor.
            ferramentas = await cliente.list_tools(meta=_meta(task))
            if TOOL_RESERVA not in {t.name for t in ferramentas.tools}:
                task.mudar(FAILED, f"O servidor MCP nao oferece a tool {TOOL_RESERVA}")
                return
            task.politica = await self._versao_da_politica(cliente, task)
            resultado = await cliente.session.call_tool(
                TOOL_RESERVA, argumentos, meta=_meta(task), allow_input_required=True
            )
        except MCPError as e:
            self._falha_mcp(task, e)
            return
        self._aplicar(task, resultado, argumentos)

    async def continuar(self, task: Task, texto: str) -> None:
        pausa = task.pausa
        assert pausa is not None
        escolha = formato.ler_escolha(texto)
        if escolha is None or (escolha != formato.RECUSAR and escolha not in pausa.alternativas):
            # Escolha fora do enum: a Task continua pausada e repete a pergunta.
            task.mudar(INPUT_REQUIRED, linha_alternativas(pausa.alternativas))
            return

        if escolha == formato.RECUSAR:
            resposta = ElicitResult(action="decline")
        else:
            resposta = ElicitResult(action="accept", content={"sala": escolha})
        task.mudar(WORKING)
        task.pausa = None
        try:
            cliente = await self._mcp()
            # Retry: novo tools/call (o SDK gera um id JSON-RPC novo), com a mesma chave
            # do inputRequests e o requestState devolvido exatamente como chegou.
            resultado = await cliente.session.call_tool(
                TOOL_RESERVA,
                pausa.argumentos,
                input_responses={pausa.chave: resposta},
                request_state=pausa.request_state,
                meta=_meta(task),
                allow_input_required=True,
            )
        except MCPError as e:
            self._falha_mcp(task, e)
            return
        self._aplicar(task, resultado, pausa.argumentos)

    async def _versao_da_politica(self, cliente: ClienteMcp, task: Task) -> str:
        lido = await cliente.read_resource(URI_POLITICA, meta=_meta(task))
        texto = next((c.text for c in lido.contents if hasattr(c, "text")), "")
        primeira = texto.splitlines()[0] if texto else ""
        return primeira.removeprefix("versao:").strip()

    def _aplicar(self, task: Task, resultado: CallToolResult | InputRequiredResult, argumentos: dict[str, str]) -> None:
        if isinstance(resultado, InputRequiredResult):
            self._pausar(task, resultado, argumentos)
            return
        if resultado.is_error:
            task.mudar(FAILED, _texto(resultado))
            return
        dados = resultado.structured_content or {}
        if dados.get("reservado") is False:
            task.mudar(CANCELED, "Reserva nao realizada: as alternativas foram recusadas.")
            return
        reserva = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": task.politica,
        }
        task.artifacts.append({"artifactId": novo_id("art"), "name": "reserva", "parts": [{"text": json.dumps(reserva)}]})
        task.mudar(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")

    def _pausar(self, task: Task, resultado: InputRequiredResult, argumentos: dict[str, str]) -> None:
        pedidos = resultado.input_requests or {}
        if len(pedidos) != 1 or not resultado.request_state:
            task.mudar(FAILED, "O servidor MCP pediu informacao em um formato que o agente nao atende")
            return
        chave, pergunta = next(iter(pedidos.items()))
        if not isinstance(pergunta, ElicitRequest) or getattr(pergunta.params, "mode", "form") != "form":
            task.mudar(FAILED, "O servidor MCP pediu informacao em um formato que o agente nao atende")
            return
        campo = (pergunta.params.requested_schema.get("properties") or {}).get("sala") or {}
        alternativas = list(campo.get("enum") or ([campo["const"]] if "const" in campo else []))
        task.pausa = Pausa(
            request_state=resultado.request_state,
            chave=chave,
            alternativas=alternativas,
            argumentos=argumentos,
        )
        task.mudar(INPUT_REQUIRED, linha_alternativas(alternativas))
