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
from control_room import format_global_snapshot, format_snapshot, global_snapshot, snapshot
from diagnostic_report import build_sections, export as export_diagnostic, preview as preview_diagnostic
from local_app import App, KINDS, MAX_FILE_BYTES, edit_lock, save_api_key
from local_recovery import recover_local
from materials_view import material_detail
from medical_illustrator import (create_manual as manual_illustration, export_illustrated_pdf, list_opportunities,
                                 plan as plan_illustrations, preview_svg, revalidate_edited_svg, REVIEW_FIELDS,
                                 set_disabled as disable_illustrations, transition as transition_illustration,
                                 update_proposal as edit_illustration_proposal)
from ingest import SUPPORTED
from model_settings import MODEL_KEYS, effective_profile_sources, format_effective_profile, save_default_profile
from monitor import batch_lines, overview
from project_wizard import validate_wizard
from project_actions import duplicate_settings, set_archived, set_favorite
from projects_view import arrange_cards, pdf_versions, project_cards
from visual_qc import export_visual_report, inspect_visual, load_visual_reviews, mark_visual_review
from ui_theme import COLORS, install_theme, vertical_scroll_area


ILLUSTRATION_STATUS_LABELS = {
    "proposed": "Proposta",
    "accepted": "Bozza in preparazione",
    "technical_qc_failed": "Controllo tecnico fallito",
    "medical_review_needed": "Da revisionare",
    "approved": "Revisionata dall'utente",
    "published": "Copia PDF illustrata esportata",
    "rejected": "Scartata",
}


def illustration_status_label(status):
    return ILLUSTRATION_STATUS_LABELS.get(status, str(status))


def readable_size(size):
    """Format observed file bytes for compact desktop tables."""
    size = int(size)
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def display_job_status(status):
    return {"PDF_OK": "PDF pronto", "HTML_OK": "HTML pronto", "DATA_OK": "Dati pronti",
            "PENDING": "In attesa", "RENDERING": "PDF in preparazione",
            "FAILED": "Errore", "SUPERSEDED": "Versione storica"}.get(status, str(status or "In attesa"))


def brief_status(value, limit=140):
    """Keep observed stage cards readable; full detail remains in the local log."""
    parts = str(value).split(" · ")
    result = " · ".join(parts[:2])
    return result if len(result) <= limit else result[:limit - 1].rstrip() + "…"


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
        self.file_rows, self.pdf_rows, self.home_card_data = {}, {}, {}
        install_theme(root)
        root.title("Schemi Esami")
        root.geometry("1280x820")
        root.minsize(880, 650)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.name = tk.StringVar()
        self.mode = tk.StringVar(value="completa")
        self.pages = tk.StringVar(value="20")
        self.notice = tk.StringVar(value="Scegli una materia oppure creane una nuova.")
        self.model = tk.StringVar()
        self.progress = tk.StringVar(value="Materiali salvati")
        self.slugs = []
        outer = ttk.Frame(root, style="App.TFrame")
        outer.pack(fill="both", expand=True)
        self.outer = outer
        outer.columnconfigure(0, minsize=242)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)
        sidebar = ttk.Frame(outer, padding=(16, 18), style="Sidebar.TFrame", width=242)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        self.sidebar = sidebar
        ttk.Label(sidebar, text="Schemi Esami", style="SidebarTitle.TLabel").pack(anchor="w", pady=(0, 4))
        ttk.Label(sidebar, text="V2 · desktop macOS", style="SidebarMuted.TLabel").pack(anchor="w", pady=(0, 18))
        ttk.Button(sidebar, text="+ Nuova materia", command=self.open_wizard,
                   style="SidebarAccent.TButton").pack(fill="x", pady=(0, 14))
        self.home_search = tk.StringVar()
        ttk.Entry(sidebar, textvariable=self.home_search, style="Sidebar.TEntry").pack(fill="x", pady=(0, 16))
        ttk.Label(sidebar, text="MATERIE", style="SidebarMuted.TLabel").pack(anchor="w", pady=(0, 7))
        self.exams = tk.Listbox(sidebar, width=22, exportselection=False, borderwidth=0,
                                highlightthickness=0, relief="flat", font=("Helvetica", 11),
                                background=COLORS["navy"], foreground=COLORS["surface"],
                                selectbackground=COLORS["sidebar_selected"], selectforeground=COLORS["surface"],
                                activestyle="none")
        self.exams.pack(fill="both", expand=True)
        self.exams.bind("<<ListboxSelect>>", self.choose_exam)
        ttk.Button(sidebar, text="Impostazioni modello", command=self.settings,
                   style="Sidebar.TButton").pack(fill="x", pady=(14, 5))
        ttk.Button(sidebar, text="Chiave OpenAI", command=self.set_api_key,
                   style="Sidebar.TButton").pack(fill="x", pady=(0, 5))
        ttk.Button(sidebar, text="Aiuto · manuale PDF", command=self.open_manual,
                   style="Sidebar.TButton").pack(fill="x", pady=(0, 5))
        main = ttk.Frame(outer, style="Main.TFrame")
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)
        header = ttk.Frame(main, padding=(20, 14))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(4, weight=1)
        ttk.Label(header, text="MATERIA CORRENTE", style="Eyebrow.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Schemi Esami V2", style="Title.TLabel").grid(row=1, column=0, sticky="w", pady=(1, 9))
        ttk.Entry(header, textvariable=self.name, width=42).grid(row=3, column=0, sticky="w", padx=(0, 16))
        ttk.Label(header, text="Locale · Mac acceso", style="Badge.TLabel").grid(row=0, column=4, sticky="e")
        ttk.Label(header, text="Obiettivo", style="Muted.TLabel").grid(row=3, column=1, sticky="e", padx=(0, 6))
        ttk.Radiobutton(header, text="Completa", variable=self.mode, value="completa", command=self.show_mode).grid(row=3, column=2)
        ttk.Radiobutton(header, text="Sintesi", variable=self.mode, value="veloce", command=self.show_mode).grid(row=3, column=3, padx=12)
        self.page_frame = ttk.Frame(header)
        self.page_frame.grid(row=2, column=4, rowspan=2)
        ttk.Label(self.page_frame, text="Massimo pagine", style="Muted.TLabel").pack(anchor="w")
        ttk.Spinbox(self.page_frame, from_=1, to=1000, textvariable=self.pages, width=9).pack(anchor="w", pady=5)
        self.notice_label = ttk.Label(main, textvariable=self.notice, style="Muted.TLabel", wraplength=500,
                                      padding=(20, 2))
        self.notice_label.grid(row=1, column=0, sticky="ew")
        self.tabs = ttk.Notebook(main)
        self.tabs.grid(row=2, column=0, sticky="nsew")
        self.home_tab = ttk.Frame(self.tabs, padding=(20, 12), style="Content.TFrame")
        self.input_tab = ttk.Frame(self.tabs, padding=(20, 12), style="Content.TFrame")
        self.output_tab = ttk.Frame(self.tabs, padding=(20, 12), style="Content.TFrame")
        self.status_tab = ttk.Frame(self.tabs, padding=(20, 12), style="Content.TFrame")
        self.control_tab = ttk.Frame(self.tabs, padding=(20, 12), style="Content.TFrame")
        self.illustrator_tab = ttk.Frame(self.tabs, padding=(20, 12), style="Content.TFrame")
        self.full_tab_labels = ("Progetti", "Materiali e domande", "Schemi e PDF", "Stato e batch",
                                "Control Room", "Medical Illustrator")
        self.compact_tab_labels = ("Progetti", "Input", "PDF", "Stato", "Control", "Tavole")
        self._compact_tabs = None
        for tab, label in zip((self.home_tab, self.input_tab, self.output_tab, self.status_tab,
                               self.control_tab, self.illustrator_tab), self.full_tab_labels):
            self.tabs.add(tab, text=label)
        self.home_tab.columnconfigure(0, weight=1)
        self.home_tab.rowconfigure(2, weight=1)
        home_heading = ttk.Frame(self.home_tab, style="Content.TFrame")
        home_heading.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(home_heading, text="Progetti", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(home_heading, text="Gestisci le materie senza perdere le versioni dei PDF.",
                  style="PageMuted.TLabel").pack(anchor="w", pady=(2, 0))
        home_filters = ttk.Frame(self.home_tab, style="Content.TFrame")
        home_filters.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.home_visibility = tk.StringVar(value="Attive")
        self.home_order = tk.StringVar(value="Nome")
        ttk.Entry(home_filters, textvariable=self.home_search, width=24).pack(side="left", padx=(0, 12))
        visible = ttk.Combobox(home_filters, textvariable=self.home_visibility,
                               values=("Attive", "Tutte", "Archiviate", "Preferite"), state="readonly", width=13)
        visible.pack(side="left", padx=(0, 12))
        visible.bind("<<ComboboxSelected>>", lambda event: self.refresh())
        ttk.Label(home_filters, text="Ordina", style="PageMuted.TLabel").pack(side="left", padx=(0, 6))
        ordered = ttk.Combobox(home_filters, textvariable=self.home_order,
                               values=("Nome", "Data esame", "PDF pronti"), state="readonly", width=15)
        ordered.pack(side="left")
        ordered.bind("<<ComboboxSelected>>", lambda event: self.refresh())
        home_body = ttk.Frame(self.home_tab, style="Content.TFrame")
        home_body.grid(row=2, column=0, sticky="nsew")
        home_body.columnconfigure(0, weight=3)
        home_body.columnconfigure(1, weight=1)
        home_body.rowconfigure(0, weight=1)
        self.home_body = home_body
        list_frame = ttk.Frame(home_body, style="Card.TFrame")
        list_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.home_list_frame = list_frame
        selected_card = ttk.Frame(home_body, style="Card.TFrame", padding=(16, 14))
        selected_card.grid(row=0, column=1, sticky="nsew")
        selected_card.columnconfigure(0, weight=1)
        self.home_selected_card = selected_card
        self.home_card_title = tk.StringVar(value="Scegli una materia")
        self.home_card_summary = tk.StringVar(value="I dettagli appariranno qui.")
        self.home_card_phase = tk.StringVar(value="Bozza")
        ttk.Label(selected_card, text="Materia selezionata", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self.home_card_title_label = ttk.Label(selected_card, textvariable=self.home_card_title,
                                               style="CardValue.TLabel", wraplength=220)
        self.home_card_title_label.grid(row=1, column=0, columnspan=2, sticky="w")
        self.home_card_badge = ttk.Label(selected_card, textvariable=self.home_card_phase, style="Badge.TLabel")
        self.home_card_badge.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.home_summary_label = ttk.Label(selected_card, textvariable=self.home_card_summary,
                                            style="CardBody.TLabel", wraplength=220, justify="left")
        self.home_summary_label.grid(row=3, column=0, columnspan=2, sticky="nw", pady=(18, 5))
        self.home_card_progress = ttk.Progressbar(selected_card, mode="determinate")
        self.home_card_progress.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.project_rows = ttk.Treeview(list_frame,
                                          columns=("name", "mode", "date", "pdf", "phase", "remote"),
                                          show="headings", selectmode="browse", height=5)
        for key, title, width in (("name", "Materia", 160), ("mode", "Obiettivo", 85),
                                  ("date", "Esame", 95), ("pdf", "PDF", 55),
                                  ("phase", "Stato", 180), ("remote", "OpenAI", 110)):
            self.project_rows.heading(key, text=title)
            self.project_rows.column(key, width=width, stretch=key in {"name", "phase"})
        self.project_rows.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(list_frame, orient="vertical", command=self.project_rows.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(list_frame, orient="horizontal", command=self.project_rows.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.project_rows.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.project_rows.tag_configure("success", foreground=COLORS["teal"])
        self.project_rows.tag_configure("attention", foreground=COLORS["amber"])
        self.project_rows.tag_configure("inactive", foreground=COLORS["muted"])
        self.project_rows.bind("<<TreeviewSelect>>", self.show_home_summary)
        self.project_rows.bind("<Double-1>", lambda event: self.open_home_project())
        home_actions = ttk.Frame(self.home_tab, style="Content.TFrame")
        home_actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(home_actions, text="Apri materia", command=self.open_home_project,
                   style="Accent.TButton").pack(side="left")
        ttk.Button(home_actions, text="Control Room", command=lambda: self.open_home_project(True)).pack(side="left", padx=7)
        ttk.Button(home_actions, text="Cartella PDF", command=self.open_home_pdf_folder).pack(side="left")
        home_secondary = ttk.Frame(home_actions, style="Content.TFrame")
        home_secondary.pack(side="left", padx=(7, 0))
        self.home_secondary = home_secondary
        ttk.Button(home_secondary, text="★ Preferito", command=self.toggle_home_favorite).pack(side="left")
        ttk.Button(home_secondary, text="Duplica", command=self.duplicate_home_settings).pack(side="left", padx=7)
        ttk.Button(home_secondary, text="Archivia / Riattiva", command=self.toggle_home_archive).pack(side="left")
        ttk.Button(home_secondary, text="Versioni PDF", command=self.show_home_pdf_versions).pack(side="left", padx=7)
        self.home_more = ttk.Menubutton(home_actions, text="Altre azioni")
        home_menu = tk.Menu(self.home_more, tearoff=False)
        for label, command in (("★ Preferito", self.toggle_home_favorite), ("Duplica", self.duplicate_home_settings),
                               ("Archivia / Riattiva", self.toggle_home_archive), ("Versioni PDF", self.show_home_pdf_versions)):
            home_menu.add_command(label=label, command=command)
        self.home_more.configure(menu=home_menu)
        inputs = vertical_scroll_area(self.input_tab)
        inputs.columnconfigure(0, weight=1)
        inputs.rowconfigure(1, weight=0)
        toolbar = ttk.Frame(inputs, style="Content.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(toolbar, text="Materiali e domande", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(toolbar, text="Fonti della materia · file e testo salvati sul Mac.",
                  style="PageMuted.TLabel").pack(anchor="w", pady=(2, 10))
        file_actions = ttk.Frame(toolbar, style="Content.TFrame")
        file_actions.pack(fill="x")
        self.material_count_vars = {}
        for kind, title in (("materiali", "Sbobine e appunti"),
                            ("domande", "Domande d’esame"), ("programma", "Programma ufficiale")):
            panel = ttk.Frame(file_actions, style="Card.TFrame", padding=(12, 10))
            panel.pack(side="left", fill="both", expand=True, padx=(0, 8))
            ttk.Label(panel, text=title, style="CardTitle.TLabel", wraplength=170).pack(anchor="w")
            count = tk.StringVar(value="0 file")
            self.material_count_vars[kind] = count
            actions_row = ttk.Frame(panel)
            actions_row.pack(fill="x", pady=(8, 0))
            ttk.Label(actions_row, textvariable=count, style="MutedBadge.TLabel").pack(side="left")
            ttk.Button(actions_row, text="Scegli file", command=lambda k=kind: self.pick_files(k)).pack(side="right")
        self.files = ttk.Treeview(inputs, columns=("area", "name", "size", "state"), show="headings", height=4, selectmode="browse")
        for key, title, width in [("area", "Area", 90), ("name", "Nome file", 300),
                                  ("size", "Dimensione", 105), ("state", "Stato", 150)]:
            self.files.heading(key, text=title)
            self.files.column(key, width=width, stretch=key == "name")
        self.files.grid(row=1, column=0, sticky="nsew")
        file_scroll = ttk.Scrollbar(inputs, orient="horizontal", command=self.files.xview)
        file_scroll.grid(row=2, column=0, sticky="ew")
        self.files.configure(xscrollcommand=file_scroll.set)
        ttk.Label(inputs, text="«Da caricare» = aggiunto alla maschera, non ancora importato.",
                  style="PageMuted.TLabel").grid(row=3, column=0, sticky="w", pady=(5, 0))
        inspect_actions = ttk.Frame(inputs, style="Content.TFrame")
        inspect_actions.grid(row=4, column=0, sticky="w", pady=(7, 12))
        ttk.Button(inspect_actions, text="Anteprima selezionato", command=self.preview_material).pack(side="left")
        ttk.Button(inspect_actions, text="Rimuovi selezionato", command=self.remove_file).pack(side="left", padx=8)
        ttk.Button(inspect_actions, text="Confronta programma", command=self.show_coverage).pack(side="left")
        text_fields = ttk.Frame(inputs, style="Content.TFrame")
        text_fields.grid(row=5, column=0, sticky="ew")
        text_fields.columnconfigure(0, weight=1)
        text_fields.columnconfigure(1, weight=1)
        ttk.Label(text_fields, text="DOMANDE INCOLLATE", style="PageMuted.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 5))
        ttk.Label(text_fields, text="INDICAZIONI PER GLI SCHEMI", style="PageMuted.TLabel").grid(row=0, column=1, sticky="w", pady=(0, 5), padx=(12, 0))
        self.questions = ScrolledText(text_fields, height=4, wrap="word", undo=True, font=("Helvetica", 11))
        self.questions.grid(row=1, column=0, sticky="nsew")
        self.instructions = ScrolledText(text_fields, height=4, wrap="word", undo=True, font=("Helvetica", 11))
        self.instructions.grid(row=1, column=1, sticky="nsew", padx=(12, 0))
        out = self.output_tab
        out.columnconfigure(0, weight=1)
        out.rowconfigure(2, weight=1)
        ttk.Label(out, text="Schemi e PDF", style="PageTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(out, textvariable=self.progress, style="PageMuted.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 12))
        output_body = ttk.Frame(out, style="Content.TFrame")
        output_body.grid(row=2, column=0, sticky="nsew")
        output_body.columnconfigure(0, weight=3)
        output_body.columnconfigure(1, weight=2)
        output_body.rowconfigure(0, weight=1)
        self.output_body = output_body
        output_list = ttk.Frame(output_body, style="Card.TFrame", padding=8)
        output_list.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        output_list.columnconfigure(0, weight=1)
        output_list.rowconfigure(0, weight=1)
        self.outputs = ttk.Treeview(output_list, columns=("name", "state"), show="headings", selectmode="browse", height=5)
        self.outputs.heading("name", text="Argomento")
        self.outputs.heading("state", text="Stato")
        self.outputs.column("name", width=290)
        self.outputs.column("state", width=130, stretch=False)
        self.outputs.grid(row=0, column=0, sticky="nsew")
        output_scroll = ttk.Scrollbar(output_list, orient="vertical", command=self.outputs.yview)
        output_scroll.grid(row=0, column=1, sticky="ns")
        self.outputs.configure(yscrollcommand=output_scroll.set)
        self.outputs.bind("<Double-1>", lambda event: self.open_result())
        pilot_controls = ttk.Frame(output_body, style="Card.TFrame", padding=(16, 14))
        pilot_controls.grid(row=0, column=1, sticky="nsew")
        self.pilot_controls = pilot_controls
        self.pilot_title_label = ttk.Label(pilot_controls, text="Approvazione del primo schema", style="CardTitle.TLabel")
        self.pilot_title_label.pack(anchor="w", pady=(0, 10))
        self.pilot_notice = tk.StringVar(value="")
        self.pilot_notice_label = ttk.Label(pilot_controls, textvariable=self.pilot_notice,
                                            style="CardBody.TLabel", wraplength=260)
        self.pilot_notice_label.pack(anchor="w", fill="x", pady=(0, 12))
        pilot_actions = ttk.Frame(pilot_controls)
        pilot_actions.pack(fill="x", side="bottom")
        self.pilot_actions = pilot_actions
        self.pilot_approve_button = ttk.Button(pilot_actions, text="Approva il primo schema", command=self.approve_pilot)
        self.pilot_approve_button.pack(side="bottom", fill="x", pady=(6, 0))
        self.pilot_feedback_button = ttk.Button(pilot_actions, text="Descrivi modifiche...", command=self.feedback_pilot)
        self.pilot_feedback_button.pack(side="bottom", fill="x", pady=(6, 0))
        self.pilot_open_button = ttk.Button(pilot_actions, text="Apri il primo PDF", command=self.open_pilot)
        self.pilot_open_button.pack(side="bottom", fill="x", pady=(6, 0))
        self.pilot_more = ttk.Menubutton(pilot_controls, text="Azioni primo schema")
        self.pilot_menu = tk.Menu(self.pilot_more, tearoff=False)
        for label, command in (("Apri il primo PDF", self.open_pilot),
                               ("Descrivi modifiche...", self.feedback_pilot),
                               ("Approva il primo schema", self.approve_pilot)):
            self.pilot_menu.add_command(label=label, command=command)
        self.pilot_more.configure(menu=self.pilot_menu)
        aggregates = ttk.Frame(out, style="Card.TFrame", padding=(12, 10))
        aggregates.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(aggregates, text="Output aggregati", style="CardTitle.TLabel").pack(side="left")
        self.aggregate_notice = tk.StringVar(value="PDF unico e ZIP non ancora disponibili.")
        ttk.Label(aggregates, textvariable=self.aggregate_notice, style="CardBody.TLabel").pack(side="left", padx=(18, 0))
        controls = ttk.Frame(out, style="Content.TFrame")
        controls.grid(row=4, column=0, sticky="ew", pady=(10, 5))
        ttk.Button(controls, text="Apri PDF", command=self.open_result).pack(side="left")
        ttk.Button(controls, text="Controllo visivo", command=self.visual_check).pack(side="left", padx=8)
        ttk.Button(controls, text="Cartella PDF", command=self.open_output_folder).pack(side="left")
        regeneration = ttk.Frame(out, style="Content.TFrame")
        regeneration.grid(row=5, column=0, sticky="w")
        ttk.Label(regeneration, text="Nuove chiamate API possibili:", style="PageMuted.TLabel").pack(side="left")
        self.upgrade_button = ttk.Button(regeneration, text="Rigenera con il modello predefinito", command=self.upgrade)
        self.upgrade_button.pack(side="left", padx=(8, 0))
        ttk.Label(self.status_tab, text="Stato e batch", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(self.status_tab, text="Preparazione locale, stato remoto osservato e risultati sul Mac.",
                  style="PageMuted.TLabel").pack(anchor="w", pady=(2, 10))
        monitor = ttk.Frame(self.status_tab, style="Content.TFrame")
        monitor.pack(fill="x", pady=(0, 10))
        self.monitor_vars = {key: self.tk.StringVar(value="Scegli una materia") for key in ("local", "server", "finish")}
        self.monitor_labels = []
        for key, title in (("local", "Sul Mac: preparazione"), ("server", "OpenAI: invio e generazione"),
                           ("finish", "Sul Mac: risultati e PDF")):
            card = ttk.Frame(monitor, style="Card.TFrame", padding=(13, 12))
            card.pack(side="left", fill="both", expand=True, padx=(0, 9) if key != "finish" else 0)
            ttk.Label(card, text=title, style="CardTitle.TLabel", wraplength=190).pack(anchor="w", pady=(0, 7))
            label = ttk.Label(card, textvariable=self.monitor_vars[key], style="CardBody.TLabel", wraplength=190)
            label.pack(anchor="w")
            self.monitor_labels.append(label)
        self.status_hint = tk.StringVar(value="")
        self.status_hint_label = ttk.Label(self.status_tab, textvariable=self.status_hint,
                                           style="AttentionBadge.TLabel", wraplength=750)
        self.status_hint_label.pack(anchor="w", fill="x", pady=(0, 8))
        self.status_hint_label.pack_forget()
        monitor_actions = ttk.Frame(self.status_tab, style="Content.TFrame")
        monitor_actions.pack(fill="x", pady=(0, 10))
        self.monitor_actions = monitor_actions
        ttk.Button(monitor_actions, text="Controlla ora lo stato su OpenAI", command=self.refresh_server,
                   style="Accent.TButton").pack(side="left", padx=(0, 8))
        self.rescue_button = ttk.Button(monitor_actions, text="Search and Rescue · recupera schemi", command=self.rescue)
        self.rescue_button.pack(side="left")
        diagnostic_card = ttk.Frame(self.status_tab, style="Card.TFrame", padding=(12, 10))
        diagnostic_card.pack(fill="both", expand=True)
        ttk.Label(diagnostic_card, text="Diagnostica · registro locale", style="CardTitle.TLabel").pack(
            anchor="w", pady=(0, 7))
        self.status_text = ScrolledText(diagnostic_card, wrap="word", font=("Courier", 10),
                                        state="disabled", relief="flat")
        self.status_text.pack(fill="both", expand=True)
        ttk.Label(self.control_tab, text="Stato del lavoro", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(self.control_tab, text="Panoramica letta dai file locali della materia selezionata.",
                  style="PageMuted.TLabel").pack(anchor="w", pady=(2, 12))
        control_summary = ttk.Frame(self.control_tab, style="Content.TFrame")
        control_summary.pack(fill="x", pady=(0, 12))
        self.control_kpis = {}
        for key, title in (("pdf", "PDF PRONTI"), ("batch", "BATCH ACCETTATI"), ("incidents", "INCIDENTI")):
            card = ttk.Frame(control_summary, style="Card.TFrame", padding=(12, 10))
            card.pack(side="left", fill="x", expand=True, padx=(0, 8) if key != "incidents" else 0)
            self.control_kpis[key] = self.tk.StringVar(value="—")
            ttk.Label(card, text=title, style="CardBody.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=self.control_kpis[key], style="CardValue.TLabel").pack(anchor="w", pady=(3, 0))
        self.control_tabs = ttk.Notebook(self.control_tab)
        self.control_tabs.pack(fill="both", expand=True)
        self.control_overview = ttk.Frame(self.control_tabs, style="Content.TFrame")
        self.control_jobs = ttk.Frame(self.control_tabs, style="Content.TFrame")
        self.control_batches = ttk.Frame(self.control_tabs, style="Content.TFrame")
        self.control_performance = ttk.Frame(self.control_tabs, style="Content.TFrame")
        for page, label in ((self.control_overview, "Overview"), (self.control_jobs, "Schemi"),
                            (self.control_batches, "Batch"), (self.control_performance, "Prestazioni")):
            self.control_tabs.add(page, text=label)
        control_cards = ttk.Frame(self.control_overview, style="Content.TFrame")
        control_cards.pack(fill="x", pady=(8, 10))
        self.control_cards = control_cards
        self.control_card_frames = []
        self.control_phase = tk.StringVar(value="Scegli una materia.")
        self.control_telemetry = tk.StringVar(value="Dati non disponibili.")
        for title, variable in (("Fase corrente", self.control_phase), ("Telemetria", self.control_telemetry)):
            card = ttk.Frame(control_cards, style="Card.TFrame", padding=(16, 14))
            card.pack(side="left", fill="both", expand=True, padx=(0, 10) if title == "Fase corrente" else 0)
            self.control_card_frames.append(card)
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
            ttk.Label(card, textvariable=variable, style="CardBody.TLabel", wraplength=310,
                      justify="left").pack(anchor="w")
        self.control_text = ScrolledText(self.control_overview, wrap="word", font=("Courier", 10), state="disabled", height=5)
        self.control_text.pack(fill="both", expand=True)
        self.control_job_rows = ttk.Treeview(self.control_jobs, columns=("number", "title", "hash", "status", "retry"), show="headings", selectmode="browse")
        for key, label, width in (("number", "N°", 55), ("title", "Titolo", 310), ("hash", "Hash", 110),
                                  ("status", "Stato", 160), ("retry", "Retry", 60)):
            self.control_job_rows.heading(key, text=label)
            self.control_job_rows.column(key, width=width, stretch=key == "title")
        self.control_job_rows.pack(fill="both", expand=True)
        job_scroll = ttk.Scrollbar(self.control_jobs, orient="horizontal", command=self.control_job_rows.xview)
        job_scroll.pack(fill="x")
        self.control_job_rows.configure(xscrollcommand=job_scroll.set)
        self.control_job_rows.bind("<<TreeviewSelect>>", self.show_control_job)
        self.control_job_detail = ScrolledText(self.control_jobs, height=8, wrap="word", font=("Courier", 10), state="disabled")
        self.control_job_detail.pack(fill="x", pady=(8, 0))
        self.control_batch_rows = ttk.Treeview(self.control_batches, columns=("mode", "id", "local", "server", "checked"), show="headings")
        for key, label, width in (("mode", "Tipo", 75), ("id", "ID remoto", 220), ("local", "Stato locale", 140),
                                  ("server", "Stato osservato", 145), ("checked", "Ultimo controllo", 190)):
            self.control_batch_rows.heading(key, text=label)
            self.control_batch_rows.column(key, width=width, stretch=key == "id")
        self.control_batch_rows.pack(fill="both", expand=True)
        batch_scroll = ttk.Scrollbar(self.control_batches, orient="horizontal", command=self.control_batch_rows.xview)
        batch_scroll.pack(fill="x")
        self.control_batch_rows.configure(xscrollcommand=batch_scroll.set)
        self.control_performance_rows = ttk.Treeview(self.control_performance, columns=("stage", "success", "fail", "median", "p90", "p95", "max"), show="headings")
        for key, label, width in (("stage", "Fase locale", 210), ("success", "Riuscite", 80),
                                  ("fail", "Errori", 70), ("median", "Mediana ms", 105),
                                  ("p90", "p90 ms", 95), ("p95", "p95 ms", 95),
                                  ("max", "Massimo ms", 105)):
            self.control_performance_rows.heading(key, text=label)
            self.control_performance_rows.column(key, width=width, stretch=key == "stage")
        self.control_performance_rows.pack(fill="both", expand=True)
        performance_scroll = ttk.Scrollbar(self.control_performance, orient="horizontal", command=self.control_performance_rows.xview)
        performance_scroll.pack(fill="x")
        self.control_performance_rows.configure(xscrollcommand=performance_scroll.set)
        control_actions = ttk.Frame(self.control_tab, style="Content.TFrame")
        control_actions.pack(fill="x", pady=(10, 0))
        ttk.Button(control_actions, text="Aggiorna", command=self.refresh,
                   style="Accent.TButton").pack(side="left")
        ttk.Button(control_actions, text="Anteprima report", command=self.diagnostic_preview).pack(side="left", padx=7)
        ttk.Button(control_actions, text="Recupera PDF locali", command=self.recover_local_pdfs).pack(side="left")
        control_views = ttk.Frame(self.control_tab, style="Content.TFrame")
        control_views.pack(fill="x", pady=(7, 0))
        ttk.Button(control_views, text="Panoramica di tutte le materie", command=self.show_global_control).pack(side="left")
        ttk.Button(control_views, text="Origine delle impostazioni", command=self.show_profile_sources).pack(side="left", padx=7)
        ttk.Button(control_views, text="Errori e recupero", command=self.show_incidents).pack(side="left")
        # Reserve space for the actions before the expanding notebook takes the rest.
        self.control_tabs.pack_forget()
        control_actions.pack_forget()
        control_views.pack_forget()
        control_views.pack(side="bottom", fill="x", pady=(7, 0))
        control_actions.pack(side="bottom", fill="x", pady=(10, 0))
        self.control_tabs.pack(fill="both", expand=True)
        ttk.Label(self.illustrator_tab, text="Tavole mediche", style="PageTitle.TLabel").pack(anchor="w")
        self.illustrator_intro = ttk.Label(self.illustrator_tab,
                                           text="Proposte locali, senza API. Ogni tavola richiede revisione.",
                                           style="PageMuted.TLabel", wraplength=500)
        self.illustrator_intro.pack(anchor="w", pady=(2, 12))
        illustrator_toolbar = ttk.Frame(self.illustrator_tab, style="Content.TFrame")
        illustrator_toolbar.pack(fill="x", pady=(0, 12))
        ttk.Button(illustrator_toolbar, text="Cerca tavole utili", command=self.plan_illustrations,
                   style="Accent.TButton").pack(side="left")
        ttk.Button(illustrator_toolbar, text="Aggiungi tavola manuale", command=self.add_manual_illustration).pack(side="left", padx=8)
        ttk.Button(illustrator_toolbar, text="Disattiva/riattiva proposte", command=self.toggle_illustrations).pack(side="left", padx=8)
        self.illustrator_notice = tk.StringVar(value="")
        self.illustrator_notice_label = ttk.Label(self.illustrator_tab, textvariable=self.illustrator_notice,
                                                  style="PageMuted.TLabel", wraplength=500)
        self.illustrator_notice_label.pack(anchor="w", pady=(0, 5))
        illustrator_body = ttk.Frame(self.illustrator_tab, style="Content.TFrame")
        illustrator_body.pack(fill="both", expand=True)
        list_card = ttk.Frame(illustrator_body, style="Card.TFrame", padding=(12, 10))
        list_card.pack(side="left", fill="both", expand=True, padx=(0, 12))
        ttk.Label(list_card, text="Elenco tavole", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
        detail_card = ttk.Frame(illustrator_body, style="Card.TFrame", padding=(12, 10))
        detail_card.pack(side="left", fill="both", expand=True)
        self.illustrator_detail_title = ttk.Label(detail_card, text="Dettaglio della proposta selezionata",
                                                  style="CardTitle.TLabel")
        self.illustrator_detail_title.pack(anchor="w", pady=(0, 8))
        self.illustrations = ttk.Treeview(list_card, columns=("schema", "concept", "state"), show="headings", selectmode="browse", height=5)
        for key, title, width in (("schema", "N°", 45), ("concept", "Tavola", 195), ("state", "Stato", 155)):
            self.illustrations.heading(key, text=title)
            self.illustrations.column(key, width=width, stretch=key == "concept")
        self.illustrations.pack(fill="both", expand=True)
        illustration_scroll = ttk.Scrollbar(list_card, orient="horizontal", command=self.illustrations.xview)
        illustration_scroll.pack(fill="x")
        self.illustrations.configure(xscrollcommand=illustration_scroll.set)
        self.illustrations.bind("<<TreeviewSelect>>", self.show_illustration_details)
        self.illustration_detail = ScrolledText(detail_card, height=8, wrap="word", font=("Helvetica", 11), state="disabled",
                                                relief="flat", borderwidth=0)
        self.illustration_detail.pack(fill="both", expand=True)
        illustrator_actions = ttk.Frame(self.illustrator_tab, style="Content.TFrame")
        illustrator_actions.pack(fill="x", pady=(8, 0))
        illustration_edit = ttk.Frame(illustrator_actions, style="Content.TFrame")
        illustration_edit.pack(fill="x", pady=(0, 7))
        ttk.Button(illustration_edit, text="Crea bozza SVG", command=lambda: self.illustration_action("accepted"),
                   style="Accent.TButton").pack(side="left")
        ttk.Button(illustration_edit, text="Modifica", command=self.edit_illustration).pack(side="left", padx=8)
        ttk.Button(illustration_edit, text="Apri bozza", command=self.open_illustration).pack(side="left")
        ttk.Button(illustration_edit, text="Valida SVG", command=self.revalidate_illustration).pack(side="left", padx=8)
        illustration_review = ttk.Frame(illustrator_actions, style="Content.TFrame")
        illustration_review.pack(fill="x")
        ttk.Button(illustration_review, text="Scarta", command=lambda: self.illustration_action("rejected")).pack(side="left")
        ttk.Button(illustration_review, text="Segna revisionata", command=lambda: self.illustration_action("approved")).pack(side="left", padx=8)
        ttk.Button(illustration_review, text="Esporta PDF con tavola", command=self.export_illustration).pack(side="left")
        # Keep both action rows reachable at the minimum supported window size.
        illustrator_body.pack_forget()
        illustrator_actions.pack_forget()
        illustrator_actions.pack(side="bottom", fill="x", pady=(8, 0))
        illustrator_body.pack(fill="both", expand=True)
        footer = ttk.Frame(main, padding=(20, 10), style="Main.TFrame")
        footer.grid(row=3, column=0, sticky="ew")
        footer_info = ttk.Frame(footer)
        footer_info.pack(side="left", fill="x", expand=True)
        ttk.Label(footer_info, textvariable=self.model, style="Muted.TLabel").pack(anchor="w", pady=(0, 4))
        ttk.Label(footer_info, text="Batch: costo ridotto · Urgente: API diretta",
                  style="Muted.TLabel").pack(anchor="w")
        actions = ttk.Frame(footer)
        actions.pack(side="right")
        self.buttons = [self.rescue_button, self.pilot_feedback_button, self.pilot_approve_button]
        self.action_buttons = {}
        for title, action in [("Avvia · Batch", "now"), ("Avvia urgente", "urgent"),
                              ("Stanotte · Batch", "night"), ("Salva", "save")]:
            button = ttk.Button(actions, text=title, command=lambda a=action: self.save(a),
                                style="Accent.TButton" if action == "now" else "TButton")
            button.pack(side="right", padx=(10, 0))
            self.buttons.append(button)
            self.action_buttons[action] = button
        for value in (self.name, self.mode, self.pages):
            value.trace_add("write", lambda *args: self.mark_dirty())
        for widget in (self.questions, self.instructions):
            widget.bind("<<Modified>>", lambda event: self.text_modified(event.widget))
        self.show_mode()
        self.home_search.trace_add("write", lambda *args: self.refresh())
        root.bind("<Configure>", self.update_responsive, add="+")
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

    def update_responsive(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        compact = self.root.winfo_width() < 1120
        wrap = max(400, self.root.winfo_width() - (250 if compact else 310))
        for label in (self.notice_label, self.illustrator_intro, self.illustrator_notice_label,
                      self.status_hint_label):
            label.configure(wraplength=wrap)
        self.pilot_notice_label.configure(wraplength=max(190, int(wrap * 0.36)))
        for label in self.monitor_labels:
            label.configure(wraplength=max(155, int(wrap / 3) - 24))
        if compact == self._compact_tabs:
            return
        self._compact_tabs = compact
        self.sidebar.configure(width=186 if compact else 242)
        self.outer.columnconfigure(0, minsize=186 if compact else 242)
        for index, label in enumerate(self.compact_tab_labels if compact else self.full_tab_labels):
            self.tabs.tab(index, text=label)
        for action, short, full in (("save", "Salva", "Salva"), ("night", "Stanotte", "Stanotte · Batch"),
                                    ("urgent", "Urgente", "Avvia urgente"), ("now", "Batch", "Avvia · Batch")):
            self.action_buttons[action].configure(text=short if compact else full)
        self.home_list_frame.grid_configure(row=0, column=0, padx=(0, 0 if compact else 12))
        self.home_selected_card.grid_configure(row=1 if compact else 0, column=0 if compact else 1,
                                              sticky="ew" if compact else "nsew", pady=(10, 0) if compact else 0)
        self.home_body.rowconfigure(1, weight=0)
        self.home_body.rowconfigure(0, minsize=100 if compact else 0)
        self.home_body.columnconfigure(1, weight=0 if compact else 1)
        self.home_selected_card.configure(padding=(12, 7) if compact else (16, 14))
        self.home_summary_label.grid_configure(pady=(6, 0) if compact else (18, 5))
        if compact:
            self.home_card_badge.grid_remove()
            self.home_card_progress.grid_remove()
        else:
            self.home_card_badge.grid()
            self.home_card_progress.grid()
        self.project_rows.configure(displaycolumns=("name", "mode", "pdf", "phase") if compact else
                                    ("name", "mode", "date", "pdf", "phase", "remote"))
        self.home_card_title_label.configure(wraplength=510 if compact else 220)
        self.home_summary_label.configure(wraplength=510 if compact else 220)
        self.pilot_controls.configure(padding=(8, 7) if compact else (16, 14))
        self.pilot_title_label.configure(text="Primo schema" if compact else "Approvazione del primo schema")
        self.pilot_notice_label.configure(wraplength=175 if compact else 260)
        self.pilot_open_button.configure(text="Apri PDF" if compact else "Apri il primo PDF")
        self.pilot_feedback_button.configure(text="Modifiche..." if compact else "Descrivi modifiche...")
        self.pilot_approve_button.configure(text="Approva" if compact else "Approva il primo schema")
        if compact:
            self.pilot_actions.pack_forget()
            self.pilot_more.pack(fill="x", side="bottom", pady=(6, 0))
        else:
            self.pilot_more.pack_forget()
            self.pilot_actions.pack(fill="x", side="bottom")
        self.outputs.column("name", width=210 if compact else 290)
        self.outputs.column("state", width=90 if compact else 130)
        self.control_cards.pack_configure(pady=(4, 0) if compact else (8, 10))
        for card in self.control_card_frames:
            card.configure(padding=(9, 7) if compact else (16, 14))
        if compact:
            self.control_text.pack_forget()
        else:
            self.control_text.pack(fill="both", expand=True)
        if getattr(self, "control_view", None):
            self.control_telemetry.set(self.telemetry_summary(self.control_view, compact))
        self.illustrations.column("concept", width=150 if compact else 195)
        self.illustrations.column("state", width=100 if compact else 155)
        self.illustrator_detail_title.configure(text="Dettaglio tavola" if compact else
                                                "Dettaglio della proposta selezionata")
        if compact:
            self.home_secondary.pack_forget()
            self.home_more.pack(side="left", padx=(7, 0))
        else:
            self.home_more.pack_forget()
            self.home_secondary.pack(side="left", padx=(7, 0))
        self.show_home_summary()

    @staticmethod
    def telemetry_summary(view, compact=False):
        usage = view["observed_usage"]
        if compact:
            tokens = (f"Token: {usage['input_tokens']:,} input / {usage['output_tokens']:,} output"
                      if usage["jobs_with_usage"] else "Token: non disponibili")
            duration = "sì" if view["telemetry_available"] else "n/d"
            return tokens + f"\nDurate: {duration} · Costi: dashboard OpenAI"
        return (
            (f"Token conservati: input {usage['input_tokens']:,} · output {usage['output_tokens']:,}.\n"
             if usage["jobs_with_usage"] else "Token conservati: non disponibili.\n") +
            ("Durate locali disponibili.\n" if view["telemetry_available"] else
             "Durate locali: non disponibili.\n") +
            "Spesa fatturata: consulta la dashboard OpenAI.")

    def mark_dirty(self):
        self.dirty = True

    def make_dialog(self, title, subtitle, geometry="880x620"):
        """Create a consistent native secondary window with a light header."""
        dialog = self.tk.Toplevel(self.root)
        dialog.title(title)
        self.center_dialog(dialog, geometry)
        dialog.configure(background=COLORS["canvas"])
        shell = self.ttk.Frame(dialog, style="App.TFrame", padding=14)
        shell.pack(fill="both", expand=True)
        header = self.ttk.Frame(shell, style="SoftCard.TFrame", padding=(18, 14))
        header.pack(fill="x")
        self.ttk.Label(header, text=title, style="ModalTitle.TLabel").pack(anchor="w")
        self.ttk.Label(header, text=subtitle, style="ModalMuted.TLabel").pack(anchor="w", pady=(3, 0))
        body = self.ttk.Frame(shell, style="Main.TFrame", padding=18)
        body.pack(fill="both", expand=True)
        return dialog, body

    def center_dialog(self, dialog, geometry):
        width, height = (int(value) for value in geometry.split("x"))
        self.root.update_idletasks()
        screen_width, screen_height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width = min(width, max(640, screen_width - 40))
        height = min(height, max(520, screen_height - 80))
        x = min(max(0, self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2),
                max(0, screen_width - width))
        y = min(max(0, self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2),
                max(0, screen_height - height))
        dialog.geometry(f"{width}x{height}+{x}+{y}")

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
        self.center_dialog(dialog, "1040x720")
        dialog.minsize(760, 560)
        dialog.configure(background=COLORS["canvas"])
        dialog.transient(self.root)
        dialog.grab_set()
        values = {"name": self.tk.StringVar(), "short_title": self.tk.StringVar(),
                  "exam_date": self.tk.StringVar(), "mode": self.tk.StringVar(value="completa"),
                  "max_pages": self.tk.StringVar(value="100"), "execution": self.tk.StringVar(value="batch"),
                  "illustrator_enabled": self.tk.BooleanVar(value=False)}
        pending = {kind: [] for kind in KINDS}
        shell = self.ttk.Frame(dialog, style="App.TFrame", padding=14)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(0, weight=1)
        wizard_sidebar = self.ttk.Frame(shell, style="Sidebar.TFrame", padding=(14, 20), width=230)
        wizard_sidebar.grid(row=0, column=0, sticky="nsew")
        wizard_sidebar.grid_propagate(False)
        self.ttk.Label(wizard_sidebar, text="+ Nuova materia", style="SidebarTitle.TLabel").pack(anchor="w")
        self.ttk.Label(wizard_sidebar, text="Procedura guidata", style="SidebarMuted.TLabel").pack(anchor="w", pady=(3, 24))
        wizard_main = self.ttk.Frame(shell, style="Main.TFrame")
        wizard_main.grid(row=0, column=1, sticky="nsew")
        wizard_main.columnconfigure(0, weight=1)
        wizard_main.rowconfigure(1, weight=1)
        step_caption = self.tk.StringVar(value="PASSO 1 DI 6")
        top = self.ttk.Frame(wizard_main, padding=(24, 18))
        top.grid(row=0, column=0, sticky="ew")
        self.ttk.Label(top, textvariable=step_caption, style="Eyebrow.TLabel").pack(anchor="w")
        page_host = self.ttk.Frame(wizard_main)
        page_host.grid(row=1, column=0, sticky="nsew")
        page_host.columnconfigure(0, weight=1)
        page_host.rowconfigure(0, weight=1)
        pages = [self.ttk.Frame(page_host, padding=(24, 16)) for _ in range(6)]
        for page in pages:
            page.grid(row=0, column=0, sticky="nsew")
        step_buttons = []
        step = 0
        def set_step(index):
            nonlocal step
            step = max(0, min(5, index))
            pages[step].tkraise()
            step_caption.set(f"PASSO {step + 1} DI 6")
            for number, button in enumerate(step_buttons):
                button.configure(style="WizardActive.TButton" if number == step else "WizardStep.TButton")
            back_button.state(["disabled"] if step == 0 else ["!disabled"])
            next_button.state(["disabled"] if step == 5 else ["!disabled"])
            show_review()
        for index, title in enumerate(("Identità", "Obiettivo", "Materiali", "Produzione", "Grafica", "Riepilogo")):
            button = self.ttk.Button(wizard_sidebar, text=f"{index + 1}   {title}",
                                     command=lambda value=index: set_step(value), style="WizardStep.TButton")
            button.pack(fill="x", pady=(0, 8))
            step_buttons.append(button)
        identity = pages[0]
        identity.columnconfigure(0, weight=1)
        self.ttk.Label(identity, text="Identità della materia", style="Title.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8))
        self.ttk.Label(identity, text="Dai un nome riconoscibile al progetto. Potrai cambiarlo.",
                       style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 22))
        for index, (label, key) in enumerate((("Nome della materia", "name"), ("Titolo breve (facoltativo)", "short_title"),
                                               ("Data esame AAAA-MM-GG (facoltativa)", "exam_date"))):
            self.ttk.Label(identity, text=label, style="Muted.TLabel").grid(
                row=2 + index * 2, column=0, sticky="w", pady=(0, 5))
            self.ttk.Entry(identity, textvariable=values[key], width=55).grid(
                row=3 + index * 2, column=0, sticky="w", pady=(0, 20))
        objective = pages[1]
        self.ttk.Label(objective, text="Obiettivo di studio", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        self.ttk.Label(objective, text="Come vuoi studiare questa materia?").pack(anchor="w", pady=(0, 10))
        self.ttk.Radiobutton(objective, text="Completa", variable=values["mode"], value="completa").pack(anchor="w")
        self.ttk.Radiobutton(objective, text="Sintesi · limite di pagine totali", variable=values["mode"], value="veloce").pack(anchor="w", pady=7)
        self.ttk.Spinbox(objective, from_=1, to=1000, textvariable=values["max_pages"], width=9).pack(anchor="w", padx=25)
        self.ttk.Label(objective, text="Il limite non garantisce copertura se le fonti sono insufficienti.").pack(anchor="w", pady=(8, 14))
        self.ttk.Label(objective, text="Indicazioni per gli schemi (facoltative)").pack(anchor="w")
        instructions = ScrolledText(objective, height=7, wrap="word")
        instructions.pack(fill="both", expand=True, pady=5)
        materials = pages[2]
        self.ttk.Label(materials, text="Materiali di partenza", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        self.ttk.Label(materials, text="Seleziona più file. Puoi anche salvare una bozza e aggiungerli dopo.").pack(anchor="w", pady=(0, 12))
        counts = {}
        for kind, title in (("materiali", "Sbobine e appunti"), ("domande", "Domande d'esame"), ("programma", "Programma ufficiale")):
            bar = self.ttk.Frame(materials, style="Card.TFrame", padding=(12, 10))
            bar.pack(fill="x", pady=8)
            counter = self.tk.StringVar(value="0 file")
            counts[kind] = counter
            self.ttk.Label(bar, text=title, style="CardTitle.TLabel").pack(side="left")
            def choose(k=kind, counter_var=counter):
                paths = filedialog.askopenfilenames(parent=dialog, title="Seleziona " + k,
                    filetypes=[("File supportati", " ".join("*" + suffix for suffix in sorted(SUPPORTED))), ("Tutti i file", "*")])
                for raw in paths:
                    path = Path(raw).resolve()
                    if path not in pending[k]:
                        pending[k].append(path)
                counter_var.set(f"{len(pending[k])} file")
            self.ttk.Label(bar, textvariable=counter, style="CardBody.TLabel").pack(side="left", padx=16)
            def clear(k=kind, counter_var=counter):
                pending[k].clear()
                counter_var.set("0 file")
            self.ttk.Button(bar, text="Svuota", command=clear).pack(side="right")
            self.ttk.Button(bar, text="Aggiungi file", command=choose).pack(side="right", padx=8)
        production = pages[3]
        self.ttk.Label(production, text="Produzione degli schemi", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        self.ttk.Label(production, text="Scegli quando avviare. Salva bozza non invia nulla.").pack(anchor="w", pady=(0, 12))
        for code, label in (("batch", "Normale · Batch OpenAI"), ("urgent", "Urgente · OpenAI a costo standard"),
                            ("night", "Notturna · Batch alle 00:30, Mac acceso")):
            self.ttk.Radiobutton(production, text=label, variable=values["execution"], value=code).pack(anchor="w", pady=7)
        self.ttk.Label(production, text="Il primo PDF resta in attesa della tua approvazione. Il prezzo API non è stimato qui.",
                       wraplength=690).pack(anchor="w", pady=22)
        graphics = pages[4]
        self.ttk.Label(graphics, text="Grafica e tavole", style="Section.TLabel").pack(anchor="w", pady=(0, 12))
        self.ttk.Checkbutton(graphics, text="Abilita proposte Medical Illustrator per questa materia",
                             variable=values["illustrator_enabled"]).pack(anchor="w", pady=(0, 16))
        self.ttk.Label(graphics, text="Le bozze SVG sono schematiche, locali e gratuite. Si generano solo quando le chiedi; "
                       "richiedono revisione umana e non entrano automaticamente nel PDF.", wraplength=690).pack(anchor="w")
        self.ttk.Label(graphics, text="Le immagini già presenti nelle fonti continuano a seguire il flusso esistente.",
                       wraplength=690).pack(anchor="w", pady=12)
        review = pages[5]
        self.ttk.Label(review, text="Controlla prima di iniziare", style="Section.TLabel").pack(anchor="w", pady=(0, 10))
        review_text = ScrolledText(review, wrap="word", state="disabled", height=15)
        review_text.pack(fill="both", expand=True)
        def show_review(_event=None):
            if step != 5:
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
        buttons = self.ttk.Frame(wizard_main, padding=(24, 10), style="Main.TFrame")
        buttons.grid(row=2, column=0, sticky="ew")
        back_button = self.ttk.Button(buttons, text="Indietro", command=lambda: set_step(step - 1))
        back_button.pack(side="left")
        next_button = self.ttk.Button(buttons, text="Avanti", command=lambda: set_step(step + 1))
        next_button.pack(side="left", padx=8)
        def finish(start):
            if start and step != 5:
                set_step(5)
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
        self.ttk.Button(buttons, text="Avvia dopo il salvataggio", command=lambda: finish(True),
                        style="Accent.TButton").pack(side="right")
        self.ttk.Button(buttons, text="Salva bozza", command=lambda: finish(False)).pack(side="right", padx=8)
        set_step(0)

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

    def show_home_summary(self, event=None):
        selection = self.project_rows.selection()
        card = self.home_card_data.get(selection[0]) if selection else None
        if not card:
            self.home_card_title.set("Scegli una materia")
            self.home_card_phase.set("Nessuna selezione")
            self.home_card_summary.set("Seleziona una riga per vedere stato e PDF correnti.")
            self.home_card_badge.configure(style="MutedBadge.TLabel")
            self.home_card_progress.grid_remove()
            return
        phase = card["phase"]
        attention = phase.startswith("Invio incerto") or phase.startswith("Primo PDF")
        quiet = card["archived"] or phase.startswith("Bozza")
        self.home_card_title.set(("★  " if card["favorite"] else "") + card["name"])
        self.home_card_phase.set(phase)
        self.home_card_badge.configure(style="AttentionBadge.TLabel" if attention else
                                       "MutedBadge.TLabel" if quiet else "Badge.TLabel")
        checked = (card["last_remote_check"] or "mai verificato").replace("T", " ")[:16]
        count = f"{card['ready']} / {card['total']} schemi correnti" if card["total"] else "Piano non ancora creato"
        mode = "Sintesi" if card["mode"] == "veloce" else "Completa"
        if self.root.winfo_width() < 1120:
            compact_count = f"{card['ready']}/{card['total']} PDF" if card["total"] else "Piano da creare"
            exam_date = " · Esame " + card["exam_date"] if card["exam_date"] else ""
            summary = mode + exam_date + " · " + compact_count + " · OpenAI: " + checked
        else:
            summary = ("Modalità: " + mode + "\nEsame: " + (card["exam_date"] or "non disponibile") +
                       "\nPDF pronti: " + count + "\nVerifica OpenAI: " + checked)
        self.home_card_summary.set(summary)
        if card["total"] and self.root.winfo_width() >= 1120:
            self.home_card_progress.configure(maximum=card["total"], value=card["ready"])
            self.home_card_progress.grid()
        else:
            self.home_card_progress.grid_remove()

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
        dialog, frame = self.make_dialog("Versioni PDF · " + selection[0],
                                         "Versioni correnti e storiche già presenti sul Mac.", "960x570")
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
        actions = self.ttk.Frame(frame)
        actions.pack(fill="x", pady=(8, 0))
        self.ttk.Button(actions, text="Chiudi", command=dialog.destroy).pack(side="right")
        self.ttk.Button(actions, text="Apri versione selezionata", command=open_selected,
                        style="Accent.TButton").pack(side="right", padx=(0, 8))

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
            cards = arrange_cards(project_cards(self.app, rows), self.home_visibility.get(), self.home_order.get())
            self.home_card_data = {card["slug"]: card for card in cards}
            for card in cards:
                phase = card["phase"]
                tag = ("attention" if phase.startswith(("Invio incerto", "Primo PDF")) else
                       "success" if card["total"] and card["ready"] == card["total"] else "inactive")
                self.project_rows.insert("", "end", iid=card["slug"],
                                         values=(("★ " if card["favorite"] else "") + card["name"],
                                                 "Sintesi" if card["mode"] == "veloce" else "Completa",
                                                 card["exam_date"] or "—", f"{card['ready']}/{card['total']}",
                                                 phase, card["last_remote_check"] or "mai"), tags=(tag,))
            preferred = selected_project[0] if selected_project else self.session.selected
            if preferred and self.project_rows.exists(preferred):
                self.project_rows.selection_set(preferred)
            elif cards:
                self.project_rows.selection_set(cards[0]["slug"])
            self.show_home_summary()
            detail = self.app.detail(self.session.selected) if self.session.selected else None
            status = overview(self.app.root(self.session.selected), detail["busy"]) if detail else None
            for key, variable in self.monitor_vars.items():
                variable.set(brief_status(status[key]) if status else "Scegli una materia")
            hint = ""
            if status and "motore locale fermo" in status["local"] and "lavori accettati" in status["server"]:
                hint = "Il Mac è fermo, ma un batch accettato può continuare su OpenAI. Il PDF sarà preparato al riavvio."
            elif status and "invio incerto" in status["server"].lower():
                hint = "Invio remoto non confermato. Verifica prima di riprovare per evitare un doppio costo."
            self.status_hint.set(hint)
            if hint and not self.status_hint_label.winfo_manager():
                self.status_hint_label.pack(before=self.monitor_actions, anchor="w", fill="x", pady=(0, 8))
            elif not hint and self.status_hint_label.winfo_manager():
                self.status_hint_label.pack_forget()
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
                area = {"materiali": "Sbobine", "domande": "Domande", "programma": "Programma"}.get(file["kind"], file["kind"])
                item = self.files.insert("", "end", values=(area, file["name"],
                                                            readable_size(file["size"]), label))
                self.file_rows[item] = (file["kind"], file["name"], False)
            for kind, paths in self.session.pending.items():
                for path in paths:
                    area = {"materiali": "Sbobine", "domande": "Domande", "programma": "Programma"}.get(kind, kind)
                    item = self.files.insert("", "end", values=(area, path.name,
                                                                readable_size(path.stat().st_size), "Da caricare"))
                    self.file_rows[item] = (kind, path, True)
            for kind, variable in self.material_count_vars.items():
                count = sum(file["kind"] == kind for file in (detail or {}).get("files", [])) + len(self.session.pending[kind])
                variable.set(f"{count} file")
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
                for index, enabled in enumerate((bool(review.get("pdf")), ready, ready)):
                    self.pilot_menu.entryconfigure(index, state="normal" if enabled else "disabled")
            else:
                self.pilot_notice.set("")
                for button in (self.pilot_open_button, self.pilot_feedback_button, self.pilot_approve_button):
                    button.state(["disabled"])
                for index in range(3):
                    self.pilot_menu.entryconfigure(index, state="disabled")
            output = report.get("exports", {})
            phase = report.get("input_status") or "Materiali salvati"
            self.progress.set(str(phase) + (" · " + str(output["pages"]) + " pagine" if output.get("pages") else ""))
            ready_exports = [label for key, label in (("combined", "PDF unico"), ("archive", "ZIP")) if output.get(key)]
            self.aggregate_notice.set("Pronti: " + " e ".join(ready_exports) if ready_exports else
                                      "PDF unico e ZIP non ancora disponibili.")
            for job in report.get("planning", []):
                self.outputs.insert("", "end", values=("Lettura dei materiali" if job.get("kind") == "analysis" else
                                                         "Organizzazione degli schemi", display_job_status(job["status"])))
            for row in report.get("rows", []):
                item = self.outputs.insert("", "end", values=(str(row["numero"])+" - "+row["title"],
                                                             display_job_status(row["status"])))
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
            counts = control_view["counts"] if control_view else None
            self.control_kpis["pdf"].set(f"{counts['pdf_ok']} / {counts['total']}" if counts else "—")
            self.control_kpis["batch"].set(str(counts["accepted_batches"]) if counts else "—")
            self.control_kpis["incidents"].set(str(len(control_view["incidents"])) if control_view else "—")
            if control_view:
                if control_view["pilot"]["ready_for_review"]:
                    phase_label = "Primo PDF pronto: approvazione richiesta."
                elif control_view["incidents"]:
                    phase_label = f"{len(control_view['incidents'])} avvisi o errori da verificare."
                else:
                    phase_label = control_view["summary"]["local"]
                self.control_phase.set(phase_label)
                self.control_telemetry.set(self.telemetry_summary(control_view, self._compact_tabs))
            else:
                self.control_phase.set("Scegli una materia.")
                self.control_telemetry.set("Dati non disponibili.")
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
                                                  values=(row["schema_number"], row["concept_title"],
                                                          illustration_status_label(row["status"])))
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

    def show_global_control(self):
        try:
            body = format_global_snapshot(global_snapshot(self.app))
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.notice.set("Panoramica non disponibile: " + str(error))
            return
        dialog, frame = self.make_dialog("Control Room · tutte le materie",
                                         "Panoramica letta dagli stati locali, senza nuove generazioni.", "880x620")
        from tkinter.scrolledtext import ScrolledText
        area = ScrolledText(frame, wrap="word", font=("Helvetica", 11), relief="flat")
        area.pack(fill="both", expand=True)
        area.insert("1.0", body)
        area.configure(state="disabled")

    def show_profile_sources(self):
        if not self.session.selected:
            self.notice.set("Scegli una materia per vedere le impostazioni effettive.")
            return
        try:
            rows = effective_profile_sources(self.app.root(self.session.selected), self.app.project)
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.notice.set("Impostazioni non disponibili: " + str(error))
            return
        dialog, frame = self.make_dialog("Impostazioni effettive · " + self.session.selected,
                                         "Valori applicati alla materia e origine delle impostazioni.", "760x610")
        from tkinter.scrolledtext import ScrolledText
        area = ScrolledText(frame, wrap="word", font=("Helvetica", 11), relief="flat")
        area.pack(fill="both", expand=True)
        area.insert("1.0", format_effective_profile(rows))
        area.configure(state="disabled")

    def preview_material(self):
        selection = self.files.selection()
        if not selection or not self.session.selected:
            self.notice.set("Seleziona un file nei Materiali.")
            return
        kind, name, pending = self.file_rows[selection[0]]
        if pending:
            row = {"name": name.name, "format": name.suffix.lower(), "bytes": name.stat().st_size,
                   "pages": None, "images": None, "sha256": None, "ocr_quality": "non disponibile",
                   "error": None, "status": "Da caricare", "text_preview": ""}
        else:
            row = material_detail(self.app.root(self.session.selected), kind, name)
        dialog, frame = self.make_dialog("Dettaglio del materiale", "Informazioni locali sul file selezionato.", "900x650")
        metadata = self.ttk.Frame(frame, style="Card.TFrame", padding=(16, 14))
        metadata.pack(fill="x", pady=(0, 12))
        metadata.columnconfigure(1, weight=1)
        metadata.columnconfigure(3, weight=1)
        self.ttk.Label(metadata, text="Informazioni sul file", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))
        fields = (("Nome", row["name"]), ("Formato", row["format"]),
                  ("Dimensione", readable_size(row["bytes"])), ("Pagine", row["pages"] or "non disponibili"),
                  ("Immagini", row["images"] if row["images"] is not None else "non disponibili"),
                  ("SHA-256", row["sha256"] or "non disponibile"),
                  ("OCR", row["ocr_quality"]), ("Errore", row["error"] or "nessuno"))
        for index, (label, value) in enumerate(fields):
            col = (index % 2) * 2
            line = index // 2 + 1
            self.ttk.Label(metadata, text=label, style="CardBody.TLabel").grid(row=line, column=col, sticky="nw", pady=5)
            self.ttk.Label(metadata, text=str(value), style="CardTitle.TLabel", wraplength=270).grid(
                row=line, column=col + 1, sticky="nw", padx=(8, 14), pady=5)
        state_style = ("AttentionBadge.TLabel" if row["status"] in {"Da caricare", "failed"} else "Badge.TLabel")
        self.ttk.Label(frame, text="Stato: " + str(row["status"]), style=state_style).pack(anchor="w", pady=(0, 12))
        preview_card = self.ttk.Frame(frame, style="Card.TFrame", padding=(14, 12))
        preview_card.pack(fill="both", expand=True)
        self.ttk.Label(preview_card, text="Campione del testo estratto", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
        from tkinter.scrolledtext import ScrolledText
        area = ScrolledText(preview_card, wrap="word", font=("Helvetica", 11), height=6, relief="flat")
        area.pack(fill="both", expand=True)
        area.insert("1.0", ("Salva la materia prima di leggere l'anteprima estratta." if pending else
                             row["text_preview"] or "Testo non disponibile."))
        area.configure(state="disabled")
        self.ttk.Button(frame, text="Chiudi", command=dialog.destroy).pack(anchor="e", pady=(12, 0))

    def show_coverage(self):
        if not self.session.selected:
            self.notice.set("Scegli una materia prima di confrontare il programma.")
            return
        try:
            result = compare_coverage(self.app.root(self.session.selected))
        except Exception as error:
            self.notice.set("Confronto non disponibile: " + str(error))
            return
        dialog, frame = self.make_dialog("Programma e fonti", "Corrispondenza lessicale indicativa: non certifica la copertura.", "960x620")
        self.ttk.Label(frame, text=result["message"], wraplength=850).pack(anchor="w", pady=(0, 12))
        table = self.ttk.Treeview(frame, columns=("topic", "hint", "sources", "questions"),
                                  show="headings", selectmode="browse", height=8)
        for key, title, width in (("topic", "Argomento", 310), ("hint", "Indizio", 240),
                                  ("sources", "File materiali", 115), ("questions", "Domande", 100)):
            table.heading(key, text=title)
            table.column(key, width=width, stretch=key in {"topic", "hint"})
        table.pack(fill="both", expand=True)
        for index, row in enumerate(result["rows"]):
            table.insert("", "end", iid=str(index), values=(row["topic"], row["hint"],
                                                          row["source_count"], row["question_count"]))
        sources = self.tk.StringVar(value="Seleziona un argomento per vedere le fonti nominate." if result["rows"] else
                                     "Nessuna riga confrontabile al momento.")
        self.ttk.Label(frame, textvariable=sources, wraplength=850).pack(anchor="w", pady=(10, 0))
        def show_sources(_event=None):
            selected = table.selection()
            if selected:
                names = result["rows"][int(selected[0])]["source_names"]
                sources.set("Fonti nominate: " + ("; ".join(names) if names else "nessuna"))
        table.bind("<<TreeviewSelect>>", show_sources)
        self.ttk.Button(frame, text="Chiudi", command=dialog.destroy).pack(anchor="e", pady=(12, 0))

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
        self.illustrator_notice.set("SVG verificato tecnicamente. Stato: " + illustration_status_label(row["status"]) + ". Ricontrolla la correttezza medica.")
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

    def show_incidents(self):
        view = getattr(self, "control_view", None)
        if not view:
            self.notice.set("Scegli una materia per consultare errori e recupero.")
            return
        incidents = view["incidents"]
        dialog, frame = self.make_dialog("Errori e recupero",
                                         "Stati locali osservati: nessun reinvio remoto automatico.", "920x610")
        if incidents:
            heading = ("Invio remoto non confermato. Verifica prima di riprovare."
                       if any(item.get("kind") == "batch" or "uncertain" in str(item.get("kind", "")) for item in incidents)
                       else f"{len(incidents)} avvisi o errori richiedono attenzione.")
        else:
            heading = "Nessun incidente rilevato nei dati locali correnti."
        self.ttk.Label(frame, text=heading, style="AttentionBadge.TLabel" if incidents else "Badge.TLabel").pack(
            anchor="w", pady=(0, 12))
        table = self.ttk.Treeview(frame, columns=("kind", "target", "status", "detail"),
                                  show="headings", selectmode="browse", height=5)
        for key, title, width in (("kind", "Tipo", 130), ("target", "Schema", 80),
                                  ("status", "Stato", 150), ("detail", "Dettaglio", 420)):
            table.heading(key, text=title)
            table.column(key, width=width, stretch=key == "detail")
        table.pack(fill="both", expand=True)
        for index, item in enumerate(incidents):
            table.insert("", "end", iid=str(index), values=(item.get("kind") or "—", item.get("number") or "—",
                                                         item.get("status") or "—", item.get("detail") or "—"))
        explanation = self.tk.StringVar(value="Seleziona una riga per leggere il dettaglio e il passo consigliato.")
        self.ttk.Label(frame, textvariable=explanation, wraplength=820).pack(anchor="w", pady=(10, 0))
        def show_selected(_event=None):
            selected = table.selection()
            if selected:
                item = incidents[int(selected[0])]
                explanation.set("Dettaglio: " + str(item.get("detail") or "non disponibile") +
                                "\nPasso consigliato: " + str(item.get("suggestion") or
                                "Controlla gli stati prima di decidere se riprendere."))
        table.bind("<<TreeviewSelect>>", show_selected)
        actions = self.ttk.Frame(frame)
        actions.pack(fill="x", pady=(16, 0))
        self.ttk.Button(actions, text="Controlla stato OpenAI", command=self.refresh_server,
                        style="Accent.TButton").pack(side="left")
        self.ttk.Button(actions, text="Recupera solo PDF locali", command=self.recover_local_pdfs).pack(side="left", padx=8)
        self.ttk.Button(actions, text="Anteprima report diagnostico", command=self.diagnostic_preview).pack(side="left")
        self.ttk.Button(actions, text="Chiudi", command=dialog.destroy).pack(side="right")

    def diagnostic_preview(self):
        if not self.session.selected:
            self.notice.set("Scegli una materia prima di creare il report.")
            return
        root = self.app.root(self.session.selected)
        sections = build_sections(root)
        dialog, area = self.make_dialog("Anteprima report diagnostico",
                                        "Verifica il contenuto prima dell'esportazione. Nessun dato viene inviato.",
                                        "920x650")
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
        self.ttk.Button(area, text="Esporta ZIP dopo la verifica", command=save,
                        style="Accent.TButton").pack(anchor="e", pady=(8, 0))

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
            self.illustrator_notice.set(f"{row['concept_title']}: {illustration_status_label(row['status'])}. Nessun PDF modificato.")
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
        dialog, frame = self.make_dialog("Revisione tavola · " + row["concept_title"],
                                         "Il controllo dell'utente non è una validazione clinica specialistica.",
                                         "1040x720")
        dialog.transient(self.root)
        dialog.grab_set()
        body = self.ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        left = self.ttk.Frame(body, style="Card.TFrame", padding=(14, 12))
        left.pack(side="left", fill="both", expand=True, padx=(0, 12))
        right = self.ttk.Frame(body, style="Card.TFrame", padding=(14, 12))
        right.pack(side="left", fill="both", expand=True)
        self.ttk.Label(left, text="Anteprima SVG", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 10))
        try:
            png = preview_svg(self.app.root(self.session.selected), selection[0])
            picture = self.tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
            divisor = max(1, (max(picture.width(), picture.height()) + 449) // 450)
            if divisor > 1:
                picture = picture.subsample(divisor)
            preview = self.ttk.Label(left, image=picture)
            preview.image = picture
            preview.pack(fill="both", expand=True)
        except (ValueError, OSError, self.tk.TclError) as error:
            self.ttk.Label(left, text="Anteprima non disponibile: " + str(error),
                           wraplength=400).pack(fill="both", expand=True)
        self.ttk.Label(left, text="Didascalia: " + str(row.get("proposed_caption") or "non disponibile"),
                       style="CardBody.TLabel", wraplength=430).pack(anchor="w", pady=(10, 0))
        self.ttk.Label(right, text="Checklist medica e di utilizzo", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 6))
        self.ttk.Label(right, text="Confronta la bozza con le fonti. Lascia in revisione ciò che non puoi verificare.",
                       style="CardBody.TLabel", wraplength=400).pack(anchor="w", pady=(0, 10))
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
            self.ttk.Checkbutton(right, text=labels[key], variable=checks[key]).pack(anchor="w", pady=8)
        info = self.tk.StringVar()
        self.ttk.Label(right, textvariable=info, style="AttentionBadge.TLabel", wraplength=400).pack(anchor="w", pady=(12, 0))
        def confirm():
            review = {key: variable.get() for key, variable in checks.items()}
            if not all(review.values()):
                info.set("Completa ogni controllo oppure lascia la tavola in revisione.")
                return
            dialog.grab_release()
            dialog.destroy()
            self.illustration_action("approved", review=review)
        actions = self.ttk.Frame(frame)
        actions.pack(fill="x", pady=(12, 0))
        self.ttk.Button(actions, text="Apri bozza", command=self.open_illustration).pack(side="left")
        self.ttk.Button(actions, text="Lascia in revisione", command=dialog.destroy).pack(side="right")
        self.ttk.Button(actions, text="Registra revisione", command=confirm,
                        style="Accent.TButton").pack(side="right", padx=(0, 8))

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
        dialog, frame = self.make_dialog("Anteprima tavola · " + row["concept_title"],
                                         "Bozza schematica da confrontare con le fonti.", "820x620")
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

    def open_manual(self):
        resource = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
        self.open_path(resource / "docs/Manuale_Utente_Schemi_Esami.pdf")

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
        dialog, frame = self.make_dialog("Controllo visivo · " + path.name,
                                         "Gli avvisi richiedono una verifica a vista e non modificano PDF_OK.", "1020x760")
        visual_body = self.ttk.Frame(frame)
        visual_body.pack(fill="both", expand=True)
        left = self.ttk.Frame(visual_body, style="Card.TFrame", padding=(14, 12))
        left.pack(side="left", fill="both", padx=(0, 12))
        self.ttk.Label(left, text="Pagine", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
        right = self.ttk.Frame(visual_body, style="Card.TFrame", padding=(14, 12))
        right.pack(side="left", fill="both", expand=True)
        self.ttk.Label(right, text="Anteprima", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
        pages = self.tk.Listbox(left, width=32, exportselection=False, borderwidth=0,
                                highlightthickness=0, selectbackground=COLORS["teal_soft"],
                                selectforeground=COLORS["ink"], activestyle="none")
        pages.pack(fill="both", expand=True)
        def page_label(item):
            decision = (reviews.get(str(item["page"])) or {}).get("decision")
            marker = " [verificata]" if decision == "verified" else " [falso positivo annotato]" if decision == "false_positive" else ""
            return f"Pagina {item['page']}{marker} · " + ("; ".join(item["warnings"]) if item["warnings"] else "Nessun avviso")
        for item in result["pages"]:
            pages.insert("end", page_label(item))
        preview = self.ttk.Label(right, text="Seleziona una pagina per vedere l'anteprima.")
        preview.pack(fill="both", expand=True)
        review_state = self.tk.StringVar(value="Nessuna pagina selezionata.")
        self.ttk.Label(right, textvariable=review_state, wraplength=500).pack(anchor="w")
        def show(_event=None):
            choice = pages.curselection()
            if not choice:
                return
            saved = reviews.get(str(choice[0] + 1)) or {}
            review_state.set("Revisione visiva: " + (saved.get("decision") or "non registrata")
                             + (" · " + saved["note"] if saved.get("note") else ""))
            import fitz
            try:
                stat = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != (result["file_size"], result["file_mtime_ns"]):
                    raise ValueError("Il PDF è cambiato; chiudi e ripeti il controllo visivo.")
                with fitz.open(path) as document:
                    page = document[choice[0]]
                    if page.rect.width <= 0 or page.rect.height <= 0:
                        raise ValueError("Dimensioni della pagina non valide.")
                    scale = min(1.0, 900 / page.rect.width, 400 / page.rect.height)
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                    data = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
            except (OSError, RuntimeError, ValueError, IndexError) as error:
                preview.configure(image="", text="Anteprima non disponibile: " + str(error))
                preview.image = None
                return
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
        from tkinter import messagebox
        if not messagebox.askyesno("Rigenerazione con il profilo globale",
                                   "La materia userà il profilo globale e potrà inviare nuove richieste OpenAI a pagamento. Continuare?",
                                   parent=self.root):
            return
        self.async_task(self.session.upgrade, lambda value: self.refresh(), "Rigenerazione con il modello predefinito…")

    def settings(self):
        from tkinter import ttk
        dialog, body = self.make_dialog("Impostazioni del modello", "Profilo API predefinito per le nuove materie.", "800x670")
        dialog.transient(self.root)
        frame = ttk.Frame(body, padding=18, style="Card.TFrame")
        frame.pack(fill="x")
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Profilo API predefinito", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
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
        ttk.Button(frame, text="Salva modello predefinito", command=save,
                   style="Accent.TButton").grid(row=9, column=1, sticky="e")
        note = ttk.Frame(body, style="SoftCard.TFrame", padding=12)
        note.pack(fill="x", pady=(12, 0))
        ttk.Label(note, text="Le materie esistenti conservano il proprio profilo. La compatibilità del modello va verificata.",
                  style="ModalMuted.TLabel", wraplength=700).pack(anchor="w")
        links = ttk.Frame(body)
        links.pack(fill="x", pady=(14, 0))
        ttk.Button(links, text="Chiave OpenAI", command=self.set_api_key).pack(side="left")
        ttk.Button(links, text="Aiuto · manuale PDF", command=self.open_manual).pack(side="left", padx=8)

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
        dialog, body = self.make_dialog("Chiave OpenAI", "Campo protetto: la chiave viene salvata nel Portachiavi del Mac.",
                                        "620x310")
        dialog.transient(self.root)
        dialog.grab_set()
        key = self.tk.StringVar()
        info = self.tk.StringVar(value="La chiave non viene mostrata nelle schermate o nei report.")
        self.ttk.Label(body, text="Incolla la chiave API", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 10))
        entry = self.ttk.Entry(body, textvariable=key, show="•")
        entry.pack(fill="x")
        entry.focus_set()
        self.ttk.Label(body, textvariable=info, wraplength=540).pack(anchor="w", pady=(12, 0))
        actions = self.ttk.Frame(body)
        actions.pack(fill="x", side="bottom")
        self.ttk.Button(actions, text="Annulla", command=dialog.destroy).pack(side="right")
        def save():
            value = key.get().strip()
            if not value:
                info.set("Incolla una chiave prima di salvare.")
                return
            if self.local_busy:
                info.set("Attendi la fine dell'operazione in corso.")
                return
            key.set("")
            dialog.grab_release()
            dialog.destroy()
            self.async_task(lambda: save_api_key(value),
                            lambda _: self.notice.set("Chiave OpenAI salvata nel Portachiavi."),
                            "Salvataggio della chiave nel Portachiavi…")
        self.ttk.Button(actions, text="Salva nel Portachiavi", command=save,
                        style="Accent.TButton").pack(side="right", padx=(0, 8))


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
