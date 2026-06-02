# Gold-layer query cookbook

Hands-on DAX and SQL for the questions customers actually ask once
the silver layer is landing. Drop these straight into Fabric SQL
endpoint, ADX/KQL, or a Power BI semantic model on top of the
template in [`dashboard/PowerBI/`](../dashboard/PowerBI/).

Assumes Delta tables with the v1.1.0 silver schema (see
[`data-dictionary.md`](data-dictionary.md)):

- `page_views_silver`     fact (page, day)
- `page_catalog_silver`   dim  (every page that EXISTS, v0.3.0)
- `report_views_silver`   fact (report, day, v0.3.0)
- `user_views_silver`     fact (hashed user, day, v0.3.0)

## 1. Top 10 underused pages tenant-wide

The question every BI admin asks first: "which pages are we paying to
maintain that nobody opens?"

### Spark / Fabric SQL

```sql
WITH page_totals AS (
    SELECT
        workspace_name,
        report_name,
        page_name,
        SUM(view_count) AS total_views,
        SUM(unique_users) AS total_unique_users,
        AVG(avg_dwell_seconds) AS avg_dwell
    FROM page_views_silver
    WHERE view_date >= current_date() - INTERVAL 90 DAYS
    GROUP BY workspace_name, report_name, page_name
)
SELECT *
FROM page_totals
WHERE total_views <= 10
ORDER BY total_views ASC, report_name, page_name
LIMIT 10;
```

### DAX

```dax
Underused Top 10 =
    VAR _90 = MAX ( 'page_views_silver'[view_date] ) - 90
    VAR _PageTotals =
        SUMMARIZE (
            FILTER ( 'page_views_silver', 'page_views_silver'[view_date] >= _90 ),
            'page_views_silver'[workspace_name],
            'page_views_silver'[report_name],
            'page_views_silver'[page_name],
            "TotalViews", SUM ( 'page_views_silver'[view_count] )
        )
    RETURN
        TOPN ( 10, FILTER ( _PageTotals, [TotalViews] <= 10 ), [TotalViews], ASC )
```

## 1a. UNUSED pages — every page in the catalog with zero views (v0.3.0)

The killer query for v0.3.0 and the one Jon at Incyte asked for:
"which pages in our 60-page Phase III report has nobody opened in 90
days?" Pages with zero views literally do not exist in
`page_views_silver` (it's a fact table) — you can only find them by
LEFT JOIN-ing the catalog.

### Spark / Fabric SQL

```sql
SELECT
    pc.workspace_name,
    pc.report_name,
    pc.page_ordinal,
    pc.page_name,
    pc.catalog_pulled_at
FROM page_catalog_silver pc
LEFT JOIN (
    SELECT DISTINCT workspace_id, report_id, page_id
    FROM page_views_silver
    WHERE view_date >= current_date() - INTERVAL 90 DAYS
) pv
  ON  pv.workspace_id = pc.workspace_id
  AND pv.report_id    = pc.report_id
  AND pv.page_id      = pc.page_id
WHERE pv.page_id IS NULL
ORDER BY pc.workspace_name, pc.report_name, pc.page_ordinal;
```

Mock-data result (`python etl/collector.py --mock`):

```
workspace_name             | report_name                | page_ordinal | page_name
---------------------------|----------------------------|--------------|--------------------------------
Clinical Operations        | Phase III Trial - STUDY-101| 47           | Protocol v1 (legacy)
Clinical Operations        | Phase III Trial - STUDY-101| 48           | DEBUG: per-site raw rates
Clinical Operations        | Phase III Trial - STUDY-101| 49           | Enrollment funnel (deprecated)
... 7 more across STUDY-202 and STUDY-303
```

### DAX (model with `page_views`, `page_catalog`)

Define a `[page_key]` calculated column on both tables (see
`dashboard/PowerBI/README.md`), then:

```dax
Unused Pages =
    VAR _Viewed =
        CALCULATETABLE (
            VALUES ( 'page_catalog'[page_key] ),
            'page_views'
        )
    RETURN
        COUNTROWS (
            EXCEPT ( VALUES ( 'page_catalog'[page_key] ), _Viewed )
        )

Reports With Unused Pages =
    VAR _UnusedByReport =
        ADDCOLUMNS (
            SUMMARIZE (
                'page_catalog',
                'page_catalog'[workspace_id],
                'page_catalog'[report_id]
            ),
            "UnusedHere", [Unused Pages]
        )
    RETURN
        COUNTROWS ( FILTER ( _UnusedByReport, [UnusedHere] > 0 ) )
```

### Take-this-to-the-report-owner export

To produce a CSV the BI ops team can email to each report owner,
group the SQL above by `workspace_name`, run once per workspace, and
attach to a templated email. Many customers automate this with a
Logic App / Power Automate flow reading the same Delta table.

## 1b. Page coverage rate per report (v0.3.0)

What % of each report's pages get used at all? A report with
`coverage_rate < 50%` is probably trying to be 2-3 reports.

### Spark / Fabric SQL

```sql
WITH defined AS (
    SELECT workspace_id, workspace_name, report_id, report_name,
           COUNT(DISTINCT page_id) AS pages_defined
    FROM page_catalog_silver
    GROUP BY workspace_id, workspace_name, report_id, report_name
),
viewed AS (
    SELECT workspace_id, report_id, COUNT(DISTINCT page_id) AS pages_viewed
    FROM page_views_silver
    WHERE view_date >= current_date() - INTERVAL 90 DAYS
    GROUP BY workspace_id, report_id
)
SELECT
    d.workspace_name,
    d.report_name,
    d.pages_defined,
    COALESCE(v.pages_viewed, 0) AS pages_viewed,
    ROUND(100.0 * COALESCE(v.pages_viewed, 0) / d.pages_defined, 1) AS coverage_pct
FROM defined d
LEFT JOIN viewed v
  ON v.workspace_id = d.workspace_id AND v.report_id = d.report_id
ORDER BY coverage_pct ASC, pages_defined DESC;
```

## 1c. Pages-per-session by report (v0.3.0)

The page grain ÷ the report grain. A report where users open only one
page per session is mostly an entrance experience — find these and
either trim them or invest in cross-page navigation.

### Spark / Fabric SQL

```sql
SELECT
    pv.workspace_name,
    pv.report_name,
    SUM(pv.view_count) AS total_page_views,
    SUM(rv.view_count) AS total_report_sessions,
    ROUND(SUM(pv.view_count) * 1.0 / NULLIF(SUM(rv.view_count), 0), 2)
        AS pages_per_session,
    AVG(rv.avg_session_seconds) AS avg_session_seconds
FROM page_views_silver pv
JOIN report_views_silver rv
  ON  rv.workspace_id = pv.workspace_id
  AND rv.report_id    = pv.report_id
  AND rv.view_date    = pv.view_date
WHERE pv.view_date >= current_date() - INTERVAL 30 DAYS
GROUP BY pv.workspace_name, pv.report_name
HAVING SUM(rv.view_count) > 0
ORDER BY pages_per_session ASC;
```

## 1d. Power users per report (v0.3.0, hashed)

Find your internal champions. Hashed UPNs from `user_views_silver` —
to convert hashes back to names, join against a separately-governed
"people" table with RLS (do NOT join in the same dataset).

### Spark / Fabric SQL

```sql
SELECT
    workspace_name,
    report_name,
    user_id_hash,
    SUM(view_count) AS lifetime_page_views,
    SUM(distinct_pages_viewed) AS lifetime_distinct_pages,
    COUNT(DISTINCT view_date) AS active_days
FROM user_views_silver
WHERE view_date >= current_date() - INTERVAL 90 DAYS
GROUP BY workspace_name, report_name, user_id_hash
HAVING SUM(view_count) >= 50
ORDER BY lifetime_page_views DESC
LIMIT 50;
```

## 2. Report half-life — how fast does a report's audience decay?

A "young" report grows traffic week-over-week. An "old" report's traffic
collapses to a power-user core. The half-life is the number of days
between a report's peak and the day its rolling-7d views fall below
half of that peak.

```sql
WITH rolling AS (
    SELECT
        report_id,
        report_name,
        view_date,
        SUM(view_count) OVER (
            PARTITION BY report_id
            ORDER BY view_date
            ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
        ) AS rolling_7d
    FROM page_views_silver
),
peak AS (
    SELECT
        report_id,
        report_name,
        MAX(rolling_7d) AS peak_rolling_7d,
        FIRST_VALUE(view_date) OVER (
            PARTITION BY report_id ORDER BY rolling_7d DESC
        ) AS peak_date
    FROM rolling
),
post_peak AS (
    SELECT
        r.report_id,
        r.report_name,
        r.view_date,
        r.rolling_7d,
        p.peak_rolling_7d,
        p.peak_date
    FROM rolling r
    JOIN peak p USING (report_id)
    WHERE r.view_date > p.peak_date
      AND r.rolling_7d < p.peak_rolling_7d / 2.0
)
SELECT
    report_name,
    peak_date,
    MIN(view_date) AS half_date,
    DATEDIFF(MIN(view_date), peak_date) AS half_life_days,
    peak_rolling_7d
FROM post_peak
GROUP BY report_name, peak_date, peak_rolling_7d
ORDER BY half_life_days;
```

Use it to identify launch reports that need a refresh — anything with
half-life < 14 days is probably a one-off email link instead of a
sustained-use report.

## 3. Long-tail distribution per capacity

Are the workloads on your Premium capacities concentrated in a handful
of reports, or genuinely fanned out? Useful for SKU justification and
for guiding workspace-to-capacity rebalancing.

```sql
WITH ranked AS (
    SELECT
        capacity_name,
        report_id,
        report_name,
        SUM(view_count) AS report_views,
        ROW_NUMBER() OVER (
            PARTITION BY capacity_name ORDER BY SUM(view_count) DESC
        ) AS rank
    FROM page_views_silver
    WHERE view_date >= current_date() - INTERVAL 30 DAYS
    GROUP BY capacity_name, report_id, report_name
),
cumulative AS (
    SELECT
        capacity_name,
        rank,
        report_views,
        SUM(report_views) OVER (
            PARTITION BY capacity_name ORDER BY rank
        ) AS running_total,
        SUM(report_views) OVER (PARTITION BY capacity_name) AS capacity_total
    FROM ranked
)
SELECT
    capacity_name,
    MIN(CASE WHEN running_total * 1.0 / capacity_total >= 0.80 THEN rank END) AS reports_for_80pct
FROM cumulative
GROUP BY capacity_name
ORDER BY reports_for_80pct;
```

Reads: "for each capacity, how many distinct reports do you need to
explain 80% of its traffic." A low number → workloads concentrated,
SKU is probably oversized for what it's actually serving. A high
number → workloads truly fanned out, justify the SKU.

## 4. Page-load funnel — where do users drop off in a long report?

This is the most powerful question for trimming bloated reports. A 30-page
clinical trial tracker where page 8 onwards never sees more than 5% of
page 1's traffic should probably be split into a 7-page core report
plus an on-demand appendix.

```sql
WITH page_traffic AS (
    SELECT
        report_id,
        report_name,
        page_ordinal,
        page_name,
        SUM(view_count) AS page_views
    FROM page_views_silver
    WHERE view_date >= current_date() - INTERVAL 30 DAYS
      AND report_id = 'rep-clin-study101'    -- pick a report
    GROUP BY report_id, report_name, page_ordinal, page_name
),
relative AS (
    SELECT
        page_ordinal,
        page_name,
        page_views,
        page_views * 1.0 / MAX(page_views) OVER () AS pct_of_max
    FROM page_traffic
)
SELECT * FROM relative ORDER BY page_ordinal;
```

DAX equivalent for a generic chart in Power BI:

```dax
% Of First Page =
VAR _First = CALCULATE (
        [Total Views],
        FILTER ( ALL ( 'page_views_silver' ),
                 'page_views_silver'[report_id] = MAX ( 'page_views_silver'[report_id] ) &&
                 'page_views_silver'[page_ordinal] = 1 )
    )
RETURN DIVIDE ( [Total Views], _First )
```

Plot `page_ordinal` × `% Of First Page` per report — the cliff
location is your "where to split" answer.

## 5. New-page adoption velocity

When a new page is added to a report, how long does it take to reach
50% of the report's average page-view count?

```sql
WITH first_seen AS (
    SELECT
        report_id,
        page_id,
        page_name,
        MIN(view_date) AS first_view
    FROM page_views_silver
    GROUP BY report_id, page_id, page_name
),
recent_avg AS (
    SELECT
        report_id,
        AVG(daily_views) AS report_page_avg
    FROM (
        SELECT
            report_id,
            page_id,
            view_date,
            SUM(view_count) AS daily_views
        FROM page_views_silver
        GROUP BY report_id, page_id, view_date
    ) t
    GROUP BY report_id
),
adoption AS (
    SELECT
        p.report_id,
        p.page_id,
        p.page_name,
        p.view_date,
        p.view_count,
        f.first_view,
        DATEDIFF(p.view_date, f.first_view) AS days_since_intro,
        r.report_page_avg
    FROM page_views_silver p
    JOIN first_seen f USING (report_id, page_id)
    JOIN recent_avg r USING (report_id)
)
SELECT
    page_name,
    MIN(CASE WHEN view_count >= report_page_avg / 2 THEN days_since_intro END)
        AS days_to_half_avg
FROM adoption
WHERE first_view >= current_date() - INTERVAL 60 DAYS  -- only newish pages
GROUP BY page_name
ORDER BY days_to_half_avg;
```

`NULL` = the page has never reached half the report's average. Anything
> 30 days is suspect — either the page is genuinely niche, or it isn't
linked from anywhere obvious.

## 6. Workspace owner accountability

Pair the silver layer with a one-row-per-workspace ownership table to
produce a per-owner "underused page" hit list — see
[`pii-and-retention.md`](pii-and-retention.md) for the
`WorkspaceOwners` table pattern.

```sql
SELECT
    o.owner_upn,
    o.owner_name,
    COUNT(DISTINCT p.page_id) FILTER (
        WHERE p.total_views <= 10
    ) AS underused_pages,
    COUNT(DISTINCT p.report_id) AS reports_owned,
    SUM(p.total_views) AS total_views_owned
FROM workspace_owners o
JOIN (
    SELECT
        workspace_id,
        report_id,
        page_id,
        SUM(view_count) AS total_views
    FROM page_views_silver
    WHERE view_date >= current_date() - INTERVAL 90 DAYS
    GROUP BY workspace_id, report_id, page_id
) p USING (workspace_id)
GROUP BY o.owner_upn, o.owner_name
ORDER BY underused_pages DESC;
```

Use it to drive a "spring cleaning" email — "your workspace has 12
pages that haven't been opened in 90 days; archive or rebuild."
