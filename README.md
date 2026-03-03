# RAMQ PDF Fee Schedule Code Extractor

Extracts FSC (Fee Schedule Code) billing codes from Quebec RAMQ physician fee schedule PDFs.

## Output

- `outputs/fsc_codes.csv` — UTF-8 with BOM (Excel-compatible)
- `outputs/fsc_codes.parquet`

| Column | Description |
|---|---|
| `FSC_CD` | 5-digit billing code (string, preserves leading zeros) |
| `FSC_DES_FR` | French description |
| `FSC_DES_EN` | Copy of French description (document is French-only) |
| `YEAR` | Year from the document cover page |

## Setup

```bash
uv sync
```

## Usage

Place the RAMQ PDF in `inputs/`, then run:

```bash
uv run python main.py
```

## How it works

Uses `pdfplumber` word-level position data (`x0` coordinate) to identify 5-digit codes at the left margin of each page. Key steps:

1. Extract words with bounding box positions
2. Group words into lines by vertical position
3. Merge split accented characters (PDF artifact)
4. Identify codes at left margin (`x0 < 105`)
5. Capture multi-line descriptions, stripping trailing amounts and indicators
6. Second pass fixes ~10 codes where description text wraps above the code line
7. Deduplicate and output as CSV + Parquet
