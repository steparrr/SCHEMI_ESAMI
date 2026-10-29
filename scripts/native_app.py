"""Finestra desktop: accesso diretto al motore, senza HTTP o browser."""
from __future__ import annotations

import fcntl
import base64
import json
import queue
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from common import PROJECT, read_json, write_json
from coverage_matrix import compare as compare_coverage
from control_room import format_snapshot, snapshot
from diagnostic_report import build_sections, export as export_diagnostic, preview as preview_diagnostic
from local_app import App, KINDS, MAX_FILE_BYTES, edit_lock, save_api_key
from local_recovery import recover_local
from materials_view import material_detail
from medical_illustrator import (create_manual as manual_illustration, export_illustrated_pdf, list_opportunities,
                                 plan as plan_illustrations, preview_svg, revalidate_edited_svg, REVIEW_FIELDS,
                                 set_disabled as disable_illustrations, transition as transition_illustration,
                                 update_proposal as edit_illustration_proposal)
from ingest import SUPPORTED
from model_settings import MODEL_KEYS, save_default_profile
from monitor import batch_lines, overview
from project_wizard import validate_wizard
from project_actions import duplicate_settings, set_archived, set_favorite
from projects_view import arrange_cards, pdf_versions, project_cards
from visual_qc import export_visual_report, inspect_visual, load_visual_reviews, mark_visual_review


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
        if self.selected and action != "save" and read_json(self.app.root(self.selected) / "esame.json").get("archived"):
            raise ValueError("La materia è archiviata. Riattivala prima di avviare la produzione.")
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
            if exam.get("archived"):
                raise ValueError("Riattiva la materia prima di rigenerarla.")
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
        ttk.Button(sidebar, text="+ Nuova materia guidata", command=self.open_wizard).pack(fill="x", pady=(0, 12))
        self.home_search = tk.StringVar()
        ttk.Entry(sidebar, textvariable=self.home_search).pack(fill="x", pady=(0, 6))
        ttk.Label(sidebar, text="Cerca materie").pack(anchor="w", pady=(0, 7))
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
        self.home_tab = ttk.Frame(self.tabs, padding=12)
        self.input_tab = ttk.Frame(self.tabs, padding=12)
        self.output_tab = ttk.Frame(self.tabs, padding=12)
        self.status_tab = ttk.Frame(self.tabs, padding=12)
        self.control_tab = ttk.Frame(self.tabs, padding=12)
        self.illustrator_tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(self.home_tab, text="Progetti")
        self.tabs.add(self.input_tab, text="Materiali e domande")
        self.tabs.add(self.output_tab, text="Schemi e PDF")
        self.tabs.add(self.status_tab, text="Stato e batch")
        self.tabs.add(self.control_tab, text="Control Room")
        self.tabs.add(self.illustrator_tab, text="Medical Illustrator")
        home_toolbar = ttk.Frame(self.home_tab)
        home_toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(home_toolbar, text="Materie locali · PDF e stati verificati sui file salvati").pack(anchor="w")
        home_actions = ttk.Frame(home_toolbar)
        home_actions.pack(fill="x", pady=(6, 0))
        ttk.Button(home_actions, text="Apri", command=self.open_home_project).pack(side="right")
        ttk.Button(home_actions, text="Apri Control Room", command=lambda: self.open_home_project(True)).pack(side="right", padx=8)
        ttk.Button(home_actions, text="Cartella PDF", command=self.open_home_pdf_folder).pack(side="right", padx=8)
        ttk.Button(home_actions, text="Archivia / Riattiva", command=self.toggle_home_archive).pack(side="right", padx=8)
        ttk.Button(home_actions, text="Duplica impostazioni", command=self.duplicate_home_settings).pack(side="right", padx=8)
        ttk.Button(home_actions, text="★ Preferito", command=self.toggle_home_favorite).pack(side="right", padx=8)
        home_filters = ttk.Frame(home_toolbar)
        home_filters.pack(fill="x", pady=(7, 0))
        self.home_visibility = tk.StringVar(value="Attive")
        self.home_order = tk.StringVar(value="Nome")
        ttk.Label(home_filters, text="Mostra").pack(side="left")
        visible = ttk.Combobox(home_filters, textvariable=self.home_visibility,
                               values=("Attive", "Tutte", "Archiviate", "Preferite"), state="readonly", width=13)
        visible.pack(side="left", padx=(6, 18))
        visible.bind("<<ComboboxSelected>>", lambda event: self.refresh())
        ttk.Label(home_filters, text="Ordina").pack(side="left")
        ordered = ttk.Combobox(home_filters, textvariable=self.home_order,
                               values=("Nome", "Data esame", "PDF pronti"), state="readonly", width=15)
        ordered.pack(side="left", padx=6)
        ordered.bind("<<ComboboxSelected>>", lambda event: self.refresh())
        ttk.Button(home_filters, text="Versioni PDF", command=self.show_home_pdf_versions).pack(side="right")
        self.project_rows = ttk.Treeview(self.home_tab, columns=("name", "pdf", "phase", "remote"), show="headings", selectmode="browse")
        for key, title, width in (("name", "Materia", 280), ("pdf", "PDF pronti", 95),
                                  ("phase", "Situazione", 290), ("remote", "Ultima verifica OpenAI", 210)):
            self.project_rows.heading(key, text=title)
            self.project_rows.column(key, width=width, stretch=key in {"name", "phase"})
        self.project_rows.pack(fill="both", expand=True)
        self.project_rows.bind("<Double-1>", lambda event: self.open_home_project())
        inputs = self.input_tab
        inputs.columnconfigure(0, weight=1)
        inputs.rowconfigure(1, weight=2)
        inputs.rowconfigure(3, weight=1)
        toolbar = ttk.Frame(inputs)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        file_actions = ttk.Frame(toolbar)
        file_actions.pack(fill="x")
        for kind, title in [("materiali", "Aggiungi materiali"), ("domande", "File delle domande"), ("programma", "Programma ufficiale")]:
            ttk.Button(file_actions, text=title, command=lambda k=kind: self.pick_files(k)).pack(side="left", padx=(0, 8))
        inspect_actions = ttk.Frame(toolbar)
        inspect_actions.pack(fill="x", pady=(5, 0))
        ttk.Button(inspect_actions, text="Confronta programma", command=self.show_coverage).pack(side="left")
        ttk.Button(inspect_actions, text="Rimuovi selezionato", command=self.remove_file).pack(side="right")
        ttk.Button(inspect_actions, text="Anteprima selezionato", command=self.preview_material).pack(side="right", padx=8)
        self.files = ttk.Treeview(inputs, columns=("area", "name", "size", "state"), show="headings", height=7, selectmode="browse")
        for key, title, width in [("area", "Area", 95), ("name", "File", 320), ("size", "Byte", 95), ("state", "Stato", 160)]:
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
        ttk.Button(controls, text="Controllo visivo PDF", command=self.visual_check).pack(side="left", padx=10)
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
        control_actions = ttk.Frame(self.control_tab)
        control_actions.pack(fill="x", pady=(0, 8))
        ttk.Label(control_actions, text="Dati locali osservati · la lettura non invia richieste API").pack(side="left")
        ttk.Button(control_actions, text="Aggiorna dati locali", command=self.refresh).pack(side="right")
        ttk.Button(control_actions, text="Anteprima report diagnostico", command=self.diagnostic_preview).pack(side="right", padx=8)
        ttk.Button(control_actions, text="Recupera solo PDF locali", command=self.recover_local_pdfs).pack(side="right", padx=8)
        self.control_tabs = ttk.Notebook(self.control_tab)
        self.control_tabs.pack(fill="both", expand=True)
        self.control_overview = ttk.Frame(self.control_tabs)
        self.control_jobs = ttk.Frame(self.control_tabs)
        self.control_batches = ttk.Frame(self.control_tabs)
        self.control_performance = ttk.Frame(self.control_tabs)
        for page, label in ((self.control_overview, "Overview"), (self.control_jobs, "Schemi"),
                            (self.control_batches, "Batch"), (self.control_performance, "Prestazioni")):
            self.control_tabs.add(page, text=label)
        self.control_text = ScrolledText(self.control_overview, wrap="word", font=("Courier", 11), state="disabled")
        self.control_text.pack(fill="both", expand=True)
        self.control_job_rows = ttk.Treeview(self.control_jobs, columns=("number", "title", "hash", "status", "retry"), show="headings", selectmode="browse")
        for key, label, width in (("number", "N°", 55), ("title", "Titolo", 310), ("hash", "Hash", 110),
                                  ("status", "Stato", 160), ("retry", "Retry", 60)):
            self.control_job_rows.heading(key, text=label)
            self.control_job_rows.column(key, width=width, stretch=key == "title")
        self.control_job_rows.pack(fill="both", expand=True)
        self.control_job_rows.bind("<<TreeviewSelect>>", self.show_control_job)
        self.control_job_detail = ScrolledText(self.control_jobs, height=8, wrap="word", font=("Courier", 10), state="disabled")
        self.control_job_detail.pack(fill="x", pady=(8, 0))
        self.control_batch_rows = ttk.Treeview(self.control_batches, columns=("mode", "id", "local", "server", "checked"), show="headings")
        for key, label, width in (("mode", "Tipo", 75), ("id", "ID remoto", 220), ("local", "Stato locale", 140),
                                  ("server", "Stato osservato", 145), ("checked", "Ultimo controllo", 190)):
            self.control_batch_rows.heading(key, text=label)
            self.control_batch_rows.column(key, width=width, stretch=key == "id")
        self.control_batch_rows.pack(fill="both", expand=True)
        self.control_performance_rows = ttk.Treeview(self.control_performance, columns=("stage", "success", "fail", "median", "p90", "p95", "max"), show="headings")
        for key, label, width in (("stage", "Fase locale", 210), ("success", "Riuscite", 80),
                                  ("fail", "Errori", 70), ("median", "Mediana ms", 105),
                                  ("p90", "p90 ms", 95), ("p95", "p95 ms", 95),
                                  ("max", "Massimo ms", 105)):
            self.control_performance_rows.heading(key, text=label)
            self.control_performance_rows.column(key, width=width, stretch=key == "stage")
        self.control_performance_rows.pack(fill="both", expand=True)
        illustrator_toolbar = ttk.Frame(self.illustrator_tab)
        illustrator_toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(illustrator_toolbar, text="Cerca tavole utili", command=self.plan_illustrations).pack(side="left")
        ttk.Button(illustrator_toolbar, text="Aggiungi tavola manuale", command=self.add_manual_illustration).pack(side="left", padx=8)
        ttk.Button(illustrator_toolbar, text="Disattiva/riattiva proposte", command=self.toggle_illustrations).pack(side="left", padx=8)
        self.illustrator_notice = tk.StringVar(value="Proposte locali, senza API. Ogni tavola richiede revisione.")
        ttk.Label(self.illustrator_tab, textvariable=self.illustrator_notice, wraplength=900).pack(anchor="w", pady=(0, 8))
        self.illustrations = ttk.Treeview(self.illustrator_tab, columns=("schema", "concept", "state"), show="headings", selectmode="browse")
        for key, title, width in (("schema", "Schema", 85), ("concept", "Tavola proposta", 510), ("state", "Stato", 200)):
            self.illustrations.heading(key, text=title)
            self.illustrations.column(key, width=width, stretch=key == "concept")
        self.illustrations.pack(fill="both", expand=True)
        self.illustrations.bind("<<TreeviewSelect>>", self.show_illustration_details)
        self.illustration_detail = ScrolledText(self.illustrator_tab, height=8, wrap="word", font=("Helvetica", 11), state="disabled")
        self.illustration_detail.pack(fill="x", pady=(8, 0))
        illustrator_actions = ttk.Frame(self.illustrator_tab)
        illustrator_actions.pack(fill="x", pady=(8, 0))
        ttk.Button(illustrator_actions, text="Accetta e crea bozza SVG", command=lambda: self.illustration_action("accepted")).pack(side="left")
        ttk.Button(illustrator_actions, text="Scarta", command=lambda: self.illustration_action("rejected")).pack(side="left", padx=8)
        ttk.Button(illustrator_actions, text="Modifica proposta", command=self.edit_illustration).pack(side="left", padx=(0, 8))
        ttk.Button(illustrator_actions, text="Apri bozza", command=self.open_illustration).pack(side="left")
        ttk.Button(illustrator_actions, text="Valida SVG modificato", command=self.revalidate_illustration).pack(side="left", padx=8)
        ttk.Button(illustrator_actions, text="Segna revisionata", command=lambda: self.illustration_action("approved")).pack(side="right")
        ttk.Button(illustrator_actions, text="Esporta PDF con tavola", command=self.export_illustration).pack(side="right", padx=8)
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
        self.home_search.trace_add("write", lambda *args: self.refresh())
        self.refresh()
        if self.slugs:
            self.exams.selection_set(0)
            self.choose_exam()
        self.tabs.select(self.home_tab)
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

    def save(self, action="save", extra=None, illustrator_enabled=None):
        visible_data = self.data()
        data = dict(visible_data, **(extra or {}))
        def done(detail):
            preference_error = None
            if illustrator_enabled is not None:
                try:
                    disable_illustrations(self.app.root(detail["slug"]), not illustrator_enabled)
                except (OSError, ValueError) as error:
                    preference_error = str(error)
            self.dirty = self.data() != visible_data
            self.notice.set("Materia salvata, ma preferenza Illustrator non registrata: " + preference_error if preference_error else
                            {"save": "Materia salvata.", "night": "Materia prenotata per il job delle 00:30. Il Mac deve essere acceso e sveglio.",
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
            return False
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
        return True

    def open_wizard(self):
        from tkinter import filedialog, messagebox
        from tkinter.scrolledtext import ScrolledText
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Nuova materia · procedura guidata")
        dialog.geometry("810x630")
        dialog.transient(self.root)
        dialog.grab_set()
        values = {"name": self.tk.StringVar(), "short_title": self.tk.StringVar(),
                  "exam_date": self.tk.StringVar(), "mode": self.tk.StringVar(value="completa"),
                  "max_pages": self.tk.StringVar(value="100"), "execution": self.tk.StringVar(value="batch"),
                  "illustrator_enabled": self.tk.BooleanVar(value=False)}
        pending = {kind: [] for kind in KINDS}
        notebook = self.ttk.Notebook(dialog)
        notebook.pack(fill="both", expand=True, padx=14, pady=12)
        pages = [self.ttk.Frame(notebook, padding=18) for _ in range(6)]
        for page, title in zip(pages, ("1 Identità", "2 Obiettivo", "3 Materiali", "4 Produzione", "5 Grafica", "6 Riepilogo")):
            notebook.add(page, text=title)
        identity = pages[0]
        identity.columnconfigure(1, weight=1)
        for index, (label, key) in enumerate((("Nome della materia", "name"), ("Titolo breve (facoltativo)", "short_title"),
                                               ("Data esame AAAA-MM-GG (facoltativa)", "exam_date"))):
            self.ttk.Label(identity, text=label).grid(row=index, column=0, sticky="w", pady=10)
            self.ttk.Entry(identity, textvariable=values[key]).grid(row=index, column=1, sticky="ew", padx=12, pady=10)
        objective = pages[1]
        self.ttk.Label(objective, text="Come vuoi studiare questa materia?").pack(anchor="w", pady=(0, 10))
        self.ttk.Radiobutton(objective, text="Completa", variable=values["mode"], value="completa").pack(anchor="w")
        self.ttk.Radiobutton(objective, text="Veloce · limite di pagine totali", variable=values["mode"], value="veloce").pack(anchor="w", pady=7)
        self.ttk.Spinbox(objective, from_=1, to=1000, textvariable=values["max_pages"], width=9).pack(anchor="w", padx=25)
        self.ttk.Label(objective, text="Il limite non garantisce copertura se le fonti sono insufficienti.").pack(anchor="w", pady=(8, 14))
        self.ttk.Label(objective, text="Indicazioni per gli schemi (facoltative)").pack(anchor="w")
        instructions = ScrolledText(objective, height=7, wrap="word")
        instructions.pack(fill="both", expand=True, pady=5)
        materials = pages[2]
        self.ttk.Label(materials, text="Seleziona più file. Puoi anche salvare una bozza e aggiungerli dopo.").pack(anchor="w", pady=(0, 12))
        counts = {}
        for kind, title in (("materiali", "Sbobine e appunti"), ("domande", "Domande d'esame"), ("programma", "Programma ufficiale")):
            bar = self.ttk.Frame(materials)
            bar.pack(fill="x", pady=8)
            counter = self.tk.StringVar(value="0 file")
            counts[kind] = counter
            def choose(k=kind, counter_var=counter):
                paths = filedialog.askopenfilenames(parent=dialog, title="Seleziona " + k,
                    filetypes=[("File supportati", " ".join("*" + suffix for suffix in sorted(SUPPORTED))), ("Tutti i file", "*")])
                for raw in paths:
                    path = Path(raw).resolve()
                    if path not in pending[k]:
                        pending[k].append(path)
                counter_var.set(f"{len(pending[k])} file")
            self.ttk.Button(bar, text="Aggiungi " + title.lower(), command=choose).pack(side="left")
            self.ttk.Label(bar, textvariable=counter).pack(side="left", padx=16)
            def clear(k=kind, counter_var=counter):
                pending[k].clear()
                counter_var.set("0 file")
            self.ttk.Button(bar, text="Svuota", command=clear).pack(side="right")
        production = pages[3]
        self.ttk.Label(production, text="Scegli quando avviare. Salva bozza non invia nulla.").pack(anchor="w", pady=(0, 12))
        for code, label in (("batch", "Normale · Batch OpenAI"), ("urgent", "Urgente · OpenAI a costo standard"),
                            ("night", "Notturna · Batch alle 00:30, Mac acceso")):
            self.ttk.Radiobutton(production, text=label, variable=values["execution"], value=code).pack(anchor="w", pady=7)
        self.ttk.Label(production, text="Il primo PDF resta in attesa della tua approvazione. Il prezzo API non è stimato qui.",
                       wraplength=690).pack(anchor="w", pady=22)
        graphics = pages[4]
        self.ttk.Checkbutton(graphics, text="Abilita proposte Medical Illustrator per questa materia",
                             variable=values["illustrator_enabled"]).pack(anchor="w", pady=(0, 16))
        self.ttk.Label(graphics, text="Le bozze SVG sono schematiche, locali e gratuite. Si generano solo quando le chiedi; "
                       "richiedono revisione umana e non entrano automaticamente nel PDF.", wraplength=690).pack(anchor="w")
        self.ttk.Label(graphics, text="Le immagini già presenti nelle fonti continuano a seguire il flusso esistente.",
                       wraplength=690).pack(anchor="w", pady=12)
        review = pages[5]
        review_text = ScrolledText(review, wrap="word", state="disabled", height=15)
        review_text.pack(fill="both", expand=True)
        def show_review(_event=None):
            if notebook.index(notebook.select()) != 5:
                return
            summary = (f"Materia: {values['name'].get().strip() or 'da inserire'}\n"
                       f"Titolo breve: {values['short_title'].get().strip() or '—'}\n"
                       f"Data esame: {values['exam_date'].get().strip() or '—'}\n"
                       f"Obiettivo: {values['mode'].get()}"
                       + (f" · massimo {values['max_pages'].get()} pagine" if values['mode'].get() == 'veloce' else "")
                       + "\n" + "\n".join(f"{kind}: {len(paths)} file" for kind, paths in pending.items())
                       + f"\nProduzione se avviata: {values['execution'].get()}\n"
                       f"Medical Illustrator: {'abilitato' if values['illustrator_enabled'].get() else 'disattivato'}\n"
                       "\nSalva bozza non chiama OpenAI. Avvia usa soltanto l'API OpenAI esistente.")
            review_text.configure(state="normal")
            review_text.delete("1.0", "end")
            review_text.insert("1.0", summary)
            review_text.configure(state="disabled")
        notebook.bind("<<NotebookTabChanged>>", show_review)
        buttons = self.ttk.Frame(dialog, padding=(14, 0, 14, 14))
        buttons.pack(fill="x")
        self.ttk.Button(buttons, text="Indietro", command=lambda: notebook.select(max(0, notebook.index(notebook.select()) - 1))).pack(side="left")
        self.ttk.Button(buttons, text="Avanti", command=lambda: notebook.select(min(5, notebook.index(notebook.select()) + 1))).pack(side="left", padx=8)
        def finish(start):
            if start and notebook.index(notebook.select()) != 5:
                notebook.select(5)
                return
            raw = {key: value.get() for key, value in values.items()}
            raw["instructions"] = instructions.get("1.0", "end-1c")
            try:
                planned, files = validate_wizard(raw, pending, start=start)
            except ValueError as error:
                messagebox.showerror("Controlla il progetto", str(error), parent=dialog)
                return
            dialog.grab_release()
            if not self.new_exam():
                dialog.grab_set()
                return
            self.name.set(planned["name"])
            self.mode.set(planned["mode"])
            self.pages.set(str(planned["max_pages"] or 100))
            self.instructions.insert("1.0", planned["instructions"])
            self.show_mode()
            for kind, paths in files.items():
                self.session.add_files(kind, paths)
            self.dirty = True
            dialog.destroy()
            action = ("save" if not start else "night" if planned["execution"] == "night"
                      else "urgent" if planned["execution"] == "urgent" else "now")
            self.save(action, extra={"short_title": planned["short_title"], "exam_date": planned["exam_date"]},
                      illustrator_enabled=planned["illustrator_enabled"])
        self.ttk.Button(buttons, text="Avvia dopo il salvataggio", command=lambda: finish(True)).pack(side="right")
        self.ttk.Button(buttons, text="Salva bozza", command=lambda: finish(False)).pack(side="right", padx=8)

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

    def open_home_project(self, control=False):
        selection = self.project_rows.selection()
        if not selection:
            self.notice.set("Seleziona una materia nella lista Progetti.")
            return
        slug = selection[0]
        if slug != self.session.selected:
            if slug not in self.slugs:
                return
            self.exams.selection_clear(0, "end")
            self.exams.selection_set(self.slugs.index(slug))
            self.choose_exam()
            if self.session.selected != slug:
                return
        self.tabs.select(self.control_tab if control else self.input_tab)

    def duplicate_home_settings(self):
        selection = self.project_rows.selection()
        if not selection:
            self.notice.set("Seleziona una materia da cui copiare le impostazioni.")
            return
        from tkinter import simpledialog
        name = simpledialog.askstring("Duplica impostazioni", "Nome della nuova materia (senza file né domande):", parent=self.root)
        if name is None:
            return
        try:
            slug = duplicate_settings(self.app, selection[0], name)
        except (ValueError, OSError) as error:
            self.notice.set(str(error))
            return
        self.notice.set("Nuova materia creata con sole impostazioni: " + slug + ". Aggiungi i materiali prima di avviare.")
        self.refresh()
        if self.project_rows.exists(slug):
            self.project_rows.selection_set(slug)

    def open_home_pdf_folder(self):
        selection = self.project_rows.selection()
        if not selection:
            self.notice.set("Seleziona una materia prima di aprire la cartella PDF.")
            return
        self.open_path(self.app.root(selection[0]) / "output/pdf")

    def show_home_pdf_versions(self):
        selection = self.project_rows.selection()
        if not selection:
            self.notice.set("Seleziona una materia prima di vedere le versioni PDF.")
            return
        rows = pdf_versions(self.app.root(selection[0]))
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Versioni PDF · " + selection[0])
        dialog.geometry("960x570")
        frame = self.ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        self.ttk.Label(frame, text="File già presenti sul disco. 'Corrente' indica lo schema associato all'hash attuale; aprire un file non avvia generazioni.",
                       wraplength=900).pack(anchor="w", pady=(0, 8))
        table = self.ttk.Treeview(frame, columns=("number", "title", "version", "state", "bytes"), show="headings", selectmode="browse")
        for key, label, width in (("number", "Schema", 75), ("title", "Titolo", 310),
                                  ("version", "Versione", 170), ("state", "Stato", 170), ("bytes", "Byte", 110)):
            table.heading(key, text=label)
            table.column(key, width=width, stretch=key == "title")
        table.pack(fill="both", expand=True)
        for index, row in enumerate(rows):
            table.insert("", "end", iid=str(index), values=(row["number"], row["title"],
                ("Corrente" if row["current"] else "Storica") + " · " + row["request_hash"],
                row["status"], row["bytes"]))
        if not rows:
            self.ttk.Label(frame, text="Nessun PDF versionato è ancora presente per questa materia.").pack(anchor="w", pady=8)
        def open_selected():
            chosen = table.selection()
            if not chosen:
                self.notice.set("Seleziona una versione PDF dalla lista.")
                return
            self.open_path(rows[int(chosen[0])]["path"])
        self.ttk.Button(frame, text="Apri versione selezionata", command=open_selected).pack(anchor="e", pady=(8, 0))

    def toggle_home_favorite(self):
        selection = self.project_rows.selection()
        if not selection:
            self.notice.set("Seleziona una materia da aggiungere ai preferiti.")
            return
        slug = selection[0]
        favorite = bool(self.app.detail(slug)["exam"].get("favorite"))
        try:
            result = set_favorite(self.app, slug, not favorite)
        except (ValueError, OSError, BlockingIOError) as error:
            self.notice.set(str(error))
            return
        self.notice.set("Materia aggiunta ai preferiti." if result else "Materia rimossa dai preferiti.")
        self.refresh()

    def toggle_home_archive(self):
        selection = self.project_rows.selection()
        if not selection:
            self.notice.set("Seleziona una materia da archiviare o riattivare.")
            return
        slug = selection[0]
        archived = bool(self.app.detail(slug)["exam"].get("archived"))
        from tkinter import messagebox
        prompt = ("Riattivare questa materia? Non partiranno invii finché non premi Avvia."
                  if archived else "Archiviare questa materia? I file restano sul disco e potrai riattivarla.")
        if not messagebox.askyesno("Stato della materia", prompt, parent=self.root):
            return
        try:
            result = set_archived(self.app, slug, not archived)
        except (ValueError, OSError, BlockingIOError) as error:
            self.notice.set(str(error))
            return
        self.notice.set("Materia archiviata e recuperabile." if result else "Materia riattivata come bozza; nessun invio avviato.")
        self.refresh()

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
            query = self.home_search.get().strip().casefold()
            if query:
                rows = [row for row in rows if query in row["name"].casefold() or query in row["slug"]]
            self.slugs = [row["slug"] for row in rows]
            self.exams.delete(0, "end")
            for index, row in enumerate(rows):
                text = row["name"] + (" · in corso" if row["busy"] else "")
                self.exams.insert("end", text)
                if row["slug"] == self.session.selected:
                    self.exams.selection_set(index)
            selected_project = self.project_rows.selection()
            self.project_rows.delete(*self.project_rows.get_children())
            for card in arrange_cards(project_cards(self.app, rows), self.home_visibility.get(), self.home_order.get()):
                self.project_rows.insert("", "end", iid=card["slug"],
                                         values=(("★ " if card["favorite"] else "") + card["name"],
                                                 f"{card['ready']}/{card['total']}",
                                                 card["phase"], card["last_remote_check"] or "mai"))
            if selected_project and self.project_rows.exists(selected_project[0]):
                self.project_rows.selection_set(selected_project[0])
            detail = self.app.detail(self.session.selected) if self.session.selected else None
            status = overview(self.app.root(self.session.selected), detail["busy"]) if detail else None
            for key, variable in self.monitor_vars.items():
                variable.set(status[key] if status else "Scegli una materia")
            profile = detail["exam"].get("pipeline", {}) if detail else self.app.defaults()
            self.model.set("Modello: " + str(profile.get("model", "—")))
            previous_file = self.file_rows.get(self.files.selection()[0]) if self.files.selection() else None
            self.files.delete(*self.files.get_children())
            self.file_rows = {}
            ingest_files = (read_json(self.app.root(self.session.selected) / "state/ingest.json", {}).get("files", {})
                            if detail else {})
            for file in (detail or {}).get("files", []):
                relative = f"input/{KINDS[file['kind']]}/{file['name']}"
                ingest_state = ingest_files.get(relative, {})
                label = {"processed": "Indicizzato", "failed": "Errore estrazione"}.get(ingest_state.get("status"), "Copiato")
                item = self.files.insert("", "end", values=(file["kind"], file["name"], file["size"], label))
                self.file_rows[item] = (file["kind"], file["name"], False)
            for kind, paths in self.session.pending.items():
                for path in paths:
                    item = self.files.insert("", "end", values=(kind, path.name, path.stat().st_size, "Da caricare"))
                    self.file_rows[item] = (kind, path, True)
            if previous_file:
                for item, value in self.file_rows.items():
                    if value == previous_file:
                        self.files.selection_set(item)
                        break
            previous_output = self.outputs.item(self.outputs.selection()[0], "values") if self.outputs.selection() else None
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
            if previous_output:
                for item in self.outputs.get_children():
                    if self.outputs.item(item, "values") == previous_output:
                        self.outputs.selection_set(item)
                        break
            self.upgrade_button.state(["!disabled"] if detail and any(profile.get(key) != self.app.defaults().get(key) for key in MODEL_KEYS) else ["disabled"])
            text = diagnostics(self.app, self.session.selected) if detail else "Scegli una materia per vedere stato, errori e identificativi dei batch."
            plan = (detail or {}).get("plan")
            if plan:
                text += "\n\nOrganizzazione e copertura delle fonti:\n" + json.dumps(plan, ensure_ascii=False, indent=2)
            self.status_text.configure(state="normal")
            self.status_text.delete("1.0", "end")
            self.status_text.insert("1.0", text)
            self.status_text.configure(state="disabled")
            control_view = snapshot(self.app.root(self.session.selected), detail["busy"]) if detail else None
            self.control_view = control_view
            control = format_snapshot(control_view) if control_view else "Scegli una materia per consultare la Control Room locale."
            self.control_text.configure(state="normal")
            self.control_text.delete("1.0", "end")
            self.control_text.insert("1.0", control)
            self.control_text.configure(state="disabled")
            previous_job = self.control_job_rows.selection()
            self.control_job_rows.delete(*self.control_job_rows.get_children())
            self.control_batch_rows.delete(*self.control_batch_rows.get_children())
            self.control_performance_rows.delete(*self.control_performance_rows.get_children())
            if control_view:
                for job in control_view["jobs"]:
                    self.control_job_rows.insert("", "end", iid=str(job["number"]),
                                                 values=(job["number"], job["title"], job["hash"], job["status"], job["retry"]))
                for batch in control_view["batches"]:
                    self.control_batch_rows.insert("", "end", values=(batch["mode"] or "batch", batch["id"] or "ID incerto",
                                                                    batch["stage"] or "—", batch["status"] or "non noto",
                                                                    batch["checked_at"] or "mai"))
                for row in control_view["performance"]:
                    self.control_performance_rows.insert("", "end", values=(row["stage"], row["success"], row["failed"],
                                                                            row["median_ms"] if row["median_ms"] is not None else "n/d",
                                                                            row["p90_ms"] if row["p90_ms"] is not None else "n/d",
                                                                            row["p95_ms"] if row["p95_ms"] is not None else "n/d",
                                                                            row["max_ms"] if row["max_ms"] is not None else "n/d"))
            if previous_job and self.control_job_rows.exists(previous_job[0]):
                self.control_job_rows.selection_set(previous_job[0])
                self.show_control_job()
            selected_illustration = self.illustrations.selection()
            self.illustrations.delete(*self.illustrations.get_children())
            if detail:
                current = read_json(self.app.root(self.session.selected) / "state/pipeline.json", {}).get("latest", {})
                for row in list_opportunities(self.app.root(self.session.selected)):
                    if current.get(str(row["schema_number"])) == row["request_hash"]:
                        self.illustrations.insert("", "end", iid=row["opportunity_id"],
                                                  values=(row["schema_number"], row["concept_title"], row["status"]))
            if selected_illustration and self.illustrations.exists(selected_illustration[0]):
                self.illustrations.selection_set(selected_illustration[0])
            self.show_illustration_details()
        except Exception as error:
            self.notice.set("Errore: " + str(error))

    def periodic_refresh(self):
        if not self.local_busy:
            self.refresh()
        if not self.closing:
            self.root.after(3000, self.periodic_refresh)

    def show_control_job(self, event=None):
        choice = self.control_job_rows.selection()
        view = getattr(self, "control_view", None)
        if not choice or not view:
            return
        job = next((item for item in view["jobs"] if str(item["number"]) == choice[0]), None)
        if not job:
            return
        lines = [f"Schema {job['number']} · {job['title']}", f"Stato: {job['status']} · tentativi: {job['retry']}",
                 f"Hash corrente: {job['hash']}", f"PDF corrente: {job['pdf'] or 'non pronto'}"]
        if job["error"]:
            lines.append("Ultimo errore: " + job["error"])
        lines.append("Eventi locali osservati:")
        lines.extend(f"{event['at']} · {event['stage']} · {event['name']} · {event['duration_ms'] if event['duration_ms'] is not None else 'n/d'} ms"
                     for event in view["events"] if event["hash"] == job["hash"])
        self.control_job_detail.configure(state="normal")
        self.control_job_detail.delete("1.0", "end")
        self.control_job_detail.insert("1.0", "\n".join(lines))
        self.control_job_detail.configure(state="disabled")

    def preview_material(self):
        selection = self.files.selection()
        if not selection or not self.session.selected:
            self.notice.set("Seleziona un file nei Materiali.")
            return
        kind, name, pending = self.file_rows[selection[0]]
        if pending:
            info = f"{name.name}\nFormato: {name.suffix.lower()} · byte: {name.stat().st_size}\nStato: da caricare. Salva la materia prima di leggere l'anteprima estratta."
        else:
            row = material_detail(self.app.root(self.session.selected), kind, name)
            info = (f"{row['name']}\nFormato: {row['format']} · byte: {row['bytes']} · pagine: {row['pages'] or 'non disponibili'}\n"
                    f"Stato: {row['status']} · immagini estratte: {row['images'] if row['images'] is not None else 'non disponibili'}\n"
                    f"SHA-256: {row['sha256'] or 'non ancora disponibile'}\nOCR: {row['ocr_quality']}\n"
                    f"Errore: {row['error'] or 'nessuno'}\n\nTesto estratto (campione):\n{row['text_preview'] or 'non disponibile'}")
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Dettaglio del materiale")
        dialog.geometry("830x600")
        from tkinter.scrolledtext import ScrolledText
        area = ScrolledText(dialog, wrap="word", font=("Helvetica", 11))
        area.pack(fill="both", expand=True, padx=12, pady=12)
        area.insert("1.0", info)
        area.configure(state="disabled")

    def show_coverage(self):
        if not self.session.selected:
            self.notice.set("Scegli una materia prima di confrontare il programma.")
            return
        try:
            result = compare_coverage(self.app.root(self.session.selected))
        except Exception as error:
            self.notice.set("Confronto non disponibile: " + str(error))
            return
        lines = [result["message"], ""]
        for row in result["rows"]:
            lines.append(f"{row['topic']} · {row['hint']} · {row['source_count']} file materiali · {row['question_count']} domande")
            if row["source_names"]:
                lines.append("  Fonti nominate: " + "; ".join(row["source_names"]))
        if not result["rows"]:
            lines.append("Nessuna riga confrontabile al momento.")
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Programma e fonti · stima lessicale")
        dialog.geometry("900x620")
        from tkinter.scrolledtext import ScrolledText
        area = ScrolledText(dialog, wrap="word", font=("Helvetica", 11))
        area.pack(fill="both", expand=True, padx=12, pady=12)
        area.insert("1.0", "\n".join(lines))
        area.configure(state="disabled")

    def plan_illustrations(self):
        if not self.session.selected:
            self.illustrator_notice.set("Scegli una materia prima di cercare tavole.")
            return
        rows = plan_illustrations(self.app.root(self.session.selected))
        self.illustrator_notice.set(f"{len(rows)} proposte registrate. Le bozze sono schematiche e non entrano nei PDF automaticamente.")
        self.refresh()

    def add_manual_illustration(self):
        if not self.session.selected:
            self.illustrator_notice.set("Scegli una materia prima di aggiungere una tavola.")
            return
        root = self.app.root(self.session.selected)
        state = read_json(root / "state/pipeline.json", {})
        jobs = state.get("jobs") or {}
        options = {f"{number} · {jobs[key].get('title', 'Schema')}": int(number)
                   for number, key in sorted((state.get("latest") or {}).items(), key=lambda pair: int(pair[0])) if key in jobs}
        if not options:
            self.illustrator_notice.set("Crea prima il piano degli schemi e i source pack.")
            return
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Nuova tavola concettuale")
        dialog.geometry("650x350")
        frame = self.ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        schema = self.tk.StringVar(value=next(iter(options)))
        title = self.tk.StringVar()
        kind = self.tk.StringVar(value="structural")
        goal = self.tk.StringVar()
        for label, variable, widget in (("Schema", schema, "combo"), ("Titolo della tavola", title, "entry"),
                                        ("Tipo di alterazione", kind, "type"), ("Obiettivo didattico", goal, "entry")):
            self.ttk.Label(frame, text=label).pack(anchor="w", pady=(8, 2))
            if widget == "combo":
                self.ttk.Combobox(frame, textvariable=variable, values=list(options), state="readonly").pack(fill="x")
            elif widget == "type":
                self.ttk.Combobox(frame, textvariable=variable,
                    values=("structural", "spatial_relations", "neural", "tissue", "vascular", "functional", "developmental", "temporal"),
                    state="readonly").pack(fill="x")
            else:
                self.ttk.Entry(frame, textvariable=variable).pack(fill="x")
        def save_manual():
            try:
                row = manual_illustration(root, options[schema.get()], title.get(), kind.get(), goal.get())
            except (ValueError, OSError) as error:
                self.illustrator_notice.set(str(error))
                return
            dialog.destroy()
            self.illustrator_notice.set("Tavola proposta: " + row["concept_title"] + ". Accettala per creare la bozza SVG.")
            self.refresh()
            self.illustrations.selection_set(row["opportunity_id"])
        self.ttk.Button(frame, text="Salva proposta locale", command=save_manual).pack(anchor="e", pady=14)

    def edit_illustration(self):
        selection = self.illustrations.selection()
        if not selection or not self.session.selected:
            self.illustrator_notice.set("Seleziona una proposta.")
            return
        root = self.app.root(self.session.selected)
        row = next((item for item in list_opportunities(root) if item["opportunity_id"] == selection[0]), None)
        if not row or row["status"] != "proposed":
            self.illustrator_notice.set("La modifica è disponibile prima di accettare la proposta.")
            return
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Modifica proposta")
        dialog.geometry("680x440")
        frame = self.ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        fields = {}
        for key, label in (("concept_title", "Titolo"), ("learning_goal", "Obiettivo"),
                           ("side_or_orientation", "Lato / orientamento"), ("region_of_interest", "Regione"),
                           ("view_type", "Vista"), ("proposed_caption", "Didascalia proposta")):
            value = self.tk.StringVar(value=row.get(key, ""))
            fields[key] = value
            self.ttk.Label(frame, text=label).pack(anchor="w", pady=(4, 0))
            self.ttk.Entry(frame, textvariable=value).pack(fill="x")
        def save_changes():
            try:
                edit_illustration_proposal(root, row["opportunity_id"], {key: variable.get() for key, variable in fields.items()})
            except (ValueError, OSError) as error:
                self.illustrator_notice.set(str(error))
                return
            dialog.destroy()
            self.illustrator_notice.set("Proposta aggiornata. Accetta solo dopo aver verificato contenuto e fonti.")
            self.refresh()
        self.ttk.Button(frame, text="Salva modifiche", command=save_changes).pack(anchor="e", pady=12)

    def revalidate_illustration(self):
        selection = self.illustrations.selection()
        if not selection or not self.session.selected:
            self.illustrator_notice.set("Seleziona una bozza SVG.")
            return
        try:
            row = revalidate_edited_svg(self.app.root(self.session.selected), selection[0])
        except (ValueError, OSError) as error:
            self.illustrator_notice.set(str(error))
            return
        self.illustrator_notice.set("SVG verificato tecnicamente. Stato: " + row["status"] + ". Ricontrolla la correttezza medica.")
        self.refresh()

    def export_illustration(self):
        selection = self.illustrations.selection()
        if not selection or not self.session.selected:
            self.illustrator_notice.set("Seleziona una tavola approvata.")
            return
        try:
            path = export_illustrated_pdf(self.app.root(self.session.selected), selection[0])
        except Exception as error:
            self.illustrator_notice.set(str(error))
            return
        self.illustrator_notice.set("PDF illustrato separato creato: " + path.name + ". Il PDF originale non è cambiato.")
        self.refresh()
        self.open_path(path)

    def show_illustration_details(self, event=None):
        selection = self.illustrations.selection()
        row = None
        if selection and self.session.selected:
            row = next((item for item in list_opportunities(self.app.root(self.session.selected))
                        if item["opportunity_id"] == selection[0]), None)
        if row:
            sources = "; ".join(f"{item.get('document') or 'fonte'} p. {item.get('page_start') or 'n/d'}"
                                for item in row.get("source_references", [])) or "fonti da verificare"
            content = (f"Obiettivo: {row['learning_goal']}\nTipi: {', '.join(row['lesion_types'])}\n"
                       f"Vista: {row['view_type']} · {row['side_or_orientation']}\n"
                       f"Regione: {row['region_of_interest']}\nFonti: {sources}\n"
                       f"Motivo: {row['confidence_explanation']}\n"
                       f"Costo aggiuntivo: {row['estimated_cost']} · {row['uncertainty']}\n"
                       f"Licenza: {row.get('license_status', 'da verificare')}\n"
                       f"Didascalia: {row['proposed_caption']}")
        else:
            content = "Seleziona una proposta per vedere motivazione, fonti, vista e revisione richiesta."
        self.illustration_detail.configure(state="normal")
        self.illustration_detail.delete("1.0", "end")
        self.illustration_detail.insert("1.0", content)
        self.illustration_detail.configure(state="disabled")

    def diagnostic_preview(self):
        if not self.session.selected:
            self.notice.set("Scegli una materia prima di creare il report.")
            return
        root = self.app.root(self.session.selected)
        sections = build_sections(root)
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Anteprima completa del report locale")
        dialog.geometry("920x650")
        area = self.ttk.Frame(dialog, padding=12)
        area.pack(fill="both", expand=True)
        self.ttk.Label(area, text="Controlla tutti i file inclusi prima di esportare. Nessun dato viene inviato.").pack(anchor="w", pady=(0, 8))
        from tkinter.scrolledtext import ScrolledText
        content = ScrolledText(area, wrap="word", font=("Courier", 10))
        content.pack(fill="both", expand=True)
        content.insert("1.0", preview_diagnostic(sections))
        content.configure(state="disabled")
        def save():
            path = export_diagnostic(root, sections)
            self.notice.set("Report diagnostico salvato: " + path.name)
            dialog.destroy()
            self.open_path(path.parent)
        self.ttk.Button(area, text="Esporta ZIP dopo la verifica", command=save).pack(anchor="e", pady=(8, 0))

    def recover_local_pdfs(self):
        if not self.session.selected:
            self.notice.set("Scegli una materia prima del recupero locale.")
            return
        root = self.app.root(self.session.selected)
        def done(actions):
            if not actions:
                self.notice.set("Nessun PDF locale da recuperare.")
            else:
                self.notice.set("Recupero locale: " + "; ".join(
                    f"schema {item['schema']} {item['action']}={item['outcome']}" for item in actions))
            self.refresh()
        self.async_task(lambda: recover_local(root, self.app.project), done,
                        "Verifico HTML e PDF locali; nessuna richiesta OpenAI sarà inviata…")

    def toggle_illustrations(self):
        if not self.session.selected:
            return
        root = self.app.root(self.session.selected)
        current = read_json(root / "state/illustrations.json", {}).get("disabled", False)
        disabled = disable_illustrations(root, not current)
        self.illustrator_notice.set("Nuove proposte disattivate per questa materia." if disabled else "Nuove proposte riattivate.")

    def illustration_action(self, action, review=None):
        selection = self.illustrations.selection()
        if not selection or not self.session.selected:
            self.illustrator_notice.set("Seleziona una tavola.")
            return
        if action == "approved" and review is None:
            self.review_illustration()
            return
        try:
            row = transition_illustration(self.app.root(self.session.selected), selection[0], action, review=review)
            self.illustrator_notice.set(f"{row['concept_title']}: {row['status']}. Nessun PDF modificato.")
            self.refresh()
        except (ValueError, OSError) as error:
            self.illustrator_notice.set(str(error))

    def review_illustration(self):
        selection = self.illustrations.selection()
        if not selection or not self.session.selected:
            return
        row = next((item for item in list_opportunities(self.app.root(self.session.selected))
                    if item["opportunity_id"] == selection[0]), None)
        if not row or row.get("status") != "medical_review_needed":
            self.illustrator_notice.set("La revisione richiede una bozza SVG corrente da controllare.")
            return
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Lista di controllo · " + row["concept_title"])
        dialog.geometry("760x540")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = self.ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        self.ttk.Label(frame, text="Apri prima la bozza e confrontala con le fonti. Se non puoi verificare un punto, lascia la tavola in revisione.",
                       wraplength=700).pack(anchor="w", pady=(0, 12))
        labels = {
            "sources": "Ho confrontato la tavola con le fonti indicate.",
            "anatomy": "Strutture e rapporti anatomici rappresentati sono corretti.",
            "orientation": "Lato, vista e orientamento sono chiari.",
            "lesion": "L'alterazione è collocata e descritta correttamente.",
            "labels_caption": "Etichette e didascalia sono coerenti e leggibili.",
            "rights": "Non sono stati aggiunti asset esterni senza diritti d'uso.",
        }
        checks = {key: self.tk.BooleanVar(value=False) for key in REVIEW_FIELDS}
        for key in REVIEW_FIELDS:
            self.ttk.Checkbutton(frame, text=labels[key], variable=checks[key]).pack(anchor="w", pady=8)
        self.ttk.Label(frame, text="Questo registra un controllo dell'utente; non certifica una validazione clinica specialistica.",
                       wraplength=700).pack(anchor="w", pady=(18, 5))
        info = self.tk.StringVar()
        self.ttk.Label(frame, textvariable=info).pack(anchor="w")
        def confirm():
            review = {key: variable.get() for key, variable in checks.items()}
            if not all(review.values()):
                info.set("Completa ogni controllo oppure lascia la tavola in revisione.")
                return
            dialog.grab_release()
            dialog.destroy()
            self.illustration_action("approved", review=review)
        self.ttk.Button(frame, text="Registra revisione", command=confirm).pack(anchor="e", pady=12)

    def open_illustration(self):
        selection = self.illustrations.selection()
        if not selection or not self.session.selected:
            self.illustrator_notice.set("Seleziona una tavola.")
            return
        root = self.app.root(self.session.selected)
        row = next((item for item in list_opportunities(root) if item["opportunity_id"] == selection[0]), None)
        if not row or not row.get("artifact"):
            self.illustrator_notice.set("Accetta la proposta per creare la bozza SVG.")
            return
        path = (root / row["artifact"]).resolve()
        if not path.is_relative_to((root / "diagrams/medical").resolve()):
            raise ValueError("Percorso della tavola non valido.")
        try:
            png = preview_svg(root, selection[0])
        except (ValueError, OSError) as error:
            self.illustrator_notice.set(str(error))
            return
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Anteprima tavola · " + row["concept_title"])
        dialog.geometry("790x590")
        frame = self.ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        self.ttk.Label(frame, text="Bozza schematica: verifica anatomia, lato, vista ed etichette sulle fonti.",
                       wraplength=740).pack(anchor="w", pady=(0, 8))
        picture = self.tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
        preview = self.ttk.Label(frame, image=picture)
        preview.image = picture
        preview.pack(fill="both", expand=True)
        self.ttk.Button(frame, text="Apri file SVG per modificarlo", command=lambda: self.open_path(path)).pack(anchor="e", pady=(8, 0))

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

    def visual_check(self):
        selection = self.outputs.selection()
        if not selection or selection[0] not in self.pdf_rows:
            self.notice.set("Seleziona un PDF pronto per il controllo visivo.")
            return
        root = self.app.root(self.session.selected)
        path = (root / self.pdf_rows[selection[0]]).resolve()
        if not path.is_relative_to((root / "output/pdf").resolve()) or path.suffix.lower() != ".pdf":
            self.notice.set("Il controllo visivo richiede un PDF della materia.")
            return
        try:
            result = inspect_visual(path)
            reviews = load_visual_reviews(root, path, result)
        except Exception as error:
            self.notice.set("Controllo visivo non riuscito: " + str(error))
            return
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Controllo visivo · " + path.name)
        dialog.geometry("1000x760")
        frame = self.ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        self.ttk.Label(frame, text="Avvisi da verificare a vista; non modificano il PDF né lo stato PDF_OK.").pack(anchor="w")
        pages = self.tk.Listbox(frame, height=7, exportselection=False)
        pages.pack(fill="x", pady=8)
        def page_label(item):
            decision = (reviews.get(str(item["page"])) or {}).get("decision")
            marker = " [verificata]" if decision == "verified" else " [falso positivo annotato]" if decision == "false_positive" else ""
            return f"Pagina {item['page']}{marker} · " + ("; ".join(item["warnings"]) if item["warnings"] else "Nessun avviso")
        for item in result["pages"]:
            pages.insert("end", page_label(item))
        preview = self.ttk.Label(frame, text="Seleziona una pagina per vedere l'anteprima.")
        preview.pack(fill="both", expand=True)
        review_state = self.tk.StringVar(value="Nessuna pagina selezionata.")
        self.ttk.Label(frame, textvariable=review_state).pack(anchor="w")
        def show(_event=None):
            choice = pages.curselection()
            if not choice:
                return
            saved = reviews.get(str(choice[0] + 1)) or {}
            review_state.set("Revisione visiva: " + (saved.get("decision") or "non registrata")
                             + (" · " + saved["note"] if saved.get("note") else ""))
            import fitz
            with fitz.open(path) as document:
                pixmap = document[choice[0]].get_pixmap(matrix=fitz.Matrix(0.8, 0.8), alpha=False)
                data = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
            image = self.tk.PhotoImage(data=data)
            preview.configure(image=image, text="")
            preview.image = image
        pages.bind("<<ListboxSelect>>", show)
        if result["pages"]:
            pages.selection_set(0)
            show()
        actions = self.ttk.Frame(frame)
        actions.pack(fill="x", pady=(8, 0))
        self.ttk.Button(actions, text="Apri PDF originale", command=lambda: self.open_path(path)).pack(side="left")
        def mark(decision):
            choice = pages.curselection()
            if not choice:
                self.notice.set("Seleziona una pagina da controllare.")
                return
            note = ""
            if decision == "false_positive":
                from tkinter import simpledialog
                note = simpledialog.askstring("Falso positivo", "Perché l'avviso non indica un difetto?", parent=dialog)
                if note is None:
                    return
            item = result["pages"][choice[0]]
            try:
                saved = mark_visual_review(root, path, result, item["page"], decision, note)
            except (ValueError, OSError) as error:
                self.notice.set("Revisione visiva non registrata: " + str(error))
                return
            reviews[str(item["page"])] = saved
            pages.delete(choice[0])
            pages.insert(choice[0], page_label(item))
            pages.selection_set(choice[0])
            show()
            self.notice.set("Revisione visiva locale salvata; il PDF e PDF_OK non sono cambiati.")
        self.ttk.Button(actions, text="Segna verificata", command=lambda: mark("verified")).pack(side="left", padx=8)
        self.ttk.Button(actions, text="Annota falso positivo", command=lambda: mark("false_positive")).pack(side="left")
        def save_report():
            try:
                report = export_visual_report(root, path, result)
            except (ValueError, OSError) as error:
                self.notice.set("Report visivo non creato: " + str(error))
                return
            self.notice.set("Report visivo salvato: " + report.name)
            self.open_path(report.parent)
        self.ttk.Button(actions, text="Esporta report tecnico locale", command=save_report).pack(side="right")

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
