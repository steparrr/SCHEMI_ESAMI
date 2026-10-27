"""Psychiatry renderer: keep all text, suppress Story's repeated page backgrounds."""
from __future__ import annotations

import importlib.util
from pathlib import Path


_path = Path(__file__).resolve().parents[1] / "renderer" / "render_pdf.py"
_spec = importlib.util.spec_from_file_location("base_study_renderer", _path)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

# Story occasionally repeats fills from cards on every later page. The borders,
# text and embedded diagrams remain; only decorative container fills are removed.
_clear_fills = ("\n.callout,.recognize-box,figure.image-card,.flowchart,.flow-branch,.web-image-slot,"
                "table,thead,tbody,tr,th,td"
                "{background:transparent!important;background-color:transparent!important;}\n")
CSS = _base.CSS + _clear_fills
OUTPUT = _base.OUTPUT
move_sources_to_end = _base.move_sources_to_end
repair_layout = _base.repair_layout


def render_pdf(article_html, pdf_path, title):
    _base.CSS = CSS
    _base.OUTPUT = OUTPUT
    return _base.render_pdf(article_html, pdf_path, title)
