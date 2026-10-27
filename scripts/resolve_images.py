#!/usr/bin/env python3
import argparse, base64, csv, json, mimetypes
from pathlib import Path
from bs4 import BeautifulSoup

ID_KEYS = ("id", "local_id", "LOCAL_IMAGE_ID", "image_id")
PATH_KEYS = ("path", "file", "filepath", "local_path", "image_path")
SOURCE_KEYS = ("source_document", "source", "source_file", "document", "origin")
LOCATION_KEYS = ("page_or_position", "location", "page", "position")
CAPTION_KEYS = ("caption", "description", "title")

def first(rec, keys):
    for k in keys:
        v = rec.get(k)
        if v not in (None, ""):
            return str(v)
    return ""

def load_manifest(path: Path):
    records = []
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as f:
            records = list(csv.DictReader(f))
    else:
        with path.open(encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"JSONL non valido alla riga {n}: {e}") from e

    out = {}
    for rec in records:
        rid = first(rec, ID_KEYS)
        if rid:
            out[rid] = rec
    return out

def file_to_data_uri(path: Path):
    mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        mime = "application/octet-stream"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"

def resolve_images(article_html: str, manifest_path: Path, project_root: Path):
    manifest = load_manifest(manifest_path)
    soup = BeautifulSoup(article_html, "html.parser")

    missing = []
    resolved = 0

    for img in soup.find_all("img"):
        local_id = img.get("data-local-id")
        if not local_id:
            continue

        rec = manifest.get(local_id)
        if not rec:
            missing.append(f"{local_id}: non presente nel manifest")
            continue

        raw_path = first(rec, PATH_KEYS)
        if not raw_path:
            missing.append(f"{local_id}: path mancante nel manifest")
            continue

        p = Path(raw_path).expanduser()
        if not p.is_absolute():
            # First interpret relative to project root, then manifest directory.
            p1 = (project_root / p).resolve()
            p2 = (manifest_path.parent / p).resolve()
            p = p1 if p1.exists() else p2

        if not p.exists():
            missing.append(f"{local_id}: file non trovato: {p}")
            continue

        img["src"] = file_to_data_uri(p)

        source = first(rec, SOURCE_KEYS)
        location = first(rec, LOCATION_KEYS)
        if source:
            img["data-local-source"] = source
        if location:
            img["data-local-location"] = location

        if not img.get("alt"):
            alt = first(rec, CAPTION_KEYS)
            if alt:
                img["alt"] = alt
        resolved += 1

    if missing:
        raise RuntimeError("Resolver immagini fallito:\n- " + "\n- ".join(missing))

    return str(soup), resolved

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_html")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--project-root", default=".")
    args = ap.parse_args()

    html_text = Path(args.input_html).read_text(encoding="utf-8")
    resolved_html, count = resolve_images(
        html_text,
        Path(args.manifest).resolve(),
        Path(args.project_root).resolve(),
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(resolved_html, encoding="utf-8")
    print(f"Creato: {out} | immagini risolte: {count}")

if __name__ == "__main__":
    main()
