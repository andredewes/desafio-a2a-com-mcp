from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from mcp import Client, ClientSession
from mcp.client import ClientRequestContext
from mcp.types import (
    CallToolResult,
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequestParams,
    ElicitResult,
    FormElicitationCapability,
    InputRequiredResult,
    InputResponses,
    Implementation,
    TextResourceContents,
)

from protocolo import Rastro

PROTOCOLO_MCP = "2026-07-28"
URI_POLITICA = "politica://uso"


async def _nunca_responder_sozinho(context: ClientRequestContext, params: ElicitRequestParams) -> ElicitResult:
    # Registrar o callback e o que faz o SDK declarar elicitation no _meta.
    # Ele nunca e chamado, porque o agente usa allow_input_required=True e devolve a
    # pergunta ao cliente A2A em vez de responde-la aqui.
    raise RuntimeError("a elicitation deve voltar para o cliente A2A, nao ser respondida pelo agente")


class _ClienteSoFormulario(Client):
    """O SDK 2.2.0 anuncia form e url juntos; o agente so repassa formularios, entao declara apenas form."""

    async def _build_session(self, exit_stack: AsyncExitStack) -> ClientSession:
        sessao = await super()._build_session(exit_stack)
        original = sessao._build_capabilities

        def so_formulario(version: str) -> ClientCapabilities:
            capacidades = original(version)
            if capacidades.elicitation is not None:
                capacidades = capacidades.model_copy(
                    update={"elicitation": ElicitationCapability(form=FormElicitationCapability())}
                )
            return capacidades

        sessao._build_capabilities = so_formulario  # type: ignore[method-assign]
        return sessao


class HostMCP:
    """Cliente MCP do agente: descobre tools, le a politica e chama reservar_sala sem fechar o MRTR sozinho."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._pilha: AsyncExitStack | None = None
        self._cliente: Client | None = None
        self._trava = asyncio.Lock()

    async def _obter(self) -> Client:
        async with self._trava:
            if self._cliente is None:
                pilha = AsyncExitStack()
                cliente = _ClienteSoFormulario(
                    self._url,
                    mode=PROTOCOLO_MCP,
                    elicitation_callback=_nunca_responder_sozinho,
                    client_info=Implementation(name="agente-central-de-salas", version="1.0.0"),
                    cache=None,
                )
                await pilha.enter_async_context(cliente)
                self._pilha, self._cliente = pilha, cliente
            return self._cliente

    async def fechar(self) -> None:
        if self._pilha is not None:
            await self._pilha.aclose()
        self._pilha = self._cliente = None

    @staticmethod
    def _meta(rastro: Rastro) -> dict[str, Any]:
        return {"traceparent": rastro.traceparent()}

    async def descobrir_tools(self, rastro: Rastro) -> set[str]:
        cliente = await self._obter()
        resultado = await cliente.list_tools(meta=self._meta(rastro))
        return {tool.name for tool in resultado.tools}

    async def versao_da_politica(self, rastro: Rastro) -> str:
        cliente = await self._obter()
        resultado = await cliente.read_resource(URI_POLITICA, meta=self._meta(rastro))
        for conteudo in resultado.contents:
            if isinstance(conteudo, TextResourceContents):
                primeira_linha = conteudo.text.splitlines()[0] if conteudo.text else ""
                chave, _, valor = primeira_linha.partition(":")
                if chave.strip() == "versao" and valor.strip():
                    return valor.strip()
        raise ValueError(f"o resource {URI_POLITICA} nao declara a versao na primeira linha")

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        rastro: Rastro,
        input_responses: InputResponses | None = None,
        request_state: str | None = None,
    ) -> CallToolResult | InputRequiredResult:
        cliente = await self._obter()
        return await cliente.session.call_tool(
            nome,
            argumentos,
            input_responses=input_responses,
            request_state=request_state,
            meta=self._meta(rastro),
            allow_input_required=True,
        )
