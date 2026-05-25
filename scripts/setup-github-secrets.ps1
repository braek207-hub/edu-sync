# Требует: gh auth login
# Заполняет секреты edu-sync из локального файла .env.sync (НЕ коммитить!)
#
# Пример .env.sync:
#   DATABASE_URL=postgresql://postgres:...@db.xxx.supabase.co:5432/postgres
#   GCP_SA_KEY={"type":"service_account",...}   # или путь: @sa.json
#   SHEET_ID_EDU=1abc...
#   DIRECT_TOKEN_EDU=...
#   DIRECT_CLIENTS_JSON_EDU=[{"login":"...","goal_ids":[]}]

param(
    [string]$Repo = "braek207-hub/edu-sync"
)

$envFile = Join-Path $PSScriptRoot ".." ".env.sync"
if (-not (Test-Path $envFile)) {
    Write-Host "Создайте $envFile по образцу docs/SECRETS.md"
    Write-Host "Либо скопируйте 4 секрета из BJ_auto_metrica вручную в GitHub UI."
    exit 1
}

gh auth status 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Сначала: gh auth login"
    exit 1
}

Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -notmatch '^\s*(\w+)=(.*)$') { return }
    $name = $Matches[1]
    $value = $Matches[2].Trim()
    if ($value.StartsWith("@")) {
        $path = $value.Substring(1)
        $value = Get-Content $path -Raw
    }
    Write-Host "Setting $name ..."
    $value | gh secret set $name --repo $Repo
}

Write-Host "Done. Run workflow: gh workflow run sync.yml --repo $Repo"
