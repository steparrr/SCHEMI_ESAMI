# Schemi Esami V2 — uso locale

Questa versione usa la finestra desktop Tkinter e il motore OpenAI già presente.
La preparazione dei materiali, il monitoraggio, i PDF e Medical Illustrator
girano sul Mac. Per completare una materia quando il Mac è spento servirebbe
un servizio cloud, che qui non è stato attivato.

## Installazione da questi sorgenti

1. Scarica il repository come ZIP, estrailo in una posizione definitiva e apri
   `INSTALLA.command` nella cartella estratta.
2. Se manca Python 3.12 o successivo con Tkinter, installa Python per macOS da
   python.org e ripeti il passo 1. Lo script crea `.venv` e installa le
   dipendenze Python elencate in `requirements.txt`.
3. Apri `APRI.command` nella stessa cartella. La finestra può essere chiusa e
   riaperta senza perdere i batch già accettati da OpenAI.

Su macOS puoi aprire `VERIFICA-MAC.command` prima di lavorare sulle tue materie.
La prova crea una materia temporanea, verifica Tkinter e un PDF sintetico e
apre la finestra con le API bloccate. Chiudendo la finestra i dati di prova
vengono eliminati; i tuoi esami non sono toccati.

Se possiedi una cartella V2 già installata con `.venv`, puoi usare
`AGGIORNA.command` direttamente dalla cartella dei nuovi sorgenti dopo aver
chiuso la finestra V2 e il suo worker locale. Seleziona la cartella V2
esistente quando viene richiesta. L'aggiornamento copia interfaccia e moduli di questa versione,
archivia i file sovrascritti in `state/aggiornamenti-nativa/` e conserva
`esami/`, `config/`, le risposte e i PDF. Non aggiorna automaticamente una
vecchia app `.app` compilata; per quella serve una build macOS separata.

## Nuove funzioni

- **Progetti**: stato delle materie, PDF pronti e invii incerti. «Nuova materia
  guidata» accompagna identità, obiettivo, materiali, produzione, grafica e
  riepilogo prima dell'avvio. Puoi cercare, filtrare e ordinare i progetti,
  segnare i preferiti, aprire la cartella e le versioni PDF, duplicare le sole impostazioni
  senza materiali o credenziali e archiviare una materia senza cancellarne i file.
- **Materiali e domande**: file nelle tre aree, stato di indicizzazione e
  anteprima locale del testo estratto. «Confronta programma» mostra solo
  corrispondenze lessicali indicative nelle fonti già indicizzate.
- **Control Room**: schemi correnti, batch, avvisi conservativi, durate locali misurate,
  p90/p95 descrittivi quando esistono almeno 20 misure per fase e stima indicativa
  della sola impaginazione locale quando ci sono almeno 20 rendering comparabili
  della stessa versione. Mostra anche i token input, cache e output delle risposte
  conservate nei job; il dato è parziale e per la spesa reale va consultata la
  dashboard OpenAI. Attesa OpenAI e revisione del primo PDF non sono stimate;
  report diagnostico con anteprima completa e recupero dei soli PDF locali.
  «Panoramica di tutte le materie» elenca stati e avvisi locali per progetto
  senza eseguire un controllo remoto.
- **Schemi e PDF**: controllo visivo manuale delle pagine sospette e report
  tecnico locale in `output/reports/visual/`. Il programma segnala i riquadri
  opachi disegnati sopra il testo e le immagini grandi con risoluzione apparente
  sotto 90 dpi; uno sfondo dietro il testo non genera l'avviso sui riquadri.
  Puoi segnare una pagina verificata o annotare un falso
  positivo; la decisione è legata all'hash del PDF e non cambia `PDF_OK`.
- **Medical Illustrator**: cerca proposte nelle fonti o permette di aggiungerne
  una manualmente, modifica il piano e crea bozze SVG schematiche. L'anteprima
  si apre nella finestra del programma senza editor esterni. Se modifichi
  l'SVG, rivalidalo tecnicamente e ripeti la revisione. Dopo l'approvazione
  puoi esportare **una copia PDF separata** con la tavola come appendice, se
  rispetta il limite di pagine. Prima dell'approvazione devi completare una
  lista di controllo sulle fonti, l'anatomia, la vista, l'alterazione, le
  etichette e gli eventuali asset esterni. Il PDF originale e `PDF_OK` non
  cambiano. La lista registra una revisione dell'utente, non una validazione
  clinica specialistica.

La generazione del testo continua a usare esclusivamente l'API OpenAI già
configurata. Aprire Progetti, Control Room, Materiali o Illustrator non invia
nuove richieste a pagamento. Le operazioni «Avvia» e la rigenerazione esplicita
continuano a poter usare l'API.

## Programmi aggiuntivi

**Per Medical Illustrator di questa versione non serve installare Inkscape,
Blender, 3D Slicer, ImageMagick o un modello di immagini.** SVG è prodotto con
Python standard, senza API grafiche. Inkscape è facoltativo se vuoi modificare
manualmente le bozze vettoriali; il file modificato deve poi superare i
controlli SVG del programma. Blender e 3D Slicer servirebbero solo per
futuri flussi 3D o segmentazioni con dati e licenze appropriati.

Per PDF scansionati e fotografie, l'OCR richiede il helper `mac-ocr` incluso
in alcune distribuzioni oppure Tesseract con lingua italiana/inglese. Se i
tuoi PDF contengono già testo selezionabile, non serve installare Tesseract.
Il renderer PDF usa PyMuPDF Story, incluso nelle dipendenze Python; Chromium
non è richiesto da questi sorgenti.

La suite automatica gira su Linux senza chiamate API reali. La GUI e
l'installer devono ancora essere provati direttamente su macOS.
