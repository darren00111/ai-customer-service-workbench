param(
    [string]$DistributionRoot = "dist",
    [switch]$SkipDependencyInstall,
    [switch]$ConsoleDiagnostics
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$SkillRoot = Join-Path (Split-Path -Parent $Root) "feige-fashion-customer-service"






















$DistributionPath = Join-Path $Root $DistributionRoot
$Output = Join-Path $DistributionPath "FeigeCustomerService"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "缺少 .venv，请先创建虚拟环境并安装 requirements.txt。"
}

if (-not $SkipDependencyInstall) {
    & $Python -m pip install "pyinstaller>=6.0"
    if ($LASTEXITCODE -ne 0) { throw "打包依赖安装失败。" }
}
$WindowMode = if ($ConsoleDiagnostics) { '--console' } else { '--windowed' }
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    $WindowMode `
    --onedir `
    --name FeigeCustomerService `
    --icon "app\assets\app-icon.ico" `
    --version-file "app\assets\windows-version.txt" `
    --distpath $DistributionPath `
    --paths (Join-Path $SkillRoot "scripts") `
    --hidden-import classify `
    --hidden-import classify_standard `
    --hidden-import kb `
    --hidden-import merchant `
    --collect-all webview `
    --add-data "app\ui;app\ui" `
    --add-data "app\assets;app\assets" `
    --add-data "app\service\fixtures.json;app\service" `
    desktop_main.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 打包失败（退出码 $LASTEXITCODE）。请先关闭正在运行的旧版桌面程序后重试。"
}

# The bundled Python runtime may load an extra public CA file from ssl.py.
# Preserve that runtime trust configuration in the frozen layout; never disable TLS verification.
$RuntimeCert = & $Python -c "from pathlib import Path; import ssl; print(Path(ssl.__file__).with_name('codex-rejected-cas.pem'))"
if (Test-Path -LiteralPath $RuntimeCert -PathType Leaf) {
    Copy-Item -LiteralPath $RuntimeCert -Destination (Join-Path $Output '_internal\codex-rejected-cas.pem') -Force
}

New-Item -ItemType Directory -Path (Join-Path $Output "data") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $Output "knowledge") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $Output "knowledge\stores") -Force | Out-Null
# Ship an empty registry. Local customer profiles, credentials, chat logs and the
# development sample knowledge base must never be copied into a distributable.
[System.IO.File]::WriteAllText(
    (Join-Path $Output "data\stores.json"),
    '{"version":1,"stores":[]}',
    (New-Object System.Text.UTF8Encoding($false))
)
Copy-Item -LiteralPath (Join-Path $Root "docs\桌面版使用说明.txt") `
    -Destination (Join-Path $Output "使用说明.txt") -Force
Copy-Item -LiteralPath (Join-Path $Root "docs\标准产品接入说明.md") `
    -Destination (Join-Path $Output "标准产品接入说明.md") -Force
Copy-Item -LiteralPath (Join-Path $Root "templates\merchant.yaml") `
    -Destination (Join-Path $Output "knowledge\merchant-template.yaml") -Force
Copy-Item -LiteralPath (Join-Path $Root "docs\接入与试聊使用说明.md") `
    -Destination (Join-Path $Output "接入与试聊使用说明.md") -Force
Copy-Item -LiteralPath (Join-Path $Root "docs\持续学习与成交优化说明.md") `
    -Destination (Join-Path $Output "持续学习与成交优化说明.md") -Force
Copy-Item -LiteralPath (Join-Path $Root "docs\多平台接入与淘宝联调.md") `
    -Destination (Join-Path $Output "多平台接入与淘宝联调.md") -Force
Copy-Item -LiteralPath (Join-Path $Root "docs\GitHub调研与连接联调.md") `
    -Destination (Join-Path $Output "GitHub调研与连接联调.md") -Force
Copy-Item -LiteralPath (Join-Path $Root "docs\持续接待开发说明.md") `
    -Destination (Join-Path $Output "持续接待开发说明.md") -Force
Copy-Item -LiteralPath (Join-Path $Root "docs\P0知识与改判闭环说明.md") `
    -Destination (Join-Path $Output "知识补全与人工改判说明.md") -Force
Copy-Item -LiteralPath (Join-Path $Root "docs\多平台店铺工作台使用说明.md") `
    -Destination (Join-Path $Output "多平台店铺工作台使用说明.md") -Force
Copy-Item -LiteralPath (Join-Path $Root "docs\无店铺体验与验收说明.md") `
    -Destination (Join-Path $Output "无店铺体验与验收说明.md") -Force
Copy-Item -LiteralPath (Join-Path $Root "docs\先看这里-无店铺体验.txt") `
    -Destination (Join-Path $Output "先看这里-无店铺体验.txt") -Force
Copy-Item -LiteralPath (Join-Path $Root "docs\简化纠错说明.md") `
    -Destination (Join-Path $Output "简化纠错说明.md") -Force
[System.IO.File]::WriteAllText((Join-Path $Output "开始试聊与改正.cmd"),
    "@echo off`r`nstart `"`" `"%~dp0FeigeCustomerService.exe`" --lab`r`n",
    (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllText((Join-Path $Output "开始无店铺体验.cmd"),
    "@echo off`r`nstart `"`" `"%~dp0FeigeCustomerService.exe`" --experience`r`n",
    (New-Object System.Text.UTF8Encoding($false)))

# Windows PowerShell 5.1 requires a BOM for Chinese script literals.
[System.IO.File]::WriteAllText((Join-Path $Output "install_desktop.ps1"),
    [System.IO.File]::ReadAllText((Join-Path $Root "install_desktop.ps1")),
    (New-Object System.Text.UTF8Encoding($true)))
Copy-Item -LiteralPath (Join-Path $Root "install_desktop.cmd") `
    -Destination (Join-Path $Output "安装到桌面.cmd") -Force

Write-Host "桌面程序已生成：$Output\FeigeCustomerService.exe"
