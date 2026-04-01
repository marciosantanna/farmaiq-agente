"""
Inicializacao de historico mensal no Neon

Le totais mensais do Farmasoft e envia para o historico_mensal no Neon.
Usar para popular a base de referencia da meta automatica.

Uso:
    python inicializar_historico.py                        # ultimos 12 meses (padrao)
    python inicializar_historico.py --ano 2025             # ano completo 2025
    python inicializar_historico.py --ano 2025 --mes 4     # so abril/2025
    python inicializar_historico.py --sobrescrever         # forca regravacao
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
    parser = argparse.ArgumentParser(
        description="Inicializa historico_mensal no Neon a partir do Farmasoft.",
        epilog=(
            "Exemplos:\n"
            "  python inicializar_historico.py              # ultimos 12 meses (padrao)\n"
            "  python inicializar_historico.py --ano 2025   # ano completo 2025\n"
            "  python inicializar_historico.py --ano 2025 --mes 4  # so abril/2025\n"
            "  python inicializar_historico.py --sobrescrever      # forca regravacao"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ano",         type=int, default=None,  help="Ano especifico (omitir = ultimos 12 meses)")
    parser.add_argument("--mes",         type=int, default=None,  help="Mes especifico 1-12 (requer --ano)")
    parser.add_argument("--sobrescrever",action="store_true", default=False, help="Sobrescrever se ja existir no Neon")
    args = parser.parse_args()

    if not CLOUD_URL or not AGENT_KEY:
        logger.error("CLOUD_API_URL e AGENT_API_KEY precisam estar no .env")
        sys.exit(1)

    hoje = date.today()
    mes_atual = hoje.replace(day=1)

    # Montar lista de (ano, mes) a processar
    if args.ano and args.mes:
        periodos = [(args.ano, args.mes)]
    elif args.ano:
        periodos = [(args.ano, m) for m in range(1, 13)]
    else:
        # Padrao: ultimos 12 meses fechados (1 ano para tras a partir de hoje)
        periodos = []
        for i in range(1, 13):
            # Subtrai i meses do mes atual
            ano_ref = hoje.year
            mes_ref = hoje.month - i
            while mes_ref <= 0:
                mes_ref += 12
                ano_ref -= 1
            periodos.append((ano_ref, mes_ref))
        periodos.reverse()

    # Excluir meses futuros ou mes corrente (ainda aberto)
    periodos = [(a, m) for a, m in periodos if date(a, m, 1) < mes_atual]

    if not periodos:
        logger.info("Nenhum mes a processar.")
        return

    descricao = f"ano={args.ano}" if args.ano else "ultimos 12 meses"
    logger.info(f"Inicializando historico [{descricao}] | filial={FILIAL_ID} | periodos={periodos} | sobrescrever={args.sobrescrever}")

    from backend.config import settings
    from backend.etl.farmasoft_reader import FarmasoftReader
    from backend.utils.database import FarmasoftConnection

    conn = FarmasoftConnection()
    if not conn.conectar():
        logger.error("Falha ao conectar ao Farmasoft. Verifique as configuracoes.")
        sys.exit(1)

    reader = FarmasoftReader(conn)

    ok_count = 0
    for ano, mes in periodos:
        if processar_mes(reader, ano, mes, args.sobrescrever):
            ok_count += 1

    conn.desconectar()
    logger.info(f"Concluido: {ok_count}/{len(periodos)} meses enviados")


if __name__ == "__main__":
    main()
