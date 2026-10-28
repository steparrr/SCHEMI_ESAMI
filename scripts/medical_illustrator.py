"""Local, reviewable schematic drafts. Never invokes a model or alters a PDF."""
from __future__ import annotations

import fcntl
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from common import digest, read_json, write_json

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
                "approved": set(), "rejected": set()}


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
            corpus = " ".join(str(chunk.get("text") or "")[:10000] for chunk in chunks[:20])
            added = 0
            for rule in RULES:
                if added >= max_per_schema or len(current_rows) >= max_project:
                    break
                hits = re.findall(rule["pattern"], corpus, re.IGNORECASE)
                if not re.search(rule["pattern"], focused, re.IGNORECASE) and len(hits) < 2:
                    continue
                # Repeated appearances elsewhere do not require duplicate atlas plates.
                if rule["id"] in seen_concepts:
                    continue
                opportunity_id = digest({"request_hash": key, "concept": rule["id"], "style": STYLE_VERSION})[:24]
                if opportunity_id in existing:
                    continue
                refs = [{"document": str(chunk.get("document_name") or "")[:160],
                         "page_start": chunk.get("page_start"), "page_end": chunk.get("page_end")}
                        for chunk in chunks[:3]]
                row = {"opportunity_id": opportunity_id, "project_id": root.name,
                       "request_hash": key, "schema_number": int(number), "concept_id": rule["id"],
                       "concept_title": rule["title"], "learning_goal": rule["goal"],
                       "lesion_types": rule["types"], "involved_structures": [],
                       "side_or_orientation": "lato e vista da verificare", "region_of_interest": scheme.get("titolo", "")[:160],
                       "view_type": "confronto schematico", "scale": "concettuale, non in scala",
                       "number_of_panels": 2, "required_annotations": ["Normale (schema)", "Alterazione (schema)"],
                       "optional_annotations": [], "proposed_caption": rule["goal"],
                       "source_references": refs, "source_hash": digest(root / job["pack"]),
                       "confidence_explanation": "Regola lessicale: presenza nel titolo/keyword o almeno due occorrenze nelle fonti; revisione necessaria.",
                       "essential_or_optional": "optional", "model_or_pipeline_route": "local_svg_template",
                       "estimated_effort": "breve bozza + revisione umana", "estimated_cost": 0,
                       "uncertainty": "anatomia e contenuto medico da verificare", "requested_by_user": False,
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


def _svg(row):
    """Use only trusted SVG shapes and escaped text; no image, script or external link."""
    svg = ET.Element("svg", {"xmlns": "http://www.w3.org/2000/svg", "viewBox": "0 0 900 520",
                              "width": "900", "height": "520", "role": "img"})
    ET.SubElement(svg, "title").text = row["concept_title"] + " — bozza da revisionare"
    ET.SubElement(svg, "rect", {"x": "0", "y": "0", "width": "900", "height": "520", "fill": STYLE["background"]})
    ET.SubElement(svg, "text", {"x": "40", "y": "48", "fill": STYLE["ink"], "font-size": "25", "font-family": "sans-serif"}).text = row["concept_title"]
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
        else:
            ET.SubElement(svg, "circle", {"cx": str(x+190), "cy": "235", "r": "72",
                                          "fill": "none", "stroke": STYLE["ink"], "stroke-width": "8"})
            ET.SubElement(svg, "circle", {"cx": str(x+(190 if index == 0 else 265)), "cy": "315", "r": "42",
                                          "fill": "none", "stroke": color, "stroke-width": "8"})
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
    try:
        root = ET.fromstring(svg_bytes)
    except ET.ParseError:
        return ["invalid_svg"]
    allowed = {"svg", "title", "rect", "text", "circle", "path"}
    if root.attrib.get("viewBox") != "0 0 900 520":
        return ["wrong_canvas"]
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] not in allowed:
            return ["unsafe_element"]
        if any(key.lower().startswith("on") or "href" in key.lower() for key in element.attrib):
            return ["unsafe_attribute"]
    text = " ".join(element.text or "" for element in root.iter() if element.tag.endswith("text"))
    if "NON VALIDATA CLINICAMENTE" not in text or "Normale (schema)" not in text:
        return ["missing_review_labels"]
    return []


def transition(root, opportunity_id, action):
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
            state = read_json(root / "state/pipeline.json", {})
            if state.get("latest", {}).get(str(row["schema_number"])) != row["request_hash"]:
                raise ValueError("Lo schema è cambiato: la tavola richiede nuova revisione.")
            current_job = state.get("jobs", {}).get(row["request_hash"], {})
            pack_path = root / current_job.get("pack", "__missing__")
            if not pack_path.is_file() or digest(pack_path) != row["source_hash"]:
                raise ValueError("Le fonti sono cambiate: la tavola richiede nuova revisione.")
            artifact = root / row["artifact"]
            if not artifact.is_file() or technical_qc(artifact.read_bytes()) or digest(artifact) != row["artifact_hash"]:
                raise ValueError("Il file SVG è mancante, modificato o non supera il controllo tecnico.")
            row["reviewed_at"] = _now()
        row["status"] = action
        if action == "accepted":
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
