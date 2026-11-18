#!/usr/bin/env python3
"""Raccoglie notizie sulla pubblica amministrazione da feed RSS/Atom pubblici -> data/notizie.json

Si salvano solo titolo, link, data, fonte e un breve estratto: il testo completo resta sul sito dell'editore.
Uso:
  python notizie.py            # aggiorna data/notizie.json
  python notizie.py --prova    # controlla ogni feed (stato, numero di notizie, prima notizia) senza salvare
"""
import argparse, json, re, sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from scrape import RequestException, UA, pulisci, richiedi

OUT = Path(__file__).parent / "data" / "notizie.json"
MAX_NOTIZIE, GIORNI, ESTRATTO = 600, 365, 200

# Aggiungi o togli fonti qui. Un feed che non risponde viene saltato con un avviso: non blocca gli altri.
# (Gli indirizzi dei feed WordPress sono quelli standard: usa --prova per verificarli.)
FONTI = [
    {"nome": "Lentepubblica", "url": "https://www.lentepubblica.it/feed/"},
    {"nome": "FORUM PA", "url": "https://www.forumpa.it/feed/"},
    {"nome": "ANCI", "url": "https://www.anci.it/feed/"},
]
ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/elements/1.1/}"


def _t(el, *tag):
    for t in tag:
        x = el.find(t)
        if x is not None and (x.text or "").strip():
            return x.text.strip()
    return ""


def pulisci_link(url):
    p = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


def estratto(testo):
    t = pulisci(testo)
    t = re.sub(r"\s*(The post .*|L.articolo .* proviene da .*|Leggi tutto.*|Continua a leggere.*)$", "", t, flags=re.I).replace("[…]", "").replace("[&#8230;]", "").strip()
    if len(t) <= ESTRATTO:
        return t
    return t[:ESTRATTO].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


def data_iso(s):
    s = (s or "").strip()
    for f in (lambda x: parsedate_to_datetime(x), lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))):
        try:
            d = f(s)
            return (d.astimezone(timezone.utc) if d.tzinfo else d).date().isoformat()
        except Exception:
            continue
    return None


def parse_feed(contenuto, fonte):
    root = ET.fromstring(contenuto)
    voci = []
    for it in root.iter("item"):                                  # RSS 2.0
        voci.append((_t(it, "title"), _t(it, "link"), _t(it, "description"), _t(it, "pubDate", DC + "date"),
                     [c.text.strip() for c in it.findall("category") if c.text]))
    for en in root.iter(ATOM + "entry"):                          # Atom
        link = next((l.get("href") for l in en.findall(ATOM + "link") if l.get("rel") in (None, "alternate")), "")
        voci.append((_t(en, ATOM + "title"), link or "", _t(en, ATOM + "summary", ATOM + "content"), _t(en, ATOM + "published", ATOM + "updated"),
                     [c.get("term") for c in en.findall(ATOM + "category") if c.get("term")]))
    out = []
    for titolo, link, desc, data, cats in voci:
        d = data_iso(data)
        if not titolo or not link or not d:
            continue
        cats = [c for c in cats if c.lower() not in ("uncategorized", "senza categoria")][:3]
        out.append({"titolo": pulisci(titolo), "link": pulisci_link(link), "data": d, "fonte": fonte, "estratto": estratto(desc), "categorie": cats})
    return out


def scarica(f):
    r = richiedi("get", f["url"], tentativi=3, timeout=30,
                 headers={**UA, "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"})
    if r.status_code != 200:
        raise RequestException(f"HTTP {r.status_code}")
    return parse_feed(r.content, f["nome"])


def prova():
    for f in FONTI:
        try:
            v = scarica(f)
            print(f"OK   {f['nome']}: {len(v)} notizie" + (f" | prima: {v[0]['data']} {v[0]['titolo'][:70]}" if v else ""))
        except Exception as e:
            print(f"KO   {f['nome']} ({f['url']}): {type(e).__name__}: {e}")


def aggiorna():
    per_link = {n["link"]: n for n in (json.loads(OUT.read_text("utf-8")) if OUT.exists() else [])}
    for f in FONTI:
        try:
            v = scarica(f)
            per_link.update({n["link"]: n for n in v})
            print(f"{f['nome']}: {len(v)} notizie lette")
        except Exception as e:
            print(f"  ! {f['nome']}: {type(e).__name__}: {e}", file=sys.stderr)
    limite = (date.today() - timedelta(days=GIORNI)).isoformat()
    tutte = sorted((n for n in per_link.values() if n["data"] >= limite), key=lambda n: n["data"], reverse=True)[:MAX_NOTIZIE]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(tutte, ensure_ascii=False, indent=1), "utf-8")
    print(f"Fatto: {len(tutte)} notizie in archivio -> {OUT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--prova", action="store_true")
    prova() if ap.parse_args().prova else aggiorna()
