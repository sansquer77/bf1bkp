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


def resolve_db_path():
	try:
		from importlib import import_module
		db_config = import_module("db.db_config")
		config_db_path = getattr(db_config, "DB_PATH", None)
		if config_db_path:
			return str(config_db_path)
	except Exception:
		return get_env("DB_PATH", default=get_env("DATABASE_PATH"), required=True)


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


def send_email(zip_path, zip_name, subject, body, smtp_settings):
	msg = EmailMessage()
	msg["Subject"] = subject
	msg["From"] = smtp_settings["from_addr"]
	msg["To"] = smtp_settings["to_addr"]
	msg.set_content(body)

	with open(zip_path, "rb") as f:
		msg.add_attachment(
			f.read(),
			maintype="application",
			subtype="zip",
			filename=zip_name,
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
		db_path = resolve_db_path()
		tz_name = get_env_str("TIMEZONE", default="America/Sao_Paulo")

		# Reuse the same env vars as the app's email_service.py
		smtp_user = get_env("EMAIL_REMETENTE", default=get_env("SMTP_USER", default=""))
		smtp_password = get_env("SENHA_EMAIL", default=get_env("SMTP_PASSWORD", default=""))
		smtp_host = get_env("SMTP_HOST", default="smtp.gmail.com")
		smtp_port = int(get_env("SMTP_PORT", default="465"))

		from_addr = get_env("SMTP_FROM", default=smtp_user)
		alert_email = get_env("EMAIL_ADMIN", default=get_env("ALERT_EMAIL", default=""))
		to_addr = get_env("SMTP_TO", default=alert_email or from_addr)
		if not to_addr:
			raise ValueError("Missing recipient email. Set SMTP_TO or EMAIL_ADMIN.")

		use_ssl = get_env_str("SMTP_USE_SSL", default="true").lower() == "true"
		use_tls = get_env_str("SMTP_USE_TLS", default="false").lower() == "true"
		if use_ssl and use_tls:
			raise ValueError("SMTP_USE_SSL and SMTP_USE_TLS cannot both be true.")

		tz = ZoneInfo(tz_name)
		date_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
		subject = get_env("BACKUP_SUBJECT", default=f"Backup diario - {date_str}")
		body = get_env("BACKUP_BODY", default=f"Backup gerado em {date_str}.")

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
		send_email(final_path, final_name, subject, body, smtp_settings)
		print("Backup enviado com sucesso.")
		return 0
	except Exception as exc:
		print(f"Erro no backup: {exc}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
