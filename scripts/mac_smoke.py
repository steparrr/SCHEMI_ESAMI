"""Interactive macOS smoke check in a disposable project; no API client."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path


def prepare_demo(project: Path):
    """Create a disposable exam with visible PDF and illustration states."""
    import fitz
    from common import read_json, write_json
    from finalize_pdf import finalize
    from local_app import App
    from medical_illustrator import plan, transition

    def blocked_api(_config):
        raise RuntimeError("Verifica Mac: nessuna chiamata API consentita.")

    app = App(project, api_factory=blocked_api)
    slug = app.save_exam({"name": "Materia dimostrativa", "mode": "completa", "active": False})
    exam = app.root(slug)
    source = exam / "input/sbobine/Fonte dimostrativa.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("Tunnel carpale e nervo mediano. Frattura ossea: confronto schematico di prova.\n",
                      encoding="utf-8")
    key = "a" * 64
    pack = exam / "source_packs/dimostrazione.json"
    pack_data = {"scheme": {"numero": 1, "titolo": "Tunnel carpale e frattura",
                            "keywords": ["tunnel carpale", "frattura"]},
                 "chunks": [{"text": "Tunnel carpale e nervo mediano.",
                             "document_name": source.name, "page_start": 1, "page_end": 1},
                            {"text": "Frattura ossea: schema di prova.",
                             "document_name": source.name, "page_start": 1, "page_end": 1}]}
    write_json(pack, pack_data)
    raw_html = exam / "output/html/verifica.raw.html"
    raw_html.parent.mkdir(parents=True, exist_ok=True)
    raw_html.write_text("<article class='study-schema'><h1>Schema dimostrativo</h1>"
                        "<p>Prova locale: testo PDF leggibile.</p></article>", encoding="utf-8")
    manifest = exam / "state/verifica-immagini.jsonl"
    manifest.write_text("", encoding="utf-8")
    sample = exam / "output/pdf/verifica.pdf"
    finalize(raw_html, sample, "Schema dimostrativo", exam, pack_data, manifest,
             project=Path(__file__).resolve().parents[1])
    with fitz.open(sample) as saved:
        if saved.page_count < 1 or "Prova locale" not in " ".join(page.get_text() for page in saved):
            raise RuntimeError("Il renderer locale non ha prodotto un PDF leggibile.")
    write_json(exam / "state/pipeline.json", {
        "version": 1, "jobs": {key: {"hash": key, "kind": "schema", "numero": 1,
                                    "title": "Tunnel carpale e frattura", "status": "PDF_OK",
                                    "pack": str(pack.relative_to(exam)), "retry_count": 0,
                                    "pdf": str(sample.relative_to(exam)),
                                    "public_pdf": str(sample.relative_to(exam))}},
        "latest": {"1": key}, "batches": []})
    write_json(exam / "output/reports/pipeline_report.json", {
        "revision": read_json(exam / "esame.json").get("revision", 0),
        "input_status": "PROVA LOCALE · nessuna API", "planning": [], "exports": {},
        "rows": [{"numero": 1, "title": "Tunnel carpale e frattura", "status": "PDF_OK",
                  "public_pdf": str(sample.relative_to(exam))}]})
    proposals = plan(exam)
    if len(proposals) != 2:
        raise RuntimeError("La prova non ha preparato le due tavole schematiche attese.")
    transition(exam, proposals[0]["opportunity_id"], "accepted")
    return app


def main():
    if sys.platform != "darwin":
        raise RuntimeError("La verifica della finestra richiede macOS.")
    if sys.version_info < (3, 12):
        raise RuntimeError("Serve Python 3.12 o successivo.")

    import tkinter as tk
    from native_app import NativeWindow

    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="schemi-esami-verifica-mac-") as folder:
        project = Path(folder)
        shutil.copytree(source / "config", project / "config")
        (project / "esami").mkdir()
        (project / "state").mkdir()

        app = prepare_demo(project)

        class SmokeWindow(NativeWindow):
            def set_api_key(self):
                self.notice.set("Verifica Mac: il Portachiavi e le API sono disabilitati.")

        window = None
        root = tk.Tk()
        try:
            window = SmokeWindow(root, app)
            root.title("VERIFICA MAC · Schemi Esami (dati temporanei)")
            window.notice.set("Questa è una prova con dati temporanei. Le API sono disabilitate. Esamina le schede, poi chiudi la finestra.")
            root.update_idletasks()
            root.mainloop()
        finally:
            app.stop.set()
            if root.winfo_exists():
                root.destroy()
    print("Verifica locale completata. Nessun esame reale o servizio API è stato usato.")


if __name__ == "__main__":
    main()
