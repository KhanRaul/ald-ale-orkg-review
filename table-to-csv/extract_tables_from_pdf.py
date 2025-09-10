import argparse
import os
import io
import json
import re
import shutil
from pathlib import Path

import requests
import pandas as pd
from lxml import etree

# Optional backends; we'll check availability dynamically
try:
    import camelot  # type: ignore
    _HAVE_CAMEL0T = True
except Exception:
    _HAVE_CAMEL0T = False

try:
    import tabula  # type: ignore
    _HAVE_TABULA = True
except Exception:
    _HAVE_TABULA = False

try:
    import fitz  # PyMuPDF
    _HAVE_PYMUPDF = True
except Exception:
    _HAVE_PYMUPDF = False


TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}


def call_grobid(pdf_path: Path, grobid_url: str) -> str:
    """
    Send PDF to GROBID /processFulltextDocument and return TEI XML (str).
    """
    endpoint = f"{grobid_url.rstrip('/')}/api/processFulltextDocument"
    with open(pdf_path, "rb") as f:
        files = {"input": (pdf_path.name, f, "application/pdf")}
        data = {
            "consolidateHeader": "1",
            "consolidateCitations": "1",
            # ask for both figure and table coordinates
            "teiCoordinates": "figure,table,pb"
        }
        resp = requests.post(endpoint, files=files, data=data, timeout=180)
    resp.raise_for_status()
    return resp.text


def parse_facsimile_zones(tei_root):
    """
    Build mapping:
      - zone_id -> dict(page_number, bbox=(ulx,uly,lrx,lry))
    Also map surfaces to sequential page numbers.
    """
    zones = {}
    page_num = 0
    for surf in tei_root.xpath("//tei:facsimile/tei:surface", namespaces=TEI_NS):
        page_num += 1
        for z in surf.xpath("./tei:zone", namespaces=TEI_NS):
            zid = z.get("{http://www.w3.org/XML/1998/namespace}id")
            try:
                ulx = float(z.get("ulx"))
                uly = float(z.get("uly"))
                lrx = float(z.get("lrx"))
                lry = float(z.get("lry"))
                zones[zid] = {"page": page_num, "bbox": (ulx, uly, lrx, lry)}
            except (TypeError, ValueError):
                continue
    return zones


def extract_tables_from_tei(tei_xml: str):
    """
    Find table-like figures and tables in TEI and return metadata:
      [{'caption': str, 'label': str, 'facs': 'zone_id' or None, 'xml_id': str, 'has_tei_table': bool}, ...],
      plus the TEI root.
    - facs may be on <figure>, or inside <graphic>.
    - Some <figure type="table"> contain a nested <table> with row/cell content.
    """
    root = etree.fromstring(tei_xml.encode("utf-8"))
    tables = []

    # Handle <figure type="table"> … possibly with <graphic facs="#zone">
    for fig in root.xpath("//tei:figure[translate(@type,'TABLE','table')='table']", namespaces=TEI_NS):
        facs = fig.get("facs")
        if not facs:
            g = fig.find(".//tei:graphic", namespaces=TEI_NS)
            if g is not None:
                facs = g.get("facs")
        if facs and facs.startswith("#"):
            facs = facs[1:]

        head = fig.find("./tei:head", namespaces=TEI_NS)
        figdesc = fig.find("./tei:figDesc", namespaces=TEI_NS)
        label_el = fig.find("./tei:label", namespaces=TEI_NS)

        caption = ""
        if head is not None:
            caption = " ".join(" ".join(head.itertext()).split())
        elif figdesc is not None:
            caption = " ".join(" ".join(figdesc.itertext()).split())
        label = " ".join(" ".join(label_el.itertext()).split()) if label_el is not None else ""
        if not caption and label:
            caption = label

        xml_id = fig.get("{http://www.w3.org/XML/1998/namespace}id") or ""

        has_tei_table = fig.find(".//tei:table", namespaces=TEI_NS) is not None

        tables.append({
            "caption": caption,
            "label": label,
            "facs": facs,
            "xml_id": xml_id,
            "has_tei_table": has_tei_table
        })

    # Also handle raw <table> (rare) outside <figure>
    for t in root.xpath("//tei:table[not(ancestor::tei:figure)]", namespaces=TEI_NS):
        facs = t.get("facs")
        if not facs:
            g = t.find(".//tei:graphic", namespaces=TEI_NS)
            if g is not None:
                facs = g.get("facs")
        if facs and facs.startswith("#"):
            facs = facs[1:]

        head = t.find("./tei:head", namespaces=TEI_NS)
        label_el = t.find("./tei:label", namespaces=TEI_NS)
        caption = " ".join(" ".join(head.itertext()).split()) if head is not None else ""
        label = " ".join(" ".join(label_el.itertext()).split()) if label_el is not None else ""
        if not caption and label:
            caption = label

        xml_id = t.get("{http://www.w3.org/XML/1998/namespace}id") or ""

        tables.append({
            "caption": caption,
            "label": label,
            "facs": facs,
            "xml_id": xml_id,
            "has_tei_table": True  # it's a <table>
        })

    return tables, root


def tei_tables_to_csvs(tei_xml: str, out_dir: Path):
    """
    Export tables that are already parsed in TEI (<table><row><cell>…) to CSV.
    Returns list of record dicts for index.csv.
    NOTE: ignores colspan/rowspan.
    """
    root = etree.fromstring(tei_xml.encode("utf-8"))
    ns = TEI_NS
    records = []
    tables_dir = out_dir / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)

    # Iterate all figure tables that contain a TEI <table>
    for fig in root.xpath("//tei:figure[translate(@type,'TABLE','table')='table']", namespaces=ns):
        tei_tbl = fig.find(".//tei:table", namespaces=ns)
        if tei_tbl is None:
            continue

        head = fig.find("./tei:head", namespaces=ns)
        figdesc = fig.find("./tei:figDesc", namespaces=ns)
        label_el = fig.find("./tei:label", namespaces=ns)
        caption = ""
        if head is not None:
            caption = " ".join(" ".join(head.itertext()).split())
        elif figdesc is not None:
            caption = " ".join(" ".join(figdesc.itertext()).split())
        label = " ".join(" ".join(label_el.itertext()).split()) if label_el is not None else ""

        xml_id = fig.get("{http://www.w3.org/XML/1998/namespace}id") or None
        if not xml_id:
            # fallback id
            xml_id = f"table_{len(records)+1:04d}"

        # Collect rows
        rows = []
        for row in tei_tbl.findall("./tei:row", namespaces=ns):
            cells = []
            for cell in row.findall("./tei:cell", namespaces=ns):
                text = " ".join(" ".join(cell.itertext()).split())
                cells.append(text)
            rows.append(cells)

        # Write CSV
        csv_path = tables_dir / f"{xml_id}.csv"
        import csv
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            for r in rows:
                writer.writerow(r)

        records.append({
            "table_id": xml_id,
            "page": None,
            "bbox": None,
            "label": label,
            "caption": caption,
            "csv_path": str(csv_path),
            "status": "ok_from_tei"
        })

    # Also handle standalone <tei:table> not under <figure>
    for t in root.xpath("//tei:table[not(ancestor::tei:figure)]", namespaces=ns):
        xml_id = t.get("{http://www.w3.org/XML/1998/namespace}id") or f"table_{len(records)+1:04d}"
        head = t.find("./tei:head", namespaces=ns)
        label_el = t.find("./tei:label", namespaces=ns)
        caption = " ".join(" ".join(head.itertext()).split()) if head is not None else ""
        label = " ".join(" ".join(label_el.itertext()).split()) if label_el is not None else ""

        rows = []
        for row in t.findall("./tei:row", namespaces=ns):
            cells = []
            for cell in row.findall("./tei:cell", namespaces=ns):
                text = " ".join(" ".join(cell.itertext()).split())
                cells.append(text)
            rows.append(cells)

        csv_path = (out_dir / "tables" / f"{xml_id}.csv")
        import csv
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            for r in rows:
                writer.writerow(r)

        records.append({
            "table_id": xml_id,
            "page": None,
            "bbox": None,
            "label": label,
            "caption": caption,
            "csv_path": str(csv_path),
            "status": "ok_from_tei"
        })

    return records


def crop_pdf_region_to_temp(pdf_path: Path, page_num: int, bbox, out_dir: Path) -> Path:
    """
    Crop the region (ulx, uly, lrx, lry) on page_num (1-indexed) to a temp single-page PDF.
    Requires PyMuPDF.
    """
    if not _HAVE_PYMUPDF:
        raise RuntimeError("PyMuPDF (fitz) not installed; cannot crop regions.")

    ulx, uly, lrx, lry = bbox
    out_dir.mkdir(parents=True, exist_ok=True)
    out_pdf = out_dir / f"crop_p{page_num}_{int(ulx)}_{int(uly)}_{int(lrx)}_{int(lry)}.pdf"

    with fitz.open(pdf_path) as doc:
        if page_num < 1 or page_num > len(doc):
            raise ValueError(f"Invalid page {page_num} for {pdf_path}")
        page = doc[page_num - 1]
        rect = fitz.Rect(ulx, uly, lrx, lry)
        new_doc = fitz.open()
        new_page = new_doc.new_page(width=rect.width, height=rect.height)
        new_page.show_pdf_page(new_page.rect, doc, page_num - 1, clip=rect)
        new_doc.save(out_pdf)
        new_doc.close()
    return out_pdf


def table_pdf_to_csv(table_pdf: Path, out_csv: Path) -> bool:
    """
    Try Camelot (lattice->stream), then Tabula. Return True if something written.
    """
    # Try Camelot first if available
    if _HAVE_CAMEL0T:
        try:
            tables = camelot.read_pdf(str(table_pdf), flavor="lattice", pages="1")
            if tables and len(tables) > 0:
                tables[0].to_csv(str(out_csv))
                print(f"[camelot-lattice] {table_pdf.name} -> {out_csv.name}")
                return True
        except Exception as e:
            print(f"[camelot-lattice] failed: {e}")

        try:
            tables = camelot.read_pdf(str(table_pdf), flavor="stream", pages="1")
            if tables and len(tables) > 0:
                tables[0].to_csv(str(out_csv))
                print(f"[camelot-stream]  {table_pdf.name} -> {out_csv.name}")
                return True
        except Exception as e:
            print(f"[camelot-stream]  failed: {e}")

    # Try Tabula if available
    if _HAVE_TABULA:
        try:
            dfs = tabula.read_pdf(str(table_pdf), pages=1, multiple_tables=False, lattice=True)
            if dfs and len(dfs) > 0:
                dfs[0].to_csv(str(out_csv), index=False)
                print(f"[tabula-lattice]  {table_pdf.name} -> {out_csv.name}")
                return True
        except Exception as e:
            print(f"[tabula-lattice]  failed: {e}")
        try:
            dfs = tabula.read_pdf(str(table_pdf), pages=1, multiple_tables=False, stream=True)
            if dfs and len(dfs) > 0:
                dfs[0].to_csv(str(out_csv), index=False)
                print(f"[tabula-stream]   {table_pdf.name} -> {out_csv.name}")
                return True
        except Exception as e:
            print(f"[tabula-stream]   failed: {e}")

    return False


def main():
    ap = argparse.ArgumentParser(description="Extract tables to CSV using GROBID TEI (direct) + optional coords/Camelot/Tabula.")
    ap.add_argument("--pdf", required=True, help="Path to input PDF")
    ap.add_argument("--out", required=True, help="Output directory")
    ap.add_argument("--grobid", default="http://localhost:8070", help="GROBID base URL")
    args = ap.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tables_dir = out_dir / "tables"
    tables_dir.mkdir(exist_ok=True)

    # 1) GROBID
    print("[1/4] Calling GROBID…")
    tei_xml = call_grobid(pdf_path, args.grobid)
    tei_path = out_dir / "tei.xml"
    tei_path.write_text(tei_xml, encoding="utf-8")
    print(f"Saved TEI: {tei_path}")

    # 2) Parse TEI → table nodes + zone map
    print("[2/4] Parsing TEI…")
    table_meta, tei_root = extract_tables_from_tei(tei_xml)
    zones = parse_facsimile_zones(tei_root)
    print(f"Found {len(table_meta)} table-like nodes; zones detected: {len(zones)}")

    # 3) First, export TEI-embedded tables directly to CSV
    print("[3a/4] Exporting TEI tables directly to CSV…")
    records = tei_tables_to_csvs(tei_xml, out_dir)
    tei_ids_done = {r["table_id"] for r in records}
    print(f"Exported {len(records)} tables from TEI")

    # 3b) For figure tables that only have coords (no TEI <table>), crop -> extractor -> CSV
    print("[3b/4] Cropping coord-only tables and extracting…")
    temp_dir = out_dir / "_temp_crops"
    temp_dir.mkdir(exist_ok=True)

    for t in table_meta:
        # Skip if we already exported this table by TEI id
        if t.get("xml_id") and t["xml_id"] in tei_ids_done:
            continue

        facs = t.get("facs")
        if not facs or facs not in zones:
            # no coordinates → nothing to crop here
            continue

        page = zones[facs]["page"]
        bbox = zones[facs]["bbox"]

        # Crop
        try:
            crop_pdf = crop_pdf_region_to_temp(pdf_path, page, bbox, temp_dir)
        except Exception as e:
            records.append({
                "table_id": t.get("xml_id") or f"coord_table_{page}",
                "page": page,
                "bbox": bbox,
                "label": t.get("label", ""),
                "caption": t.get("caption", ""),
                "csv_path": None,
                "status": f"crop_failed: {e}"
            })
            continue

        # Extract to CSV
        out_csv = tables_dir / f"{(t.get('xml_id') or 'coord_table')}.csv"
        ok = table_pdf_to_csv(crop_pdf, out_csv)
        status = "ok" if ok else "extraction_failed"

        records.append({
            "table_id": t.get("xml_id") or f"coord_table_{page}",
            "page": page,
            "bbox": [float(x) for x in bbox],
            "label": t.get("label", ""),
            "caption": t.get("caption", ""),
            "csv_path": str(out_csv) if ok else None,
            "status": status
        })

    # 4) Save index.csv
    print("[4/4] Writing index.csv …")
    idx_path = out_dir / "index.csv"
    pd.DataFrame.from_records(records).to_csv(idx_path, index=False)

    # Cleanup temp crops
    try:
        shutil.rmtree(temp_dir)
    except Exception:
        pass

    print(f"\nDone. Index: {idx_path}")
    print(f"CSV folder: {tables_dir}\n")


if __name__ == "__main__":
    main()
