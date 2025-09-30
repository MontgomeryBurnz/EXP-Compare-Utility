from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Dict, List

import pandas as pd
import plotly.express as px
import streamlit as st
from xlsxwriter.utility import xl_col_to_name, xl_rowcol_to_cell


st.set_page_config(page_title="Program Planner & Gantt Exporter", layout="wide")

# ------------------------------------------------------------------
# Session state helpers
# ------------------------------------------------------------------

if "program_name" not in st.session_state:
    st.session_state.program_name = "New Program"

if "phases" not in st.session_state:
    st.session_state.phases: List[Dict] = []

if "tasks" not in st.session_state:
    st.session_state.tasks: List[Dict] = []


def normalize_filename(name: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in name.strip())
    return safe.strip("_") or "program_plan"


def as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    raise TypeError(f"Unsupported date value: {value!r}")


def build_excel_workbook(program_name: str, phases: List[Dict], tasks: List[Dict]) -> BytesIO:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        workbook = writer.book
        date_fmt = workbook.add_format({"num_format": "yyyy-mm-dd"})
        header_fmt = workbook.add_format({
            "bold": True,
            "bg_color": "#D9E1F2",
            "border": 1,
        })
        header_date_fmt = workbook.add_format({
            "bold": True,
            "bg_color": "#D9E1F2",
            "border": 1,
            "num_format": "yyyy-mm-dd",
        })
        gantt_fill_fmt = workbook.add_format({"bg_color": "#4472C4"})
        today_header_fmt = workbook.add_format({"bg_color": "#FFE699", "bold": True, "border": 1})
        today_column_fmt = workbook.add_format({"bg_color": "#FFF2CC"})

        if phases:
            df_phases = pd.DataFrame(phases)
            df_phases.rename(columns={"phase": "Phase", "start": "Start", "finish": "Finish"}, inplace=True)
            df_phases.sort_values("Start", inplace=True)
            df_phases.to_excel(writer, sheet_name="Phases", index=False)
            sheet = writer.sheets["Phases"]
            sheet.set_column(0, 0, 25)
            sheet.set_column(1, 2, 15)
            for row in range(1, len(df_phases) + 1):
                sheet.write_datetime(row, 1, as_datetime(df_phases.iloc[row - 1]["Start"]), date_fmt)
                sheet.write_datetime(row, 2, as_datetime(df_phases.iloc[row - 1]["Finish"]), date_fmt)

        if tasks:
            df_tasks = pd.DataFrame(tasks)
            df_tasks.rename(
                columns={"task": "Task", "phase": "Phase", "start": "Start", "finish": "Finish"},
                inplace=True,
            )
            df_tasks.sort_values(["Phase", "Start", "Finish", "Task"], inplace=True)
            df_tasks.to_excel(writer, sheet_name="Tasks", index=False)
            sheet = writer.sheets["Tasks"]
            sheet.set_column(0, 1, 25)
            sheet.set_column(2, 3, 15)
            for row in range(1, len(df_tasks) + 1):
                sheet.write_datetime(row, 2, as_datetime(df_tasks.iloc[row - 1]["Start"]), date_fmt)
                sheet.write_datetime(row, 3, as_datetime(df_tasks.iloc[row - 1]["Finish"]), date_fmt)

            # Build timeline matrix
            gantt_sheet = workbook.add_worksheet("Gantt Matrix")
            writer.sheets["Gantt Matrix"] = gantt_sheet

            gantt_sheet.freeze_panes(1, 4)
            gantt_sheet.set_column(0, 0, 30)
            gantt_sheet.set_column(1, 1, 18)
            gantt_sheet.set_column(2, 3, 15)

            # Header labels
            headers = ["Task", "Phase", "Start", "Finish"]
            for idx, title in enumerate(headers):
                gantt_sheet.write(0, idx, title, header_fmt)

            all_dates = [*[(p["start"], p["finish"]) for p in phases], *[(t["start"], t["finish"]) for t in tasks]]
            flattened_dates = [d for pair in all_dates for d in pair]
            min_date = min(flattened_dates)
            max_date = max(flattened_dates)
            date_range = pd.date_range(min_date, max_date, freq="D")

            date_start_col = len(headers)
            for offset, dt_value in enumerate(date_range):
                col = date_start_col + offset
                gantt_sheet.write_datetime(0, col, dt_value.to_pydatetime(), header_date_fmt)
                col_letter = xl_col_to_name(col)

                # Highlight header if matches today
                gantt_sheet.conditional_format(0, col, 0, col, {
                    "type": "formula",
                    "criteria": f'={col_letter}1=TODAY()',
                    "format": today_header_fmt,
                })

                # Highlight entire column if it is today
                gantt_sheet.conditional_format(1, col, len(tasks), col, {
                    "type": "formula",
                    "criteria": f'=${col_letter}$1=TODAY()',
                    "format": today_column_fmt,
                })

            # Write task rows and formulas
            for row_idx, task in enumerate(tasks, start=1):
                gantt_sheet.write(row_idx, 0, task["task"])
                gantt_sheet.write(row_idx, 1, task["phase"])
                gantt_sheet.write_datetime(row_idx, 2, as_datetime(task["start"]), date_fmt)
                gantt_sheet.write_datetime(row_idx, 3, as_datetime(task["finish"]), date_fmt)

                start_cell = xl_rowcol_to_cell(row_idx, 2, row_abs=True, col_abs=True)
                finish_cell = xl_rowcol_to_cell(row_idx, 3, row_abs=True, col_abs=True)

                for offset in range(len(date_range)):
                    col = date_start_col + offset
                    header_cell = xl_rowcol_to_cell(0, col, row_abs=True, col_abs=True)
                    formula = f'=IF(AND({header_cell}>={start_cell},{header_cell}<={finish_cell}),1,"")'
                    gantt_sheet.write_formula(row_idx, col, formula)

            if date_range.size:
                date_end_col = date_start_col + len(date_range) - 1
                gantt_sheet.conditional_format(1, date_start_col, len(tasks), date_end_col, {
                    "type": "cell",
                    "criteria": "==",
                    "value": 1,
                    "format": gantt_fill_fmt,
                })

            gantt_sheet.autofilter(0, 0, len(tasks), 3)
            gantt_sheet.write(0, 0, "Task", header_fmt)

    output.seek(0)
    return output


# ------------------------------------------------------------------
# Layout
# ------------------------------------------------------------------

st.title("Program Planner & Gantt Builder")
st.caption("Capture program phases, tasks, and export an Excel workbook with a ready-to-use Gantt chart matrix and today marker.")

with st.sidebar:
    st.header("Program Settings")
    st.text_input("Program name", key="program_name")
    if st.button("Clear phases & tasks"):
        st.session_state.phases = []
        st.session_state.tasks = []
        st.experimental_rerun()

st.markdown("### Define Your Program")
phase_col, task_col = st.columns(2, gap="large")

with phase_col:
    st.subheader("Add a Phase")
    with st.form("phase_form", clear_on_submit=True):
        phase_name = st.text_input("Phase name")
        start_col, end_col = st.columns(2)
        with start_col:
            phase_start = st.date_input("Start date", value=date.today())
        with end_col:
            phase_finish = st.date_input("Finish date", value=date.today() + timedelta(days=6))
        submitted = st.form_submit_button("Add phase", use_container_width=True)

        if submitted:
            errors = []
            if not phase_name.strip():
                errors.append("Please provide a phase name.")
            if phase_finish < phase_start:
                errors.append("Phase finish date cannot be before the start date.")

            if errors:
                for err in errors:
                    st.error(err)
            else:
                st.session_state.phases.append(
                    {
                        "phase": phase_name.strip(),
                        "start": phase_start,
                        "finish": phase_finish,
                    }
                )
                st.success(f"Added phase '{phase_name.strip()}'.")

with task_col:
    st.subheader("Add a Task")
    if not st.session_state.phases:
        st.info("Add a phase first to start defining tasks.")
    else:
        phase_options = [p["phase"] for p in st.session_state.phases]
        with st.form("task_form", clear_on_submit=True):
            task_name = st.text_input("Task name")
            phase_choice = st.selectbox("Phase", options=phase_options)
            selected_phase = next(p for p in st.session_state.phases if p["phase"] == phase_choice)

            task_start = st.date_input(
                "Task start",
                value=selected_phase["start"],
                min_value=selected_phase["start"],
                max_value=selected_phase["finish"],
            )
            task_finish = st.date_input(
                "Task finish",
                value=min(selected_phase["finish"], selected_phase["start"] + timedelta(days=6)),
                min_value=selected_phase["start"],
                max_value=selected_phase["finish"],
            )
            submit_task = st.form_submit_button("Add task", use_container_width=True)

            if submit_task:
                errors = []
                if not task_name.strip():
                    errors.append("Please provide a task name.")
                if task_finish < task_start:
                    errors.append("Task finish date cannot be before the start date.")

                if errors:
                    for err in errors:
                        st.error(err)
                else:
                    st.session_state.tasks.append(
                        {
                            "task": task_name.strip(),
                            "phase": selected_phase["phase"],
                            "start": task_start,
                            "finish": task_finish,
                        }
                    )
                    st.success(f"Added task '{task_name.strip()}' to phase '{selected_phase['phase']}'.")

st.markdown("---")

if st.session_state.phases:
    st.markdown("### Phases")
    phases_df = pd.DataFrame(st.session_state.phases)
    phases_df = phases_df.rename(columns={"phase": "Phase", "start": "Start", "finish": "Finish"})
    phases_df = phases_df.sort_values("Start")
    st.dataframe(phases_df, use_container_width=True)

if st.session_state.tasks:
    st.markdown("### Tasks")
    tasks_df = pd.DataFrame(st.session_state.tasks)
    tasks_df = tasks_df.rename(columns={"task": "Task", "phase": "Phase", "start": "Start", "finish": "Finish"})
    tasks_df = tasks_df.sort_values(["Phase", "Start", "Task"])
    st.dataframe(tasks_df, use_container_width=True)

    st.markdown("### Gantt Chart Preview")
    chart_df = tasks_df.copy()
    chart_df["Start"] = pd.to_datetime(chart_df["Start"])
    chart_df["Finish"] = pd.to_datetime(chart_df["Finish"])

    chart_df["Start Date"] = chart_df["Start"].dt.strftime("%Y-%m-%d")
    chart_df["Finish Date"] = chart_df["Finish"].dt.strftime("%Y-%m-%d")

    fig = px.timeline(
        chart_df,
        x_start="Start",
        x_end="Finish",
        y="Task",
        color="Phase",
        hover_data=["Phase", "Start Date", "Finish Date"],
    )
    fig.update_yaxes(autorange="reversed")
    today_ts = pd.Timestamp(date.today())
    fig.add_vline(
        x=today_ts,
        line_dash="dash",
        line_color="red",
        annotation_text="Today",
        annotation_position="top left",
    )
    fig.update_layout(
        title=st.session_state.program_name,
        hovermode="closest",
        margin=dict(l=0, r=0, t=60, b=20),
        height=max(360, 60 * len(chart_df)),
    )

    st.plotly_chart(fig, use_container_width=True)

    st.markdown("### Download Excel Workbook")
    excel_buffer = build_excel_workbook(st.session_state.program_name, st.session_state.phases, st.session_state.tasks)
    file_stub = normalize_filename(st.session_state.program_name)
    st.download_button(
        label="⬇️ Download program_gantt.xlsx",
        data=excel_buffer,
        file_name=f"{file_stub or 'program_plan'}_gantt.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
else:
    st.info("Add at least one task to see the Gantt chart and Excel export option.")
