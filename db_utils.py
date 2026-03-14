"""
Utilitários de Banco de Dados - Versão 3.0
Melhorias: bcrypt para senhas, pool de conexões, caching
"""

import sqlite3
import pandas as pd
from pathlib import Path
import bcrypt
import logging
import os
import re
from functools import lru_cache
from typing import Optional
from db.connection_pool import get_pool, init_pool
from db.db_config import BCRYPT_ROUNDS, DB_PATH

logger = logging.getLogger(__name__)

import datetime
from db.rules_utils import init_rules_table

# NÃO inicializar pool aqui - será lazy-initialized em get_pool()
# Isso evita criar pool com arquivo antigo antes da importação substituir

# ============ FUNÇÕES DE CONEXÃO ============

def db_connect():
    """Retorna uma conexão do pool"""
    return get_pool().get_connection()

# ============ FUNÇÕES DE SEGURANÇA (BCRYPT) ============

def hash_password(senha: str) -> str:
    """
    Hash seguro de senha usando bcrypt
    
    Args:
        senha: Senha em texto plano
    
    Returns:
        Hash da senha (bcrypt)
    """
    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    return bcrypt.hashpw(senha.encode('utf-8'), salt).decode('utf-8')

def check_password(senha: str, hash_senha: str) -> bool:
    """
    Verifica se a senha corresponde ao hash
    
    Args:
        senha: Senha em texto plano
        hash_senha: Hash do bcrypt
    
    Returns:
        True se a senha é válida
    """
    try:
        return bcrypt.checkpw(senha.encode('utf-8'), hash_senha.encode('utf-8'))
    except (ValueError, TypeError):
        logger.error("Erro ao verificar password - hash inválido")
        return False

# ============ TABELAS ============

def init_db():
    """Inicializa o banco de dados com todas as tabelas necessárias"""
    # Verificar integridade e recuperar se necessário
    try:
        with sqlite3.connect(str(DB_PATH), timeout=10) as test_conn:
            test_conn.execute("PRAGMA integrity_check")
            test_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.DatabaseError as e:
        logger.error(f"Banco corrompido detectado: {e}. Tentando recuperação...")
        try:
            # Tentar recuperação via dump
            import os
            backup_path = str(DB_PATH) + ".corrupted_backup"
            if Path(DB_PATH).exists():
                os.rename(str(DB_PATH), backup_path)
                logger.info(f"Banco corrompido movido para {backup_path}")
            # O banco será recriado abaixo
        except Exception as recovery_error:
            logger.error(f"Erro na recuperação: {recovery_error}")
    
    # Cria o esquema compatível com o dump histórico (pilotos com 'equipe', provas com 'horario_prova' e 'tipo', resultados com 'posicoes')
    with db_connect() as conn:
        c = conn.cursor()

        # Tabela de usuários (compatível)
        c.execute('''
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT,
                email TEXT UNIQUE,
                senha_hash TEXT,
                perfil TEXT,
                status TEXT DEFAULT 'Ativo',
                faltas INTEGER DEFAULT 0,
                must_change_password INTEGER DEFAULT 0,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Tabela de pilotos (legacy format: equipe, status)
        c.execute('''
            CREATE TABLE IF NOT EXISTS pilotos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT,
                equipe TEXT,
                status TEXT DEFAULT 'Ativo'
            )
        ''')

        # Tabela de provas (with horario_prova and tipo)
        c.execute('''
            CREATE TABLE IF NOT EXISTS provas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT,
                data TEXT,
                horario_prova TEXT,
                status TEXT DEFAULT 'Ativo',
                tipo TEXT DEFAULT 'Normal'
            )
        ''')

        # Tabela de apostas (legacy structure used across the UI)
        c.execute('''
            CREATE TABLE IF NOT EXISTS apostas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER,
                prova_id INTEGER,
                data_envio TEXT,
                pilotos TEXT,
                fichas TEXT,
                piloto_11 TEXT,
                nome_prova TEXT,
                automatica INTEGER DEFAULT 0,
                FOREIGN KEY(usuario_id) REFERENCES usuarios(id),
                FOREIGN KEY(prova_id) REFERENCES provas(id)
            )
        ''')

        # Tabela de resultados (posicoes como texto serializado + abandonos)
        c.execute('''
            CREATE TABLE IF NOT EXISTS resultados (
                prova_id INTEGER PRIMARY KEY,
                posicoes TEXT,
                abandono_pilotos TEXT,
                FOREIGN KEY(prova_id) REFERENCES provas(id)
            )
        ''')

        # Tabela de posições por participante (Hall da Fama / histórico)
        c.execute('''
            CREATE TABLE IF NOT EXISTS posicoes_participantes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prova_id INTEGER NOT NULL,
                usuario_id INTEGER NOT NULL,
                posicao INTEGER NOT NULL,
                pontos REAL NOT NULL,
                data_registro TEXT DEFAULT (datetime('now')),
                temporada TEXT,
                UNIQUE(prova_id, usuario_id),
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id),
                FOREIGN KEY (prova_id) REFERENCES provas(id)
            )
        ''')

        # Tabela de log de tentativas de login (para rate limiting)
        c.execute('''
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                tentativa_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sucesso BOOLEAN DEFAULT 0,
                ip_address TEXT,
                action TEXT DEFAULT 'login'
            )
        ''')

        # Inicializar regras
        init_rules_table()
        conn.commit()
        _get_existing_columns_cached.cache_clear()
        logger.info("✓ Banco de dados inicializado com sucesso")

# ============ OPERAÇÕES CRUD ============

SAFE_DYNAMIC_TABLES = {
    "usuarios",
    "pilotos",
    "provas",
    "apostas",
    "resultados",
    "log_apostas",
    "regras",
    "login_attempts",
    "posicoes_participantes",
    "usuarios_status_historico",
}


def _sanitize_identifier(identifier: str) -> str:
    value = (identifier or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Identificador SQL inválido: {identifier}")
    return value


def _validate_dynamic_table_name(table: str) -> str:
    table_name = _sanitize_identifier(table)
    if table_name not in SAFE_DYNAMIC_TABLES:
        raise ValueError(f"Tabela não permitida para SQL dinâmico: {table_name}")
    return table_name


def _quote_identifier(identifier: str) -> str:
    sanitized = _sanitize_identifier(identifier)
    return f'"{sanitized}"'

@lru_cache(maxsize=64)
def _get_existing_columns_cached(table: str) -> tuple[str, ...]:
    table_name = _validate_dynamic_table_name(table)
    with db_connect() as conn:
        c = conn.cursor()
        c.execute(f"PRAGMA table_info('{table_name}')")
        cols = tuple(r[1] for r in c.fetchall())
    return cols


def _get_existing_columns(table: str, preferred: Optional[list[str]] = None) -> list[str]:
    cols = list(_get_existing_columns_cached(table))
    if preferred:
        return [c for c in preferred if c in cols]
    return cols


def get_user_by_email(email: str) -> Optional[dict]:
    """
    Retorna usuário pelo email
    
    Args:
        email: Email do usuário
    
    Returns:
        Dict com dados do usuário ou None
    """
    cols = _get_existing_columns('usuarios')
    cols_sql = ', '.join(_quote_identifier(c) for c in cols)
    with db_connect() as conn:
        c = conn.cursor()
        c.execute(f"SELECT {cols_sql} FROM usuarios WHERE email = ?", (email,))
        row = c.fetchone()
        
        if row:
            return dict(row)
        return None

def get_master_user() -> Optional[dict]:
    """Retorna o usuário Master se existir"""
    return get_user_by_email('master@sistema.local')

def cadastrar_usuario(nome: str, email: str, senha: str, perfil: str = "participante"):
    """Registra novo usuário com senha bcrypt"""
    senha_hash = hash_password(senha)
    with db_connect() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO usuarios (nome, email, senha_hash, perfil) VALUES (?, ?, ?, ?)',
            (nome, email, senha_hash, perfil)
        )
        conn.commit()
        logger.info(f"✓ Usuário cadastrado: {email}")

def autenticar_usuario(email: str, senha: str) -> dict:
    """Autentica usuário com bcrypt"""
    usuario = get_user_by_email(email)
    if usuario and check_password(senha, usuario.get('senha_hash', '')):
        return usuario
    return {}

def get_user_by_id(user_id: int) -> Optional[dict]:
    """
    Retorna usuário pelo ID
    
    Args:
        user_id: ID do usuário
    
    Returns:
        Dict com dados do usuário ou None
    """
    cols = _get_existing_columns('usuarios')
    cols_sql = ', '.join(_quote_identifier(c) for c in cols)
    with db_connect() as conn:
        c = conn.cursor()
        c.execute(f"SELECT {cols_sql} FROM usuarios WHERE id = ?", (user_id,))
        row = c.fetchone()
        
        if row:
            return dict(row)
        return None

def get_usuarios_df() -> pd.DataFrame:
    """Retorna todos os usuários como DataFrame"""
    cols = _get_existing_columns('usuarios')
    cols_sql = ', '.join(_quote_identifier(c) for c in cols)
    with db_connect() as conn:
        return pd.read_sql_query(f"SELECT {cols_sql} FROM usuarios", conn)


def _usuarios_status_historico_exists(conn) -> bool:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='usuarios_status_historico'"
    )
    return cursor.fetchone() is not None


def usuarios_status_historico_disponivel() -> bool:
    """Indica se há histórico de status de usuários para filtros sazonais confiáveis."""
    with db_connect() as conn:
        return _usuarios_status_historico_exists(conn)


def get_participantes_temporada_df(temporada: Optional[str] = None) -> pd.DataFrame:
    """Retorna participantes ativos na temporada selecionada.

    Se a tabela de historico de status existir, considera o status ativo no periodo.
    Caso contrario, usa o status atual do cadastro de usuarios.
    """
    if temporada is None:
        temporada = str(datetime.datetime.now().year)
    season_start = f"{temporada}-01-01 00:00:00"
    season_end = f"{temporada}-12-31 23:59:59"

    cols = _get_existing_columns('usuarios')
    cols_sql = ', '.join(_quote_identifier(c) for c in cols)
    with db_connect() as conn:
        if not _usuarios_status_historico_exists(conn):
            if 'status' in cols:
                return pd.read_sql_query(
                    f"""
                    SELECT {cols_sql}
                    FROM usuarios
                    WHERE lower(trim(coalesce(status, ''))) = 'ativo'
                    """,
                    conn,
                )
            return pd.read_sql_query(f"SELECT {cols_sql} FROM usuarios", conn)

        query = f"""
            SELECT DISTINCT {', '.join(f'u.{_quote_identifier(c)}' for c in cols)}
            FROM usuarios u
            JOIN usuarios_status_historico h ON h.usuario_id = u.id
                        WHERE lower(trim(coalesce(h.status, ''))) = 'ativo'
              AND datetime(h.inicio_em) <= datetime(?)
              AND (h.fim_em IS NULL OR datetime(h.fim_em) >= datetime(?))
        """
        return pd.read_sql_query(query, conn, params=(season_end, season_start))


def get_usuario_temporadas_ativas(user_id: int) -> list[str]:
    """Retorna temporadas em que o usuário esteve com status ativo.

    Usa histórico de status quando disponível. Sem histórico:
    - usuário ativo: retorna temporadas existentes em `provas`
    - usuário inativo: retorna lista vazia
    """
    with db_connect() as conn:
        c = conn.cursor()

        # Lista base de temporadas existentes em provas
        temporadas_df = pd.read_sql_query(
            """
            SELECT DISTINCT COALESCE(NULLIF(TRIM(temporada), ''), SUBSTR(data, 1, 4)) AS temporada
            FROM provas
            WHERE COALESCE(NULLIF(TRIM(temporada), ''), SUBSTR(data, 1, 4)) IS NOT NULL
            ORDER BY temporada
            """,
            conn,
        )
        temporadas_base = [str(t).strip() for t in temporadas_df.get("temporada", []).tolist() if str(t).strip()]
        if not temporadas_base:
            return []

        if not _usuarios_status_historico_exists(conn):
            c.execute("SELECT status FROM usuarios WHERE id = ?", (int(user_id),))
            row = c.fetchone()
            status = str(row[0]).strip().lower() if row and row[0] is not None else ""
            return temporadas_base if status == "ativo" else []

        temporadas_ativas_df = pd.read_sql_query(
            """
            SELECT DISTINCT s.temporada
            FROM (
                SELECT DISTINCT COALESCE(NULLIF(TRIM(temporada), ''), SUBSTR(data, 1, 4)) AS temporada
                FROM provas
            ) s
            JOIN usuarios_status_historico h ON h.usuario_id = ?
            WHERE LOWER(TRIM(COALESCE(h.status, ''))) = 'ativo'
              AND DATETIME(h.inicio_em) <= DATETIME(s.temporada || '-12-31 23:59:59')
              AND (h.fim_em IS NULL OR DATETIME(h.fim_em) >= DATETIME(s.temporada || '-01-01 00:00:00'))
            ORDER BY s.temporada
            """,
            conn,
            params=(int(user_id),),
        )

        temporadas_ativas = [str(t).strip() for t in temporadas_ativas_df.get("temporada", []).tolist() if str(t).strip()]
        return temporadas_ativas


def registrar_historico_status_usuario(
    usuario_id: int,
    novo_status: str,
    alterado_por: Optional[int] = None,
    motivo: Optional[str] = None,
    data_referencia: Optional[str] = None,
) -> None:
    """Registra alteracao de status do usuario no historico.

    Fecha o periodo anterior e abre um novo registro com o status informado.
    """
    if data_referencia is None:
        data_referencia = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with db_connect() as conn:
        cursor = conn.cursor()
        if not _usuarios_status_historico_exists(conn):
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS usuarios_status_historico (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    usuario_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    inicio_em TIMESTAMP NOT NULL,
                    fim_em TIMESTAMP,
                    alterado_por INTEGER,
                    motivo TEXT,
                    FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
                )
            ''')

        cursor.execute(
            "SELECT id, status FROM usuarios_status_historico WHERE usuario_id = ? AND fim_em IS NULL ORDER BY inicio_em DESC LIMIT 1",
            (usuario_id,)
        )
        row = cursor.fetchone()
        if row and row[1] == novo_status:
            return

        if row:
            cursor.execute(
                "UPDATE usuarios_status_historico SET fim_em = ? WHERE id = ?",
                (data_referencia, row[0])
            )

        cursor.execute(
            """
            INSERT INTO usuarios_status_historico (usuario_id, status, inicio_em, fim_em, alterado_por, motivo)
            VALUES (?, ?, ?, NULL, ?, ?)
            """,
            (usuario_id, novo_status, data_referencia, alterado_por, motivo)
        )
        conn.commit()

def get_pilotos_df() -> pd.DataFrame:
    """Retorna todos os pilotos como DataFrame"""
    cols = _get_existing_columns('pilotos')
    cols_sql = ', '.join(_quote_identifier(c) for c in cols)
    with db_connect() as conn:
        return pd.read_sql_query(f"SELECT {cols_sql} FROM pilotos", conn)

def _read_table_df(table: str, temporada: Optional[str] = None, columns: Optional[list[str]] = None) -> pd.DataFrame:
    """Helper: read table into DataFrame, filtering by `temporada` when column exists.

    If `temporada` is None, defaults to current year as string.
    Includes NULL temporada rows for backward compatibility with existing data.
    """
    table_name = _validate_dynamic_table_name(table)
    if temporada is None:
        temporada = str(datetime.datetime.now().year)
    cols = _get_existing_columns(table, columns)
    cols_sql = ', '.join(_quote_identifier(c) for c in cols)
    with db_connect() as conn:
        if 'temporada' in cols:
            # Include rows where temporada matches OR temporada is NULL (backward compat)
            return pd.read_sql_query(
                f"SELECT {cols_sql} FROM {table_name} WHERE temporada = ? OR temporada IS NULL",
                conn,
                params=(temporada,)
            )
        else:
            return pd.read_sql_query(f"SELECT {cols_sql} FROM {table_name}", conn)


def get_provas_df(temporada: Optional[str] = None) -> pd.DataFrame:
    """Retorna todas as provas como DataFrame (filtra por `temporada` quando disponível)."""
    return _read_table_df('provas', temporada)


def get_apostas_df(temporada: Optional[str] = None) -> pd.DataFrame:
    """Retorna todas as apostas como DataFrame (filtra por `temporada` quando disponível)."""
    return _read_table_df('apostas', temporada)


def get_resultados_df(temporada: Optional[str] = None) -> pd.DataFrame:
    """Retorna todos os resultados como DataFrame (filtra por `temporada` quando disponível)."""
    return _read_table_df('resultados', temporada)


def registrar_log_aposta(*args, **kwargs):
    """Registro flexível de log de apostas.

    Supports two call patterns for backward compatibility:
    1) registrar_log_aposta(usuario_id, prova_id, piloto_id, pontos=0, temporada=None)
    2) registrar_log_aposta(apostador=..., pilotos=..., aposta=..., nome_prova=..., piloto_11=..., tipo_aposta=..., automatica=..., horario=...)

    If pattern (2) is used, entries are stored in an `log_apostas` table (created on demand).
    If pattern (1) is used, an entry is inserted into `apostas` (respecting `temporada` column when present).
    
    Pattern (2) fields:
    - apostador: username/name of bettor
    - pilotos: comma-separated list of pilot names (e.g., "Oscar Piastri, Max Verstappen, George Russell")
    - aposta: comma-separated list of chips/fichas (e.g., "7, 7, 1")
    - nome_prova: race name
    - piloto_11: predicted 11th place finisher
    - tipo_aposta: 0=on-time, 1=late
    - automatica: 0=manual, 1+=automatic (with penalty if >=2)
    - horario: timestamp of bet registration
    - temporada: season/year (optional, defaults to current year)
    """
    # Pattern 2: verbose logging via kwargs
    if kwargs and ('apostador' in kwargs or 'aposta' in kwargs):
        apostador = kwargs.get('apostador')
        aposta = kwargs.get('aposta')
        pilotos = kwargs.get('pilotos')
        nome_prova = kwargs.get('nome_prova')
        piloto_11 = kwargs.get('piloto_11')
        tipo_aposta = kwargs.get('tipo_aposta')
        automatica = kwargs.get('automatica')
        horario = kwargs.get('horario')
        ip_address = kwargs.get('ip_address')
        temporada = kwargs.get('temporada', str(datetime.datetime.now().year))
        status = kwargs.get('status', 'Registrada')
        usuario_id = kwargs.get('usuario_id')
        prova_id = kwargs.get('prova_id')

        # Derivar data/horario strings
        data_str = None
        horario_str = None
        try:
            if horario:
                data_str = getattr(horario, 'date', lambda: None)()
                data_str = data_str.isoformat() if data_str else None
                horario_str = horario.isoformat() if hasattr(horario, 'isoformat') else str(horario)
        except Exception:
            data_str = None
            horario_str = None

        with db_connect() as conn:
            c = conn.cursor()
            # create log table if not exists (using log_apostas name for consistency)
            c.execute(f'''
                CREATE TABLE IF NOT EXISTS log_apostas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    usuario_id INTEGER,
                    prova_id INTEGER,
                    apostador TEXT,
                    aposta TEXT,
                    nome_prova TEXT,
                    pilotos TEXT,
                    piloto_11 TEXT,
                    tipo_aposta INTEGER,
                    automatica INTEGER,
                    data TEXT,
                    horario TIMESTAMP,
                    ip_address TEXT,
                    temporada TEXT DEFAULT '{datetime.datetime.now().year}',
                    status TEXT DEFAULT 'Registrada',
                    data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (usuario_id) REFERENCES usuarios(id),
                    FOREIGN KEY (prova_id) REFERENCES provas(id)
                )
            ''')
            # Check if temporada column exists
            c.execute("PRAGMA table_info('log_apostas')")
            cols = [r[1] for r in c.fetchall()]
            insert_cols = [
                'apostador', 'aposta', 'nome_prova', 'pilotos', 'piloto_11',
                'tipo_aposta', 'automatica', 'data', 'horario'
            ]
            insert_vals = [
                apostador, aposta, nome_prova, pilotos, piloto_11,
                tipo_aposta, automatica, data_str, horario_str
            ]
            if 'ip_address' in cols:
                insert_cols.append('ip_address')
                insert_vals.append(ip_address)
            if 'usuario_id' in cols:
                insert_cols.insert(0, 'usuario_id')
                insert_vals.insert(0, usuario_id)
            if 'prova_id' in cols:
                insert_cols.insert(1, 'prova_id')
                insert_vals.insert(1, prova_id)
            if 'temporada' in cols:
                insert_cols.append('temporada')
                insert_vals.append(temporada)
            if 'status' in cols:
                insert_cols.append('status')
                insert_vals.append(status)
            placeholders = ', '.join(['?'] * len(insert_cols))
            cols_sql = ', '.join(insert_cols)
            c.execute(
                f'INSERT INTO log_apostas ({cols_sql}) VALUES ({placeholders})',
                tuple(insert_vals)
            )
            conn.commit()
            logger.info(f"✓ Aposta log registrada (log_apostas): {apostador} - {nome_prova}")
        return

    # Pattern 1: positional insert into apostas
    # Normalize args
    usuario_id = None
    prova_id = None
    piloto_id = None
    pontos = 0
    temporada = None
    if len(args) >= 1:
        usuario_id = args[0]
    if len(args) >= 2:
        prova_id = args[1]
    if len(args) >= 3:
        piloto_id = args[2]
    if len(args) >= 4:
        pontos = args[3]
    # kwargs override
    if 'usuario_id' in kwargs:
        usuario_id = kwargs.get('usuario_id')
    if 'prova_id' in kwargs:
        prova_id = kwargs.get('prova_id')
    if 'piloto_id' in kwargs:
        piloto_id = kwargs.get('piloto_id')
    if 'pontos' in kwargs:
        pontos = kwargs.get('pontos')
    if 'temporada' in kwargs:
        temporada = kwargs.get('temporada')

    if temporada is None:
        temporada = str(datetime.datetime.now().year)

    with db_connect() as conn:
        c = conn.cursor()
        # Detect if temporada column exists
        c.execute("PRAGMA table_info('apostas')")
        cols = [r[1] for r in c.fetchall()]
        if 'temporada' in cols:
            c.execute(
                'INSERT INTO apostas (usuario_id, prova_id, piloto_id, pontos, temporada) VALUES (?, ?, ?, ?, ?)',
                (usuario_id, prova_id, piloto_id, pontos, temporada)
            )
        else:
            c.execute(
                'INSERT INTO apostas (usuario_id, prova_id, piloto_id, pontos) VALUES (?, ?, ?, ?)',
                (usuario_id, prova_id, piloto_id, pontos)
            )
        conn.commit()
        logger.info(f"✓ Aposta registrada: usuário {usuario_id}, prova {prova_id}, piloto {piloto_id}")


def log_aposta_existe(usuario_id: int, prova_id: int, temporada: Optional[str] = None) -> bool:
    """Verifica se existe aposta para usuário em uma prova (opcionalmente filtrando por temporada)."""
    if temporada is None:
        temporada = str(datetime.datetime.now().year)
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("PRAGMA table_info('apostas')")
        cols = [r[1] for r in c.fetchall()]
        if 'temporada' in cols:
            c.execute('SELECT 1 FROM apostas WHERE usuario_id = ? AND prova_id = ? AND temporada = ?', (usuario_id, prova_id, temporada))
        else:
            c.execute('SELECT 1 FROM apostas WHERE usuario_id = ? AND prova_id = ?', (usuario_id, prova_id))
        return c.fetchone() is not None

def update_user_email(user_id: int, novo_email: str) -> bool:
    """Atualiza o email do usuário"""
    try:
        with db_connect() as conn:
            c = conn.cursor()
            c.execute('UPDATE usuarios SET email = ? WHERE id = ?', (novo_email, user_id))
            conn.commit()
            logger.info(f"✓ Email do usuário {user_id} atualizado")
            return True
    except Exception as e:
        logger.error(f"Erro ao atualizar email: {e}")
        return False

def update_user_password(user_id: int, nova_senha: str) -> bool:
    """Atualiza a senha do usuário"""
    try:
        if isinstance(nova_senha, str) and nova_senha.startswith("$2"):
            senha_hash = nova_senha
        else:
            senha_hash = hash_password(nova_senha)
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("PRAGMA table_info('usuarios')")
            cols = [r[1] for r in c.fetchall()]
            if 'must_change_password' in cols:
                c.execute(
                    'UPDATE usuarios SET senha_hash = ?, must_change_password = 0 WHERE id = ?',
                    (senha_hash, user_id)
                )
            else:
                c.execute('UPDATE usuarios SET senha_hash = ? WHERE id = ?', (senha_hash, user_id))
            conn.commit()
            logger.info(f"✓ Senha do usuário {user_id} atualizada")
            return True
    except Exception as e:
        logger.error(f"Erro ao atualizar senha: {e}")
        return False

def get_horario_prova(prova_id: int) -> tuple:
    """
    Retorna informações da prova (nome, data, horário)
    
    Args:
        prova_id: ID da prova
    
    Returns:
        Tupla com (nome_prova, data_prova, horario_prova) ou (None, None, None)
    """
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("PRAGMA table_info('provas')")
        cols = [r[1] for r in c.fetchall()]
        if 'horario_prova' in cols:
            c.execute('SELECT nome, data, horario_prova FROM provas WHERE id = ?', (prova_id,))
        else:
            c.execute('SELECT nome, data FROM provas WHERE id = ?', (prova_id,))
        row = c.fetchone()

        if row:
            if 'horario_prova' in cols:
                nome, data, horario = row
            else:
                nome, data = row
                horario = "00:00"
            horario = horario or "00:00"
            return (nome, data, horario)
        return (None, None, None)
