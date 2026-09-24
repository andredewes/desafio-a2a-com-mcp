from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

DADOS = Path(__file__).resolve().parent.parent / "dados"

FUSO_SAO_PAULO = timezone(timedelta(hours=-3))
ABERTURA = time(8, 0)
FECHAMENTO = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


class ErroDeRegra(Exception):
    pass


@dataclass
class Analise:
    sala: dict
    inicio: datetime
    fim: datetime
    conflitos: list[dict] = field(default_factory=list)
    alternativas: list[str] = field(default_factory=list)

    @property
    def livre(self) -> bool:
        return not self.conflitos


def _ler_json(nome: str) -> list[dict]:
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


SALAS: list[dict] = _ler_json("salas.json")
SALAS_POR_ID: dict[str, dict] = {s["id"]: s for s in SALAS}
RESERVAS: list[dict] = _ler_json("reservas.json")
POLITICA_TEXTO: str = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO: str = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()


def _instante(valor: str) -> datetime:
    try:
        instante = datetime.fromisoformat(valor)
    except (TypeError, ValueError) as e:
        raise ErroDeRegra(f"Data invalida: {valor}") from e
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=FUSO_SAO_PAULO)
    return instante.astimezone(FUSO_SAO_PAULO)


def _sobrepoe(inicio: datetime, fim: datetime, reserva: dict) -> bool:
    return inicio < _instante(reserva["fim"]) and _instante(reserva["inicio"]) < fim


def _conflitos(sala_id: str, inicio: datetime, fim: datetime) -> list[dict]:
    return [r for r in RESERVAS if r["sala"] == sala_id and _sobrepoe(inicio, fim, r)]


def analisar(sala_id: str, inicio_iso: str, fim_iso: str) -> Analise:
    sala = SALAS_POR_ID.get(sala_id)
    if sala is None:
        raise ErroDeRegra(f"Sala inexistente: {sala_id}")
    inicio, fim = _instante(inicio_iso), _instante(fim_iso)
    if fim <= inicio:
        raise ErroDeRegra(ERRO_INTERVALO)
    if inicio.date() != fim.date() or inicio.time() < ABERTURA or fim.time() > FECHAMENTO:
        raise ErroDeRegra(ERRO_JANELA)
    if fim - inicio > DURACAO_MAXIMA:
        raise ErroDeRegra(ERRO_DURACAO)

    analise = Analise(sala=sala, inicio=inicio, fim=fim, conflitos=_conflitos(sala_id, inicio, fim))
    if not analise.livre:
        candidatas = [
            s
            for s in SALAS
            if s["id"] != sala_id and s["capacidade"] >= sala["capacidade"] and not _conflitos(s["id"], inicio, fim)
        ]
        candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
        analise.alternativas = [s["id"] for s in candidatas[:MAX_ALTERNATIVAS]]
    return analise


def criar_reserva(sala_id: str, inicio_iso: str, fim_iso: str, responsavel: str) -> dict:
    analise = analisar(sala_id, inicio_iso, fim_iso)
    if not analise.livre:
        raise ErroDeRegra(f"Sala ocupada no intervalo: {sala_id}")
    reserva = {
        "id": f"res-{len(RESERVAS) + 1:04d}",
        "sala": sala_id,
        "inicio": inicio_iso,
        "fim": fim_iso,
        "responsavel": responsavel,
    }
    RESERVAS.append(reserva)
    return reserva
