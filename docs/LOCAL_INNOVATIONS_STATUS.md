# Stato delle innovazioni locali

Ambito deciso dall'utente: app Mac e OpenAI già presenti, senza cloud operativo, provider grafici esterni o nuove API. Le istruzioni dei quattro allegati sono state usate come specifica tecnica, subordinate a questa scelta.

## Realizzato in questo branch

- Telemetria locale SQLite per importazione, preparazione batch, accettazione e controllo remoto osservati, rendering PDF e recupero locale; gli errori della telemetria non arrestano il lavoro.
- Home Progetti con ricerca, filtri, ordinamento, preferiti, cartella PDF, duplicazione delle sole impostazioni e archiviazione reversibile delle materie inattive; procedura guidata in sei passi per nuove materie con validazione di identità, file, modalità e riepilogo; Control Room con overview, schemi correnti, batch, avvisi conservativi e statistiche misurate. I progetti storici senza telemetria mostrano dati non disponibili.
- Materiali: stato di indicizzazione, anteprima del testo già estratto e confronto lessicale indicativo fra programma ufficiale e fonti, senza nuova OCR all'apertura.
- Report diagnostico locale con allowlist tecnica, preview completa, ZIP e nessuna trasmissione automatica.
- Recupero L1 esplicito di PDF esistenti o HTML correnti validi, con massimo due tentativi e audit locale; nessuna nuova POST.
- Medical Illustrator MVP: regole conservative per tunnel carpale, frattura e lussazione; proposte manuali e modificabili collegate a source pack e hash; bozze SVG editabili, QC tecnico e revisione umana esplicita. Una tavola approvata può essere esportata come appendice in un PDF separato, senza modificare il PDF originale o `PDF_OK`.
- Controllo visivo locale su PDF selezionati: warning per pagine quasi vuote, testo fuori bordo, sovrapposizioni sospette e ultima pagina scarsa, con anteprima pagina. I warning non alterano lo stato dei PDF.
- Script di installazione e aggiornamento dei sorgenti Mac; guida aggiornata sui programmi necessari.

## Limiti e lavoro successivo

- Non esiste in questo branch un atlante anatomico clinicamente validato, un generatore 3D o un modello locale capace di produrre tavole fotorealistiche accurate. Le bozze vettoriali sono dichiaratamente schematiche. Mancano modelli anatomici e pipeline specializzate per le altre classi patologiche, verifica specialistica automatizzata e inserimento della figura dentro il testo dello schema.
- La Control Room mostra p90/p95 descrittivi solo con almeno 20 misure riuscite per fase, ma non contiene ancora ETA statistica, comparazioni omogenee per classe di pagine o grafici interattivi. Il detector usa regole fisse e conserva il significato di «stato remoto osservato»; non diagnostica uno stallo server da una lunga attesa.
- La procedura guidata usa le impostazioni di produzione già presenti. Mancano confronto semantico profondo programma-fonti, profili grafici avanzati e un editor anatomico completo. Il confronto attuale usa parole chiave e può sbagliare con sinonimi o abbreviazioni. Restano disponibili i tre gruppi di file e il controllo pilota esistenti.
- Il collegamento automatico a Codex CLI non è attivo: il report include un prompt esportabile per un uso manuale scelto dall'utente.
- Non è stata prodotta né provata una nuova app `.app`. Test automatici su Linux e compilazione Python non dimostrano la resa della GUI o l'esecuzione degli script `.command` su macOS. Prima di distribuire una build Mac servono prove visive con dati fittizi, OCR e installazione su quella macchina.
- Le due proposte cloud allegate sono escluse: a Mac spento il repository GitHub non esegue da solo la pipeline né consente la revisione da telefono.

## Verifica e rollback

Suite: `PYTHONPATH=/workspace/.schemi-deps python -m unittest discover -s tests -q` (92 test verdi il 29 ottobre 2026); `python -m compileall -q scripts native.py`; `git diff --check`. Nessuna chiamata API reale nei test. Le modifiche sono su branch dedicato; gli stati di esame, le risposte e i PDF esistenti non sono stati migrati. L'aggiornamento su una cartella Mac esistente conserva copia dei file sostituiti sotto `state/aggiornamenti-nativa/`.
