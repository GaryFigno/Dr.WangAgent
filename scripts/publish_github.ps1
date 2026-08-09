# Create/push GaryFigno/Dr.WangAgent and set repo page metadata.
# Prerequisites: GitHub CLI installed + `gh auth login` done.
$ErrorActionPreference = "Stop"
$Repo = "GaryFigno/Dr.WangAgent"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
  $candidates = @(
    "$env:ProgramFiles\GitHub CLI\gh.exe",
    "$env:LocalAppData\Programs\GitHub CLI\gh.exe"
  )
  foreach ($c in $candidates) {
    if (Test-Path $c) { $gh = $c; break }
  }
}
if (-not $gh) {
  throw "gh not found. Install with: winget install --id GitHub.cli -e"
}

& $gh auth status | Out-Host

$exists = $true
try {
  & $gh repo view $Repo 1>$null 2>$null
} catch {
  $exists = $false
}
if ($LASTEXITCODE -ne 0) { $exists = $false }

if (-not $exists) {
  Write-Host "Creating public repo $Repo ..."
  & $gh repo create $Repo --public --description "Dr.Wang Agent — local desktop coding agent with Agent / Codex / Claude panels" --disable-wiki --confirm
}

git remote set-url origin "git@github.com:$Repo.git"
git push -u origin main

& $gh repo edit $Repo `
  --description "Dr.Wang Agent — local desktop coding agent for any OpenAI-compatible API; Agent / Codex / Claude panels" `
  --homepage "https://github.com/$Repo" `
  --add-topic "ai-agent" `
  --add-topic "desktop" `
  --add-topic "codex" `
  --add-topic "claude-code" `
  --add-topic "openai-compatible" `
  --add-topic "python" `
  --add-topic "windows"

Write-Host "Done: https://github.com/$Repo"
