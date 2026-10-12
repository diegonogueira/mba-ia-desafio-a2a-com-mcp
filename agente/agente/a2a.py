"""O agente por fora: Agent Card v1.0 e o endpoint JSON-RPC do A2A (SendMessage, GetTask)."""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import sys
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agente.ponte import Ponte
from agente.tarefas import INPUT_REQUIRED, Tarefas

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004

_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-(?P<trace>[0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")


def agent_card(url_a2a: str) -> dict[str, Any]:
    return {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
        "version": "1.0.0",
        "supportedInterfaces": [{"url": url_a2a, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            }
        ],
    }


class ErroRpc(Exception):
    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


def _trace_id(cabecalho: str | None) -> str:
    """trace-id do traceparent recebido; sem header valido, a Task comeca um trace novo."""
    casou = _TRACEPARENT.match((cabecalho or "").strip().lower())
    if casou and casou.group("trace") != "0" * 32:
        return casou.group("trace")
    return secrets.token_hex(16)


def criar_app(url_mcp: str, url_a2a: str) -> Starlette:
    tarefas = Tarefas()
    ponte = Ponte(url_mcp)
    card = agent_card(url_a2a)

    async def send_message(params: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
        mensagem = params.get("message")
        if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
            raise ErroRpc(INVALID_PARAMS, "params.message com parts e obrigatorio")
        texto = " ".join(p.get("text", "") for p in mensagem["parts"] if isinstance(p, dict)).strip()

        task_id = mensagem.get("taskId")
        if task_id is None:
            task = tarefas.abrir(mensagem.get("contextId"), _trace_id(traceparent))
            async with task.trava:
                task.historico.append({**mensagem, "taskId": task.id, "contextId": task.context_id})
                await ponte.iniciar(task, texto)
                return {"task": task.para_json()}

        task = tarefas.buscar(task_id)
        if task is None:
            raise ErroRpc(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
        async with task.trava:
            if task.terminal:
                raise ErroRpc(UNSUPPORTED_OPERATION, f"Task {task.id} esta em estado terminal ({task.estado})")
            if task.estado != INPUT_REQUIRED:
                raise ErroRpc(UNSUPPORTED_OPERATION, f"Task {task.id} nao esta aguardando resposta ({task.estado})")
            task.historico.append({**mensagem, "contextId": task.context_id})
            await ponte.continuar(task, texto)
            return {"task": task.para_json()}

    async def get_task(params: dict[str, Any]) -> dict[str, Any]:
        task = tarefas.buscar(str(params.get("id")))
        if task is None:
            raise ErroRpc(TASK_NOT_FOUND, f"Task nao encontrada: {params.get('id')}")
        dados = task.para_json()
        limite = params.get("historyLength")
        if isinstance(limite, int) and limite >= 0:
            dados["history"] = dados["history"][-limite:] if limite else []
        return {"task": dados}

    async def rpc(request: Request) -> JSONResponse:
        try:
            corpo = json.loads(await request.body())
        except ValueError:
            return _erro(None, PARSE_ERROR, "Parse error")
        if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
            return _erro(corpo.get("id") if isinstance(corpo, dict) else None, INVALID_REQUEST, "Invalid Request")
        id_rpc, metodo = corpo.get("id"), corpo["method"]
        params = corpo.get("params") if isinstance(corpo.get("params"), dict) else {}
        print(f"a2a request method={metodo} id={json.dumps(id_rpc)} traceparent={request.headers.get('traceparent', '-')}",
              file=sys.stderr, flush=True)
        try:
            if metodo == "SendMessage":
                resultado = await send_message(params, request.headers.get("traceparent"))
            elif metodo == "GetTask":
                resultado = await get_task(params)
            else:
                raise ErroRpc(METHOD_NOT_FOUND, f"Method not found: {metodo}")
        except ErroRpc as e:
            return _erro(id_rpc, e.codigo, e.mensagem)
        return JSONResponse({"jsonrpc": "2.0", "id": id_rpc, "result": resultado})

    async def cartao(_: Request) -> JSONResponse:
        return JSONResponse(card)

    @contextlib.asynccontextmanager
    async def ciclo(_: Starlette):
        async with ponte:
            yield

    return Starlette(
        routes=[
            Route("/.well-known/agent-card.json", cartao, methods=["GET"]),
            Route("/a2a", rpc, methods=["POST"]),
        ],
        lifespan=ciclo,
    )


def _erro(id_rpc: Any, codigo: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_rpc, "error": {"code": codigo, "message": mensagem}})
