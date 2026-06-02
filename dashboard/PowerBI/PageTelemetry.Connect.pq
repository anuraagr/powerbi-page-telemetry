// PageTelemetry.Connect.pq  -  v0.3.0 (silver schema 1.1.0)
//
// Power Query M script. Paste into Power BI Desktop:
//   Home -> Get Data -> Blank Query -> Advanced Editor -> paste -> Done
//
// In v0.3.0 the collector emits FOUR silver tables (was 1 in v0.2.x):
//
//   page_views     fact   one row per (workspace, report, page, date)
//   page_catalog   dim    every page that EXISTS in every report (catalog)
//   report_views   fact   report-level grain (no page dimension)
//   user_views     fact   per-(hashed)user grain
//
// The unused-page detection answer that Jon at Incyte asked for
// ("which pages in our 60-page clinical-trial report has nobody opened?")
// comes from page_catalog LEFT JOIN page_views.
//
// This script returns a RECORD with four tables. After pasting, click
// "Convert to Table" at the top of the editor, then each row becomes
// a query you load into the model.

let
    // ---------------- 1. Connection mode ----------------
    // "fabric"        -> read from a Fabric Lakehouse Delta table
    // "blob"          -> read from the silver CSVs in Azure Blob Storage
    //                    (the Azure Function deploy option writes here)
    // "local"         -> read from local files on disk
    //
    ConnectionMode = "fabric",

    // ---------------- 2. Per-mode settings --------------
    Fabric_WorkspaceName  = "Analytics-Ops",
    Fabric_LakehouseName  = "PageTelemetry_LH",
    // v0.3.0 Delta table names (match the Fabric notebook MERGE blocks)
    Fabric_TablePageViews    = "page_views_silver",
    Fabric_TablePageCatalog  = "page_catalog_silver",
    Fabric_TableReportViews  = "report_views_silver",
    Fabric_TableUserViews    = "user_views_silver",

    Blob_AccountUrl       = "https://<storage-account>.blob.core.windows.net",
    Blob_Container        = "page-telemetry",
    // Each file in silver/ has the schema-version comment as its first line
    Blob_PathPageViews    = "silver/page_views.csv",
    Blob_PathPageCatalog  = "silver/page_catalog.csv",
    Blob_PathReportViews  = "silver/report_views.csv",
    Blob_PathUserViews    = "silver/user_views.csv",

    Local_SilverDir       = "C:\PowerBI-PageTelemetry\out\silver",

    // ---------------- 3. Schema-version pin -------------
    // Must match SILVER_SCHEMA_VERSION in etl/collector.py.
    // v0.3.0 bumps the MINOR (additive: new tables, no changed columns).
    ExpectedSchemaVersion = "1.1.0",

    // ---------------- Implementation --------------------
    //
    // Helper: read one silver CSV (local or blob) and skip the
    //   `# silver_schema_version=...` comment line if present.
    ReadCsvFromBinary = (bin as binary) as table =>
        let
            Csv      = Csv.Document(
                          bin,
                          [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]
                       ),
            Body     = if Table.RowCount(Csv) > 0 and Text.StartsWith(Csv{0}[Column1], "#")
                       then Table.Skip(Csv, 1)
                       else Csv,
            Promoted = Table.PromoteHeaders(Body, [PromoteAllScalars = true])
        in
            Promoted,

    LoadFabricTable = (tableName as text) as table =>
        let
            Lakehouse = Fabric.Warehouse([CreateNavigationProperties = false]),
            Workspace = Lakehouse{[Name = Fabric_WorkspaceName]}[Data],
            LH        = Workspace{[Name = Fabric_LakehouseName]}[Data]
        in
            LH{[Schema = "dbo", Item = tableName]}[Data],

    LoadBlobCsv = (filePath as text) as table =>
        let
            Blob = AzureStorage.Blobs(Blob_AccountUrl),
            Cont = Blob{[Name = Blob_Container]}[Data],
            File = Cont{[Name = filePath]}[Content]
        in
            ReadCsvFromBinary(File),

    LoadLocalCsv = (fileName as text) as table =>
        ReadCsvFromBinary(File.Contents(Local_SilverDir & "\" & fileName)),

    LoadOne = (fabricTable as text, blobPath as text, localFile as text) =>
        if ConnectionMode = "fabric" then LoadFabricTable(fabricTable)
        else if ConnectionMode = "blob" then LoadBlobCsv(blobPath)
        else if ConnectionMode = "local" then LoadLocalCsv(localFile)
        else error "ConnectionMode must be one of: fabric, blob, local",

    PageViewsRaw    = LoadOne(Fabric_TablePageViews,   Blob_PathPageViews,   "page_views.csv"),
    PageCatalogRaw  = LoadOne(Fabric_TablePageCatalog, Blob_PathPageCatalog, "page_catalog.csv"),
    ReportViewsRaw  = LoadOne(Fabric_TableReportViews, Blob_PathReportViews, "report_views.csv"),
    UserViewsRaw    = LoadOne(Fabric_TableUserViews,   Blob_PathUserViews,   "user_views.csv"),

    // ---------------- Typing ----------------------------
    PageViews = Table.TransformColumnTypes(PageViewsRaw, {
        {"workspace_id",        type text},
        {"workspace_name",      type text},
        {"capacity_name",       type text},
        {"report_id",           type text},
        {"report_name",         type text},
        {"report_total_pages",  Int64.Type},
        {"page_id",             type text},
        {"page_name",           type text},
        {"page_ordinal",        Int64.Type},
        {"view_date",           type date},
        {"view_count",          Int64.Type},
        {"unique_users",        Int64.Type},
        {"avg_dwell_seconds",   type number},
        {"top_persona",         type text}
    }),

    PageCatalog = Table.TransformColumnTypes(PageCatalogRaw, {
        {"workspace_id",       type text},
        {"workspace_name",     type text},
        {"report_id",          type text},
        {"report_name",        type text},
        {"page_id",            type text},
        {"page_name",          type text},
        {"page_ordinal",       Int64.Type},
        {"catalog_pulled_at",  type datetimezone}
    }),

    ReportViews = Table.TransformColumnTypes(ReportViewsRaw, {
        {"workspace_id",         type text},
        {"workspace_name",       type text},
        {"capacity_name",        type text},
        {"report_id",            type text},
        {"report_name",          type text},
        {"view_date",            type date},
        {"view_count",           Int64.Type},
        {"unique_users",         Int64.Type},
        {"avg_session_seconds",  type number}
    }),

    UserViews = Table.TransformColumnTypes(UserViewsRaw, {
        {"workspace_id",            type text},
        {"workspace_name",          type text},
        {"report_id",               type text},
        {"report_name",             type text},
        {"user_id_hash",            type text},
        {"view_date",               type date},
        {"view_count",              Int64.Type},
        {"distinct_pages_viewed",   Int64.Type}
    }),

    // Return all four as a record. In Power BI Desktop after pasting,
    // expand the record so each becomes its own query that loads to
    // the model. Recommended relationships are in the README.
    Output = [
        page_views    = PageViews,
        page_catalog  = PageCatalog,
        report_views  = ReportViews,
        user_views    = UserViews
    ]
in
    Output
