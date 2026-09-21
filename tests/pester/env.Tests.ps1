<#
Pester tests for lib/env.ps1 (the PowerShell side of config).

Run:  Invoke-Pester tests\pester
#>

BeforeAll {
    . "$PSScriptRoot\..\..\lib\env.ps1"
}

Describe 'Get-ProjectConfig' {

    It 'returns a config object with the expected properties' {
        $c = Get-ProjectConfig
        $c.WinAppDriverUrl   | Should -Match '^https?://'
        $c.UnicornProcess    | Should -Not -BeNullOrEmpty
        $c.ColumnDialogTitle | Should -Not -BeNullOrEmpty
        $c.ExportsDir        | Should -Not -BeNullOrEmpty
        $c.WatchDir          | Should -Not -BeNullOrEmpty
    }

    It 'lets a real environment variable override the default' {
        $env:UNICORN_PROCESS = 'PesterTestProc'
        try {
            (Get-ProjectConfig).UnicornProcess | Should -Be 'PesterTestProc'
        }
        finally {
            Remove-Item Env:\UNICORN_PROCESS -ErrorAction SilentlyContinue
        }
    }

    It 'falls back to the default when unset' {
        Remove-Item Env:\WINAPPDRIVER_URL -ErrorAction SilentlyContinue
        # (assumes .env leaves it blank; the default is the loopback endpoint)
        (Get-ProjectConfig).WinAppDriverUrl | Should -Match '127\.0\.0\.1:4723'
    }
}
