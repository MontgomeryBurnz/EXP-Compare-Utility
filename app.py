import difflib
import html
import re
from io import BytesIO
from typing import Dict, List, Tuple

import streamlit as st

st.set_page_config(page_title="EXP Migrator: T-SQL ↔ Snowflake", layout="wide")

# ----------------------------------------------------------------------
# File + text utilities
# ----------------------------------------------------------------------

def read_file_to_text(file) -> str:
    if not file:
        return ""
    raw = file.getvalue()
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except Exception:
            return raw.decode("latin-1", errors="ignore")
    return str(raw)


def strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql


def normalize_whitespace(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def remove_identifier_brackets(sql: str) -> str:
    s = re.sub(r"\[([^\]]+)\]", r"\1", sql)
    return re.sub(r"`([^`]+)`", r"\1", s)


def apply_schema_mapping(sql: str, mapping: Dict[str, str]) -> str:
    s = sql
    for src, tgt in mapping.items():
        s = re.sub(re.escape(src), tgt, s, flags=re.IGNORECASE)
    return s


def normalize_for_compare(sql: str, *, strip_comments_opt: bool, casefold: bool,
                           collapse_ws: bool, drop_brackets: bool,
                           schema_map: Dict[str, str]) -> str:
    if not sql:
        return ""
    out = sql
    if strip_comments_opt:
        out = strip_sql_comments(out)
    if drop_brackets:
        out = remove_identifier_brackets(out)
    if schema_map:
        out = apply_schema_mapping(out, schema_map)
    if casefold:
        out = out.lower()
    if collapse_ws:
        out = normalize_whitespace(out)
    return out


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


def side_by_side_html(a_text: str, b_text: str) -> str:
    a_lines = a_text.splitlines()
    b_lines = b_text.splitlines()
    sm = difflib.SequenceMatcher(None, a_lines, b_lines)

    rows: List[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                a_line = html.escape(a_lines[i1 + k])
                b_line = html.escape(b_lines[j1 + k])
                rows.append(
                    "<tr>"
                    f"<td class='ok'><pre>{a_line}</pre></td>"
                    f"<td class='ok'><pre>{b_line}</pre></td>"
                    "</tr>"
                )
        else:
            maxlen = max(i2 - i1, j2 - j1)
            for offset in range(maxlen):
                a_line = a_lines[i1 + offset] if i1 + offset < i2 else ""
                b_line = b_lines[j1 + offset] if j1 + offset < j2 else ""
                a_html, b_html = inline_diff_html(a_line, b_line)
                rows.append(
                    "<tr>"
                    f"<td class='bad'><pre>{a_html}</pre></td>"
                    f"<td class='bad'><pre>{b_html}</pre></td>"
                    "</tr>"
                )

    rows_html = "\n".join(rows)
    return f"""
    <style>
      table.diff {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
      table.diff th {{ background: #111; color: #fff; padding: 6px; border: 1px solid #444; }}
      table.diff td {{ vertical-align: top; border: 1px solid #ddd; padding: 6px; }}
      table.diff td pre {{ margin: 0; white-space: pre-wrap; word-break: break-word; }}
      table.diff td.ok {{ background: #f5fff5; }}
      table.diff td.bad {{ background: #ffe5e5; }}
      .seg-repl {{ background: rgba(255,0,0,0.2); }}
      .seg-del {{ background: rgba(255,0,0,0.35); text-decoration: line-through; }}
      .seg-ins {{ background: rgba(0,128,0,0.15); font-weight: 600; }}
    </style>
    <table class="diff">
      <thead>
        <tr><th>T-SQL (normalized)</th><th>Snowflake SQL (normalized)</th></tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
    """


def explain_differences(tsql: str, snow: str) -> List[str]:
    if not tsql or not snow:
        return []
    explanations: List[str] = []
    checks = [
        (r"\bISNULL\s*\(", "T-SQL uses `ISNULL`; Snowflake prefers `COALESCE`."),
        (r"\bGETDATE\s*\(\s*\)", "`GETDATE()` exists only in T-SQL; map to `CURRENT_TIMESTAMP`."),
        (r"\bLEN\s*\(", "`LEN()` should become `LENGTH()` in Snowflake."),
        (r"\bNVARCHAR\b", "`NVARCHAR` types need to become `VARCHAR` (Snowflake stores UTF-8 by default)."),
        (r"\bWITH\s*\(\s*NOLOCK\s*\)", "`WITH (NOLOCK)` hints are unsupported in Snowflake; remove or redesign isolation."),
        (r"\bIDENTITY\s*\(", "`IDENTITY()` sequences must be replaced with `IDENTITY` columns or sequences in Snowflake."),
        (r"\bTOP\s+\d+", "`TOP N` should be rewritten as `LIMIT N` (optionally with `ORDER BY`)."),
        (r"\bMERGE\b", "Review `MERGE` syntax differences, especially `OUTPUT` clauses and semicolons."),
        (r"\bCROSS\s*APPLY\b", "`CROSS APPLY`/`OUTER APPLY` need rewrites using `LATERAL FLATTEN` or joins in Snowflake."),
    ]
    for pattern, msg in checks:
        if re.search(pattern, tsql, flags=re.IGNORECASE) and not re.search(pattern, snow, flags=re.IGNORECASE):
            explanations.append(msg)

    tsql_tokens = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", tsql.lower()))
    snow_tokens = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", snow.lower()))
    unique_tsql = sorted(t for t in (tsql_tokens - snow_tokens) if len(t) > 3)[:10]
    if unique_tsql:
        explanations.append(
            "Tokens present only in the T-SQL EXP (check they were migrated): " + ", ".join(unique_tsql)
        )
    unique_snow = sorted(t for t in (snow_tokens - tsql_tokens) if len(t) > 3)[:10]
    if unique_snow:
        explanations.append(
            "Tokens present only in the Snowflake EXP (verify intentional additions): " + ", ".join(unique_snow)
        )
    return explanations


# ----------------------------------------------------------------------
# T-SQL → Snowflake translator
# ----------------------------------------------------------------------

def t_sql_to_snowflake(tsql: str, schema_map: Dict[str, str]) -> Tuple[str, List[str]]:
    notes: List[str] = []
    s = tsql

    before = s
    s = strip_sql_comments(s)
    if s != before:
        notes.append("Removed T-SQL comments.")

    def _bracket_to_quoted(match: re.Match) -> str:
        return f'"{match.group(1)}"'

    before = s
    s = re.sub(r"\[([^\]]+)\]", _bracket_to_quoted, s)
    if s != before:
        notes.append("Converted `[identifier]` to double-quoted identifiers.")

    replacements = [
        (r"\bISNULL\s*\(", "COALESCE(", "Replaced ISNULL with COALESCE."),
        (r"\bGETDATE\s*\(\s*\)", "CURRENT_TIMESTAMP", "Replaced GETDATE() with CURRENT_TIMESTAMP."),
        (r"\bLEN\s*\(", "LENGTH(", "Replaced LEN() with LENGTH()."),
        (r"\bDATEDIFF\s*\(", "DATEDIFF(", "Check DATEDIFF order: Snowflake expects (unit, start, end)."),
    ]
    for pattern, repl, msg in replacements:
        new_s = re.sub(pattern, repl, s, flags=re.IGNORECASE)
        if new_s != s:
            notes.append(msg)
            s = new_s

    before = s
    s = re.sub(r"\bWITH\s*\(\s*NOLOCK\s*\)", "", s, flags=re.IGNORECASE)
    if s != before:
        notes.append("Removed WITH (NOLOCK) hints; Snowflake handles isolation differently.")

    def _convert_to_cast(match: re.Match) -> str:
        dtype = match.group(1).strip()
        expr = match.group(2).strip()
        return f"CAST({expr} AS {dtype})"

    before = s
    s = re.sub(r"\bCONVERT\s*\(\s*([A-Za-z0-9_]+)\s*,\s*(.*?)\)", _convert_to_cast, s, flags=re.IGNORECASE)
    if s != before:
        notes.append("Converted CONVERT(...) to CAST(... AS ...).")

    top_match = re.search(r"\bSELECT\s+TOP\s+(\d+)\s+", s, flags=re.IGNORECASE)
    if top_match:
        n = top_match.group(1)
        s = re.sub(r"(\bSELECT\s+)TOP\s+\d+\s+", r"\1", s, flags=re.IGNORECASE)
        if not re.search(r"\bLIMIT\s+\d+\b", s, flags=re.IGNORECASE):
            s = s.rstrip(" ;\n") + f"\nLIMIT {n};"
        notes.append(f"Translated TOP {n} to LIMIT {n}.")

    if schema_map:
        before = s
        s = apply_schema_mapping(s, schema_map)
        if s != before:
            notes.append("Applied schema/schema-prefix mapping.")

    s = re.sub(r";\s*;", ";", s)
    s = normalize_whitespace(s)
    return s, notes


def make_download(data: str, filename: str) -> BytesIO:
    buf = BytesIO()
    buf.write(data.encode("utf-8"))
    buf.seek(0)
    return buf


# ----------------------------------------------------------------------
# Sidebar options
# ----------------------------------------------------------------------

with st.sidebar:
    st.header("Normalization")
    strip_comments_opt = st.checkbox("Strip SQL comments", value=True)
    case_insensitive = st.checkbox("Case-insensitive compare", value=True)
    collapse_ws = st.checkbox("Normalize whitespace", value=True)
    drop_brackets = st.checkbox("Remove [] / ` ` around identifiers", value=True)

    st.markdown("---")
    st.header("Schema / Prefix Mapping")
    st.caption("One per line: `old.` -> `new.`")
    mapping_text = st.text_area(
        "Mappings",
        value="dbo. -> PUBLIC.\n[dbo]. -> PUBLIC.",
        height=100,
    )

    st.markdown("---")
    threshold = st.slider("Alert threshold (congruence %)", 50, 100, 95, 1)

schema_map: Dict[str, str] = {}
for line in mapping_text.splitlines():
    if "->" in line:
        left, right = line.split("->", 1)
        schema_map[left.strip()] = right.strip()


compare_tab, translate_tab = st.tabs([
    "Compare EXPs",
    "Translate EXP (T-SQL → Snowflake)",
])


# ----------------------------------------------------------------------
# Tab A: Compare
# ----------------------------------------------------------------------

with compare_tab:
    st.subheader("Compare two EXPs")
    st.caption("Upload or paste the T-SQL and Snowflake versions to see differences and explanations.")

    upload_col_a, upload_col_b = st.columns(2)
    with upload_col_a:
        tsql_file = st.file_uploader("T-SQL EXP file", type=["sql", "txt"], key="compare_tsql")
        tsql_text = st.text_area("Or paste T-SQL", height=220, key="compare_tsql_text")
    with upload_col_b:
        snow_file = st.file_uploader("Snowflake EXP file", type=["sql", "txt"], key="compare_snow")
        snow_text = st.text_area("Or paste SnowSQL", height=220, key="compare_snow_text")

    raw_tsql = read_file_to_text(tsql_file) if tsql_file else tsql_text
    raw_snow = read_file_to_text(snow_file) if snow_file else snow_text

    st.markdown("---")

    if raw_tsql and raw_snow:
        norm_tsql = normalize_for_compare(
            raw_tsql,
            strip_comments_opt=strip_comments_opt,
            casefold=case_insensitive,
            collapse_ws=collapse_ws,
            drop_brackets=drop_brackets,
            schema_map=schema_map,
        )
        norm_snow = normalize_for_compare(
            raw_snow,
            strip_comments_opt=strip_comments_opt,
            casefold=case_insensitive,
            collapse_ws=collapse_ws,
            drop_brackets=drop_brackets,
            schema_map=schema_map,
        )

        ratio = difflib.SequenceMatcher(None, norm_tsql, norm_snow).ratio()
        score = round(100.0 * ratio, 2)
        st.metric("Congruence Score", f"{score:.2f}%")
        status = "✅ Within threshold" if score >= threshold else "⚠️ Below threshold"
        st.write(f"**Status:** {status} (threshold = {threshold}%)")

        st.markdown("#### Difference Heatmap (normalized)")
        st.markdown(side_by_side_html(norm_tsql, norm_snow), unsafe_allow_html=True)

        st.markdown("#### Unified Diff Snapshot")
        diff_lines = difflib.unified_diff(
            norm_tsql.splitlines(),
            norm_snow.splitlines(),
            fromfile="TSQL (normalized)",
            tofile="Snowflake (normalized)",
            lineterm="",
        )
        diff_text = "\n".join(diff_lines)
        if diff_text.strip():
            st.code(diff_text, language="diff")
        else:
            st.success("No differences detected after normalization.")

        explanations = explain_differences(raw_tsql, raw_snow)
        if explanations:
            st.markdown("#### Why do they differ?")
            for item in explanations:
                st.write(f"- {item}")
        else:
            st.caption("No obvious dialect-specific differences detected.")

        with st.expander("Show original EXP files"):
            col_orig_a, col_orig_b = st.columns(2)
            with col_orig_a:
                st.markdown("**Original T-SQL**")
                st.code(raw_tsql, language="sql")
            with col_orig_b:
                st.markdown("**Original Snowflake SQL**")
                st.code(raw_snow, language="sql")

        st.markdown("#### Snowflake translation from the T-SQL EXP")
        translated_sql, translate_notes = t_sql_to_snowflake(raw_tsql, schema_map)
        st.code(translated_sql, language="sql")

        download_name = "translated_from_tsql.sql"
        if tsql_file and tsql_file.name:
            base_name = re.sub(r"[^A-Za-z0-9_-]+", "_", tsql_file.name.rsplit(".", 1)[0]) or "translated_from_tsql"
            download_name = f"{base_name}_snowflake.sql"
        st.download_button(
            label="⬇️ Download translated Snowflake EXP",
            data=make_download(translated_sql, download_name),
            file_name=download_name,
            mime="text/sql",
            key="compare_translation_download",
            use_container_width=True,
        )

        if translate_notes:
            st.caption("Translation notes:")
            for note in translate_notes:
                st.write(f"- {note}")
        else:
            st.caption("No automatic rewrites were needed; review manually for business logic differences.")
    else:
        st.info("Provide both T-SQL and Snowflake EXPs to compare.")


# ----------------------------------------------------------------------
# Tab B: Translate
# ----------------------------------------------------------------------

with translate_tab:
    st.subheader("Translate a T-SQL EXP to Snowflake")
    st.caption("Provide a T-SQL EXP. The tool applies common rewrites and highlights manual follow-ups.")

    translate_file = st.file_uploader("T-SQL EXP file", type=["sql", "txt"], key="translate_tsql")
    translate_text = st.text_area("Or paste T-SQL", height=280, key="translate_tsql_text")

    if st.button("Translate to Snowflake", use_container_width=True):
        source_sql = read_file_to_text(translate_file) if translate_file else translate_text
        if not source_sql.strip():
            st.warning("Please upload or paste a T-SQL EXP to translate.")
        else:
            translated_sql, notes = t_sql_to_snowflake(source_sql, schema_map)
            st.markdown("#### Translated Snowflake EXP")
            st.code(translated_sql, language="sql")

            download_name = "translated_snowflake.sql"
            st.download_button(
                label="⬇️ Download Snowflake EXP",
                data=make_download(translated_sql, download_name),
                file_name=download_name,
                mime="text/sql",
                use_container_width=True,
            )

            if notes:
                st.markdown("#### Translation Notes")
                for note in notes:
                    st.write(f"- {note}")
            else:
                st.caption("No dialect-specific replacements were applied. Review manually for business logic differences.")
