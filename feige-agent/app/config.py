# -*- coding: utf-8 -*-
"""Runtime configuration for one shop and for the multi-shop registry."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from .platforms import platform_info

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
    SKILL_DIR = BUNDLE_DIR / "skill"
    SCRIPTS_DIR = SKILL_DIR / "scripts"
    STORES_KB_DIR = APP_DIR / "knowledge" / "stores"
else:
    APP_DIR = Path(__file__).resolve().parent.parent
    SKILL_DIR = APP_DIR.parent / "feige-fashion-customer-service"
    SCRIPTS_DIR = SKILL_DIR / "scripts"
    STORES_KB_DIR = SKILL_DIR / "data" / "stores"
SHOP_DIR = STORES_KB_DIR / "shop_001"
DATA_DIR = APP_DIR / "data"
LOGS_DIR = DATA_DIR / "logs"
REGISTRY_PATH = DATA_DIR / "stores.json"
RECEPTION_DIR = DATA_DIR / "reception"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from merchant import read_manifest  # noqa: E402

DEFAULTS = {
    "cdp_port": 9222,
    "profile_dir": str(DATA_DIR / "profiles" / "shop_001"),
    "workspace_url": "https://im.jinritemai.com/pc_seller_v2/main/workspace",
    "chrome_path": "",
    "poll_interval": 3.0,
    "full_scan_interval": 15.0,
    "conversation_switch_wait": 0.7,
    "max_conversations_per_cycle": 12,
    "gap_min": 0.8,
    "gap_max": 2.2,
    "auto_send": True,
    "auto_send_after_sales": True,
    "pause_on_handoff": True,
    "quiet_after_hours": False,
    "max_replies_per_minute": 20,
}


class Config:
    """A shop-local config. Runtime overrides win over the optional JSON file."""

    def __init__(self, path: Path | None = None, overrides: dict | None = None,
                 base: dict | None = None):
        self.path = Path(path) if path else (DATA_DIR / "config.json")
        self.data = dict(DEFAULTS)
        if base:
            self.data.update({k: v for k, v in base.items() if k in DEFAULTS})
        self.load()
        if overrides:
            self.data.update({k: v for k, v in overrides.items() if k in DEFAULTS})

    def load(self) -> dict:
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    self.data.update({k: v for k, v in saved.items() if k in DEFAULTS})
            except Exception:
                pass
        return self.data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, key: str, default=None):
        if key in self.data:
            return self.data[key]
        return DEFAULTS.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value

    def update(self, patch: dict) -> None:
        for key, value in (patch or {}).items():
            if key in DEFAULTS:
                self.data[key] = value
        self.save()


@dataclass(frozen=True)
class StoreSpec:
    """Everything that must be isolated between two logged-in shops."""

    store_id: str
    name: str
    cdp_port: int
    profile_dir: str
    kb_store_id: str = ""
    enabled: bool = True
    auto_start: bool = False
    workspace_url: str = DEFAULTS["workspace_url"]
    chrome_path: str = ""
    runtime_overrides: dict = field(default_factory=dict)
    platform: str = "feige"
    merchant_id: str = "local"

    def __post_init__(self):
        if not self.kb_store_id:
            object.__setattr__(self, "kb_store_id", self.store_id)

    def validate(self) -> None:
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        platform_info(self.platform)
        if not isinstance(self.merchant_id, str) or not 1 <= len(self.merchant_id) <= 64 or any(ch not in allowed for ch in self.merchant_id):
            raise ValueError("merchant_id 只能包含1–64位字母、数字、下划线和短横线")
        if not isinstance(self.store_id, str) or not 1 <= len(self.store_id) <= 64 or any(ch not in allowed for ch in self.store_id):
            raise ValueError("store_id 只能包含字母、数字、下划线和短横线")
        if not self.kb_store_id or any(ch not in allowed for ch in self.kb_store_id):
            raise ValueError("kb_store_id 无效")
        if not (1024 <= int(self.cdp_port) <= 65535):
            raise ValueError("店铺 %s 的调试端口无效：%s" % (self.store_id, self.cdp_port))
        if not self.profile_dir:
            raise ValueError("店铺 %s 缺少 profile_dir" % self.store_id)
        if self.platform != "feige" and (self.auto_start or self.runtime_overrides.get("auto_send")):
            raise ValueError("该平台尚未接入，不能自动启动或自动发送")

    def config_overrides(self) -> dict:
        profile = Path(self.profile_dir)
        if not profile.is_absolute():
            profile = APP_DIR / profile
        result = {
            "cdp_port": int(self.cdp_port),
            "profile_dir": str(profile.resolve()),
            "workspace_url": self.workspace_url,
            "chrome_path": self.chrome_path,
        }
        result.update({key: value for key, value in self.runtime_overrides.items()
                       if key in DEFAULTS and key not in result})
        return result


class StoreRegistry:
    """Loads and validates the durable list used by the desktop application."""

    def __init__(self, path: Path | str = REGISTRY_PATH,
                 manifest_dir: Path | str | None = None):
        self.path = Path(path)
        self.manifest_dir = Path(manifest_dir) if manifest_dir is not None else (
            STORES_KB_DIR if self.path.resolve() == REGISTRY_PATH.resolve() else None
        )

    def load(self, enabled_only: bool = True) -> list[StoreSpec]:
        raw = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        rows = raw.get("stores", []) if isinstance(raw, dict) else raw
        specs = [StoreSpec(**row) for row in rows]
        if self.manifest_dir and self.manifest_dir.is_dir():
            for manifest_path in sorted(self.manifest_dir.glob("*/merchant.yaml")):
                manifest = read_manifest(manifest_path)
                platform = manifest["platform"]
                automation = manifest["automation"]
                if not automation.get("enabled", True):
                    continue
                spec = StoreSpec(
                    store_id=manifest["store_id"], name=manifest.get("name") or manifest["store_id"],
                    cdp_port=int(platform.get("cdp_port", 9222)),
                    profile_dir=platform.get("profile_dir") or "data/profiles/%s" % manifest["store_id"],
                    kb_store_id=manifest["store_id"],
                    auto_start=bool(automation.get("auto_start", False)),
                    workspace_url=platform.get("workspace_url") or (DEFAULTS["workspace_url"] if platform.get("type", "feige") == "feige" else ""),
                    chrome_path=platform.get("browser_path") or "",
                    runtime_overrides={key: value for key, value in automation.items() if key in DEFAULTS},
                    platform=platform.get("type", "feige"),
                    merchant_id=manifest.get("merchant_id", "local"),
                )
                if any(existing.store_id == spec.store_id for existing in specs):
                    raise ValueError("店铺 %s 同时存在于 stores.json 和 merchant.yaml" % spec.store_id)
                specs.append(spec)
        self.validate(specs)
        return [s for s in specs if s.enabled] if enabled_only else specs

    @staticmethod
    def validate(specs: list[StoreSpec]) -> None:
        ids, ports, profiles = set(), set(), set()
        for spec in specs:
            spec.validate()
            profile_path = Path(spec.profile_dir)
            if not profile_path.is_absolute():
                profile_path = APP_DIR / profile_path
            profile = str(profile_path.resolve()).lower()
            for value, used, label in (
                (spec.store_id, ids, "store_id"),
                (int(spec.cdp_port), ports, "cdp_port"),
                (profile, profiles, "profile_dir"),
            ):
                if value in used:
                    raise ValueError("多店配置存在重复 %s：%s" % (label, value))
                used.add(value)

    def scaffold(self, count: int = 10, base_port: int = 9222) -> list[StoreSpec]:
        count = max(1, int(count))
        specs = [
            StoreSpec(
                store_id="shop_%03d" % i,
                name="店铺%02d" % i,
                cdp_port=base_port + i - 1,
                profile_dir=str(
                    DATA_DIR / "chrome-profile"
                    if i == 1 and (DATA_DIR / "chrome-profile").exists()
                    else DATA_DIR / "profiles" / ("shop_%03d" % i)
                ),
            )
            for i in range(1, count + 1)
        ]
        self.save(specs)
        return specs

    def save(self, specs: list[StoreSpec]) -> None:
        self.validate(specs)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "stores": [asdict(s) for s in specs]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
