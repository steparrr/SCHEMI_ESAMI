"""Finestra desktop: accesso diretto al motore, senza HTTP o browser."""
from __future__ import annotations

import fcntl
import json
import queue
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from common import PROJECT, read_json, write_json
from local_app import App, KINDS, MAX_FILE_BYTES, edit_lock, save_api_key
from ingest import SUPPORTED
from model_settings import MODEL_KEYS, save_default_profile
from monitor import batch_lines, overview


def diagnostics(app, slug):
    root = app.root(slug)
    state = read_json(root / "state/pipeline.json", {})
    lines = ["Materia: " + read_json(root / "esame.json")["nome"],
             "Fase: " + str(state.get("input_status") or "Materiali salvati")]
    if not state.get("batches"):
        lines.append("Nessun batch registrato. Prima dell'invio vengono importate e organizzate le fonti.")
    lines.extend(batch_lines(state))
    rescue = read_json(root / "output/reports/search-and-rescue.json", {})
    if rescue:
        lines.extend(["", "Search and Rescue · " + str(rescue.get("generated_at", "")),
                      "Lavori OpenAI controllati: " + str(rescue.get("checked_batches", 0)) +
                      " · HTML ripristinati: " + str(len(rescue.get("restored_html", []))) +
                      " · PDF recuperati: " + str(len(rescue.get("recovered_pdfs", [])))])
        for row in rescue.get("rows", []):
            lines.append(f"Schema {row['numero']:02d} · {row['status']} · {row['detail']}")
        for item in rescue.get("planning", []):
            lines.append(f"Piano {item['phase']} · {item['status']} · {item['detail']}")
        for error in rescue.get("errors", []):
            lines.append("Controllo non riuscito: " + str(error)[:400])
    for job in state.get("jobs", {}).values():
        if job.get("error"):
            lines.append("Richiesta " + str(job.get("title", "")) + ": " + str(job["error"])[:2000])
    if app.errors.get(slug):
        lines.append("Errore di avvio: " + str(app.errors[slug]))
    background = read_json(app.project / "state/worker-heartbeat.json", {})
    if background.get("errors", {}).get(slug):
        lines.append("Errore del motore in background: " + str(background["errors"][slug]))
    log = root / "logs/pipeline.log"
    if log.is_file():
        with log.open("rb") as handle:
            handle.seek(max(0, log.stat().st_size - 16000))
            tail = handle.read().decode("utf-8", "replace").splitlines()[-50:]
        lines.extend(["", "Ultime operazioni:", *tail])
    return "\n".join(lines)


class NativeSession:
    def __init__(self, app):
        self.app = app
        self.selected = None
        self.pending = {kind: [] for kind in KINDS}

    def add_files(self, kind, paths):
        if kind not in KINDS:
            raise ValueError("Area di caricamento non valida.")
        incoming = []
        for raw in paths:
            path = Path(raw).resolve()
            if not path.is_file() or path.suffix.lower() not in SUPPORTED:
                raise ValueError("File non supportato: " + path.name)
            if not 0 < path.stat().st_size <= MAX_FILE_BYTES:
                raise ValueError("File vuoto o superiore a 512 MB: " + path.name)
            if path not in self.pending[kind] and path not in incoming:
                incoming.append(path)
        self.pending[kind].extend(incoming)

    def save(self, data, action="save"):
        if action not in {"save", "now", "urgent", "night"}:
            raise ValueError("Operazione non valida.")
        previous_active = (read_json(self.app.root(self.selected) / "esame.json").get("attivo", False)
                           if self.selected else False)
        data = dict(data, slug=self.selected, active=previous_active)
        if action != "save":
            data["execution_mode"] = "urgent" if action == "urgent" else "batch"
            data["start_policy"] = "night" if action == "night" else "now"
            if action == "night":
                current = datetime.now().astimezone()
                due = current.replace(hour=0, minute=30, second=0, microsecond=0)
                if due <= current:
                    due += timedelta(days=1)
                data["night_due_at"] = due.isoformat(timespec="seconds")
        self.selected = self.app.save_exam(data)
        for kind, paths in self.pending.items():
            while paths:
                path = paths[0]
                with path.open("rb") as stream:
                    self.app.upload(self.selected, kind, path.name, stream, path.stat().st_size)
                paths.pop(0)
        if action != "save" and not any(row["kind"] == "materiali" for row in self.app.detail(self.selected)["files"]):
            raise ValueError("La materia è salvata. Aggiungi almeno un file nei Materiali del corso prima di generare.")
        self.app.save_exam(dict(data, slug=self.selected, active=previous_active if action == "save" else True))
        if action in {"now", "urgent"}:
            self.app.run(self.selected)
        return self.app.detail(self.selected)

    def remove(self, kind, name):
        root = self.app.root(self.selected)
        if kind not in KINDS:
            raise ValueError("Area non valida.")
        target = (root / "input" / KINDS[kind] / name).resolve()
        if not target.is_relative_to((root / "input" / KINDS[kind]).resolve()) or not target.is_file():
            raise ValueError("File non valido.")
        with edit_lock(root):
            self.app.archive_input(root, target)
            exam = read_json(root / "esame.json")
            exam["revision"] = exam.get("revision", 0) + 1
            exam["attivo"] = False
            write_json(root / "esame.json", exam)

    def upgrade(self):
        root = self.app.root(self.selected)
        with edit_lock(root):
            exam = read_json(root / "esame.json")
            exam["pipeline"].update(self.app.defaults())
            exam["attivo"] = True
            exam["revision"] = exam.get("revision", 0) + 1
            write_json(root / "esame.json", exam)
        return self.app.run(self.selected)

    def resume(self):
        for row in self.app.list_exams():
            if not row["scheduled"]:
                continue
            state = read_json(self.app.root(row["slug"]) / "state/pipeline.json", {})
            exam = read_json(self.app.root(row["slug"]) / "esame.json")
            if exam.get("start_policy") == "now" or any(not batch.get("processed") and batch.get("stage") in {"submitted", "creating", "uncertain"}
                   for batch in state.get("batches", [])):
                try:
                    self.app.run(row["slug"])
                except Exception as error:
                    self.app.errors[row["slug"]] = str(error)


class NativeWindow:
    def __init__(self, root, app):
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText
        self.tk, self.ttk = tk, ttk
        self.root, self.app = root, app
        self.session = NativeSession(app)
        self.events = queue.Queue()
        self.local_busy, self.dirty, self.closing = False, False, False
        self.file_rows, self.pdf_rows = {}, {}
        root.title("Schemi Esami — finestra locale")
        root.geometry("1150x820")
        root.minsize(880, 650)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.name = tk.StringVar()
        self.mode = tk.StringVar(value="completa")
        self.pages = tk.StringVar(value="20")
        self.notice = tk.StringVar(value="Scegli una materia oppure creane una nuova.")
        self.model = tk.StringVar()
        self.progress = tk.StringVar(value="Materiali salvati")
        self.slugs = []
        outer = ttk.Frame(root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)
        sidebar = ttk.Frame(outer, padding=(0, 0, 16, 0))
        sidebar.grid(row=0, column=0, sticky="nsew")
        ttk.Label(sidebar, text="SCHEMI ESAMI", font=("Helvetica", 17, "bold")).pack(anchor="w", pady=(0, 16))
        ttk.Button(sidebar, text="+ Nuova materia", command=self.new_exam).pack(fill="x", pady=(0, 12))
        self.exams = tk.Listbox(sidebar, width=25, exportselection=False, borderwidth=0, font=("Helvetica", 12))
        self.exams.pack(fill="both", expand=True)
        self.exams.bind("<<ListboxSelect>>", self.choose_exam)
        ttk.Button(sidebar, text="Impostazioni del modello", command=self.settings).pack(fill="x", pady=(14, 4))
        ttk.Button(sidebar, text="Imposta chiave OpenAI", command=self.set_api_key).pack(fill="x", pady=(0, 4))
        ttk.Label(sidebar, text="Finestra desktop · V2 nativa").pack(anchor="w", pady=6)
        main = ttk.Frame(outer)
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)
        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Nome della materia").grid(row=0, column=0, sticky="w")
        ttk.Entry(header, textvariable=self.name).grid(row=1, column=0, sticky="ew", padx=(0, 20), pady=5)
        ttk.Radiobutton(header, text="Completa", variable=self.mode, value="completa", command=self.show_mode).grid(row=1, column=1)
        ttk.Radiobutton(header, text="Veloce", variable=self.mode, value="veloce", command=self.show_mode).grid(row=1, column=2, padx=12)
        self.page_frame = ttk.Frame(header)
        self.page_frame.grid(row=0, column=3, rowspan=2)
        ttk.Label(self.page_frame, text="Massimo pagine totali").pack(anchor="w")
        ttk.Spinbox(self.page_frame, from_=1, to=1000, textvariable=self.pages, width=9).pack(anchor="w", pady=5)
        ttk.Label(main, textvariable=self.notice, wraplength=820).grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.tabs = ttk.Notebook(main)
        self.tabs.grid(row=2, column=0, sticky="nsew")
        self.input_tab = ttk.Frame(self.tabs, padding=12)
        self.output_tab = ttk.Frame(self.tabs, padding=12)
        self.status_tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(self.input_tab, text="Materiali e domande")
        self.tabs.add(self.output_tab, text="Schemi e PDF")
        self.tabs.add(self.status_tab, text="Stato e batch")
        inputs = self.input_tab
        inputs.columnconfigure(0, weight=1)
        inputs.rowconfigure(1, weight=2)
        inputs.rowconfigure(3, weight=1)
        toolbar = ttk.Frame(inputs)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for kind, title in [("materiali", "Aggiungi materiali"), ("domande", "File delle domande"), ("programma", "Programma ufficiale")]:
            ttk.Button(toolbar, text=title, command=lambda k=kind: self.pick_files(k)).pack(side="left", padx=(0, 8))
        ttk.Button(toolbar, text="Rimuovi selezionato", command=self.remove_file).pack(side="right")
        self.files = ttk.Treeview(inputs, columns=("area", "name", "state"), show="headings", height=7, selectmode="browse")
        for key, title, width in [("area", "Area", 95), ("name", "File", 370), ("state", "Stato", 95)]:
            self.files.heading(key, text=title)
            self.files.column(key, width=width, stretch=key == "name")
        self.files.grid(row=1, column=0, sticky="nsew")
        ttk.Label(inputs, text="Domande incollate (facoltative; lascia vuoto se non ne hai)").grid(row=2, column=0, sticky="w", pady=(12, 5))
        self.questions = ScrolledText(inputs, height=5, wrap="word", undo=True, font=("Helvetica", 12))
        self.questions.grid(row=3, column=0, sticky="nsew")
        ttk.Label(inputs, text="Indicazioni per gli schemi (facoltative)").grid(row=4, column=0, sticky="w", pady=(10, 5))
        self.instructions = ScrolledText(inputs, height=3, wrap="word", undo=True, font=("Helvetica", 12))
        self.instructions.grid(row=5, column=0, sticky="ew")
        out = self.output_tab
        out.columnconfigure(0, weight=1)
        out.rowconfigure(1, weight=1)
        ttk.Label(out, textvariable=self.progress).grid(row=0, column=0, sticky="w", pady=(0, 12))
        self.outputs = ttk.Treeview(out, columns=("name", "state"), show="headings", selectmode="browse")
        self.outputs.heading("name", text="Argomento / risultato")
        self.outputs.heading("state", text="Stato")
        self.outputs.column("name", width=460)
        self.outputs.column("state", width=140, stretch=False)
        self.outputs.grid(row=1, column=0, sticky="nsew")
        self.outputs.bind("<Double-1>", lambda event: self.open_result())
        controls = ttk.Frame(out)
        controls.grid(row=2, column=0, sticky="ew", pady=12)
        ttk.Button(controls, text="Apri selezionato", command=self.open_result).pack(side="left")
        ttk.Button(controls, text="Apri cartella dei PDF", command=self.open_output_folder).pack(side="left", padx=10)
        self.upgrade_button = ttk.Button(controls, text="Rigenera con il modello predefinito", command=self.upgrade)
        self.upgrade_button.pack(side="right")
        pilot_controls = ttk.Frame(out)
        pilot_controls.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        self.pilot_notice = tk.StringVar(value="")
        ttk.Label(pilot_controls, textvariable=self.pilot_notice, wraplength=650).pack(side="left", fill="x", expand=True)
        self.pilot_approve_button = ttk.Button(pilot_controls, text="Approva il primo schema", command=self.approve_pilot)
        self.pilot_approve_button.pack(side="right", padx=(8, 0))
        self.pilot_feedback_button = ttk.Button(pilot_controls, text="Descrivi modifiche…", command=self.feedback_pilot)
        self.pilot_feedback_button.pack(side="right", padx=(8, 0))
        self.pilot_open_button = ttk.Button(pilot_controls, text="Apri il primo PDF", command=self.open_pilot)
        self.pilot_open_button.pack(side="right", padx=(8, 0))
        monitor = ttk.Frame(self.status_tab)
        monitor.pack(fill="x", pady=(0, 10))
        self.monitor_vars = {key: self.tk.StringVar(value="Scegli una materia") for key in ("local", "server", "finish")}
        for key, title in (("local", "1 · Sul Mac: preparazione"), ("server", "2 · OpenAI: invio e generazione"), ("finish", "3 · Sul Mac: risultati e PDF")):
            ttk.Label(monitor, text=title, font=("Helvetica", 11, "bold")).pack(anchor="w", pady=(5, 0))
            ttk.Label(monitor, textvariable=self.monitor_vars[key], wraplength=970).pack(anchor="w", pady=(0, 3))
        monitor_actions = ttk.Frame(monitor)
        monitor_actions.pack(anchor="e", pady=5)
        ttk.Button(monitor_actions, text="Controlla ora lo stato su OpenAI", command=self.refresh_server).pack(side="left", padx=(0, 8))
        self.rescue_button = ttk.Button(monitor_actions, text="Search and Rescue · recupera schemi", command=self.rescue)
        self.rescue_button.pack(side="left")
        self.status_text = ScrolledText(self.status_tab, wrap="word", font=("Courier", 11), state="disabled")
        self.status_text.pack(fill="both", expand=True)
        footer = ttk.Frame(main)
        footer.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(footer, textvariable=self.model).pack(anchor="w", pady=(0, 6))
        ttk.Label(footer, text="Normale: Batch, costo ridotto e completamento entro 24 ore. Urgente: API diretta a costo standard, recuperabile dopo lo spegnimento; risposta conservata su OpenAI per almeno 30 giorni.", wraplength=840).pack(anchor="w")
        actions = ttk.Frame(footer)
        actions.pack(fill="x", pady=(10, 0))
        self.buttons = [self.rescue_button, self.pilot_feedback_button, self.pilot_approve_button]
        for title, action in [("Salva", "save"), ("Genera stanotte · Normale", "night"), ("Avvia urgente", "urgent"), ("Avvia normale · Batch", "now")]:
            button = ttk.Button(actions, text=title, command=lambda a=action: self.save(a))
            button.pack(side="right", padx=(10, 0))
            self.buttons.append(button)
        for value in (self.name, self.mode, self.pages):
            value.trace_add("write", lambda *args: self.mark_dirty())
        for widget in (self.questions, self.instructions):
            widget.bind("<<Modified>>", lambda event: self.text_modified(event.widget))
        self.show_mode()
        self.refresh()
        if self.slugs:
            self.exams.selection_set(0)
            self.choose_exam()
        root.after(150, self.pump)
        def resumed(result):
            self.notice.set("Scegli una materia oppure creane una nuova.")
            self.refresh()
        self.async_task(self.session.resume, resumed, "Controllo dei lavori già inviati…")

    def mark_dirty(self):
        self.dirty = True

    def text_modified(self, widget):
        if widget.edit_modified():
            self.dirty = True
            widget.edit_modified(False)

    def show_mode(self):
        if self.mode.get() == "veloce":
            self.page_frame.grid()
        else:
            self.page_frame.grid_remove()

    def async_task(self, work, done, message):
        if self.local_busy:
            return
        self.local_busy = True
        self.notice.set(message)
        for button in self.buttons:
            button.state(["disabled"])
        def run():
            try:
                self.events.put((done, work(), None))
            except Exception as error:
                self.events.put((done, None, str(error)))
        threading.Thread(target=run, daemon=True).start()

    def pump(self):
        try:
            while True:
                callback, value, error = self.events.get_nowait()
                self.local_busy = False
                for button in self.buttons:
                    button.state(["!disabled"])
                if error:
                    self.notice.set("Errore: " + error)
                    self.refresh()
                else:
                    callback(value)
        except queue.Empty:
            pass
        if self.closing:
            if not self.local_busy and not any(thread.is_alive() for thread in self.app.workers.values()):
                self.root.destroy()
                return
        self.root.after(150, self.pump)

    def data(self):
        return {"name": self.name.get(), "mode": self.mode.get(), "max_pages": self.pages.get(),
                "questions_text": self.questions.get("1.0", "end-1c"),
                "instructions": self.instructions.get("1.0", "end-1c")}

    def save(self, action="save"):
        data = self.data()
        def done(detail):
            self.dirty = self.data() != data
            self.notice.set({"save": "Materia salvata.", "night": "Materia prenotata per il job delle 00:30. Il Mac deve essere acceso e sveglio.",
                             "now": "Preparazione normale avviata. L'ID del batch confermerà l'accettazione su OpenAI.",
                             "urgent": "Preparazione urgente avviata. L'ID della risposta confermerà l'accettazione su OpenAI."}[action])
            self.refresh()
            if action in {"now", "urgent"}:
                self.tabs.select(self.status_tab)
        self.async_task(lambda: self.session.save(data, action), done, "Salvataggio e caricamento dei file…")

    def permit_switch(self):
        if self.local_busy:
            return False
        if not self.dirty and not any(self.session.pending.values()):
            return True
        from tkinter import messagebox
        return messagebox.askyesno("Modifiche non salvate", "Cambiare materia e lasciare le modifiche non salvate?", parent=self.root)

    def new_exam(self):
        if not self.permit_switch():
            return
        self.session.selected = None
        self.session.pending = {kind: [] for kind in KINDS}
        self.name.set("")
        self.mode.set("completa")
        self.pages.set("20")
        for field in (self.questions, self.instructions):
            field.delete("1.0", "end")
            field.edit_modified(False)
        self.dirty = False
        self.show_mode()
        self.notice.set("Inserisci la nuova materia e aggiungi i materiali.")
        self.refresh()

    def choose_exam(self, event=None):
        selection = self.exams.curselection()
        if not selection:
            return
        slug = self.slugs[selection[0]]
        if slug == self.session.selected or not self.permit_switch():
            return
        try:
            detail = self.app.detail(slug)
            self.session.selected = slug
            self.session.pending = {kind: [] for kind in KINDS}
            self.name.set(detail["exam"]["nome"])
            self.mode.set(detail["exam"].get("modalita", "completa"))
            self.pages.set(str(detail["exam"].get("max_pagine") or 20))
            for field, text in [(self.questions, detail["questions_text"]), (self.instructions, detail["exam"].get("istruzioni", ""))]:
                field.delete("1.0", "end")
                field.insert("1.0", text)
                field.edit_modified(False)
            self.dirty = False
            self.show_mode()
            self.notice.set("Materia aperta.")
            self.refresh()
        except Exception as error:
            self.notice.set("Errore: " + str(error))

    def pick_files(self, kind):
        if self.local_busy:
            return
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(parent=self.root, title="Seleziona i file", filetypes=[("File supportati", " ".join("*"+suffix for suffix in sorted(SUPPORTED))), ("Tutti i file", "*")])
        try:
            self.session.add_files(kind, paths)
            if paths:
                self.dirty = True
            self.refresh()
        except Exception as error:
            self.notice.set("Errore: " + str(error))

    def remove_file(self):
        if self.local_busy:
            return
        selection = self.files.selection()
        if not selection:
            return
        kind, name, pending = self.file_rows[selection[0]]
        if pending:
            self.session.pending[kind].remove(name)
            self.refresh()
            return
        def done(value):
            if kind == "domande" and name == "domande_incollate.txt":
                self.questions.delete("1.0", "end")
            self.notice.set("File rimosso dai materiali correnti. Genera per aggiornare i risultati.")
            self.refresh()
        self.async_task(lambda: self.session.remove(kind, name), done, "Aggiornamento dei materiali…")

    def refresh(self):
        try:
            rows = self.app.list_exams()
            self.slugs = [row["slug"] for row in rows]
            self.exams.delete(0, "end")
            for index, row in enumerate(rows):
                text = row["name"] + (" · in corso" if row["busy"] else "")
                self.exams.insert("end", text)
                if row["slug"] == self.session.selected:
                    self.exams.selection_set(index)
            detail = self.app.detail(self.session.selected) if self.session.selected else None
            status = overview(self.app.root(self.session.selected), detail["busy"]) if detail else None
            for key, variable in self.monitor_vars.items():
                variable.set(status[key] if status else "Scegli una materia")
            profile = detail["exam"].get("pipeline", {}) if detail else self.app.defaults()
            self.model.set("Modello: " + str(profile.get("model", "—")))
            self.files.delete(*self.files.get_children())
            self.file_rows = {}
            for file in (detail or {}).get("files", []):
                item = self.files.insert("", "end", values=(file["kind"], file["name"], "Salvato"))
                self.file_rows[item] = (file["kind"], file["name"], False)
            for kind, paths in self.session.pending.items():
                for path in paths:
                    item = self.files.insert("", "end", values=(kind, path.name, "Da caricare"))
                    self.file_rows[item] = (kind, path, True)
            self.outputs.delete(*self.outputs.get_children())
            self.pdf_rows = {}
            report = (detail or {}).get("report", {})
            review = report.get("pilot_review")
            self.pilot_review = review
            if review:
                labels = {"generating": "Primo schema in preparazione urgente: gli altri aspettano il tuo controllo.",
                          "ready_for_review": "Primo PDF pronto: aprilo, descrivi le correzioni oppure approvalo.",
                          "approved": "Primo schema approvato: generazione degli altri schemi avviata."}
                self.pilot_notice.set(labels.get(review["status"], ""))
                ready = review["status"] == "ready_for_review"
                self.pilot_open_button.state(["!disabled"] if review.get("pdf") else ["disabled"])
                self.pilot_feedback_button.state(["!disabled"] if ready else ["disabled"])
                self.pilot_approve_button.state(["!disabled"] if ready else ["disabled"])
            else:
                self.pilot_notice.set("")
                for button in (self.pilot_open_button, self.pilot_feedback_button, self.pilot_approve_button):
                    button.state(["disabled"])
            output = report.get("exports", {})
            phase = report.get("input_status") or "Materiali salvati"
            self.progress.set(str(phase) + (" · " + str(output["pages"]) + " pagine" if output.get("pages") else ""))
            for job in report.get("planning", []):
                self.outputs.insert("", "end", values=("Lettura dei materiali" if job.get("kind") == "analysis" else "Organizzazione degli schemi", job["status"]))
            for row in report.get("rows", []):
                item = self.outputs.insert("", "end", values=(str(row["numero"])+" - "+row["title"], row["status"]))
                if row["status"] == "PDF_OK" and row.get("public_pdf"):
                    self.pdf_rows[item] = row["public_pdf"]
            for key, title in [("combined", "PDF unico — tutti gli schemi"), ("archive", "ZIP — tutti i PDF")]:
                if output.get(key):
                    item = self.outputs.insert("", "end", values=(title, "Pronto"))
                    self.pdf_rows[item] = output[key]
            self.upgrade_button.state(["!disabled"] if detail and any(profile.get(key) != self.app.defaults().get(key) for key in MODEL_KEYS) else ["disabled"])
            text = diagnostics(self.app, self.session.selected) if detail else "Scegli una materia per vedere stato, errori e identificativi dei batch."
            plan = (detail or {}).get("plan")
            if plan:
                text += "\n\nOrganizzazione e copertura delle fonti:\n" + json.dumps(plan, ensure_ascii=False, indent=2)
            self.status_text.configure(state="normal")
            self.status_text.delete("1.0", "end")
            self.status_text.insert("1.0", text)
            self.status_text.configure(state="disabled")
        except Exception as error:
            self.notice.set("Errore: " + str(error))

    def periodic_refresh(self):
        if not self.local_busy:
            self.refresh()
        if not self.closing:
            self.root.after(3000, self.periodic_refresh)

    def refresh_server(self):
        if not self.session.selected:
            self.notice.set("Scegli una materia prima di controllare lo stato.")
            return
        slug = self.session.selected
        def done(value):
            self.notice.set(f"Controllati {value['checked']} lavori su OpenAI." + (" Errori: " + "; ".join(value["errors"]) if value["errors"] else ""))
            self.refresh()
        self.async_task(lambda: self.app.refresh_server(slug), done, "Controllo dello stato su OpenAI…")

    def rescue(self):
        if not self.session.selected:
            self.notice.set("Scegli una materia prima di avviare Search and Rescue.")
            return
        slug = self.session.selected
        def done(result):
            if result.get("planning"):
                issue = result["planning"][0]
                self.notice.set("Search and Rescue: piano non completato (" + issue["detail"][:180] +
                                "). Non ci sono ancora schemi PDF da recuperare. Vedi Stato e batch.")
            else:
                self.notice.set(f"Search and Rescue: {len(result['recovered_pdfs'])} PDF recuperati, "
                                f"{result['checked_batches']} lavori OpenAI controllati. "
                                "Vedi il dettaglio in Stato e batch.")
            self.refresh()
        self.async_task(lambda: self.app.rescue(slug), done, "Ricerca dei risultati e recupero dei PDF…")

    def open_path(self, path):
        if not path.exists():
            self.notice.set("Il file non è ancora disponibile.")
            return
        subprocess.Popen((["open"] if sys.platform == "darwin" else ["xdg-open"]) + [str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def open_result(self):
        selection = self.outputs.selection()
        if not selection or selection[0] not in self.pdf_rows:
            self.notice.set("Seleziona un PDF pronto o lo ZIP dei risultati.")
            return
        root = self.app.root(self.session.selected)
        path = (root / self.pdf_rows[selection[0]]).resolve()
        if not path.is_relative_to((root / "output/pdf").resolve()):
            raise ValueError("Percorso del risultato non valido.")
        self.open_path(path)

    def open_output_folder(self):
        if self.session.selected:
            self.open_path(self.app.root(self.session.selected) / "output/pdf")

    def open_pilot(self):
        review = getattr(self, "pilot_review", None) or {}
        if not self.session.selected or not review.get("pdf"):
            return
        root = self.app.root(self.session.selected)
        path = (root / review["pdf"]).resolve()
        if not path.is_relative_to((root / "output/pdf").resolve()):
            raise ValueError("Percorso del PDF pilota non valido.")
        self.open_path(path)

    def approve_pilot(self):
        if not self.session.selected or (self.pilot_review or {}).get("status") != "ready_for_review":
            return
        slug = self.session.selected
        def done(value):
            self.notice.set("Primo schema approvato. Gli altri schemi possono ora partire.")
            self.refresh()
        self.async_task(lambda: self.app.review_pilot(slug, "approve"), done, "Sblocco degli altri schemi…")

    def feedback_pilot(self):
        if not self.session.selected or (self.pilot_review or {}).get("status") != "ready_for_review":
            return
        from tkinter.scrolledtext import ScrolledText
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Modifiche al primo schema")
        dialog.transient(self.root)
        dialog.geometry("650x420")
        frame = self.ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        self.ttk.Label(frame, text="Descrivi cosa correggere nel contenuto, nella grafica o nell'impaginazione.\nVerrà generato un nuovo primo PDF; gli altri schemi resteranno in attesa.", wraplength=600).pack(anchor="w", pady=(0, 10))
        field = ScrolledText(frame, wrap="word", undo=True, font=("Helvetica", 12))
        field.pack(fill="both", expand=True)
        field.focus_set()
        info = self.tk.StringVar(value="")
        self.ttk.Label(frame, textvariable=info).pack(anchor="w", pady=(8, 0))
        def send():
            feedback = field.get("1.0", "end-1c").strip()
            if len(feedback) < 5:
                info.set("Descrivi almeno una modifica concreta.")
                return
            if len(feedback) > 12000:
                info.set("Massimo 12000 caratteri.")
                return
            dialog.destroy()
            slug = self.session.selected
            def done(value):
                self.notice.set("Correzioni inviate. Nuovo primo schema in preparazione urgente.")
                self.refresh()
            self.async_task(lambda: self.app.review_pilot(slug, "revise", feedback), done, "Preparazione della nuova versione…")
        self.ttk.Button(frame, text="Rigenera il primo schema", command=send).pack(anchor="e", pady=(10, 0))

    def upgrade(self):
        if not self.session.selected or self.local_busy:
            return
        self.async_task(self.session.upgrade, lambda value: self.refresh(), "Rigenerazione con il modello predefinito…")

    def settings(self):
        from tkinter import ttk
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Impostazioni del modello")
        dialog.transient(self.root)
        dialog.geometry("570x490")
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Il profilo vale per le nuove materie.").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        defaults = self.app.defaults()
        fields = {}
        for row, key, label in [(1, "model", "Identificativo API"), (3, "reasoning_effort", "Ragionamento"), (4, "max_output_tokens", "Massimo token"), (5, "retry_max_output_tokens", "Token per il retry")]:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=7)
            fields[key] = self.tk.StringVar(value=str(defaults.get(key) or ""))
            widget = ttk.Combobox(frame, textvariable=fields[key]) if key == "model" else ttk.Entry(frame, textvariable=fields[key])
            widget.grid(row=row, column=1, sticky="ew", pady=7)
            if key == "model":
                model_box = widget
        images = self.tk.BooleanVar(value=defaults.get("image_mode") == "data-url")
        structured = self.tk.BooleanVar(value=defaults.get("structured_outputs", True))
        ttk.Checkbutton(frame, text="Il modello supporta immagini", variable=images).grid(row=6, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Checkbutton(frame, text="Il modello supporta JSON strutturato", variable=structured).grid(row=7, column=0, columnspan=2, sticky="w", pady=5)
        info = self.tk.StringVar(value="Verifica nella documentazione Batch, Responses e parametri supportati.\nLa disponibilità nell'elenco non certifica la competenza del modello.")
        ttk.Label(frame, textvariable=info, wraplength=515).grid(row=8, column=0, columnspan=2, sticky="w", pady=15)
        def refresh_models():
            def work():
                return self.app.api_factory(read_json(self.app.project / "config/pipeline.json")).list_models()
            def done(value):
                if not dialog.winfo_exists():
                    return
                model_box.configure(values=sorted(model["id"] for model in value.get("data", [])))
                info.set("Elenco aggiornato. Scegli un modello compatibile con questa pipeline.")
            self.async_task(work, done, "Lettura dei modelli disponibili…")
        ttk.Button(frame, text="Aggiorna elenco modelli", command=refresh_models).grid(row=2, column=1, sticky="w")
        def save():
            try:
                raw = {key: value.get() for key, value in fields.items()}
                raw.update(image_mode="data-url" if images.get() else "manifest", structured_outputs=structured.get())
                save_default_profile(self.app.project, raw)
                dialog.destroy()
                self.notice.set("Modello predefinito salvato. Le materie esistenti conservano il proprio profilo.")
                self.refresh()
            except Exception as error:
                info.set("Errore: " + str(error))
        ttk.Button(frame, text="Salva modello predefinito", command=save).grid(row=9, column=1, sticky="e")

    def close(self):
        if self.local_busy:
            self.notice.set("Attendi la fine dell'operazione prima di chiudere.")
            return
        if not self.permit_switch():
            return
        self.closing = True
        self.app.stop.set()
        for wake in self.app.wakes.values():
            wake.set()
        self.notice.set("Chiusura: attendo il salvataggio della lavorazione corrente…")

    def set_api_key(self):
        from tkinter import simpledialog
        key = simpledialog.askstring("Chiave OpenAI", "Incolla la chiave API. Sarà salvata nel Portachiavi del Mac.",
                                     show="*", parent=self.root)
        if key is None:
            return
        self.async_task(lambda: save_api_key(key),
                        lambda _: self.notice.set("Chiave OpenAI salvata nel Portachiavi."),
                        "Salvataggio della chiave nel Portachiavi…")


def main():
    import tkinter as tk
    project = PROJECT
    (project / "state").mkdir(exist_ok=True)
    with (project / "state/native-window.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            from tkinter import messagebox
            hidden = tk.Tk()
            hidden.withdraw()
            messagebox.showinfo("Schemi Esami", "Una finestra di questo programma è già aperta.", parent=hidden)
            hidden.destroy()
            return
        root = tk.Tk()
        window = NativeWindow(root, App(project))
        root.after(3000, window.periodic_refresh)
        root.mainloop()
