# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio do MBA Engenharia de Software com IA (curso de MCP e A2A), feita sobre o starter [devfullcycle/desafio-a2a-com-mcp](https://github.com/devfullcycle/desafio-a2a-com-mcp).

São dois processos Python separados, que conversam só por HTTP:

| Processo | Pasta | Porta | O que é |
|---|---|---|---|
| Servidor MCP | `servidor-mcp/` | 7301, Streamable HTTP em `/mcp` | `listar_salas`, `consultar_disponibilidade`, `reservar_sala` (com MRTR) e o resource `politica://uso` |
| Agente | `agente/` | 7300, JSON-RPC em `/a2a`, card em `/.well-known/agent-card.json` | Host MCP por dentro, servidor A2A v1.0 por fora |

Stack: Python 3.10+, SDK oficial `mcp==2.3.0` (revisão 2026-07-28 da spec), `starlette==1.7.0` e `uvicorn==0.54.0`. As versões estão travadas em cada `pyproject.toml`, e as dependências transitivas em cada `uv.lock`. O agente não usa LLM: ele lê um texto em formato fixo e decide por regra (`agente/agente/pedido.py`). `dados/`, `validador/` e `exemplos/` estão como vieram do starter.

```
servidor-mcp/servidor_mcp/
  __main__.py      sobe o uvicorn e lê REQUEST_STATE_SECRET
  servidor.py      MCPServer, tools, resource e o ciclo de MRTR da reserva
  dominio.py       salas, reservas em memória, regras da política e alternativas
  log_requests.py  log de cada request no stderr
agente/agente/
  __main__.py      sobe o uvicorn
  a2a.py           Agent Card e JSON-RPC (SendMessage, GetTask)
  tarefas.py       Task: estado, histórico, artifacts e a pausa
  ponte.py         host MCP e a costura input_required <-> TASK_STATE_INPUT_REQUIRED
  pedido.py        leitura de "reservar ..." e "escolha=..."
```

## Como rodar

Pré-requisitos: Python 3.10 ou superior e o [uv](https://docs.astral.sh/uv/getting-started/installation/) (testado com uv 0.11.8). O `uv run` cria o ambiente de cada pasta a partir do `uv.lock` na primeira execução.

```bash
git clone https://github.com/diegonogueira/mba-ia-desafio-a2a-com-mcp.git
```

```bash
cd mba-ia-desafio-a2a-com-mcp
```

**Terminal 1, servidor MCP.** A chave que protege o `requestState` vem de `REQUEST_STATE_SECRET`, em hexadecimal, com pelo menos 32 bytes (64 caracteres). Ela não está no código nem no repositório: gere a sua e exporte no terminal do servidor. Sem ela, ou com menos de 32 bytes, o processo não sobe.

```bash
cd servidor-mcp
```

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

```bash
uv run python -m servidor_mcp
```

Para reiniciar o servidor e continuar aceitando um `requestState` emitido antes, suba de novo no mesmo terminal (a variável continua exportada). Com outra chave, o estado antigo é rejeitado com `-32602`, como deve ser.

**Terminal 2, agente:**

```bash
cd agente
```

```bash
uv run python -m agente
```

**Terminal 3, validador** (com os dois processos recém-iniciados, porque as reservas de uma execução mudam o resultado da seguinte):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Variáveis opcionais, com os padrões que o validador espera: `MCP_HOST` (127.0.0.1) e `MCP_PORT` (7301) no servidor; `AGENTE_HOST` (127.0.0.1), `AGENTE_PORT` (7300), `MCP_URL` (`http://localhost:7301/mcp`) e `AGENTE_URL_PUBLICA` (`http://localhost:7300/a2a`, a URL publicada no card) no agente.

### O que aparece no stderr

O servidor MCP escreve uma linha por request recebido, antes de qualquer validação do SDK, com o método, o id JSON-RPC, o `traceparent` do `_meta`, o alvo (`name` da tool ou `uri`), `retry=sim` quando o request traz `requestState`, as `clientCapabilities` declaradas e os headers espelhados do corpo. Um trecho real, de uma Task que pausou e retomou:

```
mcp request method=tools/list id=4 traceparent=00-41cc39d869f1117fcf46f1b93d2ef571-...-01 clientCapabilities={"elicitation":{"form":{}}} headers[MCP-Protocol-Version=2026-07-28 Mcp-Method=tools/list]
mcp request method=resources/read id=5 traceparent=00-41cc39d869f1117fcf46f1b93d2ef571-...-01 name=politica://uso clientCapabilities={"elicitation":{"form":{}}} headers[MCP-Protocol-Version=2026-07-28 Mcp-Method=resources/read Mcp-Name=politica://uso]
mcp request method=tools/call id=6 traceparent=00-41cc39d869f1117fcf46f1b93d2ef571-...-01 name=reservar_sala clientCapabilities={"elicitation":{"form":{}}} headers[MCP-Protocol-Version=2026-07-28 Mcp-Method=tools/call Mcp-Name=reservar_sala]
mcp request method=tools/call id=7 traceparent=00-41cc39d869f1117fcf46f1b93d2ef571-...-01 name=reservar_sala retry=sim clientCapabilities={"elicitation":{"form":{}}} headers[MCP-Protocol-Version=2026-07-28 Mcp-Method=tools/call Mcp-Name=reservar_sala]
```

Para conferir a propagação do trace, procure no stderr do servidor o trace-id que o validador imprime na primeira linha. Os requests do agente têm ids numéricos (1, 2, 3, ...); os do validador, ids hexadecimais. O agente também registra no próprio stderr cada request A2A e cada transição de estado das Tasks.

## Onde a ponte acontece

Toda a costura está em [`agente/agente/ponte.py`](agente/agente/ponte.py). Um `SendMessage` sem `taskId` abre a Task ([`a2a.py:88`](agente/agente/a2a.py:88)) e chama `Ponte.iniciar`, que faz `tools/list` (descoberta em runtime: a Task falha se `reservar_sala` não estiver na lista), lê `politica://uso` para extrair a versão e emite o `tools/call` com `allow_input_required=True` ([`ponte.py:140`](agente/agente/ponte.py:140)), para receber o `input_required` cru em vez de deixar o SDK responder a elicitation sozinho. O resultado passa por `_aplicar` ([`ponte.py:186`](agente/agente/ponte.py:186)): quando é um `InputRequiredResult`, `_pausar` ([`ponte.py:208`](agente/agente/ponte.py:208)) guarda na Task um objeto `Pausa` ([`tarefas.py:29`](agente/agente/tarefas.py:29)) com o `requestState` intacto, a chave que o servidor deu à pergunta, as alternativas do `enum` (ou do `const`) e os argumentos originais, e muda a Task para `TASK_STATE_INPUT_REQUIRED` com a mensagem `alternativas: ...` ([`ponte.py:225`](agente/agente/ponte.py:225)). Esse é o ponto em que o `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED`. A `Pausa` não entra em `Task.para_json`, então o `requestState` não aparece no card, no artifact nem em mensagem nenhuma. O `SendMessage` de continuação, com `taskId`, chega a `Ponte.continuar` ([`a2a.py:100`](agente/agente/a2a.py:100), [`ponte.py:148`](agente/agente/ponte.py:148)): uma escolha fora das alternativas repete a pausa; `escolha=<id>` vira `action: accept` e `escolha=recusar` vira `action: decline`; e o retry sai em [`ponte.py:167`](agente/agente/ponte.py:167), um `tools/call` novo (o SDK gera um id JSON-RPC novo) com `inputResponses` usando a mesma chave e `requestState=pausa.request_state` ecoado sem modificação. Esse é o ponto em que o `requestState` volta para o servidor. Do lado do servidor, `reservar_sala` desvia para `_retomar` quando o request traz estado ([`servidor.py:92`](servidor-mcp/servidor_mcp/servidor.py:92), [`servidor.py:167`](servidor-mcp/servidor_mcp/servidor.py:167)) e reconstrói o pedido só a partir do estado selado.

Cada Task tem a sua própria `Pausa` e o seu próprio lock, então duas Tasks pausadas ao mesmo tempo retomam cada uma com o seu `requestState`. O `traceparent` recebido no header da chamada A2A fica guardado na Task (só o trace-id), e todo request MCP dessa Task leva `00-<mesmo trace-id>-<span-id novo>-01` no `_meta`.

O agente não decide nada de sala: conflito, política e alternativas vêm do servidor MCP. Ele traduz `isError: true` em `TASK_STATE_FAILED` com o texto da tool no histórico, `reservado: false` em `TASK_STATE_CANCELED` e o resultado completo em `TASK_STATE_COMPLETED` com o artifact `reserva`.

## Decisões técnicas

**Proteção do `requestState`.** Uso o utilitário do próprio SDK: `MCPServer(..., request_state_security=RequestStateSecurity(keys=[segredo], ttl=600))` ([`servidor.py:71`](servidor-mcp/servidor_mcp/servidor.py:71)). O `RequestStateBoundary` do SDK sela todo `requestState` que sai e verifica todo que volta. O selo é AES-256-GCM com chave derivada por HKDF-SHA256 de `REQUEST_STATE_SECRET`: o conteúdo vai cifrado, e qualquer caractere trocado faz a tag de autenticação falhar. Dentro do selo, o SDK grava `iat`, `exp`, o método, o nome da tool, um digest dos `arguments` e o nome do servidor como audiência. Estado adulterado, expirado, de outra tool ou com argumentos diferentes dos selados é rejeitado com `-32602 Invalid or expired requestState`, antes de a tool rodar. A chave é lida e validada em [`__main__.py:16`](servidor-mcp/servidor_mcp/__main__.py:16): hexadecimal, com pelo menos 32 bytes; não existe valor padrão.

**Validade.** 600 segundos (10 minutos, `TTL_REQUEST_STATE_SEGUNDOS` em [`servidor.py:28`](servidor-mcp/servidor_mcp/servidor.py:28)), dentro da faixa de 5 a 30 minutos pedida.

**MRTR escrito na tool.** A `reservar_sala` devolve `ReservaOut | InputRequiredResult` e monta o `InputRequiredResult` ela mesma ([`servidor.py:111`](servidor-mcp/servidor_mcp/servidor.py:111)), em vez de usar os resolvers `Resolve(...)` do SDK. O resolver recalcula a pergunta no retry e descarta a resposta se a pergunta mudou; como as reservas vivem em memória, depois de um restart as alternativas podem ser outras e o retry pediria input de novo em vez de concluir. Aqui, o estado selado carrega sala, início, fim, responsável e as alternativas oferecidas. No retry, o servidor não guarda nada em memória e não usa os argumentos reenviados: a escolha precisa estar entre as alternativas seladas, e a reserva é feita com os valores selados. Se a alternativa escolhida tiver sido ocupada entre a pergunta e o retry, o servidor pergunta de novo. A capability é conferida antes de qualquer pergunta ([`servidor.py:154`](servidor-mcp/servidor_mcp/servidor.py:154)): sem `elicitation.form` no `_meta` daquele request, a resposta é `-32021` com `data.requiredCapabilities`, e o transporte do SDK devolve HTTP 400.

**Estado das Tasks.** Em memória, no processo do agente: um dicionário `id -> Task` em [`agente/agente/tarefas.py`](agente/agente/tarefas.py). A Task guarda id, contextId, estado, histórico, artifacts, a versão da política lida do resource, o trace-id e a `Pausa`. Estado terminal é definitivo: `Task.mudar` recusa sair de `COMPLETED`, `CANCELED` ou `FAILED`, e um `SendMessage` para Task terminal recebe erro JSON-RPC `-32004`. Task inexistente recebe `-32001`. As Tasks não sobrevivem a um restart do agente, o que o enunciado permite; o que precisa sobreviver a restart é o `requestState`, e ele vive no cliente, não no servidor.

**Reservas.** Em memória no servidor MCP, carregadas de `dados/` na subida. Sem banco e sem ORM.

**Limitações do SDK encontradas.**

- Capability de elicitation: o `ClientSession` do `mcp==2.3.0` só declara elicitation quando recebe um callback, e nesse caso declara `form` e `url` e passa a responder sozinho. Trecho de `mcp/client/session.py`:

  ```python
  elicitation = (
      types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
      if self._elicitation_callback is not _default_elicitation_callback
      else None
  )
  ```

  Para declarar exatamente `{"elicitation": {"form": {}}}` sem callback, `ClienteMcp` ([`ponte.py`](agente/agente/ponte.py)) troca o `_build_capabilities` da sessão criada pelo `Client`. É o único ponto em que o agente toca em membro privado do SDK; o protocolo continua sendo o do SDK.
- Queda de conexão: quando o servidor MCP está fora do ar, o transporte Streamable HTTP do SDK encerra o `Client` inteiro (o task group interno termina com `httpx2.ConnectError`). A Task em andamento termina em `TASK_STATE_FAILED` com `Connection closed`, e a `Ponte` mantém o `Client` numa tarefa supervisora que abre outro em seguida, então os pedidos seguintes voltam a funcionar quando o servidor sobe de novo.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```
trace-id desta execucao: a53286456b4ab632b0edef17468ce722
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
