# A Ponte: um agente A2A com MCP por dentro

Central de Salas da Hill Valley Tech em dois processos Python:

* [servidor-mcp/](servidor-mcp/) é um servidor MCP Streamable HTTP em `http://127.0.0.1:7301/mcp`. Ele expõe as tools `listar_salas`, `consultar_disponibilidade` e `reservar_sala` e o resource `politica://uso`, e implementa o ciclo completo de MRTR.
* [agente/](agente/) é um host MCP por dentro e um servidor A2A v1.0 JSON-RPC por fora, em `http://127.0.0.1:7300/a2a`, com o card em `/.well-known/agent-card.json`. Quando o servidor MCP devolve `input_required`, o agente pausa a Task em `TASK_STATE_INPUT_REQUIRED` e retoma a mesma chamada quando o cliente A2A responde.

Stack: Python 3.10+, SDK oficial `mcp` 2.2.0 (spec `2026-07-28`), SDK oficial `a2a-sdk` 1.1.2 e `uvicorn`. As versões diretas estão fixadas no `pyproject.toml` de cada processo, e a árvore completa está no `requirements.txt` ao lado.

## Como rodar

Você precisa de Python 3.10 ou superior e de [uv](https://docs.astral.sh/uv/). Sem uv, use a alternativa com `pip` mais abaixo.

### 1. Clone e gere a chave do `requestState`

A chave vem da variável `REQUEST_STATE_SECRET`, com no mínimo 32 bytes. O servidor aceita a chave em hexadecimal (64 caracteres de `token_hex(32)`) e se recusa a subir sem ela. Gere a sua e exporte no terminal do servidor MCP. O valor nunca vai para o repositório.

```bash
git clone https://github.com/andredewes/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

No PowerShell:

```powershell
$env:REQUEST_STATE_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
```

> [!IMPORTANT]
> Para o `requestState` sobreviver a um restart do servidor MCP, suba o processo de novo no mesmo terminal, com a mesma chave exportada. Uma chave nova invalida os `requestState` emitidos antes, e o retry é rejeitado com `-32602`.

### 2. Terminal 1: servidor MCP

```bash
cd servidor-mcp
uv sync
uv run python servidor.py
```

O stderr mostra uma linha por request, com método, id JSON-RPC, nome da tool ou do resource, `retry=sim` quando o request traz `requestState`, o `traceparent` recebido, as `clientCapabilities` declaradas e o status HTTP:

```text
[mcp] method=tools/list id=4 traceparent=00-d2f8...de-2dae1b020c9148c1-01 clientCapabilities={"elicitation":{"form":{}}} http=200
[mcp] method=tools/call id=6 name=reservar_sala traceparent=00-d2f8...de-b1b0610ab8a6739f-01 clientCapabilities={"elicitation":{"form":{}}} http=200
[mcp] method=tools/call id=7 name=reservar_sala retry=sim traceparent=00-d2f8...de-945f106a9fef7184-01 clientCapabilities={"elicitation":{"form":{}}} http=200
```

### 3. Terminal 2: agente

O agente não precisa da chave, porque nunca abre o `requestState`.

```bash
cd agente
uv sync
uv run python agente.py
```

### 4. Terminal 3: validador

Na raiz do repositório, com os dois processos no ar:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

> [!NOTE]
> As reservas criadas ficam na memória do servidor MCP. Antes de rodar o validador uma segunda vez, reinicie o servidor MCP para voltar ao estado de `dados/reservas.json`. Se não reiniciar, os pedidos repetidos do validador encontram as reservas da execução anterior.

### Alternativa sem uv

Em cada pasta, `servidor-mcp` e `agente`, crie um ambiente virtual e instale as dependências fixadas:

```bash
python3 -m venv .venv
. .venv/bin/activate            # no Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python servidor.py              # em agente/: python agente.py
```

### Variáveis opcionais

| Variável | Processo | Padrão |
|----------|----------|--------|
| `MCP_HOST`, `MCP_PORT` | servidor MCP | `127.0.0.1`, `7301` |
| `AGENTE_HOST`, `AGENTE_PORT` | agente | `127.0.0.1`, `7300` |
| `AGENTE_URL` | agente, URL publicada no card | `http://127.0.0.1:7300` |
| `MCP_URL` | agente, endpoint do servidor MCP | `http://127.0.0.1:7301/mcp` |

## Onde a ponte acontece

A ponte fica em [agente/ponte.py](agente/ponte.py), no `ExecutorDeReservas`. O agente chama `reservar_sala` por [HostMCP.chamar_tool](agente/host_mcp.py:103) com `allow_input_required=True`, então o SDK devolve o `InputRequiredResult` cru em vez de tentar responder a elicitation sozinho. Em [_concluir_ou_pausar](agente/ponte.py:133), quando o resultado é `InputRequiredResult`, o agente guarda a chave de `inputRequests`, o `requestState` opaco, os argumentos originais e as alternativas do `enum` numa `Pausa` interna. Em seguida chama `updater.requires_input(...)`, e é nesse ponto que o `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED`, com a linha `alternativas: sala-fusca, sala-mirante` na mensagem da Task. Quando chega a continuação com o mesmo `taskId`, [_retomar](agente/ponte.py:111) valida a escolha contra o `enum` e reenvia o `tools/call` com os mesmos argumentos, `inputResponses` com a mesma chave e o `requestState` intacto. É nesse ponto que o `requestState` volta para o servidor, num request com id JSON-RPC novo. Uma escolha fora do `enum` mantém a Task pausada sem chamar o servidor, e `escolha=recusar` responde a elicitation com `action: "decline"`, o que termina a Task em `TASK_STATE_CANCELED`.

Do lado do servidor, quem decide pausar é o resolver [escolha_de_sala](servidor-mcp/servidor.py:92), ligado ao parâmetro `escolha` de [reservar_sala](servidor-mcp/servidor.py:124) por `Resolve(...)`. Ele devolve `Elicit(...)` com o schema de alternativas quando há conflito, e o SDK transforma isso em `resultType: "input_required"` com a chave `__main__:escolha_de_sala`. O agente não contém regra de sala: conflito, política e alternativas vêm do servidor MCP.

## Decisões técnicas

### Proteção do `requestState`

O servidor usa o `RequestStateSecurity` do SDK `mcp` ([servidor.py](servidor-mcp/servidor.py:40)), em vez de um esquema próprio. O SDK sela o estado com AES-256-GCM, usando uma chave derivada por HKDF da `REQUEST_STATE_SECRET`, e o token sai com o prefixo `v1.`. O conteúdo selado fica vinculado ao método, ao nome da tool, a um digest dos argumentos e a um digest da pergunta feita. Por isso um `requestState` com um caractere trocado falha na autenticação do GCM, e um retry com argumentos alterados falha no vínculo. Os dois casos voltam como `-32602 Invalid or expired requestState`, e o stderr registra o motivo (`requestState rejected on tools/call: seal/request binding`).

Como a chave vem do ambiente e o estado viaja inteiro no token, o servidor não guarda nada entre o `input_required` e o retry. Reiniciar o processo com a mesma chave não invalida uma pausa em andamento. Testei esse caso pelo agente: pausei uma Task, reiniciei o servidor MCP e mandei `escolha=sala-mirante`, e a reserva foi concluída no primeiro request do processo novo.

### Validade

O `requestState` vale 600 segundos (`TTL_REQUEST_STATE_SEGUNDOS`). Dez minutos cobrem uma pessoa decidindo entre alternativas sem deixar um token antigo reutilizável por muito tempo. Depois disso, o retry é rejeitado com `-32602` e a Task termina em `TASK_STATE_FAILED` com a mensagem do erro MCP.

### Estado das Tasks

As Tasks A2A ficam no `InMemoryTaskStore` do `a2a-sdk` ([agente.py](agente/agente.py:77)). O que a ponte precisa para retomar (chave, `requestState`, argumentos, alternativas, versão da política e trace-id) fica no dicionário `_pausas` do `ExecutorDeReservas`, indexado pelo `taskId`. Esse dicionário nunca é serializado para o cliente A2A. As mensagens e os artifacts da Task levam só a linha de alternativas e a reserva final, e o validador confirma que nenhuma resposta A2A carrega o `requestState`. Duas Tasks pausadas ao mesmo tempo têm entradas separadas e retomam cada uma com o seu token. O estado vive na memória do agente: reiniciar o agente perde as Tasks, o que está dentro do escopo do desafio.

### Rastreamento e protocolo sem sessão

Cada Task começa com `tools/list`, depois `resources/read` de `politica://uso` (de onde sai a versão da política para o artifact) e só então `tools/call`. Todo request MCP leva no `_meta` a versão do protocolo, o `clientInfo`, as capabilities e um `traceparent`. O trace-id vem do header `traceparent` do request A2A, quando existe, e cada request MCP ganha um span-id novo. O cliente MCP é um objeto vivo reaproveitado entre chamadas, mas o servidor roda com `stateless_http=True` e lê versão e capabilities do `_meta` de cada request.

### Divergências dos SDKs

O `a2a-sdk` 1.1.2 trata um request sem o header `A2A-Version` como versão 0.3 e o recusa no handler 1.0 com `Version mismatch: actual='0.3'`. A evidência está em `a2a/utils/version_validator.py`:

```python
# a2a-sdk 1.1.2, a2a/utils/version_validator.py
If the header is missing or empty, it is interpreted as `constants.PROTOCOL_VERSION_0_3` ('0.3').
```

O validador não envia esse header. Como o card só publica a interface 1.0, o [ContextoComVersaoPadrao](agente/agente.py:29) preenche `A2A-Version: 1.0` quando o header está ausente. Um header explícito continua sendo validado pelo SDK, e o protocolo segue inteiro com o SDK.

O SDK `mcp` 2.2.0 anuncia `{"elicitation": {"form": {}, "url": {}}}` sempre que existe um callback de elicitation, e o método que monta esse anúncio é fixo em `ClientSession._build_capabilities`. O agente só sabe repassar formulários, então o [_ClienteSoFormulario](agente/host_mcp.py:35) reduz o anúncio para `{"elicitation": {"form": {}}}`, como pede o contrato. O callback registrado nunca é chamado: a pergunta volta para o cliente A2A.

No `a2a-sdk` 1.1.2, um `SendMessage` para Task terminal é recusado corretamente com `-32602`, mas o `ActiveTask` criado para esse request não fecha as filas de eventos. Quando o coletor de lixo passa, o stderr do agente mostra `Failed to detach context` e `Task was destroyed but it is pending!` vindos de `EventQueueSource._dispatch_loop`. O ruído não afeta nenhuma resposta, e deixei o log visível em vez de silenciá-lo.

Os `uv.lock` ficam fora do repositório porque foram gerados contra um espelho de pacotes interno. O `uv sync` resolve a partir das versões fixadas no `pyproject.toml`, e o `requirements.txt` de cada pasta traz a árvore completa usada nos testes.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```text
trace-id desta execucao: d2f873c299260504cf755e9206ffd3de
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
