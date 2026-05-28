# Security policy

## Supported versions

This is a reference implementation maintained on a best-effort basis. The
latest release on `main` is the only supported version. Older release tags
remain available for reproducibility but receive no security updates.

## Reporting a vulnerability

**Do not open public GitHub issues for security reports.**

Email the maintainer (see the repository owner's GitHub profile for
contact details) with:

- A description of the vulnerability
- Steps to reproduce, or a minimal proof-of-concept
- The collector commit SHA or release tag where you observed it

We aim to acknowledge new reports within five business days and to ship
a fix or mitigation within thirty business days for high-severity issues.

## Scope

In scope:

- The collector (`etl/collector.py`) and any deploy wrapper under
  `deploy/`
- Documented APIs, deployment patterns, and sample queries
- The bundled dashboard (`dashboard/`) and aggregator
  (`etl/aggregate_for_dashboard.py`)

Out of scope:

- Vulnerabilities in upstream Microsoft services (report those to
  [MSRC](https://msrc.microsoft.com/))
- Issues that require a tenant to grant the collector permissions it
  doesn't need (this is a deployment misconfiguration, not a code bug)
- Third-party libraries — report those to their respective maintainers

## Hardening recommendations

If you deploy this in production, please:

1. Always pin `COLLECTOR_REF` (Fabric notebook) or a release tag to
   prevent supply-chain rolls from `main`.
2. Use Managed Identity (Azure Function path) or a workspace identity
   (Fabric path) for secret access — never embed secrets in code or app
   settings.
3. Apply RLS on the gold semantic model before exposing it to
   business users. The silver layer's `user` field is a UPN — see
   [`docs/pii-and-retention.md`](docs/pii-and-retention.md).
4. Set a retention policy on the bronze partitions. The default is
   "keep forever"; most tenants do not need page-attributable telemetry
   older than 90 days.
