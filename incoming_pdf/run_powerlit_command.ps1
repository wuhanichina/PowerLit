param(
    [Parameter(Mandatory = $true)]
    [string]$CommandName,

    [switch]$AddDefaultLimit100,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ForwardArgs
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir

[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH = "$repoRoot\src;$repoRoot\.venv\Lib\site-packages"

function Decode-Text {
    param([string]$Text)
    return ConvertFrom-Json ('"' + $Text.Replace('"', '\"') + '"')
}

function Has-LimitArgument {
    param([string[]]$ArgsToCheck)
    foreach ($arg in $ArgsToCheck) {
        if ($arg -eq '--limit' -or $arg.StartsWith('--limit=')) {
            return $true
        }
    }
    return $false
}

$msgResultFail = Decode-Text '\u7ed3\u679c\uff1a\u5931\u8d25'
$msgMissingPython = Decode-Text '\u672a\u627e\u5230 Python \u542f\u52a8\u5668\uff08py\uff09\u3002\u8bf7\u5148\u5b89\u88c5\u5e76\u914d\u7f6e Python 3.12\u3002'
$msgStart = Decode-Text '\u5f00\u59cb\uff1a'
$msgRepo = Decode-Text '\u4ed3\u5e93\uff1a'
$msgCommand = Decode-Text '\u547d\u4ee4\uff1a'
$msgResultSuccess = Decode-Text '\u7ed3\u679c\uff1a\u6210\u529f'
$msgResultFailPrefix = Decode-Text '\u7ed3\u679c\uff1a\u5931\u8d25\uff08\u9000\u51fa\u7801 '
$msgResultFailSuffix = Decode-Text '\uff09'
$msgPressEnter = Decode-Text '\u6309\u56de\u8f66\u952e\u7ee7\u7eed'

$effectiveArgs = @($ForwardArgs)
if ($AddDefaultLimit100 -and -not (Has-LimitArgument -ArgsToCheck $effectiveArgs)) {
    $effectiveArgs = @('--limit', '100') + $effectiveArgs
}

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if (-not $pyLauncher) {
    Write-Host ("[PowerLit] $msgResultFail")
    Write-Host ("[PowerLit] $msgMissingPython")
    exit 1
}

$commandLine = if ($effectiveArgs.Count -gt 0) {
    "$CommandName $($effectiveArgs -join ' ')"
} else {
    $CommandName
}

Write-Host ("[PowerLit] $msgStart$(Get-Date -Format 'yyyy/MM/dd HH:mm:ss.fff')")
Write-Host ("[PowerLit] $msgRepo$repoRoot")
Write-Host ("[PowerLit] $msgCommand$commandLine")
Write-Host ''

Push-Location $repoRoot
try {
    & py -3.12 -m powerlit.cli $CommandName @effectiveArgs
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

Write-Host ''
if ($exitCode -eq 0) {
    Write-Host ("[PowerLit] $msgResultSuccess")
}
else {
    Write-Host ("[PowerLit] $msgResultFailPrefix$exitCode$msgResultFailSuffix")
}

if ($env:POWERLIT_NO_PAUSE -ne '1') {
    Write-Host ''
    [void](Read-Host $msgPressEnter)
}

exit $exitCode
