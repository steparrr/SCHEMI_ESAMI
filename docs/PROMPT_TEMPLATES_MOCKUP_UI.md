# Prompt per Templates — mockup di Schemi Esami

Progetta un set **coerente e modificabile di mockup desktop macOS** per l’app italiana **Schemi Esami V2**. I mockup serviranno a rifare l’estetica delle maschere di un’app locale già funzionante. Usa il manuale utente allegato come riferimento per nomi, funzioni e flusso. Disegna l’interfaccia che segue; non inventare nuove capacità operative.

## Contesto e vincoli

- L’utente crea materie d’esame, importa sbobine/appunti, domande e programma, genera schemi tramite la **sola API OpenAI già configurata**, controlla i risultati e apre PDF sul Mac. Preparazione, monitoraggio locale, impaginazione e Medical Illustrator funzionano sul Mac. Nessun nuovo cloud o provider AI. Il Mac deve essere acceso e sveglio per completare le fasi locali.
- L’app è una finestra desktop nativa basata su Tkinter/ttk. I mockup devono essere realistici da tradurre in controlli macOS semplici: sidebar, schede, liste, tabelle, pulsanti, campi, finestre di dialogo. Evita glassmorphism, effetti 3D, grafici spettacolari, animazioni, drag-and-drop come unica via di caricamento e componenti impossibili da realizzare con il toolkit senza nuove dipendenze.
- Area ottimale **1280 × 820 px**. Mostra anche una variante di almeno due schermate a **880 × 650 px**: nessuna azione essenziale deve sparire; tabelle e form possono scorrere. Testi e pulsanti devono poter contenere le diciture italiane, anche con nomi materia lunghi.
- Tema principale **chiaro**, con sidebar blu profondo. Palette di riferimento: fondo `#F3F6F7`, superfici `#FFFFFF`, superfici secondarie `#EAF0F2`, blu `#142B40`, testo `#142C43`, turchese attivo `#087D79`, ambra attesa/avviso `#A76313`, rosso solo per errore reale `#A63A42`, bordi `#D9E2E6`. Contrasto leggibile, tipografia macOS sobria, gerarchia chiara, icona + testo per gli stati, spaziatura regolare. La Control Room può avere un’alternativa scura **solo come variante separata**, non mescolare i temi nelle altre schermate.
- Mantieni nella struttura comune: **barra laterale** con nome app, pulsante «+ Nuova materia», ricerca e lista materie, «Impostazioni modello», «Chiave OpenAI», «Aiuto · manuale PDF»; **testata** con materia corrente e scelta «Completa / Sintesi» (in Sintesi compare il massimo pagine); **schede** «Progetti», «Materiali e domande», «Schemi e PDF», «Stato e batch», «Control Room», «Medical Illustrator»; **barra inferiore** con modello corrente, testo breve su Batch/Urgente e pulsanti «Salva», «Stanotte · Batch», «Avvia urgente», «Avvia · Batch». Nella finestra stretta le schede possono chiamarsi «Progetti», «Input», «PDF», «Stato», «Control», «Tavole».
- Usa **solo dati fittizi**, marcati «demo». Per esempio «Ortopedia demo» con 12 PDF su 16, «Ginecologia demo» con primo PDF da approvare, «Materia dimostrativa» senza lavori. Non includere sbobine personali, vere chiavi API o dati sanitari identificabili. Non mostrare costi, percentuali di OCR, tempi di completamento o progressi numerici non misurati. Se un dato manca, scrivi «non disponibile» o «mai verificato».

## Tavole richieste

Produci **una tavola leggibile per ciascuna vista** sotto, con testi italiani reali. Se il numero di tavole supera un singolo output, consegnale in più gruppi mantenendo lo stesso design system. Non condensare tutte le schermate in un unico collage.

### 01 — Progetti / scelta materia
Primo avvio dell’app. Header «Progetti», ricerca, filtri «Attive / Tutte / Archiviate / Preferite», ordinamento «Nome / Data esame / PDF pronti». Lista o card con nome, modalità, eventuale data esame, PDF pronti rispetto agli schemi correnti, stato comprensibile e ultima verifica OpenAI. Evidenzia la materia selezionata con un riepilogo derivato dai dati locali. Azioni visibili: «Apri materia», «Control Room», «Cartella PDF», «Preferito», «Duplica», «Archivia / Riattiva», «Versioni PDF». Disegna anche uno stato vuoto e un progetto «Invio incerto · riconciliare». L’archiviazione non equivale alla cancellazione.

### 02–07 — Nuova materia, wizard in sei passi
Disegna **sei tavole distinte** con indicatore del passo, «Indietro», «Avanti», «Salva bozza» e, al riepilogo, «Avvia dopo il salvataggio». In ogni passo lascia visibile la distinzione tra salvare e inviare richieste OpenAI.

1. **Identità:** nome materia, titolo breve facoltativo, data esame facoltativa in formato AAAA-MM-GG.
2. **Obiettivo:** «Completa» o «Sintesi», massimo pagine totali solo per Sintesi, campo «Indicazioni per gli schemi». Spiega che il limite di pagine non garantisce la copertura di fonti insufficienti.
3. **Materiali:** tre blocchi separati «Sbobine e appunti», «Domande d’esame», «Programma ufficiale» con conteggio file, «Aggiungi file» e «Svuota». Si può salvare una bozza senza file; serve almeno un materiale per avviare.
4. **Produzione:** radio «Normale · Batch OpenAI», «Urgente · OpenAI a costo standard», «Notturna · Batch alle 00:30, Mac acceso». Nota: primo PDF da approvare; nessuna stima di prezzo fittizia.
5. **Grafica:** interruttore «Abilita proposte Medical Illustrator». Descrivi le bozze SVG schematiche e locali, la revisione umana e il fatto che non entrano automaticamente nei PDF.
6. **Riepilogo:** identità, obiettivo, numero file per categoria, modalità di produzione, Illustrator attivo/disattivo; CTA «Salva bozza» e «Avvia dopo il salvataggio», con gerarchia visiva e avviso sul costo potenziale solo quando si avvia.

### 08 — Materiali e domande / caricamento
Tre aree ben riconoscibili per sbobine/appunti, domande e programma, ognuna con conteggio e pulsante file picker. Tabella sotto con area, nome file, dimensione, stato «Da caricare / Indicizzato / Problema OCR»; distinguere file non ancora salvati da file già importati. Azioni «Anteprima selezionato», «Rimuovi selezionato», «Confronta programma». In fondo due campi di testo: «Domande incollate» e «Indicazioni per gli schemi». Mostra un esempio di scansione che richiede OCR senza inventare una percentuale di qualità.

### 09 — Dettaglio e revisione input
Finestra secondaria per un file selezionato. Mostra nome, formato, byte, pagine se note, stato, numero immagini se noto, hash, informazione OCR, errore e **campione del testo estratto**. Gerarchia chiara fra metadati e anteprima; stato «Da caricare: salva la materia per leggere l’anteprima» quando il file è ancora pendente. Prevedi un messaggio per testo non disponibile. Questa vista è di sola lettura.

### 10 — Programma e fonti
Finestra «Confronta programma»: elenco degli argomenti del programma, indizio di presenza nelle fonti, numero di file materiali e domande, nomi delle fonti corrispondenti. Intestazione evidente: **«Corrispondenza lessicale indicativa: non certifica la copertura»**. Prevedi stato vuoto se manca il programma o i dati indicizzati.

### 11 — Schemi e PDF
Titolo e fase corrente, tabella di schemi con argomento e stato («In attesa», «HTML pronto», «PDF pronto»). Righe per «PDF unico — tutti gli schemi» e «ZIP — tutti i PDF» solo se disponibili. Azioni «Apri PDF», «Controllo visivo», «Cartella PDF». Se il primo schema è pronto, mostra un pannello distinto con «Apri il primo PDF», «Descrivi modifiche…», «Approva il primo schema». «Rigenera con il modello predefinito» deve apparire come azione separata che può comportare una nuova chiamata API, non come normale refresh.

### 12 — Versioni PDF e controllo visivo
Due tavole o due finestre secondarie nello stesso gruppo: (a) «Versioni PDF» con schema, titolo, versione corrente/storica, stato, dimensione e «Apri versione selezionata»; (b) «Controllo visivo» con elenco pagine, anteprima pagina, avvisi concreti («Pagina quasi vuota», «Testo oltre margine», ecc.), azioni «Apri PDF originale», «Segna verificata» e «Annota falso positivo». Specifica che un avviso visivo non cambia automaticamente lo stato tecnico `PDF_OK`.

### 13 — Stato e batch
Vista delle **tre fasi**: «Sul Mac: preparazione», «OpenAI: invio e generazione», «Sul Mac: risultati e PDF». Mostra chiaramente motore locale attivo/fermo, stato remoto **osservato** con ora dell’ultimo controllo e PDF locali. Azioni «Controlla ora lo stato su OpenAI» e «Search and Rescue · recupera schemi». Aggiungi area diagnostica testuale scorrevole con messaggi orientati all’azione. Se il Mac è fermo e il batch è già stato accettato, non chiamare il server «fermo».

### 14 — Control Room / panoramica
Titolo «Stato del lavoro», indicatori reali «PDF pronti», «Batch accettati», «Incidenti», riepilogo di fase e pilota. Azioni «Aggiorna», «Anteprima report», «Recupera PDF locali», «Panoramica di tutte le materie», «Origine delle impostazioni». Sottoschede «Overview», «Schemi», «Batch», «Prestazioni». Mostra token disponibili come **dato parziale delle risposte conservate**, non come spesa fatturata. Se manca telemetria storica, scrivi «non disponibile».

### 15 — Control Room / esplorazione pipeline
Sottoscheda «Schemi»: tabella con numero, titolo, hash breve, stato, numero di retry; pannello dettaglio del job selezionato con eventi cronologici e tempi misurati. Sottoscheda «Batch»: modalità, ID remoto o «ID incerto», stato locale, stato remoto osservato, data ultimo controllo. Sottoscheda «Prestazioni»: fasi locali, riuscite, errori, mediana, p90/p95/max **solo se i dati esistono**. Tavola densa ma leggibile e con scroll delle tabelle, senza grafici inventati.

### 16 — Errori, recupero e diagnostica
Stato di esempio: «Invio remoto non confermato. Evito un reinvio automatico per non duplicare il costo». Mostra gravità, fase, schema/file interessato, cosa è noto e cosa fare. Distingui «Controlla stato OpenAI», «Recupera solo PDF locali» e «Anteprima report diagnostico». Per quest’ultima disegna una finestra con contenuto esportabile e CTA «Esporta ZIP dopo la verifica»; indica «Report creato localmente; nessun dato inviato a Codex». Niente pulsante automatico «Riprova tutto».

### 17 — Medical Illustrator / studio
Titolo «Tavole mediche», nota «Proposte locali, senza API. Ogni tavola richiede revisione». Azioni «Cerca tavole utili», «Aggiungi tavola manuale», «Disattiva/riattiva proposte». Elenco con numero schema, concetto, stato «Proposta / Bozza SVG / Revisione medica necessaria / Revisionata». Area dettaglio per obiettivo didattico, tipo di alterazione, vista e orientamento, regione, fonti, motivazione, didascalia e licenza. Usa solo una **tavola schematica** di esempio, non un’immagine anatomica che sembri clinicamente certificata.

### 18 — Medical Illustrator / revisione della tavola
Pannello di anteprima SVG con due pannelli schematici e didascalia; campi/azioni esistenti «Modifica», «Crea bozza SVG», «Apri bozza», «Valida SVG», «Scarta», «Segna revisionata», «Esporta PDF con tavola». La revisione richiede controlli espliciti su fonti, anatomia, orientamento, alterazione, etichette e diritti d’uso. Mostra che il PDF illustrato viene esportato come **copia separata**, lasciando intatto il PDF originale. La revisione dell’utente non va presentata come validazione clinica specialistica.

### 19 — Impostazioni e aiuto
Finestra «Impostazioni del modello» con identificativo API, ragionamento, massimo token, token per retry, compatibilità immagini e JSON strutturato, «Aggiorna elenco modelli», «Salva modello predefinito». Spiega che il profilo vale per nuove materie e non sovrascrive inaspettatamente quelle esistenti. Il comando «Chiave OpenAI» apre un input protetto; **non mostrare mai una chiave reale**. Il comando «Aiuto · manuale PDF» apre un PDF locale, non un portale web.

## Stati e consegna

Fornisci anche piccole varianti dei componenti per **vuoto, caricamento/attesa, pronto, avviso, errore e disabilitato**, con testo che spieghi cosa succede. Usa turchese per attività/pronto, ambra per attesa problematica o decisione richiesta, rosso per guasto reale; colore e icona non devono essere l’unico segnale. Non mostrare «100%» se non deriva da PDF pronti/schemi correnti. Non mostrare il Mac spento come capace di produrre i PDF da solo.

Consegna le tavole **editabili**, nominate con i numeri sopra; esporta anche un **PDF multipagina** e **PNG a dimensione piena**. Aggiungi una pagina di design system con colori, tipografia, spaziature, pulsanti, badge e tabelle, più annotazioni su comportamento a 880 × 650. Mantieni i testi in italiano come testo modificabile. Se una scelta grafica richiede una funzione che non figura qui, segnala la proposta separatamente invece di inserirla come capacità già operativa.
