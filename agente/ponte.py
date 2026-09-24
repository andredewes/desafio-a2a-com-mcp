from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from a2a.helpers.proto_helpers import new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Message
from mcp import MCPError
from mcp.types import CallToolResult, ElicitRequest, ElicitResult, InputRequiredResult, TextContent

from host_mcp import HostMCP
from protocolo import RECUSAR, Rastro, interpretar_escolha, interpretar_pedido

logger = logging.getLogger("agente.ponte")

TOOL_RESERVA = "reservar_sala"
FORMATO_PEDIDO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
CAMPOS_DO_ARTIFACT = ("reserva", "sala", "inicio", "fim", "responsavel")


@dataclass
class Pausa:
    """O que a Task pausada precisa para refazer o tools/call. Nunca sai do agente."""

    argumentos: dict[str, str]
    chave: str
    request_state: str
    alternativas: list[str]
    politica: str
    rastro: Rastro

    def linha_de_alternativas(self) -> str:
        return "alternativas: " + ", ".join(self.alternativas)


class FalhaDaPonte(Exception):
    pass


def _alternativas_da_elicitation(pedido: Any) -> list[str]:
    if not isinstance(pedido, ElicitRequest) or getattr(pedido.params, "mode", "form") != "form":
        raise FalhaDaPonte("O servidor MCP pediu um input que o agente nao sabe repassar")
    propriedade = (pedido.params.requested_schema.get("properties") or {}).get("sala") or {}
    alternativas = propriedade.get("enum") or ([propriedade["const"]] if "const" in propriedade else [])
    if not alternativas:
        raise FalhaDaPonte("A elicitation do servidor MCP nao trouxe alternativas de sala")
    return [str(a) for a in alternativas]


def _texto(resultado: CallToolResult) -> str:
    return " ".join(bloco.text for bloco in resultado.content if isinstance(bloco, TextContent)).strip()


class ExecutorDeReservas(AgentExecutor):
    def __init__(self, host: HostMCP) -> None:
        self._host = host
        self._pausas: dict[str, Pausa] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = context.current_task.context_id if context.current_task else context.context_id
        assert task_id and context_id
        updater = TaskUpdater(event_queue, task_id, context_id)

        if context.current_task is None:
            await event_queue.enqueue_event(new_task_from_user_message(context.message))

        cabecalhos = context.call_context.state.get("headers") or {}
        rastro_do_request = Rastro.do_header(cabecalhos.get("traceparent"))
        texto = context.get_user_input()

        try:
            pausa = self._pausas.get(task_id)
            if pausa is None:
                await self._iniciar(updater, texto, rastro_do_request or Rastro.novo())
            else:
                await self._retomar(updater, pausa, texto, rastro_do_request or pausa.rastro)
        except FalhaDaPonte as e:
            await self._falhar(updater, str(e))
        except MCPError as e:
            await self._falhar(updater, f"Erro do servidor MCP ({e.error.code}): {e.error.message}")
        except Exception as e:
            logger.exception("falha inesperada na Task %s", task_id)
            await self._falhar(updater, f"Falha ao falar com o servidor MCP: {type(e).__name__}")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = context.current_task.context_id if context.current_task else context.context_id
        assert task_id and context_id
        self._pausas.pop(task_id, None)
        await TaskUpdater(event_queue, task_id, context_id).cancel()

    async def _iniciar(self, updater: TaskUpdater, texto: str, rastro: Rastro) -> None:
        pedido = interpretar_pedido(texto)
        if pedido is None:
            raise FalhaDaPonte(f"Pedido fora do formato: {FORMATO_PEDIDO}")

        await updater.start_work()
        tools = await self._host.descobrir_tools(rastro)
        if TOOL_RESERVA not in tools:
            raise FalhaDaPonte(f"O servidor MCP nao oferece a tool {TOOL_RESERVA}")
        politica = await self._host.versao_da_politica(rastro)

        resultado = await self._host.chamar_tool(TOOL_RESERVA, pedido, rastro)
        await self._concluir_ou_pausar(updater, resultado, pedido, politica, rastro)

    async def _retomar(self, updater: TaskUpdater, pausa: Pausa, texto: str, rastro: Rastro) -> None:
        escolha = interpretar_escolha(texto)
        if escolha is None or (escolha != RECUSAR and escolha not in pausa.alternativas):
            await self._pausar(updater, pausa)
            return

        self._pausas.pop(updater.task_id, None)
        await updater.start_work()
        if escolha == RECUSAR:
            resposta = ElicitResult(action="decline")
        else:
            resposta = ElicitResult(action="accept", content={"sala": escolha})

        resultado = await self._host.chamar_tool(
            TOOL_RESERVA,
            pausa.argumentos,
            rastro,
            input_responses={pausa.chave: resposta},
            request_state=pausa.request_state,
        )
        await self._concluir_ou_pausar(updater, resultado, pausa.argumentos, pausa.politica, rastro)

    async def _concluir_ou_pausar(
        self,
        updater: TaskUpdater,
        resultado: CallToolResult | InputRequiredResult,
        argumentos: dict[str, str],
        politica: str,
        rastro: Rastro,
    ) -> None:
        if isinstance(resultado, InputRequiredResult):
            pedidos = resultado.input_requests or {}
            if len(pedidos) != 1 or not resultado.request_state:
                raise FalhaDaPonte("O servidor MCP pediu input em um formato que o agente nao sabe repassar")
            chave, pedido = next(iter(pedidos.items()))
            pausa = Pausa(
                argumentos=argumentos,
                chave=chave,
                request_state=resultado.request_state,
                alternativas=_alternativas_da_elicitation(pedido),
                politica=politica,
                rastro=rastro,
            )
            self._pausas[updater.task_id] = pausa
            await self._pausar(updater, pausa)
            return

        if resultado.is_error:
            await self._falhar(updater, _texto(resultado) or "A tool devolveu erro sem mensagem")
            return

        dados = resultado.structured_content or {}
        if dados.get("reservado") is False:
            motivo = dados.get("motivo") or "reserva recusada"
            await self._encerrar(updater.cancel, updater, f"Reserva nao realizada: {motivo}")
            return

        reserva = {campo: dados.get(campo) for campo in CAMPOS_DO_ARTIFACT}
        reserva["politica"] = politica
        await updater.add_artifact([new_text_part(json.dumps(reserva, ensure_ascii=False))], name="reserva")
        await self._encerrar(updater.complete, updater, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")

    @staticmethod
    async def _pausar(updater: TaskUpdater, pausa: Pausa) -> None:
        await updater.requires_input(updater.new_agent_message([new_text_part(pausa.linha_de_alternativas())]))

    @staticmethod
    async def _encerrar(
        estado_terminal: Callable[[Message], Awaitable[None]], updater: TaskUpdater, texto: str
    ) -> None:
        # O a2a-sdk so copia status.message para o history na troca seguinte de status. Estado
        # terminal nao tem troca seguinte, entao a mensagem passa antes por WORKING para ficar no historico.
        mensagem = updater.new_agent_message([new_text_part(texto)])
        await updater.start_work(mensagem)
        await estado_terminal(mensagem)

    @classmethod
    async def _falhar(cls, updater: TaskUpdater, mensagem: str) -> None:
        await cls._encerrar(updater.failed, updater, mensagem)
