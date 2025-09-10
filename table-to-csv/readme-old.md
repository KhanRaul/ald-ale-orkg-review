1. calls your local GROBID server for TEI,
2. parses TEI to find tables + captions + page/bounding boxes,
3. crops each table region into a temp PDF,
3. runs a table extractor (tries Camelot→Tabula fallback),
4. saves one CSV per table + an index.csv summarizing everything.

`pip install requests lxml pandas pymupdf camelot-py[cv] tabula-py

launch the grobid container 

`docker run -t --rm -p 8070:8070 grobid/grobid:0.8.2-full`

then run the program

`python table-to-csv\\extract_tables_from_pdf.py --pdf papers\\paper1\\rare-earth-materials.pdf --out papers\\paper1\\output\ --grobid "http://localhost:8070"`

then resolve the references

-------------------
if your references list looks like below

[1] T. Suntola, Sci Rep 1989, 4, 261.
[2] H. H. Sønsteby, A. Yanguas-Gil, J. W. Elam, J Vac Sci Technol A 2020,
38, 020804.
[3] N. E. Richey, C. De Paula, S. F. Bent, J. Chem. Phys. 2020, 152,
040902.
[4] Y. Zhao, L. Zhang, J. Liu, K. Adair, F. Zhao, Y. Sun, T. Wu, X. Bi, K.
Amine, J. Lu, X. Sun, Chem. Soc. Rev. 2021, 50, 3889.

use the following script

--------------------

Use it

Save your references to refs.txt exactly like your sample (one ref starts with [n], may wrap to next line).

Run:
```
C:\Users\dsouzaj\Desktop\Code\ald-ale-orkg-review>python table-to-csv\resolve_refs_from_txt_to_doi.py --txt papers\\paper1\\references.txt --out papers\\paper1\\references-resolved-doi.csv
```

alternatives

First run (fresh file):

```
python resolve_from_txt_crossref_resume.py ^
  --txt papers\paper1\references.txt ^
  --out papers\paper1\references-resolved-doi.csv ^
  --min-score 40 ^
  --rows 8 ^
  --pause 0.3 ^
  --limit 25
```

Resume later (automatically starts after the last idx already in the CSV):

```
python resolve_from_txt_crossref_resume.py ^
  --txt papers\paper1\references.txt ^
  --out papers\paper1\references-resolved-doi.csv ^
  --min-score 40 ^
  --rows 8 ^
  --pause 0.3 ^
  --resume
```

Force a specific restart point (e.g., continue after idx 120):
```
python resolve_from_txt_crossref_resume.py ^
  --txt papers\paper1\references.txt ^
  --out papers\paper1\references-resolved-doi.csv ^
  --min-score 40 ^
  --rows 8 ^
  --pause 0.3 ^
  --start-idx 120
```

Tips

Start with --limit 25 to spot-check.

Notes

The script appends when resuming; it does not rewrite earlier rows.

It sorts references by idx from the TXT, then processes those with idx > last_idx_from_csv.

If you previously ran with --limit 25, resume will continue at idx 26 (or whatever your last idx in the CSV is).

To reprocess earlier rows, either delete them from the CSV, run without --resume, or use --start-idx to pick your starting point.

-----

How the score is computed (per candidate)

We compute a conservative match score using only metadata that appears in your title-less refs:

Year match: +15
Candidate’s issued year equals the parsed year.

Journal (container-title) match: +20
After expanding common abbreviations (e.g., J. Chem. Phys. → The Journal of Chemical Physics), we give points when either string is a substring of the other (handles abbrev/full-name).

Volume match: +10
Exact string equality.

Page or article number match: up to +30

If the candidate’s page contains the parsed page token → +15

If the candidate’s article-number matches the parsed token (digits-only comparison) → +15

Author last names: up to +10
Compare the first up to 3 parsed last names with candidate authors’ family names → +5 per match (capped at +10).

So the typical max is 85 (if both page and article-number are present; most items only have one, so 70).
We then accept a match when score ≥ min_score (default 35). Everything else becomes low_confidence or no_match.
