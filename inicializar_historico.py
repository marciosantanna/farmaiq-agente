"""
Inicializacao de historico anual no Neon

Le totais mensais de um ano do Farmasoft e envia para o historico_mensal no Neon.
Usar uma unica vez por ano para popular a base de referencia da meta automatica.

Uso:
    python inicializar_historico.py --ano 2025
    python inicializar_historico.py --ano 2025 --mes 4     # so abril
    python inicializar_historico.py --ano 2025 --sobrescrever
"""
import os
import sys
import json
import logging
import argparse
import requests
from datetime import date
from calendar import monthrange
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

CLOUD_URL  = os.getenv("CLOUD_API_URL", "").rstrip("/")
AGENT_KEY  = os.getenv("AGENT_API_KEY", "")
FILIAL_ID  = int(os.getenv("FILIAL_ID", "1"))


def post(endpoint: str, payload: dict) -> bool:
    payload["agent_key"] = AGENT_KEY
    try:
        r = requests.post(f"{CLOUD_URL}{endpoint}", json=payload, timeout=60)
        if r.status_code == 200:
            return True
        logger.error(f"POST {endpoint} -> {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        logger.error(f"POST {endpoint} erro: {e}")
        return False


def processar_mes(reader, ano: int, mes: int, sobrescrever: bool) -> bool:
    from backend.etl.farmasoft_reader import FarmasoftReader

    ultimo_dia = monthrange(ano, mes)[1]
    data_inicio = date(ano, mes, 1)
    data_fim    = date(ano, mes, ultimo_dia)
    nomes = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]

    logger.info(f"Lendo {nomes[mes-1]}/{ano}...")

    try:
        resumo = reader.ler_resumo_periodo(data_inicio, data_fim, FILIAL_ID)
    except Exception as e:
        logger.error(f"  Erro ao ler Farmasoft {mes}/{ano}: {e}")
        return False

    total_vendido = resumo.get("total_vendido", 0)
    if not total_vendido or total_vendido == 0:
        logger.warning(f"  {nomes[mes-1]}/{ano}: sem vendas, ignorando")
        return False

    # Montar categorias simplificado (sem meta - sera 0, nao afeta sugestao)
    cats_json = {}
    for cat, d in resumo.get("categorias", {}).items():
        cats_json[cat] = {
            "vendido":  round(d["total_vendido"], 2),
            "comprado": round(d["total_comprado"], 2),
            "custo":    round(d["total_custo"], 2),
            "margem":   d["margem"],
            "status":   "OK" if d["margem"] > 15 else "ATENCAO" if d["margem"] > 5 else "CRITICO",
        }

    payload = {
        "filial_id":              FILIAL_ID,
        "ano":                    ano,
        "mes":                    mes,
        "total_vendido":          round(total_vendido, 2),
        "total_comprado":         round(resumo.get("total_comprado", 0), 2),
        "margem_geral":           resumo.get("margem_geral", 0),
        "meta_venda":             0,
        "limite_compras":         0,
        "percentual_atingido":    0,
        "percentual_compras_usado": 0,
        "categorias":             cats_json,
        "sobrescrever":           sobrescrever,
    }

    ok = post("/api/historico/inicializar-mes", payload)
    if ok:
        logger.info(f"  {nomes[mes-1]}/{ano}: R${total_vendido:,.2f} -> Neon OK")
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ano",         type=int, required=True,        help="Ano a processar (ex: 2025)")
    parser.add_argument("--mes",         type=int, default=None,          help="Mes especifico (1-12). Omitir = todos os meses")
    parser.add_argument("--sobrescrever",action="store_true", default=False, help="Sobrescrever se ja existir no Neon")
    args = parser.parse_args()

    if not CLOUD_URL or not AGENT_KEY:
        logger.error("CLOUD_API_URL e AGENT_API_KEY precisam estar no .env")
        sys.exit(1)

    hoje = date.today()
    meses = [args.mes] if args.mes else list(range(1, 13))
    # Nao processar meses futuros
    meses = [m for m in meses if date(args.ano, m, 1) < hoje.replace(day=1)]

    if not meses:
        logger.info("Nenhum mes a processar.")
        return

    logger.info(f"Inicializando historico {args.ano} | filial={FILIAL_ID} | meses={meses} | sobrescrever={args.sobrescrever}")

    from backend.config import settings
    from backend.etl.farmasoft_reader import FarmasoftReader
    from backend.utils.database import FarmasoftConnection

    conn = FarmasoftConnection()
    reader = FarmasoftReader(conn)

    ok_count = 0
    for mes in meses:
        if processar_mes(reader, args.ano, mes, args.sobrescrever):
            ok_count += 1

    conn.fechar()
    logger.info(f"Concluido: {ok_count}/{len(meses)} meses enviados")


if __name__ == "__main__":
    main()
