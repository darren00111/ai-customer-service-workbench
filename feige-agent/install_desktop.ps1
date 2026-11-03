param(
    [string]$PackageRoot = $PSScriptRoot,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\FeigeCustomerServiceCommunity'),
    [string]$DesktopDirectory = [Environment]::GetFolderPath('Desktop'),
    [string]$StartMenuDirectory = [Environment]::GetFolderPath('Programs'),
    [switch]$Launch
)
$ErrorActionPreference = 'Stop'
$PackageRoot = [IO.Path]::GetFullPath($PackageRoot)
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$SourceExe = Join-Path $PackageRoot 'FeigeCustomerService.exe'
$TargetExe = Join-Path $InstallRoot 'FeigeCustomerService.exe'
if (-not (Test-Path -LiteralPath $SourceExe -PathType Leaf)) {
    throw '请先解压完整发行包，再运行安装程序。'
}
if (-not (Test-Path -LiteralPath (Join-Path $PackageRoot '_internal\app\assets\app-icon.ico'))) {
    throw '发行包不完整，请重新解压。'
}
if ($PackageRoot.TrimEnd('\') -ne $InstallRoot.TrimEnd('\')) {
    $Running = Get-Process -Name 'FeigeCustomerService' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $TargetExe }
    if ($Running) { throw '请先关闭正在运行的抖店智能客服社区版，再安装更新。' }
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    foreach ($Item in Get-ChildItem -LiteralPath $PackageRoot -Force) {
        # These directories belong to the merchant, never to an update package.
        if ($Item.Name -in @('data', 'knowledge')) {
            if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot $Item.Name))) {
                Copy-Item -LiteralPath $Item.FullName -Destination $InstallRoot -Recurse
            }
        } else {
            Copy-Item -LiteralPath $Item.FullName -Destination $InstallRoot -Recurse -Force
        }
    }
}
$Shell = New-Object -ComObject WScript.Shell
foreach ($Directory in @($DesktopDirectory, $StartMenuDirectory)) {
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $Shortcut = $Shell.CreateShortcut((Join-Path $Directory '抖店智能客服社区版.lnk'))
    $Shortcut.TargetPath = $TargetExe
    $Shortcut.WorkingDirectory = $InstallRoot
    $Shortcut.IconLocation = (Join-Path $InstallRoot '_internal\app\assets\app-icon.ico') + ',0'
    $Shortcut.Description = '抖店智能客服社区版 · 智能接待与持续学习'
    $Shortcut.WindowStyle = 1
    $Shortcut.Save()
}
Write-Output ('安装完成：' + $TargetExe)
Write-Output '桌面和开始菜单已添加“抖店智能客服社区版”，双击即可打开。'
if ($Launch) { Start-Process -WindowStyle Hidden -FilePath $TargetExe -WorkingDirectory $InstallRoot }
