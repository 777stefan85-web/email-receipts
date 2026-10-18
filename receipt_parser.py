"""Распознавание электронных кассовых чеков в письмах.

Порядок:
1. Достаём из письма все HTML/текстовые части, PDF-вложения и вложенные .eml.
2. Пробуем разобрать позиции кодом: HTML-таблицы, затем построчно текст.
3. Если не вышло (или сумма позиций не сходится с итогом) — спрашиваем LLM (если есть ключ).
"""
import email
import io
import json
import logging
import re
from email import policy
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

import config

log = logging.getLogger(__name__)

# Число: «1 234,50», «89.99», «2», «0,532»
NUM = r"(?:\d{1,3}(?:[  ]\d{3})+|\d+)(?:[.,]\d+)?"


def to_float(s) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace(" ", "").replace(" ", "").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        raise ValueError(f"not a number: {s!r}")
    return float(m.group(0))


# ---------------------------------------------------------------- части письма

def _part_text(part) -> str:
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def pdf_to_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        log.warning("Не удалось прочитать PDF: %s", e)
        return ""


def collect_parts(msg) -> dict:
    """Все HTML, текстовые части и текст PDF, включая вложенные письма."""
    parts = {"html": [], "text": [], "pdf": []}
    # walk() сам заходит внутрь вложений message/rfc822
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        filename = (part.get_filename() or "").lower()
        if filename.endswith(".eml"):
            # .eml, приложенный как application/octet-stream
            data = part.get_payload(decode=True) or b""
            inner = collect_parts(email.message_from_bytes(data, policy=policy.default))
            for k in parts:
                parts[k] += inner[k]
        elif ctype == "application/pdf" or filename.endswith(".pdf"):
            parts["pdf"].append(pdf_to_text(part.get_payload(decode=True) or b""))
        elif ctype == "text/html" or filename.endswith((".html", ".htm")):
            parts["html"].append(_part_text(part))
        elif ctype == "text/plain":
            parts["text"].append(_part_text(part))
    return parts


# ---------------------------------------------------------------- HTML → текст

_BLOCK_TAGS = ["div", "p", "li", "td", "th", "table", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "pre"]

def html_to_text(html: str) -> str:
    """Текст из HTML. Строки таблиц склеиваются в одну строку через « | »."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tr in soup.find_all("tr"):
        if tr.find("table"):
            continue  # строки-обёртки вёрстки не трогаем
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        tr.replace_with("\n" + " | ".join(c for c in cells if c) + "\n")
    # Переносы строк — только у блочных тегов; <span>Цена</span> <span>79,90</span> остаётся одной строкой
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    text = soup.get_text()
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def extract_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [a["href"] for a in soup.find_all("a", href=True)]


# ---------------------------------------------------------------- позиции: HTML-таблица

_COLS = {
    "name": re.compile(r"наимен|товар|предмет|позици|продукт", re.I),
    "price": re.compile(r"цена", re.I),
    "qty": re.compile(r"кол|к-во|вес", re.I),
    "sum": re.compile(r"сумм|стоим", re.I),
}


def _make_item(name, qty, price, total) -> dict | None:
    # Номер позиции: «1. Молоко», «1) Молоко», «1. | Молоко»
    name = re.sub(r"^\s*\d+\s*[.)]\s*\|?\s+", "", name or "").strip(" |.-–—:*")
    if not name or qty is None or qty <= 0:
        return None
    if re.match(r"(итог|всего|сумма|ндс|скидк|оплат|налич|безнал)", name, re.I):
        return None
    if price is None and total is not None:
        price = round(total / qty, 2)
    if total is None and price is not None:
        total = round(price * qty, 2)
    if price is None or total is None:
        return None
    return {"name": name, "qty": qty, "unit_price": price, "sum": total}


def parse_html_tables(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    best: list[dict] = []
    for table in soup.find_all("table"):
        rows = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
        cols = None
        items = []
        for tr in rows:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"], recursive=False)]
            if cols is None:
                found = {}
                for i, c in enumerate(cells):
                    for key, rx in _COLS.items():
                        if key not in found and rx.search(c):
                            found[key] = i
                            break
                if "name" in found and "qty" in found and ("price" in found or "sum" in found):
                    cols = found
                continue
            if len(cells) <= max(cols.values()):
                continue

            def num(key):
                if key not in cols:
                    return None
                try:
                    return to_float(cells[cols[key]])
                except ValueError:
                    return None

            item = _make_item(cells[cols["name"]], num("qty"), num("price"), num("sum"))
            if item:
                items.append(item)
        if len(items) > len(best):
            best = items
    return best


# ---------------------------------------------------------------- позиции: построчный текст

# «[Название] 2 x 89,99 [= 179,98]» или «89.99 * 0.532 кг = 47.87»
_INLINE_RE = re.compile(
    rf"^(?P<name>.*?)[\s|]*(?P<a>{NUM})\s*(?P<ua>шт|кг|ед)?\.?\s*[xхXХ×*]\s*(?P<b>{NUM})\s*(?P<ub>шт|кг|ед)?\.?"
    rf"(?:[\s|]*(?:=|₽|руб\.?)?[\s|]*(?P<s>{NUM}))?\s*(?:₽|руб\.?|р\.?)?$",
    re.I,
)
# «Цена: 89.99», «Кол-во 2», «Сумма 179,98»
_LABEL_RE = re.compile(
    rf"^(?P<label>цена|кол-во|количество|сумма|стоимость)\b[^\d\n]*(?P<val>{NUM})", re.I
)


def _decimals(s: str) -> int:
    m = re.search(r"[.,](\d+)$", s)
    return len(m.group(1)) if m else 0


def _pick_qty_price(a: str, ua, b: str, ub) -> tuple[float, float]:
    """Какое из двух чисел количество, а какое цена."""
    fa, fb = to_float(a), to_float(b)
    if ua:
        return fa, fb
    if ub:
        return fb, fa
    da, db = _decimals(a), _decimals(b)
    if da == 3 and db != 3:  # 0.532 — вес
        return fa, fb
    if db == 3 and da != 3:
        return fb, fa
    if da == 0 and db > 0:
        return fa, fb
    if db == 0 and da > 0:
        return fb, fa
    return (fa, fb) if fa <= fb else (fb, fa)


def parse_text_lines(text: str) -> list[dict]:
    items = []
    last_text = None  # последняя «словесная» строка — кандидат в названия
    fields: dict = {}

    def flush_labelled():
        nonlocal fields
        if last_text and "qty" in fields and ("price" in fields or "sum" in fields):
            item = _make_item(last_text, fields.get("qty"), fields.get("price"), fields.get("sum"))
            if item:
                items.append(item)
        fields = {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _INLINE_RE.match(line)
        if m:
            try:
                qty, price = _pick_qty_price(m["a"], m["ua"], m["b"], m["ub"])
                total = to_float(m["s"]) if m["s"] else None
            except ValueError:
                continue
            name = m["name"].strip(" |.-–—:")
            # «Цена * Кол | 59.99 * 1 шт. = 59.99» (билайн ОФД) — это подпись, название строкой выше
            if not name or re.match(r"(цена|кол)", name, re.I):
                name = last_text
            item = _make_item(name, qty, price, total)
            if item:
                items.append(item)
            last_text, fields = None, {}
            continue
        m = _LABEL_RE.match(line)
        if m:
            label = m["label"].lower()
            key = "price" if label == "цена" else "qty" if label.startswith("кол") else "sum"
            fields[key] = to_float(m["val"])
            continue
        if fields:
            flush_labelled()
        if re.search(r"[A-Za-zА-Яа-яЁё]{2}", line):
            last_text = line
    if fields:
        flush_labelled()
    return items


# «[Название] 1.046 239.00 249.99» — количество, цена, сумма в конце строки (PDF Магнита)
_TRIPLE_RE = re.compile(r"^(?P<name>.*?)\s*(?P<q>\d+(?:[.,]\d+)?)\s+(?P<p>\d+(?:[.,]\d+)?)\s+(?P<s>\d+(?:[.,]\d+)?)$")
# Служебные строки, которые не входят в название товара
_NOISE_RE = re.compile(
    r"^(ндс|признак|способ|мера|расчета|шт\.|кг$|код товара|рез\.|пр\.$|ц\. за|сумма|наим|итог|смена|"
    r"n:|кассовый|приход|инн|дополнительные|номер заказа)",
    re.I,
)


def parse_triple_lines(text: str) -> list[dict]:
    """Название на 1–2 строках, затем «кол-во цена сумма» (в той же или отдельной строке)."""
    items = []
    name_lines: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _TRIPLE_RE.match(line)
        if m:
            q, p, s = to_float(m["q"]), to_float(m["p"]), to_float(m["s"])
            if q > 0 and abs(q * p - s) <= max(0.02, 0.01 * s):
                name = " ".join(name_lines[-2:] + [m["name"]]).strip()
                item = _make_item(name, q, p, s)
                if item:
                    items.append(item)
                name_lines = []
                continue
        if _NOISE_RE.match(line):
            name_lines = []
        elif re.search(r"[A-Za-zА-Яа-яЁё]{2}", line):
            name_lines.append(line)
    return items


def parse_text(text: str) -> list[dict]:
    """Пробует все построчные форматы и берёт тот, что лучше сходится с итогом."""
    total = find_total(text)
    variants = [parse_text_lines(text), parse_triple_lines(text)]
    return max(variants, key=lambda items: (items_match_total(items, total), len(items)))


# ---------------------------------------------------------------- реквизиты чека

_STORES = [
    (r"перекр[её]сток", "Перекрёсток"),
    (r"пят[её]рочк", "Пятёрочка"),
    (r"вкусвилл", "ВкусВилл"),
    (r"магнит\b", "Магнит"),
    (r"\bлента\b", "Лента"),
    (r"ашан", "Ашан"),
    (r"дикси", "Дикси"),
    (r"азбука вкуса", "Азбука вкуса"),
    (r"\bметро\b|\bmetro\b", "METRO"),
    (r"о['’]?кей", "О'КЕЙ"),
    (r"самокат", "Самокат"),
    (r"яндекс[ .]?лавка", "Яндекс Лавка"),
    (r"\bozon\b|\bозон\b", "Ozon"),
    (r"wildberries|вайлдберриз", "Wildberries"),
]
_RECEIPT_MARKERS = [
    r"кассовый чек", r"\bФН\b", r"\bФД\b", r"\bФПД?\b", r"\bККТ\b", r"\bОФД\b",
    r"фискальн", r"оператор фискальных данных", r"\bИТОГО?\b", r"приход",
]
_OFD_LINK_RE = re.compile(r"ofd|nalog\.(gov\.)?ru|taxcom|kontur|sbis|check|receipt|чек", re.I)
_RECEIPT_LINK_RE = re.compile(r"[?&](fn|fd|fp|fs|fpd)=\d|/\d{16}/", re.I)


def find_total(text: str) -> float | None:
    m = re.search(rf"(?:ИТОГО?|итого к оплате|сумма чека)[\s|:=₽]*({NUM})", text, re.I)
    return to_float(m.group(1)) if m else None


def find_date(text: str) -> str | None:
    for m in re.finditer(r"\b(\d{2})\.(\d{2})\.(\d{4}|\d{2})\b", text):
        d, mth, y = int(m[1]), int(m[2]), m[3]
        if 1 <= d <= 31 and 1 <= mth <= 12:
            y = y if len(y) == 4 else "20" + y
            return f"{d:02d}.{mth:02d}.{y}"
    m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
    if m:
        return f"{m[3]}.{m[2]}.{m[1]}"
    return None


def find_store(text: str) -> str | None:
    for rx, name in _STORES:
        if re.search(rx, text, re.I):
            return name
    m = re.search(r"\b(ООО|АО|ПАО|ЗАО|ИП)\s+[«\"]?([^»\"\n|]{2,60})", text)
    return f"{m[1]} {m[2].strip()}" if m else None


def find_fiscal(text: str) -> dict:
    res = {}
    for key, rx in [("ФН", r"\bФН\b[\s:№#]*(\d{16})"), ("ФД", r"\bФД\b[\s:№#]*(\d{1,10})"),
                    ("ФП", r"\bФПД?\b[\s:№#]*(\d{6,10})")]:
        m = re.search(rx, text)
        if m:
            res[key] = m.group(1)
    return res


def receipt_score(text: str) -> int:
    return sum(1 for rx in _RECEIPT_MARKERS if re.search(rx, text, re.I))


def items_match_total(items: list[dict], total: float | None) -> bool:
    if not items:
        return False
    if total is None:
        return True
    s = sum(i["sum"] for i in items)
    return abs(s - total) <= max(1.0, 0.01 * total)


# ---------------------------------------------------------------- LLM

LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "is_receipt": {"type": "boolean"},
        "store": {"type": ["string", "null"]},
        "date": {"type": ["string", "null"]},
        "total": {"type": ["number", "null"]},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "qty": {"type": "number"},
                    "unit_price": {"type": "number"},
                    "sum": {"type": "number"},
                },
                "required": ["name", "qty", "unit_price", "sum"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["is_receipt", "store", "date", "total", "items"],
    "additionalProperties": False,
}

LLM_PROMPT = """Ниже текст письма (возможно, пересланного). Определи, есть ли в нём электронный кассовый чек.
Если есть, извлеки все позиции чека: название товара точно как в чеке (без нормализации),
количество (для весового товара — в кг), цену за единицу и сумму по позиции.
Также магазин, дату чека в формате ДД.ММ.ГГГГ и общую сумму (итог).
Если чека нет или позиций в тексте нет — is_receipt соответственно и пустой items. Ничего не выдумывай.

Текст письма:
"""


def validate_llm_result(data) -> dict:
    """Проверяет и нормализует JSON от LLM. Бросает ValueError, если что-то не так."""
    if not isinstance(data, dict) or not isinstance(data.get("is_receipt"), bool):
        raise ValueError("нет поля is_receipt")
    items_raw = data.get("items") or []
    if not isinstance(items_raw, list):
        raise ValueError("items не список")
    items = []
    for it in items_raw:
        if not isinstance(it, dict) or not str(it.get("name", "")).strip():
            raise ValueError("позиция без названия")
        qty = to_float(it.get("qty"))
        price = to_float(it["unit_price"]) if it.get("unit_price") is not None else None
        total = to_float(it["sum"]) if it.get("sum") is not None else None
        item = _make_item(str(it["name"]), qty, price, total)
        if item is None:
            raise ValueError(f"некорректная позиция: {it.get('name')!r}")
        items.append(item)
    total = data.get("total")
    return {
        "is_receipt": data["is_receipt"],
        "store": (data.get("store") or None),
        "date": find_date(str(data.get("date") or "")) or (data.get("date") or None),
        "total": to_float(total) if total is not None else None,
        "items": items,
    }


def parse_with_llm(text: str) -> dict | None:
    if not config.ANTHROPIC_API_KEY:
        return None
    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        resp = client.beta.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},  # при отказе модели — авто-переключение на другую
            output_config={"format": {"type": "json_schema", "schema": LLM_SCHEMA}},
            messages=[{"role": "user", "content": LLM_PROMPT + text}],
        )
    except anthropic.APIError as e:
        log.warning("Ошибка Claude API: %s", type(e).__name__)
        return None
    if resp.stop_reason == "refusal":
        log.warning("Модель отказалась обрабатывать письмо")
        return None
    raw = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return validate_llm_result(json.loads(raw))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
        log.warning("LLM вернула некорректный JSON: %s", e)
        return None


# ---------------------------------------------------------------- общий разбор

def fetch_linked_receipt(links: list[str]) -> tuple[str, str, list[dict]] | None:
    """Скачивает чек по ссылке из письма. Возвращает (метод, текст, позиции) или None.

    Ссылка может вести на PDF, на HTML-страницу с чеком или на страницу, которая
    показывает PDF (как у ОФД Магнита): тогда достаём путь к PDF и скачиваем его в той же сессии.
    """
    import http.cookiejar
    import urllib.request
    from urllib.parse import urljoin

    best = None
    for url in links[:3]:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        opener.addheaders = [("User-Agent", "Mozilla/5.0")]
        try:
            resp = opener.open(url, timeout=LINK_TIMEOUT)
            data = resp.read(LINK_MAX_BYTES)
            ctype = resp.headers.get("Content-Type", "")
            if "pdf" in ctype or data[:5] == b"%PDF-":
                text = pdf_to_text(data)
                found = ("link_pdf", text, parse_text(text))
            else:
                html = data.decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
                # Путь к PDF внутри страницы, иногда с экранированными слэшами: \\/CheckWebApp\\/...\.pdf
                m = re.search(r"(?:\\*/[\w.-]+)+\.pdf[^'\"\]\s]*", html)
                if m:
                    pdf_url = urljoin(url, re.sub(r"\\+", "", m.group(0)))
                    pdf = opener.open(pdf_url, timeout=LINK_TIMEOUT).read(LINK_MAX_BYTES)
                    text = pdf_to_text(pdf) if pdf[:5] == b"%PDF-" else ""
                    found = ("link_pdf", text, parse_text(text))
                else:
                    text = html_to_text(html)
                    found = ("link_html", text, parse_html_tables(html) or parse_text(text))
        except Exception as e:
            log.warning("Не удалось скачать чек по ссылке: %s", type(e).__name__)
            continue
        if found[2]:
            return found
        best = best or found
    return best


LINK_TIMEOUT = 30
LINK_MAX_BYTES = 5_000_000


def parse_parts(htmls: list[str], texts: list[str], pdf_texts: list[str], use_llm: bool = True,
                fetch_links: bool = True) -> dict:
    sources = []  # (метод, текст, позиции)
    links: list[str] = []
    for html in htmls:
        links += extract_links(html)
        text = html_to_text(html)
        items = parse_html_tables(html)
        sources.append(("html_table" if items else "html_text", text, items or parse_text(text)))
    for t in texts:
        sources.append(("text", t, parse_text(t)))
    for t in pdf_texts:
        sources.append(("pdf", t, parse_text(t)))

    full_text = "\n\n".join(s[1] for s in sources)
    links += re.findall(r"https?://[^\s<>\"'|]+", full_text)
    links = list(dict.fromkeys(u.rstrip(").,") for u in links
                               if u.startswith("http") and _OFD_LINK_RE.search(u)))
    # Ссылки на конкретный чек (с ФН/ФД/ФП в адресе) важнее главных страниц сайтов ОФД и ФНС
    specific = [u for u in links if _RECEIPT_LINK_RE.search(u)]
    links = specific or links

    total = find_total(full_text)
    # лучший источник: сначала тот, где сумма позиций сходится с итогом, затем по числу позиций
    best = max(sources, key=lambda s: (items_match_total(s[2], find_total(s[1])), len(s[2])), default=None)
    method, best_text, items = best if best else (None, "", [])
    if best_text:
        total = find_total(best_text) or total

    result = {
        "is_receipt": bool(items) or receipt_score(full_text) >= 2,
        "store": find_store(best_text) or find_store(full_text),
        "date": find_date(best_text) or find_date(full_text),
        "total": total,
        "items": items,
        "fiscal": find_fiscal(full_text),
        "links": links,
        "source": method if items else None,
        "warnings": [],
    }

    # Позиций в письме нет, но есть ссылка на чек — пробуем скачать чек с сайта ОФД
    if not items and fetch_links and links:
        fetched = fetch_linked_receipt(links)
        if fetched:
            method, link_text, items = fetched
            full_text += "\n\n" + link_text
            if items:
                total = find_total(link_text) or total
                result.update(
                    is_receipt=True, items=items, source=method, total=total,
                    store=result["store"] or find_store(link_text),
                    date=result["date"] or find_date(link_text),
                )

    if items and items_match_total(items, total):
        return result

    if items:
        result["warnings"].append("Сумма позиций не совпадает с итогом чека")
    if not use_llm or receipt_score(full_text) == 0 or not full_text.strip():
        return result
    if not config.ANTHROPIC_API_KEY:
        result["warnings"].append("Ключ Claude API не задан — LLM-разбор пропущен")
        return result

    llm = parse_with_llm(full_text)
    if llm is None:
        result["warnings"].append("LLM-разбор не удался")
        return result
    if llm["items"] and (not items or items_match_total(llm["items"], llm["total"] or total)):
        result.update(
            is_receipt=True, items=llm["items"], source="llm",
            store=llm["store"] or result["store"], date=llm["date"] or result["date"],
            total=llm["total"] if llm["total"] is not None else result["total"],
            warnings=[],
        )
    elif not items:
        result["is_receipt"] = result["is_receipt"] or llm["is_receipt"]
    return result


def parse_message(msg, use_llm: bool = True, fetch_links: bool = True) -> dict:
    parts = collect_parts(msg)
    result = parse_parts(parts["html"], parts["text"], parts["pdf"], use_llm=use_llm, fetch_links=fetch_links)
    if not result["date"] and msg.get("Date"):
        try:
            result["date"] = parsedate_to_datetime(str(msg["Date"])).strftime("%d.%m.%Y")
        except (TypeError, ValueError):
            pass
    return result


def receipt_key(result: dict) -> str:
    """Отпечаток чека: одинаковый у повторных пересылок одного и того же чека."""
    items = sorted((i["name"], round(i["sum"], 2)) for i in result.get("items") or [])
    return json.dumps([result.get("date"), result.get("total"), items], ensure_ascii=False)


def status_of(result: dict) -> str:
    if result["items"]:
        return "receipt"
    return "no_items" if result["is_receipt"] else "not_receipt"


# ---------------------------------------------------------------- вывод для пользователя

def fmt_money(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


def fmt_qty(q: float) -> str:
    if abs(q - round(q)) < 1e-9:
        return f"{int(round(q))} шт"
    return f"{q:.3f}".rstrip("0").rstrip(".").replace(".", ",") + " кг"


def format_message(result: dict) -> str:
    head = "Пришёл чек"
    if result.get("date"):
        head += f" от {result['date']}"
    if result.get("store"):
        head += f", {result['store']}"
    if result.get("total") is not None:
        head += f", итого {fmt_money(result['total'])} ₽"

    if result["items"]:
        lines = [head + ":"]
        for it in result["items"]:
            lines.append(f"— {it['name']} — {fmt_qty(it['qty'])} × {fmt_money(it['unit_price'])} = {fmt_money(it['sum'])}")
    elif result["is_receipt"]:
        lines = [head + ".", "Позиций в письме нет."]
        if result.get("fiscal"):
            lines.append("Реквизиты: " + ", ".join(f"{k} {v}" for k, v in result["fiscal"].items()))
        for url in result.get("links", []):
            lines.append(f"Ссылка на чек: {url}")
    else:
        lines = ["Письмо без чека."]
    for w in result.get("warnings", []):
        lines.append(f"⚠ {w}")
    return "\n".join(lines)
