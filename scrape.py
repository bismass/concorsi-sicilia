#!/usr/bin/env python3
"""Scraper concorsi - provincia di Catania.

Uso:
  python scrape.py              # raccoglie e aggiorna data/concorsi.json
  python scrape.py --probe      # stampa la risposta grezza di inPA (per debug)
  python scrape.py --selftest   # prova le funzioni di estrazione senza rete

Il file di output ha lo stesso formato che l'interfaccia web già legge:
prov, ente, titolo, profilo, studio, posti, pub, scad, url, nota.
I risultati vengono UNITI a quelli già presenti (mai cancellati), così
backlog e statistiche crescono nel tempo.
"""
import argparse, gzip, html, io, json, logging, re, sys, time, unicodedata
from datetime import date
from pathlib import Path
import requests
from requests.exceptions import RequestException

OUT = Path(__file__).parent / "data" / "concorsi.json"
CACHE = Path(__file__).parent / "cache"  # testo dei PDF già scaricati: permette di migliorare il riconoscimento senza riscaricare
ANALISI = 2  # versione del riconoscimento: se aumenta, i bandi "senza titolo trovato" vengono rianalizzati
UA = {"User-Agent": "concorsi-sicilia-bot/0.1 (+https://github.com/maxfr/concorsi-sicilia)"}
ATTESA = 5   # secondi: base dell'attesa tra un tentativo e l'altro quando la rete cade (5, 15, 45, 135)
PAUSA = 0.7  # secondi tra una richiesta e l'altra: siamo ospiti, andiamo piano
PROV = "Catania"  # (usato solo dal vecchio percorso WordPress)
REGIONE = "Sicilia"
PROVINCE = ["Agrigento", "Caltanissetta", "Catania", "Enna", "Messina", "Palermo", "Ragusa", "Siracusa", "Trapani"]
logging.getLogger("pypdf").setLevel(logging.CRITICAL)  # i PDF rovinati non devono riempire il terminale

# Controllare l'elenco ufficiale prima di considerarlo definitivo.
COMUNI = """Aci Bonaccorsi|Aci Castello|Aci Catena|Aci Sant'Antonio|Acireale|Adrano|Belpasso|
Biancavilla|Bronte|Calatabiano|Caltagirone|Camporotondo Etneo|Castel di Iudica|
Castiglione di Sicilia|Catania|Fiumefreddo di Sicilia|Giarre|Grammichele|Gravina di Catania|
Licodia Eubea|Linguaglossa|Maletto|Maniace|Mascali|Mascalucia|Mazzarrone|Militello in Val di Catania|
Milo|Mineo|Mirabella Imbaccari|Misterbianco|Motta Sant'Anastasia|Nicolosi|Palagonia|Paternò|
Pedara|Piedimonte Etneo|Raddusa|Ragalna|Ramacca|Randazzo|Riposto|San Cono|San Giovanni la Punta|
San Gregorio di Catania|San Michele di Ganzaria|San Pietro Clarenza|Sant'Agata li Battiati|
Sant'Alfio|Santa Maria di Licodia|Santa Venerina|Scordia|Trecastagni|Tremestieri Etneo|
Valverde|Viagrande|Vizzini|Zafferana Etnea""".replace("\n", "").split("|")

ENTI_EXTRA = {
    "Città Metropolitana di Catania": ["citta metropolitana di catania"],
    "ASP Catania": ["asp di catania", "asp catania", "azienda sanitaria provinciale di catania"],
    "Università di Catania": ["universita degli studi di catania", "universita di catania"],
    "AOU Policlinico G. Rodolico-San Marco": ["rodolico"],
    "ARNAS Garibaldi": ["arnas garibaldi", "azienda ospedaliera garibaldi"],
    "Ferrovia Circumetnea": ["circumetnea"],
    "ASMEL (comuni aderenti)": ["asmel"],
}

PROFILI = [  # primo che combacia vince
    ("Polizia locale", r"polizia locale|vigil[ei] urban|agent[ei] di polizia|comandante"),
    ("Sanitario", r"infermier|medic|dirigente medico|oss\b|operatore socio|tecnico sanitario|farmacist|biolog|ostetric"),
    ("Contabile", r"contabil|ragioneri|finanziari|tributi|economic"),
    ("Tecnico", r"tecnic|ingegner|architett|geometra|lavori pubblici|urbanistic|manutent"),
    ("Educativo/Sociale", r"educator|insegnant|assistente social|pedagog|psicolog|asilo nido"),
    ("Informatico", r"informatic|sistemi informativi|\bict\b"),
    ("Amministrativo", r"amministrativ|segretari|istruttore|funzionario|dirigente|collaboratore|staff"),
]
MESI = {m: i + 1 for i, m in enumerate(
    "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre".split())}


def richiedi(metodo, url, tentativi=5, **kw):
    """Richiesta con nuovi tentativi: una rete che cade per qualche minuto non deve far perdere il lavoro."""
    for t in range(tentativi):
        try:
            r = getattr(requests, metodo)(url, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise RequestException(f"HTTP {r.status_code}")
            return r
        except RequestException as e:
            if t == tentativi - 1:
                raise
            attesa = ATTESA * 3 ** t
            print(f"  … connessione instabile ({type(e).__name__}): riprovo tra {attesa}s", file=sys.stderr)
            time.sleep(attesa)


def norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    return re.sub(r"[\u0300-\u036f]", "", s).replace("’", "'")


def pulisci(h):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h or ""))).strip()


def trova_data(testo):
    """Data di scadenza: la prima data che compare dopo 'scadenza / entro il / termine'."""
    t = norm(testo)
    for m in re.finditer(r"scadenz|entro il|entro e non oltre|termine", t):
        seg = t[m.end(): m.end() + 140]
        a = re.search(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", seg)
        if a:
            g, me, an = map(int, a.groups())
        else:
            b = re.search(r"(\d{1,2})\s+(" + "|".join(MESI) + r")\s+(\d{4})", seg)
            if not b:
                continue
            g, me, an = int(b[1]), MESI[b[2]], int(b[3])
        try:
            return date(an, me, g).isoformat()
        except ValueError:
            continue
    return None


def trova_ente(titolo, testo):
    t = norm(titolo)
    for nome, chiavi in ENTI_EXTRA.items():  # enti specifici prima: "ASP Catania" non è il Comune di Catania
        if any(k in t for k in chiavi):
            return nome
    for c in COMUNI:  # poi i comuni, nel titolo: è il segnale più affidabile
        if re.search(r"\b" + re.escape(norm(c)) + r"\b", t):
            return "Comune di " + c
    inizio = norm(testo[:500])  # fallback: solo l'inizio del testo, per non pescare citazioni casuali
    for c in COMUNI:
        if re.search(r"comune di " + re.escape(norm(c)) + r"\b", inizio):
            return "Comune di " + c
    return None


def trova_profilo(titolo):
    t = norm(titolo)
    for nome, rx in PROFILI:
        if re.search(rx, t):
            return nome
    return "Vari profili"


def _titoli(t):
    """Porta i sinonimi più comuni a tre parole chiave: laurea / licenzamedia / diploma."""
    t = re.sub(r"diploma di (?:specializzazione|perfezionamento|abilitazione)|\bmaster\b", " ", t)
    t = re.sub(r"diploma accademico|diploma universitario|diploma (?:di )?laurea|dottorato(?: di ricerca)?|laurea", " laurea ", t)
    t = re.sub(r"(?:diploma|licenza|titolo)(?: di)?(?: istruzione| scuola)?(?: secondaria)? di primo grado|licenza media|licenza di scuola media|scuola dell'obbligo|obbligo scolastico|assolvimento dell'obbligo", " licenzamedia ", t)
    return t


def trova_studio(testo):
    t = _titoli(norm(testo))
    trovati = set()
    if "laurea" in t: trovati.add("Laurea")
    if "licenzamedia" in t: trovati.add("Licenza media")
    if re.search(r"diploma|maturita|secondo grado|secondaria superiore", t): trovati.add("Diploma")
    return trovati.pop() if len(trovati) == 1 else ("Vari" if trovati else "Non indicato")


def deduci_studio(testo):
    """Quando il bando non dichiara il titolo, lo si deduce dall'inquadramento (solo casi molto standard)."""
    t = norm(testo)
    if re.search(r"funzionari|elevata qualificazione|dirigent|dirigenza|professore|ricercatore|docente di prima fascia|medico|infermier|ingegner|architett|assistente social|assistente sanitari|\bcat(?:egoria)?\.? ?d\b", t):
        return "Laurea"
    if re.search(r"istruttor|assistente(?! social| sanitari)|\bcat(?:egoria)?\.? ?c\b", t):
        return "Diploma"
    return None


def trova_posti(testo):
    m = re.search(r"(\d{1,3})\s+(?:posti|unita|figure)", norm(testo))
    return int(m[1]) if m else None


def costruisci(titolo, testo, url, pub):
    ente = trova_ente(titolo, testo)
    if not ente:
        return None
    scad = trova_data(testo)
    return {
        "prov": PROV, "ente": ente, "titolo": titolo, "profilo": trova_profilo(titolo),
        "studio": trova_studio(testo), "posti": trova_posti(testo),
        "pub": pub, "scad": scad, "url": url,
        "nota": "" if scad else "Scadenza non rilevata automaticamente: verificare sul bando",
    }


# ---------- Fonte 1: inPA (API WordPress pubblica) ----------
# ATTENZIONE: struttura ricavata dalla documentazione di terzi, non verificata dal vivo.
# Se inPA cambia, lanciare `--probe` e adattare solo questa funzione.
INPA = "https://www.inpa.gov.it/wp-json/wp/v2/posts"


def fetch_inpa(termini, pagine=2, probe=False):
    visti = {}
    for q in termini:
        for p in range(1, pagine + 1):
            try:
                r = requests.get(INPA, params={"search": q, "per_page": 50, "page": p}, headers=UA, timeout=30)
            except requests.RequestException as e:
                print(f"  ! {q}: {e}", file=sys.stderr); break
            if r.status_code != 200:
                break
            items = r.json()
            if probe:
                print(json.dumps(items[:1], ensure_ascii=False, indent=1)[:3000]); return {}
            if not items:
                break
            for it in items:
                url = it.get("link", "")
                titolo = pulisci((it.get("title") or {}).get("rendered"))
                testo = pulisci((it.get("content") or {}).get("rendered"))
                rec = costruisci(titolo, testo, url, (it.get("date") or "")[:10])
                if rec and url not in visti:
                    visti[url] = rec
            time.sleep(PAUSA)
    return visti


# ---------- Fonte 2: siti dei singoli comuni (da configurare uno a uno) ----------
# Esempio di voce (i selettori CSS vanno ricavati ispezionando la pagina "Bandi di concorso"):
# "Comune di Paternò": {"url": "https://...", "item": "article.bando", "titolo": "h2 a", "link": "h2 a"}
SITI_COMUNI = {}


def fetch_siti():
    from bs4 import BeautifulSoup
    out = {}
    for ente, c in SITI_COMUNI.items():
        try:
            r = requests.get(c["url"], headers=UA, timeout=30); r.raise_for_status()
        except requests.RequestException as e:
            print(f"  ! {ente}: {e}", file=sys.stderr); continue
        soup = BeautifulSoup(r.text, "html.parser")
        for el in soup.select(c["item"]):
            a = el.select_one(c["link"]); t = el.select_one(c["titolo"])
            if not a or not t: continue
            url = requests.compat.urljoin(c["url"], a.get("href", ""))
            rec = costruisci(t.get_text(" ", strip=True) + " " + ente, el.get_text(" ", strip=True), url, date.today().isoformat())
            if rec:
                rec["ente"] = ente
                out[url] = rec
        time.sleep(PAUSA)
    return out


# ---------- Fonte 1b: API del portale inPA (più completa, NON documentata ufficialmente) ----------
PORTALE = "https://portale.inpa.gov.it/concorsi-smart/api/concorso-public-area/search-better"


def probe_portale(body=None):
    """Stampa la risposta grezza: serve a capire quali filtri accetta l'API."""
    r = requests.post(PORTALE, params={"page": 0, "size": 3}, json=body or {},
                      headers={**UA, "Accept": "application/json"}, timeout=30)
    print("STATUS:", r.status_code, "|", r.headers.get("content-type"))
    print(r.text[:3500])


DETTAGLIO = "https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id="


def giorno(s):
    return (s or "")[:10] or None


def da_portale(it):
    """Converte un bando del portale nel nostro formato; None se non è in provincia di Catania."""
    sedi = it.get("sedi") or []
    if REGIONE not in sedi:
        return None
    province = [p for p in sedi if p in PROVINCE]
    prov = province[0] if province else REGIONE  # nessuna provincia indicata = bando regionale
    oggi = date.today().isoformat()
    pub = giorno(it.get("dataPubblicazione"))
    if not pub or pub > oggi:  # nei dati inPA esistono date di pubblicazione nel futuro: usa la visibilità
        pub = giorno(it.get("dataVisibilita")) or oggi
    scad, nota = giorno(it.get("dataScadenza")), ""
    if scad and int(scad[:4]) > int(pub[:4]) + 2:  # scadenze assurde (es. 2029): meglio dichiararle ignote
        scad, nota = None, "Data di scadenza anomala nei dati inPA: verificare sul bando"
    elif not scad:
        nota = "Scadenza non indicata: verificare sul bando"
    titolo = pulisci(it.get("titolo"))
    figura = pulisci(it.get("figuraRicercata"))
    profilo = trova_profilo(figura)
    if profilo == "Vari profili":
        profilo = trova_profilo(titolo)
    studio, dedotto = trova_studio(pulisci(it.get("descrizione")) + " " + titolo), False
    if studio == "Non indicato":
        d = deduci_studio(titolo + " " + figura)
        if d:
            studio, dedotto = d, True
    return {
        "prov": prov, "province": province or [REGIONE], "ente": ", ".join(it.get("entiRiferimento") or []) or "Ente non indicato",
        "titolo": titolo, "profilo": profilo,
        "studio": studio, "studio_dedotto": dedotto, "posti": it.get("numPosti"),
        "pub": pub, "scad": scad, "url": DETTAGLIO + str(it.get("id")),
        "categoria": ", ".join(it.get("categorie") or []), "nota": nota,
    }


def fetch_portale(max_pagine=60, size=50, inizio=0):
    """Scorre le pagine del portale e tiene solo i bandi con sede in Sicilia."""
    out, visti = {}, 0
    interrotta = None
    for p in range(inizio, max_pagine):
        try:
            r = richiedi("post", PORTALE, params={"page": p, "size": size}, json={},
                         headers={**UA, "Accept": "application/json"}, timeout=40)
        except RequestException as e:
            interrotta = p; print(f"  ! pagina {p}: {type(e).__name__}", file=sys.stderr); break
        if r.status_code != 200:
            interrotta = p; print(f"  ! pagina {p}: HTTP {r.status_code}", file=sys.stderr); break
        d = r.json()
        items = d.get("content") or []
        visti += len(items)
        for it in items:
            rec = da_portale(it)
            if rec:
                out[rec["url"]] = rec
        if p % 10 == 0:
            print(f"  pagina {p + 1}/{d.get('totalPages', '?')}: {visti} bandi letti, {len(out)} in Sicilia")
        if d.get("last") or not items or p + 1 >= (d.get("totalPages") or 1):
            break
        time.sleep(PAUSA)
    print(f"Letti {visti} bandi nazionali, {len(out)} in Sicilia.")
    if interrotta is not None:
        print(f"\n*** ATTENZIONE: lettura interrotta alla pagina {interrotta}: i dati di questo giro sono PARZIALI. ***\n"
              f"*** I dati già in archivio non si perdono. Per riprendere: python scrape.py --da-pagina {interrotta} --pagine {max_pagine} ***\n")
    return out


def estrai_pdf(b):
    """Molti bandi sono firmati digitalmente (.p7m): il PDF sta dentro una busta. Qui lo si estrae."""
    if b.startswith(b"%PDF"):
        return b
    idx = b.find(b"%PDF")
    if idx < 2:
        return b
    for k in range(2, 8):  # cerca l'intestazione "OCTET STRING" che precede il PDF
        p = idx - k
        if p < 0 or b[p] != 0x04:
            continue
        L = b[p + 1]
        hdr, n = (2, L) if L < 0x80 else (2 + (L & 0x7F), int.from_bytes(b[p + 2: p + 2 + (L & 0x7F)], "big"))
        if p + hdr == idx:
            out, pos = b"", p
            while pos < len(b) and b[pos] == 0x04:  # il contenuto è spezzato in blocchi da ~1000 byte
                L = b[pos + 1]
                hdr, n = (2, L) if L < 0x80 else (2 + (L & 0x7F), int.from_bytes(b[pos + 2: pos + 2 + (L & 0x7F)], "big"))
                out += b[pos + hdr: pos + hdr + n]
                pos += hdr + n
            return out
    return b


def studio_da_bando(testo):
    """Cerca laurea/diploma/licenza solo dove il contesto parla di requisiti (e non di punteggi dei titoli)."""
    t = _titoli(norm(testo))
    trovati = set()
    for m in re.finditer(r"laurea|licenzamedia|diploma|maturita|secondo grado|secondaria superiore", t):
        ctx = t[max(0, m.start() - 200): m.end() + 120]
        if not re.search(r"requisit|possesso|possedere|richiest|ammess|titolo di studio|titoli di studio", ctx):
            continue
        if re.search(r"\bpunti\b|punteggio|valutabil|valutazione dei titoli|merito", ctx):
            continue
        trovati.add({"laurea": "Laurea", "licenzamedia": "Licenza media"}.get(m[0], "Diploma"))
    return trovati.pop() if len(trovati) == 1 else ("Vari" if trovati else "Non indicato")


class PdfIllegibile(Exception):
    pass


OCR_PAGINE = 6  # i requisiti stanno quasi sempre nelle prime pagine: basta leggere quelle


def _scarica_pdf(media_id):
    time.sleep(PAUSA)
    pdf = richiedi("get", "https://portale.inpa.gov.it/api/media/" + media_id, headers=UA, timeout=60).content
    return b"" if len(pdf) > 25_000_000 else estrai_pdf(pdf)


def ocr_pdf(pdf):
    """Riconoscimento ottico dei caratteri per i PDF che sono scansioni. Richiede Tesseract + pymupdf + pytesseract."""
    import pytesseract
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    from PIL import Image
    win = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if win.exists():
        pytesseract.pytesseract.tesseract_cmd = str(win)
    parti = []
    for i, pagina in enumerate(fitz.open(stream=pdf, filetype="pdf")):
        if i >= OCR_PAGINE:
            break
        img = Image.open(io.BytesIO(pagina.get_pixmap(dpi=200).tobytes("png")))
        parti.append(pytesseract.image_to_string(img, lang="ita"))
    return " ".join(parti)


def controlla_ocr():
    """Verifica PRIMA di cominciare che l'OCR sia utilizzabile, per non perdere tempo."""
    try:
        import pytesseract
        try:
            import pymupdf
        except ImportError:
            import fitz
        from PIL import Image
    except ImportError as e:
        print(f"\nOCR non disponibile: manca la libreria '{e.name}'.\n  -> installala con:  pip install -r requirements-ocr.txt\n  (serve anche il programma Tesseract con la lingua italiana)")
        return False
    win = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if win.exists():
        pytesseract.pytesseract.tesseract_cmd = str(win)
    try:
        lingue = pytesseract.get_languages()
    except Exception:
        print("\nOCR non disponibile: il programma Tesseract non è stato trovato.\n  -> installalo (versione per Windows di UB Mannheim) nel percorso predefinito C:\\Program Files\\Tesseract-OCR")
        return False
    if "ita" not in lingue:
        print("\nOCR non disponibile: a Tesseract manca la lingua italiana.\n  -> reinstallalo spuntando 'Italian' in 'Additional language data'")
        return False
    return True


def testo_pdf(media_id, ocr=False):
    """(testo, usato_ocr). Usa la cache se il PDF è già stato scaricato. Una pagina rovinata non butta via le altre."""
    f, fo = CACHE / (media_id + ".txt.gz"), CACHE / (media_id + ".ocr.txt.gz")
    pdf = None
    if f.exists():
        testo = gzip.decompress(f.read_bytes()).decode("utf-8", "ignore")
    else:
        pdf = _scarica_pdf(media_id)
        if not pdf:
            return "", False
        try:
            from pypdf import PdfReader
            parti = []
            for p in PdfReader(io.BytesIO(pdf)).pages[:15]:
                try:
                    parti.append(p.extract_text() or "")
                except Exception:
                    parti.append("")
            testo = " ".join(parti)
        except Exception as e:
            if not ocr:
                raise PdfIllegibile(type(e).__name__)
            testo = ""
        CACHE.mkdir(exist_ok=True)
        f.write_bytes(gzip.compress(testo.encode("utf-8")))
    if len(testo.strip()) < 200 and ocr:  # sembra una scansione
        if fo.exists():
            return gzip.decompress(fo.read_bytes()).decode("utf-8", "ignore"), True
        pdf = pdf or _scarica_pdf(media_id)
        t = ocr_pdf(pdf) if pdf else ""
        CACHE.mkdir(exist_ok=True)
        fo.write_bytes(gzip.compress(t.encode("utf-8")))
        return t, True
    return testo, False


def definitivo(fonte, analisi=None, ocr=False):
    """Un bando va riletto solo se il PDF non è mai stato davvero provato (o con un riconoscimento superato)."""
    if not fonte:
        return False
    if ocr and "senza testo" in fonte:
        return False  # con --ocr le scansioni si riprovano
    if fonte.startswith("bando PDF (nessun titolo"):
        return analisi == ANALISI
    return fonte.startswith("bando PDF") or fonte in ("requisiti", "nessun PDF allegato")


def arricchisci(nuovi, usa_pdf=True, anche_vecchi=False, salva_ogni=100, ocr=False):
    """Per i bandi senza titolo di studio dichiarato: legge il dettaglio e il PDF del bando. Il risultato si salva e non si rifà."""
    vecchi = {c["url"]: c for c in (json.loads(OUT.read_text("utf-8")) if OUT.exists() else [])}
    base = PORTALE.replace("/search-better", "")
    anno_fa = date.today().replace(year=date.today().year - 1).isoformat()
    da_fare = []
    for url, rec in nuovi.items():  # 1) riporta subito quanto già noto, così i salvataggi parziali non perdono nulla
        v = vecchi.get(url) or {}
        if v.get("media") is not None:
            rec["media"] = v["media"]
        if definitivo(v.get("studio_fonte"), v.get("analisi"), ocr):
            rec.update(studio=v["studio"], studio_dedotto=v.get("studio_dedotto", False), studio_fonte=v["studio_fonte"], analisi=v.get("analisi"))
            continue
        if rec["studio"] not in ("Non indicato", "Vari") and not rec.get("studio_dedotto"):
            continue  # titolo già chiaro: non serve il PDF
        riferimento = rec.get("scad") or rec.get("pub")
        if not anche_vecchi and riferimento and riferimento < anno_fa:
            continue  # bando vecchio di oltre un anno: non vale il tempo di scaricare il PDF
        da_fare.append(url)
    print(f"Bandi da leggere: {len(da_fare)} (circa {len(da_fare) * 2 // 60 + 1} minuti). Ctrl+C per interrompere: quanto fatto viene salvato.")
    fatti = illeggibili = errori = di_fila = 0
    try:
        for i, url in enumerate(da_fare, 1):
            rec = nuovi[url]
            try:
                st, fonte, ids = "Non indicato", "requisiti", rec.get("media")
                if ids is None:
                    d = richiedi("get", base + "/" + url.rsplit("=", 1)[-1], headers={**UA, "Accept": "application/json"}, timeout=30).json()
                    st = studio_da_bando(pulisci(d.get("requisitiSpecifici")))
                    ids = rec["media"] = [al["mediaId"] for al in d.get("allegati") or [] if al.get("tipo") == "BANDO_CONCORSO" and al.get("mediaId")]
                    time.sleep(PAUSA)
                if st == "Non indicato" and usa_pdf:
                    fonte, lung, con_ocr = ("bando PDF" if ids else "nessun PDF allegato"), [], False
                    for mid in ids:
                        testo, usato = testo_pdf(mid, ocr)
                        con_ocr = con_ocr or usato
                        lung.append(len(testo.strip()))
                        st = studio_da_bando(testo)
                        if st != "Non indicato":
                            break
                    if st != "Non indicato" and con_ocr:
                        fonte = "bando PDF (OCR)"
                    elif st == "Non indicato" and ids:
                        corto = max(lung or [0]) < 200
                        fonte += (" (OCR, testo insufficiente)" if corto else " (OCR, nessun titolo trovato)") if con_ocr else (" (senza testo: scansione?)" if corto else " (nessun titolo trovato)")
                if st != "Non indicato":
                    rec.update(studio=st, studio_dedotto=False, studio_fonte=fonte, analisi=ANALISI); fatti += 1
                elif usa_pdf:
                    rec.update(studio_fonte=fonte, analisi=ANALISI)
                # con --senza-pdf non si segna nulla: il PDF non è stato provato
                di_fila = 0
            except PdfIllegibile:
                rec.update(studio_fonte="bando PDF (illeggibile)", analisi=ANALISI); illeggibili += 1; di_fila = 0
            except Exception as e:  # problema di rete o altro: il bando verrà riprovato al giro dopo
                errori += 1; di_fila += 1
                if errori <= 5:
                    print(f"  ! {url[-8:]}: {type(e).__name__}", file=sys.stderr)
                if isinstance(e, ImportError):
                    print(f"\n*** Manca la libreria '{e.name}': mi fermo e salvo. Installa quanto serve e rilancia. ***")
                    break
                if di_fila >= 3:
                    print(f"\n*** {di_fila} errori di fila (ultimo: {type(e).__name__}): mi fermo e salvo. Se la rete è caduta, rilancia quando torna. ***")
                    break
            if i % 20 == 0:
                print(f"  [{i}/{len(da_fare)}] titolo trovato: {fatti} | PDF illeggibili: {illeggibili} | errori: {errori}")
            if i % salva_ogni == 0:
                unisci(nuovi)
    except KeyboardInterrupt:
        print("\nInterrotto: salvo quanto fatto finora.")
    print(f"Fatto: titolo di studio letto dal bando per {fatti} concorsi; PDF illeggibili: {illeggibili}; errori da riprovare: {errori}.")


def diagnosi(parte):
    """Mostra cosa c'è nel PDF di un bando e perché il titolo di studio è stato (o no) trovato."""
    d = json.loads(OUT.read_text("utf-8"))
    c = next((x for x in d if parte in x["url"]), None)
    if not c:
        print("Bando non trovato in data/concorsi.json"); return
    print("TITOLO:", c["titolo"][:120], "\nSTUDIO SALVATO:", c["studio"], "|", c.get("studio_fonte"))
    ids = c.get("media")
    if ids is None:
        dd = requests.get(PORTALE.replace("/search-better", "") + "/" + c["url"].rsplit("=", 1)[-1], headers={**UA, "Accept": "application/json"}, timeout=30).json()
        ids = [al["mediaId"] for al in dd.get("allegati") or [] if al.get("mediaId")]
        print("Allegati:", [(al.get("tipo"), al.get("mediaId")) for al in dd.get("allegati") or []])
    for mid in ids:
        t, _ = testo_pdf(mid)
        print(f"\n--- PDF {mid}: {len(t)} caratteri di testo ---")
        n = norm(t)
        for m in list(re.finditer(r"laurea|diploma|titolo di studio|licenza media|requisiti", n))[:6]:
            print("  …" + re.sub(r"\s+", " ", n[max(0, m.start() - 100): m.end() + 160]) + "…")
        print("RISULTATO DEL RICONOSCIMENTO:", studio_da_bando(t))


def probe_dettaglio(id_bando):
    """Stampa il dettaglio di un bando: serve a vedere se contiene i requisiti (titolo di studio)."""
    r = requests.get(PORTALE.replace("/search-better", "") + "/" + id_bando, headers={**UA, "Accept": "application/json"}, timeout=30)
    print("STATUS:", r.status_code)
    print(r.text[:6000])


def report(percorso=None):
    """Riepilogo dei dati raccolti: serve a capire perché restano dei 'Non indicato'."""
    from collections import Counter
    d = json.loads((percorso or OUT).read_text("utf-8"))
    anno_fa = date.today().replace(year=date.today().year - 1).isoformat()
    vecchio = lambda c: bool(c.get("scad") or c.get("pub")) and (c.get("scad") or c.get("pub")) < anno_fa
    print(f"Totale bandi: {len(d)}")
    print("Titolo di studio:", dict(Counter(c["studio"] for c in d)))
    print("Come è stato ricavato:", dict(Counter(c.get("studio_fonte") or ("dedotto dal profilo" if c.get("studio_dedotto") else "descrizione / non letto") for c in d)))
    ni = [c for c in d if c["studio"] == "Non indicato"]
    v = [c for c in ni if vecchio(c)]
    r = [c for c in ni if not vecchio(c)]
    print(f"\n'Non indicato': {len(ni)}")
    print(f"  - scaduti da oltre un anno (PDF non letti per scelta): {len(v)}")
    print(f"  - recenti o attivi: {len(r)}")
    print(f"      di cui PDF/dettaglio già letto senza trovare il titolo: {sum(1 for c in r if c.get('studio_fonte'))}")
    print(f"      di cui mai letti (errore o da rifare): {sum(1 for c in r if not c.get('studio_fonte'))}")
    print("  Tipo di bando tra i recenti:", dict(Counter(c.get("categoria") or "?" for c in r).most_common(6)))
    print("\nEsempi di recenti senza titolo:")
    for c in r[:5]:
        print("  *", c["titolo"][:90], "|", c["url"][-12:])


def unisci(nuovi):
    vecchi = json.loads(OUT.read_text("utf-8")) if OUT.exists() else []
    per_url = {c["url"]: c for c in vecchi}
    for url, n in nuovi.items():
        v = per_url.get(url)
        if v:
            n["pub"] = v.get("pub") or n["pub"]          # la data di pubblicazione non si sposta
            n["scad"] = n["scad"] or v.get("scad")       # non perdere una scadenza già trovata
        per_url[url] = n
    tutti = sorted(per_url.values(), key=lambda c: c.get("pub") or "", reverse=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(tutti, ensure_ascii=False, indent=1), "utf-8")
    return len(tutti), len(nuovi)


def selftest():
    t = "Il Comune di Paternò indice concorso per 2 posti di istruttore amministrativo. Requisito: diploma. Scadenza: 15 maggio 2026."
    assert trova_ente("Concorso Comune di Paternò", t) == "Comune di Paternò"
    assert trova_ente("Avviso ASP Catania infermieri", "") == "ASP Catania"
    assert trova_ente("Concorso Comune di Torino", "sede Milano") is None
    assert trova_data(t) == "2026-05-15"
    assert trova_data("domande entro il 30/10/2023") == "2023-10-30"
    assert trova_profilo("Istruttore amministrativo") == "Amministrativo"
    assert trova_profilo("Agenti di Polizia Locale") == "Polizia locale"
    assert trova_studio(t) == "Diploma" and trova_studio("laurea o diploma") == "Vari"
    assert trova_posti(t) == 2
    ex = {"sedi": ["Sicilia", "Catania"], "id": "abc", "titolo": "Mobilità Funzionario Informatico", "figuraRicercata": "FUNZIONARIO INFORMATICO",
          "descrizione": "<p>Laurea richiesta</p>", "dataPubblicazione": "2099-07-22T09:04:00Z", "dataVisibilita": "2026-10-22T09:06:00Z",
          "dataScadenza": "2029-10-22T09:06:00Z", "numPosti": 2, "entiRiferimento": ["Comune di Paternò"], "categorie": ["Concorso"]}
    r = da_portale(ex)
    assert r["pub"] == "2026-10-22" and r["scad"] is None and r["profilo"] == "Informatico" and r["studio"] == "Laurea" and r["posti"] == 2
    assert da_portale({**ex, "sedi": ["Campania", "Avellino"]}) is None
    assert deduci_studio("Istruttore amministrativo") == "Diploma" and deduci_studio("Funzionario tecnico") == "Laurea"
    assert deduci_studio("Operatore tecnico") is None
    assert deduci_studio("Assistente settore amministrativo/gestionale") == "Diploma" and deduci_studio("Assistente sociale") == "Laurea"
    assert trova_studio("possesso del diploma di laurea in fisica") == "Laurea"
    assert trova_studio("laurea o diploma di maturità") == "Vari"
    assert studio_da_bando("Art. 2 Requisiti di ammissione: diploma di laurea in fisica. Art. 5 Prove d'esame") == "Laurea"
    assert studio_da_bando("Art. 1 oggetto del bando, sede di Catania") == "Non indicato"
    import os
    pdf = b"%PDF-1.4 " + os.urandom(2500)
    p7m = b"0\x80\x06\t*\x86H\x86\xf7\r\x01\x07\x02\xa0\x80" + b"".join(b"\x04\x82" + len(pdf[i:i + 1000]).to_bytes(2, "big") + pdf[i:i + 1000] for i in range(0, len(pdf), 1000)) + b"\x00\x00\x00\x00"
    assert estrai_pdf(p7m) == pdf and estrai_pdf(pdf) == pdf
    assert da_portale({**ex, "sedi": ["Sicilia", "Palermo"]})["prov"] == "Palermo"
    assert da_portale({**ex, "sedi": ["Sicilia"]})["prov"] == "Sicilia"
    assert da_portale({**ex, "sedi": ["Sicilia", "Messina", "Sicilia", "Trapani"]})["province"] == ["Messina", "Trapani"]
    assert studio_da_bando("E\' richiesto il possesso del diploma di istruzione secondaria di primo grado.") == "Licenza media"
    assert studio_da_bando("Requisiti: diploma accademico di secondo livello in canto") == "Laurea"
    assert studio_da_bando("Requisiti di ammissione: diploma di scuola secondaria di secondo grado") == "Diploma"
    assert studio_da_bando("Valutazione dei titoli: dottorato di ricerca punti 5, diploma di specializzazione punti 3") == "Non indicato"
    assert studio_da_bando("Possono partecipare coloro in possesso di laurea oppure del diploma di maturità") == "Vari"
    print("selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true"); ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--probe-portale", action="store_true"); ap.add_argument("--body", help="JSON del corpo della richiesta"); ap.add_argument("--probe-dettaglio", metavar="ID"); ap.add_argument("--report", action="store_true", help="riepilogo dei dati raccolti"); ap.add_argument("--diagnosi", metavar="ID", nargs="+", help="mostra perché uno o più bandi non hanno il titolo di studio"); ap.add_argument("--senza-pdf", action="store_true", help="non scaricare i PDF dei bandi"); ap.add_argument("--ocr", action="store_true", help="leggi con OCR i PDF che sono scansioni (richiede Tesseract)"); ap.add_argument("--pdf-anche-vecchi", action="store_true", help="leggi i PDF anche dei bandi scaduti da oltre un anno"); ap.add_argument("--solo-dettagli", action="store_true", help="salta la lettura delle pagine: lavora sui bandi già in archivio (titolo di studio/PDF/OCR)"); ap.add_argument("--da-pagina", type=int, default=0, help="riprendi la lettura da questa pagina"); ap.add_argument("--pagine", type=int, default=60, help="pagine massime da leggere (50 bandi per pagina)")
    a = ap.parse_args()
    if a.selftest: selftest(); sys.exit()
    if a.report: report(); sys.exit()
    if a.diagnosi:
        for x in a.diagnosi: diagnosi(x); print("=" * 60)
        sys.exit()
    if a.probe_dettaglio: probe_dettaglio(a.probe_dettaglio); sys.exit()
    if a.probe_portale: probe_portale(json.loads(a.body) if a.body else None); sys.exit()
    if a.probe: fetch_inpa(["Catania"], probe=True); sys.exit()
    if a.ocr and not controlla_ocr():
        sys.exit(1)
    if a.solo_dettagli:
        print("Salto la lettura delle pagine: uso i bandi già in archivio.")
        nuovi = {c["url"]: c for c in json.loads(OUT.read_text("utf-8"))}
    else:
        print("Leggo il portale inPA…")
        nuovi = fetch_portale(a.pagine, inizio=a.da_pagina)
    if SITI_COMUNI: nuovi.update(fetch_siti())
    arricchisci(nuovi, usa_pdf=not a.senza_pdf, anche_vecchi=a.pdf_anche_vecchi, ocr=a.ocr)
    tot, n = unisci(nuovi)
    print(f"Fatto: {n} trovati in questa esecuzione, {tot} in archivio -> {OUT}")
