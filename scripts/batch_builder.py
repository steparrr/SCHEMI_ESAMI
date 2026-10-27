from __future__ import annotations
import base64, mimetypes
from pathlib import Path
from typing import Any, Dict, List
PIPELINE_PROMPT=""


def data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"

def compact(text: Any, limit: int = 520) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit]

def image_manifest_text(pack: Dict[str, Any]) -> str:
    images = pack.get("images", [])
    if not images:
        return "(nessuna immagine candidata)"

    lines = []
    for im in images:
        bits = [
            f"LOCAL_IMAGE_ID={im.get('image_id')}",
            f"fonte={im.get('source_document') or '-'}",
            f"posizione={im.get('page_or_position') or '-'}",
        ]

        if im.get("heading"):
            bits.append(f"sezione={compact(im.get('heading'), 180)}")
        if im.get("caption"):
            bits.append(f"caption={compact(im.get('caption'), 300)}")
        if im.get("context_before"):
            bits.append(
                f"contesto_prima={compact(im.get('context_before'), 520)}"
            )
        if im.get("context_after"):
            bits.append(
                f"contesto_dopo={compact(im.get('context_after'), 520)}"
            )

        lines.append("- " + " | ".join(bits))

    return "\n".join(lines)

def pack_text(pack: Dict[str, Any]) -> str:
    scheme = pack["scheme"]
    coverage = pack.get("coverage", {})

    parts = [
        f"# SCHEMA {int(scheme['numero']):02d} — {scheme['titolo']}",
        "Obiettivi del piano: " + str(scheme.get("descrizione", "")),
        f"Copertura locale: {coverage.get('status', 'unknown')}",
        f"Nota copertura: {coverage.get('note', '')}",
        "",
        "## CHUNK DIDATTICI",
    ]

    for i, ch in enumerate(pack.get("chunks", []), 1):
        parts.extend([
            "",
            f"### CHUNK {i:02d} — {ch.get('chunk_id')} — {ch.get('document_name')}",
            f"Heading: {ch.get('heading') or '-'}",
            str(ch.get("text") or ""),
        ])

    parts.append("")
    parts.append("## DOMANDE / RESOCONTI D'ESAME")

    for q in pack.get("questions", []):
        loc = q.get("location") or ""
        prof = q.get("professor") or ""
        freq = q.get("frequency") or ""

        meta = ", ".join(
            x for x in [
                prof,
                loc,
                f"freq={freq}" if freq else "",
            ]
            if x
        )

        line = f"- {q.get('question_text', '')}"
        if meta:
            line += f" [{meta}]"
        parts.append(line)

        if q.get("answer_text"):
            parts.append(
                f"  Risposta presente in fonte: {q['answer_text']}"
            )

    ext = pack.get("external_supplement")
    if ext:
        parts.extend([
            "",
            "## INTEGRAZIONE ESTERNA MIRATA — separata dalle sbobine",
            "Lacuna: " + str(ext.get("gap") or ""),
        ])

        for note in ext.get("notes", []):
            parts.append("- " + str(note))

        if ext.get("sources"):
            parts.append("Fonti esterne:")
            for src in ext.get("sources", []):
                parts.append(
                    f"- {src.get('title')}: {src.get('url')}"
                )

    parts.extend([
        "",
        "## IMAGE MANIFEST — IMMAGINI CANDIDATE DELLO SCHEMA",
        image_manifest_text(pack),
    ])

    return "\n".join(parts)

def coverage_qualifier(pack: Dict[str, Any]) -> str:
    status = pack.get("coverage", {}).get("status", "unknown")

    if status == "good_with_external_supplement":
        return (
            "\nIl pack contiene una INTEGRAZIONE ESTERNA MIRATA. "
            "Usala esclusivamente per le lacune dichiarate e non confonderla "
            "con il materiale delle sbobine.\n"
        )

    if status == "partial":
        return (
            "\nLa copertura locale è PARTIAL. Non inventare contenuti mancanti. "
            "Segnala nell'HTML le lacune dichiarate dal corpus.\n"
        )

    if status == "external_needed":
        return (
            "\nIl pack è EXTERNAL_NEEDED. Questa richiesta deve essere considerata "
            "solo un'anteprima finché le fonti mancanti non vengono integrate.\n"
        )

    return ""

def build_body(
    pack: Dict[str, Any],
    model: str,
    effort: str,
    max_output_tokens: int,
    image_mode: str,
    image_detail: str,
    instructions: str | None = None,
) -> Dict[str, Any]:

    n = int(pack["scheme"]["numero"])
    title = pack["scheme"]["titolo"]

    user_text = (
        f"Genera lo SCHEMA {n:02d}: {title}.\n"
        f"{coverage_qualifier(pack)}\n"
        "Usa il SOURCE PACK come base vincolante.\n"
        "Restituisci esclusivamente HTML V4 nel formato "
        '<article class="study-schema">...</article>.\n'
        "Quando un'immagine è realmente fornita come input visivo, "
        "il testo immediatamente precedente specifica il suo LOCAL_IMAGE_ID. "
        "Se decidi di inserirla nello schema, usa ESATTAMENTE quell'ID nel "
        "tag <img data-local-id=\"...\">.\n\n"
        "SOURCE PACK:\n\n"
        + pack_text(pack)
    )
    if pack.get("page_min") and pack.get("mode") == "completa":
        user_text += (f"\n\nLUNGHEZZA DEL VOLUME: mira a {pack['page_min']}–{pack['page_budget']} pagine A4 "
                      "effettive dopo l'impaginazione. Organizza i temi in sezioni leggibili, con tabelle e schemi utili. "
                      "La lunghezza non giustifica ripetizioni, spazi vuoti o informazioni non documentate: "
                      "se le fonti non bastano, dichiara le lacune con precisione.")
    elif pack.get("page_budget"):
        pages = pack["page_budget"]
        user_text += (f"\n\nMODALITÀ VELOCE: questo schema dispone di AL MASSIMO {pages} pagine A4, "
                      "incluse immagini e bibliografia aggiunta dal renderer. "
                      f"Mira a non oltre {max(100, pages * 300 - 80)} parole. "
                      "Mantieni definizioni, criteri, distinzioni, eccezioni e risposte essenziali. "
                      "Riduci ripetizioni ed esempi secondari. Non inventare dati o omettere precisazioni che rendono vera un'affermazione. "
                      "Usa diagrammi e immagini solo se indispensabili; la gerarchia dei titoli resta coerente.")

    content: List[Dict[str, Any]] = [
        {
            "type": "input_text",
            "text": user_text,
        }
    ]

    if image_mode == "data-url":
        for im in pack.get("images", []):
            iid = str(im.get("image_id") or "")
            path = Path(im["resolved_path"])

            content.append({
                "type": "input_text",
                "text": (
                    f"IMMAGINE VISIVA SUCCESSIVA — LOCAL_IMAGE_ID={iid}\n"
                    f"Fonte: {im.get('source_document') or '-'}\n"
                    f"Posizione: {im.get('page_or_position') or '-'}\n"
                    f"Contesto: {compact(im.get('context_before'), 420)}"
                ),
            })

            content.append({
                "type": "input_image",
                "image_url": data_url(path),
                "detail": image_detail,
            })

    return {
        "model": model,
        "reasoning": {"effort": effort},
        "instructions": PIPELINE_PROMPT if instructions is None else instructions,
        "input": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "max_output_tokens": max_output_tokens,
        "store": False,
        "metadata": {
            "pipeline": "schemi-esami-v2",
            "schema": f"{n:02d}",
            "image_mode": image_mode,
        },
    }
