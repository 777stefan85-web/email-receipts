"""Разбор локального файла с чеком: .eml, .html, .txt или .pdf.

    python parse_sample.py samples/xxx.eml
    python parse_sample.py samples/xxx.html --no-llm --no-fetch
"""
import argparse
import email
import json
import sys
from email import policy
from pathlib import Path

import receipt_parser


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--no-llm", action="store_true", help="не обращаться к Claude API")
    ap.add_argument("--no-fetch", action="store_true", help="не скачивать чек по ссылке из письма")
    args = ap.parse_args()

    path = Path(args.path)
    opts = {"use_llm": not args.no_llm, "fetch_links": not args.no_fetch}
    suffix = path.suffix.lower()
    if suffix == ".eml":
        msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        result = receipt_parser.parse_message(msg, **opts)
    elif suffix in (".html", ".htm"):
        result = receipt_parser.parse_parts([path.read_text(encoding="utf-8", errors="replace")], [], [], **opts)
    elif suffix == ".pdf":
        result = receipt_parser.parse_parts([], [], [receipt_parser.pdf_to_text(path.read_bytes())], **opts)
    else:
        result = receipt_parser.parse_parts([], [path.read_text(encoding="utf-8", errors="replace")], [], **opts)

    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n" + receipt_parser.format_message(result))


if __name__ == "__main__":
    main()
