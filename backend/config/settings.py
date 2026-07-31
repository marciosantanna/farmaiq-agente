"""
Configurações do Sistema Gerencial
"""
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List

# Carregar variaveis de ambiente do .env
from dotenv import load_dotenv

# Diretório base do projeto
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Carregar .env do diretorio raiz
load_dotenv(BASE_DIR / ".env")


@dataclass
class FarmasoftConfig:
    """Configurações de conexão com Farmasoft (Firebird)"""
    host: str = "localhost"
    port: int = 3050
    database: str = ""
    user: str = "SYSDBA"
    password: str = ""
    charset: str = "WIN1252"
    fb_library_name: str = ""

    @classmethod
    def from_env(cls) -> "FarmasoftConfig":
        return cls(
            host=os.getenv("FARMASOFT_HOST", "localhost"),
            port=int(os.getenv("FARMASOFT_PORT", "3050")),
            database=os.getenv("FARMASOFT_DATABASE", ""),
            user=os.getenv("FARMASOFT_USER", "SYSDBA"),
            password=os.getenv("FARMASOFT_PASSWORD", ""),
            charset=os.getenv("FARMASOFT_CHARSET", "WIN1252"),
            fb_library_name=os.getenv("FARMASOFT_FB_LIBRARY", ""),
        )


@dataclass
class PostgresConfig:
    """Configurações de conexão com PostgreSQL"""
    host: str = "localhost"
    port: int = 5432
    database: str = "gerencial"
    user: str = "postgres"
    password: str = ""

    @classmethod
    def from_env(cls) -> "PostgresConfig":
        return cls(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            database=os.getenv("POSTGRES_DATABASE", "gerencial"),
            user=os.getenv("POSTGRES_USER", "postgres"),
            password=os.getenv("POSTGRES_PASSWORD", ""),
        )

    @property
    def connection_string(self) -> str:
        # DATABASE_URL tem prioridade (Neon/Render/Railway)
        url = os.getenv("DATABASE_URL")
        if url:
            return url
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


@dataclass
class WhatsAppConfig:
    """Configurações de integração WhatsApp (Evolution API)"""
    api_url: str = ""
    api_key: str = ""
    instance: str = ""
    ativo: bool = False

    @classmethod
    def from_env(cls) -> "WhatsAppConfig":
        return cls(
            api_url=os.getenv("EVOLUTION_API_URL", ""),
            api_key=os.getenv("EVOLUTION_API_KEY", ""),
            instance=os.getenv("EVOLUTION_INSTANCE", ""),
            ativo=os.getenv("WHATSAPP_ATIVO", "false").lower() == "true",
        )


@dataclass
class MargemConfig:
    """Configurações de análise de margem"""
    margem_critica: float = 5.0
    margem_atencao: float = 15.0
    margem_ideal: float = 25.0
    dias_historico: int = 180  # Aumentado para 180 dias (melhor analise de sazonalidade)
    queda_alerta: float = 10.0  # Percentual de queda que gera alerta


@dataclass
class Settings:
    """Configurações gerais do sistema"""
    farmasoft: FarmasoftConfig = field(default_factory=FarmasoftConfig)
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    whatsapp: WhatsAppConfig = field(default_factory=WhatsAppConfig)
    margem: MargemConfig = field(default_factory=MargemConfig)

    # Filial padrão (pode ser alterada dinamicamente)
    filial_id: int = 1

    # Horários de sincronização
    sync_manha: str = "07:00"
    sync_noite: str = "19:00"

    # Debug mode
    debug: bool = False

    # Modo cloud: True = le PostgreSQL (dados sincronizados), False = le Farmasoft direto
    cloud_mode: bool = False

    # Chave de API para o agent (validacao de origem)
    agent_api_key: str = ""

    # URL do webhook local do agent (DuckDNS ou IP local)
    agent_webhook_url: str = ""

    # Identificacao da loja
    nome_loja: str = "DonaFarma"

    # Autenticacao
    admin_secret: str = ""
    jwt_secret: str = "gerencial_jwt_secret_2026"
    jwt_expire_hours: int = 8

    @classmethod
    def load(cls) -> "Settings":
        """Carrega configurações do ambiente"""
        return cls(
            farmasoft=FarmasoftConfig.from_env(),
            postgres=PostgresConfig.from_env(),
            whatsapp=WhatsAppConfig.from_env(),
            margem=MargemConfig(),
            filial_id=int(os.getenv("FILIAL_ID", "1")),
            debug=os.getenv("DEBUG", "false").lower() == "true",
            cloud_mode=os.getenv("CLOUD_MODE", "false").lower() == "true",
            agent_api_key=os.getenv("AGENT_API_KEY", ""),
            agent_webhook_url=os.getenv("AGENT_WEBHOOK_URL", ""),
            nome_loja=os.getenv("NOME_LOJA", "DonaFarma"),
            admin_secret=os.getenv("ADMIN_SECRET", ""),
            jwt_secret=os.getenv("JWT_SECRET", "gerencial_jwt_secret_2026"),
            jwt_expire_hours=int(os.getenv("JWT_EXPIRE_HOURS", "8")),
        )


# Agrupamento de GRUPOS do Farmasoft (tabela GRUPOS, campo DESCRICAO)
# Baseado na estrutura real do banco Farmax
AGRUPAMENTO_CLASSES: Dict[str, List[str]] = {
    "ETICOS": [
        "MEDICAMENT",           # CD_GRUPO 119 - Medicamentos eticos
        "CONTROLADO (MEDIC)",   # CD_GRUPO 401 - Controlados medicamentos
        "ANTICOCEPCIONAIS",     # CD_GRUPO 707
        "MEDICAMENTO CONTINUO", # CD_GRUPO 712
    ],
    "GENERICOS_SIMILARES": [
        "GENERICO",             # CD_GRUPO 301
        "SIMILAR",              # CD_GRUPO 701
        "CONTROLADO (GEN)",     # CD_GRUPO 702 - Controlados genericos
        "FARMACIA POPULAR",     # CD_GRUPO 704
        "USO CONTINUO",         # CD_GRUPO 710
        "GRUPO 2%",             # CD_GRUPO 713
        "PROMOCAO 5%",          # CD_GRUPO 715
    ],
    "PERFUMARIA": [
        "PERFUMARIA",           # CD_GRUPO 201
        "MERCEARIA",            # CD_GRUPO 501
    ],
    "CESTO": [
        "CAIXAS DE SALAO",      # CD_GRUPO 711 - Produtos de cesto/balcao
        "CESTO",                # Alias
    ],
    "VITAMINAS": [
        "VITAMINAS",            # CD_GRUPO 714
    ],
    "CARTELADOS": [
        "CARTELADOS",           # CD_GRUPO 706
    ],
    "ONEROSOS": [
        "ONEROSOS",             # CD_GRUPO 708
    ],
}

# Mapeamento de CD_GRUPO para categoria (para queries diretas por codigo)
MAPEAMENTO_CD_GRUPO: Dict[int, str] = {
    # ETICOS
    119: "ETICOS",    # MEDICAMENT
    401: "ETICOS",    # CONTROLADO (MEDIC)
    707: "ETICOS",    # ANTICOCEPCIONAIS
    712: "ETICOS",    # MEDICAMENTO CONTINUO
    # GENERICOS_SIMILARES
    301: "GENERICOS_SIMILARES",  # GENERICO
    701: "GENERICOS_SIMILARES",  # SIMILAR
    702: "GENERICOS_SIMILARES",  # CONTROLADO (GEN)
    704: "GENERICOS_SIMILARES",  # FARMACIA POPULAR
    710: "GENERICOS_SIMILARES",  # USO CONTINUO
    713: "GENERICOS_SIMILARES",  # GRUPO 2%
    715: "GENERICOS_SIMILARES",  # PROMOCAO 5%
    # PERFUMARIA
    201: "PERFUMARIA",  # PERFUMARIA
    501: "PERFUMARIA",  # MERCEARIA
    # CESTO (produtos de cesto/balcao)
    711: "CESTO",       # CAIXAS DE SALAO / CESTO
    # CATEGORIAS SEPARADAS
    714: "VITAMINAS",   # VITAMINAS
    706: "CARTELADOS",  # CARTELADOS
    708: "ONEROSOS",    # ONEROSOS
}

# Categorias validas
CATEGORIAS_VALIDAS = ["ETICOS", "GENERICOS_SIMILARES", "PERFUMARIA", "CESTO", "VITAMINAS", "CARTELADOS", "ONEROSOS"]

# Status de margem
STATUS_MARGEM = {
    "CRITICO": "🔴",
    "ATENCAO": "🟡",
    "OK": "🟢",
}


# Instância global de configurações
settings = Settings.load()
