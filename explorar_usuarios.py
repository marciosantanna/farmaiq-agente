"""
Script de exploracao READ-ONLY do Farmasoft (Firebird).

Objetivo: localizar a(s) tabela(s) de usuarios/operadores e ver como o
campo de senha esta armazenado (texto puro, hash ou criptografado).

REGRA ABSOLUTA: apenas SELECT. Nao faz nenhuma escrita no Farmasoft.

Uso:
    python explorar_usuarios.py
"""
import fdb
from backend.utils.database import farmasoft_connection
from backend.config import settings

TERMOS_CANDIDATOS = [
    "USU", "USER", "SENHA", "OPERADOR", "FUNCIONARIO", "LOGIN", "ACESSO",
    "CONFIG", "PARAMETRO", "CONEXAO", "SERVIDOR", "BANCO", "INTERLIGACAO", "CHAVE",
    "SUPORTE", "MASTER", "ADMIN", "LICENCA", "LICENSE", "DESBLOQUEIO", "LIBERACAO",
]


def listar_tabelas_candidatas(conn):
    query = """
        SELECT TRIM(RDB$RELATION_NAME) AS NOME
        FROM RDB$RELATIONS
        WHERE RDB$SYSTEM_FLAG = 0
        ORDER BY 1
    """
    rows = conn.executar_select(query)
    todas = [r["NOME"] for r in rows]
    candidatas = [t for t in todas if any(term in t.upper() for term in TERMOS_CANDIDATOS)]
    return todas, candidatas


def listar_colunas(conn, tabela):
    query = """
        SELECT TRIM(RF.RDB$FIELD_NAME) AS COLUNA
        FROM RDB$RELATION_FIELDS RF
        WHERE RF.RDB$RELATION_NAME = ?
        ORDER BY RF.RDB$FIELD_POSITION
    """
    rows = conn.executar_select(query, (tabela,))
    return [r["COLUNA"] for r in rows]


def amostra(conn, tabela, colunas, limite=None):
    cols_sql = ", ".join(colunas)
    first_clause = f"FIRST {limite} " if limite else ""
    query = f"SELECT {first_clause}{cols_sql} FROM {tabela}"
    return conn.executar_select(query)


# Tabelas onde queremos o dump completo (nao apenas amostra)
TABELAS_COMPLETAS = ["USUARIOS"]


def listar_contas_firebird():
    """
    Lista as contas de login do PROPRIO SERVIDOR FIREBIRD (nivel de sistema,
    diferente da tabela USUARIOS que e' apenas do aplicativo Farmasoft).

    Usa a API de Services do Firebird (somente leitura - get_users()).
    O Firebird armazena a senha como hash (SRP/legacy), nao ha como ler o
    valor em texto puro por aqui - so os nomes de conta existentes.
    """
    cfg = settings.farmasoft
    try:
        if cfg.fb_library_name:
            fdb.load_api(cfg.fb_library_name)
        svc = fdb.services.connect(host=cfg.host, user=cfg.user, password=cfg.password)
        try:
            usuarios = svc.get_users()
            print(f"--- Contas de login do servidor Firebird ({len(usuarios)}) ---")
            for u in usuarios:
                print(f"  {vars(u)}")
        finally:
            svc.close()
    except Exception as e:
        print(f"Nao foi possivel listar contas do Firebird via Services API: {e}")
    print()


def main():
    listar_contas_firebird()
    with farmasoft_connection() as conn:
        todas, candidatas = listar_tabelas_candidatas(conn)
        print(f"Total de tabelas no banco: {len(todas)}")
        print(f"Tabelas candidatas (nome sugere usuario/senha/login/config): {candidatas}\n")

        for tabela in candidatas:
            colunas = listar_colunas(conn, tabela)
            print(f"--- {tabela} ---")
            print(f"Colunas: {colunas}")
            limite = None if tabela in TABELAS_COMPLETAS else 3
            try:
                linhas = amostra(conn, tabela, colunas, limite=limite)
                for linha in linhas:
                    print(linha)
            except Exception as e:
                print(f"Erro ao ler amostra de {tabela}: {e}")
            print()


if __name__ == "__main__":
    main()
