from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import retrieval as r
from common import read_json, write_json


def scheme_list(root: Path):
    explicit = root / "input" / "schemi.json"
    if explicit.exists():
        items = read_json(explicit)
        if isinstance(items, dict):
            items = items.get("schemi")
        if not isinstance(items, list):
            raise ValueError("schemi.json deve contenere una lista di schemi.")
    else:
        path = root / "input" / "programma.txt"
        items = []
        for line in path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pieces = line.split("|", 1)
            title = re.sub(r"^\s*(?:[-*•]\s*|\d+[.)\-:]\s*)", "", pieces[0]).strip()
            item = {"titolo": title}
            if len(pieces) > 1:
                item["keywords"] = [q.strip() for q in pieces[1].split(";") if q.strip()]
            items.append(item)
    result = []
    seen = set()
    for index, raw in enumerate(items, 1):
        item = {"titolo": raw} if isinstance(raw, str) else dict(raw)
        number = int(item.get("numero", index))
        title = str(item.get("titolo", "")).strip()
        if number < 1 or number in seen or not title:
            raise ValueError("Ogni schema deve avere titolo e numero positivo univoco.")
        seen.add(number)
        keys = item.get("keywords", item.get("retrieval_profile", []))
        if isinstance(keys, str):
            keys = [x.strip() for x in keys.split(";") if x.strip()]
        if not keys:
            keys = [title] + [word for word in r.words(title) if len(word) >= 4 and word not in {"della", "delle", "degli", "generale", "parte", "schema", "esame"}]
        result.append({"numero": number, "titolo": title, "descrizione": str(item.get("descrizione", "")), "keywords": list(dict.fromkeys(keys))})
    return result


def build_packs(root: Path, cfg: dict, schemes: list):
    with sqlite3.connect(root / "corpus" / "corpus.db") as con:
        con.row_factory = sqlite3.Row
        chunks = [dict(x) for x in con.execute("SELECT * FROM chunks ORDER BY chunk_id")]
        questions = [dict(x) for x in con.execute("SELECT * FROM questions ORDER BY question_id")]
        images = [dict(x) for x in con.execute("SELECT * FROM images ORDER BY image_id, document_id")]
    combined = {}
    for question in questions:
        key = question["content_hash"]
        if key in combined:
            combined[key]["frequency"] += question["frequency"]
            combined[key]["source_document"] += "; " + question["source_document"]
        else:
            combined[key] = dict(question)
    packs = []
    for scheme in schemes:
        profile = scheme["keywords"]
        automatic = "chunk_ids" in scheme
        if automatic:
            selected = [chunk for chunk in chunks if chunk["chunk_id"] in scheme["chunk_ids"]]
            if len(selected) != len(set(scheme["chunk_ids"])):
                raise ValueError("Il piano fa riferimento a parti del corpus mancanti.")
        else:
            selected = r.choose_chunks(chunks, profile, cfg["max_chunks"], cfg["token_budget"])
        # Il budget è effettivo anche quando il corpus ha pochi chunk.
        bounded, tokens = [], 0
        for chunk in selected:
            count = int(chunk.get("token_count") or r.token_estimate(chunk["text"]))
            if tokens + count <= cfg["token_budget"]:
                bounded.append(chunk)
                tokens += count
            elif automatic:
                raise ValueError("Una parte assegnata supera il budget: il piano deve suddividere l'argomento.")
        selected = bounded
        related = []
        for image in images:
            for chunk in selected:
                if image["document_id"] != chunk["document_id"]:
                    continue
                position = str(image["page_or_position"])
                if chunk.get("page_start") and position.isdigit() and not chunk["page_start"] <= int(position) <= (chunk.get("page_end") or chunk["page_start"]):
                    continue
                related.append(image)
                break
        picture = r.choose_images(related if automatic else images, profile, cfg["max_images"])
        if automatic:
            seen_images = {image["image_id"] for image in picture}
            for image in related:
                if len(picture) >= cfg["max_images"]:
                    break
                if image["image_id"] not in seen_images:
                    picture.append(dict(image))
                    seen_images.add(image["image_id"])
        for image in picture:
            relative = Path(image["path"])
            path = (root / relative).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError(f"Immagine mancante o esterna all'esame: {image['image_id']}")
            image["sha256"] = image.get("sha256") or __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        if automatic:
            relevant = [question for question in questions if question["question_id"] in scheme["question_ids"]]
            found_questions, by_hash = [], {}
            for question in relevant:
                key = question["content_hash"]
                if key in by_hash:
                    by_hash[key]["frequency"] += question["frequency"]
                    by_hash[key]["source_document"] += "; " + question["source_document"]
                else:
                    by_hash[key] = dict(question)
                    found_questions.append(by_hash[key])
        else:
            found_questions = r.choose_questions(list(combined.values()), profile, cfg["max_questions"])
        ext = read_json(root / "external_supplements" / f"schema_{scheme['numero']:02d}.json", None)
        coverage = {"status": "good" if selected else "external_needed", "note": "Selezione lessicale automatica: la completezza didattica va verificata sul PDF."}
        if automatic:
            coverage["note"] = "Selezione semantica: tutte le parti assegnate a questo schema sono incluse. L'assegnazione delle fonti non certifica la correttezza di ogni frase generata."
        if ext:
            coverage["status"] = "good_with_external_supplement" if selected else "partial"
        page_budget = scheme.get("page_budget")
        if cfg.get("mode") == "veloce" and not page_budget:
            if len(schemes) > cfg["max_pages"]:
                raise ValueError("Il massimo di pagine è inferiore al numero degli schemi manuali.")
            index = schemes.index(scheme)
            page_budget = cfg["max_pages"] // len(schemes) + int(index < cfg["max_pages"] % len(schemes))
        pack = {"exam": cfg["exam_name"], "scheme": scheme, "profile": profile, "coverage": coverage, "chunks": selected, "questions": found_questions, "images": picture, "token_count": tokens, "external_supplement": ext, "mode": cfg.get("mode", "completa"), "page_budget": page_budget, "page_min": scheme.get("page_min")}
        folder = root / "source_packs" / f"schema_{scheme['numero']:02d}"
        write_json(folder / "source_pack.json", pack)
        (folder / "source_pack.md").write_text(r.markdown_pack(pack), encoding="utf-8")
        packs.append(pack)
    write_json(root / "source_packs" / "index.json", [{"numero": p["scheme"]["numero"], "titolo": p["scheme"]["titolo"], "chunks": len(p["chunks"]), "questions": len(p["questions"]), "images": len(p["images"]), "tokens": p["token_count"], "coverage": p["coverage"]["status"]} for p in packs])
    return packs
