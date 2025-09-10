import argparse
import csv
import re
import time
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime
import requests

# ------- logging -------
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ------- normalization helpers -------
def norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s

def norm_punct(s: str) -> str:
    s = norm(s)
    s = re.sub(r"[^\w\s&]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s

def only_digits(s: str) -> str:
    return re.sub(r"\D+", "", s or "")

def get_year_from_issued(item: dict) -> str:
    issued = item.get("issued", {})
    parts = issued.get("date-parts") or []
    if parts and parts[0] and len(parts[0]) >= 1:
        return str(parts[0][0])
    return ""

# ------- journal abbreviation map (extend as you go) -------
JOURNAL_MAP = {
    "sci rep": "Scientific Reports",
    "chem commun": "Chemical Communications",
    "chem. commun.": "Chemical Communications",
    "dalton trans": "Dalton Transactions",
    "phys chem chem phys": "Physical Chemistry Chemical Physics",
    "phys. chem. chem. phys.": "Physical Chemistry Chemical Physics",
    "appl phys lett": "Applied Physics Letters",
    "appl. phys. lett.": "Applied Physics Letters",
    "appl surf sci": "Applied Surface Science",
    "appl. surf. sci.": "Applied Surface Science",
    "acs nano": "ACS Nano",
    "acs mater au": "ACS Materials Au",
    "acs appl electron mater": "ACS Applied Electronic Materials",
    "acs appl. electron. mater.": "ACS Applied Electronic Materials",
    "chem rev": "Chemical Reviews",
    "chem. rev.": "Chemical Reviews",
    "j photochem photobiol c photochem rev": "Journal of Photochemistry and Photobiology C: Photochemistry Reviews",
    "j. photochem. photobiol. c photochem. rev.": "Journal of Photochemistry and Photobiology C: Photochemistry Reviews",
    "j mater chem c": "Journal of Materials Chemistry C",
    "j. mater. chem. c": "Journal of Materials Chemistry C",
    "j nanophotonics": "Journal of Nanophotonics",
    "j vac sci technol a": "Journal of Vacuum Science & Technology A",
    "j vac sci technol b microelectron nanometer struct process meas phenom": "Journal of Vacuum Science & Technology B",
    "j. vac. sci. technol. a": "Journal of Vacuum Science & Technology A",
    "j. vac. sci. technol. b microelectron nanometer struct process meas phenom": "Journal of Vacuum Science & Technology B",
    "j chem phys": "The Journal of Chemical Physics",
    "j. chem. phys.": "The Journal of Chemical Physics",
    "j appl phys": "Journal of Applied Physics",
    "j. appl. phys.": "Journal of Applied Physics",
    "j phys chem c": "The Journal of Physical Chemistry C",
    "j. phys. chem. c": "The Journal of Physical Chemistry C",
    "j phys chem lett": "The Journal of Physical Chemistry Letters",
    "j. phys. chem. lett.": "The Journal of Physical Chemistry Letters",
    "laser photonics rev": "Laser & Photonics Reviews",
    "laser photonics rev.": "Laser & Photonics Reviews",
    "rsc adv": "RSC Advances",
    "rsc adv.": "RSC Advances",
    "nat methods": "Nature Methods",
    "j fluoresc": "Journal of Fluorescence",
    "j clinmicrobiol": "Journal of Clinical Microbiology",
    "mater sci semicond process": "Materials Science in Semiconductor Processing",
    "mater. sci. semicond. process.": "Materials Science in Semiconductor Processing",
    "mater sci eng r rep": "Materials Science and Engineering: R: Reports",
    "mater sci eng r rep.": "Materials Science and Engineering: R: Reports",
    "recl trav chim pays-bas": "Recueil des Travaux Chimiques des Pays-Bas",
}

def expand_journal(j: str) -> str:
    jn = norm_punct(j)
    return JOURNAL_MAP.get(jn, j)

# ------- TXT parsing -------
def join_wrapped_refs(txt_path: Path) -> List[str]:
    """
    Combine wrapped lines: each reference starts with [n]; subsequent lines
    belong to the same reference until the next [m].
    """
    lines = [ln.rstrip() for ln in txt_path.read_text(encoding="utf-8").splitlines()]
    refs = []
    cur = ""
    for ln in lines:
        if re.match(r"\s*\[\d+\]", ln):  # new ref starts
            if cur.strip():
                refs.append(cur.strip())
            cur = ln.strip()
        else:
            cur += " " + ln.strip()
    if cur.strip():
        refs.append(cur.strip())
    return refs

def strip_bracket_index(line: str) -> Tuple[Optional[int], str]:
    m = re.match(r"\s*\[(\d+)\]\s*(.*)", line.strip())
    if m:
        return int(m.group(1)), m.group(2)
    return None, line.strip()

def extract_year(s: str) -> Optional[str]:
    m = re.search(r"\b(19|20)\d{2}\b", s)
    return m.group(0) if m else None

def extract_volume_after_year(s: str, year: Optional[str]) -> Optional[str]:
    if not year: return None
    idx = s.find(year)
    if idx == -1: return None
    tail = s[idx + len(year):]
    # commonly "... year, VOL, PAGE"
    m = re.search(r",\s*([0-9]+)\s*,", tail)
    if m: return m.group(1)
    m = re.search(r"[,;]\s*([0-9]+)\b", tail)
    if m: return m.group(1)
    return None

def extract_page_or_artnum(s: str) -> Optional[str]:
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) >= 2:
        token = parts[-1].rstrip(".").replace(" ", "").replace("\u00a0", "")
        token = token.replace("-", "")
        if re.search(r"[0-9]{3,}", token):
            return token
    return None

def extract_journal(s: str, year: Optional[str]) -> Optional[str]:
    if not year: return None
    pos = s.find(year)
    if pos == -1: return None
    left = s[:pos].rstrip()
    cpos = left.rfind(",")
    if cpos == -1: return None
    j = left[cpos+1:].strip().rstrip(".")
    return j if j else None

def extract_author_lastnames(s: str, max_authors: int = 5) -> List[str]:
    year = extract_year(s) or ""
    j = extract_journal(s, year) or ""
    stop = s.find(j) if j else (s.find(year) if year else len(s))
    author_segment = s[:max(0, stop)]
    parts = [p.strip() for p in author_segment.split(",") if p.strip()]
    lastnames = []
    for p in parts[:max_authors]:
        ws = re.findall(r"[A-Za-z][A-Za-z\-']+", p)
        if ws:
            lastnames.append(ws[-1])
    return lastnames[:max_authors]

def parse_refs_from_txt(txt_path: Path) -> List[dict]:
    joined = join_wrapped_refs(txt_path)
    recs = []
    for i, ln in enumerate(joined, 1):
        idx, body = strip_bracket_index(ln)
        y = extract_year(body) or ""
        v = extract_volume_after_year(body, y) or ""
        p = extract_page_or_artnum(body) or ""
        j = extract_journal(body, y) or ""
        auths = extract_author_lastnames(body, max_authors=5)
        recs.append({
            "idx": idx or i,
            "raw_ref": ln,
            "authors": auths,
            "journal": j,
            "year": y,
            "volume": v,
            "page_or_article": p
        })
    # sort by idx in case the TXT is out of order
    recs.sort(key=lambda r: int(r["idx"]))
    return recs

# ------- Crossref querying -------
def crossref_query(journal: str, year: str, volume: str, page_or_art: str,
                   authors: List[str], mailto: str, rows: int = 7) -> List[dict]:
    params = {
        "rows": rows,
        "select": "DOI,title,container-title,issued,volume,page,author,article-number",
    }
    if journal:
        params["query.container-title"] = expand_journal(journal)
    if authors:
        params["query.author"] = authors[0]

    filters = []
    if year and re.fullmatch(r"\d{4}", year):
        filters.append(f"from-pub-date:{year}-01-01")
        filters.append(f"until-pub-date:{year}-12-31")
    if volume:
        filters.append(f"volume:{volume}")
    if page_or_art:
        filters.append(f"page:{page_or_art}")
    if filters:
        params["filter"] = ",".join(filters)

    headers = {"User-Agent": f"txt-ref-resolver/1.0 (mailto:{mailto})"} if mailto else {}
    try:
        r = requests.get("https://api.crossref.org/works", params=params, headers=headers, timeout=20)
        r.raise_for_status()
        items = r.json().get("message", {}).get("items", []) or []
        if items:
            return items
    except Exception:
        pass

    # Fallback: bibliographic string
    biblio = ", ".join([x for x in [", ".join(authors[:3]) if authors else "", journal, year, volume, page_or_art] if x])
    params2 = {
        "rows": rows,
        "select": "DOI,title,container-title,issued,volume,page,author,article-number",
        "query.bibliographic": biblio
    }
    if year and re.fullmatch(r"\d{4}", year):
        params2["filter"] = f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31"
    try:
        r = requests.get("https://api.crossref.org/works", params=params2, headers=headers, timeout=20)
        r.raise_for_status()
        return r.json().get("message", {}).get("items", []) or []
    except Exception:
        return []

# ------- scoring -------
def score_candidate(item: dict, want: dict) -> int:
    score = 0
    # Year match
    cy = get_year_from_issued(item)
    if want["year"] and cy == want["year"]:
        score += 15
    # Journal/container match (abbrev/full)
    cj_list = item.get("container-title") or []
    cj = norm_punct(" ".join(cj_list[:1])) if cj_list else ""
    wj = norm_punct(expand_journal(want["journal"]))
    if cj and wj and (wj in cj or cj in wj):
        score += 20
    # Volume match
    cv = (item.get("volume") or "").strip()
    if cv and want["volume"] and cv == want["volume"]:
        score += 10
    # Page / article-number match
    wp = want["page_or_article"]
    if wp:
        ip = (item.get("page") or "")
        ia = (item.get("article-number") or "")
        if ip and re.search(rf"\b{re.escape(wp)}\b", ip.replace(" ", "")):
            score += 15
        if ia and only_digits(ia) == only_digits(wp):
            score += 15
    # Author last names (first up to 3)
    want_auths = [norm_punct(a) for a in (want["authors"] or []) if a]
    item_auths = [norm_punct(a.get("family","")) for a in (item.get("author") or []) if a.get("family")]
    matches = sum(1 for x in want_auths[:3] if x and x in item_auths)
    score += min(10, matches * 5)
    return score

# ------- resume helpers -------
def last_processed_idx(out_csv: Path) -> int:
    """
    Return the max idx found in an existing output CSV.
    If the file doesn't exist or has no rows, returns 0.
    """
    if not out_csv.exists():
        return 0
    max_idx = 0
    try:
        with out_csv.open("r", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                try:
                    max_idx = max(max_idx, int(row.get("idx", "0")))
                except Exception:
                    continue
    except Exception:
        pass
    return max_idx

# ------- main resolve loop (streaming append) -------
def resolve_and_write(
    refs: List[dict],
    out_csv: Path,
    mailto: str,
    min_score: int,
    rows: int,
    pause: float,
    start_after_idx: int = 0,
    limit: Optional[int] = None,
):
    # Decide write mode and whether to emit header
    write_header = not out_csv.exists() or start_after_idx == 0
    mode = "w" if write_header else "a"

    processed = 0
    total_remaining = sum(1 for r in refs if int(r["idx"]) > start_after_idx)
    if limit is not None:
        total_remaining = min(total_remaining, limit)

    with out_csv.open(mode, encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow([
                "idx","raw_ref","best_doi","best_title","best_container_title",
                "best_year","best_volume","best_page","best_article_number","score","decision"
            ])

        for rec in refs:
            idx = int(rec["idx"])
            if idx <= start_after_idx:
                continue
            if limit is not None and processed >= limit:
                break

            want = {
                "authors": rec["authors"],
                "journal": rec["journal"],
                "year": rec["year"],
                "volume": rec["volume"],
                "page_or_article": rec["page_or_article"],
            }

            items = crossref_query(rec["journal"], rec["year"], rec["volume"], rec["page_or_article"], rec["authors"], mailto, rows=rows)
            best, best_score = None, -1
            for it in items:
                sc = score_candidate(it, want)
                if sc > best_score:
                    best, best_score = it, sc

            doi = title = cont = byear = bvol = bpage = bart = ""
            decision = "no_match"
            if best:
                doi = (best.get("DOI") or "").lower().strip()
                tl = best.get("title") or []
                title = tl[0].strip() if tl else ""
                cl = best.get("container-title") or []
                cont = cl[0].strip() if cl else ""
                byear = get_year_from_issued(best)
                bvol = (best.get("volume") or "").strip()
                bpage = (best.get("page") or "").strip()
                bart  = (best.get("article-number") or "").strip()
                decision = "accepted" if best_score >= min_score else "low_confidence"

            # Write row immediately (so an interrupt still keeps progress)
            w.writerow([idx, rec["raw_ref"], doi, title, cont, byear, bvol, bpage, bart, best_score if best else "", decision])
            processed += 1

            if processed % 10 == 0 or processed == total_remaining:
                log(f"[{processed}/{total_remaining}] idx={idx} score={best_score} decision={decision} → {doi}")

            time.sleep(pause)

# ------- CLI -------
def main():
    ap = argparse.ArgumentParser(description="Resolve DOIs from a TXT list of title-less references via Crossref (simple resume).")
    ap.add_argument("--txt", required=True, help="TXT file: each ref starts with [n]; lines may wrap")
    ap.add_argument("--out", default="resolved_refs.csv", help="Output CSV")
    ap.add_argument("--mailto", default="", help="Email for Crossref User-Agent (recommended)")
    ap.add_argument("--min-score", type=int, default=35, help="Min score to accept a match (raise to be stricter)")
    ap.add_argument("--rows", type=int, default=7, help="Crossref candidates to fetch per ref")
    ap.add_argument("--pause", type=float, default=0.25, help="Seconds to sleep between Crossref requests")
    ap.add_argument("--limit", type=int, default=None, help="Process only first N refs from the resume point")
    ap.add_argument("--resume", action="store_true", help="Read existing --out CSV and continue from the next idx")
    ap.add_argument("--start-idx", type=int, default=None, help="Override: start after this idx (ignores --resume)")
    args = ap.parse_args()

    txt_path = Path(args.txt).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()

    refs = parse_refs_from_txt(txt_path)
    log(f"Parsed {len(refs)} references from TXT (min idx={refs[0]['idx']} max idx={refs[-1]['idx']})")

    if args.start_idx is not None:
        start_after = int(args.start_idx)
        log(f"Starting after idx={start_after} (via --start-idx)")
    elif args.resume:
        start_after = last_processed_idx(out_path)
        log(f"Resuming after idx={start_after} (found in {out_path})")
    else:
        start_after = 0
        log("Fresh run: writing new CSV (header will be written)")

    resolve_and_write(
        refs=refs,
        out_csv=out_path,
        mailto=args.mailto,
        min_score=args.min_score,
        rows=args.rows,
        pause=args.pause,
        start_after_idx=start_after,
        limit=args.limit,
    )

    log(f"Done. Wrote/updated: {out_path}")

if __name__ == "__main__":
    main()
