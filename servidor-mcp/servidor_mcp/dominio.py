"""Salas, reservas e politica de uso, carregadas de dados/ na subida do processo.

As reservas vivem em memoria: as criadas durante a execucao valem para as
consultas seguintes do mesmo processo e somem no restart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

DADOS = Path(__file__).resolve().parents[2] / "dados"

SAO_PAULO = timezone(timedelta(hours=-3))
ABERTURA = time(8, 0)
FECHAMENTO = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


class RegraVioladaError(Exception):
    """Pedido recusado por regra de sala ou de politica. A mensagem vai para o cliente."""


@dataclass(frozen=True)
class Sala:
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


@dataclass(frozen=True)
class Reserva:
    id: str
    sala: str
    inicio: str
    fim: str
    responsavel: str

    def intervalo(self) -> tuple[datetime, datetime]:
        return _data_hora(self.inicio), _data_hora(self.fim)


def _data_hora(valor: str) -> datetime:
    try:
        instante = datetime.fromisoformat(valor)
    except ValueError:
        raise RegraVioladaError(f"Data invalida: {valor} nao esta em ISO 8601") from None
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=SAO_PAULO)
    return instante


SALAS: dict[str, Sala] = {s["id"]: Sala(**s) for s in json.loads((DADOS / "salas.json").read_text())}
POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text()
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].removeprefix("versao:").strip()

_reservas: list[Reserva] = [Reserva(**r) for r in json.loads((DADOS / "reservas.json").read_text())]


def validar(sala: str, inicio: str, fim: str) -> tuple[Sala, datetime, datetime]:
    """Aplica as regras de sala e de politica. Levanta RegraVioladaError com a mensagem exata."""
    if sala not in SALAS:
        raise RegraVioladaError(f"Sala inexistente: {sala}")
    comeco, termino = _data_hora(inicio), _data_hora(fim)
    if termino <= comeco:
        raise RegraVioladaError(ERRO_INTERVALO)
    local_inicio, local_fim = comeco.astimezone(SAO_PAULO), termino.astimezone(SAO_PAULO)
    dentro = (
        local_inicio.date() == local_fim.date()
        and local_inicio.time() >= ABERTURA
        and local_fim.time() <= FECHAMENTO
    )
    if not dentro:
        raise RegraVioladaError(ERRO_JANELA)
    if termino - comeco > DURACAO_MAXIMA:
        raise RegraVioladaError(ERRO_DURACAO)
    return SALAS[sala], comeco, termino


def conflitos(sala: str, comeco: datetime, termino: datetime) -> list[Reserva]:
    resultado = []
    for reserva in _reservas:
        if reserva.sala != sala:
            continue
        outro_inicio, outro_fim = reserva.intervalo()
        if comeco < outro_fim and outro_inicio < termino:
            resultado.append(reserva)
    return resultado


def alternativas(sala: Sala, comeco: datetime, termino: datetime) -> list[str]:
    """Salas livres no intervalo com capacidade >= a pedida, por capacidade e id, no maximo tres."""
    candidatas = [
        s
        for s in SALAS.values()
        if s.id != sala.id and s.capacidade >= sala.capacidade and not conflitos(s.id, comeco, termino)
    ]
    candidatas.sort(key=lambda s: (s.capacidade, s.id))
    return [s.id for s in candidatas[:MAX_ALTERNATIVAS]]


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> Reserva:
    numero = max(int(r.id.removeprefix("res-")) for r in _reservas) + 1 if _reservas else 1
    reserva = Reserva(id=f"res-{numero:04d}", sala=sala, inicio=inicio, fim=fim, responsavel=responsavel)
    _reservas.append(reserva)
    return reserva
