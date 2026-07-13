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
    POST /sync?key=CHAVE      - dispara sync manual
    GET  /buscar?termo=X&key=CHAVE  - busca produto em tempo real no Farmasoft
"""
import os
import sys
import json
import time
import logging
import argparse
import threading
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
PID_FILE    = Path(__file__).resolve().parent / "agent.pid"
DIAS_PRIMEIRA_CARGA = 180
DIAS_MAX_INCREMENTAL = 5   # maximo de dias no sync incremental
DIAS_MARGEM = 2            # margem de seguranca (reprocessa N dias atras)


_hora_universal_cache: datetime | None = None


def _obter_hora_universal(forcar: bool = False) -> datetime:
    """
    Fonte de tempo universal para todo o ciclo de sync.
    Consulta /api/server-time (PostgreSQL Render) uma vez por ciclo e guarda em cache.
    Assim o relogio local do PC nunca interfere em nenhuma decisao de data/hora.
    Fallback: datetime.now() apenas se a API estiver indisponivel.
    """
    global _hora_universal_cache
    if _hora_universal_cache is not None and not forcar:
        return _hora_universal_cache
    try:
        r = requests.get(
            f"{CLOUD_URL.rstrip('/')}/api/server-time",
            timeout=5,
        )
        if r.status_code == 200:
            dados  = r.json()
            dt_str = dados.get("timestamp", "")
            tz_off = dados.get("tz_offset", -3)
            dt     = datetime.fromisoformat(dt_str.split("+")[0].split("Z")[0])
            local  = datetime.now()
            delta_m = abs((dt - local).total_seconds() / 60)
            if delta_m > 5:
                logger.warning(
                    f"RELOGIO PC DIVERGE {delta_m:.0f}min do servidor "
                    f"(pc={local.strftime('%H:%M')} servidor={dt.strftime('%H:%M')} "
                    f"UTC{tz_off:+d}). Usando hora do servidor para todo o sync."
                )
            _hora_universal_cache = dt
            return dt
    except Exception as e:
        logger.debug(f"server-time indisponivel ({e}) - usando relogio local")
    _hora_universal_cache = datetime.now()
    return _hora_universal_cache


def _data_hoje() -> date:
    return _obter_hora_universal().date()


def calcular_dias_sync() -> int:
    """
    Retorna quantos dias sincronizar.
    Usa hora universal (servidor online) para comparar com ultimo_sync gravado.
    Assim o relogio local nunca interfere no calculo.
    """
    try:
        if STATE_FILE.exists():
            state  = json.loads(STATE_FILE.read_text())
            ultimo = datetime.fromisoformat(state.get("ultimo_sync", ""))
            agora  = _obter_hora_universal()           # hora confiavel do servidor
            delta_s = (ultimo - agora).total_seconds()
            if delta_s > 600:                          # > 10min no futuro = relogio PC divergente
                logger.warning(
                    f"ultimo_sync ({ultimo.strftime('%d/%m/%Y %H:%M')}) esta {int(delta_s/60)}min "
                    f"no futuro (relogio PC atrasado?). Sync incremental conservador ({DIAS_MARGEM}d)."
                )
                return DIAS_MARGEM                     # seguro: apenas re-sincroniza dias recentes
            dias_passados = (agora - ultimo).days + DIAS_MARGEM
            dias = max(DIAS_MARGEM, min(dias_passados, DIAS_MAX_INCREMENTAL))
            logger.info(f"Sync incremental: {dias} dias (ultimo sync: {ultimo.strftime('%d/%m %H:%M')})")
            return dias
    except Exception:
        pass
    logger.info(f"Primeiro sync ou estado invalido: carregando {DIAS_PRIMEIRA_CARGA} dias")
    return DIAS_PRIMEIRA_CARGA


def salvar_estado_sync(incluiu_produtos: bool = False):
    """Grava timestamp do sync usando hora universal (servidor), nao o relogio local."""
    try:
        agora_universal = _obter_hora_universal().isoformat()
        state = {}
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text())
        state["ultimo_sync"] = agora_universal
        if incluiu_produtos:
            state["ultimo_sync_produtos"] = agora_universal
        STATE_FILE.write_text(json.dumps(state))
    except Exception as e:
        logger.warning(f"Nao foi possivel salvar sync_state.json: {e}")


def deve_sincronizar_produtos() -> bool:
    """Produtos (metadata completa) so sincronizam uma vez a cada 6 horas."""
    try:
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text())
            ultimo = state.get("ultimo_sync_produtos")
            if ultimo:
                agora    = _obter_hora_universal()
                dt_ultimo = datetime.fromisoformat(ultimo)
                if dt_ultimo > agora:
                    logger.warning("ultimo_sync_produtos no futuro - forcando resync de metadata.")
                    return True
                horas = (agora - dt_ultimo).total_seconds() / 3600
                if horas < 6:
                    logger.info(f"Metadata produtos em cache ({horas:.1f}h) - usando sync rapido de estoque")
                    return False
    except Exception:
        pass
    return True


def headers():
    return {
        "X-Agent-Key": AGENT_KEY,
        "Content-Type": "application/json",
    }


def post(endpoint: str, payload: dict, tentativas: int = 3) -> bool:
    url = f"{CLOUD_URL.rstrip('/')}{endpoint}"
    payload["agent_key"] = AGENT_KEY
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


def _ping_api():
    """Acorda o banco (Neon serverless) antes da sync para evitar cold start nas queries."""
    try:
        requests.get(f"{CLOUD_URL.rstrip('/')}/health", timeout=15)
    except Exception:
        pass


def sincronizar(dias_vendas: int = 180):
    logger.info(f"Iniciando sync | filial={FILIAL_ID} | dias={dias_vendas}")
    _ping_api()

    if not CLOUD_URL:
        logger.error("CLOUD_API_URL nao configurado no .env")
        return False

    if not AGENT_KEY:
        logger.error("AGENT_API_KEY nao configurado no .env")
        return False

    try:
        from backend.utils.database import farmasoft_connection
        from backend.etl.farmasoft_reader import FarmasoftReader
    except ImportError as e:
        logger.error(f"Erro ao importar backend: {e}")
        return False

    global _hora_universal_cache
    _hora_universal_cache = None   # limpa para renovar neste ciclo

    # Notificar cloud que sync iniciou
    post("/api/sync/status", {
        "filial_id": FILIAL_ID,
        "em_curso": True,
        "versao_agent": VERSAO,
    })

    erros = 0
    sync_completo = False

    try:
        with farmasoft_connection() as conn:
            reader = FarmasoftReader(conn)

            # ── HORA DE REFERENCIA: Farmasoft e sempre correto (obrigacao legal NF) ──
            try:
                ts_rows = conn.executar_select(
                    "SELECT CAST(CURRENT_TIMESTAMP AS TIMESTAMP) AS TS FROM RDB$DATABASE"
                )
                if ts_rows and ts_rows[0].get("TS"):
                    ts = ts_rows[0]["TS"]
                    dt_farma = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
                    delta_m = abs((dt_farma - datetime.now()).total_seconds() / 60)
                    if delta_m > 2:
                        logger.warning(
                            f"RELOGIO PC DIVERGE {delta_m:.0f}min do Farmasoft "
                            f"(pc={datetime.now().strftime('%H:%M')} farmasoft={dt_farma.strftime('%H:%M')}). "
                            f"Usando hora do Farmasoft como referencia."
                        )
                    _hora_universal_cache = dt_farma
                    logger.info(f"Hora referencia: Farmasoft {dt_farma.strftime('%d/%m/%Y %H:%M:%S')}")
            except Exception as e:
                logger.debug(f"Hora Farmasoft indisponivel ({e}) - usando fallback")
                # fallback: tenta servidor cloud (ja pode estar acordado apos _ping_api)
                _hora_universal_cache = None
                _obter_hora_universal()

            hoje = _hora_universal_cache.date() if _hora_universal_cache else date.today()
            data_inicio_vendas = hoje - timedelta(days=dias_vendas)
            data_inicio_compras = hoje - timedelta(days=90)
            data_inicio_30d  = hoje - timedelta(days=30)
            data_inicio_180d = hoje - timedelta(days=180)

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
                    LOTE_P = 200
                    ok_prod = True
                    total_prod = len(produtos)
                    _ping_api()  # acorda Render apos leitura longa do Farmasoft
                    for i in range(0, total_prod, LOTE_P):
                        if not post("/api/sync/produtos", {
                            "filial_id": FILIAL_ID,
                            "produtos": produtos[i:i+LOTE_P],
                        }):
                            ok_prod = False
                            erros += 1
                    if ok_prod:
                        # Cleanup: remove inativos apos todos os lotes enviados
                        ids_ativos = [p["id_produto"] for p in produtos]
                        post("/api/sync/produtos/cleanup", {
                            "filial_id": FILIAL_ID,
                            "ids_ativos": ids_ativos,
                        })
                        logger.info(f"Produtos enviados: {total_prod}")
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
                            "filial_id": FILIAL_ID,
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
                # Enviar em lotes de 200 para nao estourar o HTTP
                LOTE = 200
                for i in range(0, len(vendas), LOTE):
                    lote = vendas[i:i+LOTE]
                    if not post("/api/sync/vendas", {
                        "filial_id": FILIAL_ID,
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
                    if not post("/api/sync/saidas-validade", {
                        "filial_id": FILIAL_ID,
                        "data_inicio": str(data_inicio_vendas),
                        "saidas": saidas_venc,
                    }):
                        erros += 1
                logger.info(f"Saidas validade enviadas: {len(saidas_venc)}")
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
                        "filial_id": FILIAL_ID,
                        "data_inicio": str(data_inicio_vendas),
                        "lote": i // LOTE_B,
                        "balconistas": balconistas[i:i+LOTE_B],
                    }):
                        erros += 1
                logger.info(f"Balconistas enviados: {len(balconistas)}")
            except Exception as e:
                logger.error(f"Erro ao ler balconistas: {e}")
                erros += 1

            # ── 4. COMPRAS (ultimos 90 dias) ────────────────────────────────
            logger.info("Lendo compras...")
            try:
                compras_raw = reader.ler_compras_por_nota(
                    data_inicio=data_inicio_compras,
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
                if not post("/api/sync/compras", {"filial_id": FILIAL_ID, "compras": compras}):
                    erros += 1
                else:
                    logger.info(f"Compras enviadas: {len(compras)}")
            except Exception as e:
                logger.error(f"Erro ao ler compras: {e}")
                erros += 1

            # ── 4. TRANSFERENCIAS (ultimos 30 dias) ─────────────────────────
            logger.info("Lendo transferencias...")
            try:
                transf_raw = reader.ler_transferencias(
                    data_inicio=data_inicio_30d,
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
                if not post("/api/sync/transferencias", {"filial_id": FILIAL_ID, "transferencias": transf}):
                    erros += 1
                else:
                    logger.info(f"Transferencias enviadas: {len(transf)}")
            except Exception as e:
                logger.error(f"Erro ao ler transferencias: {e}")
                erros += 1

            # ── 5. RECEBIMENTOS (ultimos 180 dias, em lotes de 30d) ─────────
            logger.info("Lendo recebimentos...")
            try:
                receb_raw = reader.ler_recebimentos_periodo(
                    data_inicio=data_inicio_180d,
                    data_fim=hoje,
                    filial_id=FILIAL_ID,
                )
                receb = []
                for r in receb_raw:
                    d = r.data_emissao
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
                        "fornecedor":      str(r.fornecedor or ""),
                    })
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
                        "filial_id": FILIAL_ID,
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
                if not post("/api/sync/contas-pagar", {"filial_id": FILIAL_ID, "contas": cp_raw}):
                    erros += 1
                else:
                    logger.info(f"Contas a pagar enviadas: {len(cp_raw)}")
            except Exception as e:
                logger.error(f"Erro ao ler contas a pagar: {e}")
                erros += 1

    except Exception as e:
        logger.error(f"Erro de conexao Farmasoft: {e}")
        post("/api/sync/status", {
            "filial_id": FILIAL_ID,
            "em_curso": False,
            "erro": str(e),
            "versao_agent": VERSAO,
        })
        return False

    # Notificar cloud que sync concluiu
    post("/api/sync/status", {
        "filial_id": FILIAL_ID,
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
        url = f"{CLOUD_URL.rstrip('/')}/api/telegram/verificar-alerta-bonus?filial_id={FILIAL_ID}"
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
        url = f"{CLOUD_URL.rstrip('/')}/api/telegram/verificar-alerta-meta-mensal?filial_id={FILIAL_ID}"
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


def _fazer_sync_thread(dias):
    """Executa sync em thread separada (nao bloqueia o webhook)."""
    if _sync_lock.locked():
        logger.info("Sync ja em curso, ignorando trigger")
        return
    with _sync_lock:
        sincronizar(dias_vendas=dias)


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
            # GET /buscar?termo=X&key=AGENT_KEY
            chave = qs.get("key", [""])[0]
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
                    "filial_id": FILIAL_ID,
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

        chave = qs.get("key", [""])[0]
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


def _adquirir_pid_lock() -> bool:
    """
    Impede que duas instancias do agente rodem ao mesmo tempo.
    Retorna True se pode continuar, False se outra instancia ja esta ativa.
    """
    pid_atual = os.getpid()
    if PID_FILE.exists():
        try:
            pid_antigo = int(PID_FILE.read_text().strip())
            if pid_antigo != pid_atual:
                # Verifica se o processo ainda existe (Windows)
                import ctypes
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_antigo)
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
                    logger.warning(
                        f"Outra instancia do agente ja esta rodando (PID {pid_antigo}). "
                        f"Encerrando esta instancia (PID {pid_atual})."
                    )
                    return False
        except Exception:
            pass  # PID invalido ou processo morto — pode continuar
    PID_FILE.write_text(str(pid_atual))
    return True


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
        if not CLOUD_URL or not AGENT_KEY:
            logger.error("Configure DATABASE_URL ou CLOUD_API_URL+AGENT_API_KEY no .env")
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

    # Impede segunda instancia (loop e webhook sao de longa duracao)
    if args.loop or args.webhook:
        if not _adquirir_pid_lock():
            sys.exit(1)

    if args.webhook:
        logger.info(f"Webhook ativo na porta {WEBHOOK_PORT} | POST /sync?key=***")
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
            dias = args.dias if args.dias else calcular_dias_sync()
            sincronizar(dias_vendas=dias)
            _verificar_relatorio_agendado()
            _verificar_alerta_boletos()
            logger.info(f"Aguardando {INTERVALO//60} minutos...")
            time.sleep(INTERVALO)
    else:
        dias = args.dias if args.dias else calcular_dias_sync()
        ok = sincronizar(dias_vendas=dias)
        sys.exit(0 if ok else 1)


_relatorio_enviado_hoje: str = ""


def _verificar_relatorio_agendado():
    """Envia relatorio ao gerente se horario configurado foi atingido (uma vez por dia)."""
    global _relatorio_enviado_hoje
    hoje = date.today().isoformat()
    if _relatorio_enviado_hoje == hoje:
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
            url2 = f"{CLOUD_URL.rstrip('/')}/api/telegram/enviar-relatorio-gerente?filial_id={FILIAL_ID}"
            r2 = requests.post(url2, headers=headers(), timeout=60)
            if r2.status_code == 200:
                _relatorio_enviado_hoje = hoje
                logger.info(f"Relatorio agendado enviado ({horario})")
            else:
                logger.warning(f"Erro ao enviar relatorio agendado: {r2.status_code} {r2.text[:100]}")
    except Exception as e:
        logger.warning(f"Erro verificar relatorio agendado: {e}")


_boletos_alerta_enviado: str = ""

BOLETOS_ALERTA_HORA = "08:00"


def _verificar_alerta_boletos():
    """Envia alerta Telegram com boletos do dia, uma vez por dia as 08:00."""
    global _boletos_alerta_enviado
    hoje = date.today().isoformat()
    if _boletos_alerta_enviado == hoje:
        return
    agora = datetime.now().strftime("%H:%M")
    if datetime.strptime(agora, "%H:%M") < datetime.strptime(BOLETOS_ALERTA_HORA, "%H:%M"):
        return
    try:
        url = f"{CLOUD_URL.rstrip('/')}/api/boletos/alerta-telegram?filial_id={FILIAL_ID}"
        r = requests.post(url, headers=headers(), timeout=30)
        if r.status_code == 200:
            d = r.json()
            _boletos_alerta_enviado = hoje
            if d.get("total_boletos", 0) > 0:
                logger.info(f"Alerta boletos enviado: {d['total_boletos']} boleto(s) R$ {d.get('valor_total', 0):.2f}")
            else:
                logger.info("Alerta boletos: nenhum vencimento hoje")
        else:
            logger.warning(f"Erro alerta boletos: {r.status_code}")
    except Exception as e:
        logger.warning(f"Erro verificar alerta boletos: {e}")


def _loop_background(dias_fixo):
    """Loop de sync automatico rodando em background junto com o webhook."""
    while True:
        dias = dias_fixo if dias_fixo else calcular_dias_sync()
        _fazer_sync_thread(dias)
        _verificar_relatorio_agendado()
        _verificar_alerta_boletos()
        logger.info(f"Proximo sync automatico em {INTERVALO//60} minutos")
        time.sleep(INTERVALO)


if __name__ == "__main__":
    main()
