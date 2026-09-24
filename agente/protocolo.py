from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

PADRAO_PEDIDO = re.compile(
    r"^reservar\s+sala=(?P<sala>\S+)\s+inicio=(?P<inicio>\S+)\s+fim=(?P<fim>\S+)\s+responsavel=(?P<responsavel>.+?)\s*$"
)
PADRAO_ESCOLHA = re.compile(r"^escolha=(?P<valor>\S+)\s*$")
PADRAO_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-(?P<trace>[0-9a-f]{32})-[0-9a-f]{16}-(?P<flags>[0-9a-f]{2})$")

RECUSAR = "recusar"


def interpretar_pedido(texto: str) -> dict[str, str] | None:
    casamento = PADRAO_PEDIDO.match(texto.strip())
    return casamento.groupdict() if casamento else None


def interpretar_escolha(texto: str) -> str | None:
    casamento = PADRAO_ESCOLHA.match(texto.strip())
    return casamento.group("valor") if casamento else None


@dataclass(frozen=True)
class Rastro:
    """Trace context W3C: o trace-id atravessa a ponte, o span-id e novo a cada request MCP."""

    trace_id: str
    flags: str = "01"

    @classmethod
    def do_header(cls, valor: str | None) -> Rastro | None:
        casamento = PADRAO_TRACEPARENT.match((valor or "").strip().lower())
        if not casamento or casamento.group("trace") == "0" * 32:
            return None
        return cls(trace_id=casamento.group("trace"), flags=casamento.group("flags"))

    @classmethod
    def novo(cls) -> Rastro:
        return cls(trace_id=secrets.token_hex(16))

    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{secrets.token_hex(8)}-{self.flags}"
