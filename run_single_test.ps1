param(
    [string]$Asin = "B0CZ767JDG"
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv/Scripts/python.exe"

$env:TEST_ASIN = $Asin

Write-Host "Running parser for ASIN: $Asin"
& $python "$root/parser_not_test.py"
