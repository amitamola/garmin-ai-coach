# run_bridge.ps1 - launch the Telegram coach bridge (persistent poller).
# Run by hand for testing, or from a scheduled task at logon. A localhost port
# lock inside the Python process guarantees only one instance ever polls.
#
# Paths are resolved RELATIVE to this script, so the repo can live anywhere.

$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot          # repo root (parent of scripts/)
$script = Join-Path $root "telegram_bridge.py"

# Prefer a local virtualenv if present, else fall back to python on PATH.
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8       = "1"

# --- Optional: pin the Copilot CLI model + reasoning effort (else CLI defaults). ---
# Usual place for these is .env, but you can also uncomment here:
# $env:AGBOT_MODEL            = "gpt-5.6-luna"   # any id from `/model`, or "auto"
# $env:AGBOT_REASONING_EFFORT = "max"           # none|minimal|low|medium|high|xhigh|max
# $env:AGBOT_LLM_TIMEOUT      = "600"            # raise for slow high-reasoning models

# Load .env (simple KEY=VALUE lines) if present, so tokens/keys are available.
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#=][^=]*)=(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), "Process")
        }
    }
}

# For the default "copilot" backend, resolve copilot.exe robustly (a scheduled
# task may have a lean PATH). Harmless if you use another backend.
if (($env:AGBOT_LLM -eq $null) -or ($env:AGBOT_LLM -eq "copilot")) {
    if (-not $env:COPILOT_EXE) {
        $copilot = (Get-Command copilot -ErrorAction SilentlyContinue).Source
        if ($copilot) { $env:COPILOT_EXE = $copilot }
    }
}

Write-Output "Starting coach bridge: $script"
& $py $script
