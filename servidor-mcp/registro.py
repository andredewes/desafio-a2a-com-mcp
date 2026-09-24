from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CABECALHOS_ESPELHADOS = ("mcp-protocol-version", "mcp-method", "mcp-name")


def _log(texto: str) -> None:
    print(f"{datetime.now().isoformat(timespec='milliseconds')} [mcp] {texto}", file=sys.stderr, flush=True)


def _descrever(mensagem: Any) -> str:
    if not isinstance(mensagem, dict):
        return "corpo=nao-json"
    params = mensagem.get("params") if isinstance(mensagem.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    partes = [f"method={mensagem.get('method')}", f"id={json.dumps(mensagem.get('id'))}"]
    alvo = params.get("name") or params.get("uri")
    if alvo:
        partes.append(f"name={alvo}")
    if "requestState" in params:
        partes.append("retry=sim")
    partes.append(f"traceparent={meta.get('traceparent', '-')}")
    capacidades = meta.get("io.modelcontextprotocol/clientCapabilities")
    if capacidades is not None:
        partes.append(f"clientCapabilities={json.dumps(capacidades, separators=(',', ':'))}")
    return " ".join(partes)


class RegistroDeRequests:
    """Registra no stderr metodo, id e traceparent de cada request JSON-RPC recebido."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        pedacos: list[bytes] = []
        while True:
            mensagem = await receive()
            pedacos.append(mensagem.get("body", b""))
            if not mensagem.get("more_body"):
                break
        corpo = b"".join(pedacos)

        try:
            descricao = _descrever(json.loads(corpo))
        except (json.JSONDecodeError, UnicodeDecodeError):
            descricao = "corpo=nao-json"
        cabecalhos = {
            nome.decode("latin-1"): valor.decode("latin-1")
            for nome, valor in scope.get("headers", [])
            if nome.decode("latin-1") in CABECALHOS_ESPELHADOS
        }
        descricao += f" headers={json.dumps(cabecalhos, separators=(',', ':'))}"

        entregue = False

        async def receive_repetido() -> Message:
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        status = 0

        async def send_com_status(mensagem: Message) -> None:
            nonlocal status
            if mensagem["type"] == "http.response.start":
                status = mensagem["status"]
            await send(mensagem)

        await self.app(scope, receive_repetido, send_com_status)
        _log(f"{descricao} http={status}")
