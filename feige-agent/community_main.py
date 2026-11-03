"""Entry point for the original desktop application with community limits."""
import json
from pathlib import Path
import sys


if __name__ == '__main__':
    if '--self-test-ui' in sys.argv:
        from app.community_gui import run_native_check
        report = run_native_check()
        if '--self-test-report' in sys.argv:
            Path(sys.argv[sys.argv.index('--self-test-report')+1]).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        raise SystemExit(0 if report['ok'] else 1)
    if '--self-test-offline' in sys.argv:
        try:
            from app.community_checks import run_checks
            report = run_checks()
        except Exception as exc:
            report = {'ok':False,'error':type(exc).__name__+': '+str(exc)}
        if '--self-test-report' in sys.argv:
            Path(sys.argv[sys.argv.index('--self-test-report')+1]).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        if sys.stdout:
            print(json.dumps(report,ensure_ascii=False,indent=2))
        raise SystemExit(0 if report['ok'] else 1)
    if '--lab' in sys.argv:
        from app.service.workbench_api import run_workbench
        run_workbench()
    elif '--experience' in sys.argv:
        import webview
        from app.config import DATA_DIR
        from app.service.experience import open_experience
        from app.branding import APP_ICON, configure_desktop_identity
        configure_desktop_identity()
        open_experience(DATA_DIR/'sandbox')
        webview.start(icon=str(APP_ICON))
    else:
        from app.desktop import run_desktop
        run_desktop()
