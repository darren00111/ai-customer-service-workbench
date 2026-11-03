"""Start the installed Qianniu client without loading UI Automation or credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess


class QianniuClientNotFound(ValueError):
    pass


def settings_path():
    from ..config import DATA_DIR
    return DATA_DIR / 'qianniu-client.json'


def executable(value):
    """Accept an install directory or DisplayIcon, never arbitrary command arguments."""
    raw = os.path.expandvars(str(value).strip())
    match = re.fullmatch(r'"([^"]+)"(?:\s*,\s*-?\d+)?', raw)
    if match:
        raw = match[1]
    else:
        raw = re.sub(r',\s*-?\d+$', '', raw)
    path = Path(raw)
    if path.suffix.lower() != '.exe':
        path = path / 'AliWorkbench.exe'
    if path.is_absolute() and path.name.lower() == 'aliworkbench.exe' and path.is_file():
        return path.resolve()
    return None


def registry_candidates():
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\AliWorkbench.exe',
                                    0, winreg.KEY_READ | view) as key:
                    yield winreg.QueryValueEx(key, '')[0]
            except OSError:
                pass
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                                    0, winreg.KEY_READ | view) as root:
                    for index in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                                name = str(winreg.QueryValueEx(key, 'DisplayName')[0]).lower()
                                if not any(word in name for word in ('千牛', 'aliworkbench', 'qianniu')):
                                    continue
                                for field in ('InstallLocation', 'DisplayIcon'):
                                    try:
                                        yield winreg.QueryValueEx(key, field)[0]
                                    except OSError:
                                        pass
                        except OSError:
                            continue
            except OSError:
                pass


def discover(config=None):
    config = Path(config) if config is not None else settings_path()
    try:
        saved = json.loads(config.read_text(encoding='utf-8')).get('executable', '')
        target = executable(saved)
        if target:
            return target
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    for candidate in registry_candidates():
        target = executable(candidate)
        if target:
            return target
    for variable in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        base = os.environ.get(variable)
        if not base:
            continue
        for folder in ('AliWorkbench', 'Alibaba/AliWorkbench', 'Alibaba/Qianniu', '千牛'):
            target = executable(Path(base) / folder)
            if target:
                return target
    return None


def save_client(value, config=None):
    target = executable(value)
    if not target:
        raise ValueError('请选择千牛安装目录中的 AliWorkbench.exe，请勿选择卸载程序或浏览器')
    config = Path(config) if config is not None else settings_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    temp = config.with_suffix('.tmp')
    temp.write_text(json.dumps({'executable': str(target)}, ensure_ascii=False), encoding='utf-8')
    temp.replace(config)
    return target


def choose_client(window):
    if window is None:
        raise ValueError('请从桌面工具中选择千牛安装位置')
    import webview
    files = window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
                                     file_types=('千牛客户端 (*.exe)',))
    if not files:
        return {'ok': False, 'cancelled': True, 'error': '已取消选择千牛程序'}
    save_client(files[0])
    return {'ok': True, 'hint': '已记住千牛安装位置，请打开千牛并自行登录'}


def launch_client():
    target = discover()
    if target is None:
        raise QianniuClientNotFound('未找到千牛客户端。请选择已安装千牛的 AliWorkbench.exe；尚未安装时，请先安装官方千牛桌面客户端。')
    # Let Qianniu handle its own running instance and login state. No browser login,
    # UIA initialization, customer message access, or credentials are needed here.
    subprocess.Popen([str(target)], cwd=str(target.parent), close_fds=True)
    return {'state': 'login_required', 'detail': '已请求打开千牛，请自行登录并进入商家工作台／接待中心；尚未核对登录状态'}
