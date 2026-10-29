"""Offline lexical hints between the official program and indexed sources."""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from pathlib import Path

_STOP = {"della", "delle", "degli", "dell", "dello", "dalla", "dalle", "negli", "nelle",
         "lezione", "programma", "esame", "argomento", "parte", "generale", "principi",
         "introduzione", "clinica", "clinici", "della", "sulla", "sulle", "dell", "alle"}


def _words(text):
    normalized = unicodedata.normalize("NFKD", str(text).casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return {word for word in re.findall(r"[a-z0-9]+", normalized) if len(word) >= 4 and word not in _STOP}


def _topics(program_texts, max_topics):
    seen = set()
    for text in program_texts:
        for line in str(text).splitlines():
            line = re.sub(r"^\s*(?:[-•*]|\d+[.)-])\s*", "", line).strip()
            if not 4 <= len(line) <= 160 or len(line.split()) > 16:
                continue
            terms = _words(line)
            if not terms or line.casefold() in seen:
                continue
            seen.add(line.casefold())
            yield line, terms
            if len(seen) >= max_topics:
                return


def compare(root: Path, max_topics=120):
    root = Path(root)
    database = root / "corpus/corpus.db"
    if not database.is_file():
        return {"status": "not_indexed", "rows": [], "message": "Materiali non ancora indicizzati; avvia prima la preparazione della materia."}
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        program = [row[0] for row in db.execute("SELECT c.text FROM chunks c JOIN documents d ON d.document_id=c.document_id WHERE d.source_type='programma' ORDER BY c.sequence_number")]
        sources = [(row[0], _words(row[1])) for row in db.execute(
            "SELECT c.document_name,c.text FROM chunks c JOIN documents d ON d.document_id=c.document_id WHERE d.source_type='sbobina'")]
        questions = [_words(row[0]) for row in db.execute("SELECT question_text FROM questions")]
    if not program:
        return {"status": "no_program", "rows": [], "message": "Nessun programma ufficiale indicizzato; non è possibile stimare il confronto."}
    rows = []
    for topic, terms in _topics(program, max_topics):
        threshold = min(2, len(terms))
        source_names = sorted({name for name, words in sources if len(terms & words) >= threshold})
        question_hits = sum(len(terms & words) >= threshold for words in questions)
        rows.append({"topic": topic, "source_count": len(source_names),
                     "source_names": source_names[:3], "question_count": question_hits,
                     "hint": "termini trovati" if source_names else "nessuna corrispondenza lessicale"})
    return {"status": "estimated", "rows": rows,
            "message": "Confronto lessicale indicativo: sinonimi, abbreviazioni e contesto possono causare errori. Non prova la copertura didattica."}
