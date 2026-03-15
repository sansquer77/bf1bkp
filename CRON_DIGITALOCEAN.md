# Backup BF1 via CRON na DigitalOcean App Platform

Este repositório já possui o script `bkp.py` pronto para rodar como job agendado.

## Comando do job

```bash
python bkp.py
```

## Agenda (a cada hora)

Use a expressão CRON:

```cron
0 * * * *
```

## Variáveis obrigatórias

- `DATABASE_PATH` ou `DB_PATH`: caminho do SQLite usado pela aplicação BF1.
- `BACKUP_TO_EMAIL`: e-mail específico que receberá o backup.
- `EMAIL_REMETENTE` (ou `SMTP_USER`): usuário SMTP.
- `SENHA_EMAIL` (ou `SMTP_PASSWORD`): senha SMTP.

## Variáveis recomendadas

- `TIMEZONE=America/Sao_Paulo`
- `SMTP_HOST=smtp.gmail.com`
- `SMTP_PORT=465`
- `SMTP_USE_SSL=true`
- `SMTP_USE_TLS=false`
- `SMTP_FROM=<EMAIL_REMETENTE>`
- `MAX_ATTACHMENT_MB=24`
- `BACKUP_SUBJECT=Backup horario - {{timestamp}}`
- `BACKUP_BODY=Backup gerado automaticamente.`

## Variáveis opcionais de segurança

- `BACKUP_ENCRYPTION_KEY`: chave para criptografar o `.zip` com OpenSSL (gera `.enc`).
- `BACKUP_REQUIRE_ENCRYPTION=true`: falha o job se a chave não estiver configurada.

## Opcao 2: Trigger remoto (recomendado para App Platform)

Quando o job nao compartilha o mesmo filesystem do servico web, configure o job para apenas chamar um endpoint interno da aplicacao.

Variaveis do job:

- `BACKUP_TRIGGER_URL`: URL HTTPS do endpoint de backup da aplicacao.
	- Para endpoint HTTP tradicional: `https://seu-app.ondigitalocean.app/internal/backup/run`
	- Para fallback interno Streamlit: `https://seu-app.ondigitalocean.app/?internal_route=%2Finternal%2Fbackup%2Frun`
- `BACKUP_TRIGGER_TOKEN`: token secreto para autenticar a chamada.
- `BACKUP_TRIGGER_TOKEN_HEADER` (opcional): header do token. Padrao `Authorization`.
- `BACKUP_TRIGGER_TIMEOUT_SECONDS` (opcional): timeout da chamada HTTP. Padrao `30`.
- `BACKUP_TRIGGER_HTTP_MODE` (opcional): `auto` (padrao), `post` ou `get`.
- `BACKUP_TRIGGER_INCLUDE_QUERY_TOKEN` (opcional): envia `token` na query string. Padrao `false`.

Com `BACKUP_TRIGGER_URL` definido, o `bkp.py` entra em modo trigger remoto e nao tenta abrir SQLite local.

Para Streamlit com rota interna por query, use `BACKUP_TRIGGER_HTTP_MODE=get` para compatibilidade maxima.

## Comportamento do script

- Se `BACKUP_TRIGGER_URL` estiver definido: chama endpoint remoto e encerra.
- Faz checkpoint WAL e backup consistente do SQLite.
- Compacta em `.zip`.
- Opcionalmente criptografa com OpenSSL (`aes-256-cbc`, `pbkdf2`).
- Envia anexo por e-mail para `BACKUP_TO_EMAIL`.
- Retorna código `0` em sucesso e `1` em falha (ideal para monitoramento do Job).

## Observações para App Platform

- O job deve ter acesso ao mesmo volume/caminho do banco de dados da aplicação BF1.
- Se o arquivo de banco não existir no caminho configurado, o job falhará com erro explícito.
- Se usar Gmail, utilize senha de app (não a senha principal da conta).
