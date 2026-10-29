from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import PROJECT, create_exam, digest, exam_dir, load_config, read_json, write_json
from ingest import SUPPORTED
from model_settings import MODEL_KEYS, save_default_profile
from pipeline import Pipeline
from remote import OpenAIHTTP
from rescue import search_and_rescue

KINDS = {"materiali": "sbobine", "domande": "domande", "programma": "programma"}
MAX_FILE_BYTES = 512 * 1024 * 1024
KEYCHAIN_SERVICE = "schemi-esami-v2.openai"


def api_key():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    for service in (KEYCHAIN_SERVICE, "schemi-esami-v1.openai", "OPENAI_API_KEY"):
        found = subprocess.run(["security", "find-generic-password", "-a", getpass.getuser(), "-s", service, "-w"],
                               capture_output=True, text=True)
        if found.returncode == 0 and found.stdout.strip():
            return found.stdout.strip()
    return ""


def save_api_key(key):
    key = key.strip()
    if not key or "\n" in key or "\r" in key:
        raise ValueError("Inserisci una chiave API valida.")
    # -w without an argument prompts via stdin; the secret is not exposed in argv.
    completed = subprocess.run(["security", "add-generic-password", "-U", "-a", getpass.getuser(),
                                "-s", KEYCHAIN_SERVICE, "-w"], input=key + "\n", capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError("Salvataggio nel Portachiavi non riuscito: " + completed.stderr.strip()[:500])


def filename(name):
    if not name or Path(name).name != name or name in {".", ".."} or name.startswith((".", "~$")) or any(c in name for c in "\\\r\n\x00"):
        raise ValueError("Nome del file non valido.")
    if len(name.encode()) > 230:
        raise ValueError("Il nome del file è troppo lungo.")
    return name


@contextlib.contextmanager
def edit_lock(root):
    with (root / "state/pipeline.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BlockingIOError("Il programma sta leggendo o impaginando questa materia. Attendi e riprova.")
        yield


class App:
    def __init__(self, project=PROJECT, api_factory=None):
        self.project = Path(project)
        (self.project / "esami").mkdir(exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.api_factory = api_factory or self.default_api
        self.workers, self.errors, self.wakes = {}, {}, {}
        self.mutex = threading.Lock()
        self.stop = threading.Event()

    def default_api(self, cfg):
        return OpenAIHTTP(api_key(), cfg["api_base"])

    def root(self, slug):
        return exam_dir(slug, self.project)

    def defaults(self):
        config = read_json(self.project / "config/pipeline.json")
        return {key: config.get(key) for key in MODEL_KEYS}

    def detail(self, slug):
        root = self.root(slug)
        exam = read_json(root / "esame.json")
        files = []
        for kind, folder in KINDS.items():
            for path in sorted((root / "input" / folder).rglob("*")):
                if path.is_file() and not path.name.startswith((".", "~$")):
                    files.append({"kind": kind, "name": str(path.relative_to(root / "input" / folder)), "size": path.stat().st_size})
        report = read_json(root / "output/reports/pipeline_report.json", {"rows": [], "planning": [], "exports": {}})
        pasted = root / "input/domande/domande_incollate.txt"
        return {"slug": slug, "exam": exam, "files": files, "questions_text": pasted.read_text(encoding="utf-8") if pasted.exists() else "", "report": report, "plan": read_json(root / "output/reports/piano.json", None), "pending_changes": exam.get("revision", 0) > report.get("revision", -1), "busy": self.workers.get(slug) is not None and self.workers[slug].is_alive(), "error": self.errors.get(slug)}

    def list_exams(self):
        rows = []
        for root in sorted((self.project / "esami").iterdir()):
            if not (root / "esame.json").is_file():
                continue
            detail = self.detail(root.name)
            rows.append({"slug": root.name, "name": detail["exam"]["nome"], "mode": detail["exam"].get("modalita", "completa"), "status": detail["report"].get("input_status"), "ready": sum(row["status"] == "PDF_OK" for row in detail["report"]["rows"]), "total": len(detail["report"]["rows"]), "busy": detail["busy"], "scheduled": detail["exam"].get("attivo", False), "archived": detail["exam"].get("archived", False), "favorite": detail["exam"].get("favorite", False), "exam_date": detail["exam"].get("data_esame")})
        return rows

    def save_exam(self, data):
        name = str(data.get("name", "")).strip()
        if not name or len(name) > 160:
            raise ValueError("Inserisci un nome della materia entro 160 caratteri.")
        mode = data.get("mode", "completa")
        pages = int(data.get("max_pages") or 0) if mode == "veloce" else None
        if mode not in {"completa", "veloce"} or (mode == "veloce" and not 1 <= pages <= 1000):
            raise ValueError("Nella modalità veloce indica un massimo di pagine fra 1 e 1000.")
        short = None
        if "short_title" in data:
            short = str(data["short_title"] or "").strip()
            if len(short) > 80:
                raise ValueError("Il titolo breve non può superare 80 caratteri.")
        raw_date = None
        if "exam_date" in data:
            raw_date = str(data["exam_date"] or "").strip()
            if raw_date:
                try:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
                        raise ValueError("Formato non valido")
                    date.fromisoformat(raw_date)
                except ValueError as error:
                    raise ValueError("La data dell'esame deve essere nel formato AAAA-MM-GG.") from error
        root = self.root(data["slug"]) if data.get("slug") else create_exam(name, self.project)
        with edit_lock(root):
            exam = read_json(root / "esame.json")
            before = {key: exam.get(key) for key in ("nome", "titolo_breve", "data_esame", "modalita", "max_pagine", "istruzioni", "execution_mode", "start_policy")}
            if exam.get("archived") and data.get("active"):
                raise ValueError("La materia è archiviata. Riattivala prima di avviare la produzione.")
            exam.update(nome=name, modalita=mode, max_pagine=pages, attivo=bool(data.get("active", False)), istruzioni=str(data.get("instructions", ""))[:20000])
            if "short_title" in data:
                exam["titolo_breve"] = short
            if "exam_date" in data:
                exam["data_esame"] = raw_date or None
            if data.get("execution_mode") is not None:
                if data["execution_mode"] not in {"batch", "urgent"}:
                    raise ValueError("Tipo di invio non valido.")
                exam["execution_mode"] = data["execution_mode"]
            if data.get("start_policy") is not None:
                if data["start_policy"] not in {"manual", "now", "night"}:
                    raise ValueError("Avvio non valido.")
                exam["start_policy"] = data["start_policy"]
            if data.get("night_due_at"):
                exam["night_due_at"] = data["night_due_at"]
            changed = before != {key: exam.get(key) for key in before}
            if "questions_text" in data:
                target = root / "input/domande/domande_incollate.txt"
                text = str(data["questions_text"]).strip()
                changed |= text != (target.read_text(encoding="utf-8").strip() if target.exists() else "")
                if text:
                    temporary = target.with_suffix(".tmp")
                    temporary.write_text(text + "\n", encoding="utf-8")
                    temporary.replace(target)
                elif target.exists():
                    self.archive_input(root, target)
            if changed:
                exam["revision"] = exam.get("revision", 0) + 1
            write_json(root / "esame.json", exam)
        return root.name

    def archive_input(self, root, path):
        destination = root / "state/input_archivio" / (digest(path)[:16] + "-" + path.name)
        destination.parent.mkdir(exist_ok=True)
        shutil.copy2(path, destination)
        path.unlink()

    def upload(self, slug, kind, name, stream, length):
        root = self.root(slug)
        if kind not in KINDS:
            raise ValueError("Area di caricamento non valida.")
        name = filename(name)
        if Path(name).suffix.lower() not in SUPPORTED:
            raise ValueError("Formato non supportato: " + Path(name).suffix + ". Converti DOC/XLS/PPT in DOCX/XLSX/PPTX.")
        if not 0 < length <= MAX_FILE_BYTES:
            raise ValueError("Il file deve essere non vuoto e non superare 512 MB.")
        folder = root / "input" / KINDS[kind]
        folder.mkdir(exist_ok=True)
        with edit_lock(root):
            temporary = root / "state" / ("upload-" + secrets.token_hex(12) + ".tmp")
            try:
                remaining = length
                with temporary.open("wb") as output:
                    while remaining:
                        block = stream.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ValueError("Caricamento interrotto: il file non è completo.")
                        output.write(block)
                        remaining -= len(block)
                    output.flush()
                    os.fsync(output.fileno())
                destination = folder / name
                if destination.exists() and digest(destination) == digest(temporary):
                    return {"name": destination.name, "duplicate": True}
                suffix = 2
                while destination.exists():
                    destination = folder / f"{Path(name).stem} ({suffix}){Path(name).suffix}"
                    suffix += 1
                temporary.replace(destination)
                exam = read_json(root / "esame.json")
                exam["revision"] = exam.get("revision", 0) + 1
                write_json(root / "esame.json", exam)
                return {"name": destination.name, "duplicate": False}
            finally:
                temporary.unlink(missing_ok=True)

    def run(self, slug):
        root = self.root(slug)
        if read_json(root / "esame.json").get("archived"):
            raise ValueError("La materia è archiviata. Riattivala prima di avviare la produzione.")
        cfg = load_config(root, self.project)
        api = self.api_factory(cfg)  # La chiave è controllata prima di avviare il worker.
        with self.mutex:
            if self.workers.get(slug) is not None and self.workers[slug].is_alive():
                self.wakes[slug].set()
                return {"started": False}
            self.errors.pop(slug, None)
            wake = threading.Event()
            self.wakes[slug] = wake
            def work():
                try:
                    while not self.stop.is_set() and read_json(root / "esame.json").get("attivo", True):
                        config = load_config(root, self.project)
                        waiting = Pipeline(root, config, self.project, api=api).tick(True)
                        if not waiting:
                            break
                        state = read_json(root / "state/pipeline.json", {})
                        pilot = state.get("pilot_review") or {}
                        pilot_job = (state.get("jobs") or {}).get((state.get("latest") or {}).get("1"), {})
                        fast_pilot = (config.get("review_first_schema") and pilot and not pilot.get("approved_hash")
                                      and pilot_job.get("status") in {"READY", "RETRY_READY", "SUBMITTED", "HTML_OK"})
                        wake.wait(min(config["poll_seconds"], 15) if fast_pilot else config["poll_seconds"])
                        wake.clear()
                except Exception as error:
                    self.errors[slug] = str(error)
            thread = threading.Thread(target=work, name="schemi-" + slug, daemon=True)
            self.workers[slug] = thread
            thread.start()
        return {"started": True}

    def review_pilot(self, slug, action, feedback=""):
        if action not in {"approve", "revise"}:
            raise ValueError("Operazione di revisione non valida.")
        root = self.root(slug)
        if not load_config(root, self.project).get("review_first_schema"):
            raise ValueError("Questa materia usa la pipeline precedente.")
        feedback = str(feedback).strip()
        if action == "revise" and not 5 <= len(feedback) <= 12000:
            raise ValueError("Descrivi le modifiche richieste in almeno 5 caratteri (massimo 12000).")
        with (root / "state/pipeline.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            state = read_json(root / "state/pipeline.json", {})
            pilot_hash = (state.get("latest") or {}).get("1")
            job = (state.get("jobs") or {}).get(pilot_hash, {})
            if not pilot_hash or job.get("status") != "PDF_OK" or not job.get("public_pdf"):
                raise ValueError("Il primo PDF deve essere pronto prima della revisione.")
            pdf = (root / job["public_pdf"]).resolve()
            if not pdf.is_file() or not pdf.is_relative_to((root / "output/pdf").resolve()):
                raise ValueError("Il PDF pilota non è disponibile.")
            review = state.get("pilot_review") or {}
            if action == "approve":
                if review.get("pending_hash") and review["pending_hash"] != pilot_hash:
                    raise ValueError("La nuova versione del primo schema è ancora in preparazione.")
                review["approved_hash"] = pilot_hash
                review["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            else:
                review["round"] = review.get("round", 0) + 1
                review["feedback"] = feedback
                review["approved_hash"] = None
                review["pending_hash"] = digest({"base": review["base_hash"], "round": review["round"], "feedback": feedback})
                review.pop("approved_at", None)
            review["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            state["pilot_review"] = review
            write_json(root / "state/pipeline.json", state)
            exam = read_json(root / "esame.json")
            exam["attivo"] = True
            exam["start_policy"] = "now"
            exam["revision"] = exam.get("revision", 0) + 1
            write_json(root / "esame.json", exam)
        self.run(slug)
        return {"action": action, "round": review.get("round", 0)}

    def refresh_server(self, slug):
        """Read current remote state without creating or resubmitting work."""
        root = self.root(slug)
        before = read_json(root / "state/pipeline.json", {})
        pending = [b for b in before.get("batches", []) if (b.get("batch_id") or b.get("response_id")) and not b.get("processed")]
        if not pending:
            return {"checked": 0, "errors": []}
        api = self.api_factory(load_config(root, self.project))
        updates, errors = {}, []
        for batch in pending:
            batch_id = batch.get("batch_id") or batch.get("response_id")
            try:
                updates[batch_id] = api.get_response(batch_id) if batch.get("mode") == "urgent" else api.get_batch(batch_id)
            except Exception as error:
                errors.append(f"{batch_id}: {error}")
        with (root / "state/pipeline.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            state = read_json(root / "state/pipeline.json", {})
            for batch in state.get("batches", []):
                remote = updates.get(batch.get("batch_id") or batch.get("response_id"))
                if remote and not batch.get("processed"):
                    batch["status"] = remote.get("status")
                    batch["request_counts"] = ({"total": 1, "completed": int(remote.get("status") == "completed"),
                                                "failed": int(remote.get("status") in {"failed", "cancelled", "incomplete"})}
                                               if batch.get("mode") == "urgent" else remote.get("request_counts"))
                    batch["remote_checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    batch.pop("error", None)
            write_json(root / "state/pipeline.json", state)
        if read_json(root / "esame.json").get("attivo", False) and any(remote.get("status") in {"completed", "failed", "expired", "cancelled", "incomplete"} for remote in updates.values()):
            self.run(slug)
        return {"checked": len(updates), "errors": errors}

    def rescue(self, slug):
        """Recover existing results without creating new API requests."""
        root = self.root(slug)
        cfg = load_config(root, self.project)
        try:
            api = self.api_factory(cfg)
        except Exception:
            api = None  # Local recovery remains useful when the API is unavailable.
        return search_and_rescue(root, cfg, self.project, api=api)


def make_server(app, port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def headers_ok(self):
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in allowed:
                raise PermissionError("Accesso consentito solo dal Mac locale.")
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + host for host in allowed}:
                raise PermissionError("Origine della richiesta non valida.")

        def authenticate(self, query=None):
            token = self.headers.get("X-Schemi-Token") or (query or {}).get("token", [""])[0]
            if not secrets.compare_digest(token, app.token):
                raise PermissionError("Sessione non valida: riapri APRI.command.")

        def send(self, value, status=200, mime="application/json; charset=utf-8"):
            data = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict, list)) else value
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", f"default-src 'self'; script-src 'self' 'nonce-{app.token}'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def read_body(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024 * 1024:
                raise ValueError("Richiesta troppo grande o vuota.")
            return json.loads(self.rfile.read(length))

        def failure(self, error):
            status = 403 if isinstance(error, PermissionError) else 409 if isinstance(error, BlockingIOError) else 400
            self.send({"error": str(error)}, status)

        def do_GET(self):
            try:
                self.headers_ok()
                parsed = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/api/ping":
                    self.send({"app": "schemi-esami-v2", "project": digest(str(app.project.resolve()))})
                    return
                if parsed.path == "/":
                    html = (app.project / "ui/index.html").read_text().replace("__TOKEN__", app.token)
                    self.send(html.encode(), mime="text/html; charset=utf-8")
                    return
                if parsed.path in {"/app.js", "/style.css"}:
                    path = app.project / "ui" / parsed.path.lstrip("/")
                    self.send(path.read_bytes(), mime="text/javascript; charset=utf-8" if path.suffix == ".js" else "text/css; charset=utf-8")
                    return
                self.authenticate(query)
                if parsed.path == "/api/state":
                    self.send({"exams": app.list_exams(), "defaults": app.defaults(), "supported": sorted(SUPPORTED)})
                elif parsed.path == "/api/exam":
                    self.send(app.detail(query.get("slug", [""])[0]))
                elif parsed.path == "/api/models":
                    defaults = read_json(app.project / "config/pipeline.json")
                    value = app.api_factory(defaults).list_models()
                    self.send({"models": sorted(value.get("data", []), key=lambda model: model["id"])})
                elif parsed.path == "/file":
                    root = app.root(query.get("slug", [""])[0])
                    relative = query.get("path", [""])[0]
                    report = read_json(root / "output/reports/exports.json", {})
                    allowed = {row.get("file") for row in report.get("rows", [])} | {report.get("combined"), report.get("archive")}
                    file = (root / relative).resolve()
                    if relative not in allowed or not file.is_relative_to((root / "output/pdf").resolve()) or not file.is_file():
                        raise PermissionError("File non disponibile fra i risultati correnti.")
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(file.name)[0] or "application/octet-stream")
                    self.send_header("Content-Length", str(file.stat().st_size))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(file.name))
                    self.end_headers()
                    with file.open("rb") as stream:
                        shutil.copyfileobj(stream, self.wfile)
                else:
                    self.send({"error": "Pagina non trovata."}, 404)
            except Exception as error:
                self.failure(error)

        def do_POST(self):
            try:
                self.headers_ok()
                self.authenticate()
                path = urllib.parse.urlsplit(self.path).path
                if path == "/api/upload":
                    self.send(app.upload(self.headers.get("X-Exam", ""), self.headers.get("X-Kind", ""), urllib.parse.unquote(self.headers.get("X-Filename", "")), self.rfile, int(self.headers.get("Content-Length", "0"))))
                    return
                data = self.read_body()
                if path == "/api/save":
                    self.send({"slug": app.save_exam(data)})
                elif path == "/api/run":
                    self.send(app.run(data["slug"]))
                elif path == "/api/settings":
                    self.send({"profile": save_default_profile(app.project, data)})
                elif path == "/api/remove":
                    root = app.root(data["slug"])
                    kind = data["kind"]
                    if kind not in KINDS:
                        raise ValueError("Area non valida.")
                    relative = Path(data["name"])
                    file = (root / "input" / KINDS[kind] / relative).resolve()
                    if not file.is_relative_to((root / "input" / KINDS[kind]).resolve()) or not file.is_file():
                        raise PermissionError("File non valido.")
                    with edit_lock(root):
                        app.archive_input(root, file)
                        exam = read_json(root / "esame.json")
                        exam["revision"] = exam.get("revision", 0) + 1
                        exam["attivo"] = False
                        write_json(root / "esame.json", exam)
                    self.send({"removed": True})
                elif path == "/api/upgrade":
                    root = app.root(data["slug"])
                    with edit_lock(root):
                        exam = read_json(root / "esame.json")
                        exam["pipeline"].update(app.defaults())
                        exam["attivo"] = True
                        exam["revision"] = exam.get("revision", 0) + 1
                        write_json(root / "esame.json", exam)
                    self.send(app.run(root.name))
                else:
                    self.send({"error": "Comando non trovato."}, 404)
            except Exception as error:
                self.failure(error)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="Maschera locale per Schemi Esami")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    app = App()
    project_id = digest(str(app.project.resolve()))
    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = make_server(app, port)
            break
        except OSError:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as response:
                    existing = json.load(response)
                if existing == {"app": "schemi-esami-v2", "project": project_id}:
                    if not args.no_open:
                        webbrowser.open(f"http://127.0.0.1:{port}/")
                    print("La maschera è già aperta.")
                    return
            except Exception:
                pass
    if server is None:
        raise SystemExit("Impossibile trovare una porta locale libera.")
    address = f"http://127.0.0.1:{server.server_port}/"
    print("Maschera pronta: " + address, flush=True)
    print("Lascia aperta questa finestra durante il lavoro. Ctrl+C per chiudere.", flush=True)
    if not args.no_open:
        webbrowser.open(address)
    # Riprendi lavori già inviati; le materie prenotate per stanotte attendono il job.
    for row in app.list_exams():
        if row["scheduled"]:
            state = read_json(app.root(row["slug"]) / "state/pipeline.json", {})
            if any(not batch.get("processed") and batch["stage"] in {"submitted", "creating", "uncertain"} for batch in state.get("batches", [])):
                try:
                    app.run(row["slug"])
                except ValueError as error:
                    app.errors[row["slug"]] = str(error)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        server.server_close()
