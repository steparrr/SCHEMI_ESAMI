from __future__ import annotations
import re, unicodedata
from typing import Any


def norm(s: Any) -> str:
    s = "" if s is None else str(s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("’", "'")
    s = re.sub(r"[^a-z0-9à-öø-ÿ' -]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9à-öø-ÿ]+", norm(s))

def token_estimate(s: str) -> int:
    return max(1, round(len(words(s)) * 1.35)) if s.strip() else 0

def query_weight(q: str) -> float:
    n = max(1, len(words(q)))
    # Le query specifiche contano più dei termini generici.
    return 1.0 + 0.8 * (n - 1)

def lexical_score(
    profile: list[str],
    text: str,
    heading: str = "",
    source: str = "",
) -> tuple[float, list[str]]:
    nt = norm(text)
    nh = norm(heading)
    ns = norm(source)

    score = 0.0
    hits: list[str] = []

    for raw_q in profile:
        q = norm(raw_q)
        if not q:
            continue

        weight = query_weight(q)
        count_text = nt.count(q)
        count_heading = nh.count(q)
        count_source = ns.count(q)

        if count_text or count_heading or count_source:
            hits.append(raw_q)

            # Heading e nome documento hanno forte valore topografico.
            score += min(count_text, 5) * 2.0 * weight
            score += min(count_heading, 3) * 6.0 * weight
            score += min(count_source, 2) * 2.5 * weight

            # Bonus per query composte, molto più discriminanti.
            if len(words(q)) >= 2:
                score += 2.5 * weight

    return round(score, 4), hits

def choose_chunks(
    rows: list[dict[str, Any]],
    profile: list[str],
    max_chunks: int,
    token_budget: int,
) -> list[dict[str, Any]]:
    ranked = []

    for row in rows:
        score, hits = lexical_score(
            profile,
            row.get("text") or "",
            row.get("heading") or "",
            row.get("document_name") or "",
        )
        if score <= 0:
            continue

        item = dict(row)
        item["_score"] = score
        item["_hits"] = hits
        ranked.append(item)

    ranked.sort(
        key=lambda x: (
            -float(x["_score"]),
            int(x.get("token_count") or 0),
            str(x.get("chunk_id") or ""),
        )
    )

    selected = []
    total = 0

    for item in ranked:
        tok = int(item.get("token_count") or token_estimate(item.get("text") or ""))

        if len(selected) >= max_chunks:
            break

        # Garantiamo comunque qualche chunk prima di applicare rigidamente il budget.
        if selected and total + tok > token_budget and len(selected) >= 8:
            continue

        selected.append(item)
        total += tok

    return selected

def choose_questions(
    rows: list[dict[str, Any]],
    profile: list[str],
    max_questions: int,
) -> list[dict[str, Any]]:
    ranked = []

    for row in rows:
        score, hits = lexical_score(
            profile,
            row.get("question_text") or "",
            row.get("topic") or "",
            row.get("source_document") or "",
        )
        if score <= 0:
            continue

        item = dict(row)
        item["_score"] = score
        item["_hits"] = hits
        ranked.append(item)

    ranked.sort(
        key=lambda x: (
            -float(x["_score"]),
            -int(x.get("frequency") or 0),
            str(x.get("question_id") or ""),
        )
    )

    return ranked[:max_questions]

def choose_images(
    rows: list[dict[str, Any]],
    profile: list[str],
    max_images: int,
) -> list[dict[str, Any]]:
    ranked = []

    for row in rows:
        if str(row.get("status") or "").lower() != "kept":
            continue

        context = "\n".join([
            str(row.get("caption") or ""),
            str(row.get("context_before") or ""),
            str(row.get("context_after") or ""),
        ])

        score, hits = lexical_score(
            profile,
            context,
            row.get("heading") or "",
            row.get("source_document") or "",
        )
        if score <= 0:
            continue

        item = dict(row)
        item["_score"] = score
        item["_hits"] = hits

        image_id = str(item.get("image_id") or "")
        ext = str(item.get("format") or "").lower()
        if ext == "jpeg":
            ext = "jpg"

        # Il builder V4 risolve le immagini da images/originals tramite ID.
        if image_id and ext:
            item["pack_path"] = f"images/{image_id}.{ext}"

        ranked.append(item)

    ranked.sort(
        key=lambda x: (
            -float(x["_score"]),
            str(x.get("image_id") or ""),
        )
    )

    # Deduplica per image_id.
    out = []
    seen = set()
    for item in ranked:
        iid = item.get("image_id")
        if not iid or iid in seen:
            continue
        seen.add(iid)
        out.append(item)
        if len(out) >= max_images:
            break

    return out

def markdown_pack(pack: dict[str, Any]) -> str:
    s = pack["scheme"]
    cov = pack.get("coverage", {})

    out = [
        f"# Schema {int(s['numero']):02d} — {s['titolo']}",
        "",
        f"**Copertura:** {cov.get('status', 'unknown')}",
        "",
        str(cov.get("note") or ""),
        "",
        "## Profilo di retrieval",
        "",
    ]

    out.extend(f"- {q}" for q in pack.get("profile", []))

    out.extend(["", "## Chunk didattici", ""])

    for i, ch in enumerate(pack.get("chunks", []), 1):
        out.extend([
            f"### {i:02d}. {ch.get('chunk_id')} — {ch.get('document_name')}",
            "",
            f"Score: {ch.get('_score')} — hits: {', '.join(ch.get('_hits', []))}",
            "",
            str(ch.get("text") or ""),
            "",
        ])

    out.extend(["## Domande d'esame", ""])
    for q in pack.get("questions", []):
        meta = []
        if q.get("professor"):
            meta.append(str(q["professor"]))
        if q.get("frequency"):
            meta.append(f"freq={q['frequency']}")
        suffix = f" [{' | '.join(meta)}]" if meta else ""
        out.append(f"- {q.get('question_text', '')}{suffix}")

    out.extend(["", "## Immagini candidate", ""])
    for im in pack.get("images", []):
        out.append(
            f"- {im.get('image_id')} — {im.get('source_document')} — "
            f"{im.get('page_or_position')} — score={im.get('_score')}"
        )

    if pack.get("external_supplement"):
        out.extend([
            "",
            "## Supplemento esterno",
            "",
            "Conservato dal pack precedente con provenienza distinta.",
        ])

    return "\n".join(out).rstrip() + "\n"
