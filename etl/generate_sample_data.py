"""Generate synthetic Power BI page-level telemetry for the demo.

This produces an aggregated daily fact: one row per
(workspace, report, page, view_date) with view counts and unique users —
the same shape the auto-generated "Report Usage Metrics" dataset emits.
"""
from __future__ import annotations

import csv
import math
import random
import sys
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

# Force UTF-8 stdout so em-dashes in log lines render correctly on Windows.
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

RNG = random.Random(20260528)


def _stable_hash(value: str) -> int:
    """Deterministic hash usable as an RNG seed.

    Python's built-in `hash(str)` is randomised per process (PYTHONHASHSEED),
    which would make the generated dataset non-deterministic across runs.
    CRC32 over the UTF-8 bytes gives us a fixed 32-bit integer.
    """
    return zlib.crc32(value.encode("utf-8")) & 0xFFFFFFFF

# ---------------------------------------------------------------------------
# Catalog: workspaces / reports / pages
# ---------------------------------------------------------------------------

@dataclass
class Report:
    report_id: str
    report_name: str
    workspace_id: str
    workspace_name: str
    capacity_name: str
    pages: list[str] = field(default_factory=list)
    base_traffic: float = 1.0  # multiplier on overall popularity
    persona_weights: dict[str, float] = field(default_factory=dict)


def _clinical_trial_pages(study: str, total: int) -> list[str]:
    """Realistic clinical-trial report page list — long, structured."""
    fixed = [
        f"{study} — Study Overview",
        f"{study} — Enrollment Funnel",
        f"{study} — Site Performance",
        f"{study} — Screen Fail Reasons",
        f"{study} — Protocol Deviations",
        f"{study} — Adverse Events Summary",
        f"{study} — Serious Adverse Events",
        f"{study} — SUSAR Tracking",
        f"{study} — Concomitant Medications",
        f"{study} — Lab Out-of-Range",
        f"{study} — Vital Signs Trends",
        f"{study} — ECG Findings",
        f"{study} — Pharmacokinetics",
        f"{study} — Dose Modifications",
        f"{study} — Discontinuations",
        f"{study} — Demographics",
        f"{study} — Baseline Characteristics",
        f"{study} — Patient Disposition",
        f"{study} — Cohort Comparison",
        f"{study} — Efficacy Primary Endpoint",
        f"{study} — Efficacy Secondary Endpoints",
        f"{study} — Tumor Response (RECIST)",
        f"{study} — Survival Curves",
        f"{study} — Biomarker Analysis",
        f"{study} — Genomic Signatures",
        f"{study} — PK/PD Modeling",
        f"{study} — Exposure-Response",
        f"{study} — Data Quality Dashboard",
        f"{study} — Query Aging",
        f"{study} — Monitoring Visits",
        f"{study} — IMP Accountability",
        f"{study} — Temperature Excursions",
        f"{study} — Vendor SLAs",
        f"{study} — Country Operations",
        f"{study} — Recruitment Forecast",
        f"{study} — Enrollment vs Plan",
        f"{study} — Budget Burn",
        f"{study} — Milestone Tracker",
        f"{study} — Risk Register",
        f"{study} — Communications Log",
        f"{study} — Regulatory Submissions",
        f"{study} — IRB/EC Approvals",
        f"{study} — ICF Versions",
        f"{study} — Translator Status",
        f"{study} — Lab Kit Inventory",
        f"{study} — Sample Shipments",
        f"{study} — Central Lab QC",
        f"{study} — Imaging Read Backlog",
        f"{study} — IRT Drug Resupply",
        f"{study} — eCRF Completion",
        f"{study} — Source Data Verification",
        f"{study} — Audit Findings",
        f"{study} — Training Compliance",
        f"{study} — Investigator Brochure Updates",
        f"{study} — Statistical Analysis Plan",
        f"{study} — Interim Analysis Tracker",
        f"{study} — DSMB Meeting Notes",
        f"{study} — Closeout Activities",
        f"{study} — CSR Section Status",
        f"{study} — Publication Tracker",
        f"{study} — Appendix: Glossary",
        f"{study} — Appendix: Data Dictionary",
        f"{study} — Appendix: Visit Schedule",
        f"{study} — Appendix: Reference Ranges",
    ]
    return fixed[:total]


CATALOG: list[Report] = [
    # ---- ClinicalOps workspace ------------------------------------------------
    Report(
        report_id="rep-clin-study101",
        report_name="Phase III STUDY-101 — Clinical Trial Tracking",
        workspace_id="ws-clinops",
        workspace_name="Clinical Operations",
        capacity_name="Capacity-P1",
        pages=_clinical_trial_pages("STUDY-101", 60),
        base_traffic=1.6,
        persona_weights={"ClinOps Mgr": 3.0, "Med Director": 1.8, "Biostatistician": 1.2,
                         "Safety": 1.4, "Data Mgr": 1.5, "Exec": 0.6, "Commercial": 0.1},
    ),
    Report(
        report_id="rep-clin-study202",
        report_name="Phase II STUDY-202 — Safety Review",
        workspace_id="ws-clinops",
        workspace_name="Clinical Operations",
        capacity_name="Capacity-P1",
        pages=_clinical_trial_pages("STUDY-202", 35),
        base_traffic=1.1,
        persona_weights={"Safety": 3.0, "Med Director": 2.0, "ClinOps Mgr": 1.0,
                         "Biostatistician": 0.8, "Data Mgr": 0.7},
    ),
    Report(
        report_id="rep-clin-study303",
        report_name="Phase I STUDY-303 — Dose Escalation",
        workspace_id="ws-clinops",
        workspace_name="Clinical Operations",
        capacity_name="Capacity-P1",
        pages=_clinical_trial_pages("STUDY-303", 22),
        base_traffic=0.7,
        persona_weights={"Med Director": 2.5, "Biostatistician": 2.0, "Safety": 1.4,
                         "ClinOps Mgr": 0.6},
    ),
    Report(
        report_id="rep-clin-portfolio",
        report_name="Clinical Portfolio — Executive Summary",
        workspace_id="ws-clinops",
        workspace_name="Clinical Operations",
        capacity_name="Capacity-P1",
        pages=["Portfolio Heatmap", "Milestones at Risk", "Enrollment Forecast",
               "Spend vs Budget", "Pipeline Map", "Site Geography",
               "Risks & Mitigations", "Next 90 Days"],
        base_traffic=1.4,
        persona_weights={"Exec": 3.5, "Med Director": 1.5, "ClinOps Mgr": 1.2,
                         "Commercial": 0.4},
    ),

    # ---- Medical Affairs workspace -------------------------------------------
    Report(
        report_id="rep-med-kol",
        report_name="KOL Engagement Tracker",
        workspace_id="ws-medaffairs",
        workspace_name="Medical Affairs",
        capacity_name="Capacity-P1",
        pages=["KOL Overview", "Engagement Heatmap", "Speaker Bureau Activity",
               "Publication Co-Authoring", "Advisory Board Topics",
               "MSL Activity by Region", "Tier 1 KOL Profiles",
               "Sentiment Analysis", "Conference Attendance"],
        base_traffic=0.9,
        persona_weights={"Med Director": 2.0, "MSL": 3.0, "Commercial": 1.2},
    ),
    Report(
        report_id="rep-med-pub",
        report_name="Publications & Evidence Generation",
        workspace_id="ws-medaffairs",
        workspace_name="Medical Affairs",
        capacity_name="Capacity-P1",
        pages=["Pipeline Publications", "Journal Acceptance Rates",
               "Time-to-Publication", "Co-Author Networks", "Citation Impact",
               "RWE Manuscripts", "Conference Abstracts Pending",
               "Plain Language Summaries"],
        base_traffic=0.7,
        persona_weights={"Med Director": 2.2, "MSL": 2.0, "Biostatistician": 1.0},
    ),
    Report(
        report_id="rep-med-msl",
        report_name="MSL Field Activity",
        workspace_id="ws-medaffairs",
        workspace_name="Medical Affairs",
        capacity_name="Capacity-P1",
        pages=["MSL Coverage Map", "Interaction Volume by Therapeutic Area",
               "Scientific Inquiries", "Insight Themes", "Training Status",
               "Territory Workload Balance"],
        base_traffic=1.0,
        persona_weights={"MSL": 3.5, "Med Director": 1.6, "Commercial": 0.6},
    ),

    # ---- RWE workspace --------------------------------------------------------
    Report(
        report_id="rep-rwe-onc",
        report_name="Real-World Evidence — Oncology Cohorts",
        workspace_id="ws-rwe",
        workspace_name="Real World Evidence",
        capacity_name="Capacity-P2",
        pages=["Cohort Builder", "Demographics", "Comorbidity Profile",
               "Line-of-Therapy Patterns", "Time-to-Next-Treatment",
               "Overall Survival (KM)", "Real-World Response", "Adverse Event Rates",
               "Healthcare Resource Utilization", "Cost of Care",
               "Site of Care Trends", "Geographic Disparities",
               "Insurance Coverage Mix", "SDoH Overlay", "Biomarker Prevalence",
               "Genomic Subtypes", "Treatment Sequencing", "Switching Patterns",
               "Discontinuation Reasons", "Subgroup Analyses",
               "Forest Plot Builder", "Sensitivity Analyses"],
        base_traffic=1.0,
        persona_weights={"HEOR Analyst": 3.0, "Biostatistician": 2.2,
                         "Med Director": 1.4, "Exec": 0.6},
    ),
    Report(
        report_id="rep-rwe-immuno",
        report_name="Real-World Evidence — Immunology",
        workspace_id="ws-rwe",
        workspace_name="Real World Evidence",
        capacity_name="Capacity-P2",
        pages=["Cohort Builder", "Disease Severity", "Biologic Switching",
               "Persistence Curves", "Flare Rates", "PRO Trajectories",
               "Comorbidity Burden", "Healthcare Costs", "Specialty Distribution",
               "Geographic Heatmap", "Subgroup Analyses", "Sensitivity Analyses"],
        base_traffic=0.6,
        persona_weights={"HEOR Analyst": 2.6, "Biostatistician": 1.8,
                         "Med Director": 1.0},
    ),
    Report(
        report_id="rep-rwe-claims",
        report_name="Claims Data Quality Monitor",
        workspace_id="ws-rwe",
        workspace_name="Real World Evidence",
        capacity_name="Capacity-P2",
        pages=["Freshness", "Coverage by Payer", "ICD-10 Drift",
               "NDC Mapping Issues", "Outlier Claims", "Backfill Backlog"],
        base_traffic=0.4,
        persona_weights={"Data Mgr": 3.0, "HEOR Analyst": 1.4},
    ),

    # ---- Commercial workspace -------------------------------------------------
    Report(
        report_id="rep-com-launch",
        report_name="Launch Performance — PRODUCT-A",
        workspace_id="ws-commercial",
        workspace_name="Commercial Analytics",
        capacity_name="Capacity-P1",
        pages=["Launch Scorecard", "Weekly TRx/NRx", "Payer Mix",
               "Prior Auth Funnel", "Specialty Pharmacy Pull-Through",
               "Field Force Effectiveness", "Speaker Programs ROI",
               "Sample Distribution", "Digital Engagement",
               "Patient Hub Enrollment", "Competitive Share",
               "Forecast vs Actual"],
        base_traffic=1.5,
        persona_weights={"Commercial": 3.2, "Exec": 1.8, "Field Rep": 2.0,
                         "MSL": 0.4},
    ),
    Report(
        report_id="rep-com-payer",
        report_name="Payer Access & Coverage",
        workspace_id="ws-commercial",
        workspace_name="Commercial Analytics",
        capacity_name="Capacity-P1",
        pages=["National Formulary Status", "Regional Plan Detail",
               "PBM Rebate Performance", "GTN Walk", "ASP Tracking",
               "Patient Out-of-Pocket Distribution", "340B Volume",
               "Government Channel Mix"],
        base_traffic=0.9,
        persona_weights={"Commercial": 2.8, "Exec": 1.6, "HEOR Analyst": 0.7},
    ),
    Report(
        report_id="rep-com-field",
        report_name="Field Force Daily",
        workspace_id="ws-commercial",
        workspace_name="Commercial Analytics",
        capacity_name="Capacity-P1",
        pages=["My Territory", "Call Plan vs Actual", "HCP Targeting",
               "Sample Inventory", "Speaker Booking Pipeline",
               "Latest Approvals/Labels"],
        base_traffic=2.4,
        persona_weights={"Field Rep": 4.0, "Commercial": 1.0},
    ),

    # ---- Manufacturing workspace ---------------------------------------------
    Report(
        report_id="rep-mfg-yield",
        report_name="Manufacturing Yield & OEE",
        workspace_id="ws-mfg",
        workspace_name="Manufacturing & Supply",
        capacity_name="HLS-Fabric-Premium-P2",
        pages=["Plant Overview", "Line OEE", "Yield by Batch",
               "Deviations", "CAPA Aging", "Cycle Time", "Downtime Reasons",
               "Cleaning Validation", "Stability Studies", "Cold Chain Events"],
        base_traffic=0.8,
        persona_weights={"Mfg Engineer": 3.0, "Quality": 2.2, "Exec": 0.6},
    ),
    Report(
        report_id="rep-mfg-supply",
        report_name="Supply Chain Tower",
        workspace_id="ws-mfg",
        workspace_name="Manufacturing & Supply",
        capacity_name="HLS-Fabric-Premium-P2",
        pages=["DC Inventory", "Days of Supply", "Backorder Risk",
               "API Sourcing", "CMO Performance", "Shipping Lane Reliability",
               "Tariff Impact"],
        base_traffic=0.6,
        persona_weights={"Mfg Engineer": 1.6, "Commercial": 1.0, "Exec": 1.0},
    ),
]

# Personas with realistic monthly active users per persona
PERSONAS = {
    "ClinOps Mgr": 18,
    "Med Director": 12,
    "Biostatistician": 14,
    "Safety": 9,
    "Data Mgr": 11,
    "MSL": 26,
    "HEOR Analyst": 8,
    "Commercial": 22,
    "Exec": 6,
    "Field Rep": 64,
    "Mfg Engineer": 14,
    "Quality": 9,
}


def _zipf_weights(n: int, s: float = 1.35) -> list[float]:
    """Power-law (Zipf-like) weights — long-tail page popularity."""
    raw = [1.0 / ((i + 1) ** s) for i in range(n)]
    total = sum(raw)
    return [r / total for r in raw]


def _shuffled_zipf(n: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    w = _zipf_weights(n)
    # Bias so the first 1/3 of pages tend to be the hot ones,
    # but shuffle within bands so it's not perfectly monotonic.
    bands = [w[:n // 3], w[n // 3: 2 * n // 3], w[2 * n // 3:]]
    for b in bands:
        rng.shuffle(b)
    return bands[0] + bands[1] + bands[2]


# ---------------------------------------------------------------------------
# Event generation
# ---------------------------------------------------------------------------

@dataclass
class PageViewRow:
    workspace_id: str
    workspace_name: str
    capacity_name: str
    report_id: str
    report_name: str
    report_total_pages: int
    page_id: str
    page_name: str
    page_ordinal: int
    view_date: date
    view_count: int
    unique_users: int
    avg_dwell_seconds: float
    top_persona: str


def generate_rows(
    start: date,
    end: date,
    catalog: Iterable[Report] = CATALOG,
) -> list[PageViewRow]:
    rows: list[PageViewRow] = []
    days = (end - start).days + 1
    for report in catalog:
        page_weights = _shuffled_zipf(len(report.pages), seed=_stable_hash(report.report_id))
        # Designate a few "appendix" pages in large reports as never-viewed
        # to demonstrate the underused-pages narrative more dramatically.
        skip_ordinals: set[int] = set()
        if len(report.pages) >= 30:
            # Force the last 3-5 pages (typically appendices) to have zero views.
            num_skip = 3 + (len(report.pages) // 30)
            skip_ordinals = set(range(len(report.pages) - num_skip, len(report.pages)))
        for d_offset in range(days):
            day = start + timedelta(days=d_offset)
            # weekday seasonality
            dow = day.weekday()
            dow_mult = 1.0 if dow < 5 else 0.25
            # monthly ramp: gradual growth over 90 days
            ramp = 0.6 + (d_offset / max(days - 1, 1)) * 0.8
            # daily volume target for this report
            target_views = int(
                max(1, round(report.base_traffic * 140 * dow_mult * ramp))
            )
            # spread across pages by zipf weight
            for page_ix, page_name in enumerate(report.pages):
                if page_ix in skip_ordinals:
                    continue  # appendix never visited
                expected = target_views * page_weights[page_ix]
                # Poisson-ish noise via gauss
                noise = RNG.gauss(0, math.sqrt(max(expected, 0.5)))
                views = max(0, int(round(expected + noise)))
                if views == 0:
                    continue
                # Pick top persona for this page weighted by the report's persona profile
                personas = list(report.persona_weights.keys()) or list(PERSONAS.keys())
                weights = [report.persona_weights.get(p, 0.1) for p in personas]
                top_persona = RNG.choices(personas, weights=weights, k=1)[0]
                # Unique users: capped by active per persona, scaled by sqrt
                pool = PERSONAS.get(top_persona, 10)
                unique_users = min(pool, max(1, int(round(math.sqrt(views) * 0.7))))
                # Dwell time: hot pages ~45s, cold pages ~12s; deep clinical pages longer
                base_dwell = 15 + 60 * page_weights[page_ix] / max(page_weights)
                avg_dwell = round(max(4.0, RNG.gauss(base_dwell, 6.0)), 1)

                rows.append(
                    PageViewRow(
                        workspace_id=report.workspace_id,
                        workspace_name=report.workspace_name,
                        capacity_name=report.capacity_name,
                        report_id=report.report_id,
                        report_name=report.report_name,
                        report_total_pages=len(report.pages),
                        page_id=f"{report.report_id}::p{page_ix:03d}",
                        page_name=page_name,
                        page_ordinal=page_ix + 1,
                        view_date=day,
                        view_count=views,
                        unique_users=unique_users,
                        avg_dwell_seconds=avg_dwell,
                        top_persona=top_persona,
                    )
                )
    return rows


def write_csv(rows: list[PageViewRow], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "workspace_id", "workspace_name", "capacity_name",
        "report_id", "report_name", "report_total_pages",
        "page_id", "page_name", "page_ordinal",
        "view_date", "view_count", "unique_users",
        "avg_dwell_seconds", "top_persona",
    ]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for r in rows:
            w.writerow([
                r.workspace_id, r.workspace_name, r.capacity_name,
                r.report_id, r.report_name, r.report_total_pages,
                r.page_id, r.page_name, r.page_ordinal,
                r.view_date.isoformat(), r.view_count, r.unique_users,
                r.avg_dwell_seconds, r.top_persona,
            ])


def write_catalog(out: Path) -> None:
    """Sidecar file listing every defined page (including never-viewed) so
    downstream aggregations can render real page names for zero-view pages.
    The actual Power BI Usage Metrics dataset includes this catalog via the
    Reports table joined with Report pages table."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["workspace_id", "workspace_name", "report_id", "report_name",
                    "page_id", "page_name", "page_ordinal", "report_total_pages"])
        for report in CATALOG:
            for ix, page in enumerate(report.pages):
                w.writerow([
                    report.workspace_id, report.workspace_name,
                    report.report_id, report.report_name,
                    f"{report.report_id}::p{ix:03d}", page, ix + 1,
                    len(report.pages),
                ])


if __name__ == "__main__":
    # Generate 90 days ending "today" relative to the demo
    end = date(2026, 5, 27)
    start = end - timedelta(days=89)
    rows = generate_rows(start, end)
    out = Path(__file__).parent / "sample_data" / "page_views.csv"
    write_csv(rows, out)
    catalog_out = Path(__file__).parent / "sample_data" / "reports_catalog.csv"
    write_catalog(catalog_out)
    print(f"wrote {len(rows):,} rows to {out}")
    print(f"wrote catalog to {catalog_out}")
