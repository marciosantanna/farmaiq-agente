"""
Utilitários de conexão com bancos de dados
"""
import logging
from contextlib import contextmanager
from typing import Generator, Any, Optional, List, Dict

try:
    import fdb
    FDB_AVAILABLE = True
except ImportError:
    FDB_AVAILABLE = False

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

from backend.config import settings

logger = logging.getLogger(__name__)


class FarmasoftConnection:
    """
    Gerenciador de conexão com Farmasoft (Firebird)

    REGRA ABSOLUTA: APENAS LEITURA (SELECT)
    """

    def __init__(self, config=None):
        if not FDB_AVAILABLE:
            raise ImportError("Biblioteca 'fdb' não instalada. Execute: pip install fdb")

        self.config = config or settings.farmasoft
        self.conn = None

    def conectar(self) -> bool:
        """Conecta ao banco Firebird"""
        try:
            self.conn = fdb.connect(
                host=self.config.host,
                port=self.config.port,
                database=self.config.database,
                user=self.config.user,
                password=self.config.password,
                charset=self.config.charset,
            )
            logger.info(f"Conectado ao Farmasoft: {self.config.host}")
            return True
        except Exception as e:
            logger.error(f"Erro ao conectar ao Farmasoft: {e}")
            return False

    def desconectar(self):
        """Fecha conexão"""
        if self.conn:
            self.conn.close()
            self.conn = None
            logger.info("Desconectado do Farmasoft")

    def executar_select(self, query: str, params: tuple = None) -> List[Dict]:
        """
        Executa query SELECT (APENAS SELECT)

        Args:
            query: Query SQL (deve começar com SELECT)
            params: Parâmetros da query

        Returns:
            Lista de dicionários com resultados
        """
        # Validação de segurança: APENAS SELECT
        query_upper = query.strip().upper()
        if not query_upper.startswith("SELECT"):
            raise PermissionError(
                "OPERAÇÃO PROIBIDA: Apenas SELECT é permitido no Farmasoft. "
                "Operações de escrita (INSERT, UPDATE, DELETE) são bloqueadas."
            )

        if not self.conn:
            raise ConnectionError("Não conectado ao Farmasoft")

        cursor = self.conn.cursor()
        try:
            cursor.execute(query, params or ())
            columns = [desc[0] for desc in cursor.description]
            results = []
            for row in cursor.fetchall():
                results.append(dict(zip(columns, row)))
            return results
        finally:
            cursor.close()

    @contextmanager
    def cursor(self) -> Generator:
        """Context manager para cursor"""
        if not self.conn:
            raise ConnectionError("Não conectado ao Farmasoft")

        cursor = self.conn.cursor()
        try:
            yield cursor
        finally:
            cursor.close()


class PostgresConnection:
    """Gerenciador de conexão com PostgreSQL"""

    def __init__(self, config=None):
        if not PSYCOPG2_AVAILABLE:
            raise ImportError("Biblioteca 'psycopg2' não instalada. Execute: pip install psycopg2-binary")

        self.config = config or settings.postgres
        self.conn = None

    def conectar(self) -> bool:
        """Conecta ao PostgreSQL"""
        try:
            dsn = self.config.connection_string
            self.conn = psycopg2.connect(dsn)
            logger.info(f"Conectado ao PostgreSQL")
            return True
        except Exception as e:
            logger.error(f"Erro ao conectar ao PostgreSQL: {e}")
            return False

    def desconectar(self):
        """Fecha conexão"""
        if self.conn:
            self.conn.close()
            self.conn = None
            logger.info("Desconectado do PostgreSQL")

    def executar(self, query: str, params: tuple = None, commit: bool = True) -> Optional[List[Dict]]:
        """
        Executa query no PostgreSQL

        Args:
            query: Query SQL
            params: Parâmetros da query
            commit: Se deve fazer commit após execução

        Returns:
            Lista de dicionários para SELECT e RETURNING, None para outras operações
        """
        if not self.conn:
            raise ConnectionError("Não conectado ao PostgreSQL")

        cursor = self.conn.cursor(cursor_factory=RealDictCursor)
        try:
            cursor.execute(query, params or ())

            query_upper = query.strip().upper()
            # Retornar resultados para SELECT ou queries com RETURNING
            if query_upper.startswith("SELECT") or "RETURNING" in query_upper:
                results = [dict(row) for row in cursor.fetchall()]
                if commit and not query_upper.startswith("SELECT"):
                    self.conn.commit()
                return results
            else:
                if commit:
                    self.conn.commit()
                return None
        except Exception as e:
            self.conn.rollback()
            raise e
        finally:
            cursor.close()

    def executar_many(self, query: str, params_list: List[tuple], commit: bool = True):
        """Executa query com múltiplos parâmetros"""
        if not self.conn:
            raise ConnectionError("Não conectado ao PostgreSQL")

        cursor = self.conn.cursor()
        try:
            cursor.executemany(query, params_list)
            if commit:
                self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            raise e
        finally:
            cursor.close()

    @contextmanager
    def cursor(self, dict_cursor: bool = True) -> Generator:
        """Context manager para cursor"""
        if not self.conn:
            raise ConnectionError("Não conectado ao PostgreSQL")

        factory = RealDictCursor if dict_cursor else None
        cursor = self.conn.cursor(cursor_factory=factory)
        try:
            yield cursor
        finally:
            cursor.close()

    def commit(self):
        """Commit explicito da transacao"""
        if self.conn:
            self.conn.commit()

    @contextmanager
    def transaction(self) -> Generator:
        """Context manager para transação"""
        try:
            yield self
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise


@contextmanager
def farmasoft_connection() -> Generator[FarmasoftConnection, None, None]:
    """Context manager para conexão Farmasoft"""
    conn = FarmasoftConnection()
    if not conn.conectar():
        raise ConnectionError("Falha ao conectar ao Farmasoft. Verifique as configuracoes.")
    try:
        yield conn
    finally:
        conn.desconectar()


@contextmanager
def postgres_connection() -> Generator[PostgresConnection, None, None]:
    """Context manager para conexão PostgreSQL"""
    conn = PostgresConnection()
    conn.conectar()
    try:
        yield conn
    finally:
        conn.desconectar()
