"""Interactive macOS smoke check in a disposable project; no API client."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path


def main():
    if sys.platform != "darwin":
        raise RuntimeError("La verifica della finestra richiede macOS.")
    if sys.version_info < (3, 12):
        raise RuntimeError("Serve Python 3.12 o successivo.")

    import tkinter as tk
    import fitz
    from local_app import App
    from native_app import NativeWindow

    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="schemi-esami-verifica-mac-") as folder:
        project = Path(folder)
        shutil.copytree(source / "config", project / "config")
        (project / "esami").mkdir()
        (project / "state").mkdir()

        def blocked_api(_config):
            raise RuntimeError("Verifica Mac: nessuna chiamata API consentita.")

        app = App(project, api_factory=blocked_api)
        app.save_exam({"name": "Materia dimostrativa", "mode": "completa", "active": False})
        sample = project / "esami/materia-dimostrativa/output/pdf/verifica.pdf"
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 90), "Prova locale: testo PDF leggibile.")
        document.save(sample)
        document.close()
        with fitz.open(sample) as saved:
            if saved.page_count != 1 or "Prova locale" not in saved[0].get_text():
                raise RuntimeError("PyMuPDF non riesce a rileggere il PDF di prova.")

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
