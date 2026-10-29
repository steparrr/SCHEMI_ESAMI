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

L'apertura di `APRI.command` registra un worker locale di accesso per questa
installazione dai sorgenti. Il worker può proseguire la preparazione, i
controlli e l'impaginazione dopo la chiusura della finestra, e avviare un esame
prenotato per la notte se il Mac rimane acceso, sveglio e con l'utente connesso.
Usa un LaunchAgent separato da quello di un'eventuale app `.app`; non aggiunge
servizi cloud né provider API. A Mac spento il lavoro locale si ferma.

Su macOS puoi aprire `VERIFICA-MAC.command` prima di lavorare sulle tue materie.
La prova crea una materia temporanea con un PDF sintetico, una fonte fittizia,
due proposte di tavole e una bozza SVG. Verifica Tkinter e apre la finestra con
le API bloccate: puoi ispezionare Progetti, Schemi e PDF, Control Room e
Medical Illustrator. Chiudendo la finestra i dati di prova vengono eliminati;
i tuoi esami non sono toccati.

Se possiedi una cartella V2 già installata con `.venv`, puoi usare
`AGGIORNA.command` direttamente dalla cartella dei nuovi sorgenti dopo aver
chiuso la finestra V2 e il suo worker locale. Seleziona la cartella V2
esistente quando viene richiesta. L'aggiornamento copia interfaccia e moduli di questa versione,
archivia i file sovrascritti in `state/aggiornamenti-nativa/` e conserva
`esami/`, `config/`, le risposte e i PDF. Non aggiorna automaticamente una
vecchia app `.app` compilata; per quella serve una build macOS separata.

Per costruire una nuova `.app` sul Mac, dopo `INSTALLA.command` apri
`CREA-APP.command`. Lo script installa PyInstaller solo nella `.venv`, esegue
un controllo senza API sui sorgenti, crea `dist/Schemi Esami.app` e controlla
anche l'eseguibile con dati temporanei. La build così prodotta va ancora aperta
e verificata visivamente sul Mac prima di sostituire una versione già usata.
La build non copia materie, PDF o chiavi API dentro la `.app`.

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
  conservate per tentativo, compresi i retry successivi all'aggiornamento; per
  i job storici usa l'ultimo consumo salvato. Le nuove risposte sono distinte
  fra Batch e Urgente; quelle storiche senza modalità sono «Non attribuite».
  Il dato resta parziale: per la spesa reale va consultata la dashboard
  OpenAI. Attesa OpenAI e revisione del primo PDF non sono stimate. Sono
  disponibili un report diagnostico con anteprima completa e il recupero
  esplicito dei soli PDF locali. Il recupero può ripubblicare una versione
  corrente valida se il file pubblico manca o è diverso, senza rigenerare con
  OpenAI. Prima di sostituire un file diverso ne salva una copia in
  `state/local_recovery_backups/`. I nuovi PDF conservano un hash della versione:
  se il file versionato cambia, l'app può reimpaginarlo dall'HTML locale;
  il recupero non lo ripubblica come se fosse l'originale.
  «Panoramica di tutte le materie» elenca stati e avvisi locali per progetto
  senza eseguire un controllo remoto. «Origine delle impostazioni» mostra i
  valori effettivi e se arrivano dalla materia o dal profilo globale. La
  rigenerazione con il profilo globale richiede una conferma perché può creare
  nuove richieste OpenAI a pagamento.
- **Schemi e PDF**: controllo visivo manuale delle pagine sospette e report
  tecnico locale in `output/reports/visual/`. Il programma segnala i riquadri
  opachi disegnati sopra il testo e le immagini grandi con risoluzione apparente
  sotto 90 dpi; uno sfondo dietro il testo non genera l'avviso sui riquadri.
  Puoi segnare una pagina verificata o annotare un falso
  positivo; la decisione è legata all'hash del PDF e non cambia `PDF_OK`.
  Se il PDF unico o lo ZIP cambia rispetto ai metadati dell'ultimo export,
  viene ricreato dai PDF correnti senza inviare nuove richieste OpenAI.
- **Medical Illustrator**: cerca proposte nelle fonti o permette di aggiungerne
  una manualmente, modifica il piano e crea bozze SVG schematiche. L'anteprima
  si apre nella finestra del programma senza editor esterni. Se modifichi
  l'SVG, rivalidalo tecnicamente e ripeti la revisione. Dopo l'approvazione
  puoi esportare **una copia PDF separata** con la tavola come appendice, se
  rispetta il limite di pagine. Prima dell'approvazione devi completare una
  lista di controllo sulle fonti, l'anatomia, la vista, l'alterazione, le
  etichette e gli eventuali asset esterni. Il PDF originale e `PDF_OK` non
  cambiano. La lista registra una revisione dell'utente, non una validazione
  clinica specialistica. L'esportazione si ferma se la didascalia non entra
  nella pagina illustrata.

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
l'installer, incluso il LaunchAgent dei sorgenti e la procedura di build,
devono ancora essere provati direttamente su macOS.
