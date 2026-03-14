import streamlit as st
import pandas as pd
import sqlite3
import os
import io  # IMPORTANTE: necessário para exportar Excel em memória
import shutil
import re  # Para conversão de sintaxe SQL
from pathlib import Path
from datetime import datetime
from db.db_utils import db_connect
from db.db_config import DB_PATH  # Importar caminho correto do banco


def _parse_backup_datetime_value(value):
    """Normaliza valores de data/hora de importacao sem acionar warnings do pandas."""
    if pd.isna(value):
        return None

    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')

    raw = str(value).strip()
    if not raw:
        return None

    formatos_explicitos = (
        '%Y-%m-%dT%H:%M:%S.%f%z',
        '%Y-%m-%dT%H:%M:%S%z',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d',
        '%d/%m/%Y %H:%M:%S',
        '%d/%m/%Y',
    )
    for formato in formatos_explicitos:
        parsed = pd.to_datetime(raw, format=formato, errors='coerce')
        if pd.notna(parsed):
            return parsed.strftime('%Y-%m-%d %H:%M:%S')

    usa_dayfirst = bool(re.match(r'^\d{1,2}[-/]\d{1,2}[-/]\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?$', raw))
    parsed = pd.to_datetime(raw, errors='coerce', dayfirst=usa_dayfirst)
    if pd.notna(parsed):
        return parsed.strftime('%Y-%m-%d %H:%M:%S')
    return None


def _require_master_access():
    perfil = st.session_state.get("user_role", "participante")
    if perfil != "master":
        st.warning("Acesso restrito ao usuário master.")
        st.stop()

def download_db():
    """Permite fazer o download do arquivo inteiro do banco de dados SQLite (versão limpa e consolidada)."""
    _require_master_access()
    if DB_PATH.exists():
        import tempfile
        
        try:
            # Consolidar WAL no banco original
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
            
            # Criar versão limpa via backup API do sqlite3
            temp_dir = tempfile.mkdtemp()
            temp_clean = Path(temp_dir) / "bolao_f1_clean.db"
            
            st.info("🔄 Preparando banco de dados limpo para download...")
            
            # Usar backup API do sqlite3 (mais confiável)
            source = sqlite3.connect(str(DB_PATH), timeout=30)
            dest = sqlite3.connect(str(temp_clean), timeout=30)
            
            with source:
                source.backup(dest)
            
            source.close()
            
            # Otimizar banco destino
            with dest:
                dest.execute("PRAGMA integrity_check")
                dest.execute("VACUUM")
            dest.close()
            
            # Ler arquivo limpo
            with open(temp_clean, "rb") as fp:
                db_data = fp.read()
            
            # Limpar temporário
            shutil.rmtree(temp_dir)
            
            st.download_button(
                label="⬇️ Baixar banco de dados completo (limpo e consolidado)",
                data=db_data,
                file_name=f"bolao_f1_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db",
                mime="application/octet-stream",
                width="stretch",
                help="Banco de dados validado, consolidado e livre de corrupção WAL"
            )
            
        except Exception as e:
            st.error(f"⚠️ Erro ao preparar download: {e}")
            st.info("Tentando download direto (pode conter WAL não consolidado)...")
            # Fallback: download direto
            try:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except:
                pass
            with open(DB_PATH, "rb") as fp:
                db_data = fp.read()
            st.download_button(
                label="⬇️ Baixar banco de dados completo (.db)",
                data=db_data,
                file_name=DB_PATH.name,
                mime="application/octet-stream",
                width="stretch"
            )
    else:
        st.warning(f"⚠️ Arquivo do banco de dados não encontrado: {DB_PATH}")
        st.info(f"📍 Caminho esperado: {DB_PATH.absolute()}")

def upload_db():
    """Permite upload de um novo arquivo .db ou .sql, substituindo o banco atual."""
    _require_master_access()
    
    # Mostrar mensagem de sucesso se houver importação recente
    if 'import_success' in st.session_state:
        info = st.session_state.import_success
        
        if info.get('type') == 'db':
            st.success("✅ Banco de dados .db validado e restaurado com sucesso!")
        else:
            st.success(f"✅ Importação concluída: {info['tables']} tabelas, {info['records']} registros")
            if info['errors'] > 0:
                st.warning(f"⚠️ {info['errors']} comandos falharam (podem ser erros esperados de sintaxe)")
        st.info(f"📊 Tamanho do banco: {info.get('db_size', 0) / 1024:.1f} KB")
        st.info("💾 Backup do banco anterior salvo em /backups/")
        del st.session_state.import_success
        return  # IMPORTANTE: Sair da função após mostrar sucesso
    
    st.error("🚨 **ATENÇÃO: SUBSTITUIÇÃO COMPLETA DO BANCO**")
    st.warning("⚠️ Esta operação irá **DELETAR E SUBSTITUIR TODO O BANCO DE DADOS**. Um backup automático será criado antes da substituição.")
    
    uploaded_file = st.file_uploader(
        "Faça upload de um arquivo .db (SQLite) ou .sql (dump MySQL/SQLite)",
        type=["db", "sqlite", "sql"],
        key="upload_whole_db",
        help="Arquivos .db: banco SQLite completo | Arquivos .sql: dump SQL (converte MySQL→SQLite automaticamente)"
    )
    
    if uploaded_file is not None:
        
        import tempfile
        import re
        
        # Detectar tipo de arquivo
        file_extension = uploaded_file.name.split('.')[-1].lower()
        
        if file_extension == 'sql':
            # ===== IMPORTAÇÃO DE ARQUIVO SQL =====
            st.info("📄 Detectado arquivo SQL - Iniciando conversão e importação...")
            
            # Salvar arquivo SQL temporário
            temp_dir = tempfile.mkdtemp()
            temp_sql = Path(temp_dir) / "uploaded.sql"
            temp_new_db = Path(temp_dir) / "new_database.db"
            
            with open(temp_sql, "wb") as f:
                f.write(uploaded_file.getbuffer())
            
            try:
                # Ler e converter SQL
                with open(temp_sql, 'r', encoding='utf-8') as f:
                    sql_content = f.read()
                
                # Converter sintaxe MySQL para SQLite
                st.info("🔄 Convertendo sintaxe MySQL → SQLite...")
                
                # Remover AUTO_INCREMENT mas marcar onde estava
                sql_content = re.sub(r'`(\w+)`\s+integer\s+AUTO_INCREMENT', r'"\1" INTEGER PRIMARY KEY AUTOINCREMENT', sql_content, flags=re.IGNORECASE)
                sql_content = re.sub(r'(\w+)\s+integer\s+AUTO_INCREMENT', r'\1 INTEGER PRIMARY KEY AUTOINCREMENT', sql_content, flags=re.IGNORECASE)
                
                # Remover PRIMARY KEY separado se já está na coluna
                sql_content = re.sub(r',?\s*PRIMARY KEY\s*\([^)]+\)\s*', '', sql_content, flags=re.IGNORECASE)
                
                # Substituir backticks por aspas duplas
                sql_content = sql_content.replace('`', '"')
                
                # Remover cláusulas MySQL-specific
                sql_content = re.sub(r'\s*ENGINE\s*=\s*\w+', '', sql_content, flags=re.IGNORECASE)
                sql_content = re.sub(r'\s*DEFAULT\s+CHARSET\s*=\s*\w+', '', sql_content, flags=re.IGNORECASE)
                sql_content = re.sub(r'\s*COLLATE\s*=\s*\w+', '', sql_content, flags=re.IGNORECASE)
                
                # Criar novo banco e importar
                st.info("📥 Importando dados para novo banco...")
                conn = sqlite3.connect(str(temp_new_db), timeout=300, isolation_level='DEFERRED')
                cursor = conn.cursor()
                cursor.execute("PRAGMA foreign_keys=OFF")
                cursor.execute("PRAGMA synchronous=FULL")  # CRITICAL: FULL garante persistência
                cursor.execute("PRAGMA journal_mode=WAL")  # WAL para performance mantendo segurança
                cursor.execute("PRAGMA cache_size=10000")  # Cache maior para performance
                cursor.execute("PRAGMA temp_store=MEMORY")  # Usar memória para temp
                
                # Separar em comandos
                statements = []
                current_statement = []
                for line in sql_content.split('\n'):
                    line = line.strip()
                    if not line or line.startswith('--'):
                        continue
                    current_statement.append(line)
                    if line.endswith(';'):
                        statements.append(' '.join(current_statement))
                        current_statement = []
                
                # Executar comandos em lotes
                successful = 0
                failed = 0
                total = len(statements)
                
                progress_bar = st.progress(0)
                status_text = st.empty()
                status_text.text(f"Iniciando importação de {total} comandos...")
                
                cursor.execute("BEGIN")
                last_update = 0
                
                for i, statement in enumerate(statements):
                    try:
                        cursor.execute(statement)
                        successful += 1
                        
                        # Commit a cada 100 comandos para não perder progresso
                        if i > 0 and i % 100 == 0:
                            conn.commit()
                            cursor.execute("BEGIN")
                            
                            # Atualizar UI apenas a cada 100 comandos (não mais frequente)
                            if i - last_update >= 100:
                                progress_bar.progress(min(i / total, 0.99))
                                status_text.text(f"Importando: {i}/{total} comandos ({int(i/total*100)}%)")
                                last_update = i
                            
                    except sqlite3.Error as e:
                        failed += 1
                        if failed <= 3:  # Mostrar apenas os 3 primeiros erros
                            st.warning(f"⚠️ Erro {failed}: {str(e)[:80]}")
                
                conn.commit()  # Commit final
                progress_bar.progress(1.0)
                status_text.text(f"Finalizando importação...")
                
                cursor.execute("PRAGMA foreign_keys=ON")
                
                # CRÍTICO: Consolidar WAL antes de fechar - força todos os dados para o arquivo principal
                cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                cursor.execute("VACUUM")  # Otimizar banco
                conn.commit()
                
                # Verificar dados importados
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
                tables_imported = cursor.fetchall()
                
                # Contar registros totais
                total_records = 0
                for table in tables_imported:
                    cursor.execute(f"SELECT COUNT(*) FROM \"{table[0]}\"")
                    total_records += cursor.fetchone()[0]
                
                conn.close()
                
                # Limpar elementos de progresso antes das mensagens finais
                progress_bar.empty()
                status_text.empty()
                
                # CRÍTICO: Fechar TODAS as conexões ANTES de substituir arquivo
                from db.connection_pool import close_pool
                try:
                    close_pool()  # Fecha pool e libera locks
                except Exception as e:
                    pass  # Ignorar se pool não existir
                
                # Pequena pausa para garantir que locks foram liberados
                import time
                time.sleep(0.3)
                
                # Criar backup do banco atual usando sqlite3.backup (garante WAL)
                if DB_PATH.exists():
                    backup_path = Path("backups")
                    backup_path.mkdir(exist_ok=True)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    backup_file = backup_path / f"backup_antes_sql_import_{timestamp}.db"
                    src = sqlite3.connect(str(DB_PATH), timeout=30)
                    bkp = sqlite3.connect(str(backup_file), timeout=30)
                    try:
                        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        src.backup(bkp)
                    finally:
                        src.close()
                        bkp.close()
                
                # Remover arquivo destino antigo e seus WAL/SHM
                if DB_PATH.exists():
                    DB_PATH.unlink()
                wal_file = Path(str(DB_PATH) + "-wal")
                shm_file = Path(str(DB_PATH) + "-shm")
                if wal_file.exists():
                    wal_file.unlink()
                if shm_file.exists():
                    shm_file.unlink()
                
                # Usar backup API do sqlite3 (mais confiável que shutil.copy2)
                source_conn = sqlite3.connect(str(temp_new_db), timeout=30)
                dest_conn = sqlite3.connect(str(DB_PATH), timeout=30)
                source_conn.backup(dest_conn)
                source_conn.close()
                
                # Consolidar WAL no destino e fechar
                dest_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                dest_conn.execute("VACUUM")
                dest_conn.close()
                
                # Verificar que o arquivo foi copiado corretamente
                if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
                    raise Exception(f"Erro ao salvar banco: arquivo vazio ou não existe! Path: {DB_PATH}")
                
                # Remover WAL/SHM que podem ter sido criados
                if wal_file.exists():
                    wal_file.unlink()
                if shm_file.exists():
                    shm_file.unlink()
                
                shutil.rmtree(temp_dir)
                
                # Limpar cache e criar mensagem de sucesso
                st.cache_data.clear()
                st.session_state['import_success'] = {
                    'tables': len(tables_imported),
                    'records': total_records,
                    'commands': successful,
                    'errors': failed,
                    'db_size': DB_PATH.stat().st_size
                }
                st.rerun()
                
            except Exception as e:
                st.error(f"❌ Erro ao importar SQL: {e}")
                import traceback
                st.code(traceback.format_exc())
                try:
                    shutil.rmtree(temp_dir)
                except:
                    pass
                return
        
        else:
            # ===== IMPORTAÇÃO DE ARQUIVO .DB =====
            import tempfile
        
            # Salvar arquivo temporário
            temp_dir = tempfile.mkdtemp()
            temp_uploaded = Path(temp_dir) / "uploaded.db"
            temp_clean = Path(temp_dir) / "clean.db"
        
            with open(temp_uploaded, "wb") as f:
                f.write(uploaded_file.getbuffer())
        
            try:
                st.info("🔄 Validando e limpando banco de dados...")
                
                # Verificar integridade e consolidar WAL usando API Python nativa
                source_conn = sqlite3.connect(str(temp_uploaded), timeout=30)
                
                try:
                    result = source_conn.execute("PRAGMA integrity_check").fetchone()
                    if result[0] != "ok":
                        st.error(f"❌ Arquivo de backup está corrompido: {result[0]}")
                        source_conn.close()
                        shutil.rmtree(temp_dir)
                        return
                    
                    # Consolidar WAL
                    source_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    source_conn.execute("VACUUM")
                    
                    # Criar versão limpa usando backup API
                    dest_conn = sqlite3.connect(str(temp_clean), timeout=30)
                    source_conn.backup(dest_conn)
                    source_conn.close()
                    
                    # Otimizar destino
                    dest_conn.execute("VACUUM")
                    dest_conn.close()
                    
                except sqlite3.DatabaseError as db_error:
                    st.error(f"❌ Erro no banco de dados: {db_error}")
                    try:
                        source_conn.close()
                    except:
                        pass
                    shutil.rmtree(temp_dir)
                    return
                
                # CRÍTICO: Fechar TODAS as conexões ANTES de substituir arquivo
                from db.connection_pool import close_pool
                try:
                    close_pool()  # Fecha pool e libera locks
                except Exception as e:
                    st.warning(f"⚠️ Aviso ao fechar pool: {e}")
                
                # Pequena pausa para garantir que locks foram liberados
                import time
                time.sleep(0.5)
                
                # Criar backup antes de sobrescrever usando sqlite3.backup (garante WAL)
                if DB_PATH.exists():
                    backup_path = Path("backups")
                    backup_path.mkdir(exist_ok=True)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    backup_file = backup_path / f"backup_antes_restauracao_{timestamp}.db"
                    src = sqlite3.connect(str(DB_PATH), timeout=30)
                    bkp = sqlite3.connect(str(backup_file), timeout=30)
                    try:
                        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        src.backup(bkp)
                    finally:
                        src.close()
                        bkp.close()
                
                # Sobrescrever banco com versão limpa usando sqlite3.backup (garante WAL)
                st.info(f"📋 Debug: Copiando {temp_clean} → {DB_PATH}")
                st.info(f"📋 Tamanho fonte: {temp_clean.stat().st_size} bytes")
                
                # Usar API de backup do SQLite para garantir integridade
                src_conn = sqlite3.connect(str(temp_clean), timeout=30)
                dst_conn = sqlite3.connect(str(DB_PATH), timeout=30)
                try:
                    src_conn.backup(dst_conn)
                    dst_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    dst_conn.execute("VACUUM")
                finally:
                    src_conn.close()
                    dst_conn.close()
                
                st.info(f"📋 Tamanho destino: {DB_PATH.stat().st_size} bytes")
                
                # Verificar que o arquivo foi copiado corretamente
                if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
                    raise Exception(f"Erro ao salvar banco: arquivo vazio ou não existe! Path: {DB_PATH}")
                
                # IMPORTANTE: Remover arquivos WAL/SHM antigos que podem causar problemas
                wal_file = Path(str(DB_PATH) + "-wal")
                shm_file = Path(str(DB_PATH) + "-shm")
                if wal_file.exists():
                    wal_file.unlink()
                if shm_file.exists():
                    shm_file.unlink()
                
                # Limpar temporários
                shutil.rmtree(temp_dir)
                
                # Debug: verificar arquivo DIRETAMENTE (sem pool/cache)
                try:
                    direct_conn = sqlite3.connect(str(DB_PATH))
                    cursor = direct_conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM usuarios")
                    usuarios_direct = cursor.fetchone()[0]
                    cursor.execute("SELECT COUNT(*) FROM apostas")
                    apostas_direct = cursor.fetchone()[0]
                    direct_conn.close()
                    st.info(f"🔍 Arquivo direto: {usuarios_direct} usuários, {apostas_direct} apostas")
                except Exception as e:
                    st.error(f"❌ Erro ao ler arquivo direto: {e}")

                # Debug: verificar dados PELO POOL (pool será recriado lazily)
                try:
                    with db_connect() as conn:
                        cursor = conn.cursor()
                        cursor.execute("SELECT COUNT(*) FROM usuarios")
                        usuarios_pool = cursor.fetchone()[0]
                        cursor.execute("SELECT COUNT(*) FROM apostas")
                        apostas_pool = cursor.fetchone()[0]
                        st.info(f"🔍 Pelo pool: {usuarios_pool} usuários, {apostas_pool} apostas")
                except Exception as e:
                    st.warning(f"⚠️ Erro ao verificar pelo pool: {e}")

                # Limpar cache e criar mensagem de sucesso
                st.cache_data.clear()
                st.session_state['import_success'] = {
                    'tables': 0,
                    'records': 0,
                    'commands': 0,
                    'errors': 0,
                    'type': 'db',
                    'db_size': DB_PATH.stat().st_size
                }
                st.rerun()
                
            except Exception as e:
                st.error(f"❌ Erro inesperado: {e}")
                import traceback
                st.code(traceback.format_exc())
                try:
                    shutil.rmtree(temp_dir)
                except:
                    pass

def listar_tabelas():
    """Retorna o nome de todas as tabelas do banco de dados."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            query = "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
            tabelas = pd.read_sql(query, conn)["name"].tolist()
        return tabelas
    except Exception as e:
        st.error(f"❌ Erro ao listar tabelas: {e}")
        return []

def exportar_tabela_excel(tabela):
    """Exporta os dados da tabela como arquivo Excel em buffer de memória."""
    with db_connect() as conn:
        df = pd.read_sql(f"SELECT * FROM {tabela}", conn)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name=tabela)
    output.seek(0)
    return output

def download_tabela():
    """Interface para download de tabela específica."""
    _require_master_access()
    tabelas = listar_tabelas()
    
    if not tabelas:
        st.warning("⚠️ Nenhuma tabela encontrada no banco de dados.")
        return
    
    tabela = st.selectbox("Selecione a tabela para exportar", tabelas, key="select_export")
    
    if st.button("📊 Exportar para Excel", width="stretch", type="primary"):
        try:
            excel_buffer = exportar_tabela_excel(tabela)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            st.download_button(
                label=f"⬇️ Baixar tabela {tabela} (.xlsx)",
                data=excel_buffer,
                file_name=f"{tabela}_{timestamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch"
            )
            st.success(f"✅ Tabela '{tabela}' exportada com sucesso!")
        except Exception as e:
            st.error(f"❌ Erro ao exportar tabela: {e}")

def upload_tabela():
    """Interface para upload/importação de tabela específica."""
    _require_master_access()
    tabelas = listar_tabelas()
    
    if not tabelas:
        st.warning("⚠️ Nenhuma tabela encontrada no banco de dados.")
        return
    
    tabela = st.selectbox("Escolha a tabela para sobrescrever:", tabelas, key="select_import")
    
    st.error("🚨 **ATENÇÃO: OPERAÇÃO DESTRUTIVA**")
    st.warning("⚠️ Esta operação irá **DELETAR PERMANENTEMENTE TODOS OS DADOS** da tabela selecionada e substituí-los pelo conteúdo do arquivo Excel. Esta ação não pode ser desfeita!")
    
    uploaded_file = st.file_uploader(
        f"Upload do arquivo .xlsx para substituir dados da tabela '{tabela}'",
        type=["xlsx"], 
        key="upload_one_table"
    )
    
    if uploaded_file is not None and tabela:
        try:
            df = pd.read_excel(uploaded_file)
        except Exception as e:
            st.error(f"❌ Não foi possível ler o Excel: {e}")
            return

        st.write(f"👀 Prévia dos dados ({len(df)} linhas):")
        st.dataframe(df.head(10))

        if st.button("✅ Confirmar Importação - SOBRESCREVER TODOS OS DADOS", type="primary", width="stretch"):
            try:
                # Fechar todas as conexões do pool antes de operação crítica
                from db.connection_pool import get_pool
                try:
                    pool = get_pool()
                    pool.close_all()
                except:
                    pass
                
                # Conectar diretamente ao banco para garantir commit explícito
                conn = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)  # autocommit desligado
                cursor = conn.cursor()
                
                cursor.execute("PRAGMA foreign_keys=OFF")  # evita falhas ao limpar e regravar
                
                # Garantir alinhamento de colunas antes de importar
                cols_info = cursor.execute(f"PRAGMA table_info('{tabela}')").fetchall()
                if not cols_info:
                    conn.close()
                    raise ValueError(f"Tabela '{tabela}' não encontrada no banco.")
                db_cols = [r[1] for r in cols_info]
                db_col_types = {r[1]: (r[2] or '').upper() for r in cols_info}

                missing_cols = [c for c in db_cols if c not in df.columns]
                extra_cols = [c for c in df.columns if c not in db_cols]
                if missing_cols:
                    conn.close()
                    raise ValueError(f"Colunas faltantes no Excel: {missing_cols}")
                if extra_cols:
                    st.info(f"ℹ️ Colunas extras no Excel serão ignoradas: {extra_cols}")
                df_alinhado = df[db_cols].copy()

                # Coerce types to avoid SQLite datatype mismatch
                for col_name in db_cols:
                    col_type = db_col_types.get(col_name, '')
                    series = df_alinhado[col_name]

                    if "INT" in col_type or "BOOL" in col_type:
                        coerced = pd.to_numeric(series, errors='coerce').astype('Int64')
                        df_alinhado[col_name] = coerced.where(~coerced.isna(), None)
                        continue
                    if any(t in col_type for t in ["REAL", "FLOA", "DOUB", "NUM", "DEC"]):
                        coerced = pd.to_numeric(series, errors='coerce')
                        df_alinhado[col_name] = coerced.where(~coerced.isna(), None)
                        continue
                    if any(t in col_type for t in ["DATE", "TIME"]):
                        # Handle mixed Excel datetime values safely (strings, numbers, native datetimes).
                        df_alinhado[col_name] = series.apply(_parse_backup_datetime_value)
                        continue

                df_alinhado = df_alinhado.where(pd.notnull(df_alinhado), None)

                # Contar registros atuais antes de deletar
                count_before = cursor.execute(f'SELECT COUNT(*) FROM "{tabela}"').fetchone()[0]

                # DELETAR TODOS OS DADOS DA TABELA
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(f'DELETE FROM "{tabela}"')
                conn.commit()  # Commit do DELETE
                
                count_after_delete = cursor.execute(f'SELECT COUNT(*) FROM "{tabela}"').fetchone()[0]
                
                # Inserir novos dados
                cursor.execute("BEGIN IMMEDIATE")
                df_alinhado.to_sql(
                    tabela,
                    conn,
                    if_exists='append',
                    index=False,
                    method='multi',
                    chunksize=500
                )
                conn.commit()  # Commit do INSERT
                
                count_after_insert = cursor.execute(f'SELECT COUNT(*) FROM "{tabela}"').fetchone()[0]
                
                cursor.execute("PRAGMA foreign_keys=ON")
                conn.commit()  # Commit final
                conn.close()
                
                # CRÍTICO: Fechar pool para forçar recriação após modificar dados
                from db.connection_pool import close_pool
                try:
                    close_pool()  # Fecha pool atual, será recriado automaticamente na próxima consulta
                except Exception as e:
                    st.error(f"❌ Erro ao fechar pool de conexões: {e}")
                    raise

                st.success(f"✅ Tabela '{tabela}' completamente sobrescrita!")
                st.info(f"🗑️ Registros deletados: {count_before}")
                st.info(f"📥 Registros importados: {count_after_insert}")
                st.cache_data.clear()
                st.rerun()
            except Exception as e:
                try:
                    conn.rollback()
                    conn.close()
                except:
                    pass
                st.error(f"❌ Erro ao importar tabela: {e}")
                st.info("💡 Verifique se as colunas do arquivo Excel correspondem exatamente às colunas da tabela.")
                import traceback
                st.code(traceback.format_exc())

def main():
    st.title("💾 Backup e Restauração do Banco de Dados")
    st.markdown("""
    - **Download Completo:** Baixe uma cópia limpa do banco inteiro (.db).
    - **Upload Completo:** Substitua todo o banco por arquivo .db (SQLite) ou .sql (dump MySQL/PostgreSQL).
    - **Exportar tabela:** Exporte uma tabela específica (.xlsx).
    - **Importar tabela:** Importe dados para uma tabela específica (sobrescreve).
    
    💡 **Novidade:** Agora aceita arquivos .sql! O sistema converte automaticamente sintaxe MySQL→SQLite.
    """)
    
    # Mostrar info do banco
    st.info(f"📍 Banco de dados: `{DB_PATH.name}` | Status: {'Existe' if DB_PATH.exists() else 'Não encontrado'}")
    
    st.header("Backup/Restauração do arquivo completo")
    col1, col2 = st.columns(2)
    with col1:
        download_db()
    with col2:
        upload_db()
    st.divider()
    st.header("Backup/Restauração de tabelas específicas")
    tab1, tab2 = st.tabs(["Exportar Tabela", "Importar Tabela"])
    with tab1:
        download_tabela()
    with tab2:
        upload_tabela()

    st.divider()
    st.header("Temporadas")
    st.write("Gerencie as temporadas visíveis no sistema. A criação de uma nova temporada fará com que ela possa aparecer em seletores que leem a tabela `temporadas`.")
    col_a, col_b = st.columns([2, 8])
    with col_a:
        if st.button("➕ Criar próxima temporada"):
            new_year = create_next_temporada()
            st.success(f"✅ Temporada {new_year} criada/registrada com sucesso.")
            st.rerun()
    with col_b:
        existing = list_temporadas()
        if existing:
            st.write("Temporadas cadastradas:")
            st.write(", ".join(existing))
        else:
            st.info("Nenhuma temporada cadastrada. Botão acima cria a próxima temporada.")

if __name__ == "__main__":
    main()

# ============ FUNÇÕES DE BACKUP E RESTAURAÇÃO ============

def backup_banco(backup_dir: str = "backups") -> str:
    """
    Cria um backup do banco de dados usando a API de backup do SQLite.
    Isso garante que todos os dados do WAL sejam incluídos no backup.
    
    Args:
        backup_dir: Diretório para armazenar backups
    
    Returns:
        Caminho do arquivo de backup criado
    """
    backup_path = Path(backup_dir)
    backup_path.mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_path / f"backup_{timestamp}.db"
    
    # Usar API de backup do SQLite para garantir integridade (inclui WAL)
    source_conn = sqlite3.connect(str(DB_PATH), timeout=30)
    dest_conn = sqlite3.connect(str(backup_file), timeout=30)
    
    try:
        # Consolidar WAL antes do backup
        source_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        source_conn.backup(dest_conn)
        dest_conn.execute("VACUUM")
    finally:
        source_conn.close()
        dest_conn.close()
    
    return str(backup_file)

def restaurar_backup(backup_file: str) -> bool:
    """
    Restaura o banco de dados a partir de um backup usando a API de backup do SQLite.
    Isso garante que todos os dados do WAL sejam tratados corretamente.
    
    Args:
        backup_file: Caminho do arquivo de backup
    
    Returns:
        True se restaurado com sucesso, False caso contrário
    """
    try:
        if not Path(backup_file).exists():
            return False
        
        # Fechar pool para evitar locks
        from db.connection_pool import close_pool
        try:
            close_pool()
        except:
            pass
        
        # Remover arquivos WAL/SHM antigos que podem causar problemas
        wal_file = Path(str(DB_PATH) + "-wal")
        shm_file = Path(str(DB_PATH) + "-shm")
        if wal_file.exists():
            wal_file.unlink()
        if shm_file.exists():
            shm_file.unlink()
        
        # Usar API de backup do SQLite para garantir integridade
        source_conn = sqlite3.connect(str(backup_file), timeout=30)
        dest_conn = sqlite3.connect(str(DB_PATH), timeout=30)
        
        try:
            source_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            source_conn.backup(dest_conn)
            dest_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dest_conn.execute("VACUUM")
        finally:
            source_conn.close()
            dest_conn.close()
        
        # Remover arquivos WAL/SHM que podem ter sido criados
        if wal_file.exists():
            wal_file.unlink()
        if shm_file.exists():
            shm_file.unlink()
        
        return True
    except Exception as e:
        print(f"Erro ao restaurar backup: {e}")
        return False


# ============ FUNÇÕES DE TEMPORADAS ============
def ensure_temporadas_table() -> None:
    """Garante que a tabela `temporadas` exista no banco de dados."""
    try:
        with db_connect() as conn:
            c = conn.cursor()
            c.execute('''
                CREATE TABLE IF NOT EXISTS temporadas (
                    temporada TEXT PRIMARY KEY,
                    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
    except Exception as e:
        st.error(f"❌ Erro ao garantir tabela 'temporadas': {e}")


def create_next_temporada() -> str:
    """Cria (se ainda não existir) a temporada do próximo ano (ano atual + 1).

    Retorna a string do ano criado (ex: '2026').
    """
    from datetime import datetime as _dt
    next_year = _dt.now().year + 1
    ensure_temporadas_table()
    try:
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO temporadas (temporada) VALUES (?)", (str(next_year),))
            conn.commit()
    except Exception as e:
        st.error(f"❌ Erro ao criar temporada {next_year}: {e}")
    return str(next_year)


def list_temporadas() -> list:
    """Retorna lista de temporadas cadastradas (strings)."""
    try:
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='temporadas'")
            if not c.fetchone():
                return []
            c.execute("SELECT temporada FROM temporadas ORDER BY temporada ASC")
            return [str(r[0]) for r in c.fetchall()]
    except Exception as e:
        st.error(f"❌ Erro ao listar temporadas: {e}")
        return []
