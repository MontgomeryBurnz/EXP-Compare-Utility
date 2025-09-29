import re
import difflib
import html
from typing import List, Dict, Tuple

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Side-by-Side Diff + T-SQL → Snowflake", page_icon="🧪", layout="wide")
st.title("🧪 Side-by-Side Compare (CSV / SQL / TXT) + Translate T-SQL → Snowflake")

# =========================================================
# I/O helpers
# =========================================================

def read_file_to_text(file) -> str:
    """Return file contents as text. If CSV, flatten rows to lines for comparison."""
    if not file:
        return ""
    name = file.name.lower()
    if name.endswith(".csv"):
        try:
            df = pd.read_csv(file)
            lines = []
            for row in df.itertuples(index=False):
                vals = ["" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v) for v in row]
                lines.append(",".join(vals))
            return "\n".join(lines)
        except Exception as e:
            return f"ERROR reading CSV: {e}"
    raw = file.read()
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("latin-1", errors="ignore")

# =========================================================
# Normalization utilities
# =========================================================

def strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql

def normalize_whitespace(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()

def remove_identifier_brackets(sql: str) -> str:
    return re.sub(r"\[([^\]]+)\]", r"\1", sql)

def apply_schema_mapping(sql: str, mapping: Dict[str, str]) -> str:
    s = sql
    for src, tgt in mapping.items():
        s = re.sub(re.escape(src), tgt, s, flags=re.IGNORECASE)
    return s

# =========================================================
# T-SQL → Snowflake translator
# =========================================================

def t_sql_to_snowflake(tsql: str, schema_map: Dict[str, str]) -> Tuple[str, List[str]]:
    notes: List[str] = []
    s = tsql

    before = s
    s = strip_sql_comments(s)
    if s != before:
        notes.append("Removed T-SQL comments.")

    def _bracket_to_quoted(m): return f"\"{m.group(1)}\""
    before = s
    s = re.sub(r"\[([^\]]+)\]", _bracket_to_quoted, s)
    if s != before:
        notes.append("Converted `[Identifier]` to double-quoted identifiers.")

    mappings = [
        (r"\bISNULL\s*\(", "COALESCE(", "Use COALESCE instead of ISNULL."),
        (r"\bGETDATE\s*\(\s*\)", "CURRENT_TIMESTAMP", "Use CURRENT_TIMESTAMP instead of GETDATE()."),
        (r"\bLEN\s*\(", "LENGTH(", "Use LENGTH instead of LEN."),
    ]
    for pat, rep, msg in mappings:
        new = re.sub(pat, rep, s, flags=re.IGNORECASE)
        if new != s:
            notes.append(msg)
            s = new

    before = s
    s = re.sub(r"\bWITH\s*\(\s*NOLOCK\s*\)", "", s, flags=re.IGNORECASE)
    if s != before:
        notes.append("Removed `WITH (NOLOCK)`.")

    before = s
    s = re.sub(r"\bCOLLATE\b\s+\w+", "", s, flags=re.IGNORECASE)
    if s != before:
        notes.append("Removed `COLLATE` clauses.")

    def _convert_to_cast(m):
        dtype, expr = m.group(1).strip(), m.group(2).strip()
        return f"CAST({expr} AS {dtype})"
    before = s
    s = re.sub(r"\bCONVERT\s*\(\s*([A-Za-z0-9_]+)\s*,\s*(.*?)\)", _convert_to_cast, s, flags=re.IGNORECASE)
    if s != before:
        notes.append("Converted CONVERT() to CAST().")

    top = re.search(r"\bSELECT\s+TOP\s+(\d+)\s+", s, flags=re.IGNORECASE)
    if top:
        n = top.group(1)
        notes.append(f"Translated TOP {n} to LIMIT {n}.")
        s = re.sub(r"(\bSELECT\s+)TOP\s+\d+\s+", r"\1", s, flags=re.IGNORECASE)
        if not re.search(r"\bLIMIT\s+\d+\b", s, flags=re.IGNORECASE):
            s = re.sub(r";\s*$", "", s)
            s = s.strip() + f"\nLIMIT {n};"

    if schema_map:
        before = s
        s = apply_schema_mapping(s, schema_map)
        if s != before:
            notes.append("Applied schema mapping.")

    s = normalize_whitespace(s)
    return s, notes

# =========================================================
# Inline diff helpers (token-level)
# =========================================================

def _tokenize_for_inline(s: str) -> List[str]:
    return re.split(r"(\W+)", s)

def inline_diff_html(a_line: str, b_line: str) -> Tuple[str, str]:
    a_toks = _tokenize_for_inline(a_line)
    b_toks = _tokenize_for_inline(b_line)
    sm = difflib.SequenceMatcher(None, a_toks, b_toks)

    a_out, b_out = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            a_out.append(html.escape("".join(a_toks[i1:i2])))
            b_out.append(html.escape("".join(b_toks[j1:j2])))
        elif tag == "replace":
            a_seg = html.escape("".join(a_toks[i1:i2]))
            b_seg = html.escape("".join(b_toks[j1:j2]))
            if a_seg:
                a_out.append(f"<span class='seg-repl'>{a_seg}</span>")
            if b_seg:
                b_out.append(f"<span class='seg-repl'>{b_seg}</span>")
        elif tag == "delete":
            a_seg = html.escape("".join(a_toks[i1:i2]))
            if a_seg:
                a_out.append(f"<span class='seg-del'>{a_seg}</span>")
        elif tag == "insert":
            b_seg = html.escape("".join(b_toks[j1:j2]))
            if b_seg:
                b_out.append(f"<span class='seg-ins'>{b_seg}</span>")
    return "".join(a_out), "".join(b_out)

# =========================================================
# Side-by-side diff (high-contrast + inline highlights)
# =========================================================

def side_by_side_html(a_text: str, b_text: str) -> str:
    a_lines = a_text.splitlines()
    b_lines = b_text.splitlines()
    sm = difflib.SequenceMatcher(None, a_lines, b_lines)

    rows = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                a_line = html.escape(a_lines[i1 + k])
                b_line = html.escape(b_lines[j1 + k])
                rows.append(
                    '<tr>'
                    f'<td class="ok"><pre>{a_line}</pre></td>'
                    f'<td class="ok"><pre>{b_line}</pre></td>'
                    '</tr>'
                )
        elif tag == "replace":
            maxlen = max(i2 - i1, j2 - j1)
            for k in range(maxlen):
                a_line = a_lines[i1 + k] if i1 + k < i2 else ""
                b_line = b_lines[j1 + k] if j1 + k < j2 else ""
                a_html, b_html = inline_diff_html(a_line, b_line)
                rows.append(
                    '<tr>'
                    f'<td class="bad"><pre>{a_html}</pre></td>'
                    f'<td class="bad"><pre>{b_html}</pre></td>'
                    '</tr>'
                )
        elif tag == "delete":
            for k in range(i2 - i1):
                a_line = a_lines[i1 + k]
                a_html, _ = inline_diff_html(a_line, "")
                rows.append(
                    '<tr>'
                    f'<td class="bad"><pre>{a_html}</pre></td>'
                    f'<td class="bad"><pre></pre></td>'
                    '</tr>'
                )
        elif tag == "insert":
            for k in range(j2 - j1):
                b_line = b_lines[j1 + k]
                _, b_html = inline_diff_html("", b_line)
                rows.append(
                    '<tr>'
                    f'<td class="bad"><pre></pre></td>'
                    f'<td class="bad"><pre>{b_html}</pre></td>'
                    '</tr>'
                )

    rows_html = "\n".join(rows)
    table = f"""
    <style>
      table.diff {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
      table.diff th {{ background: #ffffff; color: #000000; border: 1px solid #ddd; padding: 8px; }}
      table.diff td {{ vertical-align: top; border: 1px solid #ddd; padding: 8px; }}
      table.diff td pre {{
        margin: 0; white-space: pre-wrap; word-wrap: break-word;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      }}
      table.diff td.ok  {{ background: #c6f6c6; color: #000000; }}
      table.diff td.bad {{ background: #c62828; color: #ffffff; }}
      .seg-repl {{ background: rgba(255,255,255,0.25); border-bottom: 2px solid #ffffff; }}
      .seg-del  {{ background: rgba(255,0,0,0.35); text-decoration: line-through; }}
      .seg-ins  {{ background: rgba(255,255,255,0.25); font-weight: 700; }}
      .hdr {{ font-weight: 700; }}
      /* Wrapped code box for translated SQL */
      .codewrap pre {{
        background: #f7f7f7; color: #000; border: 1px solid #ddd; border-radius: 6px;
        padding: 10px; white-space: pre-wrap; word-break: break-word; margin: 0;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      }}
    </style>
    <table class="diff">
      <thead>
        <tr><th class="hdr">File A (normalized)</th><th class="hdr">File B (normalized)</th></tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
    """
    return table

# =========================================================
# UI
# =========================================================

left, right = st.columns(2)
with left:
    st.subheader("Upload File A (T-SQL / CSV / TXT)")
    file_a = st.file_uploader("Choose File A", type=["sql", "csv", "txt"], key="file_a")
with right:
    st.subheader("Upload File B (Snowflake SQL / CSV / TXT)")
    file_b = st.file_uploader("Choose File B", type=["sql", "csv", "txt"], key="file_b")

with st.sidebar:
    st.header("Compare Options")
    strip_comments_opt = st.checkbox("Strip SQL comments", value=True)
    case_insensitive = st.checkbox("Case-insensitive compare", value=True)
    collapse_ws = st.checkbox("Normalize whitespace", value=True)

    st.markdown("---")
    st.subheader("Schema / Prefix Mapping")
    mapping_text = st.text_area("Mappings", value="dbo. -> PUBLIC.\n[dbo]. -> PUBLIC.", height=80)
    threshold = st.slider("Pass threshold (%)", 50, 100, 95, 1)

# Parse mapping to dict
schema_map: Dict[str, str] = {}
for line in mapping_text.splitlines():
    if "->" in line:
        l, r = line.split("->", 1)
        schema_map[l.strip()] = r.strip()

# Read & normalize
text_a_raw = read_file_to_text(file_a)
text_b_raw = read_file_to_text(file_b)

def normalize_for_compare(s: str) -> str:
    if not s:
        return ""
    out = s
    if strip_comments_opt:
        out = strip_sql_comments(out)
    out = remove_identifier_brackets(out)            # treat [Col] as Col for compare
    if schema_map:
        out = apply_schema_mapping(out, schema_map)
    if case_insensitive:
        out = out.lower()
    if collapse_ws:
        out = normalize_whitespace(out)
    return out

text_a_norm = normalize_for_compare(text_a_raw)
text_b_norm = normalize_for_compare(text_b_raw)

# =========================================================
# Compare + Render
# =========================================================

if file_a and file_b:
    ratio = round(100.0 * difflib.SequenceMatcher(None, text_a_norm, text_b_norm).ratio(), 2)
    st.metric("Congruence Score", f"{ratio:.2f}%")
    st.write(f"**Status:** {'✅ PASS' if ratio >= threshold else '❌ FAIL'} (threshold = {threshold}%)")

    st.markdown("### Side-by-Side Diff (green = same, red = different; inline highlights show exact changes)")
    st.markdown(side_by_side_html(text_a_norm, text_b_norm), unsafe_allow_html=True)

    st.markdown("## T-SQL → Snowflake Translation (from File A)")
    if file_a.name.lower().endswith((".sql", ".txt")):
        translated_sql, notes = t_sql_to_snowflake(text_a_raw, schema_map)

        # ✅ Wrapped, fully visible, copy-friendly:
        st.markdown("**Translated Snowflake SQL (wrapped)**")
        st.markdown(f"<div class='codewrap'><pre>{html.escape(translated_sql)}</pre></div>", unsafe_allow_html=True)

        # Optional: quick download
        st.download_button(
            label="⬇️ Download translated_snowflake.sql",
            data=translated_sql.encode("utf-8"),
            file_name="translated_snowflake.sql",
            mime="text/sql",
        )

        if notes:
            st.markdown("**What changed**")
            for n in notes:
                st.write(f"- {n}")
        else:
            st.caption("No T-SQL specific constructs were found to translate.")
    else:
        st.info("File A is not SQL/TXT, so translation is skipped.")

else:
    st.info("⬆️ Upload **two files** to compare. Then see the Snowflake translation of File A below.")
