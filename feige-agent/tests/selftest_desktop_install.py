"""Exercise the real Windows installer with isolated packages and shortcuts."""
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(script):
    return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                           "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); " + script],
                          check=True, capture_output=True, text=True, encoding="utf-8").stdout


def quote(path):
    return "'" + str(path).replace("'", "''") + "'"


def main():
    with tempfile.TemporaryDirectory(prefix="feige-install-check-") as temp:
        base = Path(temp)
        package, installed = base / "发行 包", base / "固定 安装目录"
        icon = package / "_internal" / "app" / "assets" / "app-icon.ico"
        icon.parent.mkdir(parents=True)
        icon.write_bytes((ROOT / "app/assets/app-icon.ico").read_bytes())
        (package / "FeigeCustomerService.exe").write_bytes(b"first build")
        for name in ("data", "knowledge"):
            (package / name).mkdir()
            (package / name / "merchant.txt").write_text("package default")
        arguments = (f"& {quote(ROOT / 'install_desktop.ps1')} -PackageRoot {quote(package)} "
                     f"-InstallRoot {quote(installed)} -DesktopDirectory {quote(base / 'desktop')} "
                     f"-StartMenuDirectory {quote(base / 'menu')}")
        run(arguments)
        for name in ("data", "knowledge"):
            (installed / name / "merchant.txt").write_text("merchant current")
            (installed / name / "added.txt").write_text("merchant history")
        (package / "FeigeCustomerService.exe").write_bytes(b"updated build")
        run(arguments)
        assert (installed / "FeigeCustomerService.exe").read_bytes() == b"updated build"
        for name in ("data", "knowledge"):
            assert (installed / name / "merchant.txt").read_text() == "merchant current"
            assert (installed / name / "added.txt").read_text() == "merchant history"
        metadata = run("[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
                       "$shell=New-Object -ComObject WScript.Shell; "
                       f"$link=$shell.CreateShortcut({quote(base / 'desktop' / '抖店智能客服社区版.lnk')}); "
                       "@{target=$link.TargetPath;cwd=$link.WorkingDirectory;icon=$link.IconLocation} | ConvertTo-Json -Compress")
        link = json.loads(metadata)
        assert Path(link["target"]) == installed / "FeigeCustomerService.exe"
        assert Path(link["cwd"]) == installed
        assert link["icon"] == str(installed / "_internal/app/assets/app-icon.ico") + ",0"
        assert (base / "menu" / "抖店智能客服社区版.lnk").is_file()
        print("PASS: fresh install, update, merchant data preservation, fixed shortcut target, start menu, paths with Chinese/spaces")


if __name__ == "__main__":
    main()
