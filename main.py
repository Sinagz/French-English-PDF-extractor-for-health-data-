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

# --- NEW: heading indentation heuristics ---
HEADING_LVL1_MAX_X0 = 190.0  # subheading indent usually still fairly left
HEADING_MIN_CHARS = 4


def is_fee_amount(word: dict) -> bool:
    """Check if a word is a dollar amount (e.g. '62,85', '1 490,30')."""
    return word["x0"] > AMOUNT_X0 and bool(re.match(r"^\d[\d\s]*,\d{2}$", word["text"]))


def is_non_description(word: dict) -> bool:
    """Check if a word should be excluded from descriptions."""
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
    """Merge words that were split by PDF artifacts (e.g. 'E' + 'valuation')."""
    if len(words) < 2:
        return words
    merged = [words[0]]
    for w in words[1:]:
        prev = merged[-1]
        gap = w["x0"] - prev["x1"]
        same_line = abs(w["top"] - prev["top"]) <= LINE_Y_TOLERANCE
        if same_line and gap < MERGE_GAP and len(prev["text"]) == 1:
            merged[-1] = {**prev, "text": prev["text"] + w["text"], "x1": w["x1"]}
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


# --- NEW: helpers to detect headings and build prefix ---
_HEADING_STOPWORDS = {
    "AVIS",
    "NOTE",
    "TABLEAU",
    "TABLEAUX",
    "RÉF.",
    "RÉF.:",
    "RÉF",
}


def line_has_amount(line_words: list[dict]) -> bool:
    return any(is_fee_amount(w) for w in line_words)


def is_code_line(line_words: list[dict]) -> bool:
    if not line_words:
        return False
    first = line_words[0]
    return bool(re.match(r"^\d{5}$", first["text"])) and first["x0"] < LEFT_MARGIN_X0


def normalize_line_text(words: list[dict]) -> str:
    txt = " ".join(extract_desc_words(words)).strip()
    # normalize whitespace and dangling punctuation spacing a bit
    txt = re.sub(r"\s+", " ", txt)
    return txt


def is_heading_line(line_words: list[dict]) -> bool:
    """
    Heuristic:
    - not a code line
    - no fee amount on the line
    - mostly text (not just symbols/dashes)
    - near left side (x0 not too far right)
    """
    if not line_words:
        return False
    if is_code_line(line_words):
        return False
    if line_has_amount(line_words):
        return False

    first = line_words[0]
    if first["x0"] > HEADING_LVL1_MAX_X0:
        return False

    text = normalize_line_text(line_words)
    if len(text) < HEADING_MIN_CHARS:
        return False

    # ignore dashed separators
    if re.fullmatch(r"-{3,}", text):
        return False

    # ignore pure numbers / page artifacts
    if re.fullmatch(r"\d{1,4}", text):
        return False

    # ignore AVIS/NOTE blocks (they often start near left)
    first_word = line_words[0]["text"].strip().upper().rstrip(":")
    if first_word in _HEADING_STOPWORDS:
        return False

    # require at least one letter
    if not re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", text):
        return False

    return True


def heading_level(line_words: list[dict]) -> int:
    """Return 0 for left-margin heading, 1 for indented subheading."""
    return 0 if line_words[0]["x0"] < LEFT_MARGIN_X0 else 1


def build_heading_prefix(lvl0: str | None, lvl1: str | None) -> str:
    parts = [p for p in [lvl0, lvl1] if p]
    return " — ".join(parts).strip()


def prefix_if_missing(prefix: str, desc: str) -> str:
    if not prefix:
        return desc
    if not desc:
        return prefix
    # avoid double-prefixing
    if desc.startswith(prefix):
        return desc
    return f"{prefix} — {desc}"


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

    # --- NEW: current heading context (2-level) ---
    current_h0: str | None = None
    current_h1: str | None = None

    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        for page_idx, page in enumerate(pdf.pages):
            page_num = page_idx + 1
            if page_num < START_PAGE:
                continue
            if page_num % 50 == 0:
                print(f"  page {page_num}/{total_pages}...")

            # NOTE: keep default extract_words; no new deps
            words = merge_split_words(page.extract_words())
            lines = group_words_into_lines(words)

            for line_words in lines:
                if not line_words:
                    continue

                first = line_words[0]
                first_text = first["text"]

                # Skip ---- placeholder lines
                if re.match(r"^-{3,}$", first_text):
                    continue

                # --- NEW: update heading context when we are NOT in the middle of capturing a code description ---
                # If we're in a code and haven't hit amount yet, treat lines as description continuation, not headings.
                if (current_code is None or code_has_amount) and is_heading_line(line_words):
                    htxt = normalize_line_text(line_words)
                    lvl = heading_level(line_words)
                    if lvl == 0:
                        current_h0 = htxt
                        current_h1 = None  # reset subheading when top heading changes
                    else:
                        current_h1 = htxt
                    continue

                # New FSC code at left margin?
                if is_code_line(line_words):
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
                    line_desc = " ".join(desc_words).strip()

                    # --- NEW: prefix with active heading/subheading ---
                    prefix = build_heading_prefix(current_h0, current_h1)
                    line_desc = prefix_if_missing(prefix, line_desc)

                    current_desc_parts = [line_desc] if line_desc else ([prefix] if prefix else [])

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
    empty_codes = {code for code, desc in records if not desc}
    if empty_codes:
        print(f"Fixing {len(empty_codes)} codes with empty descriptions...")
        fixes: dict[str, str] = {}
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages[START_PAGE - 1 :]:
                words = merge_split_words(page.extract_words())
                lines = group_words_into_lines(words)

                # --- NEW: re-track headings within the page ---
                h0: str | None = None
                h1: str | None = None

                for i, line_words in enumerate(lines):
                    if not line_words:
                        continue

                    # update heading context as we go
                    if is_heading_line(line_words):
                        htxt = normalize_line_text(line_words)
                        lvl = heading_level(line_words)
                        if lvl == 0:
                            h0 = htxt
                            h1 = None
                        else:
                            h1 = htxt
                        continue

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
                            if not prev_line:
                                continue
                            pf = prev_line[0]
                            if pf["x0"] < LEFT_MARGIN_X0:
                                break  # hit another code or left-margin text
                            dw = extract_desc_words(prev_line)
                            if dw:
                                parts.insert(0, " ".join(dw))

                        # Look BELOW: walk forward from this line
                        for j in range(i + 1, min(i + 4, len(lines))):
                            next_line = lines[j]
                            if not next_line:
                                continue
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

                        # --- NEW: apply heading prefix even for these fixed cases ---
                        prefix = build_heading_prefix(h0, h1)
                        fixed = " ".join(parts).strip()
                        fixed = prefix_if_missing(prefix, fixed)

                        if fixed and code not in fixes:
                            fixes[code] = fixed

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