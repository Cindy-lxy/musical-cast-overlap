import asyncio
import csv
import html
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DOWNLOAD_PAGE = "https://y.saoju.net/yyj/download/"
CSV_URL = "https://y.saoju.net/yyj/download/{year}_2.csv"
API_ROOT = "https://y.saoju.net/yyj/api"
FALLBACK_YEARS = [2023, 2024, 2025]
API_BACKFILL_YEARS = [2022, 2026]
SEARCH_DAY_BACKFILL_FILE = STATIC_DIR / "data" / "2022-search-day.json"
MANUAL_SUPPLEMENT_FILES = [
    STATIC_DIR / "data" / "manual-2014-supplement.json",
    STATIC_DIR / "data" / "manual-2015-supplement.json",
    STATIC_DIR / "data" / "manual-2016-supplement.json",
    STATIC_DIR / "data" / "manual-2017-supplement.json",
    STATIC_DIR / "data" / "manual-2018-supplement.json",
    STATIC_DIR / "data" / "manual-2019-supplement.json",
    STATIC_DIR / "data" / "manual-2020-supplement.json",
    STATIC_DIR / "data" / "manual-2021-supplement.json",
    STATIC_DIR / "data" / "manual-2022-supplement.json",
    STATIC_DIR / "data" / "manual-2023-supplement.json",
    STATIC_DIR / "data" / "manual-2024-supplement.json",
    STATIC_DIR / "data" / "manual-2025-supplement.json",
    STATIC_DIR / "data" / "manual-2026-supplement.json",
    STATIC_DIR / "data" / "manual-2027-supplement.json",
]
CACHE_TTL_SECONDS = 24 * 60 * 60
# 部署时 static 目录会被覆盖，因此把可变缓存/日志/锁写到 /tmp 下不受影响的位置。
TMP_DIR = Path("/tmp/musical-cast-overlap")
TMP_DIR.mkdir(parents=True, exist_ok=True)
SEED_RUNTIME_CACHE_FILE = STATIC_DIR / "data" / "runtime-cache.json"
RUNTIME_CACHE_FILE = TMP_DIR / "runtime-cache.json"
REFRESH_LOCK_FILE = TMP_DIR / "runtime-refresh.lock"
REFRESH_LOG_FILE = TMP_DIR / "runtime-refresh.log"
API_MAX_WORKERS = 32
API_TIMEOUT_SECONDS = 20.0
# 单次刷新总预算：给 BytFaaS 子进程留出安全时间；超时后 break 并合并已抓到的 shows。
TIMEOUT_TOTAL_SECONDS = 180
API_HEADERS = {"User-Agent": "Aime musical overlap public app/2.0"}
ROLE_SPLIT_RE = re.compile(r"\s+")
# 用于识别形如 "Chi Chi:xxx" / "Sonny Boy:xxx" / "El Gallo:xxx" 的多单词英文角色名。
# CSV 中 role 与 artist 之间用空格分隔，但英文角色可能自带空格；
# 若某个 token 是纯拉丁字母（无冒号），且下一个 token 也是纯拉丁字母且带冒号，
# 则将它们合并为同一个 role:artist 词元。
LATIN_NO_COLON_RE = re.compile(r"^[A-Za-z][A-Za-z\-\.']*$")
LATIN_WITH_COLON_RE = re.compile(r"^[A-Za-z][A-Za-z\-\.']*[:：]")
YEAR_LINK_RE = re.compile(r"/yyj/download/(20\d{2})_2\.csv")

# ---------------------------------------------------------------------------
# 归一化规则：把已确认的剧场/艺名等价合并到规范值，避免刷新时被 CSV/API 反覆盖。
# 只改前端能看到的字段，不动 sourceType / sourceUrl。
# 若后续再发现新等价关系，请在此处扩展。
# ---------------------------------------------------------------------------
THEATRE_ALIAS_RULES: Dict[tuple, str] = {
    # (musical, theatre_from) -> theatre_to；musical=None 表示全剧目通用规则。
    ("时光代理人", "时光剧场"): "上海•时光剧场•大世界2楼E厅",
    (None, "上海大剧院中剧场"): "上海大剧院",
}
ARTIST_ALIAS_RULES: Dict[tuple, str] = {
    # (musical, artist_from) -> artist_to
    ("时光代理人", "恩妤"): "李恩妤",
    ("悟空", "智涵"): "张智涵",
}


def normalize_text(value: Any) -> str:
    return html.unescape(str(value or "")).strip()


def canonical_theatre(musical: Optional[str], theatre: Any) -> str:
    theatre_text = normalize_text(theatre)
    return (
        THEATRE_ALIAS_RULES.get((musical, theatre_text))
        or THEATRE_ALIAS_RULES.get((None, theatre_text))
        or theatre_text
    )


def equivalent_show_identity(show: Dict) -> str:
    """用于识别同一场重复录入：同时间/城市/剧目/同演员阵容视为同一场。"""
    artists = "/".join(sorted({normalize_text(item.get("artist")) for item in show.get("cast", []) if item.get("artist")}))
    return "|".join([
        normalize_text(show.get("time")),
        normalize_text(show.get("city")),
        normalize_text(show.get("musical")),
        artists,
    ])


def duplicate_preference_score(show: Dict) -> int:
    source_type = normalize_text(show.get("sourceType"))
    source_url = normalize_text(show.get("sourceUrl"))
    score = 0
    if "/api/show/" in source_url:
        score += 50
    if source_type == "csv":
        score += 40
    elif source_type == "api-backfill":
        score += 30
    elif source_type == "search-day-backfill":
        score += 20
    elif source_type == "manual-verified-backfill":
        score += 10
    score += min(len(show.get("cast") or []), 20)
    return score


def apply_alias_normalization(shows: List[Dict]) -> Dict[str, int]:
    """就地对 shows 的 theatre / cast[*].artist 应用等价规则。

    返回 {'theatre': n, 'artist': m} 便于日志追踪。
    """
    stats = {"theatre": 0, "artist": 0}
    for show in shows:
        musical = show.get("musical")
        target = canonical_theatre(musical, show.get("theatre"))
        if target and show.get("theatre") != target:
            show["theatre"] = target
            stats["theatre"] += 1
        for entry in show.get("cast") or []:
            akey = (musical, entry.get("artist"))
            atarget = ARTIST_ALIAS_RULES.get(akey)
            if atarget and entry.get("artist") != atarget:
                entry["artist"] = atarget
                stats["artist"] += 1
    return stats


def is_small_theatre_name(name: Any) -> bool:
    name = normalize_text(name)
    keywords = [
        "小剧场",
        "黑匣子",
        "新空间",
        "星空间",
        "实验剧场",
        "studio",
    ]
    return any(k.lower() in name.lower() for k in keywords)


def normalize_musical_variants(shows: List[Dict]) -> Dict[str, int]:
    """处理少数需要按场馆拆分的剧目口径。"""
    changed = 0
    for show in shows:
        musical = normalize_text(show.get("musical"))
        if musical == "#0528":
            theatre = normalize_text(show.get("theatre"))
            show["musical"] = "0528" if is_small_theatre_name(theatre) else "0528 镜框板"
            changed += 1
    return {"musical": changed}


def rebuild_artist_indexes(shows: List[Dict]) -> tuple:
    """归一化后重建 artist_index / artist_count，保证 lookup 与旧值不残留。"""
    artist_index: Dict[str, List[int]] = {}
    artist_count: Dict[str, int] = {}
    for idx, show in enumerate(shows):
        seen = {c.get("artist") for c in (show.get("cast") or []) if c.get("artist")}
        for artist in sorted(seen):
            artist_index.setdefault(artist, []).append(idx)
            artist_count[artist] = artist_count.get(artist, 0) + 1
    return artist_index, artist_count


_DATA_CACHE: Optional[Dict] = None
_DATA_CACHE_KEY: Optional[str] = None
_DATA_CACHE_TIME = 0.0
_DATA_REFRESH_LOCK: Optional[asyncio.Lock] = None


def read_refresh_lock() -> Dict[str, Any]:
    if not REFRESH_LOCK_FILE.exists():
        return {}
    try:
        return json.loads(REFRESH_LOCK_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_refresh_pid() -> Optional[int]:
    payload = read_refresh_lock()
    pid = payload.get("pid")
    return pid if isinstance(pid, int) and pid > 0 else None


def get_refresh_lock() -> asyncio.Lock:
    global _DATA_REFRESH_LOCK
    if _DATA_REFRESH_LOCK is None:
        _DATA_REFRESH_LOCK = asyncio.Lock()
    return _DATA_REFRESH_LOCK


def is_refresh_running() -> bool:
    pid = get_refresh_pid()
    if not pid:
        if REFRESH_LOCK_FILE.exists():
            clear_refresh_lock()
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        clear_refresh_lock()
        return False


def beijing_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=8)


def beijing_date_key() -> str:
    return beijing_now().strftime("%Y-%m-%d")


def parse_cast(cast_text: str) -> List[Dict[str, str]]:
    entries = []
    raw_tokens = [t for t in ROLE_SPLIT_RE.split((cast_text or "").strip()) if t]

    # 合并多单词英文角色（如 "Chi Chi:xxx" / "Sonny Boy:xxx" / "El Gallo:xxx"）：
    # 连续多个纯拉丁字母 token（无冒号）后面紧跟一个"拉丁字母+冒号"的 token 时，视为同一角色。
    merged: List[str] = []
    i = 0
    while i < len(raw_tokens):
        tok = raw_tokens[i]
        if LATIN_NO_COLON_RE.match(tok):
            j = i + 1
            while j < len(raw_tokens) and LATIN_NO_COLON_RE.match(raw_tokens[j]):
                j += 1
            if j < len(raw_tokens) and LATIN_WITH_COLON_RE.match(raw_tokens[j]):
                merged.append(" ".join(raw_tokens[i:j + 1]))
                i = j + 1
                continue
        merged.append(tok)
        i += 1

    for token in merged:
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            role, artist = token.split(":", 1)
        elif "：" in token:
            role, artist = token.split("：", 1)
        else:
            role, artist = "", token
        role = role.strip()
        artist = artist.strip()
        if artist:
            entries.append({"role": role, "artist": artist})
    return entries


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Aime musical overlap public app/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8-sig")


def fetch_json(url: str) -> List[Dict]:
    return json.loads(fetch_text(url))


def discover_years() -> List[int]:
    try:
        html = fetch_text(DOWNLOAD_PAGE)
        years = sorted({int(year) for year in YEAR_LINK_RE.findall(html)})
        return years or FALLBACK_YEARS
    except Exception as error:
        LOGGER.warning("Unable to discover years from download page: %s", error)
        return FALLBACK_YEARS


def add_show_to_indexes(show: Dict, shows: List[Dict], artist_index: Dict[str, List[int]], artist_count: Dict[str, int]) -> None:
    show_id = len(shows)
    shows.append(show)
    for artist in sorted({item["artist"] for item in show["cast"] if item.get("artist")}):
        artist_index.setdefault(artist, []).append(show_id)
        artist_count[artist] = artist_count.get(artist, 0) + 1


def show_identity(show: Dict) -> str:
    artists = "/".join(sorted({normalize_text(item.get("artist")) for item in show.get("cast", []) if item.get("artist")}))
    return "|".join([
        normalize_text(show.get("time")),
        normalize_text(show.get("city")),
        normalize_text(show.get("musical")),
        canonical_theatre(show.get("musical"), show.get("theatre")),
        artists,
    ])


def deduplicate_equivalent_shows(shows: List[Dict]) -> Dict[str, int]:
    """删除同一时间/城市/剧目/演员阵容的重复场次。

    这类重复通常来自同一场被 tour 页、show/cast API 或剧场别名重复录入。
    同一演员同一时间不应被同一剧目重复计算，因此保留来源更直接的一条。
    """
    kept: List[Dict] = []
    key_to_index: Dict[str, int] = {}
    removed = 0
    replaced = 0
    for show in shows:
        key = equivalent_show_identity(show)
        existing_index = key_to_index.get(key)
        if existing_index is None:
            key_to_index[key] = len(kept)
            kept.append(show)
            continue
        removed += 1
        existing = kept[existing_index]
        if duplicate_preference_score(show) > duplicate_preference_score(existing):
            kept[existing_index] = show
            replaced += 1
    if removed:
        shows[:] = kept
    return {"removed": removed, "replaced": replaced}


def merge_backfill_file(file_path: Path, shows: List[Dict], artist_index: Dict[str, List[int]], artist_count: Dict[str, int]) -> int:
    if not file_path.exists():
        return 0
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as error:
        LOGGER.warning("Unable to read backfill file %s: %s", file_path, error)
        return 0
    existing = {show_identity(show) for show in shows}
    added = 0
    for show in payload.get("shows", []):
        if not show.get("cast"):
            continue
        identity = show_identity(show)
        if identity in existing:
            continue
        add_show_to_indexes(show, shows, artist_index, artist_count)
        existing.add(identity)
        added += 1
    return added


def merge_manual_supplements(shows: List[Dict], artist_index: Dict[str, List[int]], artist_count: Dict[str, int]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for file_path in MANUAL_SUPPLEMENT_FILES:
        added = merge_backfill_file(file_path, shows, artist_index, artist_count)
        if added:
            year_match = re.search(r"manual-(\d{4})-supplement", file_path.name)
            year = year_match.group(1) if year_match else file_path.stem
            counts[year] = added
    return counts


def merge_search_day_backfill(shows: List[Dict], artist_index: Dict[str, List[int]], artist_count: Dict[str, int]) -> int:
    return merge_backfill_file(SEARCH_DAY_BACKFILL_FILE, shows, artist_index, artist_count)


def parse_csv_year(year: int, shows: List[Dict], artist_index: Dict[str, List[int]], artist_count: Dict[str, int]) -> int:
    text = fetch_text(CSV_URL.format(year=year))
    before = len(shows)
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        cast = parse_cast(row.get("卡司", ""))
        if not cast:
            continue
        show = {
            "time": (row.get("时间") or "").strip(),
            "city": (row.get("城市") or "").strip(),
            "musical": (row.get("音乐剧") or "").strip(),
            "theatre": (row.get("剧院") or "").strip(),
            "cast": cast,
            "sourceType": "csv",
        }
        add_show_to_indexes(show, shows, artist_index, artist_count)
    return len(shows) - before


def parse_api_time_to_beijing(value: str) -> str:
    if not value:
        return ""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(hours=8)
    return dt.strftime("%Y-%m-%d %H:%M")


def load_api_maps() -> Dict[str, Dict]:
    # 并发拉 9 个端点，避免顺序请求把 180s 预算吃光。
    endpoints = ["artist", "musical", "role", "musicalcast", "city", "theatre", "stage", "tour", "schedule"]
    raw: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(endpoints)) as pool:
        futures = {ep: pool.submit(fetch_json_with_httpx, f"{API_ROOT}/{ep}/") for ep in endpoints}
        for ep, future in futures.items():
            raw[ep] = future.result()
    def to_map(items):
        return {item["pk"]: item["fields"] for item in items}
    return {
        "artists": to_map(raw["artist"]),
        "musicals": to_map(raw["musical"]),
        "roles": to_map(raw["role"]),
        "musical_casts": to_map(raw["musicalcast"]),
        "cities": to_map(raw["city"]),
        "theatres": to_map(raw["theatre"]),
        "stages": to_map(raw["stage"]),
        "tours": to_map(raw["tour"]),
        "schedules": to_map(raw["schedule"]),
    }


def schedule_overlaps_year(schedule: Dict, year: int) -> bool:
    begin = schedule.get("begin_date") or ""
    end = schedule.get("end_date") or ""
    if not begin:
        return False
    start_boundary = f"{year}-01-01"
    end_boundary = f"{year}-12-31"
    return begin <= end_boundary and (schedule.get("is_long_term") or not end or end >= start_boundary)


def format_stage_name(stage: Dict, theatre: Dict) -> str:
    theatre_name = theatre.get("name") or "未知剧院"
    stage_name = stage.get("name") or ""
    if stage_name and stage_name not in theatre_name:
        return f"{theatre_name} {stage_name}"
    return theatre_name


def fetch_json_with_httpx(url: str, retries: int = 3) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            return json.loads(fetch_text(url))
        except Exception as error:
            last_error = error
            time.sleep(0.6 * (attempt + 1))
    raise last_error or RuntimeError(f"Unable to fetch {url}")


def build_show_cast_from_refs(cast_refs: List[int], maps: Dict[str, Dict]) -> List[Dict[str, str]]:
    cast = []
    for ref_pk in cast_refs:
        cast_fields = maps["musical_casts"].get(ref_pk, {})
        role = maps["roles"].get(cast_fields.get("role"), {})
        artist = maps["artists"].get(cast_fields.get("artist"), {})
        artist_name = artist.get("name")
        if artist_name:
            cast.append({"role": role.get("name") or "", "artist": artist_name})
    return cast


def parse_api_year(year: int, shows: List[Dict], artist_index: Dict[str, List[int]], artist_count: Dict[str, int], maps: Dict[str, Dict]) -> int:
    before = len(shows)
    existing_ids = {show_identity(show) for show in shows}
    year_prefix = str(year)

    schedule_tasks = []
    for schedule_pk, schedule in maps["schedules"].items():
        if not schedule_overlaps_year(schedule, year):
            continue
        tour = maps["tours"].get(schedule.get("tour"), {})
        musical = maps["musicals"].get(tour.get("musical"), {})
        stage = maps["stages"].get(schedule.get("stage"), {})
        theatre = maps["theatres"].get(stage.get("theatre"), {})
        city = maps["cities"].get(theatre.get("city"), {})
        schedule_tasks.append({
            "schedule_pk": schedule_pk,
            "musical": musical.get("name") or "未知音乐剧",
            "city": city.get("name") or "未知城市",
            "theatre": format_stage_name(stage, theatre),
        })

    def fetch_schedule_shows(task: Dict[str, Any]):
        try:
            items = fetch_json_with_httpx(f"{API_ROOT}/schedule/{task['schedule_pk']}/show/")
        except Exception as error:
            return [], (task["schedule_pk"], str(error))
        rows = []
        for item in items:
            time_text = parse_api_time_to_beijing(item.get("fields", {}).get("time", ""))
            if time_text.startswith(year_prefix):
                rows.append((item["pk"], time_text, task))
        return rows, None

    show_meta: Dict[int, Any] = {}
    schedule_errors = []
    with ThreadPoolExecutor(max_workers=API_MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_schedule_shows, task) for task in schedule_tasks]
        for future in as_completed(futures):
            rows, error = future.result()
            if error:
                schedule_errors.append(error)
                continue
            for show_pk, time_text, task in rows:
                show_meta[show_pk] = (time_text, task)

    if schedule_errors:
        LOGGER.warning("API year %s schedule errors: %s", year, schedule_errors[:5])

    def fetch_show_cast_refs(show_pk: int):
        try:
            items = fetch_json_with_httpx(f"{API_ROOT}/show/{show_pk}/cast/")
            return show_pk, [item["pk"] for item in items], None
        except Exception as error:
            return show_pk, [], str(error)

    cast_refs_by_show: Dict[int, List[int]] = {}
    cast_errors = []
    with ThreadPoolExecutor(max_workers=API_MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_show_cast_refs, show_pk) for show_pk in show_meta.keys()]
        for future in as_completed(futures):
            show_pk, cast_refs, error = future.result()
            if error:
                cast_errors.append((show_pk, error))
                continue
            cast_refs_by_show[show_pk] = cast_refs

    if cast_errors:
        LOGGER.warning("API year %s cast errors: %s", year, cast_errors[:5])

    seen_new_ids = set()
    new_shows = []
    for show_pk, (time_text, task) in sorted(show_meta.items(), key=lambda item: (item[1][0], item[1][1]["musical"], item[1][1]["city"], item[1][1]["theatre"])):
        cast = build_show_cast_from_refs(cast_refs_by_show.get(show_pk, []), maps)
        if not cast:
            continue
        show = {
            "time": time_text,
            "city": task["city"],
            "musical": task["musical"],
            "theatre": task["theatre"],
            "cast": cast,
            "sourceType": "api-backfill",
        }
        ident = show_identity(show)
        if ident in existing_ids or ident in seen_new_ids:
            continue
        seen_new_ids.add(ident)
        new_shows.append(show)

    for show in new_shows:
        add_show_to_indexes(show, shows, artist_index, artist_count)
    return len(shows) - before


def show_position_identity(show: Dict) -> str:
    """位置指纹：不含 cast，用于判断 API 拉回来的场次是否已在库存中。"""
    return "|".join([
        (show.get("time") or "").strip(),
        (show.get("city") or "").strip(),
        (show.get("musical") or "").strip(),
        (show.get("theatre") or "").strip(),
    ])


def parse_api_year_incremental(
    year: int,
    baseline_shows_for_year: List[Dict],
    maps: Dict[str, Dict],
    deadline_ts: float,
) -> Dict[str, Any]:
    """增量拉取：按时间从新到旧，遇到已在库位置指纹立刻 break。

    返回 {new_shows, scanned, total_candidates, stopped_at, truncated_by_timeout,
          schedule_errors, cast_errors, first_new_time, last_new_time}。
    """
    year_prefix = str(year)
    baseline_positions = {show_position_identity(s) for s in baseline_shows_for_year}

    # 1) 汇总所有与目标年份有交集的 schedule。
    schedule_tasks: List[Dict[str, Any]] = []
    for schedule_pk, schedule in maps["schedules"].items():
        if not schedule_overlaps_year(schedule, year):
            continue
        tour = maps["tours"].get(schedule.get("tour"), {})
        musical = maps["musicals"].get(tour.get("musical"), {})
        stage = maps["stages"].get(schedule.get("stage"), {})
        theatre = maps["theatres"].get(stage.get("theatre"), {})
        city = maps["cities"].get(theatre.get("city"), {})
        schedule_tasks.append({
            "schedule_pk": schedule_pk,
            "musical": musical.get("name") or "未知音乐剧",
            "city": city.get("name") or "未知城市",
            "theatre": format_stage_name(stage, theatre),
        })

    # 2) 并发拉每个 schedule 下的 show 列表。
    show_meta: Dict[int, Any] = {}
    schedule_errors: List[Any] = []

    def fetch_schedule_shows(task: Dict[str, Any]):
        try:
            items = fetch_json_with_httpx(f"{API_ROOT}/schedule/{task['schedule_pk']}/show/")
        except Exception as error:
            return [], (task["schedule_pk"], str(error))
        rows = []
        for item in items:
            time_text = parse_api_time_to_beijing(item.get("fields", {}).get("time", ""))
            if time_text.startswith(year_prefix):
                rows.append((item["pk"], time_text, task))
        return rows, None

    truncated = False
    with ThreadPoolExecutor(max_workers=API_MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_schedule_shows, task) for task in schedule_tasks]
        for future in as_completed(futures):
            if time.time() > deadline_ts:
                truncated = True
                break
            rows, error = future.result()
            if error:
                schedule_errors.append(error)
                continue
            for show_pk, time_text, task in rows:
                show_meta[show_pk] = (time_text, task)

    # 3) 按时间从新到旧排序。
    ordered = sorted(show_meta.items(), key=lambda kv: kv[1][0], reverse=True)

    # 4) 逐条判断位置指纹；命中基线立即 break。
    new_shows: List[Dict[str, Any]] = []
    scanned = 0
    stopped_at: Optional[str] = None
    cast_errors: List[Any] = []
    first_new_time: Optional[str] = None
    last_new_time: Optional[str] = None

    for show_pk, (time_text, task) in ordered:
        if time.time() > deadline_ts:
            truncated = True
            break
        scanned += 1
        position_key = "|".join([time_text, task["city"], task["musical"], task["theatre"]])
        if position_key in baseline_positions:
            stopped_at = position_key
            break
        # 拉 cast
        try:
            items = fetch_json_with_httpx(f"{API_ROOT}/show/{show_pk}/cast/")
            cast_refs = [item["pk"] for item in items]
        except Exception as error:
            cast_errors.append((show_pk, str(error)))
            continue
        cast = build_show_cast_from_refs(cast_refs, maps)
        if not cast:
            continue
        show = {
            "time": time_text,
            "city": task["city"],
            "musical": task["musical"],
            "theatre": task["theatre"],
            "cast": cast,
            "sourceType": "api-backfill",
        }
        new_shows.append(show)
        if first_new_time is None:
            first_new_time = time_text
        last_new_time = time_text

    return {
        "new_shows": new_shows,
        "scanned": scanned,
        "total_candidates": len(ordered),
        "stopped_at": stopped_at,
        "truncated_by_timeout": truncated,
        "schedule_errors": schedule_errors[:5],
        "cast_errors": cast_errors[:5],
        "first_new_time": first_new_time,
        "last_new_time": last_new_time,
        "baseline_positions": len(baseline_positions),
    }


def load_runtime_cache_from_disk() -> Optional[Dict]:
    global _DATA_CACHE, _DATA_CACHE_KEY, _DATA_CACHE_TIME
    # 优先读取 /tmp 里可写位置（每天首访生成的最新缓存）；
    # 若不存在，回退到部署包内的种子缓存 static/data/runtime-cache.json；
    # 最后再兜底 data.json（历史遗留）。
    candidates = [RUNTIME_CACHE_FILE, SEED_RUNTIME_CACHE_FILE, STATIC_DIR / "data" / "data.json"]
    cache_path: Optional[Path] = None
    for candidate in candidates:
        if candidate.exists():
            cache_path = candidate
            break
    if cache_path is None:
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception as error:
        LOGGER.warning("Unable to read runtime cache %s: %s", cache_path, error)
        return None

    meta = payload.get("meta", {})
    cache_key = meta.get("cacheKey")
    if not cache_key:
        updated_at = meta.get("updatedAt", "")
        cache_key = updated_at[:10] if updated_at else None
        if cache_key:
            meta["cacheKey"] = cache_key

    _DATA_CACHE = payload
    _DATA_CACHE_KEY = cache_key
    _DATA_CACHE_TIME = cache_path.stat().st_mtime
    return payload


def persist_runtime_cache(payload: Dict) -> None:
    # 只写 /tmp 下的可写位置；部署包内的 seed 保持不变。
    try:
        RUNTIME_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as error:
        LOGGER.warning("Unable to write runtime cache %s: %s", RUNTIME_CACHE_FILE, error)


def write_refresh_lock(pid: Optional[int] = None) -> None:
    try:
        payload: Dict[str, Any] = {"startedAt": beijing_now().isoformat(timespec="seconds")}
        if pid:
            payload["pid"] = pid
        REFRESH_LOCK_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as error:
        LOGGER.warning("Unable to write refresh lock %s: %s", REFRESH_LOCK_FILE, error)


def clear_refresh_lock() -> None:
    try:
        if REFRESH_LOCK_FILE.exists():
            REFRESH_LOCK_FILE.unlink()
    except Exception as error:
        LOGGER.warning("Unable to clear refresh lock %s: %s", REFRESH_LOCK_FILE, error)


def spawn_refresh_process() -> bool:
    if is_refresh_running():
        return False
    REFRESH_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with REFRESH_LOG_FILE.open("ab") as log_file:
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "scripts" / "refresh_runtime_cache.py")],
                cwd=str(ROOT),
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
        write_refresh_lock(process.pid)
        return True
    except Exception as error:
        clear_refresh_lock()
        LOGGER.warning("Unable to spawn refresh process: %s", error)
        return False


def store_runtime_cache(payload: Dict) -> Dict:
    global _DATA_CACHE, _DATA_CACHE_KEY, _DATA_CACHE_TIME
    _DATA_CACHE = payload
    _DATA_CACHE_KEY = payload.get("meta", {}).get("cacheKey")
    _DATA_CACHE_TIME = time.time()
    persist_runtime_cache(payload)
    return payload


def with_runtime_meta(payload: Dict, refresh_triggered: bool = False) -> Dict:
    meta = dict(payload.get("meta", {}))
    meta["refreshInProgress"] = is_refresh_running()
    if refresh_triggered:
        meta["refreshTriggered"] = True
    return {
        **payload,
        "meta": meta,
    }


def build_dataset_sync(today_key: Optional[str] = None) -> Dict:
    today_key = today_key or beijing_date_key()
    deadline_ts = time.time() + TIMEOUT_TOTAL_SECONDS

    # 基线：如果内存里已有 cache，用它；否则从磁盘（TMP 或 SEED）加载一次。
    baseline: Optional[Dict] = _DATA_CACHE
    if baseline is None:
        try:
            baseline = load_runtime_cache_from_disk()
        except Exception as error:
            LOGGER.warning("Unable to load baseline cache: %s", error)
            baseline = None

    baseline_shows: List[Dict] = list(baseline.get("shows", [])) if baseline else []
    baseline_by_year: Dict[int, List[Dict]] = {}
    for show in baseline_shows:
        year_str = (show.get("time") or "")[:4]
        if year_str.isdigit():
            baseline_by_year.setdefault(int(year_str), []).append(show)

    shows: List[Dict] = []
    artist_index: Dict[str, List[int]] = {}
    artist_count: Dict[str, int] = {}
    loaded_years: List[int] = []
    failed_years: List[int] = []
    year_sources: Dict[str, Any] = {}
    incremental_stats: Dict[str, Any] = {}

    years = sorted(set(discover_years()).union(API_BACKFILL_YEARS))
    api_maps: Optional[Dict[str, Dict]] = None
    # 已经通过 CSV 成功装载的年份，不需要再走 baseline fallback。
    csv_loaded_years: set = set()

    for year in years:
        if time.time() > deadline_ts:
            LOGGER.warning("build_dataset_sync deadline exceeded before processing year %s", year)
            break

        # === CSV 尝试（对所有年份都先尝试，成功即结束该年份处理）===
        csv_success = False
        try:
            added = parse_csv_year(year, shows, artist_index, artist_count)
            if added:
                csv_success = True
                csv_loaded_years.add(year)
                loaded_years.append(year)
                year_sources[str(year)] = "csv"
        except Exception as error:
            LOGGER.warning("Unable to fetch CSV year %s: %s", year, error)

        if csv_success:
            # 对于同时属于 API_BACKFILL_YEARS 的年份（如 2022/2026），CSV 有数据时以 CSV 为准，跳过 API 增量。
            continue

        if year in API_BACKFILL_YEARS:
            year_baseline = baseline_by_year.get(year, [])
            # 冷启动兜底：如果没有基线且是 2022，可以先尝试 search-day backfill。
            if not year_baseline and year == 2022:
                file_added = merge_search_day_backfill(shows, artist_index, artist_count)
                if file_added:
                    loaded_years.append(year)
                    year_sources[str(year)] = "search-day-backfill"
                    continue

            try:
                if api_maps is None:
                    api_maps = load_api_maps()
                result = parse_api_year_incremental(year, year_baseline, api_maps, deadline_ts)
                incremental_stats[str(year)] = {
                    "scanned": result["scanned"],
                    "totalCandidates": result["total_candidates"],
                    "newShows": len(result["new_shows"]),
                    "stoppedAt": result["stopped_at"],
                    "truncatedByTimeout": result["truncated_by_timeout"],
                    "firstNewTime": result["first_new_time"],
                    "lastNewTime": result["last_new_time"],
                    "baselinePositions": result["baseline_positions"],
                    "scheduleErrors": result["schedule_errors"],
                    "castErrors": [(pk, err[:200]) for pk, err in result["cast_errors"]],
                }
                # 先把 baseline 的场次重新加回来（这年的现有数据保留）。
                for show in year_baseline:
                    add_show_to_indexes(show, shows, artist_index, artist_count)
                # 再合并新增，注意去重（防止 baseline 里就已经有的完全一致 identity）。
                existing_identity = {show_identity(s) for s in shows}
                for show in result["new_shows"]:
                    ident = show_identity(show)
                    if ident in existing_identity:
                        continue
                    add_show_to_indexes(show, shows, artist_index, artist_count)
                    existing_identity.add(ident)
                loaded_years.append(year)
                if year_baseline or result["new_shows"]:
                    year_sources[str(year)] = "api-backfill"
                else:
                    failed_years.append(year)
                    year_sources[str(year)] = "api-backfill-empty"
                continue
            except Exception as error:
                LOGGER.warning("Unable to incrementally backfill API year %s: %s", year, error)
                # 落到下面的 baseline fallback。

        # CSV 失败 + 非 API 年，或 API 年也失败 → fallback 到 baseline。
        year_baseline = baseline_by_year.get(year, [])
        if year_baseline:
            for show in year_baseline:
                add_show_to_indexes(show, shows, artist_index, artist_count)
            if year not in loaded_years:
                loaded_years.append(year)
            year_sources[str(year)] = "baseline-fallback"
        else:
            if year not in failed_years:
                failed_years.append(year)

    manual_counts = merge_manual_supplements(shows, artist_index, artist_count)
    for year, count in manual_counts.items():
        year_int = int(year) if year.isdigit() else None
        if year_int and year_int not in loaded_years:
            loaded_years.append(year_int)
        year_sources[f"{year}_manual_verified"] = f"{count} shows"

    if not shows:
        if _DATA_CACHE is not None:
            return _DATA_CACHE
        raise HTTPException(status_code=503, detail="暂时无法从数据源加载演出数据")

    # 应用等价归一化（剧场别名 / 艺名别名），以及少量剧目口径归一化（如 #0528）。
    alias_stats = apply_alias_normalization(shows)
    musical_stats = normalize_musical_variants(shows)

    # 清理“同一场被重复录入”的数据问题（同时间/城市/剧目/同演员阵容）。
    dedup_stats = deduplicate_equivalent_shows(shows)

    if alias_stats["theatre"] or alias_stats["artist"] or musical_stats["musical"] or dedup_stats["removed"]:
        LOGGER.info(
            "Normalization applied: theatre=%s, artist=%s, musical=%s; equivalent duplicate shows removed=%s replaced=%s",
            alias_stats["theatre"], alias_stats["artist"], musical_stats["musical"], dedup_stats["removed"], dedup_stats["replaced"],
        )

    artist_index, artist_count = rebuild_artist_indexes(shows)

    artists = sorted(artist_index.keys(), key=lambda name: (-artist_count[name], name))
    artist_lookup = {re.sub(r"\s+", "", name): name for name in artists}
    dates = sorted({show["time"][:10] for show in shows if show.get("time")})

    return {
        "meta": {
            "source": DOWNLOAD_PAGE,
            "years": sorted(loaded_years),
            "yearSources": year_sources,
            "failedYears": failed_years,
            "dateRange": [dates[0], dates[-1]] if dates else [],
            "showCount": len(shows),
            "artistCount": len(artists),
            "updatedAt": beijing_now().isoformat(timespec="seconds"),
            "cacheKey": today_key,
            "refreshPolicy": "服务端会保留最近一次成功缓存；每天北京时间 0 点后第一位访问者会触发一次后台更新，更新完成后当天其余访问都直接复用新缓存。",
            "incrementalStats": incremental_stats,
        },
        "artists": artists,
        "artistLookup": artist_lookup,
        "artistIndex": artist_index,
        "shows": shows,
    }


async def refresh_dataset(force: bool = False) -> Dict:
    today_key = beijing_date_key()
    if (
        not force
        and _DATA_CACHE is not None
        and _DATA_CACHE_KEY == today_key
        and time.time() - _DATA_CACHE_TIME < CACHE_TTL_SECONDS
    ):
        return _DATA_CACHE

    async with get_refresh_lock():
        today_key = beijing_date_key()
        if (
            not force
            and _DATA_CACHE is not None
            and _DATA_CACHE_KEY == today_key
            and time.time() - _DATA_CACHE_TIME < CACHE_TTL_SECONDS
        ):
            return _DATA_CACHE
        payload = await asyncio.to_thread(build_dataset_sync, today_key)
        return store_runtime_cache(payload)


async def trigger_background_refresh(force: bool = False) -> bool:
    today_key = beijing_date_key()
    if (
        not force
        and _DATA_CACHE is not None
        and _DATA_CACHE_KEY == today_key
        and time.time() - _DATA_CACHE_TIME < CACHE_TTL_SECONDS
    ):
        return False

    async with get_refresh_lock():
        today_key = beijing_date_key()
        if (
            not force
            and _DATA_CACHE is not None
            and _DATA_CACHE_KEY == today_key
            and time.time() - _DATA_CACHE_TIME < CACHE_TTL_SECONDS
        ):
            return False
        return spawn_refresh_process()


def reload_runtime_cache_if_disk_newer() -> bool:
    """如果 TMP 中的 runtime-cache.json 比内存中缓存新（子进程刷新后落盘），则重新读入。"""
    try:
        if not RUNTIME_CACHE_FILE.exists():
            return False
        disk_mtime = RUNTIME_CACHE_FILE.stat().st_mtime
        if _DATA_CACHE is not None and disk_mtime <= _DATA_CACHE_TIME:
            return False
        load_runtime_cache_from_disk()
        return True
    except Exception as error:
        LOGGER.warning("Unable to reload runtime cache from disk: %s", error)
        return False


async def get_dataset(force: bool = False) -> Dict:
    today_key = beijing_date_key()
    if _DATA_CACHE is None:
        load_runtime_cache_from_disk()
    else:
        # 子进程刚刷新完可能已经把新 cache 写到 /tmp；及时同步到内存。
        reload_runtime_cache_if_disk_newer()

    if force:
        if _DATA_CACHE is None:
            payload = await refresh_dataset(force=True)
            return with_runtime_meta(payload)
        refresh_triggered = await trigger_background_refresh(force=True)
        return with_runtime_meta(_DATA_CACHE, refresh_triggered=refresh_triggered)

    if (
        _DATA_CACHE is not None
        and _DATA_CACHE_KEY == today_key
        and time.time() - _DATA_CACHE_TIME < CACHE_TTL_SECONDS
    ):
        return with_runtime_meta(_DATA_CACHE)

    if _DATA_CACHE is None:
        payload = await refresh_dataset(force=True)
        return with_runtime_meta(payload)

    refresh_triggered = await trigger_background_refresh(force=False)
    return with_runtime_meta(_DATA_CACHE, refresh_triggered=refresh_triggered)


app = FastAPI(
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/styles.css")
async def styles():
    return FileResponse(STATIC_DIR / "styles.css")


@app.get("/app.js")
async def app_js():
    return FileResponse(STATIC_DIR / "app.js")


@app.get("/actor", response_class=HTMLResponse)
async def actor_page():
    return FileResponse(STATIC_DIR / "actor.html")


@app.get("/actor.html", response_class=HTMLResponse)
async def actor_page_html():
    return FileResponse(STATIC_DIR / "actor.html")


@app.get("/actor.js")
async def actor_js_file():
    return FileResponse(STATIC_DIR / "actor.js")


@app.get("/api/data")
async def data():
    payload = await get_dataset(force=False)
    return JSONResponse(payload)


@app.post("/api/refresh")
async def refresh():
    payload = await get_dataset(force=True)
    return JSONResponse({
        "ok": True,
        "started": bool(payload["meta"].get("refreshTriggered")),
        "refreshInProgress": bool(payload["meta"].get("refreshInProgress")),
        "meta": payload["meta"],
    })


@app.get("/api/health")
async def health():
    if _DATA_CACHE is None:
        load_runtime_cache_from_disk()
    else:
        reload_runtime_cache_if_disk_newer()
    return {
        "ok": True,
        "cacheKey": _DATA_CACHE_KEY,
        "hasCache": _DATA_CACHE is not None,
        "refreshInProgress": is_refresh_running(),
        "runtimeCacheFile": str(RUNTIME_CACHE_FILE),
        "runtimeCacheFileExists": RUNTIME_CACHE_FILE.exists(),
        "seedCacheFile": str(SEED_RUNTIME_CACHE_FILE),
        "seedCacheFileExists": SEED_RUNTIME_CACHE_FILE.exists(),
        "refreshLogFile": str(REFRESH_LOG_FILE),
        "refreshLogFileExists": REFRESH_LOG_FILE.exists(),
    }


@app.get("/api/debug/refresh-log")
async def refresh_log():
    """返回最近一次刷新日志（最多 8 KB），便于线上定位问题。"""
    if not REFRESH_LOG_FILE.exists():
        return JSONResponse({
            "ok": False,
            "reason": "log-file-missing",
            "path": str(REFRESH_LOG_FILE),
        })
    try:
        raw = REFRESH_LOG_FILE.read_bytes()
    except Exception as error:
        return JSONResponse({"ok": False, "reason": "read-error", "error": str(error)})
    tail = raw[-8192:]
    try:
        text = tail.decode("utf-8", errors="replace")
    except Exception:
        text = repr(tail)
    return JSONResponse({
        "ok": True,
        "path": str(REFRESH_LOG_FILE),
        "size": len(raw),
        "lockExists": REFRESH_LOCK_FILE.exists(),
        "refreshInProgress": is_refresh_running(),
        "tail": text,
    })


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


# ---------------------------DO NOT EDIT CODE BELOW THIS LINE---------------------------------
# This is the entry point for the FastAPI application.
if __name__ == "__main__":
    port = int(os.environ.get("_BYTEFAAS_RUNTIME_PORT", 8000))
    config = uvicorn.Config("main:app", port=port, log_level="info", host=None)
    server = uvicorn.Server(config)
    server.run()
# --------------------------------------------------------------------------------------------
