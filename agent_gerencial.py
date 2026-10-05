"""
Agent Gerencial - Sincronizador local

Le dados do Farmasoft (Firebird, READ-ONLY) e envia
para a API na nuvem (Render).

Uso:
    python agent_gerencial.py             # sync unico
    python agent_gerencial.py --loop      # loop continuo (15min)
    python agent_gerencial.py --dias 180  # quantos dias de vendas enviar
    python agent_gerencial.py --webhook   # servidor HTTP para trigger remoto + busca (porta 5001)

Endpoints do webhook:
    GET  /health              - status do agent
    POST /sync                - dispara sync manual (chave em X-Agent-Key header)
    GET  /buscar?termo=X      - busca produto em tempo real no Farmasoft (chave em X-Agent-Key header)
"""
import os
import sys
import json
import time
import logging
import argparse
import threading
import subprocess
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import date, timedelta, datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

# Pasta desta instalacao standalone (agente_local/)
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Adicionar esta pasta ao path (backend/ esta aqui dentro)
sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AGENT] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "agent.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)

# ── Configuracoes via .env ─────────────────────────────────────────────────
CLOUD_URL   = os.getenv("CLOUD_API_URL", "")
AGENT_KEY   = os.getenv("AGENT_API_KEY", "")
FILIAL_ID   = int(os.getenv("FILIAL_ID", "1"))
EMPRESA_ID  = int(os.getenv("EMPRESA_ID", "1"))
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "")   # token por filial; vazio = usa AGENT_KEY global
VERSAO      = "1.0.0"
TIMEOUT_HTTP = 180
INTERVALO   = int(os.getenv("AGENT_INTERVALO_MIN", "15")) * 60

DUCKDNS_DOMAIN   = os.getenv("DUCKDNS_DOMAIN", "")
DUCKDNS_TOKEN    = os.getenv("DUCKDNS_TOKEN", "")
DUCKDNS_INTERVALO = int(os.getenv("DUCKDNS_INTERVALO_MIN", "5")) * 60


def _atualizar_duckdns():
    """Atualiza IP publico no DuckDNS."""
    if not DUCKDNS_DOMAIN or not DUCKDNS_TOKEN:
        return
    try:
        url = f"https://www.duckdns.org/update?domains={DUCKDNS_DOMAIN}&token={DUCKDNS_TOKEN}&ip="
        r = requests.get(url, timeout=10)
        if r.text.strip() == "OK":
            logger.info(f"DuckDNS atualizado: {DUCKDNS_DOMAIN}.duckdns.org")
        else:
            logger.warning(f"DuckDNS retornou: {r.text.strip()}")
    except Exception as e:
        logger.warning(f"Erro ao atualizar DuckDNS: {e}")


def _loop_duckdns():
    """Thread que atualiza DuckDNS periodicamente."""
    while True:
        _atualizar_duckdns()
        time.sleep(DUCKDNS_INTERVALO)

STATE_FILE  = Path(__file__).resolve().parent / "sync_state.json"
DIAS_PRIMEIRA_CARGA = 180
DIAS_MAX_INCREMENTAL = 5     # maximo de dias no sync incremental (gap grande / catch-up diario)
DIAS_MARGEM_ROTINA = 1       # janela do ciclo normal (~15min, agente rodando continuo)


_data_servidor_cache: date | None = None


def _data_hoje_servidor() -> date:
    """
    Retorna a data atual do servidor cloud (PostgreSQL).
    Protege contra relogio local incorreto no PC da farmacia.
    Fallback para date.today() se API indisponivel.
    """
    global _data_servidor_cache
    if _data_servidor_cache is not None:
        return _data_servidor_cache
    try:
        r = requests.get(
            f"{CLOUD_URL.rstrip('/')}/api/server-time",
            timeout=5,
        )
        if r.status_code == 200:
            data_banco = date.fromisoformat(r.json()["date"])
            data_local = date.today()
            delta = abs((data_banco - data_local).days)
            if delta > 0:
                logger.warning(
                    f"RELOGIO LOCAL INCORRETO: local={data_local} servidor={data_banco} "
                    f"(diferenca de {delta} dia(s)). Usando data do servidor."
                )
            _data_servidor_cache = data_banco
            return data_banco
    except Exception as e:
        logger.debug(f"server-time indisponivel ({e}), usando relogio local")
    return date.today()


def calcular_dias_sync() -> int:
    """
    Retorna quantos dias sincronizar:
    - Sem historico local: 180 dias (carga completa)
    - Gap desde o ultimo sync (agente ficou offline, ex: PC desligado a noite):
      dias desde o ultimo sync + margem, capado em DIAS_MAX_INCREMENTAL --
      recupera o que perdeu
    - Ciclo normal (~15min, agente rodando continuo, gap < 1 dia): so
      DIAS_MARGEM_ROTINA (hoje + ontem) -- nao precisa reler uma janela de
      varios dias toda vez, o proximo ciclo em 15min ja cobre o que mudar.
      1x/dia faz um catch-up mais largo (DIAS_MAX_INCREMENTAL) pra pegar
      alguma correcao tardia lancada no Farmasoft que o ciclo apertado
      sozinho nao cobriria (mesmo padrao ja usado pra compras/transferencias/
      recebimentos, ver `_ja_feito_hoje`)
    """
    try:
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text())
            ultimo = datetime.fromisoformat(state.get("ultimo_sync", ""))
            agora  = datetime.now()
            if ultimo > agora:
                logger.warning(
                    f"Ultimo sync ({ultimo.strftime('%d/%m/%Y %H:%M')}) esta no futuro "
                    f"- relogio local estava incorreto. Forcando recarga completa."
                )
                raise ValueError("sync no futuro")
            dias_passados = (agora - ultimo).days
            if dias_passados == 0:
                if not _ja_feito_hoje("vendas_catchup_amplo"):
                    _marcar_feito_hoje("vendas_catchup_amplo")
                    dias = DIAS_MAX_INCREMENTAL
                    logger.info(f"Sync incremental (catch-up diario amplo): {dias} dias")
                else:
                    dias = DIAS_MARGEM_ROTINA
                    logger.info(f"Sync incremental (ciclo normal): {dias} dias (ultimo sync: {ultimo.strftime('%d/%m %H:%M')})")
            else:
                dias = max(DIAS_MARGEM_ROTINA, min(dias_passados + DIAS_MARGEM_ROTINA, DIAS_MAX_INCREMENTAL))
                logger.info(f"Sync incremental (gap de {dias_passados}d): {dias} dias (ultimo sync: {ultimo.strftime('%d/%m %H:%M')})")
            return dias
    except Exception:
        pass
    logger.info(f"Primeiro sync ou estado invalido: carregando {DIAS_PRIMEIRA_CARGA} dias")
    return DIAS_PRIMEIRA_CARGA


def salvar_estado_sync(incluiu_produtos: bool = False):
    """Grava timestamp do sync bem-sucedido para uso na proxima execucao."""
    try:
        state = {}
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text())
        state["ultimo_sync"] = datetime.now().isoformat()
        if incluiu_produtos:
            state["ultimo_sync_produtos"] = datetime.now().isoformat()
        STATE_FILE.write_text(json.dumps(state))
    except Exception as e:
        logger.warning(f"Nao foi possivel salvar sync_state.json: {e}")


def _ja_feito_hoje(chave_state: str) -> bool:
    """Verifica no sync_state.json (em disco) se uma tarefa 'uma vez por dia' ja rodou hoje.

    Usa disco em vez de variavel em memoria -- uma variavel em memoria zera toda
    vez que o agente reinicia (deploy, queda de luz, reabrir o .bat), fazendo a
    tarefa rodar de novo no mesmo dia.
    """
    try:
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text())
            return state.get(chave_state) == date.today().isoformat()
    except Exception:
        pass
    return False


def _marcar_feito_hoje(chave_state: str):
    """Grava no sync_state.json que uma tarefa 'uma vez por dia' rodou hoje."""
    try:
        state = {}
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text())
        state[chave_state] = date.today().isoformat()
        STATE_FILE.write_text(json.dumps(state))
    except Exception as e:
        logger.warning(f"Nao foi possivel salvar estado ({chave_state}): {e}")


def deve_sincronizar_produtos() -> bool:
    """Produtos (metadata completa) so sincronizam uma vez por dia."""
    try:
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text())
            ultimo = state.get("ultimo_sync_produtos")
            if ultimo:
                agora = datetime.now()
                dt_ultimo = datetime.fromisoformat(ultimo)
                if dt_ultimo > agora:
                    logger.warning("Ultimo sync produtos esta no futuro - forcando resync.")
                    return True
                horas = (agora - dt_ultimo).total_seconds() / 3600
                if horas < 6:
                    logger.info(f"Metadata produtos em cache ({horas:.1f}h) - usando sync rapido de estoque")
                    return False
    except Exception:
        pass
    return True


def headers():
    h = {"Content-Type": "application/json"}
    # Se ha token por filial usa ele como credencial primaria
    # Caso contrario usa a chave global (retrocompatibilidade)
    h["X-Agent-Key"] = AGENT_TOKEN if AGENT_TOKEN else AGENT_KEY
    return h


def post(endpoint: str, payload: dict, tentativas: int = 3) -> bool:
    url = f"{CLOUD_URL.rstrip('/')}{endpoint}"
    # token enviado apenas no header X-Agent-Key (ver headers()), nao no corpo
    for tentativa in range(1, tentativas + 1):
        try:
            r = requests.post(url, json=payload, headers=headers(), timeout=TIMEOUT_HTTP)
            if r.status_code == 200:
                return True
            if r.status_code in (502, 503, 504) and tentativa < tentativas:
                logger.warning(f"POST {endpoint} retornou {r.status_code} (tentativa {tentativa}/{tentativas}), aguardando 5s...")
                time.sleep(5)
                continue
            logger.error(f"POST {endpoint} retornou {r.status_code}: {r.text[:200]}")
            return False
        except Exception as e:
            if tentativa < tentativas:
                logger.warning(f"Erro HTTP {endpoint} (tentativa {tentativa}/{tentativas}): {e}, aguardando 5s...")
                time.sleep(5)
                continue
            logger.error(f"Erro HTTP {endpoint}: {e}")
            return False
    return False


def conectar_farmasoft():
    from backend.utils.database import farmasoft_connection
    return farmasoft_connection


def sincronizar(dias_vendas: int = 180):
    logger.info(f"Iniciando sync | filial={FILIAL_ID} | dias={dias_vendas}")

    if not CLOUD_URL:
        logger.error("CLOUD_API_URL nao configurado no .env")
        return False

    if not AGENT_KEY and not AGENT_TOKEN:
        logger.error("Credencial nao configurada: defina AGENT_TOKEN (Admin > Agente > Gerar Token) ou AGENT_API_KEY no .env")
        return False

    try:
        from backend.utils.database import farmasoft_connection
        from backend.etl.farmasoft_reader import FarmasoftReader
    except ImportError as e:
        logger.error(f"Erro ao importar backend: {e}")
        return False

    global _data_servidor_cache
    _data_servidor_cache = None  # limpa cache para obter data atualizada a cada sync
    hoje = _data_hoje_servidor()
    data_inicio_vendas = hoje - timedelta(days=dias_vendas)
    data_inicio_compras = hoje - timedelta(days=90)
    data_inicio_30d  = hoje - timedelta(days=30)
    data_inicio_180d = hoje - timedelta(days=180)

    # Notificar cloud que sync iniciou
    post("/api/sync/status", {
        "empresa_id": EMPRESA_ID, "filial_id": FILIAL_ID,
        "em_curso": True,
        "versao_agent": VERSAO,
    })

    erros = 0
    sync_completo = False

    try:
        with farmasoft_connection() as conn:
            reader = FarmasoftReader(conn)

            # ── 1. PRODUTOS ──────────────────────────────────────────────────
            sync_completo = deve_sincronizar_produtos()
            if sync_completo:
                logger.info("Lendo produtos completo (metadata + estoque)...")
                try:
                    estoque_rows = reader.ler_estoque_produtos(filial_id=FILIAL_ID, apenas_ativos=True)
                    produtos = []
                    for p in estoque_rows:
                        produtos.append({
                            "id_produto":      p.get("id_produto") or p.get("ID_PRODUTO"),
                            "cd_produto":      str(p.get("cd_produto") or p.get("CD_PRODUTO") or ""),
                            "descricao":       str(p.get("descricao") or p.get("DESCRICAO") or ""),
                            "principio_ativo": str(p.get("principio_ativo") or p.get("PRINCIPIOATIVO") or ""),
                            "laboratorio":     str(p.get("laboratorio") or p.get("LABORATORIO") or ""),
                            "cd_laboratorio":  int(p.get("cd_laboratorio") or p.get("CD_LABORATORIO") or 0),
                            "cd_grupo":        int(p.get("cd_grupo") or p.get("CD_GRUPO") or 0),
                            "cd_classe":       int(p.get("cd_classe") or p.get("CD_CLASSE") or 0),
                            "estoque_atual":   float(p.get("estoque") or p.get("ESTOQUE") or 0),
                            "custo_unitario":  float(p.get("custo_unitario") or p.get("CUSTO_UNITARIO") or 0),
                            "preco_venda":     float(p.get("preco_venda") or p.get("PRECO_VENDA") or 0),
                            "ean":             str(p.get("ean") or p.get("EAN") or ""),
                            "ean2":            str(p.get("ean2") or p.get("EAN2") or ""),
                        })
                    LOTE_P = 500
                    ok_prod = True
                    total_prod = len(produtos)
                    ids_ativos = [p["id_produto"] for p in produtos]
                    for i in range(0, total_prod, LOTE_P):
                        if not post("/api/sync/produtos", {
                            "empresa_id": EMPRESA_ID, "filial_id": FILIAL_ID,
                            "produtos": produtos[i:i+LOTE_P],
                        }):
                            ok_prod = False
                            erros += 1
                    if ok_prod and total_prod > 0:
                        # Cleanup por lista de IDs ativos (nao depende de relogio)
                        # Evita que diferenca de fuso/skew entre agente e servidor
                        # apague linhas recem-enviadas (problema do modo antes_de)
                        post("/api/sync/produtos/cleanup", {
                            "empresa_id": EMPRESA_ID, "filial_id": FILIAL_ID,
                            "ids_ativos": ids_ativos,
                            "total_ativos": total_prod,
                        })
                        logger.info(f"Produtos enviados: {total_prod}")
                    elif ok_prod and total_prod == 0:
                        logger.warning("ler_estoque_produtos retornou 0 produtos - cleanup ignorado para nao apagar dados existentes")
                except Exception as e:
                    logger.error(f"Erro ao ler produtos: {e}")
                    erros += 1
                    sync_completo = False
            else:
                # Sync incremental: atualiza apenas estoque/custo/preco (sem metadata)
                logger.info("Sync incremental: lendo estoque rapido...")
                try:
                    estoq_rows = reader.ler_estoque_rapido(filial_id=FILIAL_ID)
                    itens_estoq = [
                        {
                            "id_produto":    int(r.get("id_produto") or r.get("ID_PRODUTO") or 0),
                            "estoque":       float(r.get("estoque") or r.get("ESTOQUE") or 0),
                            "custo_unitario":float(r.get("custo_unitario") or r.get("CUSTO_UNITARIO") or 0),
                            "preco_venda":   float(r.get("preco_venda") or r.get("PRECO_VENDA") or 0),
                        }
                        for r in estoq_rows
                        if (r.get("id_produto") or r.get("ID_PRODUTO"))
                    ]
                    LOTE_E = 500
                    ok_estoq = True
                    for i in range(0, len(itens_estoq), LOTE_E):
                        if not post("/api/sync/estoque", {
                            "empresa_id": EMPRESA_ID, "filial_id": FILIAL_ID,
                            "itens": itens_estoq[i:i+LOTE_E],
                        }):
                            ok_estoq = False
                            erros += 1
                    if ok_estoq:
                        logger.info(f"Estoque rapido enviado: {len(itens_estoq)} produtos")
                    else:
                        logger.warning("Estoque rapido: alguns lotes falharam")
                except Exception as e:
                    logger.error(f"Estoque rapido erro: {e}")
                    erros += 1

            # ── 2. VENDAS (ultimos N dias, por produto por dia) ─────────────
            logger.info(f"Lendo vendas desde {data_inicio_vendas}...")
            try:
                vendas_raw = reader.ler_vendas_por_produto_dia(
                    data_inicio=data_inicio_vendas,
                    data_fim=hoje,
                    filial_id=FILIAL_ID,
                )
                vendas = []
                for v in vendas_raw:
                    d = v.get("data_venda") or v.get("DATA_VENDA")
                    vendas.append({
                        "data_venda":      str(d)[:10] if d else None,
                        "id_produto":      int(v.get("id_produto") or v.get("ID_PRODUTO") or 0),
                        "cd_produto":      str(v.get("cd_produto") or v.get("CD_PRODUTO") or ""),
                        "descricao":       str(v.get("descricao") or v.get("DESCRICAO") or ""),
                        "cd_grupo":        int(v.get("cd_grupo") or v.get("CD_GRUPO") or 0),
                        "cd_classe":       int(v.get("cd_classe") or v.get("CD_CLASSE") or 0),
                        "cd_balconista":   int(v.get("cd_balconista") or v.get("CD_BALCONISTA") or 0),
                        "nome_balconista": str(v.get("nome_balconista") or v.get("NOME_BALCONISTA") or ""),
                        "quantidade":      float(v.get("quantidade") or v.get("QUANTIDADE") or 0),
                        "valor_total":     float(v.get("valor_total") or v.get("VALOR_TOTAL") or 0),
                        "custo_total":     float(v.get("custo_total") or v.get("CUSTO_TOTAL") or 0),
                    })
                # Enviar em lotes de 500
                LOTE = 500
                for i in range(0, len(vendas), LOTE):
                    lote = vendas[i:i+LOTE]
                    if not post("/api/sync/vendas", {
                        "empresa_id": EMPRESA_ID, "filial_id": FILIAL_ID,
                        "data_inicio": str(data_inicio_vendas),
                        "lote": i // LOTE,
                        "total_lotes": (len(vendas) + LOTE - 1) // LOTE,
                        "vendas": lote,
                    }):
                        erros += 1
                logger.info(f"Vendas enviadas: {len(vendas)}")
            except Exception as e:
                logger.error(f"Erro ao ler vendas: {e}")
                erros += 1

            # ── 2b. SAIDAS POR VALIDADE (LANCAMENTOS TIPO_VENDA='X') ──────────
            logger.info("Lendo saidas por validade...")
            try:
                saidas_venc_raw = reader.ler_saidas_vencimento(
                    data_inicio=data_inicio_vendas,
                    data_fim=hoje,
                    filial_id=FILIAL_ID,
                )
                saidas_venc = [
                    {
                        "data":           str(s["data"])[:10] if s.get("data") else None,
                        "id_produto":     int(s["id_produto"]),
                        "descricao":      str(s.get("descricao", "") or ""),
                        "cd_grupo":       int(s.get("cd_grupo", 0) or 0),
                        "quantidade":     float(s.get("quantidade", 0) or 0),
                        "custo_unitario": float(s.get("custo_unitario", 0) or 0),
                        "usuario":        str(s.get("usuario", "") or ""),
                    }
                    for s in saidas_venc_raw
                ]
                if saidas_venc:
                    if post("/api/sync/saidas-validade", {
                        "empresa_id": EMPRESA_ID, "filial_id": FILIAL_ID,
                        "data_inicio": str(data_inicio_vendas),
                        "saidas": saidas_venc,
                    }):
                        logger.info(f"Saidas validade enviadas: {len(saidas_venc)}")
                    else:
                        erros += 1
                else:
                    logger.info("Saidas validade enviadas: 0")
            except Exception as e:
                logger.error(f"Erro ao ler saidas validade: {e}")

            # ── 3. BALCONISTAS (agregados diarios com contagem de transacoes) ─
            logger.info("Lendo balconistas dia...")
            try:
                balc_raw = reader.ler_balconistas_dia(
                    data_inicio=data_inicio_vendas,
                    data_fim=hoje,
                    filial_id=FILIAL_ID,
                )
                balconistas = []
                for b in balc_raw:
                    d = b.get("DATA_VENDA") or b.get("data_venda")
                    balconistas.append({
                        "data_venda":       str(d)[:10] if d else None,
                        "cd_balconista":    int(b.get("CD_BALCONISTA") or b.get("cd_balconista") or 0),
                        "nome_balconista":  str(b.get("NOME_BALCONISTA") or b.get("nome_balconista") or ""),
                        "qtd_transacoes":   int(b.get("QTD_TRANSACOES") or b.get("qtd_transacoes") or 0),
                        "qtd_itens":        int(b.get("QTD_ITENS") or b.get("qtd_itens") or 0),
                        "valor_total":      float(b.get("VALOR_TOTAL") or b.get("valor_total") or 0),
                        "custo_total":      float(b.get("CUSTO_TOTAL") or b.get("custo_total") or 0),
                        "comissao":         float(b.get("COMISSAO_DIA") or b.get("comissao_dia") or b.get("comissao") or 0),
                    })
                LOTE_B = 500
                for i in range(0, len(balconistas), LOTE_B):
                    if not post("/api/sync/balconistas", {
                        "empresa_id": EMPRESA_ID, "filial_id": FILIAL_ID,
                        "data_inicio": str(data_inicio_vendas),
                        "lote": i // LOTE_B,
                        "balconistas": balconistas[i:i+LOTE_B],
                    }):
                        erros += 1
                logger.info(f"Balconistas enviados: {len(balconistas)}")
            except Exception as e:
                logger.error(f"Erro ao ler balconistas: {e}")
                erros += 1

            # ── 4. COMPRAS ───────────────────────────────────────────────────
            # Full 90d so 1x/dia -- reenviar isso toda sincronizacao (96x/dia)
            # reenviava o mesmo historico o dia inteiro sem necessidade (mesmo
            # padrao ja aplicado em recebimentos, achado 31/08/2026). Nos outros
            # ciclos, janela curta (7d) cobre nota lancada com atraso.
            compras_full = not _ja_feito_hoje("compras_full_90d")
            data_inicio_compras_ef = data_inicio_compras if compras_full else (hoje - timedelta(days=7))
            logger.info(
                "Lendo compras... "
                + ("(sync completo 90d, 1x/dia)" if compras_full else "(incremental 7d)")
            )
            try:
                compras_raw = reader.ler_compras_por_nota(
                    data_inicio=data_inicio_compras_ef,
                    data_fim=hoje,
                    filial_id=FILIAL_ID,
                )
                compras = []
                for c in compras_raw:
                    d = c.get("data_compra") or c.get("DATA_NF")
                    compras.append({
                        "data_compra": str(d)[:10] if d else None,
                        "numero_nf":   str(c.get("numero_nf") or c.get("NOTA_FISCAL") or ""),
                        "cd_classe":   int(c.get("cd_classe") or c.get("CD_CLASSE") or 0),
                        "cd_grupo":    int(c.get("cd_grupo") or c.get("CD_GRUPO") or 0),
                        "valor_total": float(c.get("valor_total") or c.get("VALOR_TOTAL") or 0),
                    })
                if not post("/api/sync/compras", {
                    "empresa_id": EMPRESA_ID, "filial_id": FILIAL_ID, "compras": compras,
                    "data_inicio": str(data_inicio_compras_ef),
                }):
                    erros += 1
                else:
                    logger.info(f"Compras enviadas: {len(compras)}")
                    if compras_full:
                        _marcar_feito_hoje("compras_full_90d")
            except Exception as e:
                logger.error(f"Erro ao ler compras: {e}")
                erros += 1

            # ── 4. TRANSFERENCIAS ────────────────────────────────────────────
            # Mesmo padrao: full 30d so 1x/dia, janela curta (7d) nos outros ciclos.
            transf_full = not _ja_feito_hoje("transferencias_full_30d")
            data_inicio_transf_ef = data_inicio_30d if transf_full else (hoje - timedelta(days=7))
            logger.info(
                "Lendo transferencias... "
                + ("(sync completo 30d, 1x/dia)" if transf_full else "(incremental 7d)")
            )
            try:
                transf_raw = reader.ler_transferencias(
                    data_inicio=data_inicio_transf_ef,
                    data_fim=hoje,
                    filial_id=FILIAL_ID,
                )
                transf = []
                for t in transf_raw:
                    sentido = str(t.get("sentido") or "")
                    # Data efetiva: ENVIADA usa data_envio, RECEBIDA usa data_conclusao
                    # Fallback para data_geracao em ambos
                    if sentido == "ENVIADA":
                        d = (t.get("data_envio") or t.get("DATA_ENVIO")
                             or t.get("data_geracao") or t.get("DATA_GERACAO"))
                    else:
                        d = (t.get("data_conclusao") or t.get("DATA_CONCLUSAO")
                             or t.get("data_geracao") or t.get("DATA_GERACAO"))
                    qtd = float(t.get("qtd_enviada") or t.get("qtd_recebida") or t.get("qtd_solicitada") or 0)
                    custo_unit = float(t.get("custo_unitario_prod") or 0)
                    custo_medio = float(t.get("custo_medio_prod") or 0)
                    valor_it = float(t.get("valor") or 0)
                    if custo_unit > 0 and custo_unit <= valor_it:
                        valor_total = custo_unit * qtd
                    elif custo_medio > 0 and custo_medio <= valor_it:
                        valor_total = custo_medio * qtd
                    else:
                        valor_total = valor_it * qtd
                    transf.append({
                        "cd_transfer":          int(t.get("cd_transfer") or 0),
                        "data_transfer":        str(d)[:10] if d else None,
                        "id_produto":           int(t.get("id_produto") or 0),
                        "descricao":            str(t.get("descricao") or ""),
                        "filial_origem":        int(t.get("filial_origem") or 0),
                        "nome_filial_origem":   str(t.get("nome_filial_origem") or ""),
                        "filial_destino":       int(t.get("filial_destino") or 0),
                        "nome_filial_destino":  str(t.get("nome_filial_destino") or ""),
                        "quantidade":           qtd,
                        "valor":                round(valor_total, 2),
                        "valor_embalagem_raw":  round(valor_it, 4),
                        "sentido":              sentido,
                        "status_transfer":      str(t.get("status_transfer") or ""),
                    })
                if not post("/api/sync/transferencias", {
                    "empresa_id": EMPRESA_ID, "filial_id": FILIAL_ID, "transferencias": transf,
                    "data_inicio": str(data_inicio_transf_ef),
                }):
                    erros += 1
                else:
                    logger.info(f"Transferencias enviadas: {len(transf)}")
                    if transf_full:
                        _marcar_feito_hoje("transferencias_full_30d")
            except Exception as e:
                logger.error(f"Erro ao ler transferencias: {e}")
                erros += 1

            # ── 4b. ENTREGAS (proxy: cliente identificado + endereco completo) ──
            # Farmasoft nao tem campo nativo de entrega -- ver ler_entregas_por_periodo.
            # Mesma janela incremental de vendas (nao um full-scan a cada ciclo).
            logger.info("Lendo entregas (estimativa por endereco de cliente)...")
            try:
                entregas_raw = reader.ler_entregas_por_periodo(
                    data_inicio=data_inicio_vendas,
                    data_fim=hoje,
                    filial_id=FILIAL_ID,
                )
                entregas = []
                for e in entregas_raw:
                    d = e.get("data_venda") or e.get("DATA_VENDA")
                    entregas.append({
                        "cd_venda":        int(e.get("cd_venda") or e.get("CD_VENDA") or 0),
                        "data_venda":      str(d)[:10] if d else None,
                        "id_produto":      int(e.get("id_produto") or e.get("ID_PRODUTO") or 0),
                        "descricao":       str(e.get("descricao") or e.get("DESCRICAO") or ""),
                        "quantidade":      float(e.get("quantidade") or e.get("QUANTIDADE") or 0),
                        "valor_total":     float(e.get("valor_total") or e.get("VALOR_TOTAL") or 0),
                        "cd_balconista":   int(e.get("cd_balconista") or e.get("CD_BALCONISTA") or 0),
                        "nome_balconista": str(e.get("nome_balconista") or e.get("NOME_BALCONISTA") or ""),
                    })
                if not post("/api/sync/entregas", {
                    "empresa_id": EMPRESA_ID, "filial_id": FILIAL_ID,
                    "entregas": entregas, "data_inicio": str(data_inicio_vendas),
                }):
                    erros += 1
                else:
                    logger.info(f"Entregas enviadas: {len(entregas)}")
            except Exception as e:
                logger.error(f"Erro ao ler entregas: {e}")
                erros += 1

            # ── 5. RECEBIMENTOS ──────────────────────────────────────────
            # Full 180d (+ passe extra 730d) so 1x/dia -- reenviar isso toda
            # sincronizacao (96x/dia) reenviava ~6000+ itens repetidos sem
            # necessidade (achado 30/08/2026, log do Render). Nos outros ciclos
            # do dia, janela curta (7d) cobre nota lancada com atraso no Farmasoft.
            recebimentos_full = not _ja_feito_hoje("recebimentos_full_180d")
            data_inicio_receb = data_inicio_180d if recebimentos_full else (hoje - timedelta(days=7))
            logger.info(
                "Lendo recebimentos... "
                + ("(sync completo 180d, 1x/dia)" if recebimentos_full else "(incremental 7d)")
            )
            try:
                receb_raw = reader.ler_recebimentos_periodo(
                    data_inicio=data_inicio_receb,
                    data_fim=hoje,
                    filial_id=FILIAL_ID,
                )
                receb = []
                ids_com_fornecedor = set()
                for r in receb_raw:
                    d = r.data_emissao
                    forn = str(r.fornecedor or "").strip()
                    receb.append({
                        "cd_compras":      int(r.cd_compras or 0),
                        "numero_nf":       str(r.numero_nf or ""),
                        "data_emissao":    str(d)[:10] if d else None,
                        "id_produto":      int(r.id_produto or 0),
                        "descricao":       str(r.descricao or ""),
                        "laboratorio":     str(r.laboratorio or ""),
                        "principio_ativo": str(r.principio_ativo or ""),
                        "quantidade":      int(r.quantidade or 0),
                        "valor_total":     float(r.valor_total or 0),
                        "fornecedor":      forn,
                    })
                    if forn:
                        ids_com_fornecedor.add(int(r.id_produto or 0))

                # Passe extra: produtos com estoque >= 1 sem fornecedor nos 180 dias,
                # busca ate 730 dias atras no Farmasoft. So faz sentido junto do sync
                # completo (recebimentos_full) -- com janela curta (7d) quase todo
                # produto pareceria "sem fornecedor" e essa consulta de 730d rodaria
                # toda sincronizacao (96x/dia) por engano.
                if recebimentos_full:
                    try:
                        _plist = locals().get("produtos") or []
                        ids_com_estoque = {
                            int(p["id_produto"])
                            for p in _plist
                            if float(p.get("estoque_atual") or 0) >= 1
                        }
                        ids_sem_forn = ids_com_estoque - ids_com_fornecedor
                        if ids_sem_forn:
                            logger.info(f"Recebimentos extra: {len(ids_sem_forn)} produtos com estoque sem fornecedor, buscando ate 730 dias...")
                            data_inicio_730d = hoje - timedelta(days=730)
                            receb_extra_raw = reader.ler_recebimentos_periodo(
                                data_inicio=data_inicio_730d,
                                data_fim=data_inicio_180d,
                                filial_id=FILIAL_ID,
                                produtos_ids=list(ids_sem_forn),
                            )
                            extra_com_forn = [
                                {
                                    "cd_compras":      int(r.cd_compras or 0),
                                    "numero_nf":       str(r.numero_nf or ""),
                                    "data_emissao":    str(r.data_emissao)[:10] if r.data_emissao else None,
                                    "id_produto":      int(r.id_produto or 0),
                                    "descricao":       str(r.descricao or ""),
                                    "laboratorio":     str(r.laboratorio or ""),
                                    "principio_ativo": str(r.principio_ativo or ""),
                                    "quantidade":      int(r.quantidade or 0),
                                    "valor_total":     float(r.valor_total or 0),
                                    "fornecedor":      str(r.fornecedor or "").strip(),
                                }
                                for r in receb_extra_raw
                                if str(r.fornecedor or "").strip()
                            ]
                            receb.extend(extra_com_forn)
                            logger.info(f"Recebimentos extra: {len(extra_com_forn)} itens com fornecedor adicionados")
                    except Exception as e:
                        logger.warning(f"Recebimentos extra (730d) erro: {e}")

                # Envia em lotes de 500 (servidor faz UPSERT puro, sem DELETE)
                LOTE = 500
                total_enviados = 0
                falhou = False
                total_lotes = max(1, (len(receb) + LOTE - 1) // LOTE)
                for i in range(0, max(len(receb), 1), LOTE):
                    lote = receb[i:i + LOTE]
                    if not lote:
                        break
                    lote_idx = i // LOTE
                    if not post("/api/sync/recebimentos", {
                        "empresa_id": EMPRESA_ID, "filial_id": FILIAL_ID,
                        "recebimentos": lote,
                        "lote_idx": lote_idx,
                        "total_lotes": total_lotes,
                    }):
                        falhou = True
                        break
                    total_enviados += len(lote)
                    logger.info(f"Recebimentos lote {lote_idx+1}/{total_lotes}: {len(lote)} itens (total {total_enviados})")
                if falhou:
                    erros += 1
                else:
                    logger.info(f"Recebimentos concluidos: {total_enviados} itens")
                    if recebimentos_full:
                        _marcar_feito_hoje("recebimentos_full_180d")
            except Exception as e:
                logger.error(f"Erro ao ler recebimentos: {e}")
                erros += 1

            # ── 6. CONTAS A PAGAR / BOLETOS (a partir de 01/06/2026) ────────
            logger.info("Lendo contas a pagar...")
            try:
                DATA_INICIO_BOLETOS = date(2026, 6, 1)
                cp_raw = reader.ler_contas_pagar(
                    data_inicio=DATA_INICIO_BOLETOS,
                    filial_id=FILIAL_ID,
                )
                if not post("/api/sync/contas-pagar", {"empresa_id": EMPRESA_ID, "filial_id": FILIAL_ID, "contas": cp_raw}):
                    erros += 1
                else:
                    logger.info(f"Contas a pagar enviadas: {len(cp_raw)}")
            except Exception as e:
                logger.error(f"Erro ao ler contas a pagar: {e}")
                erros += 1

    except Exception as e:
        logger.error(f"Erro de conexao Farmasoft: {e}")
        post("/api/sync/status", {
            "empresa_id": EMPRESA_ID, "filial_id": FILIAL_ID,
            "em_curso": False,
            "erro": str(e),
            "versao_agent": VERSAO,
        })
        return False

    # Notificar cloud que sync concluiu
    post("/api/sync/status", {
        "empresa_id": EMPRESA_ID, "filial_id": FILIAL_ID,
        "em_curso": False,
        "erro": f"{erros} erros" if erros else None,
        "versao_agent": VERSAO,
    })

    logger.info(f"Sync concluido | erros={erros}")

    # Salva estado mesmo com erros parciais para evitar loop de sync completo.
    # Produtos so sao marcados se nao houve erros (garantia de integridade).
    salvar_estado_sync(incluiu_produtos=(sync_completo and erros == 0))

    # Verificar se meta bonus do dia foi batida (envia alerta uma vez por dia)
    try:
        url = f"{CLOUD_URL.rstrip('/')}/api/telegram/verificar-alerta-bonus?filial_id={FILIAL_ID}&empresa_id={EMPRESA_ID}"
        r = requests.post(url, headers=headers(), timeout=30)
        if r.status_code == 200:
            status = r.json().get("status", "")
            if status == "alerta_enviado":
                logger.info("Alerta de meta bonus enviado ao gerente")
            else:
                logger.info(f"Verificacao bonus: {status}")
    except Exception as e:
        logger.warning(f"Erro ao verificar alerta bonus: {e}")

    # Verificar se meta mensal atingiu 90% ou 100% (envia alerta uma vez por gatilho/mes)
    try:
        url = f"{CLOUD_URL.rstrip('/')}/api/telegram/verificar-alerta-meta-mensal?filial_id={FILIAL_ID}&empresa_id={EMPRESA_ID}"
        r = requests.post(url, headers=headers(), timeout=30)
        if r.status_code == 200:
            status = r.json().get("status", "")
            if status == "alerta_enviado":
                gatilho = r.json().get("gatilho", "")
                pct = r.json().get("pct", 0)
                logger.info(f"Alerta meta mensal {gatilho}% enviado ({pct}%)")
            else:
                logger.info(f"Verificacao meta mensal: {status}")
    except Exception as e:
        logger.warning(f"Erro ao verificar alerta meta mensal: {e}")

    return erros == 0


WEBHOOK_PORT = int(os.getenv("AGENT_WEBHOOK_PORT", "5001"))

# flag para evitar sync simultaneo
_sync_lock = threading.Lock()

CACHE_RECOMPUTE_THROTTLE_SEC = 60  # piso de seguranca -- ver docstring


def _recomputar_cache_cloud():
    """Invalida (NAO recalcula) o cache dos endpoints pesados apos o sync.

    Ate 12/09/2026 isso era throttlado a 1x/hora: o recalculo (cache-aside,
    paga quem abrir o dashboard logo depois) custava ~20-60s de queries
    pesadas contra o Supabase (Sao Paulo, longe do backend na Europa), e cada
    invalidacao multiplicava egress la. Com o banco primario local (mesmo
    host do backend, ver DECISIONS.md/memoria de infra), esse recalculo caro
    caiu pra ~1-2s e nao ha mais egress a poupar -- entao aqui so mantemos um
    piso curto (60s) contra rajadas (ex: sync manual repetido), nao mais um
    throttle real de 1h. TTL de 30min (max_age_sec em compras.py) segue como
    garantia de que os dados nunca ficam mais velhos que isso de qualquer
    forma, mesmo se essa invalidacao falhar.
    """
    if not CLOUD_URL or (not AGENT_KEY and not AGENT_TOKEN):
        return
    try:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        ultimo_iso = state.get("cache_recompute_em")
        if ultimo_iso:
            elapsed = (datetime.now() - datetime.fromisoformat(ultimo_iso)).total_seconds()
            if 0 <= elapsed < CACHE_RECOMPUTE_THROTTLE_SEC:
                logger.info(f"[cache/recomputar] throttle ativo ({elapsed:.0f}s < {CACHE_RECOMPUTE_THROTTLE_SEC}s), pulando")
                return
    except Exception as e:
        logger.debug(f"[cache/recomputar] erro ao checar throttle, seguindo sem throttle: {e}")

    try:
        url = f"{CLOUD_URL.rstrip('/')}/api/cache/recomputar?filial_id={FILIAL_ID}&empresa_id={EMPRESA_ID}"
        r = requests.post(url, headers=headers(), timeout=TIMEOUT_HTTP)
        if r.status_code == 200:
            data = r.json()
            logger.info(f"[cache/recomputar] ok={data.get('ok')} erros={data.get('erros')}")
            try:
                state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
                state["cache_recompute_em"] = datetime.now().isoformat()
                STATE_FILE.write_text(json.dumps(state))
            except Exception as e_state:
                logger.debug(f"[cache/recomputar] erro ao gravar throttle: {e_state}")
        else:
            logger.warning(f"[cache/recomputar] status={r.status_code}")
    except Exception as e:
        logger.warning(f"[cache/recomputar] erro: {e}")


def _fazer_sync_thread(dias):
    """Executa sync em thread separada (nao bloqueia o webhook)."""
    if _sync_lock.locked():
        logger.info("Sync ja em curso, ignorando trigger")
        return
    with _sync_lock:
        sincronizar(dias_vendas=dias)
    _recomputar_cache_cloud()


def _buscar_produtos_farmasoft(termo: str) -> list:
    """
    Busca produtos no Farmasoft pelo termo (DESCRICAO, PRINCIPIOATIVO ou CD_PRODUTO).
    Retorna lista de dicts. Chamada direta ao Firebird, apenas leitura.
    """
    from backend.utils.database import FarmasoftConnection

    estoque_campo = f"ESTOQUE_{FILIAL_ID}" if FILIAL_ID <= 30 else "ESTOQUE_1"
    custo_campo   = f"CUSTO_UNITARIO_{FILIAL_ID}" if FILIAL_ID <= 30 else "CUSTO_UNITARIO"
    termo_upper   = termo.upper().strip().replace("'", "''")  # escape basico

    query = f"""
        SELECT FIRST 40
            p.ID_PRODUTO,
            p.CD_PRODUTO,
            p.DESCRICAO,
            COALESCE(p.PRINCIPIOATIVO, '') as PRINCIPIO_ATIVO,
            COALESCE(l.NOME, '') as LABORATORIO,
            COALESCE(p.{estoque_campo}, 0) as ESTOQUE,
            COALESCE(p.{custo_campo}, p.CUSTO_UNITARIO, p.CUSTO_MEDIO, 0) as CUSTO_UNITARIO,
            COALESCE(p.PRECO_VENDA, 0) as PRECO_VENDA
        FROM PRODUTOS p
        LEFT JOIN LABORATORIOS l ON p.CD_LABORATORIO = l.CD_LABORATORIO
        WHERE p.STATUS = 'A'
          AND (
            UPPER(p.DESCRICAO) CONTAINING '{termo_upper}'
            OR UPPER(COALESCE(p.PRINCIPIOATIVO,'')) CONTAINING '{termo_upper}'
            OR UPPER(COALESCE(p.CD_PRODUTO,'')) CONTAINING '{termo_upper}'
          )
        ORDER BY p.DESCRICAO
    """

    conn = FarmasoftConnection()  # usa settings.farmasoft do .env
    if not conn.conectar():
        raise RuntimeError("Nao foi possivel conectar ao Farmasoft")
    try:
        rows = conn.executar_select(query)
        return [
            {
                "id_produto":      int(r.get("ID_PRODUTO") or 0),
                "cd_produto":      str(r.get("CD_PRODUTO") or ""),
                "descricao":       str(r.get("DESCRICAO") or ""),
                "principio_ativo": str(r.get("PRINCIPIO_ATIVO") or ""),
                "laboratorio":     str(r.get("LABORATORIO") or ""),
                "estoque_atual":   float(r.get("ESTOQUE") or 0),
                "custo_unitario":  float(r.get("CUSTO_UNITARIO") or 0),
                "preco_venda":     float(r.get("PRECO_VENDA") or 0),
            }
            for r in rows
        ]
    finally:
        conn.desconectar()


class WebhookHandler(BaseHTTPRequestHandler):
    """Handler HTTP para trigger de sync e busca de produtos em tempo real."""

    dias_vendas = 180

    def do_GET(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)
        path   = parsed.path

        if path == "/health":
            self._responder(200, "ok")

        elif path == "/buscar":
            # GET /buscar?termo=X  (chave em X-Agent-Key)
            chave = self.headers.get("X-Agent-Key", "")
            if chave != AGENT_KEY:
                self._responder_json(401, {"erro": "Chave invalida"})
                return
            termo = qs.get("termo", [""])[0].strip()
            if len(termo) < 2:
                self._responder_json(400, {"erro": "Termo deve ter pelo menos 2 caracteres"})
                return
            try:
                produtos = _buscar_produtos_farmasoft(termo)
                self._responder_json(200, {
                    "empresa_id": EMPRESA_ID, "filial_id": FILIAL_ID,
                    "fonte": "farmasoft_realtime",
                    "total": len(produtos),
                    "produtos": produtos,
                })
            except Exception as e:
                logger.error(f"Erro buscar produtos: {e}")
                self._responder_json(500, {"erro": str(e)})

        else:
            self._responder(404, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)

        if parsed.path != "/sync":
            self._responder(404, "Not found")
            return

        chave = self.headers.get("X-Agent-Key", "")
        if chave != AGENT_KEY:
            self._responder(401, "Chave invalida")
            return

        threading.Thread(
            target=_fazer_sync_thread,
            args=(self.dias_vendas,),
            daemon=True
        ).start()
        self._responder(200, "Sync iniciado")

    def _responder(self, codigo, msg):
        body = msg.encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _responder_json(self, codigo, dados):
        body = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.info("Webhook: " + fmt % args)


def main():
    parser = argparse.ArgumentParser(description="Agent Gerencial - Sync Farmasoft -> Cloud")
    parser.add_argument("--loop",    action="store_true", help="Rodar em loop continuo")
    parser.add_argument("--webhook", action="store_true", help="Servidor HTTP para trigger remoto")
    parser.add_argument("--dias",    type=int, default=None, help="Forcar N dias (ignora calculo automatico)")
    parser.add_argument("--reset",   action="store_true", help="Apagar historico local e fazer carga completa (180 dias)")
    parser.add_argument("--migrate", action="store_true", help="Criar/atualizar tabelas no banco Neon e sair")
    args = parser.parse_args()

    if args.migrate:
        if not CLOUD_URL:
            logger.error("CLOUD_API_URL nao configurado no .env")
            sys.exit(1)
        if not AGENT_KEY:
            logger.error("AGENT_API_KEY nao configurado no .env")
            sys.exit(1)
        # Tenta conexao direta se DATABASE_URL estiver disponivel
        db_url = os.getenv("DATABASE_URL", "")
        if db_url:
            import psycopg2
            logger.info("Conectando diretamente ao banco via DATABASE_URL...")
            sqls = [
                """CREATE TABLE IF NOT EXISTS sync_contas_pagar (
                    id BIGSERIAL PRIMARY KEY, filial_id INTEGER NOT NULL,
                    cd_contas_pagar INTEGER NOT NULL, cd_distribuidor INTEGER,
                    fornecedor TEXT, numero_nf TEXT, dt_nota DATE,
                    dt_vencimento DATE NOT NULL, valor NUMERIC(12,2) DEFAULT 0,
                    vl_saldo NUMERIC(12,2) DEFAULT 0, codigo_barras TEXT,
                    banco TEXT, historico TEXT, pago BOOLEAN DEFAULT FALSE,
                    pago_em TIMESTAMP, pago_por TEXT, synced_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE (filial_id, cd_contas_pagar))""",
                "CREATE INDEX IF NOT EXISTS idx_sync_cp_filial_venc ON sync_contas_pagar (filial_id, dt_vencimento)",
                "CREATE INDEX IF NOT EXISTS idx_sync_cp_pago ON sync_contas_pagar (filial_id, pago, dt_vencimento)",
            ]
            try:
                conn = psycopg2.connect(db_url)
                conn.autocommit = True
                cur = conn.cursor()
                for sql in sqls:
                    cur.execute(sql)
                cur.close()
                conn.close()
                logger.info("Migrations concluidas com sucesso.")
            except Exception as e:
                logger.error(f"Erro: {e}")
                sys.exit(1)
            sys.exit(0)

        # Fallback: chama via API HTTP
        if not CLOUD_URL or (not AGENT_KEY and not AGENT_TOKEN):
            logger.error("Configure DATABASE_URL ou CLOUD_API_URL+AGENT_API_KEY (ou AGENT_TOKEN) no .env")
            sys.exit(1)
        base = CLOUD_URL.rstrip('/')
        logger.info("Acordando servidor Render (pode levar ~60s)...")
        for _ in range(3):
            try:
                r = requests.get(f"{base}/health", timeout=90)
                if r.status_code < 500:
                    logger.info("Servidor acordado.")
                    break
            except Exception:
                pass
        logger.info("Executando migrations via API...")
        try:
            r = requests.post(
                f"{base}/api/admin/migrate",
                headers={"X-Admin-Secret": AGENT_KEY},
                timeout=90,
            )
            if r.status_code == 200:
                logger.info(f"Migrations OK: {r.json().get('mensagem', r.text)}")
            elif r.status_code == 404:
                logger.error("Endpoint nao encontrado. Render ainda nao deployou o novo codigo.")
                logger.error("Solucao: va ao painel Render e clique em 'Manual Deploy'.")
                sys.exit(1)
            else:
                logger.error(f"Erro {r.status_code}: {r.text}")
                sys.exit(1)
        except Exception as e:
            logger.error(f"Erro: {e}")
            sys.exit(1)
        sys.exit(0)

    if args.reset:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        logger.info("Historico de sync apagado. Proxima execucao fara carga completa.")
        if not args.loop and not args.webhook:
            sys.exit(0)

    if args.webhook:
        logger.info(f"Webhook ativo na porta {WEBHOOK_PORT} | POST /sync (X-Agent-Key header)")
        threading.Thread(target=_loop_background, args=(args.dias,), daemon=True).start()
        if DUCKDNS_DOMAIN and DUCKDNS_TOKEN:
            _atualizar_duckdns()
            threading.Thread(target=_loop_duckdns, daemon=True).start()
            logger.info(f"DuckDNS ativo: {DUCKDNS_DOMAIN}.duckdns.org a cada {DUCKDNS_INTERVALO//60} min")
        server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Agent encerrado pelo usuario")
            server.shutdown()

    elif args.loop:
        logger.info(f"Modo loop: sincronizando a cada {INTERVALO//60} minutos")
        while True:
            if _pausa_noturna():
                continue
            dias = args.dias if args.dias else calcular_dias_sync()
            sincronizar(dias_vendas=dias)
            _recomputar_cache_cloud()
            _verificar_relatorio_agendado()
            _verificar_alerta_boletos()
            _verificar_limpeza_retencao()
            _verificar_auto_update()
            logger.info(f"Aguardando {INTERVALO//60} minutos...")
            time.sleep(INTERVALO)
    else:
        dias = args.dias if args.dias else calcular_dias_sync()
        ok = sincronizar(dias_vendas=dias)
        sys.exit(0 if ok else 1)


def _encontrar_repo_git(start: Path) -> Path:
    """Sobe ate 4 niveis procurando a raiz do repo git. Retorna None se nao encontrar."""
    current = start.resolve()
    for _ in range(5):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _verificar_auto_update():
    """Verifica uma vez ao dia se ha nova versao no repo remoto.
    Se houver, faz git pull e encerra com sys.exit(0) -- o NSSM reinicia
    automaticamente com o codigo novo."""
    if _ja_feito_hoje("auto_update_verificado"):
        return
    _marcar_feito_hoje("auto_update_verificado")
    try:
        repo = _encontrar_repo_git(BASE_DIR)
        if not repo:
            logger.info("[auto-update] nao e um repo git, pulando verificacao de atualizacao")
            return
        fetch = subprocess.run(
            ["git", "fetch", "origin", "master"],
            cwd=repo, timeout=30, capture_output=True, text=True
        )
        if fetch.returncode != 0:
            logger.warning(f"[auto-update] git fetch falhou: {fetch.stderr.strip()}")
            return
        local  = subprocess.check_output(["git", "rev-parse", "HEAD"],          cwd=repo).decode().strip()
        remote = subprocess.check_output(["git", "rev-parse", "origin/master"], cwd=repo).decode().strip()
        if local == remote:
            logger.info(f"[auto-update] ja na versao mais recente ({local[:8]})")
            return
        logger.info(f"[auto-update] nova versao detectada: {local[:8]} -> {remote[:8]}")
        result = subprocess.run(
            ["git", "pull", "origin", "master"],
            cwd=repo, timeout=60, capture_output=True, text=True
        )
        if result.returncode != 0:
            logger.warning(f"[auto-update] git pull falhou: {result.stderr.strip()}")
            return
        logger.info("[auto-update] git pull concluido, reiniciando via NSSM...")
        sys.exit(0)
    except Exception as e:
        logger.warning(f"[auto-update] erro: {e}")


def _verificar_relatorio_agendado():
    """Envia relatorio ao gerente se horario configurado foi atingido (uma vez por dia)."""
    if _ja_feito_hoje("relatorio_agendado_enviado"):
        return
    try:
        url = f"{CLOUD_URL.rstrip('/')}/api/loja/config?filial_id={FILIAL_ID}"
        r = requests.get(url, headers=headers(), timeout=10)
        if r.status_code != 200:
            return
        horario = r.json().get("horario_relatorio") or ""
        if not horario:
            return
        agora = datetime.now().strftime("%H:%M")
        h_conf = datetime.strptime(horario, "%H:%M")
        h_agora = datetime.strptime(agora, "%H:%M")
        if h_agora >= h_conf:
            url2 = f"{CLOUD_URL.rstrip('/')}/api/telegram/enviar-relatorio-gerente?filial_id={FILIAL_ID}&empresa_id={EMPRESA_ID}"
            r2 = requests.post(url2, headers=headers(), timeout=60)
            if r2.status_code == 200:
                _marcar_feito_hoje("relatorio_agendado_enviado")
                logger.info(f"Relatorio agendado enviado ({horario})")
            else:
                logger.warning(f"Erro ao enviar relatorio agendado: {r2.status_code} {r2.text[:100]}")
    except Exception as e:
        logger.warning(f"Erro verificar relatorio agendado: {e}")


BOLETOS_ALERTA_HORA = "08:00"


def _verificar_alerta_boletos():
    """Envia alerta Telegram com boletos do dia, uma vez por dia as 08:00."""
    if _ja_feito_hoje("boletos_alerta_enviado"):
        return
    agora = datetime.now().strftime("%H:%M")
    if datetime.strptime(agora, "%H:%M") < datetime.strptime(BOLETOS_ALERTA_HORA, "%H:%M"):
        return
    try:
        url = f"{CLOUD_URL.rstrip('/')}/api/boletos/alerta-telegram?filial_id={FILIAL_ID}"
        r = requests.post(url, headers=headers(), timeout=30)
        if r.status_code == 200:
            d = r.json()
            _marcar_feito_hoje("boletos_alerta_enviado")
            if d.get("total_boletos", 0) > 0:
                logger.info(f"Alerta boletos enviado: {d['total_boletos']} boleto(s) R$ {d.get('valor_total', 0):.2f}")
            else:
                logger.info("Alerta boletos: nenhum vencimento hoje")
        else:
            logger.warning(f"Erro alerta boletos: {r.status_code}")
    except Exception as e:
        logger.warning(f"Erro verificar alerta boletos: {e}")


def _verificar_limpeza_retencao():
    """Purga registros mortos (compras_pendentes EXPIRADO, telegram_log, chaves de
    alerta em configuracoes) uma vez por dia. Nao roda a cada sync -- e manutencao,
    nao invalidacao de cache."""
    if _ja_feito_hoje("limpeza_retencao_executada"):
        return
    try:
        url = f"{CLOUD_URL.rstrip('/')}/api/cache/limpeza-retencao?filial_id={FILIAL_ID}&empresa_id={EMPRESA_ID}"
        r = requests.post(url, headers=headers(), timeout=30)
        if r.status_code == 200:
            _marcar_feito_hoje("limpeza_retencao_executada")
            logger.info(f"Limpeza de retencao ok: {r.json().get('resultado')}")
        else:
            logger.warning(f"Erro limpeza de retencao: {r.status_code}")
    except Exception as e:
        logger.warning(f"Erro verificar limpeza de retencao: {e}")


def _pausa_noturna():
    """
    Retorna True se o horario atual esta na janela de pausa (0h-5h).
    Quando em pausa, dorme ate as 05:00 e retorna True para o chamador pular o ciclo.
    """
    import datetime as _dt
    agora = _dt.datetime.now()
    if 0 <= agora.hour < 5:
        acordar = agora.replace(hour=5, minute=0, second=0, microsecond=0)
        segundos = (acordar - agora).total_seconds()
        logger.info(f"Pausa noturna ativa (0h-5h). Dormindo {int(segundos//60)} min ate 05:00...")
        time.sleep(segundos)
        return True
    return False


def _loop_background(dias_fixo):
    """Loop de sync automatico rodando em background junto com o webhook."""
    while True:
        if _pausa_noturna():
            continue
        dias = dias_fixo if dias_fixo else calcular_dias_sync()
        _fazer_sync_thread(dias)
        _verificar_relatorio_agendado()
        _verificar_alerta_boletos()
        _verificar_limpeza_retencao()
        logger.info(f"Proximo sync automatico em {INTERVALO//60} minutos")
        time.sleep(INTERVALO)


if __name__ == "__main__":
    main()
