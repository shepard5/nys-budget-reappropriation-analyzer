"""
NYS Reappropriation Automator — web UI.

Wraps the CLI pipeline so coworkers can upload the two budget PDFs +
SFS export, pick an agency (optional), and get back:
  - tracker.pdf
  - zip of insert PDFs
  - audit.html

Runs the pipeline per-session in an isolated temp directory. All pipeline
scripts honor REAPPROPS_ROOT env var (see src/config.py) so concurrent
sessions don't collide.

Usage (local):
    streamlit run app.py

Deploy to Streamlit Community Cloud: point it at this repo, entrypoint app.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st


REPO = Path(__file__).resolve().parent
SRC = REPO / "src"
# Subprocess Python: the active interpreter. Works locally (whatever venv
# launched streamlit) and on Streamlit Community Cloud (the container
# Python).
VENV_PY = sys.executable


# ----------------------------------------------------------------------
# Workspace lifecycle
# ----------------------------------------------------------------------

def _workspace() -> Path:
    """Per-session workspace. Persists across Streamlit reruns via
    st.session_state so the user can extract → filter → generate without
    re-uploading."""
    if "workspace" not in st.session_state:
        ws = Path(tempfile.mkdtemp(prefix="reapprops_"))
        (ws / "inputs").mkdir()
        (ws / "cache").mkdir()
        (ws / "outputs").mkdir()
        st.session_state.workspace = ws
    return st.session_state.workspace


def _reset_workspace():
    ws = st.session_state.get("workspace")
    if ws and ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    for key in list(st.session_state.keys()):
        del st.session_state[key]


def _run_script(script: str, workspace: Path, args: list[str] = None,
                log_box=None) -> int:
    """Run a pipeline script against this workspace. Streams stdout to
    log_box (a Streamlit container) if provided."""
    args = args or []
    env = {**os.environ, "REAPPROPS_ROOT": str(workspace),
           "PYTHONPATH": str(SRC)}
    cmd = [VENV_PY, str(SRC / script), *args]
    proc = subprocess.Popen(
        cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    lines: list[str] = []
    for line in proc.stdout:  # type: ignore
        lines.append(line.rstrip())
        if log_box is not None:
            log_box.code("\n".join(lines[-25:]))
    proc.wait()
    return proc.returncode


# ----------------------------------------------------------------------
# Pipeline stages
# ----------------------------------------------------------------------

def stage_upload_inputs(enacted_pdf, exec_pdf, sfs_xlsx, workspace: Path):
    """Save uploaded files into the workspace's inputs/."""
    (workspace / "inputs" / "enacted_25-26.pdf").write_bytes(enacted_pdf.getvalue())
    (workspace / "inputs" / "executive_26-27.pdf").write_bytes(exec_pdf.getvalue())
    (workspace / "inputs" / "sfs_export.xlsx").write_bytes(sfs_xlsx.getvalue())


def stage_upload_and_cache(workspace: Path, log_box=None) -> int:
    return _run_script("upload_and_cache.py", workspace, log_box=log_box)


def stage_extract(workspace: Path, log_box=None) -> int:
    rc1 = _run_script("extract.py", workspace, log_box=log_box)
    if rc1 != 0:
        return rc1
    return _run_script("extract_approps.py", workspace, log_box=log_box)


def stage_compare(workspace: Path, log_box=None) -> int:
    return _run_script("compare.py", workspace, log_box=log_box)


def stage_sfs(workspace: Path, log_box=None) -> int:
    return _run_script("sfs.py", workspace, log_box=log_box)


def stage_plan(workspace: Path, agency_filter: list[str] | None, log_box=None) -> int:
    """Build the insert plan. If agency_filter is set, only items matching
    those agencies are kept as eligible — the plan anchors still resolve
    against the full exec bill, but the eligible_map is scoped to the
    chosen agency. Preserves a pristine copy of dropped_with_sfs.csv so
    the user can re-run with a different agency selection."""
    drops_path = workspace / "outputs" / "dropped_with_sfs.csv"
    pristine = workspace / "outputs" / "dropped_with_sfs__all.csv"
    if not pristine.exists():
        shutil.copy(drops_path, pristine)
    if agency_filter:
        df = pd.read_csv(pristine)
        df = df[df["agency"].isin(agency_filter)]
        df.to_csv(drops_path, index=False)
    else:
        # Restore full view when user picks "(all agencies)"
        shutil.copy(pristine, drops_path)
    return _run_script("insert_plan.py", workspace, log_box=log_box)


def stage_generate_inserts(workspace: Path, log_box=None) -> int:
    inserts_dir = workspace / "outputs" / "inserts"
    if inserts_dir.exists():
        shutil.rmtree(inserts_dir)
    return _run_script("generate_inserts.py", workspace, log_box=log_box)


def stage_tracker(workspace: Path, log_box=None) -> int:
    return _run_script("generate_tracker.py", workspace, log_box=log_box)


def stage_audit(workspace: Path, log_box=None) -> int:
    return _run_script("audit.py", workspace, log_box=log_box)


# ----------------------------------------------------------------------
# Output packaging
# ----------------------------------------------------------------------

def package_inserts_zip(workspace: Path) -> Path:
    zip_path = workspace / "outputs" / "inserts.zip"
    inserts_dir = workspace / "outputs" / "inserts"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for pdf in sorted(inserts_dir.glob("*.pdf")):
            zf.write(pdf, arcname=pdf.name)
    return zip_path


# ----------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------

st.set_page_config(page_title="NYS Reappropriation Automator", layout="wide")
st.title("NYS Reappropriation Automator")
st.caption(
    "Upload the 25-26 enacted + 26-27 executive bills and the SFS "
    "Budgetary Overview export. The tool identifies reapprops dropped "
    "from the executive, joins SFS undisbursed balances, and produces "
    "insert PDFs + a tracker PDF with inline labels."
)

with st.sidebar:
    st.header("Inputs")
    enacted_pdf = st.file_uploader(
        "25-26 enacted bill (AFP-produced PDF)", type="pdf", key="enacted_pdf"
    )
    exec_pdf = st.file_uploader(
        "26-27 executive bill (AFP-produced PDF)", type="pdf", key="exec_pdf"
    )
    sfs_xlsx = st.file_uploader(
        "SFS Appropriation Budgetary Overview (.xlsx)", type="xlsx", key="sfs_xlsx"
    )
    st.divider()
    if st.button("Reset session"):
        _reset_workspace()
        st.rerun()


workspace = _workspace()
outputs = workspace / "outputs"

# ---- Stage 1: upload + extract --------------------------------------------

st.subheader("1. Upload and extract")
extract_done = (outputs / "dropped_with_sfs.csv").exists()

if not extract_done:
    col_a, col_b = st.columns([3, 1])
    with col_b:
        start = st.button(
            "Run extraction",
            type="primary",
            disabled=not (enacted_pdf and exec_pdf and sfs_xlsx),
        )
    with col_a:
        if not (enacted_pdf and exec_pdf and sfs_xlsx):
            st.info("Upload all three files to enable.")
        else:
            st.success(
                f"Ready: {enacted_pdf.name}  ·  {exec_pdf.name}  ·  {sfs_xlsx.name}"
            )
    if start:
        stage_upload_inputs(enacted_pdf, exec_pdf, sfs_xlsx, workspace)
        with st.status("Running extraction pipeline…", expanded=True) as status:
            st.write("📤 Uploading PDFs to LBDC editor…")
            log1 = st.empty()
            rc = stage_upload_and_cache(workspace, log1)
            if rc != 0:
                status.update(label="Upload failed", state="error")
                st.stop()
            st.write("🔍 Extracting reapprops from both bills…")
            log2 = st.empty()
            if stage_extract(workspace, log2) != 0:
                status.update(label="Extract failed", state="error"); st.stop()
            st.write("⚖️  Comparing enacted vs executive…")
            log3 = st.empty()
            if stage_compare(workspace, log3) != 0:
                status.update(label="Compare failed", state="error"); st.stop()
            st.write("💰 Joining SFS undisbursed balances…")
            log4 = st.empty()
            if stage_sfs(workspace, log4) != 0:
                status.update(label="SFS join failed", state="error"); st.stop()
            status.update(label="Extraction complete ✓", state="complete")
        st.rerun()
else:
    drops = pd.read_csv(outputs / "dropped_with_sfs.csv")
    eligible = drops[drops["insert_eligible"]]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Drops found", f"{len(drops):,}")
    c2.metric("SFS-matched", f"{drops['sfs_balance'].notna().sum():,}")
    c3.metric("Eligible", f"{len(eligible):,}")
    c4.metric("Total $ eligible", f"${eligible['sfs_rounded'].sum():,.0f}")


# ---- Stage 2: agency filter + generate ------------------------------------

if extract_done:
    st.divider()
    st.subheader("2. Pick agency and generate inserts")
    drops = pd.read_csv(outputs / "dropped_with_sfs.csv")
    eligible = drops[drops["insert_eligible"]]
    agency_counts = (
        eligible.groupby("agency")
        .agg(n=("sfs_rounded", "size"), dollars=("sfs_rounded", "sum"))
        .sort_values("dollars", ascending=False)
        .reset_index()
    )
    agency_counts["label"] = agency_counts.apply(
        lambda r: f"{r['agency']}  —  {r['n']} insert(s), ${r['dollars']:,.0f}",
        axis=1,
    )
    options = ["(all agencies)"] + agency_counts["label"].tolist()
    choice = st.selectbox("Agency", options, index=0)

    if choice == "(all agencies)":
        filter_list = None
        n_expected = len(eligible)
        minutes = max(1, round(n_expected * 2 / 60))
    else:
        chosen_agency = choice.split("  —  ")[0]
        filter_list = [chosen_agency]
        n_expected = int(
            agency_counts.loc[agency_counts["agency"] == chosen_agency, "n"].iloc[0]
        )
        minutes = max(1, round(n_expected * 2 / 60))

    st.caption(
        f"Will generate ~{n_expected} eligible item(s). "
        f"Estimated time ≈ {minutes} min (LBDC PDF generation is ~2s per insert)."
    )

    plan_exists = (outputs / "insert_plan.json").exists()
    if st.button("Generate inserts + tracker", type="primary"):
        with st.status("Generating inserts…", expanded=True) as status:
            st.write("📋 Planning inserts…")
            log5 = st.empty()
            if stage_plan(workspace, filter_list, log5) != 0:
                status.update(label="Plan failed", state="error"); st.stop()
            plan = json.loads((outputs / "insert_plan.json").read_text())
            st.write(f"✓ Plan: {len(plan)} inserts")
            st.write("📄 Generating insert PDFs…")
            log6 = st.empty()
            if stage_generate_inserts(workspace, log6) != 0:
                status.update(label="Insert generation failed", state="error"); st.stop()
            st.write("🧭 Building tracker PDF with inline labels…")
            log7 = st.empty()
            if stage_tracker(workspace, log7) != 0:
                status.update(label="Tracker failed", state="error"); st.stop()
            st.write("🔎 Running audit…")
            log8 = st.empty()
            stage_audit(workspace, log8)  # audit is non-fatal
            status.update(label="Generation complete ✓", state="complete")
        st.rerun()


# ---- Stage 3: downloads ---------------------------------------------------

if (outputs / "tracker.pdf").exists():
    st.divider()
    st.subheader("3. Downloads")
    inserts_dir = outputs / "inserts"
    n_inserts = len(list(inserts_dir.glob("*.pdf"))) if inserts_dir.exists() else 0
    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button(
            "📄 tracker.pdf",
            data=(outputs / "tracker.pdf").read_bytes(),
            file_name="tracker.pdf",
            mime="application/pdf",
        )
    with c2:
        if n_inserts:
            zip_path = package_inserts_zip(workspace)
            st.download_button(
                f"🗂️  inserts.zip  ({n_inserts} PDFs)",
                data=zip_path.read_bytes(),
                file_name="inserts.zip",
                mime="application/zip",
            )
    with c3:
        audit_path = outputs / "audit.html"
        if audit_path.exists():
            st.download_button(
                "🔎 audit.html",
                data=audit_path.read_bytes(),
                file_name="audit.html",
                mime="text/html",
            )

    # Tail of audit summary (anomaly counts)
    if (outputs / "insert_plan.json").exists():
        plan = json.loads((outputs / "insert_plan.json").read_text())
        total_dollars = sum(
            s["new_reapprop_amount"] for i in plan for s in i["survivors"]
        )
        st.caption(
            f"{len(plan)} inserts · "
            f"{sum(len(i['survivors']) for i in plan)} survivors · "
            f"${total_dollars:,.0f} total reinsertion amount"
        )
