# Deploy locally (cron / systemd / Task Scheduler)

For small tenants, prototypes, or before you've got a Fabric / Azure
subscription provisioned. The collector runs on whatever box you put it on
and writes the silver CSV to local disk (or a mounted file share).

## What's in this folder

| File | What it is |
| --- | --- |
| `run-collector.ps1` | PowerShell wrapper — Windows Task Scheduler friendly. Pulls secrets from env or `Microsoft.PowerShell.SecretManagement`. Logs to a timestamped file. |
| `run-collector.sh` | Bash wrapper — cron / systemd friendly. Reads env vars, logs to a timestamped file. |
| `powerbi-page-telemetry.service` | systemd unit — runs `run-collector.sh` once. |
| `powerbi-page-telemetry.timer` | systemd timer — daily 06:00 UTC. |
| `env.example` | Template `EnvironmentFile` for the systemd service. |

## Windows — Task Scheduler

```powershell
# 1. Store secrets in the user's SecretManagement vault (one-time)
Install-Module Microsoft.PowerShell.SecretManagement, Microsoft.PowerShell.SecretStore -Scope CurrentUser
Register-SecretVault -Name pbi -ModuleName Microsoft.PowerShell.SecretStore -DefaultVault
Set-Secret -Name pbi-tenant-id     -Secret (Read-Host -AsSecureString)
Set-Secret -Name pbi-client-id     -Secret (Read-Host -AsSecureString)
Set-Secret -Name pbi-client-secret -Secret (Read-Host -AsSecureString)

# 2. Test the wrapper interactively
cd C:\path\to\powerbi-page-telemetry\deploy\local
./run-collector.ps1

# 3. Register a daily task at 06:00 local time
$action  = New-ScheduledTaskAction -Execute "pwsh.exe" `
              -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PWD\run-collector.ps1`""
$trigger = New-ScheduledTaskTrigger -Daily -At 6am
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName "PowerBI Page Telemetry" -Action $action -Trigger $trigger -Principal $principal
```

To unregister:

```powershell
Unregister-ScheduledTask -TaskName "PowerBI Page Telemetry" -Confirm:$false
```

## Linux — systemd timer

```bash
# 1. Create the runtime user + dirs
sudo useradd --system --no-create-home --shell /usr/sbin/nologin pbi-telemetry
sudo install -d -o pbi-telemetry -g pbi-telemetry /var/lib/powerbi-page-telemetry
sudo install -d -m 750 -o root -g pbi-telemetry /etc/powerbi-page-telemetry

# 2. Clone & install
sudo git clone https://github.com/anuraagr/powerbi-page-telemetry.git \
     /opt/powerbi-page-telemetry
sudo chown -R pbi-telemetry:pbi-telemetry /opt/powerbi-page-telemetry
sudo -u pbi-telemetry python3 -m pip install --user -r /opt/powerbi-page-telemetry/etl/requirements.txt

# 3. Write the secrets file
sudo cp /opt/powerbi-page-telemetry/deploy/local/env.example \
        /etc/powerbi-page-telemetry/env
sudo chmod 600 /etc/powerbi-page-telemetry/env
sudo $EDITOR /etc/powerbi-page-telemetry/env   # fill in real values

# 4. Install the service + timer units
sudo install -m 644 /opt/powerbi-page-telemetry/deploy/local/powerbi-page-telemetry.service \
                    /etc/systemd/system/
sudo install -m 644 /opt/powerbi-page-telemetry/deploy/local/powerbi-page-telemetry.timer \
                    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now powerbi-page-telemetry.timer

# 5. Verify
systemctl list-timers powerbi-page-telemetry.timer
systemctl status powerbi-page-telemetry.service
sudo journalctl -u powerbi-page-telemetry.service -n 50
```

To run once on demand:

```bash
sudo systemctl start powerbi-page-telemetry.service
```

## Linux / macOS — plain cron

```bash
# 1. Install deps
pip install --user -r /path/to/powerbi-page-telemetry/etl/requirements.txt

# 2. Add to crontab (`crontab -e`):
0 6 * * *  PBI_TENANT_ID=xxx PBI_CLIENT_ID=xxx PBI_CLIENT_SECRET=xxx /path/to/powerbi-page-telemetry/deploy/local/run-collector.sh
```

For real deployments, store the env vars in a `~/.config/powerbi-page-telemetry/env`
file with `chmod 600` and source it inside the wrapper instead of putting
secrets in the crontab.

## Output

Both wrappers write:

| Artifact | Path |
| --- | --- |
| Bronze per-report CSVs | `$PBI_OUTPUT_DIR/bronze/*.csv` |
| Silver conformed fact | `$PBI_OUTPUT_DIR/silver/page_views.csv` |
| Run summary | `$PBI_OUTPUT_DIR/_run_summary.json` |
| Stdout/stderr log | `$LOG_DIR/collector-YYYYMMDD-HHMMSS.log` |

Surface the silver CSV in Power BI Desktop or a Lakehouse Shortcut and you
get the same dashboard as `dashboard/PageUsageDashboard.html` — just
pointed at live data.

## When to graduate to Fabric or Azure Function

- **You need durable, monitored, retried scheduling**: move to Azure
  Function (see `../azure-function/`).
- **You're already in Fabric**: move to a Fabric notebook (see
  `../fabric-notebook/`). DirectLake + Spark MERGE is the cleanest path.
