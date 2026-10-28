# Stima preliminare dei costi cloud

## Architettura scelta: Google Cloud Run + Mistral AI

La scelta del 28 ottobre 2026 è eseguire il programma nel cloud **a consumo**,
con Cloud Run per API e worker e Mistral AI per la generazione. Il calcolo
Google Cloud più sotto resta uno scenario illustrativo, non una tariffa
verificata: circa **€3–8 per un esame al mese** con 5 ore di calcolo attivo,
10 GB di dati temporanei per 7 giorni e 10 GB di download. Cloud Run può
fermarsi quando è inattivo, mentre storage, stato e controlli periodici
possono avere piccoli costi anche tra un esame e l'altro.

Il catalogo pubblico LiteLLM consultato il 28 ottobre 2026 riporta per
`mistral-large-4` **$0,68 per milione di token in ingresso** e **$2,09 per
milione in uscita**. Il listino ufficiale non è raggiungibile da questo
ambiente e va controllato prima di impostare un budget. La spesa effettiva
dipenderà dai token e dalle immagini conteggiati da Mistral, dalla qualità
degli output, da eventuali retry e da tasse e cambio. La formula è
`milioni_input × 0,68 + milioni_output × 2,09` dollari, se quelle tariffe
sono ancora applicabili. Non è stato eseguito alcun lavoro reale con Mistral.

Fonte tariffaria da ricontrollare: [catalogo LiteLLM](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json),
[scheda Mistral Large 4](https://docs.mistral.ai/models/mistral-large-4).

## Alternativa senza cloud aggiuntivo

Se preparazione, OCR, PDF pilota e PDF finali restano sul Mac, e OpenAI esegue
soltanto i Batch già accettati mentre il Mac è spento, il **costo aggiuntivo di
Google Cloud è €0**. Restano le normali tariffe OpenAI, l'elettricità del Mac e
lo spazio disco locale. Il Mac deve restare acceso per generare e approvare il
pilota; dopo l'accettazione degli altri Batch può essere spento, ma download e
PDF finali aspettano la riapertura. Nessuna pagina mobile dell'app è operativa
in questo percorso.

Per Psichiatria non è disponibile una stima numerica verificata delle richieste
OpenAI: gli allegati citano quell'esame, ma non forniscono dimensioni, durata o
token misurati. Il report cloud precedente dice esplicitamente che la sua
vecchia stima per Psichiatria era ipotetica. La spesa OpenAI effettiva va
ricostruita dal riepilogo d'uso o dagli output del lavoro, senza caricare
materiali di studio integrali su GitHub.

## Opzione Google Cloud completa

**Data:** 28 ottobre 2026. **Stato:** simulazione per pianificare il budget,
non preventivo. Il servizio cloud non è ancora distribuito. Nel repository ci
sono i sorgenti, ma non misure pubblicabili di durate, dimensioni o consumo
di token per il nuovo modello. I listini ufficiali Google Cloud e Mistral
hanno restituito HTTP 403 dalla rete di questo ambiente: le tariffe
ipotizzate qui devono essere verificate prima di attivare risorse a pagamento.

## Ipotesi del calcolo

- Architettura proposta in `CLOUD_ROADMAP.md`: API che può fermarsi quando è
  inattiva, worker di calcolo attivo solo per preparazione, OCR e PDF; attesa
  del Batch e della decisione umana gestita tramite stato persistente e
  controlli periodici. Nessuna GPU.
- Worker rappresentativo: 4 vCPU, 8 GiB di memoria; **€0,40 per ora attiva**
  usati come segnaposto di calcolo, non come listino verificato.
- Storage temporaneo: **€0,02 per GB per mese** usati come segnaposto;
  conservazione di 7 giorni. Download verso il Mac: **€0,10 per GB** usati
  come segnaposto. Upload iniziale assunto gratuito.
- Stato, controlli periodici, segreti, notifiche e log: **€0–5 al mese** come
  margine illustrativo per uso personale. Eventuali quote gratuite potrebbero
  abbassare il costo; configurazioni sempre accese o log abbondanti possono
  aumentarlo.

| Scenario ipotetico | Calcolo attivo | Dati temporanei | Download | Cloud indicativo, IVA esclusa |
| --- | ---: | ---: | ---: | ---: |
| 1 esame al mese | 5 ore | 10 GB per 7 giorni | 10 GB | circa **€3–8/mese** |
| 10 esami al mese | 50 ore | 100 GB per 7 giorni | 100 GB | circa **€30–50/mese** con margine |

Per il primo scenario: `5 × 0,40 + 10 × 7/30 × 0,02 + 10 × 0,10 + (0–5)` =
circa €3–8. Il tempo di attesa di OpenAI o dell'approvazione del pilota non è
contato come ora di worker acceso. Le dimensioni e le durate sono scenari,
**non misure dell'esame di Psichiatria o di un altro esame reale**.

## Costi esclusi e limiti

Le richieste **Mistral** sono una voce distinta, potenzialmente superiore al
costo dell'infrastruttura. Senza misure reali di token in ingresso/uscita con
il modello scelto, numero di schemi, immagini e retry, non è corretto
attribuire un importo finale per esame. La formula è:
`token_input/1.000.000 × prezzo_input + token_output/1.000.000 × prezzo_output`,
aggiungendo immagini, retry e altri addebiti applicabili. Codex e l'eventuale
abbonamento dell'utente non sono inclusi. Nessuna cifra sopra è un tetto
automatico di spesa; gli avvisi di budget informano, ma non bloccano tutte le
risorse.

La stima non include IVA, variazioni di cambio, eventuale dominio personalizzato,
backup, traffico aggiuntivo, conservazione più lunga, esami più grandi o
hardware più potente. L'obiettivo di non creare un archivio cloud permanente
non elimina storage e log temporanei. Anche gli oggetti OpenAI seguono proprie
regole di conservazione.

## Dati necessari per un preventivo utile

Misurare su un esame rappresentativo, senza condividere fonti private: GB di
input, GB di PDF finali, ore effettive di preparazione/OCR/rendering, durata di
conservazione richiesta, numero di schemi e token/immagini/retry registrati
dall'uso del modello. Verificare poi prezzi e disponibilità dei servizi nella
regione europea scelta, prima di impostare un budget di avviso e distribuire
il servizio.

Listini ufficiali da ricontrollare: [Cloud Run](https://cloud.google.com/run/pricing),
[Cloud Storage](https://cloud.google.com/storage/pricing),
[Firestore](https://cloud.google.com/firestore/pricing),
[Mistral AI](https://docs.mistral.ai/models/mistral-large-4).
