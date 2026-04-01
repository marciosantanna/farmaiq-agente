"""
Leitor de dados do Farmasoft (Firebird)

REGRA ABSOLUTA: APENAS SELECT - NUNCA ESCREVER

Estrutura do banco Farmax:
- VENDAS: tabela de vendas (CD_FILIAL, CD_GRUPO, DATA_CAIXA, PRECO_TOTAL, etc)
- PRODUTOS: cadastro de produtos (ID_PRODUTO, CD_GRUPO, CUSTO_MEDIO, etc)
- GRUPOS: grupos/classes de produtos (CD_GRUPO, DESCRICAO)
- COMPRAS: compras/notas fiscais
- ITENS_COMPRA: itens das compras
"""
import logging
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional
from dataclasses import dataclass

from backend.utils.database import FarmasoftConnection, farmasoft_connection
from backend.config import settings, MAPEAMENTO_CD_GRUPO

logger = logging.getLogger(__name__)


@dataclass
class VendaGrupo:
    """Dados de venda por grupo"""
    cd_grupo: int
    grupo_descricao: str
    categoria: str
    total_vendido: float
    total_custo: float
    quantidade: int


@dataclass
class VendaProduto:
    """Dados de venda por produto individual"""
    id_produto: int
    descricao: str
    cd_grupo: int
    grupo_descricao: str
    categoria: str
    # Vendas
    total_vendido: float
    total_custo: float
    quantidade_vendida: int
    margem: float
    # Medias
    media_diaria_valor: float
    media_diaria_qtd: float
    # Ranking
    dias_com_venda: int
    frequencia_venda: float  # % dos dias que teve venda
    # Laboratorio e principio ativo
    cd_laboratorio: int = 0
    laboratorio_nome: str = ""
    principio_ativo: str = ""
    # Estoque (injetado pelo endpoint)
    estoque_atual: int = 0


@dataclass
class CompraGrupo:
    """Dados de compra por grupo"""
    cd_grupo: int
    grupo_descricao: str
    categoria: str
    total_comprado: float
    quantidade: int


@dataclass
class NotaCompra:
    """Dados de nota de compra individual"""
    cd_compras: int
    numero_nf: str
    data_emissao: date
    fornecedor: str
    valor_total: float
    categoria_principal: str
    grupos: str


@dataclass
class VendaProdutoLab:
    """Dados de venda por produto com informacao de laboratorio"""
    id_produto: int
    descricao: str
    cd_laboratorio: int
    laboratorio_nome: str
    principio_ativo: str
    categoria: str
    cd_grupo: int
    # Vendas
    total_vendido_qtd: int
    total_vendido_valor: float
    # Percentual em relacao a outros labs do mesmo principio ativo
    percentual_vendas: float
    ranking_lab: int
    # Estoque atual
    estoque_atual: int


@dataclass
class RecebimentoProduto:
    """Dados de recebimento de produto (nota de entrada)"""
    cd_compras: int
    numero_nf: str
    data_emissao: date
    fornecedor: str
    id_produto: int
    descricao: str
    quantidade: int
    valor_unitario: float
    valor_total: float
    laboratorio: str
    principio_ativo: str = ""


@dataclass
class VendaBalconista:
    """Dados de venda por balconista"""
    cd_funcionario: int
    nome: str
    total_vendido: float
    total_custo: float
    quantidade_vendas: int
    ticket_medio: float
    margem_percentual: float
    comissao: float = 0.0
    itens_por_venda: float = 0.0
    mix: Dict = None
    top_produtos: list = None
    historico_diario: list = None


class FarmasoftReader:
    """
    Leitor do banco Farmasoft/Farmax (Firebird)

    REGRA: Apenas SELECT - NUNCA escrever

    Estrutura identificada:
    - VENDAS: CD_FILIAL, CD_GRUPO, DATA_CAIXA, PRECO_TOTAL, QUANTIDADE, ID_PRODUTO
    - PRODUTOS: ID_PRODUTO, CD_GRUPO, CUSTO_MEDIO, CUSTO_UNITARIO
    - GRUPOS: CD_GRUPO, DESCRICAO
    """

    def __init__(self, connection: Optional[FarmasoftConnection] = None):
        self.conn = connection
        self._owns_connection = connection is None

    def _get_connection(self) -> FarmasoftConnection:
        """Obtém conexão (cria se necessário)"""
        if self.conn is None:
            self.conn = FarmasoftConnection()
            self.conn.conectar()
        return self.conn

    def _obter_categoria(self, cd_grupo: int) -> str:
        """Retorna categoria baseada no CD_GRUPO"""
        return MAPEAMENTO_CD_GRUPO.get(cd_grupo, "OUTROS")

    def listar_grupos(self) -> List[Dict]:
        """
        Lista todos os grupos de produtos

        Returns:
            Lista com CD_GRUPO, DESCRICAO de cada grupo
        """
        conn = self._get_connection()
        query = """
            SELECT CD_GRUPO, DESCRICAO, TIPO
            FROM GRUPOS
            ORDER BY CD_GRUPO
        """
        return conn.executar_select(query)

    def ler_vendas_periodo(
        self,
        data_inicio: date,
        data_fim: date,
        filial_id: int = 1
    ) -> List[VendaGrupo]:
        """
        Lê vendas do período agrupadas por grupo

        STATUS incluidos:
        - 'V' = Venda confirmada
        - 'S' = Venda em servico/suspensa (conta como venda no Farmasoft)

        Exclui:
        - 'D' = Devoluções
        - 'C' = Canceladas
        - 'P' = Pendentes
        - 'T' = Transferências

        Args:
            data_inicio: Data inicial
            data_fim: Data final
            filial_id: ID da filial (padrão: 1)

        Returns:
            Lista de VendaGrupo
        """
        conn = self._get_connection()

        # Usar custo medio da filial especifica (CUSTO_MEDIO_N onde N = filial_id)
        # Fallback para CUSTO_MEDIO geral se o da filial nao existir
        custo_campo = f"CUSTO_MEDIO_{filial_id}" if filial_id <= 30 else "CUSTO_MEDIO"
        custo_unitario_campo = f"CUSTO_UNITARIO_{filial_id}" if filial_id <= 30 else "CUSTO_UNITARIO"

        query = f"""
            SELECT
                v.CD_GRUPO,
                g.DESCRICAO as GRUPO_DESCRICAO,
                SUM(CASE WHEN v.STATUS IN ('V', 'S') THEN v.PRECO_TOTAL ELSE 0 END) as TOTAL_VENDIDO,
                SUM(CASE
                    WHEN v.STATUS IN ('V', 'S') THEN
                        CASE
                            WHEN COALESCE(p.{custo_unitario_campo}, p.CUSTO_UNITARIO, 0) > 0
                             AND COALESCE(p.{custo_unitario_campo}, p.CUSTO_UNITARIO, 0) * v.QUANTIDADE <= v.PRECO_TOTAL
                            THEN COALESCE(p.{custo_unitario_campo}, p.CUSTO_UNITARIO, 0) * v.QUANTIDADE
                            WHEN COALESCE(p.{custo_campo}, p.CUSTO_MEDIO, 0) > 0
                             AND COALESCE(p.{custo_campo}, p.CUSTO_MEDIO, 0) * v.QUANTIDADE <= v.PRECO_TOTAL
                            THEN COALESCE(p.{custo_campo}, p.CUSTO_MEDIO, 0) * v.QUANTIDADE
                            ELSE v.PRECO_TOTAL * 0.65
                        END
                    ELSE 0
                END) as TOTAL_CUSTO,
                COUNT(CASE WHEN v.STATUS IN ('V', 'S') THEN 1 END) as QUANTIDADE
            FROM VENDAS v
            LEFT JOIN GRUPOS g ON v.CD_GRUPO = g.CD_GRUPO
            LEFT JOIN PRODUTOS p ON v.ID_PRODUTO = p.ID_PRODUTO
            WHERE v.CD_FILIAL = ?
              AND v.DATA_CAIXA BETWEEN ? AND ?
              AND v.STATUS IN ('V', 'S', 'D')
              AND v.CONCLUIDO = 'S'
            GROUP BY v.CD_GRUPO, g.DESCRICAO
            ORDER BY TOTAL_VENDIDO DESC
        """

        params = (filial_id, data_inicio, data_fim)

        try:
            results = conn.executar_select(query, params)
            return [
                VendaGrupo(
                    cd_grupo=int(r["CD_GRUPO"]) if r["CD_GRUPO"] else 0,
                    grupo_descricao=r["GRUPO_DESCRICAO"] or "SEM_GRUPO",
                    categoria=self._obter_categoria(int(r["CD_GRUPO"]) if r["CD_GRUPO"] else 0),
                    total_vendido=float(r["TOTAL_VENDIDO"] or 0),
                    total_custo=float(r["TOTAL_CUSTO"] or 0),
                    quantidade=int(r["QUANTIDADE"] or 0),
                )
                for r in results
            ]
        except Exception as e:
            logger.error(f"Erro ao ler vendas: {e}")
            raise

    def ler_compras_periodo(
        self,
        data_inicio: date,
        data_fim: date,
        filial_id: int = 1
    ) -> List[CompraGrupo]:
        """
        Lê compras do período agrupadas por grupo

        IMPORTANTE: Usa TOTAL_NOTA da tabela COMPRAS para coincidir com
        os valores exibidos no Farmasoft. O valor é distribuído proporcionalmente
        entre os grupos de cada nota.

        Args:
            data_inicio: Data inicial
            data_fim: Data final
            filial_id: ID da filial (padrão: 1)

        Returns:
            Lista de CompraGrupo
        """
        conn = self._get_connection()

        # Query usando TOTAL_NOTA proporcional por grupo
        # Calcula proporção de cada grupo nos itens e aplica ao TOTAL_NOTA
        query = """
            SELECT
                ic.GRUPO as GRUPO_DESCRICAO,
                g.CD_GRUPO,
                SUM(
                    CASE
                        WHEN c.VL_TOTALPRODUTOS > 0
                        THEN (ic.VL_TOTAL / c.VL_TOTALPRODUTOS) * c.TOTAL_NOTA
                        ELSE ic.VL_TOTAL
                    END
                ) as TOTAL_COMPRADO,
                COUNT(*) as QUANTIDADE
            FROM ITENS_COMPRA ic
            INNER JOIN COMPRAS c ON ic.CD_COMPRAS = c.CD_COMPRAS
            LEFT JOIN GRUPOS g ON ic.GRUPO = g.DESCRICAO
            WHERE c.CD_FILIAL = ?
              AND c.DT_EMISSAO BETWEEN ? AND ?
              AND c.STATUS = 'C'
            GROUP BY ic.GRUPO, g.CD_GRUPO
            ORDER BY TOTAL_COMPRADO DESC
        """

        params = (filial_id, data_inicio, data_fim)

        try:
            results = conn.executar_select(query, params)
            return [
                CompraGrupo(
                    cd_grupo=int(r["CD_GRUPO"]) if r["CD_GRUPO"] else 0,
                    grupo_descricao=r["GRUPO_DESCRICAO"] or "SEM_GRUPO",
                    categoria=self._obter_categoria(int(r["CD_GRUPO"]) if r["CD_GRUPO"] else 0),
                    total_comprado=float(r["TOTAL_COMPRADO"] or 0),
                    quantidade=int(r["QUANTIDADE"] or 0),
                )
                for r in results
            ]
        except Exception as e:
            logger.error(f"Erro ao ler compras: {e}")
            raise

    def ler_notas_compras_periodo(
        self,
        data_inicio: date,
        data_fim: date,
        filial_id: int = 1
    ) -> List[NotaCompra]:
        """
        Lista notas de compras individuais do periodo

        Retorna cada nota com valor total e categoria principal
        (categoria com maior valor na nota).

        Args:
            data_inicio: Data inicial
            data_fim: Data final
            filial_id: ID da filial

        Returns:
            Lista de NotaCompra
        """
        conn = self._get_connection()

        query = """
            SELECT
                c.CD_COMPRAS,
                c.NOTA_FISCAL,
                c.DT_EMISSAO,
                d.NOME as NOME_FORNECEDOR,
                c.TOTAL_NOTA
            FROM COMPRAS c
            LEFT JOIN DISTRIBUIDORES d ON c.CD_DISTRIBUIDOR = d.CD_DISTRIBUIDOR
            WHERE c.CD_FILIAL = ?
              AND c.DT_EMISSAO BETWEEN ? AND ?
              AND c.STATUS = 'C'
            ORDER BY c.DT_EMISSAO DESC, c.CD_COMPRAS DESC
        """

        params = (filial_id, data_inicio, data_fim)

        try:
            results = conn.executar_select(query, params)
            notas = []
            for r in results:
                notas.append(NotaCompra(
                    cd_compras=int(r["CD_COMPRAS"]),
                    numero_nf=str(r.get("NOTA_FISCAL") or ""),
                    data_emissao=r["DT_EMISSAO"],
                    fornecedor=(r.get("NOME_FORNECEDOR") or "").strip(),
                    valor_total=float(r["TOTAL_NOTA"] or 0),
                    categoria_principal="OUTROS",
                    grupos="",
                ))
            return notas
        except Exception as e:
            logger.error(f"Erro ao ler notas de compras: {e}")
            raise

    def _identificar_categoria_principal(self, grupos_str: str) -> str:
        """Identifica categoria principal baseado nos grupos"""
        if not grupos_str:
            return "OUTROS"

        grupos = [g.strip().upper() for g in grupos_str.split(",")]

        from backend.config.settings import AGRUPAMENTO_CLASSES
        categorias_encontradas = []
        for grupo in grupos:
            for cat, lista_grupos in AGRUPAMENTO_CLASSES.items():
                if grupo in [g.upper() for g in lista_grupos]:
                    categorias_encontradas.append(cat)
                    break

        if not categorias_encontradas:
            return "OUTROS"

        from collections import Counter
        contador = Counter(categorias_encontradas)
        return contador.most_common(1)[0][0]

    def ler_vendas_consolidadas_por_categoria(
        self,
        data_inicio: date,
        data_fim: date,
        filial_id: int = 1
    ) -> Dict[str, Dict]:
        """
        Lê vendas consolidadas por categoria gerencial

        Returns:
            Dict com categoria -> {total_vendido, total_custo, margem, grupos}
        """
        vendas = self.ler_vendas_periodo(data_inicio, data_fim, filial_id)

        categorias = {}
        for venda in vendas:
            cat = venda.categoria
            if cat not in categorias:
                categorias[cat] = {
                    "total_vendido": 0,
                    "total_custo": 0,
                    "quantidade": 0,
                    "grupos": [],
                }

            categorias[cat]["total_vendido"] += venda.total_vendido
            categorias[cat]["total_custo"] += venda.total_custo
            categorias[cat]["quantidade"] += venda.quantidade
            categorias[cat]["grupos"].append(venda.grupo_descricao)

        # Calcular margem
        for cat, dados in categorias.items():
            if dados["total_vendido"] > 0:
                lucro = dados["total_vendido"] - dados["total_custo"]
                dados["margem"] = round((lucro / dados["total_vendido"]) * 100, 2)
            else:
                dados["margem"] = 0

        return categorias

    def ler_compras_consolidadas_por_categoria(
        self,
        data_inicio: date,
        data_fim: date,
        filial_id: int = 1
    ) -> Dict[str, Dict]:
        """
        Lê compras consolidadas por categoria gerencial

        Returns:
            Dict com categoria -> {total_comprado, quantidade, grupos}
        """
        compras = self.ler_compras_periodo(data_inicio, data_fim, filial_id)

        categorias = {}
        for compra in compras:
            cat = compra.categoria
            if cat not in categorias:
                categorias[cat] = {
                    "total_comprado": 0,
                    "quantidade": 0,
                    "grupos": [],
                }

            categorias[cat]["total_comprado"] += compra.total_comprado
            categorias[cat]["quantidade"] += compra.quantidade
            categorias[cat]["grupos"].append(compra.grupo_descricao)

        return categorias

    def ler_resumo_periodo(
        self,
        data_inicio: date,
        data_fim: date,
        filial_id: int = 1
    ) -> Dict:
        """
        Lê resumo completo do período (vendas + compras por categoria)

        Returns:
            Dict com resumo consolidado
        """
        vendas = self.ler_vendas_consolidadas_por_categoria(data_inicio, data_fim, filial_id)
        compras = self.ler_compras_consolidadas_por_categoria(data_inicio, data_fim, filial_id)

        # Consolidar
        categorias = {}
        todas_cats = set(list(vendas.keys()) + list(compras.keys()))

        for cat in todas_cats:
            v = vendas.get(cat, {"total_vendido": 0, "total_custo": 0, "margem": 0})
            c = compras.get(cat, {"total_comprado": 0})

            categorias[cat] = {
                "total_vendido": v["total_vendido"],
                "total_custo": v["total_custo"],
                "margem": v["margem"],
                "total_comprado": c["total_comprado"],
            }

        # Totais gerais
        total_vendido = sum(c["total_vendido"] for c in categorias.values())
        total_custo = sum(c["total_custo"] for c in categorias.values())
        total_comprado = sum(c["total_comprado"] for c in categorias.values())
        margem_geral = round(((total_vendido - total_custo) / total_vendido * 100), 2) if total_vendido > 0 else 0

        return {
            "periodo": {"inicio": data_inicio, "fim": data_fim},
            "filial_id": filial_id,
            "total_vendido": round(total_vendido, 2),
            "total_custo": round(total_custo, 2),
            "total_comprado": round(total_comprado, 2),
            "margem_geral": margem_geral,
            "categorias": categorias,
        }

    def ler_vendas_por_produto(
        self,
        data_inicio: date,
        data_fim: date,
        filial_id: int = 1,
    ) -> List[VendaProduto]:
        """
        Lê vendas detalhadas por PRODUTO individual

        Retorna os produtos mais vendidos com:
        - Total vendido e custo
        - Quantidade vendida
        - Margem individual
        - Media diaria de venda
        - Frequencia de venda (quantos dias vendeu)

        Args:
            data_inicio: Data inicial
            data_fim: Data final
            filial_id: ID da filial
            limite: Quantidade maxima de produtos a retornar

        Returns:
            Lista de VendaProduto ordenada por valor vendido
        """
        conn = self._get_connection()

        # Calcular dias do periodo
        dias_periodo = (data_fim - data_inicio).days + 1

        custo_campo = f"CUSTO_MEDIO_{filial_id}" if filial_id <= 30 else "CUSTO_MEDIO"
        custo_unitario_campo = f"CUSTO_UNITARIO_{filial_id}" if filial_id <= 30 else "CUSTO_UNITARIO"

        query = f"""
            SELECT
                p.ID_PRODUTO,
                p.DESCRICAO as PRODUTO_DESCRICAO,
                p.CD_GRUPO,
                g.DESCRICAO as GRUPO_DESCRICAO,
                p.CD_LABORATORIO,
                l.NOME as LABORATORIO_NOME,
                p.PRINCIPIOATIVO,
                SUM(v.PRECO_TOTAL) as TOTAL_VENDIDO,
                SUM(CASE
                    WHEN COALESCE(p.{custo_unitario_campo}, p.CUSTO_UNITARIO, 0) > 0
                     AND COALESCE(p.{custo_unitario_campo}, p.CUSTO_UNITARIO, 0) * v.QUANTIDADE <= v.PRECO_TOTAL
                    THEN COALESCE(p.{custo_unitario_campo}, p.CUSTO_UNITARIO, 0) * v.QUANTIDADE
                    WHEN COALESCE(p.{custo_campo}, p.CUSTO_MEDIO, 0) > 0
                     AND COALESCE(p.{custo_campo}, p.CUSTO_MEDIO, 0) * v.QUANTIDADE <= v.PRECO_TOTAL
                    THEN COALESCE(p.{custo_campo}, p.CUSTO_MEDIO, 0) * v.QUANTIDADE
                    ELSE v.PRECO_TOTAL * 0.65
                END) as TOTAL_CUSTO,
                SUM(v.QUANTIDADE) as QUANTIDADE_VENDIDA,
                COUNT(DISTINCT v.DATA_CAIXA) as DIAS_COM_VENDA
            FROM VENDAS v
            INNER JOIN PRODUTOS p ON v.ID_PRODUTO = p.ID_PRODUTO
            LEFT JOIN GRUPOS g ON p.CD_GRUPO = g.CD_GRUPO
            LEFT JOIN LABORATORIOS l ON p.CD_LABORATORIO = l.CD_LABORATORIO
            WHERE v.CD_FILIAL = ?
              AND v.DATA_CAIXA BETWEEN ? AND ?
              AND v.STATUS IN ('V', 'S')
              AND v.CONCLUIDO = 'S'
            GROUP BY p.ID_PRODUTO, p.DESCRICAO, p.CD_GRUPO, g.DESCRICAO,
                     p.CD_LABORATORIO, l.NOME, p.PRINCIPIOATIVO
            ORDER BY TOTAL_VENDIDO DESC
        """

        params = (filial_id, data_inicio, data_fim)

        try:
            results = conn.executar_select(query, params)

            produtos = []
            for r in results:
                total_vendido = float(r["TOTAL_VENDIDO"] or 0)
                total_custo = float(r["TOTAL_CUSTO"] or 0)
                quantidade = int(r["QUANTIDADE_VENDIDA"] or 0)
                dias_com_venda = int(r["DIAS_COM_VENDA"] or 0)

                # Calcular margem
                margem = ((total_vendido - total_custo) / total_vendido * 100) if total_vendido > 0 else 0

                # Calcular medias
                media_diaria_valor = total_vendido / dias_periodo if dias_periodo > 0 else 0
                media_diaria_qtd = quantidade / dias_periodo if dias_periodo > 0 else 0

                # Frequencia de venda (% dos dias que vendeu)
                frequencia = (dias_com_venda / dias_periodo * 100) if dias_periodo > 0 else 0

                cd_grupo = int(r["CD_GRUPO"]) if r["CD_GRUPO"] else 0

                produtos.append(VendaProduto(
                    id_produto=int(r["ID_PRODUTO"]),
                    descricao=r["PRODUTO_DESCRICAO"] or "SEM_DESCRICAO",
                    cd_grupo=cd_grupo,
                    grupo_descricao=r["GRUPO_DESCRICAO"] or "SEM_GRUPO",
                    categoria=self._obter_categoria(cd_grupo),
                    total_vendido=round(total_vendido, 2),
                    total_custo=round(total_custo, 2),
                    quantidade_vendida=quantidade,
                    margem=round(margem, 2),
                    media_diaria_valor=round(media_diaria_valor, 2),
                    media_diaria_qtd=round(media_diaria_qtd, 2),
                    dias_com_venda=dias_com_venda,
                    frequencia_venda=round(frequencia, 1),
                    cd_laboratorio=int(r["CD_LABORATORIO"]) if r.get("CD_LABORATORIO") else 0,
                    laboratorio_nome=(r.get("LABORATORIO_NOME") or "").strip(),
                    principio_ativo=(r.get("PRINCIPIOATIVO") or "").strip(),
                ))

            return produtos
        except Exception as e:
            logger.error(f"Erro ao ler vendas por produto: {e}")
            raise

    def ler_historico_produto(
        self,
        id_produto: int,
        filial_id: int = 1,
        dias: int = 180
    ) -> List[Dict]:
        """
        Lê historico de vendas de um produto especifico

        Retorna vendas diarias para analise de sazonalidade

        Args:
            id_produto: ID do produto
            filial_id: ID da filial
            dias: Numero de dias de historico (padrao 180 para melhor analise)

        Returns:
            Lista com vendas diarias {data, quantidade, valor}
        """
        conn = self._get_connection()

        data_fim = date.today()
        data_inicio = data_fim - timedelta(days=dias)

        query = """
            SELECT
                v.DATA_CAIXA as DATA,
                SUM(v.QUANTIDADE) as QUANTIDADE,
                SUM(v.PRECO_TOTAL) as VALOR
            FROM VENDAS v
            WHERE v.ID_PRODUTO = ?
              AND v.CD_FILIAL = ?
              AND v.DATA_CAIXA BETWEEN ? AND ?
              AND v.STATUS IN ('V', 'S')
              AND v.CONCLUIDO = 'S'
            GROUP BY v.DATA_CAIXA
            ORDER BY v.DATA_CAIXA
        """

        params = (id_produto, filial_id, data_inicio, data_fim)

        try:
            results = conn.executar_select(query, params)
            return [
                {
                    "data": r["DATA"],
                    "quantidade": int(r["QUANTIDADE"] or 0),
                    "valor": float(r["VALOR"] or 0),
                }
                for r in results
            ]
        except Exception as e:
            logger.error(f"Erro ao ler historico do produto {id_produto}: {e}")
            raise

    def ler_ultimas_vendas(
        self,
        ids_produtos: List[int],
        filial_id: int = 1,
        limite_datas: int = 3,
        dias_busca: int = 90
    ) -> Dict[int, List[Dict]]:
        """
        Busca as ultimas N datas de venda para cada produto

        Args:
            ids_produtos: Lista de IDs de produtos
            filial_id: ID da filial
            limite_datas: Quantas ultimas datas retornar (padrao 3)
            dias_busca: Quantos dias para tras buscar (padrao 90)

        Returns:
            Dict[id_produto, List[{data, quantidade, dias_atras}]]
        """
        if not ids_produtos:
            return {}

        conn = self._get_connection()
        hoje = date.today()
        data_inicio = hoje - timedelta(days=dias_busca)

        # Firebird tem limite de 1500 valores no IN clause - dividir em lotes
        BATCH_SIZE = 1000
        vendas_por_produto = {}

        for i in range(0, len(ids_produtos), BATCH_SIZE):
            batch_ids = ids_produtos[i:i + BATCH_SIZE]
            placeholders = ",".join(["?" for _ in batch_ids])
            query = f"""
                SELECT
                    v.ID_PRODUTO,
                    v.DATA_CAIXA as DATA,
                    SUM(v.QUANTIDADE) as QUANTIDADE
                FROM VENDAS v
                WHERE v.ID_PRODUTO IN ({placeholders})
                  AND v.CD_FILIAL = ?
                  AND v.DATA_CAIXA BETWEEN ? AND ?
                  AND v.STATUS IN ('V', 'S')
                  AND v.CONCLUIDO = 'S'
                GROUP BY v.ID_PRODUTO, v.DATA_CAIXA
                ORDER BY v.ID_PRODUTO, v.DATA_CAIXA DESC
            """

            params = tuple(batch_ids) + (filial_id, data_inicio, hoje)

            try:
                results = conn.executar_select(query, params)

                for r in results:
                    pid = int(r["ID_PRODUTO"])
                    if pid not in vendas_por_produto:
                        vendas_por_produto[pid] = []
                    if len(vendas_por_produto[pid]) < limite_datas:
                        data_venda = r["DATA"]
                        if isinstance(data_venda, datetime):
                            data_venda = data_venda.date()
                        dias_atras = (hoje - data_venda).days
                        vendas_por_produto[pid].append({
                            "data": data_venda.isoformat() if data_venda else None,
                            "quantidade": int(r["QUANTIDADE"] or 0),
                            "dias_atras": dias_atras,
                        })
            except Exception as e:
                logger.error(f"Erro ao ler ultimas vendas (batch {i//BATCH_SIZE + 1}): {e}")

        logger.info(f"ler_ultimas_vendas: {len(vendas_por_produto)} de {len(ids_produtos)} produtos com dados")
        return vendas_por_produto

    def ler_produtos_por_categoria(
        self,
        categoria: str,
        data_inicio: date,
        data_fim: date,
        filial_id: int = 1,
        limite: int = 50
    ) -> List[VendaProduto]:
        """
        Lê produtos de uma categoria especifica ordenados por venda

        Args:
            categoria: ETICOS, GENERICOS_SIMILARES, PERFUMARIA, OUTROS
            data_inicio: Data inicial
            data_fim: Data final
            filial_id: ID da filial
            limite: Quantidade maxima

        Returns:
            Lista de VendaProduto da categoria
        """
        # Obter CD_GRUPOs da categoria
        from backend.config import MAPEAMENTO_CD_GRUPO

        grupos_categoria = [
            cd for cd, cat in MAPEAMENTO_CD_GRUPO.items()
            if cat == categoria
        ]

        if not grupos_categoria:
            return []

        # Buscar todos produtos e filtrar
        todos = self.ler_vendas_por_produto(data_inicio, data_fim, filial_id)
        return [p for p in todos if p.categoria == categoria][:limite]

    def ler_vendas_por_laboratorio(
        self,
        filial_id: int = 1,
        dias: int = 180,
        limite: int = 500
    ) -> List[VendaProdutoLab]:
        """
        Le vendas agrupadas por produto COM informacao de laboratorio

        Calcula para cada produto:
        - Qual laboratorio fabrica
        - Quanto vendeu (qtd e valor)
        - Qual o principio ativo
        - Ranking do laboratorio vs concorrentes (mesmo principio ativo)

        Args:
            filial_id: ID da filial
            dias: Dias de historico (padrao 180)
            limite: Quantidade maxima de produtos

        Returns:
            Lista de VendaProdutoLab com dados de laboratorio
        """
        conn = self._get_connection()

        data_fim = date.today()
        data_inicio = data_fim - timedelta(days=dias)

        # Campo de estoque da filial
        estoque_campo = f"ESTOQUE_{filial_id}" if filial_id <= 30 else "ESTOQUE_1"

        query = f"""
            SELECT
                p.ID_PRODUTO,
                p.DESCRICAO as PRODUTO_DESCRICAO,
                p.CD_LABORATORIO,
                l.NOME as LABORATORIO_NOME,
                p.PRINCIPIOATIVO,
                p.CD_GRUPO,
                g.DESCRICAO as GRUPO_DESCRICAO,
                p.{estoque_campo} as ESTOQUE_ATUAL,
                SUM(v.QUANTIDADE) as TOTAL_QTD,
                SUM(v.PRECO_TOTAL) as TOTAL_VALOR
            FROM VENDAS v
            INNER JOIN PRODUTOS p ON v.ID_PRODUTO = p.ID_PRODUTO
            LEFT JOIN LABORATORIOS l ON p.CD_LABORATORIO = l.CD_LABORATORIO
            LEFT JOIN GRUPOS g ON p.CD_GRUPO = g.CD_GRUPO
            WHERE v.CD_FILIAL = ?
              AND v.DATA_CAIXA BETWEEN ? AND ?
              AND v.STATUS IN ('V', 'S')
              AND v.CONCLUIDO = 'S'
            GROUP BY p.ID_PRODUTO, p.DESCRICAO, p.CD_LABORATORIO, l.NOME,
                     p.PRINCIPIOATIVO, p.CD_GRUPO, g.DESCRICAO, p.{estoque_campo}
            ORDER BY TOTAL_VALOR DESC
        """

        params = (filial_id, data_inicio, data_fim)

        try:
            results = conn.executar_select(query, params)

            # Agrupar por principio ativo para calcular ranking
            vendas_por_principio = {}
            for r in results:
                principio = (r.get("PRINCIPIOATIVO") or "").strip().upper()
                if principio:
                    if principio not in vendas_por_principio:
                        vendas_por_principio[principio] = []
                    vendas_por_principio[principio].append({
                        "id_produto": int(r["ID_PRODUTO"]),
                        "total_qtd": int(r["TOTAL_QTD"] or 0),
                        "total_valor": float(r["TOTAL_VALOR"] or 0),
                        "laboratorio": (r.get("LABORATORIO_NOME") or "").strip(),
                    })

            # Calcular ranking e percentual por principio ativo
            rankings = {}
            for principio, vendas in vendas_por_principio.items():
                total_principio = sum(v["total_qtd"] for v in vendas)
                # Ordenar por quantidade vendida
                vendas_ordenadas = sorted(vendas, key=lambda x: x["total_qtd"], reverse=True)
                for i, v in enumerate(vendas_ordenadas, 1):
                    pct = (v["total_qtd"] / total_principio * 100) if total_principio > 0 else 0
                    rankings[v["id_produto"]] = {"ranking": i, "percentual": pct}

            # Montar lista final
            produtos = []
            for r in results[:limite]:
                id_produto = int(r["ID_PRODUTO"])
                cd_grupo = int(r["CD_GRUPO"]) if r["CD_GRUPO"] else 0
                ranking_info = rankings.get(id_produto, {"ranking": 1, "percentual": 100})

                produtos.append(VendaProdutoLab(
                    id_produto=id_produto,
                    descricao=(r.get("PRODUTO_DESCRICAO") or "").strip(),
                    cd_laboratorio=int(r["CD_LABORATORIO"]) if r["CD_LABORATORIO"] else 0,
                    laboratorio_nome=(r.get("LABORATORIO_NOME") or "SEM_LAB").strip(),
                    principio_ativo=(r.get("PRINCIPIOATIVO") or "").strip(),
                    categoria=self._obter_categoria(cd_grupo),
                    cd_grupo=cd_grupo,
                    total_vendido_qtd=int(r["TOTAL_QTD"] or 0),
                    total_vendido_valor=float(r["TOTAL_VALOR"] or 0),
                    percentual_vendas=round(ranking_info["percentual"], 1),
                    ranking_lab=ranking_info["ranking"],
                    estoque_atual=int(r["ESTOQUE_ATUAL"] or 0),
                ))

            return produtos
        except Exception as e:
            logger.error(f"Erro ao ler vendas por laboratorio: {e}")
            raise

    def ler_recebimentos_periodo(
        self,
        data_inicio: date,
        data_fim: date,
        filial_id: int = 1,
        produtos_ids: List[int] = None
    ) -> List[RecebimentoProduto]:
        """
        Le recebimentos (notas de entrada) do periodo

        Usado para comparar o que foi SUGERIDO vs o que foi COMPRADO vs o que CHEGOU

        Args:
            data_inicio: Data inicial
            data_fim: Data final
            filial_id: ID da filial
            produtos_ids: Lista opcional de IDs de produtos para filtrar

        Returns:
            Lista de RecebimentoProduto
        """
        conn = self._get_connection()

        # Montar filtro de produtos se fornecido
        filtro_produtos = ""
        if produtos_ids:
            ids_str = ",".join(str(int(i)) for i in produtos_ids)
            filtro_produtos = f"AND ic.ID_PRODUTO IN ({ids_str})"

        query = f"""
            SELECT
                ic.CD_COMPRAS,
                c.NOTA_FISCAL as NUMERO_NF,
                c.DT_EMISSAO,
                d.NOME as NOME_FORNECEDOR,
                ic.ID_PRODUTO,
                ic.DESCRICAO,
                ic.QUANTIDADE,
                ic.VL_UNITARIO,
                ic.VL_TOTAL,
                ic.LABORATORIO,
                p.PRINCIPIOATIVO
            FROM ITENS_COMPRA ic
            INNER JOIN COMPRAS c ON ic.CD_COMPRAS = c.CD_COMPRAS
            LEFT JOIN DISTRIBUIDORES d ON c.CD_DISTRIBUIDOR = d.CD_DISTRIBUIDOR
            LEFT JOIN PRODUTOS p ON ic.ID_PRODUTO = p.ID_PRODUTO
            WHERE c.CD_FILIAL = ?
              AND c.DT_EMISSAO BETWEEN ? AND ?
              AND c.STATUS = 'C'
              {filtro_produtos}
            ORDER BY c.DT_EMISSAO DESC, ic.DESCRICAO
        """

        params = (filial_id, data_inicio, data_fim)

        try:
            results = conn.executar_select(query, params)

            recebimentos = []
            for r in results:
                recebimentos.append(RecebimentoProduto(
                    cd_compras=int(r["CD_COMPRAS"]),
                    numero_nf=str(r.get("NUMERO_NF") or ""),
                    data_emissao=r["DT_EMISSAO"],
                    fornecedor=(r.get("NOME_FORNECEDOR") or "").strip(),
                    id_produto=int(r["ID_PRODUTO"]) if r["ID_PRODUTO"] else 0,
                    descricao=(r.get("DESCRICAO") or "").strip(),
                    quantidade=int(r["QUANTIDADE"] or 0),
                    valor_unitario=float(r["VL_UNITARIO"] or 0),
                    valor_total=float(r["VL_TOTAL"] or 0),
                    laboratorio=(r.get("LABORATORIO") or "").strip(),
                    principio_ativo=(r.get("PRINCIPIOATIVO") or "").strip(),
                ))

            return recebimentos
        except Exception as e:
            logger.error(f"Erro ao ler recebimentos: {e}")
            raise

    def ler_saidas_vencimento(
        self,
        data_inicio: date,
        data_fim: date,
        filial_id: int = 1,
        produtos_ids: List[int] = None,
    ) -> List[dict]:
        """
        Retorna saidas por vencimento/perda do periodo.

        Marcador: LANCAMENTOS.TIPO_VENDA = 'X' + ENTRADA_SAIDA = 'S' + CD_VENDA IS NULL
        Confirmado via produto 172545 (Seretide) retirado em 18/02/2026.

        Returns:
            List[{id_produto, data, quantidade, usuario}]
        """
        conn = self._get_connection()
        BATCH = 500  # Firebird limita IN() a 1500; usar 500 por seguranca

        def _executar_batch(ids_batch):
            filtro = ""
            if ids_batch is not None:
                ids_str = ",".join(str(int(i)) for i in ids_batch)
                filtro = f"AND l.ID_PRODUTO IN ({ids_str})"
            query = f"""
                SELECT
                    l.ID_PRODUTO,
                    l.DESCRICAO,
                    l.CD_GRUPO,
                    l.DATA_CAIXA,
                    l.QUANTIDADE,
                    l.USUARIO,
                    l.CUSTO_UNITARIO
                FROM LANCAMENTOS l
                WHERE l.CD_FILIAL = ?
                  AND l.DATA_CAIXA BETWEEN ? AND ?
                  AND l.TIPO_VENDA = 'X'
                  AND l.ENTRADA_SAIDA = 'S'
                  AND l.CD_VENDA IS NULL
                  {filtro}
                ORDER BY l.DATA_CAIXA DESC
            """
            return conn.executar_select(query, (filial_id, data_inicio, data_fim))

        try:
            if produtos_ids:
                all_results = []
                for i in range(0, len(produtos_ids), BATCH):
                    all_results.extend(_executar_batch(produtos_ids[i:i + BATCH]))
            else:
                all_results = _executar_batch(None)

            return [
                {
                    "id_produto": int(r["ID_PRODUTO"]),
                    "descricao": (r.get("DESCRICAO") or "").strip(),
                    "cd_grupo": int(r.get("CD_GRUPO") or 0),
                    "data": r["DATA_CAIXA"],
                    "quantidade": float(r["QUANTIDADE"] or 0),
                    "usuario": (r.get("USUARIO") or "").strip(),
                    "custo_unitario": float(r.get("CUSTO_UNITARIO") or 0),
                }
                for r in all_results
            ]
        except Exception as e:
            logger.error(f"Erro ao ler saidas vencimento: {e}")
            raise

    def ler_estoque_e_custo(
        self,
        ids_produtos: List[int],
        filial_id: int = 1
    ) -> Dict[int, Dict]:
        """
        Busca estoque atual e custo unitario para uma lista de produtos.

        Returns:
            {id_produto: {"estoque": int, "custo_unitario": float}}
        """
        if not ids_produtos:
            return {}

        conn = self._get_connection()
        estoque_campo = f"ESTOQUE_{filial_id}" if filial_id <= 30 else "ESTOQUE_1"
        custo_unit_campo = f"CUSTO_UNITARIO_{filial_id}" if filial_id <= 30 else "CUSTO_UNITARIO"

        try:
            dados = {}
            # Buscar em lotes de 500 (limite do Firebird para IN clause)
            for i in range(0, len(ids_produtos), 500):
                lote = ids_produtos[i:i+500]
                ids_str = ",".join(str(int(x)) for x in lote)
                query = f"""
                    SELECT
                        p.ID_PRODUTO,
                        p.{estoque_campo} as ESTOQUE,
                        COALESCE(p.{custo_unit_campo}, p.CUSTO_UNITARIO, p.CUSTO_MEDIO) as CUSTO_UNIT
                    FROM PRODUTOS p
                    WHERE p.ID_PRODUTO IN ({ids_str})
                """
                results = conn.executar_select(query)
                for r in results:
                    dados[int(r["ID_PRODUTO"])] = {
                        "estoque": int(r["ESTOQUE"] or 0),
                        "custo_unitario": round(float(r["CUSTO_UNIT"] or 0), 2),
                    }
            return dados
        except Exception as e:
            logger.error(f"Erro ao ler estoque e custo: {e}")
            return {}

    def ler_estoque_produtos(self, filial_id: int = 1, apenas_ativos: bool = True) -> List[Dict]:
        """Retorna todos produtos com estoque, custo e info basica para sync cloud.

        STATUS='I' = Inativo no Farmasoft. apenas_ativos=True exclui esses produtos.
        """
        conn = self._get_connection()
        estoque_campo = f"ESTOQUE_{filial_id}" if filial_id <= 30 else "ESTOQUE_1"
        custo_campo = f"CUSTO_UNITARIO_{filial_id}" if filial_id <= 30 else "CUSTO_UNITARIO"
        filtro_status = "AND p.STATUS != 'I'" if apenas_ativos else ""
        query = f"""
            SELECT
                p.ID_PRODUTO,
                p.CD_PRODUTO,
                p.DESCRICAO,
                p.PRINCIPIOATIVO,
                p.CD_LABORATORIO,
                COALESCE(l.NOME, '') as LABORATORIO,
                p.CD_GRUPO,
                p.CD_CLASSE,
                COALESCE(p.{estoque_campo}, 0) as ESTOQUE,
                COALESCE(p.{custo_campo}, p.CUSTO_UNITARIO, p.CUSTO_MEDIO, 0) as CUSTO_UNITARIO,
                COALESCE(p.PRECO_VENDA, 0) as PRECO_VENDA
            FROM PRODUTOS p
            LEFT JOIN LABORATORIOS l ON p.CD_LABORATORIO = l.CD_LABORATORIO
            WHERE 1=1
            {filtro_status}
            ORDER BY p.ID_PRODUTO
        """
        try:
            return conn.executar_select(query)
        except Exception as e:
            logger.error(f"Erro ao ler estoque produtos: {e}")
            return []

    def ler_estoque_rapido(self, filial_id: int = 1) -> List[Dict]:
        """Retorna apenas ID_PRODUTO + estoque + custo para update rapido de estoque."""
        conn = self._get_connection()
        estoque_campo = f"ESTOQUE_{filial_id}" if filial_id <= 30 else "ESTOQUE_1"
        custo_campo = f"CUSTO_UNITARIO_{filial_id}" if filial_id <= 30 else "CUSTO_UNITARIO"
        query = f"""
            SELECT
                p.ID_PRODUTO,
                COALESCE(p.{estoque_campo}, 0) as ESTOQUE,
                COALESCE(p.{custo_campo}, p.CUSTO_UNITARIO, p.CUSTO_MEDIO, 0) as CUSTO_UNITARIO,
                COALESCE(p.PRECO_VENDA, 0) as PRECO_VENDA
            FROM PRODUTOS p
            ORDER BY p.ID_PRODUTO
        """
        try:
            return conn.executar_select(query)
        except Exception as e:
            logger.error(f"Erro ao ler estoque rapido: {e}")
            return []

    def ler_vendas_por_produto_dia(
        self,
        data_inicio: date,
        data_fim: date,
        filial_id: int = 1,
    ) -> List[Dict]:
        """Retorna vendas agrupadas por data + produto para sync cloud."""
        conn = self._get_connection()
        custo_campo = f"CUSTO_UNITARIO_{filial_id}" if filial_id <= 30 else "CUSTO_UNITARIO"
        custo_medio_campo = f"CUSTO_MEDIO_{filial_id}" if filial_id <= 30 else "CUSTO_MEDIO"
        query = f"""
            SELECT
                v.DATA_CAIXA as DATA_VENDA,
                v.ID_PRODUTO,
                p.CD_PRODUTO,
                p.DESCRICAO,
                p.CD_GRUPO,
                p.CD_CLASSE,
                v.CD_VENDEDOR as CD_BALCONISTA,
                COALESCE(vd.NOME, '') as NOME_BALCONISTA,
                SUM(v.QUANTIDADE) as QUANTIDADE,
                SUM(v.PRECO_TOTAL) as VALOR_TOTAL,
                SUM(CASE
                    WHEN COALESCE(p.{custo_campo}, p.CUSTO_UNITARIO, 0) > 0
                     AND COALESCE(p.{custo_campo}, p.CUSTO_UNITARIO, 0) * v.QUANTIDADE <= v.PRECO_TOTAL
                    THEN COALESCE(p.{custo_campo}, p.CUSTO_UNITARIO, 0) * v.QUANTIDADE
                    WHEN COALESCE(p.{custo_medio_campo}, p.CUSTO_MEDIO, 0) > 0
                     AND COALESCE(p.{custo_medio_campo}, p.CUSTO_MEDIO, 0) * v.QUANTIDADE <= v.PRECO_TOTAL
                    THEN COALESCE(p.{custo_medio_campo}, p.CUSTO_MEDIO, 0) * v.QUANTIDADE
                    ELSE v.PRECO_TOTAL * 0.65
                END) as CUSTO_TOTAL
            FROM VENDAS v
            INNER JOIN PRODUTOS p ON v.ID_PRODUTO = p.ID_PRODUTO
            LEFT JOIN VENDEDORES vd ON v.CD_VENDEDOR = vd.CD_VENDEDOR
            WHERE v.CD_FILIAL = ?
              AND v.DATA_CAIXA BETWEEN ? AND ?
              AND v.STATUS IN ('V', 'S')
              AND v.CONCLUIDO = 'S'
            GROUP BY v.DATA_CAIXA, v.ID_PRODUTO, p.CD_PRODUTO, p.DESCRICAO,
                     p.CD_GRUPO, p.CD_CLASSE, v.CD_VENDEDOR, vd.NOME
            ORDER BY v.DATA_CAIXA, v.ID_PRODUTO
        """
        try:
            return conn.executar_select(query, (filial_id, data_inicio, data_fim))
        except Exception as e:
            logger.error(f"Erro ao ler vendas por produto dia: {e}")
            return []

    def ler_compras_por_nota(
        self,
        data_inicio: date,
        data_fim: date,
        filial_id: int = 1,
    ) -> List[Dict]:
        """Retorna compras por nota+grupo como dicts para sync cloud."""
        conn = self._get_connection()
        query = """
            SELECT
                c.DT_EMISSAO as DATA_NF,
                c.NOTA_FISCAL,
                g.CD_GRUPO,
                SUM(
                    CASE
                        WHEN c.VL_TOTALPRODUTOS > 0
                        THEN (ic.VL_TOTAL / c.VL_TOTALPRODUTOS) * c.TOTAL_NOTA
                        ELSE ic.VL_TOTAL
                    END
                ) as VALOR_TOTAL
            FROM ITENS_COMPRA ic
            INNER JOIN COMPRAS c ON ic.CD_COMPRAS = c.CD_COMPRAS
            LEFT JOIN GRUPOS g ON ic.GRUPO = g.DESCRICAO
            WHERE c.CD_FILIAL = ?
              AND c.DT_EMISSAO BETWEEN ? AND ?
              AND c.STATUS = 'C'
            GROUP BY c.DT_EMISSAO, c.NOTA_FISCAL, g.CD_GRUPO
            ORDER BY c.DT_EMISSAO DESC
        """
        try:
            return conn.executar_select(query, (filial_id, data_inicio, data_fim))
        except Exception as e:
            logger.error(f"Erro ao ler compras por nota: {e}")
            return []

    def ler_pa_produtos(self, ids_produtos: List[int], filial_id: int = 1) -> Dict[int, str]:
        """Retorna {id_produto: principio_ativo} para lista de IDs"""
        if not ids_produtos:
            return {}
        conn = self._get_connection()
        try:
            ids_str = ",".join(str(int(x)) for x in ids_produtos)
            rows = conn.executar_select(
                f"SELECT ID_PRODUTO, PRINCIPIOATIVO FROM PRODUTOS WHERE ID_PRODUTO IN ({ids_str})"
            )
            return {
                int(r["ID_PRODUTO"]): (r.get("PRINCIPIOATIVO") or "").strip().upper()
                for r in rows
            }
        except Exception as e:
            logger.error(f"Erro ao buscar PA produtos: {e}")
            return {}

    def ler_vendas_ontem(
        self,
        ids_produtos: List[int],
        filial_id: int = 1
    ) -> Dict[int, int]:
        """
        Busca quantidade vendida ontem (D-1) para uma lista de produtos.

        Returns:
            {id_produto: qtd_vendida_ontem}
        """
        if not ids_produtos:
            return {}

        conn = self._get_connection()
        ontem = date.today() - timedelta(days=1)

        try:
            dados = {}
            for i in range(0, len(ids_produtos), 500):
                lote = ids_produtos[i:i+500]
                ids_str = ",".join(str(int(x)) for x in lote)
                query = f"""
                    SELECT
                        v.ID_PRODUTO,
                        SUM(v.QUANTIDADE) as QTD_ONTEM
                    FROM VENDAS v
                    WHERE v.CD_FILIAL = ?
                      AND v.DATA_CAIXA = ?
                      AND v.STATUS IN ('V', 'S')
                      AND v.CONCLUIDO = 'S'
                      AND v.ID_PRODUTO IN ({ids_str})
                    GROUP BY v.ID_PRODUTO
                """
                results = conn.executar_select(query, (filial_id, ontem))
                for r in results:
                    dados[int(r["ID_PRODUTO"])] = int(r["QTD_ONTEM"] or 0)
            return dados
        except Exception as e:
            logger.error(f"Erro ao ler vendas ontem: {e}")
            return {}

    def ler_vendas_hoje(
        self,
        ids_produtos: List[int],
        filial_id: int = 1
    ) -> Dict[int, int]:
        """
        Busca quantidade vendida hoje para uma lista de produtos.

        Returns:
            {id_produto: qtd_vendida_hoje}
        """
        if not ids_produtos:
            return {}

        conn = self._get_connection()
        hoje = date.today()

        try:
            dados = {}
            for i in range(0, len(ids_produtos), 500):
                lote = ids_produtos[i:i+500]
                ids_str = ",".join(str(int(x)) for x in lote)
                query = f"""
                    SELECT
                        v.ID_PRODUTO,
                        SUM(v.QUANTIDADE) as QTD_HOJE
                    FROM VENDAS v
                    WHERE v.CD_FILIAL = ?
                      AND v.DATA_CAIXA = ?
                      AND v.STATUS IN ('V', 'S')
                      AND v.CONCLUIDO = 'S'
                      AND v.ID_PRODUTO IN ({ids_str})
                    GROUP BY v.ID_PRODUTO
                """
                results = conn.executar_select(query, (filial_id, hoje))
                for r in results:
                    dados[int(r["ID_PRODUTO"])] = int(r["QTD_HOJE"] or 0)
            return dados
        except Exception as e:
            logger.error(f"Erro ao ler vendas hoje: {e}")
            return {}

    def ler_produto_com_laboratorio(
        self,
        id_produto: int,
        filial_id: int = 1
    ) -> Optional[Dict]:
        """
        Le dados completos de um produto especifico incluindo laboratorio

        Args:
            id_produto: ID do produto
            filial_id: ID da filial

        Returns:
            Dict com dados do produto ou None se nao encontrado
        """
        conn = self._get_connection()

        estoque_campo = f"ESTOQUE_{filial_id}" if filial_id <= 30 else "ESTOQUE_1"
        custo_unit_campo = f"CUSTO_UNITARIO_{filial_id}" if filial_id <= 30 else "CUSTO_UNITARIO"
        custo_medio_campo = f"CUSTO_MEDIO_{filial_id}" if filial_id <= 30 else "CUSTO_MEDIO"

        query = f"""
            SELECT
                p.ID_PRODUTO,
                p.DESCRICAO,
                p.CD_LABORATORIO,
                l.NOME as LABORATORIO_NOME,
                p.PRINCIPIOATIVO,
                p.CD_GRUPO,
                g.DESCRICAO as GRUPO_DESCRICAO,
                p.CODIGO_BARRAS_1 as EAN,
                p.{estoque_campo} as ESTOQUE,
                COALESCE(p.{custo_unit_campo}, p.CUSTO_UNITARIO, p.{custo_medio_campo}) as CUSTO_UNIT,
                p.GENERICO
            FROM PRODUTOS p
            LEFT JOIN LABORATORIOS l ON p.CD_LABORATORIO = l.CD_LABORATORIO
            LEFT JOIN GRUPOS g ON p.CD_GRUPO = g.CD_GRUPO
            WHERE p.ID_PRODUTO = ?
        """

        try:
            results = conn.executar_select(query, (id_produto,))
            if results:
                r = results[0]
                cd_grupo = int(r["CD_GRUPO"]) if r["CD_GRUPO"] else 0
                return {
                    "id_produto": int(r["ID_PRODUTO"]),
                    "descricao": (r.get("DESCRICAO") or "").strip(),
                    "cd_laboratorio": int(r["CD_LABORATORIO"]) if r["CD_LABORATORIO"] else None,
                    "laboratorio_nome": (r.get("LABORATORIO_NOME") or "").strip(),
                    "principio_ativo": (r.get("PRINCIPIOATIVO") or "").strip(),
                    "cd_grupo": cd_grupo,
                    "grupo_descricao": (r.get("GRUPO_DESCRICAO") or "").strip(),
                    "categoria": self._obter_categoria(cd_grupo),
                    "ean": (r.get("EAN") or "").strip(),
                    "estoque": int(r["ESTOQUE"] or 0),
                    "custo_medio": float(r["CUSTO_UNIT"] or 0),
                    "generico": (r.get("GENERICO") or "N") == "S",
                }
            return None
        except Exception as e:
            logger.error(f"Erro ao ler produto {id_produto}: {e}")
            raise

    def ler_balconistas_dia(
        self,
        data_inicio: date,
        data_fim: date,
        filial_id: int = 1,
    ) -> List[Dict]:
        """Retorna vendas agregadas por (data, balconista) com contagem real de transacoes - para sync cloud."""
        conn = self._get_connection()
        custo_campo = f"CUSTO_UNITARIO_{filial_id}" if filial_id <= 30 else "CUSTO_UNITARIO"
        custo_medio_campo = f"CUSTO_MEDIO_{filial_id}" if filial_id <= 30 else "CUSTO_MEDIO"
        query = f"""
            SELECT
                v.DATA_CAIXA as DATA_VENDA,
                v.CD_VENDEDOR as CD_BALCONISTA,
                COALESCE(vd.NOME, '') as NOME_BALCONISTA,
                COUNT(DISTINCT v.ID_VENDA) as QTD_TRANSACOES,
                COUNT(*) as QTD_ITENS,
                SUM(v.PRECO_TOTAL) as VALOR_TOTAL,
                SUM(CASE
                    WHEN COALESCE(p.{custo_campo}, p.CUSTO_UNITARIO, 0) > 0
                     AND COALESCE(p.{custo_campo}, p.CUSTO_UNITARIO, 0) * v.QUANTIDADE <= v.PRECO_TOTAL
                    THEN COALESCE(p.{custo_campo}, p.CUSTO_UNITARIO, 0) * v.QUANTIDADE
                    WHEN COALESCE(p.{custo_medio_campo}, p.CUSTO_MEDIO, 0) > 0
                     AND COALESCE(p.{custo_medio_campo}, p.CUSTO_MEDIO, 0) * v.QUANTIDADE <= v.PRECO_TOTAL
                    THEN COALESCE(p.{custo_medio_campo}, p.CUSTO_MEDIO, 0) * v.QUANTIDADE
                    ELSE v.PRECO_TOTAL * 0.65
                END) as CUSTO_TOTAL
            FROM VENDAS v
            LEFT JOIN VENDEDORES vd ON v.CD_VENDEDOR = vd.CD_VENDEDOR
            LEFT JOIN PRODUTOS p ON v.ID_PRODUTO = p.ID_PRODUTO
            WHERE v.CD_FILIAL = ?
              AND v.DATA_CAIXA BETWEEN ? AND ?
              AND v.STATUS IN ('V', 'S')
              AND v.CONCLUIDO = 'S'
              AND v.CD_VENDEDOR IS NOT NULL
              AND v.CD_VENDEDOR > 0
            GROUP BY v.DATA_CAIXA, v.CD_VENDEDOR, vd.NOME
            ORDER BY v.DATA_CAIXA, v.CD_VENDEDOR
        """
        query_comissao = """
            SELECT l.CD_VENDEDOR, l.DATA_CAIXA, SUM(l.COMISSAO) as COMISSAO_DIA
            FROM LANCAMENTOS l
            WHERE l.CD_FILIAL = ?
              AND l.DATA_CAIXA BETWEEN ? AND ?
              AND l.ENTRADA_SAIDA = 'S'
              AND EXISTS (
                SELECT 1 FROM VENDAS v
                WHERE v.CD_VENDA = l.CD_VENDA
                  AND v.CD_FILIAL = l.CD_FILIAL
                  AND v.STATUS NOT IN ('C', 'P', 'T')
                  AND v.CONCLUIDO = 'S'
              )
            GROUP BY l.CD_VENDEDOR, l.DATA_CAIXA
        """
        try:
            rows = conn.executar_select(query, (filial_id, data_inicio, data_fim))
            # Mapa (data, cd_vendedor) -> comissao_dia
            comissao_map = {}
            try:
                comissoes = conn.executar_select(query_comissao, (filial_id, data_inicio, data_fim))
                for c in comissoes:
                    chave = (str(c["DATA_CAIXA"])[:10], int(c["CD_VENDEDOR"] or 0))
                    comissao_map[chave] = float(c["COMISSAO_DIA"] or 0)
            except Exception as e_com:
                logger.warning(f"ler_balconistas_dia: comissao indisponivel: {e_com}")
            # Injetar comissao em cada linha
            for row in rows:
                chave = (str(row.get("DATA_VENDA") or "")[:10], int(row.get("CD_BALCONISTA") or 0))
                row["COMISSAO_DIA"] = comissao_map.get(chave, 0.0)
            return rows
        except Exception as e:
            logger.error(f"Erro em ler_balconistas_dia: {e}")
            return []

    def ler_vendas_por_balconista(
        self,
        data_inicio: date,
        data_fim: date,
        filial_id: int = 1
    ) -> List[VendaBalconista]:
        """
        Le vendas agrupadas por balconista no periodo

        Args:
            data_inicio: Data inicial
            data_fim: Data final
            filial_id: ID da filial

        Returns:
            Lista de VendaBalconista ordenada por total vendido
        """
        conn = self._get_connection()

        # Campos de custo por filial (mesmo padrao de ler_vendas_por_grupo)
        custo_campo = f"CUSTO_MEDIO_{filial_id}" if filial_id <= 30 else "CUSTO_MEDIO"
        custo_unitario_campo = f"CUSTO_UNITARIO_{filial_id}" if filial_id <= 30 else "CUSTO_UNITARIO"

        # Query vendas com custo real por filial
        query_vendas = f"""
            SELECT
                v.CD_VENDEDOR as CD_FUNCIONARIO,
                COALESCE(vd.NOME, 'Vendedor ' || CAST(v.CD_VENDEDOR AS VARCHAR(10))) as NOME,
                SUM(v.PRECO_TOTAL) as TOTAL_VENDIDO,
                SUM(CASE
                    WHEN COALESCE(p.{custo_unitario_campo}, p.CUSTO_UNITARIO, 0) > 0
                     AND COALESCE(p.{custo_unitario_campo}, p.CUSTO_UNITARIO, 0) * v.QUANTIDADE <= v.PRECO_TOTAL
                    THEN COALESCE(p.{custo_unitario_campo}, p.CUSTO_UNITARIO, 0) * v.QUANTIDADE
                    WHEN COALESCE(p.{custo_campo}, p.CUSTO_MEDIO, 0) > 0
                     AND COALESCE(p.{custo_campo}, p.CUSTO_MEDIO, 0) * v.QUANTIDADE <= v.PRECO_TOTAL
                    THEN COALESCE(p.{custo_campo}, p.CUSTO_MEDIO, 0) * v.QUANTIDADE
                    ELSE v.PRECO_TOTAL * 0.65
                END) as TOTAL_CUSTO,
                COUNT(DISTINCT v.ID_VENDA) as QTD_VENDAS,
                COUNT(*) as QTD_ITENS
            FROM VENDAS v
            LEFT JOIN VENDEDORES vd ON v.CD_VENDEDOR = vd.CD_VENDEDOR
            LEFT JOIN PRODUTOS p ON v.ID_PRODUTO = p.ID_PRODUTO
            WHERE v.CD_FILIAL = ?
              AND v.DATA_CAIXA BETWEEN ? AND ?
              AND v.STATUS IN ('V', 'S')
              AND v.CONCLUIDO = 'S'
              AND v.CD_VENDEDOR IS NOT NULL
              AND v.CD_VENDEDOR > 0
            GROUP BY v.CD_VENDEDOR, vd.NOME
            ORDER BY TOTAL_VENDIDO DESC
        """

        # Top produtos por balconista
        query_top_produtos = """
            SELECT
                v.CD_VENDEDOR,
                v.ID_PRODUTO,
                COALESCE(p.DESCRICAO, 'Produto ' || CAST(v.ID_PRODUTO AS VARCHAR(10))) as DESCRICAO,
                SUM(v.PRECO_TOTAL) as TOTAL,
                SUM(v.QUANTIDADE) as QTD
            FROM VENDAS v
            LEFT JOIN PRODUTOS p ON v.ID_PRODUTO = p.ID_PRODUTO
            WHERE v.CD_FILIAL = ?
              AND v.DATA_CAIXA BETWEEN ? AND ?
              AND v.STATUS IN ('V', 'S')
              AND v.CONCLUIDO = 'S'
              AND v.CD_VENDEDOR IS NOT NULL
              AND v.CD_VENDEDOR > 0
            GROUP BY v.CD_VENDEDOR, v.ID_PRODUTO, p.DESCRICAO
            ORDER BY v.CD_VENDEDOR, TOTAL DESC
        """

        # Historico diario por balconista (para queda brusca)
        query_historico = """
            SELECT
                v.CD_VENDEDOR,
                v.DATA_CAIXA,
                SUM(v.PRECO_TOTAL) as TOTAL_DIA,
                COUNT(DISTINCT v.ID_VENDA) as QTD_DIA
            FROM VENDAS v
            WHERE v.CD_FILIAL = ?
              AND v.DATA_CAIXA BETWEEN ? AND ?
              AND v.STATUS IN ('V', 'S')
              AND v.CONCLUIDO = 'S'
              AND v.CD_VENDEDOR IS NOT NULL
              AND v.CD_VENDEDOR > 0
            GROUP BY v.CD_VENDEDOR, v.DATA_CAIXA
            ORDER BY v.CD_VENDEDOR, v.DATA_CAIXA
        """

        # Mix de categoria por balconista
        query_mix = """
            SELECT
                v.CD_VENDEDOR,
                v.CD_GRUPO,
                SUM(v.PRECO_TOTAL) as TOTAL
            FROM VENDAS v
            WHERE v.CD_FILIAL = ?
              AND v.DATA_CAIXA BETWEEN ? AND ?
              AND v.STATUS IN ('V', 'S')
              AND v.CONCLUIDO = 'S'
              AND v.CD_VENDEDOR IS NOT NULL
              AND v.CD_VENDEDOR > 0
            GROUP BY v.CD_VENDEDOR, v.CD_GRUPO
        """

        # Comissao da tabela LANCAMENTOS usando EXISTS para evitar duplicacao
        # quando VENDAS tem multiplas linhas com mesmo (CD_VENDA, CD_FILIAL, ID_PRODUTO)
        query_lancamentos_comissao = """
            SELECT
                l.CD_VENDEDOR,
                SUM(l.COMISSAO) as TOTAL_COMISSAO
            FROM LANCAMENTOS l
            WHERE l.CD_FILIAL = ?
              AND l.DATA_CAIXA BETWEEN ? AND ?
              AND l.ENTRADA_SAIDA = 'S'
              AND EXISTS (
                SELECT 1 FROM VENDAS v
                WHERE v.CD_VENDA = l.CD_VENDA
                  AND v.CD_FILIAL = l.CD_FILIAL
                  AND v.STATUS NOT IN ('C', 'P', 'T')
                  AND v.CONCLUIDO = 'S'
              )
            GROUP BY l.CD_VENDEDOR
        """

        params = (filial_id, data_inicio, data_fim)

        try:
            results = conn.executar_select(query_vendas, params)

            # Comissao
            comissoes_result = conn.executar_select(query_lancamentos_comissao, params)
            comissoes = {int(r["CD_VENDEDOR"]): float(r["TOTAL_COMISSAO"] or 0) for r in comissoes_result}

            # Mix de categoria por balconista
            mix_result = conn.executar_select(query_mix, params)
            mix_por_balconista: Dict[int, Dict[str, float]] = {}
            for m in mix_result:
                cd = int(m["CD_VENDEDOR"] or 0)
                categoria = self._obter_categoria(int(m["CD_GRUPO"] or 0))
                valor = float(m["TOTAL"] or 0)
                if cd not in mix_por_balconista:
                    mix_por_balconista[cd] = {}
                mix_por_balconista[cd][categoria] = mix_por_balconista[cd].get(categoria, 0) + valor
            for cd, cats in mix_por_balconista.items():
                total = sum(cats.values())
                mix_por_balconista[cd] = {
                    k: round(v / total * 100, 1) for k, v in cats.items() if total > 0
                }

            # Top produtos por balconista (top 3)
            top_result = conn.executar_select(query_top_produtos, params)
            top_por_balconista: Dict[int, list] = {}
            for tp in top_result:
                cd = int(tp["CD_VENDEDOR"] or 0)
                if cd not in top_por_balconista:
                    top_por_balconista[cd] = []
                if len(top_por_balconista[cd]) < 3:
                    top_por_balconista[cd].append({
                        "id_produto": int(tp["ID_PRODUTO"] or 0),
                        "descricao": str(tp["DESCRICAO"] or "").strip(),
                        "total": round(float(tp["TOTAL"] or 0), 2),
                        "qtd": int(tp["QTD"] or 0),
                    })

            # Historico diario (queda brusca + dia da semana)
            hist_result = conn.executar_select(query_historico, params)
            hist_por_balconista: Dict[int, list] = {}
            for h in hist_result:
                cd = int(h["CD_VENDEDOR"] or 0)
                if cd not in hist_por_balconista:
                    hist_por_balconista[cd] = []
                hist_por_balconista[cd].append({
                    "data": str(h["DATA_CAIXA"]),
                    "total": round(float(h["TOTAL_DIA"] or 0), 2),
                    "qtd": int(h["QTD_DIA"] or 0),
                })

            balconistas = []
            for r in results:
                cd_func = int(r["CD_FUNCIONARIO"] or 0)
                total_vendido = float(r["TOTAL_VENDIDO"] or 0)
                total_custo = float(r["TOTAL_CUSTO"] or 0)
                qtd_vendas = int(r["QTD_VENDAS"] or 0)
                qtd_itens = int(r.get("QTD_ITENS") or qtd_vendas)
                comissao = comissoes.get(cd_func, 0)
                ticket_medio = total_vendido / qtd_vendas if qtd_vendas > 0 else 0
                margem = ((total_vendido - total_custo) / total_vendido * 100) if total_vendido > 0 else 0
                itens_por_venda = qtd_itens / qtd_vendas if qtd_vendas > 0 else 0

                balconistas.append(VendaBalconista(
                    cd_funcionario=cd_func,
                    nome=(r.get("NOME") or f"Vendedor {cd_func}").strip(),
                    total_vendido=total_vendido,
                    total_custo=total_custo,
                    quantidade_vendas=qtd_vendas,
                    ticket_medio=round(ticket_medio, 2),
                    margem_percentual=round(margem, 2),
                    comissao=round(comissao, 2),
                    itens_por_venda=round(itens_por_venda, 1),
                    mix=mix_por_balconista.get(cd_func, {}),
                    top_produtos=top_por_balconista.get(cd_func, []),
                    historico_diario=hist_por_balconista.get(cd_func, []),
                ))
            return balconistas
        except Exception as e:
            logger.warning(f"Erro em ler_vendas_por_balconista: {e}")
            return []

    def _carregar_nomes_filiais(self) -> Dict[int, str]:
        """Busca nomes das filiais no Farmasoft. Retorna dict vazio se nao encontrar."""
        for tabela, col_id, col_nome in [
            ("FILIAIS", "CD_FILIAL", "NOME"),
            ("FILIAIS", "CD_FILIAL", "DESCRICAO"),
            ("LOJAS", "CD_LOJA", "NOME"),
            ("LOJAS", "CD_LOJA", "DESCRICAO"),
            ("EMPRESAS", "CD_EMPRESA", "NOME"),
            ("EMPRESAS", "CD_EMPRESA", "DESCRICAO"),
        ]:
            try:
                rows = self.conn.executar_select(
                    f"SELECT {col_id}, {col_nome} FROM {tabela}", ()
                )
                if rows:
                    return {int(r[col_id]): str(r[col_nome]).strip() for r in rows if r[col_id] is not None}
            except Exception:
                continue
        return {}

    def ler_transferencias(self, data_inicio: date, data_fim: date, filial_id: int) -> List[Dict]:
        """
        Le transferencias entre filiais no periodo.
        Retorna tanto as enviadas (CD_FILIAL_ORIGEM) quanto as recebidas (CD_FILIAL_DESTINO).
        Tabelas: TRANSFER (cabecalho) + ITENS_TRANSFER (itens)
        """
        nomes_filiais = self._carregar_nomes_filiais()

        query = """
            SELECT
                t.CD_TRANSFER,
                t.DATA_GERACAO,
                t.DATA_ENVIO,
                t.DATA_CONCLUSAO,
                t.CD_FILIAL_ORIGEM,
                t.CD_FILIAL_DESTINO,
                t.STATUS,
                t.OBSERVACOES,
                it.ID_PRODUTO,
                it.CD_PRODUTO,
                it.DESCRICAO,
                it.CD_GRUPO,
                it.CD_CLASSE,
                it.QUANTIDADE_SOLICITADA,
                it.QUANTIDADE_ENVIADA,
                it.QUANTIDADE_RECEBIDA,
                it.VALOR,
                it.STATUS as STATUS_ITEM
            FROM TRANSFER t
            INNER JOIN ITENS_TRANSFER it ON it.CD_TRANSFER = t.CD_TRANSFER
            WHERE (t.CD_FILIAL_ORIGEM = ? OR t.CD_FILIAL_DESTINO = ?)
              AND t.DATA_GERACAO BETWEEN ? AND ?
            ORDER BY t.DATA_GERACAO DESC, t.CD_TRANSFER DESC
        """
        try:
            rows = self.conn.executar_select(query, (filial_id, filial_id, data_inicio, data_fim))
        except Exception as e:
            logger.warning(f"[transferencias] Erro: {e}")
            return []

        resultado = []
        for r in rows:
            def s(v): return str(v).strip() if v is not None else ""
            def f(v): return float(v) if v is not None else 0.0
            def i(v): return int(v) if v is not None else 0
            def d(v): return str(v)[:10] if v is not None else None

            id_orig = i(r.get("CD_FILIAL_ORIGEM"))
            id_dest = i(r.get("CD_FILIAL_DESTINO"))
            resultado.append({
                "cd_transfer": i(r.get("CD_TRANSFER")),
                "data_geracao": d(r.get("DATA_GERACAO")),
                "data_envio": d(r.get("DATA_ENVIO")),
                "data_conclusao": d(r.get("DATA_CONCLUSAO")),
                "filial_origem": id_orig,
                "nome_filial_origem": nomes_filiais.get(id_orig),
                "filial_destino": id_dest,
                "nome_filial_destino": nomes_filiais.get(id_dest),
                "status_transfer": s(r.get("STATUS")),
                "id_produto": i(r.get("ID_PRODUTO")),
                "cd_produto": s(r.get("CD_PRODUTO")),
                "descricao": s(r.get("DESCRICAO")),
                "cd_grupo": i(r.get("CD_GRUPO")),
                "cd_classe": i(r.get("CD_CLASSE")),
                "qtd_solicitada": f(r.get("QUANTIDADE_SOLICITADA")),
                "qtd_enviada": f(r.get("QUANTIDADE_ENVIADA")),
                "qtd_recebida": f(r.get("QUANTIDADE_RECEBIDA")),
                "valor": f(r.get("VALOR")),
                "status_item": s(r.get("STATUS_ITEM")),
                "sentido": "RECEBIDA" if id_dest == filial_id else "ENVIADA",
            })
        return resultado

    def close(self):
        """Fecha conexão se foi criada internamente"""
        if self._owns_connection and self.conn:
            self.conn.desconectar()
            self.conn = None


# Função auxiliar para uso rápido
def ler_resumo_mes_atual(filial_id: int = 1) -> Dict:
    """Lê resumo do mês atual"""
    hoje = date.today()
    data_inicio = hoje.replace(day=1)

    with farmasoft_connection() as conn:
        reader = FarmasoftReader(conn)
        return reader.ler_resumo_periodo(data_inicio, hoje, filial_id)
