"""Streamlit-интерфейс: лента чеков из почты. Запуск: streamlit run app.py"""
import csv
import io
import logging
import time
from datetime import datetime

import streamlit as st

import config
import mail_client
import receipt_parser
import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


def check_mail() -> tuple[int, str | None]:
    """Забирает новые письма, разбирает и сохраняет. Возвращает (кол-во новых, ошибка)."""
    try:
        messages = mail_client.fetch_new_messages(
            lambda mid: storage.is_processed(mid, receipt_parser.PARSER_VERSION))
    except Exception as e:
        log.warning("Ошибка IMAP: %s", type(e).__name__)
        return 0, mail_client.safe_error_text(e)

    # Отпечатки уже показанных чеков — чтобы повторно пересланный чек не дублировался в ленте.
    # Письма, которые сейчас разбираются заново, не считаем: иначе чек совпадёт сам с собой.
    batch = {mail_client.message_id_of(m) for m in messages}
    known = {receipt_parser.receipt_key(e["data"]) for e in storage.list_entries()
             if e["status"] == "receipt" and e["message_id"] not in batch}
    for msg in messages:
        mid = mail_client.message_id_of(msg)
        subject = str(msg.get("Subject", ""))[:200]
        try:
            result = receipt_parser.parse_message(msg)
            status = receipt_parser.status_of(result)
            if status == "receipt":
                key = receipt_parser.receipt_key(result)
                if key in known:
                    status = "duplicate"
                known.add(key)
        except Exception as e:
            log.exception("Ошибка разбора письма %s", mid)
            result, status = {"error": type(e).__name__}, "error"
        storage.save(mid, str(msg.get("Date", "")), subject, status, result)
        log.info("Письмо %s: %s", mid, status)
    st.session_state["last_check"] = datetime.now().strftime("%H:%M:%S")
    st.session_state["last_check_ts"] = time.time()
    return len(messages), None


def items_csv(items: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Название", "Количество", "Цена", "Сумма"])
    for it in items:
        w.writerow([it["name"], str(it["qty"]).replace(".", ","),
                    f"{it['unit_price']:.2f}".replace(".", ","), f"{it['sum']:.2f}".replace(".", ",")])
    return buf.getvalue().encode("utf-8-sig")  # BOM, чтобы Excel открыл кириллицу


st.set_page_config(page_title="Чеки из почты", page_icon="🧾")
st.title("🧾 Чеки из почты")
senders = "любые" if config.ANY_SENDER else (", ".join(config.ALLOWED_SENDERS) or "—")
st.caption(f"Ящик: {config.IMAP_USER or '—'} · отправители: {senders} · "
           f"LLM: {'вкл' if config.ANTHROPIC_API_KEY else 'выкл'}")

col1, col2 = st.columns([1, 2])
if col1.button("Проверить почту", type="primary"):
    with st.spinner("Проверяю почту…"):
        n, err = check_mail()
    if err:
        st.error(f"Ошибка почты: {err}")
    else:
        st.success(f"Новых писем: {n}")
show_all = col2.checkbox("Показывать письма без чека")


@st.fragment(run_every=f"{config.AUTO_REFRESH_MINUTES}m")
def auto_check():
    # Фрагмент перезапускается и при любом клике — проверяем почту, только если прошло N минут
    now = time.time()
    last = st.session_state.setdefault("last_check_ts", now)
    if now - last >= config.AUTO_REFRESH_MINUTES * 60 - 5:
        n, err = check_mail()
        if n and not err:
            st.rerun()  # перерисовать ленту целиком
    st.caption(f"Автопроверка каждые {config.AUTO_REFRESH_MINUTES} мин · "
               f"последняя проверка: {st.session_state.get('last_check', 'ещё не было')}")


auto_check()
st.divider()

entries = storage.list_entries(include_non_receipts=show_all)
if not entries:
    st.info("Пока пусто. Нажмите «Проверить почту».")

for e in entries:
    data = e["data"] or {}
    with st.chat_message("assistant", avatar="🧾"):
        if e["status"] == "error":
            st.markdown(f"Не удалось разобрать письмо «{e['subject']}» ({data.get('error')})")
            continue
        if e["status"] == "duplicate":
            st.caption(f"Повтор уже показанного чека: «{e['subject']}» · {e['mail_date']}")
            continue
        text = receipt_parser.format_message(data)
        head, _, rest = text.partition("\n")
        st.markdown(f"**{head}**")
        if rest:
            st.text(rest)
        st.caption(f"Письмо: {e['subject']} · {e['mail_date']} · разбор: {data.get('source') or '—'}")
        items = data.get("items") or []
        if items:
            with st.expander(f"Позиции ({len(items)})"):
                st.dataframe(
                    [{"Название": i["name"], "Кол-во": i["qty"], "Цена": i["unit_price"], "Сумма": i["sum"]}
                     for i in items],
                    width="stretch",
                )
                st.download_button(
                    "Скачать CSV", items_csv(items),
                    file_name=f"chek_{(data.get('date') or 'bez_daty').replace('.', '-')}.csv",
                    mime="text/csv", key=f"csv_{e['message_id']}",
                )
