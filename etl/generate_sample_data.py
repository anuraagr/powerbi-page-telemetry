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
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

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


# ---------------------------------------------------------------------------
# Themes
#
# `--theme` lets demoers regenerate the synthetic sample data with
# workspace / report / page names that fit the customer's industry. The
# statistical model is identical across themes — same traffic patterns,
# same Zipf distribution, same dwell times, same never-viewed appendices —
# only the labels change. That keeps the dashboard narratives ("9 pages
# never opened in 90 days", "top 5 reports account for 65% of views")
# consistent across themes.
# ---------------------------------------------------------------------------


def _retheme(catalog: list[Report], name_prefix: str, ws_themes: dict[str, dict]) -> list[Report]:
    """Clone the structural shape of `catalog` (page counts, traffic, persona
    weights, capacity attribution) and substitute domain-specific names.

    `ws_themes` maps the original `workspace_id` (e.g. "ws-clinops") to:
        - `workspace_id`   : new workspace id
        - `workspace_name` : new workspace display name
        - `reports`        : list of (report_id, report_name, [page_names]) tuples
                             in the same order as the original workspace's reports
    Any tuple whose page list is shorter than the original gets padded with
    "Section N" pages so total_pages match — preserving the never-viewed
    appendix percentages.
    """
    new_catalog: list[Report] = []
    # Group original by workspace_id, preserving order.
    by_ws: dict[str, list[Report]] = {}
    for rep in catalog:
        by_ws.setdefault(rep.workspace_id, []).append(rep)

    for orig_ws_id, reports in by_ws.items():
        theme = ws_themes.get(orig_ws_id)
        if theme is None:
            new_catalog.extend(reports)
            continue
        themed_reports = theme.get("reports", [])
        for i, orig in enumerate(reports):
            if i < len(themed_reports):
                new_id, new_name, page_names = themed_reports[i]
            else:
                new_id = f"{name_prefix}-rep-{orig.workspace_id}-{i}"
                new_name = f"{theme['workspace_name']} Report {i + 1}"
                page_names = []
            total = len(orig.pages)
            # Pad / trim page name list to match original total
            pages = list(page_names)[:total]
            while len(pages) < total:
                pages.append(f"Section {len(pages) + 1}")
            new_catalog.append(
                Report(
                    report_id=new_id,
                    report_name=new_name,
                    workspace_id=theme["workspace_id"],
                    workspace_name=theme["workspace_name"],
                    capacity_name=orig.capacity_name,
                    pages=pages,
                    base_traffic=orig.base_traffic,
                    persona_weights=orig.persona_weights,
                ),
            )
    return new_catalog


_GENERIC_THEMES = {
    "ws-clinops": {
        "workspace_id": "ws-ops",
        "workspace_name": "Operations Analytics",
        "reports": [
            ("rep-ops-orders", "Order Lifecycle — Region East",
             ["Funnel", "Status", "SLAs", "Backlog", "Exceptions", "Aging"]),
            ("rep-ops-fulfillment", "Fulfillment — Daily Standup",
             ["Inbound", "Outbound", "Returns", "Carrier Mix"]),
            ("rep-ops-warehouse", "Warehouse Throughput",
             ["Picks/Hour", "Dock Door Utilization", "Damage Rate"]),
            ("rep-ops-portfolio", "Operations Portfolio",
             ["Heatmap", "Milestones", "Forecast", "Budget", "Risks"]),
        ],
    },
    "ws-medaffairs": {
        "workspace_id": "ws-marketing",
        "workspace_name": "Marketing Analytics",
        "reports": [
            ("rep-mkt-campaigns", "Campaign Performance",
             ["Spend", "Reach", "Engagement", "Conversion"]),
            ("rep-mkt-attribution", "Multi-Touch Attribution",
             ["Channel Mix", "Path Length", "Decay Models"]),
            ("rep-mkt-events", "Event ROI",
             ["Tickets", "Attendance", "Pipeline Influence"]),
        ],
    },
    "ws-rwe": {
        "workspace_id": "ws-product",
        "workspace_name": "Product Analytics",
        "reports": [
            ("rep-prd-funnel", "Activation Funnel — Cohort Builder",
             ["Cohort", "DAU/MAU", "Retention", "Stickiness"]),
            ("rep-prd-feature", "Feature Adoption",
             ["Top Features", "Adoption Curve", "Stickiness"]),
            ("rep-prd-quality", "Telemetry Quality",
             ["Freshness", "Coverage", "Drift"]),
        ],
    },
    "ws-commercial": {
        "workspace_id": "ws-sales",
        "workspace_name": "Sales Analytics",
        "reports": [
            ("rep-sales-launch", "Launch Scorecard — Product A",
             ["Pipeline", "Win Rate", "Forecast", "Quotas"]),
            ("rep-sales-customers", "Customer Health",
             ["NPS", "Renewals", "Risk", "Expansion"]),
            ("rep-sales-rep", "Rep Performance",
             ["Activity", "Quota Attainment", "Coaching"]),
        ],
    },
    "ws-mfg": {
        "workspace_id": "ws-platform",
        "workspace_name": "Platform Engineering",
        "reports": [
            ("rep-plat-services", "Service Reliability",
             ["SLOs", "Error Budgets", "Incidents", "Postmortems"]),
            ("rep-plat-capacity", "Capacity Planning",
             ["Compute", "Storage", "Network", "Forecast"]),
        ],
    },
}

_FINANCIAL_THEMES = {
    "ws-clinops": {
        "workspace_id": "ws-fin-trading",
        "workspace_name": "Trading Floor Analytics",
        "reports": [
            ("rep-fin-pnl", "Daily P&L by Desk",
             ["FX", "Rates", "Credit", "Equities", "Commodities"]),
            ("rep-fin-risk", "VaR & Risk Limits",
             ["95% VaR", "ES", "Limit Breaches"]),
            ("rep-fin-positions", "Positions & Hedging",
             ["Open Positions", "Hedge Effectiveness"]),
            ("rep-fin-exec", "Executive P&L Summary",
             ["Headlines", "Drivers", "Outlook"]),
        ],
    },
    "ws-medaffairs": {
        "workspace_id": "ws-fin-treasury",
        "workspace_name": "Treasury & Liquidity",
        "reports": [
            ("rep-fin-cash", "Cash Forecast",
             ["13-Week Cash", "Counterparty Mix", "FX Hedges"]),
            ("rep-fin-funding", "Funding & Bond Issuance",
             ["Pipeline", "Spreads", "Investor Demand"]),
            ("rep-fin-liquidity", "Liquidity Coverage Ratio",
             ["LCR", "NSFR", "Stress Scenarios"]),
        ],
    },
    "ws-rwe": {
        "workspace_id": "ws-fin-research",
        "workspace_name": "Quant Research",
        "reports": [
            ("rep-fin-factor", "Factor Performance",
             ["Value", "Momentum", "Quality", "Size", "Low Vol"]),
            ("rep-fin-backtest", "Strategy Backtests",
             ["Sharpe", "Drawdown", "Turnover"]),
            ("rep-fin-data", "Alt Data Coverage",
             ["Coverage", "Freshness", "Outliers"]),
        ],
    },
    "ws-commercial": {
        "workspace_id": "ws-fin-wealth",
        "workspace_name": "Wealth Management",
        "reports": [
            ("rep-fin-aum", "AUM by Advisor",
             ["AUM", "Net New", "Concentration"]),
            ("rep-fin-fees", "Fee Realization",
             ["Effective Fee Rate", "Discounts", "Trends"]),
            ("rep-fin-client", "Client Engagement",
             ["Reviews", "Topics", "Action Items"]),
        ],
    },
    "ws-mfg": {
        "workspace_id": "ws-fin-ops",
        "workspace_name": "Operations & Settlements",
        "reports": [
            ("rep-fin-settle", "Settlement Exceptions",
             ["Fails", "Aging", "Root Cause"]),
            ("rep-fin-reconcile", "Reconciliations",
             ["Match Rate", "Aging", "Sources"]),
        ],
    },
}

THEMES: dict[str, list[Report]] = {
    "healthcare": CATALOG,
    "generic": _retheme(CATALOG, "gen", _GENERIC_THEMES),
    "financial": _retheme(CATALOG, "fin", _FINANCIAL_THEMES),
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


def write_catalog(out: Path, catalog: list[Report] | None = None) -> None:
    """Sidecar file listing every defined page (including never-viewed) so
    downstream aggregations can render real page names for zero-view pages.
    The actual Power BI Usage Metrics dataset includes this catalog via the
    Reports table joined with Report pages table."""
    cat = catalog if catalog is not None else CATALOG
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["workspace_id", "workspace_name", "report_id", "report_name",
                    "page_id", "page_name", "page_ordinal", "report_total_pages"])
        for report in cat:
            for ix, page in enumerate(report.pages):
                w.writerow([
                    report.workspace_id, report.workspace_name,
                    report.report_id, report.report_name,
                    f"{report.report_id}::p{ix:03d}", page, ix + 1,
                    len(report.pages),
                ])


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="generate_sample_data.py",
        description="Generate the bundled synthetic sample data for --mock mode. "
                    "The default invocation regenerates the committed bundle byte-for-byte "
                    "(see tests/test_mock_reproducibility.py).",
    )
    parser.add_argument(
        "--theme",
        choices=sorted(THEMES.keys()),
        default="healthcare",
        help="Catalog theme (default: healthcare). "
             "Other themes use the same traffic model and statistical shape, "
             "only the workspace / report / page names change. Use them to make the demo "
             "feel like the customer's own industry.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: ./etl/sample_data for the healthcare theme; "
             "./etl/sample_data/<theme> for other themes, to avoid clobbering the bundled fixture).",
    )
    args = parser.parse_args()

    catalog = THEMES[args.theme]
    out_dir = args.out
    if out_dir is None:
        base = Path(__file__).parent / "sample_data"
        out_dir = base if args.theme == "healthcare" else base / args.theme
    out_dir.mkdir(parents=True, exist_ok=True)

    end = date(2026, 5, 27)
    start = end - timedelta(days=89)
    rows = generate_rows(start, end, catalog=catalog)
    rows_out = out_dir / "page_views.csv"
    write_csv(rows, rows_out)
    cat_out = out_dir / "reports_catalog.csv"
    write_catalog(cat_out, catalog=catalog)
    print(f"wrote {len(rows):,} rows to {rows_out}")
    print(f"wrote catalog to {cat_out}")
