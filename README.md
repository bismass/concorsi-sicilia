# Concorsi Sicilia

Raccoglie i concorsi pubblici della Sicilia dal portale inPA e li mostra in una pagina web.

## In locale
    pip install -r requirements.txt          # (+ requirements-ocr.txt e Tesseract per --ocr)
    python scrape.py --pagine 1700           # giro completo (30-60 min la prima volta)
    python scrape.py --solo-dettagli --ocr   # solo titoli di studio, senza rileggere le pagine
    python scrape.py --report                # riepilogo dei dati
    python -m http.server                    # poi apri http://localhost:8000

## Online (GitHub Pages) con aggiornamento automatico
1. Crea un account su github.com e un repository **pubblico** (es. `concorsi-sicilia`).
2. In `scrape.py` sostituisci lo `User-Agent` con l'indirizzo del tuo repository.
3. Dalla cartella del progetto:

       git init
       git add .
       git commit -m "Prima versione"
       git branch -M main
       git remote add origin https://github.com/TUO_UTENTE/concorsi-sicilia.git
       git push -u origin main

   (`.gitignore` esclude `venv` e `cache`; `data/concorsi.json` va invece caricato: contiene i dati già raccolti.)
4. Su GitHub: **Settings → Pages → Source: GitHub Actions**.
5. Su GitHub: **Actions → "Aggiorna concorsi e pubblica il sito" → Run workflow**.
6. A fine esecuzione il sito è su `https://TUO_UTENTE.github.io/concorsi-sicilia/`.

Poi si aggiorna da solo: ogni giorno controlla le pagine più recenti, ogni domenica fa il giro completo.
