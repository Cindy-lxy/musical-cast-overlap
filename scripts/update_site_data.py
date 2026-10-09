from __future__ import annotations

import importlib.util
import json
import shutil
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
SITE_DIR = ROOT / "site"
DIST_DIR = ROOT / "dist"
MAIN_FILE = ROOT / "main.py"
LIVE_DATA_URL = "https://cindy-lxy.github.io/musical-cast-overlap/data.json"
SEED_CACHE_FILE = ROOT / "static" / "data" / "runtime-cache.json"
MIN_SEARCH_DAY_SUCCESS_RATIO = 0.95
MAX_TOTAL_SHOW_DROP_RATIO = 0.02
MIN_SEED_SHOW_COUNT = 10_000
MAX_SEED_AGE_DAYS = 3
SEED_FETCH_RETRIES = 3
SEED_FETCH_TIMEOUT_SECONDS = 30
BEIJING_TZ = timezone(timedelta(hours=8))


def validate_seed_payload(payload: dict, now: Optional[datetime] = None) -> None:
    """拒绝空、过小或过期的基线，避免网络异常时用陈旧快照覆盖线上数据。"""
    shows = payload.get("shows")
    if not isinstance(shows, list) or len(shows) < MIN_SEED_SHOW_COUNT:
        raise RuntimeError(f"seed snapshot is too small: {len(shows) if isinstance(shows, list) else 0} shows")

    updated_at_text = str(payload.get("meta", {}).get("updatedAt") or "")
    if not updated_at_text:
        raise RuntimeError("seed snapshot has no updatedAt")
    try:
        updated_at = datetime.fromisoformat(updated_at_text.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"invalid seed updatedAt: {updated_at_text}") from error
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=BEIJING_TZ)
    else:
        updated_at = updated_at.astimezone(BEIJING_TZ)

    now = now or datetime.now(BEIJING_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=BEIJING_TZ)
    age = now.astimezone(BEIJING_TZ) - updated_at
    if age < timedelta(hours=-1):
        raise RuntimeError(f"seed snapshot timestamp is in the future: {updated_at_text}")
    if age > timedelta(days=MAX_SEED_AGE_DAYS):
        raise RuntimeError(f"seed snapshot is stale by {age}: {updated_at_text}")


def validate_seed_file() -> dict:
    payload = json.loads(SEED_CACHE_FILE.read_text(encoding="utf-8"))
    validate_seed_payload(payload)
    return payload


def refresh_seed_from_live_site() -> bool:
    """让无状态 GitHub Actions 继承上一次成功发布的数据快照。"""
    last_error: Optional[Exception] = None
    for attempt in range(SEED_FETCH_RETRIES):
        try:
            request = urllib.request.Request(
                LIVE_DATA_URL,
                headers={"User-Agent": "Aime musical overlap GitHub Pages builder/2.0"},
            )
            with urllib.request.urlopen(request, timeout=SEED_FETCH_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            validate_seed_payload(payload)
            SEED_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary_file = SEED_CACHE_FILE.with_suffix(".json.tmp")
            temporary_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary_file.replace(SEED_CACHE_FILE)
            return True
        except Exception as error:
            last_error = error
            if attempt + 1 < SEED_FETCH_RETRIES:
                time.sleep(1.5 * (attempt + 1))
    print(f"Unable to refresh seed from live site; checking repository baseline: {last_error}")
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
    seed_payload = validate_seed_file()
    module = load_builder_module()
    payload = module.build_dataset_sync(module.beijing_date_key())

    seed_show_count = len(seed_payload.get("shows") or [])
    output_show_count = len(payload.get("shows") or [])
    minimum_output_count = int(seed_show_count * (1 - MAX_TOTAL_SHOW_DROP_RATIO))
    if output_show_count < minimum_output_count:
        raise RuntimeError(
            f"output show count dropped unexpectedly: {output_show_count} < {minimum_output_count}; "
            "keeping the previous published snapshot"
        )

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
