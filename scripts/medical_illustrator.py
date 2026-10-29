"""Local, reviewable schematic drafts. Never invokes a model or alters a PDF."""
from __future__ import annotations

import fcntl
import math
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import fitz

from common import digest, read_json, write_json
from qc_pdf import inspect_pdf

STYLE_VERSION = "atlas-svg-1"
STYLE = {"background": "#ffffff", "ink": "#17263c", "normal": "#287d85",
         "lesion": "#b33f35", "muted": "#526276", "canvas": (900, 520)}
RULES = (
    {"id": "carpal_tunnel", "pattern": r"tunnel carpale|sindrome carpale",
     "types": ["neural", "spatial_relations", "functional"], "title": "Compressione nel tunnel carpale",
     "goal": "Localizzare la compressione e distinguere il percorso del nervo dal punto critico."},
    {"id": "fracture", "pattern": r"frattur[ae]|framment[io] osse[io]|rima di frattura",
     "types": ["structural"], "title": "Continuità ossea e frattura",
     "goal": "Confrontare continuità schematica e interruzione da verificare sulle fonti."},
    {"id": "dislocation", "pattern": r"lussazion[ei]|sublussazion[ei]|dislocazion[ei] articolari",
     "types": ["spatial_relations", "structural"], "title": "Rapporti articolari e lussazione",
     "goal": "Confrontare allineamento e disallineamento schematici."},
)
_TRANSITIONS = {"proposed": {"accepted", "rejected"},
                "accepted": {"medical_review_needed", "technical_qc_failed", "rejected"},
                "technical_qc_failed": {"accepted", "rejected"},
                "medical_review_needed": {"approved", "rejected"},
                "approved": {"published"}, "published": set(), "rejected": set()}
_EDITABLE = {"concept_title": 120, "learning_goal": 350, "side_or_orientation": 120,
             "region_of_interest": 160, "view_type": 120, "proposed_caption": 350}
_TYPES = {"structural", "spatial_relations", "neural", "tissue", "vascular",
          "functional", "developmental", "temporal"}
REVIEW_FIELDS = ("sources", "anatomy", "orientation", "lesion", "labels_caption", "rights")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _lock(root):
    path = root / "state/illustrations.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _state(root):
    return read_json(root / "state/illustrations.json", {"version": 1, "opportunities": []})


def list_opportunities(root):
    return _state(Path(root))["opportunities"]


def plan(root, max_project=7, max_per_schema=2):
    """Suggest only when a named clinical concept is found in the current source pack."""
    root = Path(root)
    state = read_json(root / "state/pipeline.json", {})
    jobs = state.get("jobs") or {}
    latest = state.get("latest") or {}
    with _lock(root):
        data = _state(root)
        if data.get("disabled", False):
            return data["opportunities"]
        existing = {row["opportunity_id"] for row in data["opportunities"]}
        current_rows = [row for row in data["opportunities"]
                        if latest.get(str(row["schema_number"])) == row["request_hash"] and row["status"] != "rejected"]
        seen_concepts = {row["concept_id"] for row in current_rows}
        for number, key in sorted(latest.items(), key=lambda item: int(item[0])):
            if len(current_rows) >= max_project:
                break
            job = jobs.get(key)
            if not job or not job.get("pack"):
                continue
            pack = read_json(root / job["pack"], {})
            scheme = pack.get("scheme") or {}
            focused = " ".join([scheme.get("titolo", ""), scheme.get("descrizione", ""),
                                *scheme.get("keywords", [])])
            chunks = pack.get("chunks") or []
            if not chunks and not pack.get("external_supplement"):
                continue
            added = 0
            for rule in RULES:
                if added >= max_per_schema or len(current_rows) >= max_project:
                    break
                matching = [chunk for chunk in chunks[:20]
                            if re.search(rule["pattern"], str(chunk.get("text") or "")[:10000], re.IGNORECASE)]
                mentions = sum(len(re.findall(rule["pattern"], str(chunk.get("text") or "")[:10000], re.IGNORECASE))
                               for chunk in matching)
                if not matching or (not re.search(rule["pattern"], focused, re.IGNORECASE) and mentions < 2):
                    continue
                # Repeated appearances elsewhere do not require duplicate atlas plates.
                if rule["id"] in seen_concepts:
                    continue
                opportunity_id = digest({"request_hash": key, "concept": rule["id"], "style": STYLE_VERSION})[:24]
                if opportunity_id in existing:
                    continue
                refs = [{"document": str(chunk.get("document_name") or "")[:160],
                         "page_start": chunk.get("page_start"), "page_end": chunk.get("page_end")}
                        for chunk in matching[:3]]
                row = {"opportunity_id": opportunity_id, "project_id": root.name,
                       "request_hash": key, "schema_number": int(number), "concept_id": rule["id"],
                       "concept_title": rule["title"], "learning_goal": rule["goal"],
                       "lesion_types": rule["types"], "involved_structures": [],
                       "side_or_orientation": "lato e vista da verificare", "region_of_interest": scheme.get("titolo", "")[:160],
                       "view_type": "confronto schematico", "scale": "concettuale, non in scala",
                       "number_of_panels": 2, "required_annotations": ["Normale (schema)", "Alterazione (schema)"],
                       "optional_annotations": [], "proposed_caption": rule["goal"],
                       "source_references": refs, "source_hash": digest(root / job["pack"]),
                       "confidence_explanation": "Regola lessicale: presenza nella fonte e nel titolo/keyword, oppure almeno due occorrenze nelle fonti; revisione necessaria.",
                       "essential_or_optional": "optional", "model_or_pipeline_route": "local_svg_template",
                       "estimated_effort": "breve bozza + revisione umana", "estimated_cost": 0,
                       "uncertainty": "anatomia e contenuto medico da verificare", "requested_by_user": False,
                       "asset_provenance": "forme vettoriali integrate; nessun asset esterno",
                       "license_status": "diritti di ridistribuzione da valutare prima della pubblicazione",
                       "label_overlay": "editable_svg_text",
                       "status": "proposed", "artifact_hash": None, "artifact": None,
                       "style_contract_version": STYLE_VERSION, "created_at": _now()}
                data["opportunities"].append(row)
                current_rows.append(row)
                existing.add(opportunity_id)
                seen_concepts.add(rule["id"])
                added += 1
        write_json(root / "state/illustrations.json", data)
        return data["opportunities"]


def set_disabled(root, disabled):
    root = Path(root)
    with _lock(root):
        data = _state(root)
        data["disabled"] = bool(disabled)
        write_json(root / "state/illustrations.json", data)
        return data["disabled"]


def _current_job(root, row):
    state = read_json(root / "state/pipeline.json", {})
    if state.get("latest", {}).get(str(row["schema_number"])) != row["request_hash"]:
        raise ValueError("Lo schema è cambiato: prepara una nuova proposta.")
    job = state.get("jobs", {}).get(row["request_hash"], {})
    relative = Path(job.get("pack", "__missing__"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Percorso delle fonti non valido.")
    pack = root / relative
    if not pack.is_file() or digest(pack) != row["source_hash"]:
        raise ValueError("Le fonti sono cambiate: prepara una nuova proposta.")
    return job


def create_manual(root, schema_number, title, lesion_type, goal):
    """User-requested conceptual plate tied to a current source pack."""
    root = Path(root)
    title, goal = str(title).strip(), str(goal).strip()
    if not 3 <= len(title) <= 120 or not 8 <= len(goal) <= 350:
        raise ValueError("Indica titolo (3–120 caratteri) e obiettivo (8–350 caratteri).")
    if lesion_type not in _TYPES:
        raise ValueError("Tipo di alterazione non valido.")
    with _lock(root):
        state = read_json(root / "state/pipeline.json", {})
        key = (state.get("latest") or {}).get(str(schema_number))
        job = (state.get("jobs") or {}).get(key)
        if not job or not job.get("pack"):
            raise ValueError("Lo schema non ha ancora un source pack corrente.")
        pack_path = root / job["pack"]
        pack = read_json(pack_path, {})
        chunks = pack.get("chunks") or []
        if not chunks and not pack.get("external_supplement"):
            raise ValueError("Lo schema non ha fonti verificabili per una tavola.")
        data = _state(root)
        opportunity_id = digest({"manual": title.casefold(), "schema": schema_number,
                                 "request_hash": key, "type": lesion_type, "style": STYLE_VERSION})[:24]
        if any(row["opportunity_id"] == opportunity_id for row in data["opportunities"]):
            raise ValueError("Questa tavola è già stata proposta per lo schema corrente.")
        refs = [{"document": str(chunk.get("document_name") or "")[:160],
                 "page_start": chunk.get("page_start"), "page_end": chunk.get("page_end")}
                for chunk in chunks[:3]]
        row = {"opportunity_id": opportunity_id, "project_id": root.name, "request_hash": key,
               "schema_number": int(schema_number), "concept_id": "manual", "concept_title": title,
               "learning_goal": goal, "lesion_types": [lesion_type], "involved_structures": [],
               "side_or_orientation": "lato e vista da verificare", "region_of_interest": title,
               "view_type": "schema concettuale", "scale": "concettuale, non in scala",
               "number_of_panels": 2, "required_annotations": ["Normale (schema)", "Alterazione (schema)"],
               "optional_annotations": [], "proposed_caption": goal, "source_references": refs,
               "source_hash": digest(pack_path),
               "confidence_explanation": "Richiesta manuale; pertinenza clinica e anatomica da verificare sulle fonti.",
               "essential_or_optional": "optional", "model_or_pipeline_route": "local_svg_template",
               "estimated_effort": "bozza + revisione umana", "estimated_cost": 0,
               "uncertainty": "anatomia e contenuto medico da verificare", "requested_by_user": True,
               "asset_provenance": "forme vettoriali integrate; nessun asset esterno",
               "license_status": "diritti di ridistribuzione da valutare prima della pubblicazione",
               "label_overlay": "editable_svg_text",
               "status": "proposed", "artifact_hash": None, "artifact": None,
               "style_contract_version": STYLE_VERSION, "created_at": _now()}
        data["opportunities"].append(row)
        write_json(root / "state/illustrations.json", data)
        return row


def update_proposal(root, opportunity_id, changes):
    root = Path(root)
    if not set(changes).issubset(_EDITABLE):
        raise ValueError("Campo della proposta non modificabile.")
    clean = {}
    for key, limit in _EDITABLE.items():
        if key in changes:
            value = str(changes[key]).strip()
            if not value or len(value) > limit:
                raise ValueError(f"{key}: indica un testo entro {limit} caratteri.")
            clean[key] = value
    with _lock(root):
        data = _state(root)
        row = next((item for item in data["opportunities"] if item["opportunity_id"] == opportunity_id), None)
        if not row or row["status"] != "proposed":
            raise ValueError("Puoi modificare solo una proposta non ancora accettata.")
        _current_job(root, row)
        row.update(clean, updated_at=_now())
        write_json(root / "state/illustrations.json", data)
        return row


def _svg(row):
    """Use only trusted SVG shapes and escaped text; no image, script or external link."""
    svg = ET.Element("svg", {"xmlns": "http://www.w3.org/2000/svg", "viewBox": "0 0 900 520",
                              "width": "900", "height": "520", "role": "img"})
    ET.SubElement(svg, "title").text = row["concept_title"] + " — bozza da revisionare"
    ET.SubElement(svg, "rect", {"x": "0", "y": "0", "width": "900", "height": "520", "fill": STYLE["background"]})
    heading = row["concept_title"][:54] + ("…" if len(row["concept_title"]) > 54 else "")
    ET.SubElement(svg, "text", {"x": "40", "y": "48", "fill": STYLE["ink"], "font-size": "25", "font-family": "sans-serif"}).text = heading
    for index, x in enumerate((40, 460)):
        ET.SubElement(svg, "rect", {"x": str(x), "y": "85", "width": "400", "height": "340",
                                    "rx": "12", "fill": "#f5f8fa", "stroke": "#9dabb8", "stroke-width": "2"})
        ET.SubElement(svg, "text", {"x": str(x + 20), "y": "120", "fill": STYLE["ink"],
                                    "font-size": "20", "font-family": "sans-serif"}).text = row["required_annotations"][index]
        color = STYLE["normal"] if index == 0 else STYLE["lesion"]
        if row["concept_id"] == "carpal_tunnel":
            # Deliberately abstract paths: no claim of exact anatomy or laterality.
            ET.SubElement(svg, "path", {"d": f"M {x+75} 190 Q {x+200} 135 {x+325} 190 L {x+325} 325 Q {x+200} 380 {x+75} 325 Z",
                                        "fill": "none", "stroke": STYLE["ink"], "stroke-width": "5"})
            ET.SubElement(svg, "path", {"d": f"M {x+195} 165 Q {x+205} 250 {x+195} 345", "fill": "none",
                                        "stroke": color, "stroke-width": "14" if index == 0 else "7"})
            if index:
                ET.SubElement(svg, "circle", {"cx": str(x+200), "cy": "255", "r": "18",
                                              "fill": "none", "stroke": color, "stroke-width": "3"})
        elif row["concept_id"] == "fracture":
            d = f"M {x+110} 190 L {x+290} 320" if index == 0 else f"M {x+110} 190 L {x+195} 245 M {x+213} 263 L {x+290} 320"
            ET.SubElement(svg, "path", {"d": d, "fill": "none", "stroke": color,
                                        "stroke-width": "38", "stroke-linecap": "round"})
        elif row["concept_id"] == "dislocation":
            ET.SubElement(svg, "circle", {"cx": str(x+190), "cy": "235", "r": "72",
                                          "fill": "none", "stroke": STYLE["ink"], "stroke-width": "8"})
            ET.SubElement(svg, "circle", {"cx": str(x+(190 if index == 0 else 265)), "cy": "315", "r": "42",
                                          "fill": "none", "stroke": color, "stroke-width": "8"})
        else:
            ET.SubElement(svg, "rect", {"x": str(x+95), "y": "175", "width": "210", "height": "145",
                                        "rx": "10", "fill": "none", "stroke": color, "stroke-width": "5"})
            ET.SubElement(svg, "text", {"x": str(x+112), "y": "255", "fill": STYLE["muted"],
                                        "font-size": "16", "font-family": "sans-serif"}).text = "Strutture da disegnare"
        ET.SubElement(svg, "text", {"x": str(x+20), "y": "402", "fill": STYLE["muted"],
                                    "font-size": "16", "font-family": "sans-serif"}).text = "Rapporti e orientamento da verificare"
    ET.SubElement(svg, "text", {"x": "40", "y": "474", "fill": STYLE["lesion"],
                                "font-size": "18", "font-family": "sans-serif"}).text = "BOZZA ILLUSTRATIVA · NON VALIDATA CLINICAMENTE"
    ET.SubElement(svg, "text", {"x": "40", "y": "501", "fill": STYLE["muted"],
                                "font-size": "14", "font-family": "sans-serif"}).text = "Verificare struttura, lato, vista, alterazione ed etichette sulle fonti."
    return ET.tostring(svg, encoding="utf-8", xml_declaration=True)


def technical_qc(svg_bytes):
    if len(svg_bytes) > 512_000:
        return ["svg_too_large"]
    upper = svg_bytes.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper or b"<?XML-STYLESHEET" in upper:
        return ["unsafe_xml_declaration"]
    try:
        root = ET.fromstring(svg_bytes)
    except ET.ParseError:
        return ["invalid_svg"]
    allowed = {"svg", "title", "rect", "text", "circle", "path"}
    attributes = {"svg": {"viewBox", "width", "height", "role"}, "title": set(),
                  "rect": {"x", "y", "width", "height", "rx", "fill", "stroke", "stroke-width"},
                  "text": {"x", "y", "fill", "font-size", "font-family"},
                  "circle": {"cx", "cy", "r", "fill", "stroke", "stroke-width"},
                  "path": {"d", "fill", "stroke", "stroke-width", "stroke-linecap"}}
    if root.tag != "{http://www.w3.org/2000/svg}svg" or root.attrib.get("viewBox") != "0 0 900 520":
        return ["wrong_canvas"]
    for element in root.iter():
        if not str(element.tag).startswith("{http://www.w3.org/2000/svg}"):
            return ["unsafe_namespace"]
        tag = element.tag.rsplit("}", 1)[-1]
        if tag not in allowed:
            return ["unsafe_element"]
        if any(key not in attributes[tag] for key in element.attrib):
            return ["unsafe_attribute"]
        if any("url(" in value.lower() or "http" in value.lower() or len(value) > 1500
               for value in element.attrib.values()):
            return ["external_reference_or_long_attribute"]
        if tag == "path" and not re.fullmatch(r"[MmLlQqCcZzHhVvSsTtAa0-9 .,+-]+", element.attrib.get("d", "")):
            return ["invalid_path"]
        if tag in {"rect", "circle", "text", "path"}:
            for key, value in element.attrib.items():
                if key in {"fill", "stroke"} and not re.fullmatch(r"#[0-9a-fA-F]{6}|none", value):
                    return ["invalid_color"]
                if key == "font-family" and not re.fullmatch(r"[A-Za-z -]{1,60}", value):
                    return ["invalid_font"]
            try:
                if tag == "rect":
                    x, y, width, height = (float(element.attrib[key]) for key in ("x", "y", "width", "height"))
                    if not all(math.isfinite(value) for value in (x, y, width, height)) or x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 900 or y + height > 520:
                        return ["element_outside_canvas"]
                elif tag == "circle":
                    x, y, radius = (float(element.attrib[key]) for key in ("cx", "cy", "r"))
                    if not all(math.isfinite(value) for value in (x, y, radius)) or radius <= 0 or x - radius < 0 or y - radius < 0 or x + radius > 900 or y + radius > 520:
                        return ["element_outside_canvas"]
                elif tag == "text":
                    x, y, size = (float(element.attrib[key]) for key in ("x", "y", "font-size"))
                    if not all(math.isfinite(value) for value in (x, y, size)) or not 0 <= x <= 900 or not 8 <= y <= 520 or not 8 <= size <= 32:
                        return ["element_outside_canvas"]
            except (KeyError, ValueError, OverflowError):
                return ["invalid_geometry"]
    text = " ".join(element.text or "" for element in root.iter() if element.tag.endswith("text"))
    if "NON VALIDATA CLINICAMENTE" not in text or "Normale (schema)" not in text:
        return ["missing_review_labels"]
    return []


def revalidate_edited_svg(root, opportunity_id):
    """Adopt a locally edited SVG only after strict technical checks; medical review restarts."""
    root = Path(root)
    with _lock(root):
        data = _state(root)
        row = next((item for item in data["opportunities"] if item["opportunity_id"] == opportunity_id), None)
        if not row or row["status"] not in {"medical_review_needed", "approved", "published"} or not row.get("artifact"):
            raise ValueError("Apri prima una bozza SVG esistente.")
        _current_job(root, row)
        path = (root / row["artifact"]).resolve()
        if not path.is_relative_to((root / "diagrams/medical").resolve()) or not path.is_file():
            raise ValueError("File della tavola non valido o mancante.")
        content = path.read_bytes()
        errors = technical_qc(content)
        if errors:
            raise ValueError("Controllo tecnico SVG fallito: " + ", ".join(errors))
        current_hash = digest(content)
        if current_hash == row.get("artifact_hash"):
            return row
        row.update(artifact_hash=current_hash, status="medical_review_needed", updated_at=_now(),
                   editor_revision=int(row.get("editor_revision") or 0) + 1)
        row.pop("reviewed_at", None)
        row.pop("review_checklist", None)
        row.pop("review_method", None)
        row.pop("published_pdf", None)
        row.pop("published_at", None)
        row.pop("published_source_hash", None)
        row.pop("published_artifact_hash", None)
        write_json(root / "state/illustrations.json", data)
        return row


def preview_svg(root, opportunity_id, scale=0.8):
    """Render a technically valid local SVG for in-app review, without approving it."""
    root = Path(root)
    row = next((item for item in list_opportunities(root) if item["opportunity_id"] == opportunity_id), None)
    if not row or not row.get("artifact"):
        raise ValueError("La tavola non ha ancora una bozza SVG.")
    path = (root / row["artifact"]).resolve()
    if not path.is_relative_to((root / "diagrams/medical").resolve()) or not path.is_file():
        raise ValueError("File della tavola non valido o mancante.")
    svg = path.read_bytes()
    errors = technical_qc(svg)
    if errors:
        raise ValueError("La bozza non supera il controllo tecnico SVG: " + ", ".join(errors))
    if not 0.25 <= scale <= 1.5:
        raise ValueError("Scala di anteprima non valida.")
    with fitz.open(stream=svg, filetype="svg") as vector:
        if vector.page_count != 1:
            raise ValueError("La bozza SVG deve avere una sola pagina.")
        return vector[0].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")


def export_illustrated_pdf(root, opportunity_id):
    """Append an approved plate to a separate PDF; never mutate pipeline artifacts."""
    root = Path(root)
    with _lock(root):
        data = _state(root)
        row = next((item for item in data["opportunities"] if item["opportunity_id"] == opportunity_id), None)
        if not row or row["status"] not in {"approved", "published"}:
            raise ValueError("Approva la tavola dopo revisione prima di esportare il PDF.")
        job = _current_job(root, row)
        if job.get("status") != "PDF_OK" or not job.get("public_pdf"):
            raise ValueError("Lo schema corrente non ha ancora un PDF pubblico pronto.")
        source = (root / job["public_pdf"]).resolve()
        if not source.is_relative_to((root / "output/pdf").resolve()) or not source.is_file():
            raise ValueError("PDF originale non valido o mancante.")
        version = root / job.get("pdf", "__missing__")
        if not version.is_file() or digest(version) != digest(source) or not inspect_pdf(source)["ok"]:
            raise ValueError("Il PDF pubblico non corrisponde alla versione corrente valida.")
        artifact = (root / row.get("artifact", "__missing__")).resolve()
        if not artifact.is_relative_to((root / "diagrams/medical").resolve()) or not artifact.is_file():
            raise ValueError("Bozza SVG mancante.")
        svg = artifact.read_bytes()
        if technical_qc(svg) or digest(svg) != row["artifact_hash"]:
            raise ValueError("La tavola è stata modificata o non supera il QC tecnico. Rivalidala e revisiona di nuovo.")
        with fitz.open(source) as original:
            if job.get("page_budget") and original.page_count + 1 > job["page_budget"]:
                raise ValueError("La tavola supererebbe il limite di pagine dello schema; il PDF non è stato modificato.")
            signature = digest({"pdf": digest(source), "svg": row["artifact_hash"],
                                "pack": row["source_hash"], "style": row["style_contract_version"]})
            folder = root / "output/pdf/illustrati"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"schema_{row['schema_number']:02d}_{signature[:24]}.pdf"
            if not path.is_file():
                temporary = path.with_suffix(".tmp.pdf")
                try:
                    with fitz.open(stream=svg, filetype="svg") as vector:
                        vector_pdf = vector.convert_to_pdf()
                    with fitz.open(stream=vector_pdf, filetype="pdf") as figure, fitz.open() as combined:
                        combined.insert_pdf(original)
                        page = combined.new_page(width=595, height=842)
                        page.insert_text((36, 62), "Tavola illustrativa - revisione utente", fontsize=16)
                        page.show_pdf_page(fitz.Rect(36, 125, 559, 430), figure, 0)
                        page.insert_textbox(fitz.Rect(36, 475, 559, 620), row["proposed_caption"], fontsize=11)
                        page.insert_textbox(fitz.Rect(36, 720, 559, 790),
                                            "Schema concettuale. Revisione umana registrata; validazione clinica specialistica non attestata.",
                                            fontsize=9)
                        toc = original.get_toc()
                        toc.append([1, "Tavola illustrativa", combined.page_count])
                        combined.set_toc(toc)
                        combined.save(temporary, garbage=3, deflate=True)
                    if not inspect_pdf(temporary)["ok"]:
                        raise ValueError("Il PDF illustrato non supera il controllo tecnico.")
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        row.update(status="published", published_pdf=str(path.relative_to(root)),
                   published_at=_now(), published_source_hash=digest(source),
                   published_artifact_hash=row["artifact_hash"])
        write_json(root / "state/illustrations.json", data)
        return path


def transition(root, opportunity_id, action, review=None):
    """User action. Acceptance creates a local draft; approval only records review."""
    root = Path(root)
    with _lock(root):
        data = _state(root)
        row = next((item for item in data["opportunities"] if item["opportunity_id"] == opportunity_id), None)
        if row is None:
            raise ValueError("Tavola inesistente.")
        if action not in _TRANSITIONS.get(row["status"], set()):
            raise ValueError("Transizione della tavola non valida.")
        if action == "approved":
            if not isinstance(review, dict) or set(review) != set(REVIEW_FIELDS) or any(review[key] is not True for key in REVIEW_FIELDS):
                raise ValueError("Completa la lista di controllo prima di segnare revisionata la tavola.")
            _current_job(root, row)
            artifact = (root / row["artifact"]).resolve()
            if not artifact.is_relative_to((root / "diagrams/medical").resolve()):
                raise ValueError("Percorso della tavola non valido.")
            if not artifact.is_file() or technical_qc(artifact.read_bytes()) or digest(artifact) != row["artifact_hash"]:
                raise ValueError("Il file SVG è mancante, modificato o non supera il controllo tecnico.")
            row["reviewed_at"] = _now()
            row["review_checklist"] = {key: True for key in REVIEW_FIELDS}
            row["review_method"] = "user_self_check_not_specialist_validation"
        row["status"] = action
        if action == "accepted":
            _current_job(root, row)
            if not re.fullmatch(r"[0-9a-f]{24}", opportunity_id):
                raise ValueError("Identificativo della tavola non valido.")
            content = _svg(row)
            errors = technical_qc(content)
            if errors:
                row["status"] = "technical_qc_failed"
                row["qc_errors"] = errors
            else:
                path = root / "diagrams/medical" / f"{opportunity_id}.svg"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                row["artifact"] = str(path.relative_to(root))
                row["artifact_hash"] = digest(content)
                row["status"] = "medical_review_needed"
                row["technical_qc"] = "passed"
        row["updated_at"] = _now()
        write_json(root / "state/illustrations.json", data)
        return row
