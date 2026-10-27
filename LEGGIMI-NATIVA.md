# Schemi Esami — finestra locale

La nuova interfaccia è una finestra desktop Tkinter/ttk. Non apre un browser, non avvia un server HTTP e non richiede un indirizzo IP. Usa direttamente lo stesso motore della V2.

## Aggiornare la V2 già installata

1. Estrai `schemi-esami-v2-nativa.zip`.
2. Apri `AGGIORNA.command` nella cartella estratta. Il programma cerca la V2 già installata sul Desktop o in Download; se necessario ti chiede di selezionarla.
3. L'aggiornamento copia solo la nuova maschera, il suo launcher, le istruzioni e i test. Conserva materie, input, profili dei modelli, stato degli invii, PDF, dipendenze e job notturno. Archivia gli eventuali file sostituiti in `state/aggiornamenti-nativa/`.
4. Se il vecchio programma è ancora in esecuzione, chiudilo con Ctrl+C nella sua finestra del Terminale. I batch già inviati restano sul servizio.
5. Apri `APRI.command` **dentro la V2 già installata**. Comparirà la finestra locale. Il launcher la avvia come processo separato: puoi chiudere il Terminale.

Se non hai mai installato la V2, il programma completo è nella cartella `programma` di questo pacchetto. Collocala in una posizione definitiva, apri il suo `INSTALLA.command` e poi `APRI.command`.

Serve Tkinter, incluso nell'installazione standard di Python da python.org. L'aggiornamento controlla che sia disponibile e non installa nuove dipendenze.

## Uso

- Crea una materia e scegli Completa o Veloce. Per Veloce indica il massimo totale di pagine.
- Usa **Aggiungi materiali**, **File delle domande** e **Programma ufficiale** per selezionare più file dal Finder. Le domande si possono anche incollare.
- Premi Salva, Genera ora oppure Genera stanotte. Salva non effettua richieste API.
- **Schemi e PDF** mostra i risultati: doppio clic su un PDF pronto per aprirlo con l'applicazione predefinita del Mac. Sono disponibili PDF unico e ZIP.
- **Stato e batch** mostra fase, identificativi dei batch, stato del servizio, errori e ultime operazioni. Un batch con ID e stato in_progress è stato inviato e sta lavorando; un messaggio generico di avvio, da solo, non prova che l'invio sia stato accettato.
- **Impostazioni del modello** aggiorna il profilo per le nuove materie. La rigenerazione di una materia esistente è una scelta esplicita e usa l'API.

Lascia aperta la finestra dell'app durante Genera ora. Alla chiusura il programma attende che il worker corrente registri lo stato. Gli invii ancora in corso sul servizio vengono ripresi alla riapertura. Il job notturno continua a funzionare come nella V2.

La maschera, il corpus, l'OCR disponibile e il rendering sono locali. La generazione e l'analisi semantica usano le API e richiedono Internet. Il renderer PDF usa ancora Chromium senza finestra visibile. Questa versione elimina il browser per l'interfaccia, non il servizio API né il motore di rendering interno.

## Verifica

45 test automatici passati: i 35 della V2 più 10 per il percorso nativo, compresi salvataggio senza browser/server/API, ripresa degli invii, aggiornamento esplicito del modello, errori di caricamento recuperabili e diagnostica degli identificativi. Nessuna richiesta OpenAI a pagamento nel collaudo. La finestra è stata collaudata in un display Linux reale: creazione, caricamento, salvataggio, riapertura di una materia e impostazioni, senza avviare server HTTP o browser. L'aggiornamento è stato verificato su una V2 temporanea: fonti, configurazione e stato di un batch precedente restano identici. Il primo avvio su macOS resta da verificare sul tuo Mac.
