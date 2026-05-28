# Gold-layer query cookbook

Hands-on DAX and SQL for the questions customers actually ask once
the silver layer is landing. Drop these straight into Fabric SQL
endpoint, ADX/KQL, or a Power BI semantic model on top of the
template in [`dashboard/PowerBI/`](../dashboard/PowerBI/).

Assumes a Delta table `page_views_silver` with the silver schema
(see [`data-dictionary.md`](data-dictionary.md)).

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
