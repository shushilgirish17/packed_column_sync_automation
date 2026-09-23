<#
env.ps1
-------
Load the project's .env into a hashtable for PowerShell scripts, mirroring
lib/config.py. Dot-source it:

    . "$PSScriptRoot\..\lib\env.ps1"
    $cfg = Get-ProjectConfig
    $cfg.WinAppDriverUrl   # 'http://127.0.0.1:4723'

Precedence: real environment variable > .env file > default.
#>

function Get-ProjectConfig {
    $root = Split-Path $PSScriptRoot -Parent
    $envFile = Join-Path $root '.env'

    $fileValues = @{}
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            $t = $line.Trim()
            if (-not $t -or $t.StartsWith('#') -or ($t -notmatch '=')) { continue }
            $k, $v = $t -split '=', 2
            $fileValues[$k.Trim()] = $v.Trim().Trim('"').Trim("'")
        }
    }

    function Resolve-Val($key, $default) {
        $env = [Environment]::GetEnvironmentVariable($key)
        if ($env) { return $env }
        if ($fileValues.ContainsKey($key) -and $fileValues[$key]) { return $fileValues[$key] }
        return $default
    }

    $exports = Resolve-Val 'EXPORTS_DIR' (Join-Path $root 'exports')
    $watch   = Resolve-Val 'WATCH_DIR'   (Join-Path $root 'watch')

    return [pscustomobject]@{
        WinAppDriverUrl   = Resolve-Val 'WINAPPDRIVER_URL'    'http://127.0.0.1:4723'
        UnicornProcess    = Resolve-Val 'UNICORN_PROCESS'     'UNICORNClient'
        ColumnDialogTitle = Resolve-Val 'COLUMN_DIALOG_TITLE' 'Column Handling'
        ImportAccessScope = Resolve-Val 'IMPORT_ACCESS_SCOPE' 'Personal'
        ExportsDir        = $exports
        WatchDir          = $watch
        # Non-secret launch settings (safe to print).
        UnicornExe        = Resolve-Val 'UNICORN_EXE'    ''
        UnicornUser       = Resolve-Val 'UNICORN_USER'   ''
        UnicornModule     = Resolve-Val 'UNICORN_MODULE' 'Evaluation'
        WinAppDriverExe   = Resolve-Val 'WINAPPDRIVER_EXE' ''
    }
}

function Get-EnvValue {
    <#
      Read a single key from the real environment (first) or the .env file.
      Used for SECRETS (e.g. UNICORN_PASSWORD) that must NOT live on the
      printable Get-ProjectConfig object, so they can't leak into a log dump.
    #>
    param([Parameter(Mandatory)][string]$Key, [string]$Default = '')
    $v = [Environment]::GetEnvironmentVariable($Key)
    if ($v) { return $v }
    $envFile = Join-Path (Split-Path $PSScriptRoot -Parent) '.env'
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            $t = $line.Trim()
            if (-not $t -or $t.StartsWith('#') -or ($t -notmatch '=')) { continue }
            $k, $val = $t -split '=', 2
            if ($k.Trim() -eq $Key) { return $val.Trim().Trim('"').Trim("'") }
        }
    }
    return $Default
}
