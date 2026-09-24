from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import DefaultServerCallContextBuilder, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentProvider, AgentSkill
from a2a.utils.constants import VERSION_HEADER
from starlette.applications import Starlette
from starlette.requests import Request

from host_mcp import HostMCP
from ponte import ExecutorDeReservas

HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://{HOST}:{PORTA}").rstrip("/")
URL_MCP = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
CAMINHO_A2A = "/a2a"
VERSAO_A2A = "1.0"


class ContextoComVersaoPadrao(DefaultServerCallContextBuilder):
    """O a2a-sdk trata request sem header A2A-Version como 0.3 e o recusa no handler 1.0.

    Este agente so publica a interface 1.0 no card, entao um request sem o header
    e interpretado como 1.0. Um header explicito continua sendo validado pelo SDK.
    """

    def build(self, request: Request) -> ServerCallContext:
        contexto = super().build(request)
        cabecalhos = contexto.state.setdefault("headers", {})
        if not (cabecalhos.get(VERSION_HEADER) or cabecalhos.get(VERSION_HEADER.lower())):
            cabecalhos[VERSION_HEADER.lower()] = VERSAO_A2A
        return contexto


def montar_card() -> AgentCard:
    return AgentCard(
        name="Central de Salas",
        description="Reserva salas de reuniao da Hill Valley Tech.",
        provider=AgentProvider(organization="Hill Valley Tech", url="https://hillvalley.example"),
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(url=f"{URL_PUBLICA}{CAMINHO_A2A}", protocol_binding="JSONRPC", protocol_version="1.0"),
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="reservar-sala",
                name="Reservar sala",
                description="Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                tags=["salas", "agenda"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
                examples=[
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            )
        ],
    )


def montar_app() -> Starlette:
    host_mcp = HostMCP(URL_MCP)
    card = montar_card()
    handler = DefaultRequestHandler(
        agent_executor=ExecutorDeReservas(host_mcp),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )

    @asynccontextmanager
    async def ciclo_de_vida(_app: Starlette):
        yield
        await host_mcp.fechar()

    rotas = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(handler, CAMINHO_A2A, context_builder=ContextoComVersaoPadrao()),
    ]
    return Starlette(routes=rotas, lifespan=ciclo_de_vida)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(asctime)s [agente] %(message)s")
    print(f"[agente] A2A em {URL_PUBLICA}{CAMINHO_A2A}, card em {URL_PUBLICA}/.well-known/agent-card.json, MCP em {URL_MCP}",
          file=sys.stderr, flush=True)
    uvicorn.run(montar_app(), host=HOST, port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
