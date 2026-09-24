from __future__ import annotations

import os
import sys
from functools import lru_cache
from typing import Annotated, Literal

import uvicorn
from pydantic import BaseModel, Field, create_model

from mcp.server.elicitation import CancelledElicitation, DeclinedElicitation, ElicitationResult
from mcp.server.mcpserver import Elicit, MCPServer, RequestStateSecurity, Resolve
from mcp.server.mcpserver.exceptions import ToolError

import dominio
from dominio import ERRO_SEM_ALTERNATIVA, ErroDeRegra
from registro import RegistroDeRequests

TTL_REQUEST_STATE_SEGUNDOS = 600
MENSAGEM_CONFLITO = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."
MOTIVO_RECUSA = "Usuario recusou as salas alternativas"


def carregar_segredo() -> bytes:
    bruto = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    if not bruto:
        sys.exit("REQUEST_STATE_SECRET nao definido. Gere com: python3 -c \"import secrets; print(secrets.token_hex(32))\"")
    try:
        segredo = bytes.fromhex(bruto)
    except ValueError:
        segredo = bruto.encode("utf-8")
    if len(segredo) < 32:
        sys.exit("REQUEST_STATE_SECRET precisa ter no minimo 32 bytes")
    return segredo


mcp = MCPServer(
    "central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[carregar_segredo()], ttl=TTL_REQUEST_STATE_SEGUNDOS),
)


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


class EscolhaDeSala(BaseModel):
    sala: str | None = None


@lru_cache(maxsize=None)
def schema_da_escolha(alternativas: tuple[str, ...]) -> type[EscolhaDeSala]:
    return create_model(
        "EscolhaDeSala",
        __base__=EscolhaDeSala,
        sala=(Literal[alternativas], Field(title="Sala", description="Sala alternativa escolhida")),
    )


async def escolha_de_sala(sala: str, inicio: str, fim: str) -> EscolhaDeSala | Elicit[EscolhaDeSala]:
    try:
        analise = dominio.analisar(sala, inicio, fim)
    except ErroDeRegra:
        return EscolhaDeSala()
    if analise.livre or not analise.alternativas:
        return EscolhaDeSala()
    return Elicit(MENSAGEM_CONFLITO, schema_da_escolha(tuple(analise.alternativas)))


def _analisar_ou_falhar(sala: str, inicio: str, fim: str) -> dominio.Analise:
    try:
        return dominio.analisar(sala, inicio, fim)
    except ErroDeRegra as e:
        raise ToolError(str(e)) from e


@mcp.tool()
async def listar_salas() -> ListaDeSalas:
    """Lista todas as salas com capacidade e recursos."""
    return ListaDeSalas(salas=[SalaOut(**s) for s in dominio.SALAS])


@mcp.tool()
async def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    """Diz se uma sala esta livre no intervalo, e quais reservas conflitam."""
    analise = _analisar_ou_falhar(sala, inicio, fim)
    conflitos = [ConflitoOut(id=r["id"], inicio=r["inicio"], fim=r["fim"], responsavel=r["responsavel"]) for r in analise.conflitos]
    return Disponibilidade(sala=sala, livre=analise.livre, conflitos=conflitos)


@mcp.tool()
async def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[EscolhaDeSala], Resolve(escolha_de_sala)],
) -> ReservaOut:
    """Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar."""
    if isinstance(escolha, DeclinedElicitation | CancelledElicitation):
        return ReservaOut(
            reservado=False,
            sala=sala,
            inicio=inicio,
            fim=fim,
            responsavel=responsavel,
            politica=dominio.POLITICA_VERSAO,
            motivo=MOTIVO_RECUSA,
        )

    destino = escolha.data.sala
    if destino is None:
        analise = _analisar_ou_falhar(sala, inicio, fim)
        if not analise.livre:
            raise ToolError(ERRO_SEM_ALTERNATIVA)
        destino = sala

    try:
        reserva = dominio.criar_reserva(destino, inicio, fim, responsavel)
    except ErroDeRegra as e:
        raise ToolError(str(e)) from e
    return ReservaOut(
        reserva=reserva["id"],
        reservado=True,
        sala=reserva["sala"],
        inicio=reserva["inicio"],
        fim=reserva["fim"],
        responsavel=reserva["responsavel"],
        politica=dominio.POLITICA_VERSAO,
    )


@mcp.resource("politica://uso", name="politica-de-uso", description="Politica de uso das salas", mime_type="text/markdown")
def politica_de_uso() -> str:
    return dominio.POLITICA_TEXTO


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    porta = int(os.environ.get("MCP_PORT", "7301"))
    app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True, host=host)
    print(f"[mcp] central-de-salas ouvindo em http://{host}:{porta}/mcp", file=sys.stderr, flush=True)
    uvicorn.run(RegistroDeRequests(app), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
