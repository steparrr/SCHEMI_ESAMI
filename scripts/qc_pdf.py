#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from pathlib import Path
import fitz

def inspect_pdf(p: Path):
    result={"pdf":str(p),"ok":False,"pages":0,"text_chars":0,"size_bytes":0,"errors":[]}
    if not p.exists():
        result["errors"].append("pdf_missing")
    else:
        result["size_bytes"]=p.stat().st_size
        if result["size_bytes"]<10000:
            result["errors"].append("pdf_too_small")
        try:
            doc=fitz.open(p)
            result["pages"]=doc.page_count
            if doc.page_count<1:
                result["errors"].append("no_pages")
            text_chars=0
            for page in doc:
                text_chars+=len(page.get_text("text") or "")
            result["text_chars"]=text_chars
            if text_chars<500:
                result["errors"].append("too_little_extractable_text")
        except Exception as e:
            result["errors"].append(f"fitz:{e}")
    result["ok"]=not result["errors"]
    return result

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("pdf",type=Path)
    args=ap.parse_args()
    result=inspect_pdf(args.pdf)
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(0 if result["ok"] else 2)

if __name__=="__main__":
    main()
