"""
Módulo ETL - Extração e consolidação de dados
"""
from .farmasoft_reader import (
    FarmasoftReader,
    VendaGrupo,
    CompraGrupo,
    ler_resumo_mes_atual,
)

__all__ = [
    "FarmasoftReader",
    "VendaGrupo",
    "CompraGrupo",
    "ler_resumo_mes_atual",
]
