"""Extract FSC billing codes from RAMQ fee schedule PDF."""

import re
from pathlib import Path

import pandas as pd
import pdfplumber

YEAR = 2025
LEFT_MARGIN_X0 = 105.0  # Codes at left margin are x0 < this value
AMOUNT_X0 = 410.0  # Description text is always left of this
LINE_Y_TOLERANCE = 3.0  # Points tolerance for grouping words into lines
START_PAGE = 87  # First page with actual codes (1-indexed)


def is_fee_amount(word: dict) -> bool:
    """Check if a word is a dollar amount (e.g. '62,85', '1 490,30')."""
    return word["x0"] > AMOUNT_X0 and bool(
        re.match(r"^\d[\d\s]*,\d{2}$", word["text"])
    )


def is_non_description(word: dict) -> bool:
    """Check if a word should be excluded from descriptions.

    Covers dollar amounts, trailing PADT indicators, page numbers, and
    special markers like C.S. or 5*.
    """
    text = word["text"]
    x0 = word["x0"]
    if x0 > AMOUNT_X0 and re.match(r"^\d[\d\s]*,\d{2}$", text):
        return True
    if x0 > AMOUNT_X0 and re.match(r"^\d{1,3}$", text):
        return True
    if x0 > AMOUNT_X0 and text in ("C.S.", "C.S"):
        return True
    if x0 > AMOUNT_X0 and re.match(r"^\d\*$", text):
        return True
    return False


MERGE_GAP = 1.0  # Max gap (points) to merge split characters into one word


def merge_split_words(words: list[dict]) -> list[dict]:
    """Merge words that were split by PDF artifacts (e.g. 'E' + 'valuation').

    When a single non-ASCII character has zero gap to the next word, they
    belong together (accented first letter split by embedded space in PDF).
    """
    if len(words) < 2:
        return words
    merged = [words[0]]
    for w in words[1:]:
        prev = merged[-1]
        gap = w["x0"] - prev["x1"]
        same_line = abs(w["top"] - prev["top"]) <= LINE_Y_TOLERANCE
        if same_line and gap < MERGE_GAP and len(prev["text"]) == 1:
            merged[-1] = {
                **prev,
                "text": prev["text"] + w["text"],
                "x1": w["x1"],
            }
        else:
            merged.append(w)
    return merged


def group_words_into_lines(words: list[dict]) -> list[list[dict]]:
    """Group words by vertical position, returning lines sorted top-to-bottom."""
    if not words:
        return []
    sorted_words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines = []
    current_line = [sorted_words[0]]
    current_top = sorted_words[0]["top"]
    for word in sorted_words[1:]:
        if abs(word["top"] - current_top) <= LINE_Y_TOLERANCE:
            current_line.append(word)
        else:
            lines.append(sorted(current_line, key=lambda w: w["x0"]))
            current_line = [word]
            current_top = word["top"]
    lines.append(sorted(current_line, key=lambda w: w["x0"]))
    return lines


def extract_desc_words(words: list[dict]) -> list[str]:
    """Return description text from a list of words, filtering out amounts."""
    return [w["text"] for w in words if not is_non_description(w)]


def main():
    input_dir = Path("inputs")
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)

    pdf_path = next(input_dir.glob("*.pdf"))
    print(f"Processing: {pdf_path.name}")

    records: list[tuple[str, str]] = []
    current_code: str | None = None
    current_desc_parts: list[str] = []
    code_has_amount = False

    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        for page_idx, page in enumerate(pdf.pages):
            page_num = page_idx + 1
            if page_num < START_PAGE:
                continue
            if page_num % 50 == 0:
                print(f"  page {page_num}/{total_pages}...")

            words = merge_split_words(page.extract_words())
            lines = group_words_into_lines(words)

            for line_words in lines:
                first = line_words[0]
                first_text = first["text"]

                # Skip ---- placeholder lines
                if re.match(r"^-{3,}$", first_text):
                    continue

                # New FSC code at left margin?
                if (
                    re.match(r"^\d{5}$", first_text)
                    and first["x0"] < LEFT_MARGIN_X0
                ):
                    # Save the previous code
                    if current_code is not None:
                        desc = " ".join(current_desc_parts).strip()
                        records.append((current_code, desc))

                    current_code = first_text
                    code_has_amount = False

                    # Extract description from rest of line
                    rest = line_words[1:]
                    desc_words = []
                    for w in rest:
                        if is_fee_amount(w):
                            code_has_amount = True
                        if not is_non_description(w):
                            desc_words.append(w["text"])
                    current_desc_parts = (
                        [" ".join(desc_words)] if desc_words else []
                    )

                elif current_code is not None and not code_has_amount:
                    # Possible continuation line — only if fee amount not yet seen.
                    # Check for AVIS/NOTE at near-left margin to stop continuation.
                    if first_text in ("AVIS", "NOTE") and first["x0"] < 130:
                        desc = " ".join(current_desc_parts).strip()
                        records.append((current_code, desc))
                        current_code = None
                        current_desc_parts = []
                        code_has_amount = False
                        continue

                    desc_words = []
                    for w in line_words:
                        if is_fee_amount(w):
                            code_has_amount = True
                        if not is_non_description(w):
                            desc_words.append(w["text"])
                    if desc_words:
                        current_desc_parts.append(" ".join(desc_words))

        # Save the last code
        if current_code is not None:
            desc = " ".join(current_desc_parts).strip()
            records.append((current_code, desc))

    print(f"Raw records extracted: {len(records)}")

    # --- Second pass: fix codes with empty descriptions ---
    # Some codes have their description split ABOVE and BELOW the code line.
    # For these, gather indented text from the lines immediately surrounding
    # the code on the same page.
    empty_codes = {code for code, desc in records if not desc}
    if empty_codes:
        print(f"Fixing {len(empty_codes)} codes with empty descriptions...")
        fixes: dict[str, str] = {}
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages[START_PAGE - 1 :]:
                words = merge_split_words(page.extract_words())
                lines = group_words_into_lines(words)
                for i, line_words in enumerate(lines):
                    first = line_words[0]
                    if (
                        re.match(r"^\d{5}$", first["text"])
                        and first["x0"] < LEFT_MARGIN_X0
                        and first["text"] in empty_codes
                    ):
                        code = first["text"]
                        parts: list[str] = []
                        # Look ABOVE: walk backwards from this line
                        for j in range(i - 1, max(i - 4, -1), -1):
                            prev_line = lines[j]
                            pf = prev_line[0]
                            if pf["x0"] < LEFT_MARGIN_X0:
                                break  # hit another code or left-margin text
                            dw = extract_desc_words(prev_line)
                            if dw:
                                parts.insert(0, " ".join(dw))
                        # Look BELOW: walk forward from this line
                        for j in range(i + 1, min(i + 4, len(lines))):
                            next_line = lines[j]
                            nf = next_line[0]
                            if (
                                re.match(r"^\d{5}$", nf["text"])
                                and nf["x0"] < LEFT_MARGIN_X0
                            ):
                                break
                            if nf["x0"] < LEFT_MARGIN_X0:
                                break
                            dw = extract_desc_words(next_line)
                            if dw:
                                parts.append(" ".join(dw))
                        if parts and code not in fixes:
                            fixes[code] = " ".join(parts).strip()
        # Apply fixes to records
        records = [
            (code, fixes.get(code, desc) if not desc else desc)
            for code, desc in records
        ]
        print(f"  Fixed {len(fixes)} codes")

    df = pd.DataFrame(records, columns=["FSC_CD", "FSC_DES_FR"])
    df["FSC_DES_FR"] = df["FSC_DES_FR"].fillna("").astype(str)
    df = df.drop_duplicates(subset=["FSC_CD", "FSC_DES_FR"])
    df["FSC_DES_EN"] = df["FSC_DES_FR"]
    df["YEAR"] = YEAR
    df = df[["FSC_CD", "FSC_DES_FR", "FSC_DES_EN", "YEAR"]]
    df = df.sort_values("FSC_CD").reset_index(drop=True)

    csv_path = output_dir / "fsc_codes.csv"
    parquet_path = output_dir / "fsc_codes.parquet"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    df.to_parquet(parquet_path, index=False)

    print(f"\nExtracted {len(df)} unique FSC codes")
    print(f"Output: {csv_path}, {parquet_path}")
    print(f"\nFirst 10 codes:")
    print(df.head(10).to_string(index=False))
    print(f"\nLast 10 codes:")
    print(df.tail(10).to_string(index=False))


if __name__ == "__main__":
    main()
