"""Разбор локального файла с чеком: .eml, .html, .txt или .pdf.

    python parse_sample.py samples/xxx.eml
    python parse_sample.py samples/xxx.html --no-llm
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
    args = ap.parse_args()

    path = Path(args.path)
    use_llm = not args.no_llm
    suffix = path.suffix.lower()
    if suffix == ".eml":
        msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        result = receipt_parser.parse_message(msg, use_llm=use_llm)
    elif suffix in (".html", ".htm"):
        result = receipt_parser.parse_parts([path.read_text(encoding="utf-8", errors="replace")], [], [], use_llm)
    elif suffix == ".pdf":
        result = receipt_parser.parse_parts([], [], [receipt_parser.pdf_to_text(path.read_bytes())], use_llm)
    else:
        result = receipt_parser.parse_parts([], [path.read_text(encoding="utf-8", errors="replace")], [], use_llm)

    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n" + receipt_parser.format_message(result))


if __name__ == "__main__":
    main()
