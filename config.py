"""Настройки из .env."""
import os

from dotenv import load_dotenv

load_dotenv()

IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")
# Один или несколько адресов через запятую — письма от них разбираются всегда
ALLOWED_SENDERS = [s.strip().lower() for s in os.getenv("ALLOWED_SENDER", "").split(",") if s.strip()]
# Принимать чеки от любых отправителей (письмо похоже на чек по теме или отправителю — ОФД)
ANY_SENDER = os.getenv("ANY_SENDER", "1").strip().lower() not in ("0", "false", "no", "нет")
SINCE_DAYS = int(os.getenv("SINCE_DAYS", "30"))
MAX_EMAILS_PER_CHECK = int(os.getenv("MAX_EMAILS_PER_CHECK", "50"))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")

AUTO_REFRESH_MINUTES = int(os.getenv("AUTO_REFRESH_MINUTES", "10"))
DB_PATH = os.getenv("DB_PATH", "receipts.db")
