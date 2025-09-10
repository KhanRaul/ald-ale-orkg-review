#!/usr/bin/env python3
"""
Expand reference lists and attach DOIs from a Crossref mapping.

Usage:
  python expand_refs_attach_doi.py \
    --data data.csv \
    --mapping resolved_refs.csv \
    --out data_expanded.csv \
    --refs-col "Refs." \
    --doi-col "doi"

Notes:
- If --doi-col is not provided, the script will use an existing column named
  'doi' or 'doi_list' if present; otherwise it creates a new 'doi' column.
- The data file delimiter is auto-detected (sep=None, engine='python').
- Only mappings with decision == 'accepted' are used; 'low_confidence' and
  'no_match' are ignored (rows not written).
"""

import argparse
import re
from pathlib import Path
from typing import List, Dict, Optional

import pandas as pd

EN_DASHES = ["–", "—", "‒", "−"]  # common unicode dashes


def _expand_token_to_numbers(tok: str) -> List[int]:
    """
    Expand a token like '28', '224-226', '224–226' into a list of ints.
    """
    tok = (tok or "").strip()
    for d in EN_DASHES:
        tok = tok.replace(d, "-")
    # Handle range a-b
    if "-" in tok:
        parts = [p.strip() for p in tok.split("-") if p.strip().isdigit()]
        if len(parts) == 2:
            a, b = int(parts[0]), int(parts[1])
            if a <= b:
                return list(range(a, b + 1))
            else:
                return list(range(b, a + 1))
    # Single number
    m = re.fullmatch(r"\d+", tok)
    if m:
        return [int(tok)]
    return []


def parse_refs_cell(val: Optional[str]) -> List[int]:
    """
    Parse a 'Refs.' cell into a list of integers.
    Handles:
      - "[28,224-226]"  → [28,224,225,226]
      - "208"           → [208]
      - "207,233"       → [207,233]
      - Spaces inside brackets: "[ 184 ]"
      - En-dash ranges.
    """
    s = ("" if val is None else str(val)).strip()
    if not s:
        return []

    # Normalize: collapse spaces inside bracketed groups
    # If there are bracket groups, prefer parsing inside them
    groups = re.findall(r"\[(.*?)\]", s)
    tokens: List[str] = []
    if groups:
        for g in groups:
            # split by comma/semicolon
            tokens.extend([t.strip() for t in re.split(r"[;,]", g) if t.strip()])
    else:
        # No brackets: split the whole string on commas/semicolons
        tokens = [t.strip() for t in re.split(r"[;,]", s) if t.strip()]

    nums: List[int] = []
    for tok in tokens:
        nums.extend(_expand_token_to_numbers(tok))

    # If nothing parsed yet and the string is just a number, capture it
    if not nums:
        lone_nums = re.findall(r"\d+", s)
        for n in lone_nums:
            try:
                nums.append(int(n))
            except Exception:
                pass

    # Dedupe preserving order
    seen = set()
    out = []
    for n in nums:
        if n not in seen:
            out.append(n)
            seen.add(n)
    return out


def load_ref_to_doi(mapping_csv: Path) -> Dict[int, str]:
    """
    Load mapping CSV with columns: idx, best_doi, decision (and others).
    Returns a dict: ref_number (int) -> best_doi, only for decision == 'accepted' and non-empty best_doi.
    """
    df = pd.read_csv(mapping_csv, sep=None, engine="python")
    # Normalize column names (trim)
    cols = {c: c.strip() for c in df.columns}
    df.rename(columns=cols, inplace=True)

    # Required columns
    for c in ["idx", "best_doi", "decision"]:
        if c not in df.columns:
            raise ValueError(f"Mapping file missing required column: '{c}'")

    df_ok = df[df["decision"].astype(str).str.strip().str.lower() == "accepted"].copy()
    df_ok = df_ok[df_ok["best_doi"].astype(str).str.strip() != ""]
    df_ok["idx"] = pd.to_numeric(df_ok["idx"], errors="coerce").astype("Int64")
    df_ok = df_ok.dropna(subset=["idx"])

    mapping = {int(row["idx"]): str(row["best_doi"]).strip() for _, row in df_ok.iterrows()}
    return mapping


def main():
    ap = argparse.ArgumentParser(description="Expand 'Refs.' and attach DOIs from a mapping; drop non-accepted matches.")
    ap.add_argument("--data", required=True, help="Input data CSV (table rows with 'Refs.' and a DOI column).")
    ap.add_argument("--mapping", required=True, help="Resolved refs CSV (with columns: idx, best_doi, decision).")
    ap.add_argument("--out", required=True, help="Output CSV path.")
    ap.add_argument("--refs-col", default="Refs.", help="Name of the references column in the data CSV (default: 'Refs.')")
    ap.add_argument("--doi-col", default="", help="Name of the DOI column in the data CSV. If empty, use 'doi' or 'doi_list' if present; otherwise create 'doi'.")
    args = ap.parse_args()

    data_path = Path(args.data).expanduser().resolve()
    mapping_path = Path(args.mapping).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()

    # Load mapping
    ref_to_doi = load_ref_to_doi(mapping_path)
    if not ref_to_doi:
        print("Warning: no accepted DOIs found in the mapping file; output may be empty.")

    # Load data (auto-detect separator)
    df = pd.read_csv(data_path, sep=None, engine="python", dtype=str).fillna("")
    # Figure out DOI column
    doi_col = args.doi_col.strip()
    if not doi_col:
        if "doi" in df.columns:
            doi_col = "doi"
        elif "doi_list" in df.columns:
            doi_col = "doi_list"
        else:
            doi_col = "doi"
            df[doi_col] = ""

    # Sanity: refs column present?
    refs_col = args.refs_col
    if refs_col not in df.columns:
        raise ValueError(f"Refs column '{refs_col}' not found. Available columns: {list(df.columns)}")

    # Build new rows
    new_rows = []
    kept = 0
    expanded = 0
    dropped = 0

    # Keep columns order
    columns = list(df.columns)
    if doi_col not in columns:
        columns.append(doi_col)

    for _, row in df.iterrows():
        row_dict = {c: row.get(c, "") for c in columns}
        has_doi = str(row_dict.get(doi_col, "")).strip() != ""
        refs_val = row_dict.get(refs_col, "")

        if has_doi:
            # Keep as-is
            new_rows.append(row_dict)
            kept += 1
            continue

        # No DOI → expand refs
        ref_nums = parse_refs_cell(refs_val)
        wrote_any = False
        for n in ref_nums:
            doi = ref_to_doi.get(n, "")
            if not doi:
                continue  # skip non-accepted or unknown refs
            rnew = dict(row_dict)
            rnew[doi_col] = doi
            # reflect exactly which ref we used
            rnew[refs_col] = f"[{n}]"
            new_rows.append(rnew)
            wrote_any = True
            expanded += 1

        if not wrote_any:
            # Drop this row (no accepted DOIs for its refs)
            dropped += 1

    out_df = pd.DataFrame(new_rows, columns=columns)
    out_df.to_csv(out_path, index=False)
    print(f"Done. Wrote: {out_path}")
    print(f"Kept with DOI: {kept} | Expanded rows created: {expanded} | Dropped (no accepted DOI): {dropped}")


if __name__ == "__main__":
    main()
