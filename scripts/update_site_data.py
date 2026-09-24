from __future__ import annotations

import importlib.util
import json
import shutil
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE_DIR = ROOT / "site"
DIST_DIR = ROOT / "dist"
MAIN_FILE = ROOT / "main.py"
LIVE_DATA_URL = "https://cindy-lxy.github.io/musical-cast-overlap/data.json"
SEED_CACHE_FILE = ROOT / "static" / "data" / "runtime-cache.json"
MIN_SEARCH_DAY_SUCCESS_RATIO = 0.95


def refresh_seed_from_live_site() -> bool:
    """让无状态 GitHub Actions 继承上一次成功发布的数据快照。"""
    try:
        request = urllib.request.Request(
            LIVE_DATA_URL,
            headers={"User-Agent": "Aime musical overlap GitHub Pages builder/1.0"},
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("shows"):
            return False
        SEED_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SEED_CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception as error:
        print(f"Unable to refresh seed from live site; using repository baseline: {error}")
        return False


def load_builder_module():
    spec = importlib.util.spec_from_file_location("musical_overlap_main", MAIN_FILE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载构建模块: {MAIN_FILE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_site() -> dict:
    refresh_seed_from_live_site()
    module = load_builder_module()
    payload = module.build_dataset_sync(module.beijing_date_key())

    meta = payload.get("meta", {})
    search_day_stats = meta.get("searchDayStats", {})
    total_days = int(search_day_stats.get("totalDays") or 0)
    fetched_days = int(search_day_stats.get("fetchedDays") or 0)
    if total_days and fetched_days / total_days < MIN_SEARCH_DAY_SUCCESS_RATIO:
        raise RuntimeError(
            f"search_day refresh incomplete: {fetched_days}/{total_days} days; "
            "keeping the previous published snapshot"
        )
    if meta.get("failedYears"):
        raise RuntimeError(
            f"year refresh failed for {meta.get('failedYears')}; keeping the previous published snapshot"
        )

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
