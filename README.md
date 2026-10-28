# Schemi Esami V2 — sorgenti

Questa cartella contiene il codice dell'app macOS importato dallo ZIP
`Sorgenti.zip`. I dati degli esami, i PDF e le chiavi API non fanno parte del
repository. La GUI desktop usa Tkinter; il motore Python è in `scripts/` e il
renderer in `renderer/`.

## Sviluppo e test

Richiede Python 3.12 o superiore. In una directory di sviluppo:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -q
```

Per avviare la GUI su macOS servono inoltre Tkinter e i permessi locali
richiesti dall'app. `APRI.command` usa `.venv`. Il file `Schemi Esami.spec` è
destinato a PyInstaller su macOS; la build macOS non è stata verificata in un
ambiente Linux. L'OCR delle scansioni usa il helper macOS se installato oppure
Tesseract con i pacchetti linguistici disponibili.

## Funzioni cloud

Il codice su GitHub consente di sviluppare e testare il programma nel cloud
anche quando il Mac è spento. **La produzione degli esami non è ancora
autonoma nel cloud.** La preparazione, il polling e i PDF dipendono tuttora da
un processo in esecuzione. Non spegnere il Mac durante un esame aspettandoti
che il solo repository completi la pipeline.

`scripts/cloud_handoff.py` prepara e verifica un pacchetto di un nuovo esame
per un futuro servizio cloud. Esempio per sviluppatori:

```sh
.venv/bin/python scripts/cloud_handoff.py build nome-esame /tmp/nome-esame.zip --project .
.venv/bin/python scripts/cloud_handoff.py verify /tmp/nome-esame.zip --project .
```

Il pacchetto contiene i materiali d'esame e non va caricato su GitHub. Il
servizio che lo riceverà, la pagina mobile, l'autenticazione, la conferma di
presa in carico e la sincronizzazione finale sono descritti in
[`docs/CLOUD_ROADMAP.md`](docs/CLOUD_ROADMAP.md) e devono ancora essere
implementati e distribuiti.
