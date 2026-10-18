"""Чтение почты по IMAP. Только чтение: ящик открывается в режиме readonly,
письма забираются через BODY.PEEK, поэтому флаг «прочитано» не ставится."""
import email
import hashlib
import imaplib
import logging
import re
from datetime import date, timedelta
from email import policy
from email.utils import parseaddr

import config

log = logging.getLogger(__name__)

HEADER_SCAN_LIMIT = 2000  # сколько последних писем за период просматривать по заголовкам
HEADER_CHUNK = 100  # по сколько писем запрашивать заголовки за раз

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _imap_date(d: date) -> str:
    # Формат IMAP: 18-Oct-2026, не зависит от локали
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def message_id_of(msg) -> str:
    mid = (msg.get("Message-ID") or "").strip()
    if mid:
        return mid
    # Письмо без Message-ID: берём хэш нескольких заголовков
    raw = f"{msg.get('Date')}|{msg.get('From')}|{msg.get('Subject')}"
    return "sha1:" + hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def fetch_new_messages(is_processed) -> list:
    """Возвращает список email.message.EmailMessage, которых ещё нет в базе.

    is_processed(message_id) -> bool — функция проверки по базе.
    """
    if not (config.IMAP_USER and config.IMAP_PASSWORD and config.ALLOWED_SENDERS):
        raise RuntimeError("Не заданы IMAP_USER / IMAP_PASSWORD / ALLOWED_SENDER в .env")

    conn = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=120)
    try:
        conn.login(config.IMAP_USER, config.IMAP_PASSWORD)
        status, _ = conn.select(config.IMAP_FOLDER, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Не удалось открыть папку {config.IMAP_FOLDER}")

        since = _imap_date(date.today() - timedelta(days=config.SINCE_DAYS))
        # Поиск FROM на сервере ненадёжен (у Яндекса новые письма попадают в индекс с задержкой),
        # поэтому берём все письма за период, а отправителя проверяем сами по заголовкам.
        status, data = conn.uid("search", None, "SINCE", since)
        if status != "OK":
            raise RuntimeError("IMAP SEARCH завершился ошибкой")
        uids = data[0].split()[-HEADER_SCAN_LIMIT:]
        log.info("Писем за %s дн.: %d", config.SINCE_DAYS, len(uids))

        # Заголовки запрашиваем порциями от новых к старым: большой запрос Яндекс может оборвать.
        # Порция — диапазон «первый:последний»: длинный список через запятую обрабатывается очень долго.
        candidates = []  # (uid, заголовки) от разрешённого отправителя
        for end in range(len(uids), 0, -HEADER_CHUNK):
            chunk = uids[max(0, end - HEADER_CHUNK):end]
            uid_range = chunk[0] + b":" + chunk[-1]
            status, resp = conn.uid("fetch", uid_range, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM DATE SUBJECT)])")
            if status != "OK":
                raise RuntimeError("IMAP FETCH завершился ошибкой")
            for item in resp:
                if not isinstance(item, tuple):
                    continue
                m = re.search(rb"UID (\d+)", item[0])
                if not m:
                    continue
                head = email.message_from_bytes(item[1], policy=policy.default)
                sender = parseaddr(str(head.get("From", "")))[1].lower()
                if sender in config.ALLOWED_SENDERS:
                    candidates.append((int(m.group(1)), head))
        candidates.sort(key=lambda c: c[0])
        log.info("Писем от разрешённого отправителя: %d", len(candidates))

        new_messages = []
        # Новые письма в конце списка — идём с конца
        for uid, head in reversed(candidates):
            if len(new_messages) >= config.MAX_EMAILS_PER_CHECK:
                break
            if is_processed(message_id_of(head)):
                continue

            status, body = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
            if status != "OK" or not body or not isinstance(body[0], tuple):
                continue
            new_messages.append(email.message_from_bytes(body[0][1], policy=policy.default))

        log.info("Новых писем к обработке: %d", len(new_messages))
        return list(reversed(new_messages))  # от старых к новым
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def safe_error_text(exc: Exception) -> str:
    """Текст ошибки для показа пользователю — без пароля на всякий случай."""
    text = str(exc)
    if config.IMAP_PASSWORD:
        text = text.replace(config.IMAP_PASSWORD, "***")
    return re.sub(r"\s+", " ", text)[:300]
