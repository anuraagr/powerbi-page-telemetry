// PageTelemetry.Connect.pq
//
// Power Query M script. Paste into Power BI Desktop:
//   Home -> Get Data -> Blank Query -> Advanced Editor -> paste -> Done
//
// Then in the Power BI ribbon: Home -> Refresh.
//
// Parameters at the top — edit before pasting. The three connection
// modes below cover the three deploy options.

let
    // ---------------- 1. Connection mode ----------------
    // "fabric"        -> read from a Fabric Lakehouse Delta table
    // "blob"          -> read from the silver CSV in Azure Blob Storage
    //                    (the Azure Function deploy option writes here)
    // "local"         -> read from a local file on disk
    //
    ConnectionMode = "fabric",

    // ---------------- 2. Per-mode settings --------------
    Fabric_WorkspaceName   = "Analytics-Ops",
    Fabric_LakehouseName   = "PageTelemetry_LH",
    Fabric_TableName       = "page_views_silver",

    Blob_AccountUrl        = "https://<storage-account>.blob.core.windows.net",
    Blob_Container         = "page-telemetry",
    Blob_FilePath          = "silver/page_views.csv",

    Local_CsvPath          = "C:\PowerBI-PageTelemetry\out\silver\page_views.csv",

    // ---------------- 3. Schema-version pin -------------
    // Must match SILVER_SCHEMA_VERSION in etl/collector.py.
    ExpectedSchemaVersion  = "1.0.0",

    // ---------------- Implementation --------------------
    Source =
        if ConnectionMode = "fabric" then
            let
                Lakehouse = Fabric.Warehouse([CreateNavigationProperties = false]),
                Workspace = Lakehouse{[Name = Fabric_WorkspaceName]}[Data],
                LH        = Workspace{[Name = Fabric_LakehouseName]}[Data],
                Table     = LH{[Schema = "dbo", Item = Fabric_TableName]}[Data]
            in
                Table

        else if ConnectionMode = "blob" then
            let
                Blob   = AzureStorage.Blobs(Blob_AccountUrl),
                Cont   = Blob{[Name = Blob_Container]}[Data],
                File   = Cont{[Name = Blob_FilePath]}[Content],
                Csv    = Csv.Document(
                            File,
                            [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]
                        ),
                // Drop the first row if it's the schema-version comment.
                Body   = if Text.StartsWith(Csv{0}[Column1], "#") then
                            Table.Skip(Csv, 1)
                         else
                            Csv,
                Promoted = Table.PromoteHeaders(Body, [PromoteAllScalars = true])
            in
                Promoted

        else if ConnectionMode = "local" then
            let
                Csv    = Csv.Document(
                            File.Contents(Local_CsvPath),
                            [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]
                        ),
                Body   = if Text.StartsWith(Csv{0}[Column1], "#") then
                            Table.Skip(Csv, 1)
                         else
                            Csv,
                Promoted = Table.PromoteHeaders(Body, [PromoteAllScalars = true])
            in
                Promoted

        else
            error "ConnectionMode must be one of: fabric, blob, local",

    // ---------------- Typing ----------------------------
    Typed = Table.TransformColumnTypes(
        Source,
        {
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
        }
    )
in
    Typed
