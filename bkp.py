import os
import sys
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo
import smtplib
from email.message import EmailMessage
from pathlib import Path
import shutil
import subprocess
import json
import urllib.error
import urllib.parse
import urllib.request


def get_env(name, default=None, required=False):
	value = os.getenv(name, default)
	if required and not value:
		raise ValueError(f"Missing required env var: {name}")
	return value


def get_env_str(name, default="", required=False):
	value = get_env(name, default=default, required=required)
	if value is None:
		return ""
	return str(value)


def get_env_bool(name, default=False):
	raw = get_env(name, default=str(default).lower())
	if raw is None:
		return bool(default)
	return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _append_query_token(url, token):
	if not token:
		return url
	parsed = urllib.parse.urlsplit(url)
	query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
	if "token" not in query:
		query["token"] = token
	new_query = urllib.parse.urlencode(query)
	return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))


def _perform_trigger_request(url, method, token, timeout_seconds, include_query_token):
	request_url = _append_query_token(url, token) if include_query_token else url
	payload = {
		"source": "do-app-platform-cron",
		"triggered_at": datetime.utcnow().isoformat() + "Z",
	}
	request_data = None
	if method == "POST":
		request_data = json.dumps(payload).encode("utf-8")

	request = urllib.request.Request(
		request_url,
		data=request_data,
		method=method,
	)
	if method == "POST":
		request.add_header("Content-Type", "application/json")

	header_name = get_env_str("BACKUP_TRIGGER_TOKEN_HEADER", default="Authorization")
	if token:
		header_value = token
		if header_name.lower() == "authorization" and not token.lower().startswith("bearer "):
			header_value = f"Bearer {token}"
		request.add_header(header_name, header_value)

	with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
		response_body = response.read().decode("utf-8", errors="replace")
		if response.status < 200 or response.status >= 300:
			raise RuntimeError(f"HTTP {response.status}: {response_body[:400]}")
		print(f"Trigger remoto executado com sucesso via {method} (HTTP {response.status}).")
		if response_body.strip():
			print(f"Resposta: {response_body[:400]}")


def trigger_remote_backup(url, token="", timeout_seconds=30.0):
	http_mode = get_env_str("BACKUP_TRIGGER_HTTP_MODE", default="auto").strip().lower()
	if http_mode not in {"auto", "post", "get"}:
		raise ValueError("BACKUP_TRIGGER_HTTP_MODE invalido. Use auto, post ou get.")

	include_query_token = get_env_bool("BACKUP_TRIGGER_INCLUDE_QUERY_TOKEN", default=False)

	try:
		if http_mode == "get":
			_perform_trigger_request(url, "GET", token, timeout_seconds, include_query_token)
			return

		if http_mode == "post":
			_perform_trigger_request(url, "POST", token, timeout_seconds, include_query_token)
			return

		# auto: tenta POST e faz fallback para GET quando endpoint Streamlit rejeita verbo.
		try:
			_perform_trigger_request(url, "POST", token, timeout_seconds, include_query_token)
			return
		except urllib.error.HTTPError as post_err:
			if post_err.code not in {404, 405}:
				raise
			print(
				f"Aviso: trigger POST retornou HTTP {post_err.code}. Tentando GET como fallback.",
				file=sys.stderr,
			)
			_perform_trigger_request(url, "GET", token, timeout_seconds, include_query_token)
	except urllib.error.HTTPError as err:
		body = ""
		try:
			body = err.read().decode("utf-8", errors="replace")
		except Exception:
			pass
		raise RuntimeError(f"Falha ao chamar endpoint remoto: HTTP {err.code}. {body[:400]}") from err
	except urllib.error.URLError as err:
		raise RuntimeError(f"Falha de rede ao chamar endpoint remoto: {err.reason}") from err


def resolve_db_path():
	# Aceita estrutura do app BF1 tanto em `db/db_config.py` quanto em `db_config.py`.
	try:
		from importlib import import_module
		db_config = import_module("db.db_config")
		config_db_path = getattr(db_config, "DB_PATH", None)
		if config_db_path:
			return str(config_db_path)
	except Exception:
		pass

	try:
		from importlib import import_module
		db_config = import_module("db_config")
		config_db_path = getattr(db_config, "DB_PATH", None)
		if config_db_path:
			return str(config_db_path)
	except Exception:
		pass

	configured_path = get_env("DB_PATH", default=get_env("DATABASE_PATH"))
	if configured_path:
		return str(configured_path)

	# Fallback local padrão para facilitar execução em jobs simples.
	return str(Path("bolao_f1.db").resolve())


def build_backup(db_path, tz_name, temp_dir):
	if not os.path.exists(db_path):
		raise FileNotFoundError(f"Database file not found: {db_path}")

	tz = ZoneInfo(tz_name)
	timestamp = datetime.now(tz).strftime("%Y%m%d_%H%M%S")
	base_name = f"backup_{timestamp}.sqlite"

	backup_path = os.path.join(temp_dir, base_name)

	with sqlite3.connect(db_path, timeout=30) as src_conn:
		src_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
		with sqlite3.connect(backup_path, timeout=30) as dst_conn:
			src_conn.backup(dst_conn)
			if get_env_str("BACKUP_VACUUM", default="true").lower() == "true":
				dst_conn.execute("VACUUM")

	# Cleanup any WAL/SHM that might be created for the temp backup
	for suffix in ("-wal", "-shm"):
		try:
			Path(f"{backup_path}{suffix}").unlink()
		except FileNotFoundError:
			pass

	zip_name = f"backup_{timestamp}.zip"
	zip_path = os.path.join(temp_dir, zip_name)
	with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
		zf.write(backup_path, arcname=base_name)

	return zip_path, zip_name


def encrypt_with_openssl(input_path, output_path, password):
	openssl = shutil.which("openssl")
	if not openssl:
		raise RuntimeError("OpenSSL not found. Install openssl or disable encryption.")
	cmd = [
		openssl,
		"enc",
		"-aes-256-cbc",
		"-salt",
		"-pbkdf2",
		"-in",
		input_path,
		"-out",
		output_path,
		"-pass",
		f"pass:{password}",
	]
	result = subprocess.run(cmd, capture_output=True, text=True)
	if result.returncode != 0:
		raise RuntimeError(result.stderr.strip() or "OpenSSL encryption failed")


def enforce_size_limit(file_path, max_mb):
	max_bytes = int(max_mb * 1024 * 1024)
	file_size = os.path.getsize(file_path)
	if file_size > max_bytes:
		raise ValueError(
			f"Backup file too large: {file_size / (1024 * 1024):.2f} MB. "
			f"Limit is {max_mb} MB."
		)


def send_email(file_path, file_name, subject, body, smtp_settings):
	msg = EmailMessage()
	msg["Subject"] = subject
	msg["From"] = smtp_settings["from_addr"]
	msg["To"] = smtp_settings["to_addr"]
	msg.set_content(body)

	with open(file_path, "rb") as f:
		subtype = "zip" if file_name.lower().endswith(".zip") else "octet-stream"
		msg.add_attachment(
			f.read(),
			maintype="application",
			subtype=subtype,
			filename=file_name,
		)

	if smtp_settings["use_ssl"]:
		server = smtplib.SMTP_SSL(smtp_settings["host"], smtp_settings["port"])
	else:
		server = smtplib.SMTP(smtp_settings["host"], smtp_settings["port"])

	with server:
		if smtp_settings["use_tls"]:
			server.starttls()
		if smtp_settings["user"]:
			server.login(smtp_settings["user"], smtp_settings["password"])
		server.send_message(msg)


def main():
	try:
		trigger_url = get_env_str("BACKUP_TRIGGER_URL", default="").strip()
		if trigger_url:
			trigger_token = get_env_str("BACKUP_TRIGGER_TOKEN", default="")
			timeout_seconds = float(get_env("BACKUP_TRIGGER_TIMEOUT_SECONDS", default="30"))
			trigger_remote_backup(
				url=trigger_url,
				token=trigger_token,
				timeout_seconds=timeout_seconds,
			)
			return 0

		db_path = resolve_db_path()
		tz_name = get_env_str("TIMEZONE", default="America/Sao_Paulo")

		# Reuse the same env vars as the app's email_service.py
		smtp_user = get_env("EMAIL_REMETENTE", default=get_env("SMTP_USER", default=""))
		smtp_password = get_env("SENHA_EMAIL", default=get_env("SMTP_PASSWORD", default=""))
		smtp_host = get_env("SMTP_HOST", default="smtp.gmail.com")
		smtp_port = int(get_env("SMTP_PORT", default="465"))

		from_addr = get_env("SMTP_FROM", default=smtp_user)
		alert_email = get_env("EMAIL_ADMIN", default=get_env("ALERT_EMAIL", default=""))
		to_addr = get_env("BACKUP_TO_EMAIL", default=get_env("SMTP_TO", default=alert_email or from_addr))
		if not to_addr:
			raise ValueError("Missing recipient email. Set BACKUP_TO_EMAIL, SMTP_TO or EMAIL_ADMIN.")

		require_auth = get_env_bool("SMTP_REQUIRE_AUTH", default=True)
		if require_auth and (not smtp_user or not smtp_password):
			raise ValueError("SMTP credentials missing. Set EMAIL_REMETENTE/SMTP_USER and SENHA_EMAIL/SMTP_PASSWORD.")

		use_ssl = get_env_bool("SMTP_USE_SSL", default=True)
		use_tls = get_env_bool("SMTP_USE_TLS", default=False)
		if use_ssl and use_tls:
			raise ValueError("SMTP_USE_SSL and SMTP_USE_TLS cannot both be true.")

		tz = ZoneInfo(tz_name)
		date_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
		subject = get_env("BACKUP_SUBJECT", default=f"Backup horario - {date_str}")
		body = get_env("BACKUP_BODY", default=f"Backup gerado em {date_str}.")

		smtp_settings = {
			"host": smtp_host,
			"port": smtp_port,
			"user": smtp_user,
			"password": smtp_password,
			"from_addr": from_addr,
			"to_addr": to_addr,
			"use_ssl": use_ssl,
			"use_tls": use_tls,
		}

		with tempfile.TemporaryDirectory() as temp_dir:
			backup_zip_path, backup_zip_name = build_backup(db_path, tz_name, temp_dir)
			final_path = backup_zip_path
			final_name = backup_zip_name

			encryption_key = get_env("BACKUP_ENCRYPTION_KEY", default="")
			require_encryption = get_env_str("BACKUP_REQUIRE_ENCRYPTION", default="false").lower() == "true"
			if encryption_key:
				encrypted_name = f"{backup_zip_name}.enc"
				encrypted_path = f"{backup_zip_path}.enc"
				encrypt_with_openssl(backup_zip_path, encrypted_path, encryption_key)
				final_path = encrypted_path
				final_name = encrypted_name
			elif require_encryption:
				raise ValueError("BACKUP_REQUIRE_ENCRYPTION is true but no BACKUP_ENCRYPTION_KEY set.")
			else:
				print("Aviso: backup enviado sem criptografia.", file=sys.stderr)

			max_mb = float(get_env("MAX_ATTACHMENT_MB", default="24"))
			enforce_size_limit(final_path, max_mb)
			send_email(final_path, final_name, subject, body, smtp_settings)
		print("Backup enviado com sucesso.")
		return 0
	except Exception as exc:
		print(f"Erro no backup: {exc}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
