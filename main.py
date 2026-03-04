"""
Extract FSC billing codes from RAMQ fee schedule PDF (robust, local-context).

What this version does differently (to fix your issues):
- FSC codes are ALWAYS 5-char strings (leading zeros preserved).
- NO global "heading stack" that can get poisoned (e.g., "R = R =" or "ALLERGIE" leaking).
- For EACH code, it rebuilds context locally by scanning UP from the code line:
  - Collects nearby headings/subheadings/labels (with font + layout cues).
  - Collects the sticky note paragraph(s) (e.g., "Une biopsie ...") when they are directly tied to that block.
  - Stops at hard boundaries (new section title, big vertical gap, header/footer zone, or enough context gathered).
- Uses pdfplumber only + pandas + stdlib. Python <= 3.11 OK.

You can tune only a few knobs at top if needed:
- MAX_SCAN_UP_LINES
- GAP_STOP_PX
- HEADER_TOP_Y / FOOTER_BOTTOM_PAD
- TITLE_SIZE_BONUS
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd
import pdfplumber

YEAR = 2025
START_PAGE = 87  # 1-indexed

LEFT_MARGIN_X0 = 105.0
AMOUNT_X0 = 410.0
LINE_Y_TOLERANCE = 3.0
MERGE_GAP = 1.0

# --- Local-context scan knobs ---
MAX_SCAN_UP_LINES = 120        # how far up we look for headings/notes for each code
GAP_STOP_PX = 18.0             # stop upward scan if big vertical gap (new block)
MAX_PAGES_BACK = 1             # allow scanning into previous page at most 1 page

# --- Header/footer suppression (prevents legends like "R = R = ..." from being picked)
HEADER_TOP_Y = 70.0
FOOTER_BOTTOM_PAD = 60.0

# --- Font classification (relative to body font size per page)
TITLE_SIZE_BONUS = 1.2         # heading if max_size >= body_size + this


# ----------------------------
# Word / line utilities
# ----------------------------
def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def is_fee_amount_word(word: dict) -> bool:
    return word["x0"] > AMOUNT_X0 and bool(re.match(r"^\d[\d\s]*,\d{2}$", word["text"]))


def is_non_description_word(word: dict) -> bool:
    """Exclude from descriptions: amounts, page numbers, markers like C.S., and star footnotes."""
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


def merge_split_words(words: list[dict]) -> list[dict]:
    """Merge split characters like 'É' + 'valuation'."""
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
    if not words:
        return []
    sorted_words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = []
    cur = [sorted_words[0]]
    cur_top = sorted_words[0]["top"]
    for w in sorted_words[1:]:
        if abs(w["top"] - cur_top) <= LINE_Y_TOLERANCE:
            cur.append(w)
        else:
            lines.append(sorted(cur, key=lambda x: x["x0"]))
            cur = [w]
            cur_top = w["top"]
    lines.append(sorted(cur, key=lambda x: x["x0"]))
    return lines


def line_font_stats(line_words: list[dict]) -> tuple[float, float, bool]:
    """Return (avg_size, max_size, is_bold) for the line."""
    sizes: list[float] = []
    bold = False
    for w in line_words:
        sz = w.get("size", None)
        if sz is not None:
            try:
                sizes.append(float(sz))
            except Exception:
                pass
        fn = (w.get("fontname") or "")
        if "Bold" in fn or "bold" in fn:
            bold = True
    avg = sum(sizes) / len(sizes) if sizes else 0.0
    mx = max(sizes) if sizes else 0.0
    return avg, mx, bold


def is_code_line_words(line_words: list[dict]) -> bool:
    if not line_words:
        return False
    first = line_words[0]
    return first["x0"] < LEFT_MARGIN_X0 and bool(re.fullmatch(r"\d{4,5}", first["text"]))


def line_has_amount_words(line_words: list[dict]) -> bool:
    return any(is_fee_amount_word(w) for w in line_words)


def extract_line_text(line_words: list[dict]) -> str:
    return normalize_text(" ".join(w["text"] for w in line_words if not is_non_description_word(w)))


def extract_leaf_from_code_line(line_words: list[dict]) -> tuple[str, bool]:
    """Return (leaf_desc, saw_amount_on_line)."""
    saw_amount = False
    parts: list[str] = []
    for w in line_words[1:]:
        if is_fee_amount_word(w):
            saw_amount = True
        if not is_non_description_word(w):
            parts.append(w["text"])
    return normalize_text(" ".join(parts)), saw_amount


# ----------------------------
# Noise filtering
# ----------------------------
def is_header_footer_line(page_height: float, top: float) -> bool:
    if top < HEADER_TOP_Y:
        return True
    if top > page_height - FOOTER_BOTTOM_PAD:
        return True
    return False


def is_legend_noise(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if re.search(r"\bR\s*=\s*R\b", t):
        return True
    if t.count("=") >= 2:
        return True
    return False


# ----------------------------
# Line classification (local)
# ----------------------------
_STOP_PREFIXES = ("(", "réf", "ref", "p.g.", "pg", "règle", "regle")
_STOP_WORDS = {"AVIS", "NOTE", "TABLEAU", "TABLEAUX", "C.S.", "C.S"}


def looks_like_section_title(text: str, max_size: float, is_bold: bool, body_size: float) -> bool:
    """Major category like 'CARDIOLOGIE ET ANGIOLOGIE' or 'C — ...'."""
    t = (text or "").strip()
    if not t or is_legend_noise(t):
        return False
    if "—" in t and (is_bold or max_size >= body_size + TITLE_SIZE_BONUS):
        return True
    if re.fullmatch(r"[A-ZÀ-ÖØ-Þ0-9\s'’(),.-]+", t) and len(t) >= 8:
        if is_bold or max_size >= body_size + TITLE_SIZE_BONUS:
            return True
    return False


def looks_like_heading_or_label(
    text: str, max_size: float, is_bold: bool, body_size: float
) -> bool:
    """Heading/subheading/label candidate (NOT prose note)."""
    t = (text or "").strip()
    if not t:
        return False
    if is_legend_noise(t):
        return False
    if len(t) > 260:
        return False
    low = t.lower().strip()
    if low.startswith(_STOP_PREFIXES):
        return False
    if "réf" in low[:12] or "ref" in low[:12]:
        return False
    if re.fullmatch(r"-{3,}", t) or re.fullmatch(r"\d{1,4}", t):
        return False
    if not re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", t):
        return False

    # strong heading signals
    if is_bold or max_size >= body_size + TITLE_SIZE_BONUS:
        return True

    # short label lines like "Angiologie", "anus (P.A.D.T. 1.4)"
    if len(t) <= 80 and len(t.split()) <= 9:
        return True

    return False


def looks_like_prose_note(text: str, max_size: float, is_bold: bool, body_size: float) -> bool:
    """Sticky paragraph lines (like 'Une biopsie ...')."""
    t = (text or "").strip()
    if not t or is_legend_noise(t):
        return False
    if is_bold or max_size >= body_size + TITLE_SIZE_BONUS:
        return False
    if len(t) < 45:
        return False
    words = t.split()
    if len(words) < 7:
        return False
    if not re.search(r"[a-zà-öø-ÿ]", t):  # sentence-like lowercase
        return False
    return True


def prefix_if_missing(prefix: str, desc: str) -> str:
    if not prefix:
        return desc
    if not desc:
        return prefix
    if desc.startswith(prefix):
        return desc
    return f"{prefix} — {desc}"


# ----------------------------
# Build structured lines per page
# ----------------------------
def build_page_lines(page) -> tuple[list[dict[str, Any]], float]:
    """Return (lines, body_font_size). Each line is a dict with text/layout/font flags."""
    words = page.extract_words(extra_attrs=["size", "fontname"])
    words = merge_split_words(words)
    grouped = group_words_into_lines(words)

    # estimate body font size on this page
    sizes = [float(w["size"]) for w in words if w.get("size") and w["x0"] < AMOUNT_X0]
    body_size = float(pd.Series(sizes).median()) if sizes else 0.0

    lines: list[dict[str, Any]] = []
    for lw in grouped:
        if not lw:
            continue
        text = extract_line_text(lw)
        if not text:
            continue
        avg_sz, max_sz, bold = line_font_stats(lw)
        first = lw[0]
        top = float(first["top"])
        x0 = float(first["x0"])
        is_code = is_code_line_words(lw)
        has_amount = line_has_amount_words(lw)

        lines.append(
            {
                "text": text,
                "x0": x0,
                "top": top,
                "avg_size": avg_sz,
                "max_size": max_sz,
                "bold": bold,
                "is_code": is_code,
                "has_amount": has_amount,
                "raw_words": lw,
            }
        )
    return lines, body_size


# ----------------------------
# Local context extraction for a given code line index
# ----------------------------
def collect_leaf_and_end_index(lines: list[dict[str, Any]], code_idx: int) -> tuple[str, int]:
    """From a code line, collect leaf text + continuation lines until amount or next code."""
    code_line = lines[code_idx]
    lw = code_line["raw_words"]
    leaf, saw_amount = extract_leaf_from_code_line(lw)
    parts: list[str] = []
    if leaf:
        parts.append(leaf)

    end_idx = code_idx
    if saw_amount:
        return normalize_text(" ".join(parts)), end_idx

    # continuation lines
    for j in range(code_idx + 1, len(lines)):
        ln = lines[j]
        if ln["is_code"]:
            break
        # stop if it looks like a section title (prevents absorbing unrelated blocks)
        # (we classify again using text/font; caller has body_size; we’ll be conservative below by only stopping on true code)
        # Continue until amount appears on a continuation line
        cont_words = ln["raw_words"]
        cont_text = normalize_text(" ".join(w["text"] for w in cont_words if not is_non_description_word(w)))
        if cont_text:
            parts.append(cont_text)
        end_idx = j
        if ln["has_amount"]:
            break

    return normalize_text(" ".join(parts)), end_idx


def collect_local_context(
    pages_lines: list[list[dict[str, Any]]],
    pages_body: list[float],
    page_idx: int,
    code_idx: int,
    page_height: float,
) -> tuple[str, str]:
    """
    Walk upward from the code line and gather:
      - headings/labels (ordered top->bottom)
      - sticky prose note paragraph(s) near that block
    Returns (prefix_heading_chain, sticky_note_text)
    """
    headings_rev: list[tuple[int, float, str]] = []  # (level_hint, x0, text) collected bottom-up
    note_lines_rev: list[str] = []

    collected_any_heading = False
    scan_count = 0

    cur_page = page_idx
    cur_idx = code_idx - 1

    # to prevent very far contamination, stop after we got enough signals
    # (section title or 3 headings + optional note)
    def enough() -> bool:
        # if we have a section title (level_hint=0) OR 4 heading-ish lines, stop
        if any(lvl == 0 for (lvl, _, _) in headings_rev):
            return True
        if len(headings_rev) >= 5:
            return True
        return False

    while scan_count < MAX_SCAN_UP_LINES and cur_page >= max(0, page_idx - MAX_PAGES_BACK):
        lines = pages_lines[cur_page]
        body_size = pages_body[cur_page]

        while cur_idx >= 0 and scan_count < MAX_SCAN_UP_LINES:
            ln = lines[cur_idx]
            scan_count += 1

            # stop at header/footer zones for context gathering
            if is_header_footer_line(page_height, ln["top"]):
                cur_idx -= 1
                continue

            text = ln["text"]
            if not text:
                cur_idx -= 1
                continue

            # hard stop if legend noise
            if is_legend_noise(text):
                cur_idx -= 1
                continue

            # big vertical gap boundary
            # compare this line to the one below it (closer to code) when possible
            if cur_idx < len(lines) - 1:
                below = lines[cur_idx + 1]
                if abs(float(below["top"]) - float(ln["top"])) > GAP_STOP_PX and collected_any_heading:
                    # we already found headings; gap likely starts new block
                    return (
                        build_heading_chain(headings_rev),
                        build_note(note_lines_rev),
                    )

            # stop on another code line once we already have at least one heading
            if ln["is_code"] and collected_any_heading:
                return build_heading_chain(headings_rev), build_note(note_lines_rev)

            # ignore explicit AVIS/NOTE blocks as context
            fw = text.split()[0].upper().rstrip(":") if text.split() else ""
            if fw in _STOP_WORDS:
                cur_idx -= 1
                continue

            # classify this line
            is_section = looks_like_section_title(text, ln["max_size"], ln["bold"], body_size)
            is_note = looks_like_prose_note(text, ln["max_size"], ln["bold"], body_size)
            is_head = looks_like_heading_or_label(text, ln["max_size"], ln["bold"], body_size)

            if is_section:
                headings_rev.append((0, ln["x0"], text))
                collected_any_heading = True
                return build_heading_chain(headings_rev), build_note(note_lines_rev)

            # capture note lines but only if they are close to the code block:
            # we collect notes until we have a heading; once a heading is found, we stop collecting more notes above it.
            if is_note and not collected_any_heading:
                note_lines_rev.append(text)
                cur_idx -= 1
                continue

            # heading/label candidates
            if is_head:
                # level hint based on x0 ordering (smaller x0 = higher)
                lvl_hint = 1
                headings_rev.append((lvl_hint, ln["x0"], text))
                collected_any_heading = True

                if enough():
                    return build_heading_chain(headings_rev), build_note(note_lines_rev)

                cur_idx -= 1
                continue

            cur_idx -= 1

        # move to previous page (continue scan up)
        cur_page -= 1
        if cur_page >= 0:
            cur_idx = len(pages_lines[cur_page]) - 1

    return build_heading_chain(headings_rev), build_note(note_lines_rev)


def build_heading_chain(headings_rev: list[tuple[int, float, str]]) -> str:
    """
    Convert bottom-up collected headings into a clean top-down chain.
    - reverse order
    - merge wrapped headings with similar x0 (very common in this manual)
    - de-duplicate immediate repeats
    """
    if not headings_rev:
        return ""

    items = list(reversed(headings_rev))  # top-down now: [(lvl,x0,text), ...]

    merged: list[tuple[float, str]] = []
    for _, x0, text in items:
        t = normalize_text(text)
        if not t:
            continue

        if merged:
            prev_x0, prev_t = merged[-1]
            # merge wrapped heading lines if indentation is near-identical and neither looks like prose
            if abs(prev_x0 - x0) <= 4.0 and len(prev_t) < 180 and len(t) < 180:
                merged[-1] = (prev_x0, normalize_text(prev_t + " " + t))
                continue

        # remove immediate duplicates
        if merged and merged[-1][1] == t:
            continue
        merged.append((x0, t))

    # Also remove very common false positives (extra safety)
    cleaned: list[str] = []
    for x0, t in merged:
        low = t.lower()
        if low.startswith(_STOP_PREFIXES):
            continue
        if is_legend_noise(t):
            continue
        cleaned.append(t)

    return " — ".join(cleaned).strip()


def build_note(note_lines_rev: list[str]) -> str:
    if not note_lines_rev:
        return ""
    # join in correct order
    note = normalize_text(" ".join(reversed(note_lines_rev)))
    return note


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    input_dir = Path("inputs")
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)

    pdf_path = next(input_dir.glob("*.pdf"))
    print(f"Processing: {pdf_path.name}")

    records: list[tuple[str, str]] = []

    with pdfplumber.open(pdf_path) as pdf:
        pages_lines: list[list[dict[str, Any]]] = []
        pages_body: list[float] = []
        page_heights: list[float] = []

        # Build structured lines for all pages we will use
        for page_idx, page in enumerate(pdf.pages):
            pnum = page_idx + 1
            if pnum < START_PAGE:
                pages_lines.append([])
                pages_body.append(0.0)
                page_heights.append(float(page.height))
                continue
            lines, body = build_page_lines(page)
            pages_lines.append(lines)
            pages_body.append(body)
            page_heights.append(float(page.height))

        # Extract codes with local context
        for page_idx, lines in enumerate(pages_lines):
            pnum = page_idx + 1
            if pnum < START_PAGE or not lines:
                continue

            i = 0
            while i < len(lines):
                ln = lines[i]
                if not ln["is_code"]:
                    i += 1
                    continue

                code_raw = ln["raw_words"][0]["text"]
                code = code_raw.zfill(5)

                leaf, end_idx = collect_leaf_and_end_index(lines, i)

                heading_chain, note = collect_local_context(
                    pages_lines=pages_lines,
                    pages_body=pages_body,
                    page_idx=page_idx,
                    code_idx=i,
                    page_height=page_heights[page_idx],
                )

                prefix = heading_chain
                if note:
                    prefix = prefix_if_missing(prefix, note)
                full_desc = prefix_if_missing(prefix, leaf)

                records.append((code, full_desc))

                # jump to end of this code block
                i = end_idx + 1

    df = pd.DataFrame(records, columns=["FSC_CD", "FSC_DES_FR"])
    df["FSC_CD"] = df["FSC_CD"].astype(str).str.zfill(5)
    df["FSC_DES_FR"] = df["FSC_DES_FR"].fillna("").astype(str)

    df = df.drop_duplicates(subset=["FSC_CD", "FSC_DES_FR"])
    df["FSC_DES_EN"] = df["FSC_DES_FR"]
    df["YEAR"] = YEAR
    df = df[["FSC_CD", "FSC_DES_FR", "FSC_DES_EN", "YEAR"]].sort_values("FSC_CD").reset_index(drop=True)

    csv_path = output_dir / "fsc_codes.csv"
    parquet_path = output_dir / "fsc_codes.parquet"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    df.to_parquet(parquet_path, index=False)

    print(f"Extracted {len(df)} unique FSC codes")
    print(f"Output: {csv_path}, {parquet_path}")


if __name__ == "__main__":
    main()