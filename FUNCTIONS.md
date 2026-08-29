# FUNCTIONS.md — agente_local/

Documentação função a função de `agent_gerencial.py` (1154 linhas) e
`inicializar_historico.py` (179 linhas). Cada entrada: o que faz, parâmetros,
retorno, efeitos colaterais, e de que outras funções depende / quem chama.

Convenção: "Firebird" = leitura direta no Farmasoft (sempre SELECT, nunca escreve —
ver `CLAUDE.md`). "Cloud" = HTTP para a API no Render/Supabase.

---

## `agent_gerencial.py`

### Configuração e estado (sem I/O de rede)

#### `_atualizar_duckdns()`
- **Parâmetros**: nenhum. **Retorno**: `None`.
- **Efeito colateral**: `GET` no DuckDNS pra atualizar o IP público do domínio configurado. Sem-op se `DUCKDNS_DOMAIN`/`DUCKDNS_TOKEN` vazios.
- **Depende de**: `DUCKDNS_DOMAIN`, `DUCKDNS_TOKEN` (globais do `.env`).
- **Chamada por**: `_loop_duckdns` (a cada ciclo), `main()` (uma vez, ao iniciar `--webhook`, antes de subir a thread do loop).

#### `_loop_duckdns()`
- **Parâmetros**: nenhum. **Retorno**: nunca retorna (`while True`).
- **Efeito colateral**: chama `_atualizar_duckdns()` a cada `DUCKDNS_INTERVALO` (default 5min).
- **Depende de**: `_atualizar_duckdns`.
- **Chamada por**: `main()`, como thread daemon (só em `--webhook`, se DuckDNS configurado).

#### `_data_hoje_servidor() -> date`
- **Parâmetros**: nenhum. **Retorno**: `date` — a data do servidor cloud, cacheada em `_data_servidor_cache` (variável global) até o próximo `sincronizar()` limpar o cache.
- **Efeito colateral**: `GET {CLOUD_URL}/api/server-time`; loga `WARNING` se o relógio local divergir do servidor; se a API estiver fora do ar, cai pro `date.today()` local.
- **Por quê existe**: protege contra o relógio do PC da farmácia estar errado (ver `b44cdf5` no histórico do projeto).
- **Chamada por**: `sincronizar()`.

#### `calcular_dias_sync() -> int`
- **Parâmetros**: nenhum. **Retorno**: `int` — quantos dias de vendas sincronizar.
- **Lógica**: sem `sync_state.json` ou estado inválido → `DIAS_PRIMEIRA_CARGA` (180). Com estado → `(dias desde último sync) + DIAS_MARGEM (2)`, limitado entre 2 e `DIAS_MAX_INCREMENTAL` (5). Se o "último sync" registrado está no futuro (relógio local bugado), força carga completa de 180 dias.
- **Efeito colateral**: lê `STATE_FILE` (`sync_state.json`) do disco.
- **Chamada por**: `main()` (modo default e `--loop`, quando `--dias` não foi passado), `_loop_background` (mesma condição, modo `--webhook`).

#### `salvar_estado_sync(incluiu_produtos: bool = False)`
- **Parâmetros**: `incluiu_produtos` — se o sync que acabou incluiu metadata completa de produtos.
- **Retorno**: `None`.
- **Efeito colateral**: grava `ultimo_sync` (sempre) e `ultimo_sync_produtos` (se `incluiu_produtos`) em `STATE_FILE`.
- **Chamada por**: `sincronizar()`, no final — **sempre**, mesmo com erros parciais (só não marca `incluiu_produtos` se houve erro, pra não perder integridade do metadata).

#### `_ja_feito_hoje(chave_state: str) -> bool`
- **Parâmetros**: `chave_state` — nome da chave no `sync_state.json` (ex: `"boletos_alerta_enviado"`).
- **Retorno**: `bool` — se essa chave já foi marcada com a data de hoje.
- **Por quê em disco, não em memória**: uma flag em RAM zera a cada restart do agente (deploy, queda de luz, `.bat` reaberto), fazendo a tarefa "uma vez por dia" rodar de novo no mesmo dia. Foi bug real corrigido em 25-29/08 (ver histórico do projeto).
- **Chamada por**: `_verificar_relatorio_agendado`, `_verificar_alerta_boletos`, `_verificar_limpeza_retencao`.

#### `_marcar_feito_hoje(chave_state: str)`
- **Parâmetros**: `chave_state`. **Retorno**: `None`.
- **Efeito colateral**: grava `state[chave_state] = hoje` em `STATE_FILE`.
- **Chamada por**: as mesmas 3 funções acima, depois de executar a ação com sucesso.

#### `deve_sincronizar_produtos() -> bool`
- **Parâmetros**: nenhum. **Retorno**: `bool`.
- **Lógica**: `True` se nunca sincronizou produtos completo, ou se passaram ≥6h desde o último; `False` (usa sync rápido de estoque) se foi há menos de 6h.
- **Efeito colateral**: lê `STATE_FILE`.
- **Chamada por**: `sincronizar()` (decide entre ler produtos completo vs `ler_estoque_rapido`).

#### `headers() -> dict`
- **Parâmetros**: nenhum. **Retorno**: `dict` com `Content-Type` e `X-Agent-Key` (usa `AGENT_TOKEN` se configurado, senão `AGENT_KEY` global).
- **Chamada por**: todo lugar que monta um request `requests.post/get(..., headers=headers())` fora de `post()` — `_recomputar_cache_cloud`, `_verificar_relatorio_agendado`, `_verificar_alerta_boletos`, `_verificar_limpeza_retencao`, e as 2 chamadas de verificação de alerta dentro de `sincronizar()`.

### HTTP e sync propriamente dito

#### `post(endpoint: str, payload: dict, tentativas: int = 3) -> bool`
- **Parâmetros**: `endpoint` (path, ex: `/api/sync/vendas`), `payload` (dict — **mutado in-place**: ganha as chaves `agent_key` e, se houver, `agente_token`), `tentativas` (default 3).
- **Retorno**: `bool` — sucesso (HTTP 200).
- **Efeito colateral**: `POST {CLOUD_URL}{endpoint}`; retry com 5s de espera fixa (não é backoff exponencial) em erro de rede ou HTTP 502/503/504; loga erro/warning conforme o caso.
- **Nota**: como `payload` é mutado, cada chamador deve montar um dict novo por request (é o que o código faz — nenhum reuso de dict entre chamadas).
- **Depende de**: `CLOUD_URL`, `AGENT_KEY`, `AGENT_TOKEN`, `headers()` (só pra montar o header separado do JSON de auth do body).
- **Chamada por**: todo o corpo de `sincronizar()` (um `post()` por dataset).

#### `conectar_farmasoft()`
- **Parâmetros**: nenhum. **Retorno**: a própria função `farmasoft_connection` (não uma conexão!).
- **Status**: **não é chamada em lugar nenhum do arquivo** — `sincronizar()` importa `farmasoft_connection` direto. Código morto, aparentemente um helper de uma versão anterior.

#### `_ping_api()`
- **Parâmetros**: nenhum. **Retorno**: `None`.
- **Efeito colateral**: `GET {CLOUD_URL}/health`, engole qualquer exceção — só serve pra acordar o Render/Supabase antes de uma leitura longa.
- **Chamada por**: `sincronizar()` (2x: no início, e de novo depois de ler produtos — leitura do Firebird pode levar minutos e deixar o Render dormir de novo nesse meio tempo).

#### `sincronizar(dias_vendas: int = 180) -> bool`
A função central do agente. **Parâmetros**: `dias_vendas` — quantos dias de vendas puxar. **Retorno**: `bool` (`True` se `erros == 0`).

- **Efeitos colaterais** (em ordem):
  1. `POST /api/sync/status` (`em_curso=True`)
  2. Produtos: completo (`ler_estoque_produtos` + metadata) se `deve_sincronizar_produtos()`, senão rápido (`ler_estoque_rapido`, só estoque/custo/preço) — em lotes de 500 via `POST /api/sync/produtos` ou `/api/sync/estoque`; se completo, ainda dispara `POST /api/sync/produtos/cleanup` com a lista de IDs ativos
  3. Vendas: `ler_vendas_por_produto_dia` → `POST /api/sync/vendas` em lotes de 500
  4. Saídas por validade: `ler_saidas_vencimento` → `POST /api/sync/saidas-validade`
  5. Balconistas: `ler_balconistas_dia` → `POST /api/sync/balconistas` em lotes de 500
  6. Compras (90d): `ler_compras_por_nota` → `POST /api/sync/compras`
  7. Transferências (30d): `ler_transferencias` (calcula valor real corrigindo caixaria/MOQ) → `POST /api/sync/transferencias`
  8. Recebimentos (180d): `ler_recebimentos_periodo` → `POST /api/sync/recebimentos` em lotes de 500; **passe extra**: produtos com estoque ≥1 sem fornecedor identificado nos 180d são buscados de novo até 730d atrás
  9. Contas a pagar (desde 01/06/2026, hardcoded): `ler_contas_pagar` → `POST /api/sync/contas-pagar`
  10. `POST /api/sync/status` (`em_curso=False`, com contagem de erros se houver)
  11. `salvar_estado_sync(incluiu_produtos=...)`
  12. `POST /api/telegram/verificar-alerta-bonus` e `POST /api/telegram/verificar-alerta-meta-mensal`
- **Depende de**: `_ping_api`, `_data_hoje_servidor`, `deve_sincronizar_produtos`, `post`, `headers`, `salvar_estado_sync`, `FarmasoftReader`/`farmasoft_connection` (`backend.etl`/`backend.utils`).
- **Chamada por**: `main()` (modo default e `--loop`), `_fazer_sync_thread` (modo `--webhook`, tanto pelo loop de background quanto por trigger HTTP sob demanda).

### Cache e sync sob demanda (modo `--webhook`)

#### `_recomputar_cache_cloud()`
- **Parâmetros**: nenhum. **Retorno**: `None`.
- **Efeito colateral**: `POST /api/cache/recomputar` — desde o commit `af73758`, isso só invalida (DELETE) o cache dos 3 endpoints pesados no backend, não recalcula (ver `DECISIONS.md` item 2).
- **Chamada por**: `main()` (dentro do `while True` do modo `--loop`, logo após `sincronizar()`), `_fazer_sync_thread` (modo `--webhook`).

#### `_fazer_sync_thread(dias)`
- **Parâmetros**: `dias` — dias de vendas a sincronizar.
- **Retorno**: `None`.
- **Efeito colateral**: se `_sync_lock` já está travado, loga e sai sem fazer nada (evita sync concorrente); senão adquire o lock, chama `sincronizar(dias_vendas=dias)`, libera o lock, chama `_recomputar_cache_cloud()`.
- **Depende de**: `_sync_lock`, `sincronizar`, `_recomputar_cache_cloud`.
- **Chamada por**: `WebhookHandler.do_POST` (em thread nova, fire-and-forget), `_loop_background`.

#### `_buscar_produtos_farmasoft(termo: str) -> list`
- **Parâmetros**: `termo` — string de busca (nome, princípio ativo ou código).
- **Retorno**: `list[dict]` — até 40 produtos (descrição, PA, laboratório, estoque, custo, preço).
- **Efeito colateral**: conexão Firebird nova (`FarmasoftConnection`), 1 SELECT (`FIRST 40`), fecha a conexão no `finally`. Escapa aspas simples manualmente no termo (proteção básica contra quebra de sintaxe SQL — Firebird embutido não suporta parâmetros nomeados nesse ponto do código).
- **Chamada por**: `WebhookHandler.do_GET` (rota `/buscar`).

### Servidor webhook (`class WebhookHandler(BaseHTTPRequestHandler)`)

| Método | Rota | O que faz |
|---|---|---|
| `do_GET` | `GET /health` | Responde `200 ok` — usado pelo `trigger_agent_sync`/`agent_status` do backend pra checar se o agente está acessível |
| `do_GET` | `GET /buscar?termo=X&key=` | Valida `key == AGENT_KEY` (só a chave global — **não aceita `AGENT_TOKEN` por filial** aqui), chama `_buscar_produtos_farmasoft`, responde JSON |
| `do_POST` | `POST /sync?key=` | Valida `key == AGENT_KEY` (mesma observação), dispara `_fazer_sync_thread` em thread nova com `self.dias_vendas` e responde `200` imediatamente (fire-and-forget, não espera o sync terminar) |
| `_responder`/`_responder_json` | — | Helpers de resposta HTTP (texto puro / JSON) |
| `log_message` | — | Sobrescreve o log padrão do `BaseHTTPRequestHandler` (que iria pro stderr) pra usar o `logger` do módulo |

- **Atributo de classe `dias_vendas = 180`**: todo trigger via `POST /sync` força um sync de 180 dias completos — **não** usa `calcular_dias_sync()` (incremental). Diferente do loop de background (`_loop_background`), que sempre calcula o incremental correto.

### Verificações periódicas (uma vez por dia)

#### `_verificar_relatorio_agendado()`
- **Parâmetros**: nenhum. **Retorno**: `None`.
- **Efeito colateral**: se `_ja_feito_hoje("relatorio_agendado_enviado")`, sai. Senão `GET /api/loja/config?filial_id=` pra ler `horario_relatorio`; se o horário atual já passou do configurado, `POST /api/telegram/enviar-relatorio-gerente` e `_marcar_feito_hoje`.
- **Chamada por**: `main()` (`--loop`), `_loop_background` (`--webhook`).

#### `_verificar_alerta_boletos()`
- **Parâmetros**: nenhum. **Retorno**: `None`. Constante local `BOLETOS_ALERTA_HORA = "08:00"`.
- **Efeito colateral**: se já feito hoje ou ainda não são 08:00, sai. Senão `POST /api/boletos/alerta-telegram` e marca feito.
- **Chamada por**: `main()` (`--loop`), `_loop_background` (`--webhook`).

#### `_verificar_limpeza_retencao()`
- **Parâmetros**: nenhum. **Retorno**: `None`.
- **Efeito colateral**: se já feito hoje, sai. Senão `POST /api/cache/limpeza-retencao` (purga `compras_pendentes`/`telegram_log`/`configuracoes` expirados) e marca feito.
- **Chamada por**: `main()` (`--loop`), `_loop_background` (`--webhook`).

### Loops de background

#### `_loop_keepalive()`
- **Parâmetros**: nenhum. **Retorno**: nunca (`while True`).
- **Efeito colateral**: dorme 60s, depois `GET {CLOUD_URL}/health` a cada 10min pra sempre, indefinidamente, engolindo erros — evita o Render dormir (cold start de ~90s).
- **Chamada por**: `main()` (thread daemon, em `--loop` e `--webhook`, se `CLOUD_URL` estiver setado).

#### `_pausa_noturna() -> bool`
- **Parâmetros**: nenhum. **Retorno**: `bool` — `True` se estava (e ficou) em pausa.
- **Efeito colateral**: se a hora atual está entre 0h-5h, **dorme (bloqueia a thread) até as 05:00** e retorna `True`; fora dessa janela, retorna `False` imediatamente sem dormir.
- **Chamada por**: `main()` (início de cada iteração do `while True` em `--loop`, via `continue` se `True`), `_loop_background` (mesmo padrão).

#### `_loop_background(dias_fixo)`
- **Parâmetros**: `dias_fixo` — `int` fixo ou `None` (calcula via `calcular_dias_sync()`).
- **Retorno**: nunca (`while True`).
- **Efeito colateral**: a cada ciclo — `_pausa_noturna()`, calcula dias, `_fazer_sync_thread(dias)` (não `sincronizar()` direto — passa pelo `_sync_lock`, importante porque um trigger HTTP `/sync` pode chegar no meio do ciclo), `_verificar_relatorio_agendado`, `_verificar_alerta_boletos`, `_verificar_limpeza_retencao`, dorme `INTERVALO` (default 15min).
- **Chamada por**: `main()`, como a thread daemon principal do modo `--webhook`.

### Entry point

#### `main()`
- **Parâmetros**: nenhum (lê `sys.argv` via `argparse`). **Retorno**: `None` (ou `sys.exit()`).
- **Flags**: `--loop`, `--webhook`, `--dias N`, `--reset`, `--migrate`.
- **`--migrate`**: se `DATABASE_URL` está no `.env`, conecta direto no Postgres e roda um DDL pequeno e hardcoded — só a tabela `sync_contas_pagar` e seus índices, um subconjunto antigo/limitado, não é o `criar_tabelas_app()` real. Sem `DATABASE_URL`, cai no fallback: acorda o Render (`GET /health` até 3x) e chama `POST /api/admin/migrate` (esse sim roda `criar_tabelas_app()` completo do `backend/sync/schema.py`). Sai em seguida.
- **`--reset`**: apaga `STATE_FILE`; sai, a menos que combinado com `--loop`/`--webhook` (aí só afeta o próximo cálculo de `calcular_dias_sync()`).
- **`--webhook`**: sobe `_loop_background` (thread daemon), `_loop_keepalive` (thread daemon, se `CLOUD_URL`), `_atualizar_duckdns()` + `_loop_duckdns` (thread daemon, se DuckDNS configurado); depois `HTTPServer(...).serve_forever()` bloqueando a thread principal em `WebhookHandler`.
- **`--loop`**: sobe `_loop_keepalive` (se `CLOUD_URL`); depois `while True`: `_pausa_noturna()`, `sincronizar()`, `_recomputar_cache_cloud()`, `_verificar_relatorio_agendado()`, `_verificar_alerta_boletos()`, `_verificar_limpeza_retencao()`, dorme `INTERVALO`.
- **Nenhuma flag**: `sincronizar()` único, `sys.exit(0 ou 1)`.

---

## `inicializar_historico.py`

Script standalone (não roda junto com `agent_gerencial.py`) pra popular
`historico_mensal` retroativamente — base pra sugestão automática de meta
("mesmo mês do ano anterior +10%").

#### `post(endpoint: str, payload: dict) -> bool`
- Versão própria e mais simples da `post()` do `agent_gerencial.py`: sem retry, timeout fixo de 60s, mesma mutação in-place do `payload` (adiciona `agent_key`).
- **Chamada por**: `processar_mes`.

#### `processar_mes(reader, ano: int, mes: int, sobrescrever: bool) -> bool`
- **Parâmetros**: `reader` (instância de `FarmasoftReader` já conectada), `ano`, `mes`, `sobrescrever` (força regravação se já existir no Neon/Supabase).
- **Retorno**: `bool` — sucesso.
- **Efeito colateral**: `reader.ler_resumo_periodo(...)` (Firebird, 1 mês inteiro); se não houve venda no mês, loga e retorna `False` sem enviar nada; senão monta o payload (meta/limite zerados de propósito — não afeta a sugestão) e `POST /api/historico/inicializar-mes`.
- **Depende de**: `FarmasoftReader.ler_resumo_periodo`, `post`.
- **Chamada por**: `main()`, uma vez por `(ano, mes)` da lista de períodos.

#### `main()`
- **Parâmetros**: nenhum (via `argparse`: `--ano`, `--mes`, `--sobrescrever`). **Retorno**: `None`.
- **Monta a lista de períodos**: `--ano`+`--mes` → só esse mês; só `--ano` → os 12 meses daquele ano; nenhum dos dois → últimos 12 meses fechados a partir de hoje (padrão). Sempre exclui meses futuros e o mês corrente (ainda aberto).
- **Efeito colateral**: valida `CLOUD_URL`/`AGENT_KEY` no `.env` (sai se faltar); conecta no Farmasoft via `FarmasoftConnection`; cria um `FarmasoftReader`; chama `processar_mes` pra cada período da lista; desconecta; loga quantos meses foram enviados com sucesso.
- **Depende de**: `processar_mes`, `FarmasoftConnection`, `FarmasoftReader`.

---

**Relacionado**: `ARCHITECTURE.md` (seção 3, `agente_local/`), `DECISIONS.md`
(item 1, regra de `empresa_id` via JWT — não se aplica aqui, o agente não tem JWT,
usa `AGENT_TOKEN`/`AGENT_KEY` por design).
