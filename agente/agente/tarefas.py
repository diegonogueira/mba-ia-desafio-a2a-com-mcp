"""Tasks do A2A em memoria: identidade, estado, historico e artifacts.

A pausa (requestState, chave da elicitation e alternativas) fica num campo
privado da Task e nunca entra na serializacao devolvida ao cliente A2A.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from dataclasses import dataclass, field
from typing import Any

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
TERMINAIS = frozenset({COMPLETED, CANCELED, FAILED})


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


@dataclass
class Pausa:
    """O que o agente guarda entre o input_required do MCP e a continuacao do cliente A2A."""

    request_state: str  # opaco: guardado e ecoado sem abrir
    chave: str  # chave que o servidor atribuiu em inputRequests
    alternativas: list[str]  # enum da elicitation, na ordem recebida
    argumentos: dict[str, str]  # argumentos do tools/call original, reenviados no retry


@dataclass
class Task:
    id: str
    context_id: str
    trace_id: str
    estado: str = SUBMITTED
    mensagem: dict[str, Any] | None = None
    historico: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    politica: str | None = None
    pausa: Pausa | None = None
    trava: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def terminal(self) -> bool:
        return self.estado in TERMINAIS

    def mudar(self, estado: str, texto: str | None = None) -> None:
        if self.terminal:
            raise RuntimeError(f"Task {self.id} ja terminou em {self.estado}")
        self.estado = estado
        self.mensagem = None
        if texto is not None:
            self.mensagem = {
                "messageId": novo_id("msg"),
                "role": "ROLE_AGENT",
                "parts": [{"text": texto}],
                "taskId": self.id,
                "contextId": self.context_id,
            }
            self.historico.append(self.mensagem)
        print(f"a2a task={self.id} estado={estado}", file=sys.stderr, flush=True)

    def para_json(self) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.estado}
        if self.mensagem is not None:
            status["message"] = self.mensagem
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": self.historico,
            "artifacts": self.artifacts,
        }


class Tarefas:
    def __init__(self) -> None:
        self._por_id: dict[str, Task] = {}

    def abrir(self, context_id: str | None, trace_id: str) -> Task:
        task = Task(id=novo_id("task"), context_id=context_id or novo_id("ctx"), trace_id=trace_id)
        self._por_id[task.id] = task
        print(f"a2a task={task.id} estado={task.estado}", file=sys.stderr, flush=True)
        return task

    def buscar(self, task_id: str) -> Task | None:
        return self._por_id.get(task_id)
