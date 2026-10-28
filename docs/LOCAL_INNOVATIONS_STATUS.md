# Stato delle innovazioni locali

Ambito deciso dall'utente: app Mac e OpenAI già presenti, senza cloud operativo, provider grafici esterni o nuove API. Le istruzioni dei quattro allegati sono state usate come specifica tecnica, subordinate a questa scelta.

## Realizzato in questo branch

- Telemetria locale SQLite per importazione, preparazione batch, accettazione e controllo remoto osservati, rendering PDF e recupero locale; gli errori della telemetria non arrestano il lavoro.
- Home Progetti filtrabile; Control Room con overview, schemi correnti, batch, anomalie e statistiche misurate. I progetti storici senza telemetria mostrano dati non disponibili.
- Materiali: stato di indicizzazione e anteprima del testo già estratto, senza nuova OCR all'apertura.
- Report diagnostico locale con allowlist tecnica, preview completa, ZIP e nessuna trasmissione automatica.
- Recupero L1 esplicito di PDF esistenti o HTML correnti validi, con massimo due tentativi e audit locale; nessuna nuova POST.
- Medical Illustrator MVP: regole conservative per compressione del mediano, frattura e lussazione; opportunità collegate a source pack e hash; bozze SVG editabili, QC tecnico e revisione umana esplicita; nessuna modifica automatica ai PDF.
- Controllo visivo locale su PDF selezionati: warning per pagine quasi vuote, testo fuori bordo, sovrapposizioni sospette e ultima pagina scarsa, con anteprima pagina. I warning non alterano lo stato dei PDF.
- Script di installazione e aggiornamento dei sorgenti Mac; guida aggiornata sui programmi necessari.

## Limiti e lavoro successivo

- Non esiste in questo branch un atlante anatomico clinicamente validato, un generatore 3D o un modello locale capace di produrre tavole fotorealistiche accurate. Le bozze vettoriali sono dichiaratamente schematiche. Mancano ancora le altre classi patologiche, l'editing guidato delle opportunità, la verifica specialistica automatizzata e l'inserimento controllato in PDF.
- La Control Room non contiene ancora ETA statistica, comparazioni omogenee p95, grafici interattivi o un detector di colli di bottiglia calibrato su dati reali. Mostra soltanto conteggi e durate osservate.
- Il wizard a sei passi, archiviazione/duplicazione dei progetti, confronto semantico programma-fonti e un editor completo delle immagini non sono presenti. Restano disponibili il flusso di creazione, i tre gruppi di file e il controllo pilota esistenti.
- Il collegamento automatico a Codex CLI non è attivo: il report include un prompt esportabile per un uso manuale scelto dall'utente.
- Non è stata prodotta né provata una nuova app `.app`. Test automatici su Linux e compilazione Python non dimostrano la resa della GUI o l'esecuzione degli script `.command` su macOS. Prima di distribuire una build Mac servono prove visive con dati fittizi, OCR e installazione su quella macchina.
- Le due proposte cloud allegate sono escluse: a Mac spento il repository GitHub non esegue da solo la pipeline né consente la revisione da telefono.

## Verifica e rollback

Suite: `PYTHONPATH=/workspace/.schemi-deps python -m unittest discover -s tests -q` (78 test verdi il 28 ottobre 2026); `python -m compileall -q scripts native.py`; `git diff --check`. Nessuna chiamata API reale nei test. Le modifiche sono su branch dedicato; gli stati di esame, le risposte e i PDF esistenti non sono stati migrati. L'aggiornamento su una cartella Mac esistente conserva copia dei file sostituiti sotto `state/aggiornamenti-nativa/`.
