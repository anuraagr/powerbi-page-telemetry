"""Aggregate the page-views CSV into a compact JSON for the dashboard."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
CSV_PATH = HERE / "sample_data" / "page_views.csv"
CATALOG_PATH = HERE / "sample_data" / "reports_catalog.csv"
JSON_PATH = HERE.parent / "dashboard" / "page_views.json"


def _row_iter(path: Path):
    with path.open("r", encoding="utf-8") as f:
        # Skip a leading `# silver_schema_version=...` comment line if present.
        first = f.readline()
        if not first.startswith("#"):
            f.seek(0)
        for r in csv.DictReader(f):
            r["view_count"] = int(r["view_count"])
            r["unique_users"] = int(r["unique_users"])
            r["avg_dwell_seconds"] = float(r["avg_dwell_seconds"])
            r["report_total_pages"] = int(r["report_total_pages"])
            r["page_ordinal"] = int(r["page_ordinal"])
            yield r


def aggregate() -> dict:
    rows = list(_row_iter(CSV_PATH))

    # Load full report/page catalog (so we know names for never-viewed pages).
    catalog: dict[str, dict[int, str]] = defaultdict(dict)
    if CATALOG_PATH.exists():
        with CATALOG_PATH.open("r", encoding="utf-8") as f:
            for cr in csv.DictReader(f):
                catalog[cr["report_id"]][int(cr["page_ordinal"])] = cr["page_name"]

    # ---- meta -------------------------------------------------------------
    dates = sorted({r["view_date"] for r in rows})
    workspaces = {r["workspace_id"]: r["workspace_name"] for r in rows}
    reports = {r["report_id"]: r for r in rows}
    pages = {r["page_id"]: r for r in rows}
    total_views = sum(r["view_count"] for r in rows)

    # ---- per-workspace summary --------------------------------------------
    ws_views: dict[str, int] = defaultdict(int)
    ws_reports: dict[str, set] = defaultdict(set)
    ws_pages: dict[str, set] = defaultdict(set)
    ws_users_proxy: dict[str, int] = defaultdict(int)
    for r in rows:
        ws_views[r["workspace_id"]] += r["view_count"]
        ws_reports[r["workspace_id"]].add(r["report_id"])
        ws_pages[r["workspace_id"]].add(r["page_id"])
        ws_users_proxy[r["workspace_id"]] = max(ws_users_proxy[r["workspace_id"]], r["unique_users"])

    workspaces_out = []
    for wid, name in workspaces.items():
        workspaces_out.append({
            "workspace_id": wid,
            "workspace_name": name,
            "report_count": len(ws_reports[wid]),
            "page_count": len(ws_pages[wid]),
            "total_views": ws_views[wid],
        })
    workspaces_out.sort(key=lambda x: -x["total_views"])

    # ---- per-report summary + per-page detail -----------------------------
    report_views: dict[str, int] = defaultdict(int)
    report_pages: dict[str, dict[str, dict]] = defaultdict(dict)
    report_persona_views: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    report_active_users_proxy: dict[str, int] = defaultdict(int)
    page_first_seen: dict[str, str] = {}
    page_last_seen: dict[str, str] = {}

    for r in rows:
        report_views[r["report_id"]] += r["view_count"]
        rp = report_pages[r["report_id"]].setdefault(r["page_id"], {
            "page_id": r["page_id"],
            "page_name": r["page_name"],
            "page_ordinal": r["page_ordinal"],
            "views": 0,
            "users_peak": 0,
            "dwell_total": 0.0,
            "dwell_n": 0,
        })
        rp["views"] += r["view_count"]
        rp["users_peak"] = max(rp["users_peak"], r["unique_users"])
        rp["dwell_total"] += r["avg_dwell_seconds"] * r["view_count"]
        rp["dwell_n"] += r["view_count"]
        report_persona_views[r["report_id"]][r["top_persona"]] += r["view_count"]
        report_active_users_proxy[r["report_id"]] = max(
            report_active_users_proxy[r["report_id"]], r["unique_users"]
        )
        if r["page_id"] not in page_first_seen or r["view_date"] < page_first_seen[r["page_id"]]:
            page_first_seen[r["page_id"]] = r["view_date"]
        if r["page_id"] not in page_last_seen or r["view_date"] > page_last_seen[r["page_id"]]:
            page_last_seen[r["page_id"]] = r["view_date"]

    reports_out = []
    for rid, sample in reports.items():
        pages_list = []
        for p in sorted(report_pages[rid].values(), key=lambda x: x["page_ordinal"]):
            avg_dwell = round(p["dwell_total"] / p["dwell_n"], 1) if p["dwell_n"] else 0.0
            pages_list.append({
                "page_id": p["page_id"],
                "page_name": p["page_name"],
                "page_ordinal": p["page_ordinal"],
                "views": p["views"],
                "users_peak": p["users_peak"],
                "avg_dwell_seconds": avg_dwell,
                "first_seen": page_first_seen.get(p["page_id"]),
                "last_seen": page_last_seen.get(p["page_id"]),
            })

        # Pages defined in the report but never viewed (zero-view pages)
        defined_ordinals = {p["page_ordinal"] for p in pages_list}
        defined_count = sample["report_total_pages"]
        missing = defined_count - len(defined_ordinals)
        for ord_i in range(1, defined_count + 1):
            if ord_i not in defined_ordinals:
                real_name = catalog.get(rid, {}).get(ord_i, f"(page {ord_i})")
                pages_list.append({
                    "page_id": f"{rid}::p{ord_i - 1:03d}",
                    "page_name": real_name,
                    "page_ordinal": ord_i,
                    "views": 0,
                    "users_peak": 0,
                    "avg_dwell_seconds": 0.0,
                    "first_seen": None,
                    "last_seen": None,
                })

        pages_list.sort(key=lambda x: x["page_ordinal"])
        persona_breakdown = sorted(
            [{"persona": k, "views": v} for k, v in report_persona_views[rid].items()],
            key=lambda x: -x["views"],
        )
        reports_out.append({
            "report_id": rid,
            "report_name": sample["report_name"],
            "workspace_id": sample["workspace_id"],
            "workspace_name": sample["workspace_name"],
            "capacity_name": sample["capacity_name"],
            "total_pages": sample["report_total_pages"],
            "pages_with_views": len([p for p in pages_list if p["views"] > 0]),
            "pages_never_viewed": missing,
            "total_views": report_views[rid],
            "active_users_peak": report_active_users_proxy[rid],
            "personas": persona_breakdown,
            "pages": pages_list,
        })
    reports_out.sort(key=lambda x: -x["total_views"])

    # ---- daily trend -------------------------------------------------------
    daily_totals: dict[str, int] = defaultdict(int)
    for r in rows:
        daily_totals[r["view_date"]] += r["view_count"]
    daily_out = [{"date": d, "views": daily_totals[d]} for d in dates]

    # ---- top pages tenant-wide --------------------------------------------
    flat_pages: list[dict] = []
    for r in reports_out:
        for p in r["pages"]:
            if p["views"] <= 0:
                continue
            flat_pages.append({
                "workspace_name": r["workspace_name"],
                "report_name": r["report_name"],
                "page_name": p["page_name"],
                "views": p["views"],
            })
    top_pages = sorted(flat_pages, key=lambda x: -x["views"])[:25]

    # ---- underused pages: pages in active reports with <50 views in 90d ----
    underused_threshold = 100
    underused = [p for p in flat_pages if p["views"] < underused_threshold]
    underused.sort(key=lambda x: x["views"])

    # ---- distribution histogram ------------------------------------------
    buckets = [(0, 0), (1, 10), (11, 50), (51, 200), (201, 1000), (1001, 5000), (5001, 10**9)]
    bucket_labels = ["0 (never)", "1–10", "11–50", "51–200", "201–1k", "1k–5k", "5k+"]
    bucket_counts = [0] * len(buckets)
    all_pages_iter = [p for r in reports_out for p in r["pages"]]
    for p in all_pages_iter:
        v = p["views"]
        for i, (lo, hi) in enumerate(buckets):
            if lo <= v <= hi:
                bucket_counts[i] += 1
                break
    distribution = [{"bucket": l, "pages": c} for l, c in zip(bucket_labels, bucket_counts)]

    # ---- meta header -------------------------------------------------------
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "date_range": [dates[0], dates[-1]],
        "days": len(dates),
        "total_workspaces": len(workspaces),
        "total_reports": len(reports_out),
        "total_pages_defined": sum(r["total_pages"] for r in reports_out),
        "total_pages_with_views": sum(r["pages_with_views"] for r in reports_out),
        "total_pages_never_viewed": sum(r["pages_never_viewed"] for r in reports_out),
        "total_views": total_views,
        "underused_threshold": underused_threshold,
        "underused_count": len(underused),
    }

    return {
        "meta": meta,
        "workspaces": workspaces_out,
        "reports": reports_out,
        "daily": daily_out,
        "top_pages": top_pages,
        "underused_pages": underused[:200],
        "distribution": distribution,
    }


if __name__ == "__main__":
    out = aggregate()
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {JSON_PATH} "
          f"({JSON_PATH.stat().st_size / 1024:.1f} KB, "
          f"{out['meta']['total_reports']} reports, "
          f"{out['meta']['total_pages_defined']} pages, "
          f"{out['meta']['total_views']:,} views)")
