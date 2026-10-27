# Schemi Esami nel cloud: stato e percorso

## Stato verificato

Il codice estratto dallo ZIP `Sorgenti` è nel repository. La GUI è Tkinter su
macOS (`native.py`, `scripts/native_app.py`). `scripts/local_app.py` avvia un
thread locale; `scripts/background_worker.py` riprende i lavori al login. La
preparazione degli input e il rendering PDF avvengono tramite
`scripts/pipeline.py` sulla macchina che esegue l'app. OpenAI elabora le
richieste remote, ma l'app locale continua a fare polling, scaricare i risultati
e produrre i PDF.

La pipeline conserva ID remoti, hash, stato e output nelle cartelle dei singoli
esami. `Pipeline.queue` blocca gli altri schemi in attesa del PDF pilota;
`App.review_pilot` approva o richiede modifiche. La revisione attuale richiede
la GUI sul Mac. GitHub e l'ambiente Codex permettono **sviluppo del codice** a
Mac spento; non sono il servizio che esegue gli esami o la pagina mobile.

`scripts/cloud_handoff.py` è il primo componente aggiunto. Per un esame **non
ancora avviato** crea un pacchetto con soli input, configurazione e prompt,
manifest con hash SHA-256 e impronta del codice. Rifiuta esami con job o batch
esistenti per non perdere ID o duplicare richieste. Verifica l'integrità e
permette un'estrazione atomica in una directory nuova. Non invia il pacchetto
in rete e non autorizza ancora il messaggio «Puoi spegnere il Mac».

`scripts/cloud_inbox.py` registra localmente una presa in carico durevole e
idempotente: un retry dello stesso pacchetto restituisce la stessa ricevuta,
mentre lo stesso ID con contenuto diverso è rifiutato. È una libreria per il
futuro gateway HTTPS, non un servizio esposto in rete. L'accettazione reale
richiede ancora gateway autenticato, disco persistente del fornitore e prova
di riavvio del servizio.

## Fornitore proposto

Per un solo utente propongo **Google Cloud**. Il codice rimane su GitHub e
l'app Mac rimane l'interfaccia principale; Google Cloud ospiterà solo la presa
in carico, il lavoro temporaneo e la pagina privata del pilota. La proposta usa
Cloud Run per API e worker, Cloud Storage per pacchetti e PDF temporanei,
Firestore per stato e decisioni idempotenti, Cloud Tasks o Scheduler per
riconciliare i Batch e Secret Manager per la chiave OpenAI. L'accesso da Mac e
telefono richiederà autenticazione gestita, non un link pubblico al PDF.

Le risorse andranno collocate in una regione europea dopo aver verificato la
disponibilità di tutti i servizi necessari. Non è stato creato alcun progetto
Google Cloud e non esiste un preventivo misurato. Prima di attivare la
fatturazione servono una prova con input fittizi, limiti di spesa e una stima
basata su dimensioni e durate misurate. Anche quando il worker è fermo possono
restare costi di storage temporaneo, richieste, log e servizi di controllo.

## Architettura proposta per il flusso completo

1. **Mac e presa in carico.** L'app crea il pacchetto, lo carica tramite HTTPS
   con un ID di operazione stabile e ne verifica l'accettazione interrogando il
   servizio in caso di risposta ambigua. Il servizio registra pacchetto,
   impronta e stato su storage persistente prima di rispondere. Solo allora
   l'app mostra che il Mac può essere spento.
2. **Servizio e worker.** Un'API privata conserva stato e artefatti temporanei.
   Un worker Linux usa il motore esistente con una directory per lavoro,
   volume persistente, versioni del codice verificate, credenziale OpenAI nel
   gestore segreti del fornitore e un lock per lavoro. Il polling può essere
   schedulato; il worker di rendering non deve restare acceso durante l'attesa
   del Batch. Riavvii e timeout devono riconciliare gli ID prima di qualsiasi
   nuova POST.
3. **Pilota sul telefono.** Una pagina privata mostra stato essenziale e PDF
   pilota. Approvazione o feedback sono autenticati, registrati una volta e
   legati all'hash esatto del PDF. Gli altri schemi restano bloccati fino alla
   decisione.
4. **Rientro sul Mac.** L'app scarica stato e PDF con hash, verifica versione e
   integrità, li salva senza sovrascrivere PDF validi e conferma il ritiro. Il
   servizio elimina le copie controllate dal progetto solo dopo questa prova.
   La scadenza per lavori non ritirati richiede avviso e una politica esplicita
   per non eliminare l'unica copia valida.

## Criteri prima di dichiarare funzionante il flusso a Mac spento

- Il servizio, lo storage e l'autenticazione sono operativi su un fornitore
  scelto, con HTTPS e backup/recupero misurabili.
- Una prova completa con materiali fittizi supera spegnimento del Mac dopo
  l'ack, riavvio del worker, Batch incerto senza doppia POST, pilota approvato
  dal telefono, PDF verificati, download e pulizia.
- Il rendering Linux viene confrontato con quello macOS per font, paginazione,
  contenuto e controllo PDF. Il comportamento Mac esistente resta utilizzabile.
- Si misurano dimensioni e durate reali e si controllano prezzi e politiche di
  conservazione del fornitore prima di fare una stima dei costi.

## Dipendenze ancora aperte

Servono un account Google Cloud con fatturazione, una regione, storage temporaneo
persistente, una modalità di accesso privata da Mac e telefono e un canale di
avviso. Nessun servizio è stato distribuito e nessuna chiave OpenAI è stata
salvata nel repository. Il sistema deve restare in modalità locale finché il
flusso completo non supera i criteri sopra.

Il worker Linux necessita anche dell'OCR Tesseract con lingua italiana per
scansioni e foto. L'ambiente di sviluppo attuale dispone della lingua inglese,
ma non di quella italiana; questo non basta a verificare un esame reale in
italiano. La build macOS con PyInstaller e la fedeltà dei PDF Linux rispetto ai
PDF Mac richiedono prove sulle due piattaforme.
