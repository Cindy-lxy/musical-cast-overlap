from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE_DIR = ROOT / "site"
DIST_DIR = ROOT / "dist"
MAIN_FILE = ROOT / "main.py"


def load_builder_module():
    spec = importlib.util.spec_from_file_location("musical_overlap_main", MAIN_FILE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载构建模块: {MAIN_FILE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_site() -> dict:
    module = load_builder_module()
    payload = module.build_dataset_sync(module.beijing_date_key())

    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    shutil.copytree(SITE_DIR, DIST_DIR)

    output_file = DIST_DIR / "data.json"
    output_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (DIST_DIR / ".nojekyll").write_text("", encoding="utf-8")

    return payload


if __name__ == "__main__":
    payload = build_site()
    meta = payload.get("meta", {})
    print(
        json.dumps(
            {
                "cacheKey": meta.get("cacheKey"),
                "updatedAt": meta.get("updatedAt"),
                "showCount": meta.get("showCount"),
                "artistCount": meta.get("artistCount"),
                "failedYears": meta.get("failedYears"),
            },
            ensure_ascii=False,
        )
    )
