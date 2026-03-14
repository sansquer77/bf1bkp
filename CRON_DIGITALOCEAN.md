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

## Comportamento do script

- Faz checkpoint WAL e backup consistente do SQLite.
- Compacta em `.zip`.
- Opcionalmente criptografa com OpenSSL (`aes-256-cbc`, `pbkdf2`).
- Envia anexo por e-mail para `BACKUP_TO_EMAIL`.
- Retorna código `0` em sucesso e `1` em falha (ideal para monitoramento do Job).

## Observações para App Platform

- O job deve ter acesso ao mesmo volume/caminho do banco de dados da aplicação BF1.
- Se o arquivo de banco não existir no caminho configurado, o job falhará com erro explícito.
- Se usar Gmail, utilize senha de app (não a senha principal da conta).
