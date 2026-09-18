import json
import base64
import hashlib
import hmac
import logging
import math
import os
import shutil
import sqlite3
import secrets
import sys
import time
import traceback
import urllib.parse
from functools import lru_cache
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logger = logging.getLogger(__name__)


def request_without_env_proxy(method: str, url: str, **kwargs):
    session = requests.Session()
    session.trust_env = False
    try:
        return session.request(method=method, url=url, **kwargs)
    finally:
        session.close()


def log_exception_to_terminal(message: str, **context):
    logger.exception("%s | context=%s", message, context)
    print(f"[ERROR] {message}", file=sys.stderr, flush=True)
    if context:
        print(f"[ERROR] context={context}", file=sys.stderr, flush=True)
    traceback.print_exc(file=sys.stderr)

# ---------------- 基础配置 ----------------

# Overpass API 地址，用于检索 OpenStreetMap 数据（按顺序回退）
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

# 北京六环大致范围（南, 西, 北, 东），后续查询都限制在这个盒子内
BEIJING_BBOX = (39.2, 115.7, 40.6, 117.5)
# 惠州大致范围（南, 西, 北, 东）
HUIZHOU_BBOX = (22.6, 113.7, 23.8, 115.3)

# 支持的城市与对应 bbox
CITY_BBOXES = {
    "beijing": BEIJING_BBOX,
    "北京": BEIJING_BBOX,
    "huizhou": HUIZHOU_BBOX,
    "惠州": HUIZHOU_BBOX,
}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 开发阶段先放开
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

base_dir = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(base_dir)), name="static")

BAIDU_MAP_AK = (os.getenv("BAIDU_MAP_AK") or "FpAB3l4XdVStTv7D8VaMsFk7n2rMFAAK").strip()
BAIDU_MAP_SK = (os.getenv("BAIDU_MAP_SK") or "cqjLb12n2Du6tqAAk6qnIa4jUHY8RWiY").strip()

def resolve_db_path() -> Path:
    env_path = (os.getenv("LUCKINIT_DB_PATH") or "").strip()
    if env_path:
        db_path = Path(env_path).expanduser().resolve()
    else:
        db_path = (base_dir / "cache.sqlite3").resolve()

    db_path.parent.mkdir(parents=True, exist_ok=True)

    legacy_db = base_dir / "cache.sqlite3"
    if db_path != legacy_db and not db_path.exists() and legacy_db.exists():
        try:
            src = sqlite3.connect(str(legacy_db))
            try:
                dst = sqlite3.connect(str(db_path))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        except sqlite3.Error:
            try:
                shutil.copy2(legacy_db, db_path)
            except OSError:
                pass

    return db_path


DB_PATH = resolve_db_path()
AUTH_COOKIE_NAME = "auth_token"


def build_login_redirect(request: Request) -> RedirectResponse:
    next_path = request.url.path or "/"
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    target = f"/?next={urllib.parse.quote(next_path, safe='')}"
    return RedirectResponse(url=target, status_code=303)


def extract_auth_token(request: Request) -> str:
    auth = request.headers.get("Authorization") or ""
    if auth.startswith("Bearer "):
        token = auth[len("Bearer ") :].strip()
        if token:
            return token
    cookie_token = (request.cookies.get(AUTH_COOKIE_NAME) or "").strip()
    if cookie_token:
        return cookie_token
    legacy_cookie_token = (request.cookies.get("authToken") or "").strip()
    if legacy_cookie_token:
        return legacy_cookie_token
    return ""


def require_current_user(request: Request):
    return get_current_user(request)

@app.get("/", include_in_schema=False)
def root():
    return FileResponse(str(base_dir / "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(str(base_dir / "favicon.ico"))


@app.get("/city_analysis", include_in_schema=False)
def city_analysis_page(request: Request):
    try:
        get_current_user(request)
    except HTTPException:
        return build_login_redirect(request)
    return FileResponse(
        str(base_dir / "city_analysis.html"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/mengniu_beijing", include_in_schema=False)
def mengniu_beijing_page(request: Request):
    try:
        get_current_user(request)
    except HTTPException:
        return build_login_redirect(request)
    return FileResponse(
        str(base_dir / "mengniu_beijing.html"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/beijing_office_map", include_in_schema=False)
def beijing_office_map_page(request: Request):
    try:
        get_current_user(request)
    except HTTPException:
        return build_login_redirect(request)
    return FileResponse(
        str(base_dir / "beijing_office_map.html"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/proxy/baidu/{endpoint}", include_in_schema=False)
def baidu_js_proxy(endpoint: str, request: Request, _user=Depends(require_current_user)):
    if endpoint not in {"api", "getscript"}:
        raise HTTPException(status_code=404, detail="Unsupported Baidu SDK endpoint")

    upstream_url = f"https://api.map.baidu.com/{endpoint}"
    params = dict(request.query_params)

    try:
        resp = requests.get(upstream_url, params=params, timeout=30)
        resp.raise_for_status()
    except Exception:
        log_exception_to_terminal(
            "baidu_js_proxy failed",
            endpoint=endpoint,
            upstream_url=upstream_url,
            params=params,
        )
        raise HTTPException(status_code=502, detail="Baidu JS SDK proxy unavailable")

    content = resp.content
    content_type = resp.headers.get("Content-Type", "application/javascript")
    if "javascript" in content_type.lower() or "text" in content_type.lower():
        text = resp.text
        proxy_prefix = "/proxy/baidu/getscript?"
        text = text.replace("https://api.map.baidu.com/getscript?", proxy_prefix)
        text = text.replace("http://api.map.baidu.com/getscript?", proxy_prefix)
        text = text.replace("//api.map.baidu.com/getscript?", proxy_prefix)
        content = text.encode(resp.encoding or "utf-8", errors="ignore")

    return Response(content=content, media_type=content_type)


@app.get("/proxy/remote-script", include_in_schema=False)
def remote_script_proxy(
    url: str = Query(..., description="Remote script URL"),
    _user=Depends(require_current_user),
):
    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").lower()
    allowed_hosts = {
        "api.map.baidu.com",
        "maponline0.bdimg.com",
        "maponline1.bdimg.com",
        "maponline2.bdimg.com",
        "maponline3.bdimg.com",
        "dlswbr.baidu.com",
        "webapi.amap.com",
    }
    amap_tile_prefixes = ("webrd0", "wprd0", "webst0")

    is_allowed_amap_tile_host = (
        hostname.endswith(".is.autonavi.com")
        and hostname.startswith(amap_tile_prefixes)
    )

    if parsed.scheme not in {"http", "https"} or (
        hostname not in allowed_hosts and not is_allowed_amap_tile_host
    ):
        raise HTTPException(status_code=400, detail="Unsupported proxy host")

    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except Exception:
        log_exception_to_terminal("remote_script_proxy failed", url=url)
        raise HTTPException(status_code=502, detail="Remote script unavailable")

    return Response(
        content=resp.content,
        media_type=resp.headers.get("Content-Type", "application/javascript"),
    )


@app.get("/proxy/remote-asset", include_in_schema=False)
def remote_asset_proxy(
    url: str = Query(..., description="Remote asset URL"),
    _user=Depends(require_current_user),
):
    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").lower()
    allowed_hosts = {
        "api.map.baidu.com",
        "maponline0.bdimg.com",
        "maponline1.bdimg.com",
        "maponline2.bdimg.com",
        "maponline3.bdimg.com",
        "dlswbr.baidu.com",
        "webapi.amap.com",
    }
    amap_tile_prefixes = ("webrd0", "wprd0", "webst0")

    is_allowed_amap_tile_host = (
        hostname.endswith(".is.autonavi.com")
        and hostname.startswith(amap_tile_prefixes)
    )

    if parsed.scheme not in {"http", "https"} or (
        hostname not in allowed_hosts and not is_allowed_amap_tile_host
    ):
        raise HTTPException(status_code=400, detail="Unsupported proxy host")

    try:
        resp = requests.get(
            url,
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0 LuckinitMapProxy",
                "Referer": "http://127.0.0.1:8008/",
            },
        )
        resp.raise_for_status()
    except Exception:
        log_exception_to_terminal("remote_asset_proxy failed", url=url)
        raise HTTPException(status_code=502, detail="Remote asset unavailable")

    headers = {}
    cache_control = resp.headers.get("Cache-Control")
    if cache_control:
        headers["Cache-Control"] = cache_control

    return Response(
        content=resp.content,
        media_type=resp.headers.get("Content-Type", "application/octet-stream"),
        headers=headers,
    )


def build_baidu_sn(path: str, params: dict[str, str]) -> str:
    query = urllib.parse.urlencode(params, safe=",:|")
    raw = f"{path}?{query}{BAIDU_MAP_SK}"
    quoted = urllib.parse.quote(raw, safe="/:=&?#+!$,;'@()*[]|,")
    return hashlib.md5(quoted.encode("utf-8")).hexdigest()


def build_baidu_static_map_url(
    center: str,
    markers: str,
    width: int = 960,
    height: int = 640,
    zoom: int = 13,
) -> str:
    if not BAIDU_MAP_AK:
        raise HTTPException(status_code=500, detail="Baidu AK not configured")

    path = "/staticimage/v2"
    params = {
        "ak": BAIDU_MAP_AK,
        "center": center,
        "width": str(max(200, min(width, 1024))),
        "height": str(max(200, min(height, 1024))),
        "zoom": str(max(3, min(zoom, 18))),
        "markers": markers,
        "markerStyles": "l,0xE65C00,0xFFFFFF",
    }
    if BAIDU_MAP_SK:
        params["sn"] = build_baidu_sn(path, params)
    return f"https://api.map.baidu.com{path}?{urllib.parse.urlencode(params, safe=':,|')}"




# ---------------- Overpass 调用 ----------------


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache (
                city TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                version INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beijing_flow_heatmap_cells (
                cell_id INTEGER PRIMARY KEY AUTOINCREMENT,
                district TEXT,
                category TEXT,
                weight REAL NOT NULL,
                poi_count INTEGER NOT NULL,
                sample_names TEXT,
                wgs84_latitude REAL NOT NULL,
                wgs84_longitude REAL NOT NULL,
                gcj02_latitude REAL NOT NULL,
                gcj02_longitude REAL NOT NULL,
                updated_at INTEGER NOT NULL,
                source TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_beijing_flow_heatmap_district ON beijing_flow_heatmap_cells(district)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_beijing_flow_heatmap_updated ON beijing_flow_heatmap_cells(updated_at)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")
        # 兼容老库：给 cache 表补 version 字段
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(cache)")]
        if "version" not in cols:
            conn.execute("ALTER TABLE cache ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        conn.commit()


def load_cache(city: str):
    try:
        city_key = city.strip().lower()
        with get_db() as conn:
            row = conn.execute(
                "SELECT data, updated_at, version FROM cache WHERE city = ?",
                (city_key,),
            ).fetchone()
            if not row:
                return None
            if row["version"] != CACHE_VERSION:
                return None
            if int(time.time()) - int(row["updated_at"]) > CACHE_TTL_SECONDS:
                return None
            return json.loads(row["data"])
    except HTTPException:
        raise
    except HTTPException:
        raise
    except Exception:
        return None
    return None


def load_cache_entry(city: str):
    try:
        city_key = city.strip().lower()
        with get_db() as conn:
            row = conn.execute(
                "SELECT data, updated_at, version FROM cache WHERE city = ?",
                (city_key,),
            ).fetchone()
            if not row:
                return None
            if row["version"] != CACHE_VERSION:
                return None
            if int(time.time()) - int(row["updated_at"]) > CACHE_TTL_SECONDS:
                return None
            return {
                "data": json.loads(row["data"]),
                "updated_at": int(row["updated_at"]),
            }
    except HTTPException:
        raise
    except Exception:
        return None
    return None


def fetch_beijing_office_buildings(district: str | None = None):
    sql = """
        SELECT
            id,
            city_name,
            district,
            location_name,
            location_type,
            location_subtype,
            recommendation_status,
            opened_store_count,
            work_population,
            work_target_consumption,
            work_target_industry_ratio,
            daily_traffic,
            traffic_target_consumption,
            traffic_high_end_phone_ratio,
            student_population,
            student_target_consumption,
            student_female_ratio,
            throughput,
            target_consumption_traffic,
            daily_delivery_orders,
            longitude,
            latitude,
            gcj02_longitude,
            gcj02_latitude,
            geo_confidence,
            geo_method,
            geo_query,
            geo_match_address,
            geo_match_level,
            geo_score,
            excel_row_number,
            source_sheet,
            source_file
        FROM beijing_office_buildings
    """
    params = []
    if district:
        sql += " WHERE district = ?"
        params.append(district)
    sql += " ORDER BY district ASC, location_name ASC"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        districts = [
            r["district"]
            for r in conn.execute(
                """
                SELECT DISTINCT district
                FROM beijing_office_buildings
                WHERE district IS NOT NULL AND TRIM(district) <> ''
                ORDER BY district ASC
                """
            ).fetchall()
        ]
    items = []
    for row in rows:
        items.append(
            {
                "id": row["id"],
                "city_name": row["city_name"],
                "district": row["district"],
                "location_name": row["location_name"],
                "location_type": row["location_type"],
                "location_subtype": row["location_subtype"],
                "recommendation_status": row["recommendation_status"],
                "opened_store_count": row["opened_store_count"],
                "work_population": row["work_population"],
                "work_target_consumption": row["work_target_consumption"],
                "work_target_industry_ratio": row["work_target_industry_ratio"],
                "daily_traffic": row["daily_traffic"],
                "traffic_target_consumption": row["traffic_target_consumption"],
                "traffic_high_end_phone_ratio": row["traffic_high_end_phone_ratio"],
                "student_population": row["student_population"],
                "student_target_consumption": row["student_target_consumption"],
                "student_female_ratio": row["student_female_ratio"],
                "throughput": row["throughput"],
                "target_consumption_traffic": row["target_consumption_traffic"],
                "daily_delivery_orders": row["daily_delivery_orders"],
                "longitude": row["longitude"],
                "latitude": row["latitude"],
                "gcj02_longitude": row["gcj02_longitude"],
                "gcj02_latitude": row["gcj02_latitude"],
                "geo_confidence": row["geo_confidence"],
                "geo_method": row["geo_method"],
                "geo_query": row["geo_query"],
                "geo_match_address": row["geo_match_address"],
                "geo_match_level": row["geo_match_level"],
                "geo_score": row["geo_score"],
                "excel_row_number": row["excel_row_number"],
                "source_sheet": row["source_sheet"],
                "source_file": row["source_file"],
            }
        )
    return {
        "districts": districts,
        "items": items,
        "count": len(items),
    }


BEIJING_FLOW_PROXY_RULES = [
    {"key": "subway_entrance", "weight": 18.0, "tags": {"railway": {"subway_entrance"}}},
    {
        "key": "subway_station",
        "weight": 16.0,
        "tags": {
            "station": {"subway"},
            "railway": {"station"},
            "public_transport": {"station"},
        },
    },
    {
        "key": "bus_hub",
        "weight": 8.0,
        "tags": {
            "amenity": {"bus_station"},
            "public_transport": {"platform", "stop_position"},
        },
    },
    {"key": "bus_stop", "weight": 5.0, "tags": {"highway": {"bus_stop"}}},
    {
        "key": "mall",
        "weight": 14.0,
        "tags": {
            "shop": {"mall", "department_store"},
            "amenity": {"marketplace"},
        },
    },
    {
        "key": "supermarket",
        "weight": 8.0,
        "tags": {"shop": {"supermarket", "convenience"}},
    },
    {
        "key": "food_beverage",
        "weight": 5.5,
        "tags": {
            "amenity": {
                "restaurant",
                "fast_food",
                "food_court",
                "cafe",
                "bar",
                "pub",
                "biergarten",
                "ice_cream",
            },
            "shop": {"bakery", "confectionery", "deli"},
        },
    },
    {
        "key": "tea_drink",
        "weight": 4.5,
        "tags": {"amenity": {"tea", "bubble_tea"}},
    },
    {
        "key": "office_cluster",
        "weight": 4.8,
        "tags": {
            "building": {"office"},
            "amenity": {"business_centre"},
            "office": {"company", "government", "it", "telecommunication"},
        },
    },
    {
        "key": "education",
        "weight": 4.2,
        "tags": {"amenity": {"college", "university", "school"}},
    },
    {
        "key": "leisure",
        "weight": 4.8,
        "tags": {"amenity": {"cinema", "theatre"}},
    },
]

FLOW_GRID_LAT_STEP = 250 / 111320
FLOW_GRID_LNG_STEP = 250 / (111320 * math.cos(math.radians(39.9)))


def classify_beijing_flow_proxy(tags: dict) -> tuple[str, float] | None:
    if not tags:
        return None
    for rule in BEIJING_FLOW_PROXY_RULES:
        for tag_key, allowed_values in rule["tags"].items():
            tag_value = (tags.get(tag_key) or "").strip().lower()
            if tag_value and tag_value in allowed_values:
                return rule["key"], float(rule["weight"])
    return None


def aggregate_beijing_flow_proxy(points: list[dict]) -> list[dict]:
    grid: dict[tuple[int, int], dict] = {}
    for point in points:
        lat = point["lat"]
        lon = point["lon"]
        lat_idx = int(round(lat / FLOW_GRID_LAT_STEP))
        lon_idx = int(round(lon / FLOW_GRID_LNG_STEP))
        cell_key = (lat_idx, lon_idx)
        bucket = grid.get(cell_key)
        if not bucket:
            bucket = {
                "lat_sum": 0.0,
                "lon_sum": 0.0,
                "weight": 0.0,
                "poi_count": 0,
                "categories": {},
                "districts": {},
                "sample_names": [],
            }
            grid[cell_key] = bucket
        bucket["lat_sum"] += lat
        bucket["lon_sum"] += lon
        bucket["weight"] += point["weight"]
        bucket["poi_count"] += 1
        bucket["categories"][point["category"]] = bucket["categories"].get(point["category"], 0) + 1
        district = (point.get("district") or "").strip()
        if district:
            bucket["districts"][district] = bucket["districts"].get(district, 0) + 1
        name = (point.get("name") or "").strip()
        if name and len(bucket["sample_names"]) < 3 and name not in bucket["sample_names"]:
            bucket["sample_names"].append(name)

    items = []
    for idx, bucket in enumerate(grid.values(), start=1):
        lat = bucket["lat_sum"] / max(bucket["poi_count"], 1)
        lon = bucket["lon_sum"] / max(bucket["poi_count"], 1)
        gcj_lat, gcj_lon = wgs84_to_gcj02(lat, lon)
        top_category = max(bucket["categories"].items(), key=lambda item: item[1])[0]
        district = (
            max(bucket["districts"].items(), key=lambda item: item[1])[0]
            if bucket["districts"]
            else ""
        )
        items.append(
            {
                "id": idx,
                "district": district,
                "category": top_category,
                "weight": round(bucket["weight"], 2),
                "poi_count": bucket["poi_count"],
                "sample_names": bucket["sample_names"],
                "location_wgs84": {"lat": lat, "lng": lon},
                "location_gcj02": {"lat": gcj_lat, "lng": gcj_lon},
            }
        )
    items.sort(key=lambda item: item["weight"], reverse=True)
    return items


def load_beijing_flow_proxy_from_table(district: str | None = None) -> dict | None:
    now = int(time.time())
    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(updated_at) AS updated_at FROM beijing_flow_heatmap_cells"
        ).fetchone()
        updated_at = int(row["updated_at"] or 0)
        if not updated_at or now - updated_at > CACHE_TTL_SECONDS:
            return None

        params = []
        sql = """
            SELECT
                district,
                category,
                weight,
                poi_count,
                sample_names,
                wgs84_latitude,
                wgs84_longitude,
                gcj02_latitude,
                gcj02_longitude,
                updated_at,
                source
            FROM beijing_flow_heatmap_cells
        """
        if district:
            sql += " WHERE district = ?"
            params.append(district)
        sql += " ORDER BY weight DESC, poi_count DESC"
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        return None

    points = []
    for idx, row in enumerate(rows, start=1):
        sample_names = []
        raw_sample_names = row["sample_names"]
        if raw_sample_names:
            try:
                sample_names = json.loads(raw_sample_names)
            except json.JSONDecodeError:
                sample_names = []
        points.append(
            {
                "id": idx,
                "district": row["district"],
                "category": row["category"],
                "weight": row["weight"],
                "poi_count": row["poi_count"],
                "sample_names": sample_names,
                "location_wgs84": {
                    "lat": row["wgs84_latitude"],
                    "lng": row["wgs84_longitude"],
                },
                "location_gcj02": {
                    "lat": row["gcj02_latitude"],
                    "lng": row["gcj02_longitude"],
                },
            }
        )
    return {
        "source": rows[0]["source"] or "sqlite_table",
        "cached_at": updated_at,
        "description": "Heatmap uses OSM/Overpass proxy signals stored in SQLite cells.",
        "points": points,
    }


def save_beijing_flow_proxy_to_table(points: list[dict], source: str, updated_at: int):
    with get_db() as conn:
        conn.execute("DELETE FROM beijing_flow_heatmap_cells")
        conn.executemany(
            """
            INSERT INTO beijing_flow_heatmap_cells (
                district,
                category,
                weight,
                poi_count,
                sample_names,
                wgs84_latitude,
                wgs84_longitude,
                gcj02_latitude,
                gcj02_longitude,
                updated_at,
                source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.get("district"),
                    item.get("category"),
                    float(item.get("weight") or 0),
                    int(item.get("poi_count") or 0),
                    json.dumps(item.get("sample_names") or [], ensure_ascii=False),
                    float((item.get("location_wgs84") or {}).get("lat") or 0),
                    float((item.get("location_wgs84") or {}).get("lng") or 0),
                    float((item.get("location_gcj02") or {}).get("lat") or 0),
                    float((item.get("location_gcj02") or {}).get("lng") or 0),
                    int(updated_at),
                    source,
                )
                for item in points
            ],
        )
        conn.commit()


def build_beijing_flow_proxy() -> dict:
    table_payload = load_beijing_flow_proxy_from_table()
    if table_payload and (table_payload.get("points") or []):
        return table_payload

    cache_key = "beijing_flow_proxy_osm_v1"
    cached_entry = load_cache_entry(cache_key)
    if cached_entry and (cached_entry["data"].get("points") or []):
        cached_data = dict(cached_entry["data"])
        cached_data["cached_at"] = cached_entry["updated_at"]
        cached_data["source"] = "cache"
        if cached_data.get("points"):
            save_beijing_flow_proxy_to_table(
                cached_data["points"],
                source="sqlite_cache_bootstrap",
                updated_at=int(cached_data["cached_at"]),
            )
        return cached_data

    area_id = fetch_admin_area_id("Beijing")
    if not area_id:
        raise HTTPException(status_code=500, detail="Beijing area boundary unavailable")

    query = f"""
    [out:json][timeout:45];
    (
      nwr["railway"="subway_entrance"](area:{area_id});
      nwr["railway"="station"](area:{area_id});
      nwr["station"="subway"](area:{area_id});
      nwr["public_transport"="station"](area:{area_id});
      nwr["public_transport"="platform"](area:{area_id});
      nwr["public_transport"="stop_position"](area:{area_id});
      nwr["highway"="bus_stop"](area:{area_id});
      nwr["amenity"="bus_station"](area:{area_id});
      nwr["shop"="mall"](area:{area_id});
      nwr["shop"="department_store"](area:{area_id});
      nwr["shop"="supermarket"](area:{area_id});
      nwr["shop"="convenience"](area:{area_id});
      nwr["amenity"="marketplace"](area:{area_id});
      nwr["amenity"="restaurant"](area:{area_id});
      nwr["amenity"="fast_food"](area:{area_id});
      nwr["amenity"="food_court"](area:{area_id});
      nwr["amenity"="cafe"](area:{area_id});
      nwr["amenity"="tea"](area:{area_id});
      nwr["amenity"="bubble_tea"](area:{area_id});
      nwr["amenity"="bar"](area:{area_id});
      nwr["amenity"="pub"](area:{area_id});
      nwr["amenity"="biergarten"](area:{area_id});
      nwr["amenity"="ice_cream"](area:{area_id});
      nwr["shop"="bakery"](area:{area_id});
      nwr["shop"="confectionery"](area:{area_id});
      nwr["shop"="deli"](area:{area_id});
      nwr["building"="office"](area:{area_id});
      nwr["amenity"="business_centre"](area:{area_id});
      nwr["office"](area:{area_id});
      nwr["amenity"="college"](area:{area_id});
      nwr["amenity"="university"](area:{area_id});
      nwr["amenity"="school"](area:{area_id});
      nwr["amenity"="cinema"](area:{area_id});
      nwr["amenity"="theatre"](area:{area_id});
    );
    out center;
    """

    try:
        elements = overpass_request(query)
    except Exception:
        log_exception_to_terminal("build_beijing_flow_proxy failed", area_id=area_id)
        raise HTTPException(status_code=502, detail="OpenStreetMap flow proxy unavailable")

    raw_points = []
    for el in elements:
        tags = el.get("tags") or {}
        classified = classify_beijing_flow_proxy(tags)
        if not classified:
            continue
        center = el.get("center") or {}
        lat = el.get("lat") or center.get("lat")
        lon = el.get("lon") or center.get("lon")
        if lat is None or lon is None:
            continue
        category, weight = classified
        raw_points.append(
            {
                "name": (tags.get("name") or "").strip(),
                "category": category,
                "weight": weight,
                "district": infer_beijing_district(tags, lat, lon),
                "lat": lat,
                "lon": lon,
            }
        )

    aggregated = aggregate_beijing_flow_proxy(raw_points)
    updated_at = int(time.time())
    payload = {
        "source": "osm_overpass_proxy",
        "cached_at": updated_at,
        "description": "Heatmap uses OSM/Overpass proxy signals: subway, bus, retail, food, office and education POIs.",
        "points": aggregated,
    }
    save_beijing_flow_proxy_to_table(
        aggregated,
        source="osm_overpass_proxy",
        updated_at=updated_at,
    )
    save_cache(cache_key, payload)
    return payload


def has_non_empty_hotspots(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    hotspots = payload.get("hotspots")
    return isinstance(hotspots, list) and len(hotspots) > 0


def save_cache(city: str, data: dict):
    try:
        city_key = city.strip().lower()
        payload = json.dumps(data, ensure_ascii=False)
        ts = int(time.time())
        with get_db() as conn:
            conn.execute(
                "REPLACE INTO cache(city, data, updated_at, version) VALUES (?, ?, ?, ?)",
                (city_key, payload, ts, CACHE_VERSION),
            )
            conn.commit()
    except Exception:
        return


def resolve_bbox(city: str):
    """
    根据城市名称获取约束范围，支持中英文。
    """
    key = city.lower()
    return CITY_BBOXES.get(key) or CITY_BBOXES.get(city)


# 目前仅支持北京/惠州，直接使用已知的 OSM 行政边界 relation id，避免误匹配/超时
CITY_BOUNDARY_REL_IDS = {
    "beijing": 912940,  # 北京市（admin_level=4）
    "北京": 912940,
    "北京市": 912940,
    "huizhou": 3209912,  # 惠州市（admin_level=5）
    "惠州": 3209912,
    "惠州市": 3209912,
}


def resolve_city_boundary_rel_id(city: str):
    key = (city or "").strip().lower()
    return CITY_BOUNDARY_REL_IDS.get(key) or CITY_BOUNDARY_REL_IDS.get(city)


@lru_cache(maxsize=32)
def fetch_admin_area_id(city: str):
    """
    获取城市的行政边界 area_id（Overpass area 由 relation_id 推导：3600000000 + rel_id）。
    找不到则返回 None。
    """
    rel_id = resolve_city_boundary_rel_id(city)
    if rel_id:
        return 3600000000 + int(rel_id)

    # 兜底：如果未来扩展更多城市，可在这里做 name/admin_level 搜索
    return None


def overpass_request(query: str):
    # 发送 Overpass 查询并提取元素列表（多端点回退）
    last_exc = None
    for url in OVERPASS_URLS:
        try:
            resp = request_without_env_proxy(
                "POST",
                url,
                data={"data": query},
                timeout=60,
            )
            # 429/5xx 在 Overpass 上较常见，尝试回退
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = requests.HTTPError(
                    f"{resp.status_code} from {url}", response=resp
                )
                continue
            resp.raise_for_status()
            data = resp.json()
            return data.get("elements", [])
        except Exception as e:
            last_exc = e
            logger.warning("overpass_request failed for url=%s error=%s", url, e)
            continue
    if last_exc:
        log_exception_to_terminal("overpass_request failed", urls=OVERPASS_URLS)
        raise last_exc
    return []


def format_address(tags: dict, lat: float, lon: float):
    """
    尝试从多种字段拼装地址，若缺失则回退到经纬度。
    """
    if not tags:
        return f"坐标：{lat:.4f}, {lon:.4f}"
    candidates = [
        tags.get("addr:full"),
        " ".join(
            filter(
                None,
                [
                    tags.get("addr:province"),
                    tags.get("addr:city"),
                    tags.get("addr:district"),
                    tags.get("addr:subdistrict"),
                    tags.get("addr:suburb"),
                    tags.get("addr:neighbourhood"),
                    tags.get("addr:street"),
                    tags.get("addr:housenumber"),
                ],
            )
        ),
        tags.get("addr:place"),
        tags.get("addr:hamlet"),
        tags.get("addr:village"),
        tags.get("addr:postcode"),
    ]
    for cand in candidates:
        if cand and cand.strip():
            return cand.strip()
    return f"坐标：{lat:.4f}, {lon:.4f}"


# 初始化缓存数据库
init_db()


# ---------------- 账号与鉴权（SQLite） ----------------

PWD_ALGO = "pbkdf2_sha256"
PWD_ITERS = 180_000
SESSION_TTL_SECONDS = 7 * 24 * 3600
CACHE_TTL_SECONDS = 6 * 3600
# 缓存版本：用于在算法/查询方式变化时失效旧缓存
CACHE_VERSION = 14


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not password:
        raise ValueError("password required")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PWD_ITERS)
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    hash_b64 = base64.urlsafe_b64encode(dk).decode("ascii").rstrip("=")
    return f"{PWD_ALGO}${PWD_ITERS}${salt_b64}${hash_b64}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$", 3)
        if algo != PWD_ALGO:
            return False
        iters = int(iters_s)
        salt = base64.urlsafe_b64decode(salt_b64 + "==")
        expected = base64.urlsafe_b64decode(hash_b64 + "==")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def get_user_by_username(conn: sqlite3.Connection, username: str):
    return conn.execute(
        "SELECT id, username, password_hash, is_active FROM users WHERE username = ?",
        (username,),
    ).fetchone()


def create_session(conn: sqlite3.Connection, user_id: int) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    expires_at = now + SESSION_TTL_SECONDS
    conn.execute(
        "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, now, expires_at),
    )
    conn.commit()
    return token, expires_at


def delete_session(conn: sqlite3.Connection, token: str):
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()


def get_current_user(request: Request):
    token = extract_auth_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    now = int(time.time())
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        row = conn.execute(
            """
            SELECT u.id, u.username, u.is_active
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires_at > ?
            """,
            (token, now),
        ).fetchone()
        if not row or not row["is_active"]:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return {"id": row["id"], "username": row["username"], "token": token}


def get_user_by_token(token: str):
    token = (token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    now = int(time.time())
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        row = conn.execute(
            """
            SELECT u.id, u.username, u.is_active
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires_at > ?
            """,
            (token, now),
        ).fetchone()
        if not row or not row["is_active"]:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return {"id": row["id"], "username": row["username"], "token": token}


@app.get("/map/static")
def map_static(
    request: Request,
    center: str = Query(..., description="center as lng,lat in bd09"),
    markers: str = Query(..., description="marker list as lng,lat|lng,lat in bd09"),
    width: int = Query(960, ge=200, le=1600),
    height: int = Query(640, ge=200, le=1200),
    zoom: int = Query(13, ge=3, le=18),
    token: str = Query("", description="session token for image tag requests"),
):
    effective_token = (token or "").strip() or extract_auth_token(request)
    _user = get_user_by_token(effective_token)
    url = build_baidu_static_map_url(
        center=center, markers=markers, width=width, height=height, zoom=zoom
    )
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "image" not in content_type.lower():
            body_preview = resp.text[:500]
            log_exception_to_terminal(
                "map_static returned non-image response",
                center=center,
                zoom=zoom,
                content_type=content_type,
                response_body=body_preview,
                request_url=url,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Baidu static map error: {body_preview}",
            )
    except HTTPException:
        raise
    except Exception:
        body_preview = ""
        status_code = None
        if "resp" in locals():
            status_code = resp.status_code
            try:
                body_preview = resp.text[:500]
            except Exception:
                body_preview = "<non-text response>"
        log_exception_to_terminal(
            "map_static failed",
            center=center,
            zoom=zoom,
            status_code=status_code,
            response_body=body_preview,
            request_url=url,
        )
        detail = "Baidu static map unavailable"
        if status_code:
            detail = f"Baidu static map unavailable: HTTP {status_code}"
        if body_preview:
            detail = f"{detail} | {body_preview}"
        raise HTTPException(status_code=502, detail=detail)
    return Response(
        content=resp.content,
        media_type=resp.headers.get("Content-Type", "image/png"),
    )


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def login(body: LoginRequest, response: Response):
    username = (body.username or "").strip()
    password = body.password or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="Missing username/password")

    with get_db() as conn:
        user = get_user_by_username(conn, username)
        if not user or not user["is_active"] or not verify_password(password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        token, expires_at = create_session(conn, user["id"])
        response.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=token,
            max_age=SESSION_TTL_SECONDS,
            expires=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=False,
            path="/",
        )
        return {"token": token, "username": user["username"], "expires_at": expires_at}


@app.post("/auth/logout")
def logout(response: Response, user=Depends(get_current_user)):
    with get_db() as conn:
        delete_session(conn, user["token"])
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    response.delete_cookie("authToken", path="/")
    return {"ok": True}


@app.get("/auth/me")
def me(user=Depends(get_current_user)):
    return {"username": user["username"]}


def fetch_hotspot_candidates(city: str):
    """
    用城市行政边界查商场/百货/集市/写字楼作为热点候选（严格按边界）
    目前支持北京、惠州；查不到边界则返回空列表
    """
    area_id = fetch_admin_area_id(city)
    if not area_id:
        return []

    query = f"""
    [out:json][timeout:25];
    (
      nwr["shop"="mall"](area:{area_id});
      nwr["shop"="department_store"](area:{area_id});
      nwr["amenity"="marketplace"](area:{area_id});
      nwr["building"="office"](area:{area_id});
      nwr["amenity"="business_centre"](area:{area_id});
    );
    out center;
    """
    try:
        elements = overpass_request(query)
    except Exception:
        log_exception_to_terminal("fetch_hotspots failed", city=city)
        return []
    hotspots = []
    for el in elements:
        tags = el.get("tags", {})
        center = el.get("center") or {}
        lat = el.get("lat") or center.get("lat")
        lon = el.get("lon") or center.get("lon")
        if lat is None or lon is None:
            continue
        if (
            tags.get("building") == "office"
            or tags.get("amenity") == "business_centre"
        ) and not tags.get("name"):
            continue
        hotspots.append(
            {
                "id": el.get("id"),
                "name": tags.get("name", "未命名地点"),
                "lat": lat,
                "lon": lon,
                "tags": tags,
                "type": tags.get("shop")
                or tags.get("amenity")
                or ("office" if tags.get("building") == "office" else None),
            }
        )
    return hotspots


def fetch_attractors(city: str):
    """
    扩大人流吸引业态，除了咖啡/甜品，加入餐饮、酒吧、面包房等
    同时考虑写字楼/商务中心密度，作为人流来源
    目前支持北京、惠州（严格按行政边界）
    """
    area_id = fetch_admin_area_id(city)
    if not area_id:
        return []

    # amenity/ shop tag 映射到简要分类名称，便于前端展示
    amenity_map = {
        "cafe": "咖啡",
        "fast_food": "快餐",
        "ice_cream": "冰淇淋",
        "restaurant": "餐厅",
        "food_court": "美食广场",
        "bar": "酒吧",
        "pub": "小酒馆",
        "biergarten": "啤酒花园",
        "tea": "茶馆",
        "bubble_tea": "奶茶",
        "cinema": "电影院",
        "theatre": "剧院",
    }
    shop_map = {
        "bakery": "面包房",
        "confectionery": "甜品店",
        "chocolate": "巧克力店",
        "deli": "熟食店",
    }
    # 写字楼/商务中心
    office_filters = [
        f'      nwr["building"="office"](area:{area_id});',
        f'      nwr["amenity"="business_centre"](area:{area_id});',
    ]

    amenity_filters = "\n".join(
        [f'      nwr["amenity"="{k}"](area:{area_id});' for k in amenity_map]
    )
    shop_filters = "\n".join(
        [f'      nwr["shop"="{k}"](area:{area_id});' for k in shop_map]
    )
    query = f"""
    [out:json][timeout:25];
    (
{amenity_filters}
{shop_filters}
{chr(10).join(office_filters)}
    );
    out center;
    """
    try:
        elements = overpass_request(query)
    except Exception:
        log_exception_to_terminal("fetch_attractors failed", city=city)
        return []
    venues = []
    for el in elements:
        tags = el.get("tags", {})
        center = el.get("center") or {}
        lat = el.get("lat") or center.get("lat")
        lon = el.get("lon") or center.get("lon")
        if lat is None or lon is None:
            continue
        amenity = tags.get("amenity")
        shop = tags.get("shop")
        is_office = tags.get("building") == "office"
        is_biz = tags.get("amenity") == "business_centre"
        category = (
            amenity_map.get(amenity)
            or shop_map.get(shop)
            or ("写字楼" if is_office else None)
            or ("商务中心" if is_biz else None)
            or amenity
            or shop
        )
        # 写字楼/商务中心大量缺 name，用地址或坐标作兜底，避免全是“未命名”
        name = tags.get("name")
        if not name and (is_office or is_biz):
            name = format_address(tags, lat, lon)
        if not name:
            name = "未命名店铺"
        venues.append(
            {
                "id": el.get("id"),
                "name": name,
                "lat": lat,
                "lon": lon,
                "tags": tags,
                "category": category,
            }
        )
    return venues


# ---------------- 距离计算（米） ----------------


def haversine_distance_m(lat1, lon1, lat2, lon2):
    # Haversine 公式计算两点球面距离（米）
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(
        dlambda / 2
    ) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


# ---------------- 坐标系转换 WGS84 -> GCJ02 / BD09 ----------------

PI = 3.14159265358979323846
AXIS = 6378245.0
EE = 0.00669342162296594323


def _out_of_china(lat, lon):
    # 判断坐标是否在国界外，国外无需坐标偏移
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x, y):
    ret = (
        -100.0
        + 2.0 * x
        + 3.0 * y
        + 0.2 * y * y
        + 0.1 * x * y
        + 0.2 * math.sqrt(abs(x))
    )
    ret += (
        (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    )
    ret += (
        (20.0 * math.sin(y * PI) + 40.0 * math.sin(y / 3.0 * PI)) * 2.0 / 3.0
    )
    ret += (
        (160.0 * math.sin(y / 12.0 * PI) + 320 * math.sin(y * PI / 30.0)) * 2.0 / 3.0
    )
    return ret


def _transform_lon(x, y):
    ret = (
        300.0
        + x
        + 2.0 * y
        + 0.1 * x * x
        + 0.1 * x * y
        + 0.1 * math.sqrt(abs(x))
    )
    ret += (
        (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    )
    ret += (
        (20.0 * math.sin(x * PI) + 40.0 * math.sin(x / 3.0 * PI)) * 2.0 / 3.0
    )
    ret += (
        (150.0 * math.sin(x / 12.0 * PI) + 300.0 * math.sin(x / 30.0 * PI))
        * 2.0
        / 3.0
    )
    return ret


def wgs84_to_gcj02(lat, lon):
    if _out_of_china(lat, lon):
        return lat, lon
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * PI
    magic = math.sin(radlat)
    magic = 1 - EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((AXIS * (1 - EE)) / (magic * sqrtmagic) * PI)
    dlon = (dlon * 180.0) / (AXIS / sqrtmagic * math.cos(radlat) * PI)
    mglat = lat + dlat
    mglon = lon + dlon
    return mglat, mglon


def gcj02_to_bd09(lat, lon):
    z = math.sqrt(lon * lon + lat * lat) + 0.00002 * math.sin(lat * PI * 3000.0 / 180.0)
    theta = math.atan2(lat, lon) + 0.000003 * math.cos(lon * PI * 3000.0 / 180.0)
    bd_lon = z * math.cos(theta) + 0.0065
    bd_lat = z * math.sin(theta) + 0.006
    return bd_lat, bd_lon


def wgs84_to_bd09(lat, lon):
    # 先转 GCJ-02 再转 BD-09
    lat_gcj, lon_gcj = wgs84_to_gcj02(lat, lon)
    return gcj02_to_bd09(lat_gcj, lon_gcj)


def bd09_to_gcj02(lat, lon):
    x = lon - 0.0065
    y = lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * PI * 3000.0 / 180.0)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * PI * 3000.0 / 180.0)
    gcj_lon = z * math.cos(theta)
    gcj_lat = z * math.sin(theta)
    return gcj_lat, gcj_lon


def gcj02_to_wgs84(lat, lon):
    if _out_of_china(lat, lon):
        return lat, lon
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * PI
    magic = math.sin(radlat)
    magic = 1 - EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((AXIS * (1 - EE)) / (magic * sqrtmagic) * PI)
    dlon = (dlon * 180.0) / (AXIS / sqrtmagic * math.cos(radlat) * PI)
    mglat = lat + dlat
    mglon = lon + dlon
    return lat * 2 - mglat, lon * 2 - mglon


def bd09_to_wgs84(lat, lon):
    lat_gcj, lon_gcj = bd09_to_gcj02(lat, lon)
    return gcj02_to_wgs84(lat_gcj, lon_gcj)


# ---------------- 构建热点数据 ----------------


def build_hotspots(city: str, radius_m: int = 1200, limit: int = 30):
    """
    逻辑：
    1. 找出城市里的商场/百货/集市作为候选热点
    2. 找出城市里的“人流吸引”业态（餐饮/酒吧/咖啡/甜品等）
    3. 对每个候选热点，统计半径 radius_m 内的店铺数量作为 hot_score
    4. 按 hot_score 排序，取前 limit 个
    """
    hotspots = fetch_hotspot_candidates(city)
    venues = fetch_attractors(city)

    result = []
    for spot in hotspots:
        lat_s, lon_s = spot["lat"], spot["lon"]
        nearby_venues = []

        for v in venues:
            d = haversine_distance_m(lat_s, lon_s, v["lat"], v["lon"])
            if d <= radius_m:
                nearby_venues.append(v)

        score = len(nearby_venues)
        if score == 0:
            continue

        spot["hot_score"] = score
        result.append(
            {
                "spot": spot,
                "venues": nearby_venues,
            }
        )

    result.sort(key=lambda x: x["spot"]["hot_score"], reverse=True)
    return result[:limit]


BEIJING_DISTRICTS = [
    "东城区",
    "西城区",
    "朝阳区",
    "海淀区",
    "丰台区",
    "石景山区",
    "通州区",
    "昌平区",
    "大兴区",
    "顺义区",
    "房山区",
    "门头沟区",
    "怀柔区",
    "平谷区",
    "密云区",
    "延庆区",
]

TIANANMEN_WGS84 = (39.9087, 116.3975)
BEIJING_5TH_RING_RADIUS_M = 15000


def infer_beijing_district(tags: dict, lat: float, lon: float) -> str:
    direct = (
        (tags or {}).get("addr:district")
        or (tags or {}).get("district")
        or (tags or {}).get("is_in:district")
        or ""
    ).strip()
    if direct:
        for district in BEIJING_DISTRICTS:
            if district in direct:
                return district

    address = format_address(tags or {}, lat, lon)
    for district in BEIJING_DISTRICTS:
        if district in address:
            return district
    return "未识别区"


def is_retail_friendly_candidate(spot: dict, address: str) -> bool:
    name = (spot.get("name") or "").strip()
    spot_type = (spot.get("type") or "").strip()
    text = f"{name} {address}"

    if not name or name.startswith("未命名"):
        return False
    if address.startswith("坐标："):
        return False

    banned_keywords = [
        "政府",
        "委员会",
        "公安",
        "法院",
        "检察院",
        "中学",
        "小学",
        "大学",
        "医院",
        "研究院",
        "实验室",
        "部",
        "局",
        "署",
        "大使馆",
        "总队",
    ]
    if any(keyword in text for keyword in banned_keywords):
        return False

    if spot_type in {"mall", "department_store", "marketplace", "business_centre"}:
        return True

    if spot_type == "office":
        office_like_keywords = [
            "中心",
            "广场",
            "大厦",
            "大楼",
            "天地",
            "城",
            "汇",
            "广场",
            "商务",
        ]
        return any(keyword in name for keyword in office_like_keywords)

    return False


def is_premium_office_candidate(spot: dict, address: str) -> bool:
    name = (spot.get("name") or "").strip()
    spot_type = (spot.get("type") or "").strip()
    text = f"{name} {address}"

    premium_keywords = [
        "国贸",
        "金融街",
        "金融中心",
        "国际金融",
        "财富中心",
        "环球",
        "银泰",
        "华贸",
        "嘉里",
        "SKP",
        "世贸",
        "丽泽",
        "总部基地",
        "企业天地",
        "CBD",
    ]
    if any(keyword.lower() in text.lower() for keyword in premium_keywords):
        return True

    return spot_type in {"office", "business_centre"} and (
        "国际" in text or "金融" in text or "总部" in text or "甲级" in text
    )


def build_mengniu_beijing_candidates(limit: int = 50, radius_m: int = 1800):
    groups = build_hotspots("Beijing", radius_m=radius_m, limit=2600)
    selected = []

    drink_amenities = {"cafe", "tea", "bubble_tea", "ice_cream"}
    drink_shops = {"bakery", "confectionery", "chocolate", "deli"}
    food_amenities = {"restaurant", "fast_food", "food_court", "bar", "pub", "biergarten"}
    leisure_amenities = {"cinema", "theatre"}

    for group in groups:
        spot = group["spot"]
        venues = group["venues"]
        address = format_address(spot.get("tags") or {}, spot["lat"], spot["lon"])

        if (
            haversine_distance_m(
                TIANANMEN_WGS84[0],
                TIANANMEN_WGS84[1],
                spot["lat"],
                spot["lon"],
            )
            > BEIJING_5TH_RING_RADIUS_M
        ):
            continue

        if not is_retail_friendly_candidate(spot, address):
            continue

        premium_office = is_premium_office_candidate(spot, address)

        office_count = 0
        drink_count = 0
        food_count = 0
        leisure_count = 0
        category_names = set()

        for venue in venues:
            tags = venue.get("tags", {})
            amenity = tags.get("amenity")
            shop = tags.get("shop")
            if tags.get("building") == "office" or amenity == "business_centre":
                office_count += 1
            if amenity in drink_amenities or shop in drink_shops:
                drink_count += 1
            if amenity in food_amenities:
                food_count += 1
            if amenity in leisure_amenities:
                leisure_count += 1
            if venue.get("category"):
                category_names.add(venue["category"])

        if len(venues) < 6:
            continue

        score = (
            len(venues) * 1.6
            + office_count * 3.2
            + drink_count * 2.2
            + food_count * 2.1
            + leisure_count * 2.4
            + len(category_names) * 3.0
        )
        spot_type = spot.get("type") or ""
        if spot_type in {"mall", "department_store", "marketplace"}:
            score += 8
        if spot_type in {"office", "business_centre"}:
            score += 2
        if office_count >= 8:
            score += 4
        elif office_count >= 5:
            score += 2
        elif office_count < 2:
            score -= 8
        if (drink_count + food_count) < 6:
            score -= 6
        if premium_office:
            score -= 14
            if (drink_count + food_count + leisure_count) < 10:
                continue

        if spot_type in {"mall", "department_store"}:
            scene_type = "商圈店优先区"
        elif spot_type == "marketplace":
            scene_type = "街区店优先区"
        elif office_count >= 6 and food_count >= 8:
            scene_type = "高密办公餐饮复合区"
        elif drink_count >= 5 and food_count >= 6:
            scene_type = "饮品餐饮活跃街区"
        elif office_count >= 3 and leisure_count >= 1:
            scene_type = "办公娱乐复合区"
        else:
            scene_type = "成熟商住混合片区"

        selected.append(
            {
                "spot": spot,
                "address": address,
                "district": infer_beijing_district(spot.get("tags") or {}, spot["lat"], spot["lon"]),
                "score": round(score, 1),
                "office_count": office_count,
                "drink_count": drink_count,
                "food_count": food_count,
                "leisure_count": leisure_count,
                "category_count": len(category_names),
                "scene_type": scene_type,
                "spot_type": spot_type,
            }
        )

    selected.sort(key=lambda item: item["score"], reverse=True)

    def build_candidate_summary(item: dict) -> dict:
        spot = item["spot"]
        return {
            "name": spot.get("name"),
            "address": item.get("address"),
            "district": item.get("district"),
            "score": item.get("score"),
            "scene_type": item.get("scene_type"),
            "hot_score": spot.get("hot_score"),
            "office_count": item.get("office_count"),
            "drink_count": item.get("drink_count"),
            "food_count": item.get("food_count"),
            "leisure_count": item.get("leisure_count"),
            "category_count": item.get("category_count"),
            "spot_type": item.get("spot_type"),
            "location_wgs84": {
                "lat": spot["lat"],
                "lng": spot["lon"],
            },
        }

    def cluster_candidates(items: list[dict], cluster_radius_m: int) -> list[dict]:
        clustered_items = []
        for item in items:
            lat = item["spot"]["lat"]
            lon = item["spot"]["lon"]
            merged_into_existing = False
            for cluster in clustered_items:
                d = haversine_distance_m(
                    lat,
                    lon,
                    cluster["spot"]["lat"],
                    cluster["spot"]["lon"],
                )
                if d < cluster_radius_m:
                    cluster["merged_count"] = cluster.get("merged_count", 1) + 1
                    cluster.setdefault("merged_candidates", []).append(
                        build_candidate_summary(item)
                    )
                    cluster["merged_candidates"].extend(item.get("merged_candidates", []))
                    merged_into_existing = True
                    break
            if merged_into_existing:
                continue
            item_copy = dict(item)
            item_copy["merged_count"] = item.get("merged_count", 1)
            item_copy["merged_candidates"] = list(item.get("merged_candidates", []))
            clustered_items.append(item_copy)
        return clustered_items

    selected_before_cluster = selected
    selected = []
    for cluster_radius_m in (1500, 1200, 900, 700, 500):
        clustered = cluster_candidates(selected_before_cluster, cluster_radius_m)
        selected = clustered
        if len(clustered) >= max(limit, 40):
            break

    district_buckets: dict[str, list[dict]] = {}
    for item in selected:
        district_buckets.setdefault(item["district"], []).append(item)
    for bucket in district_buckets.values():
        bucket.sort(key=lambda item: item["score"], reverse=True)

    spread = []
    district_take_count = {district: 0 for district in district_buckets}
    recognized_districts = [
        district for district in BEIJING_DISTRICTS if district in district_buckets
    ]
    if not recognized_districts:
        recognized_districts = sorted(district_buckets.keys())
    base_quota = max(1, min(3, limit // max(len(recognized_districts), 1)))
    remainder = max(0, limit - base_quota * len(recognized_districts))
    district_extra_quota = {
        district: 1 if idx < remainder else 0
        for idx, district in enumerate(
            sorted(
                recognized_districts,
                key=lambda district: district_buckets[district][0]["score"]
                if district_buckets[district]
                else -1,
                reverse=True,
            )
        )
    }
    district_max = {
        district: base_quota + district_extra_quota.get(district, 0)
        for district in district_buckets
    }

    def can_select(candidate: dict, min_distance_m: int) -> bool:
        lat = candidate["spot"]["lat"]
        lon = candidate["spot"]["lon"]
        for chosen in spread:
            d = haversine_distance_m(
                lat, lon, chosen["spot"]["lat"], chosen["spot"]["lon"]
            )
            if d < min_distance_m:
                return False
        return True

    def pick_from_bucket(bucket: list[dict], distance_steps: list[int]):
        for min_distance_m in distance_steps:
            for item in bucket:
                if can_select(item, min_distance_m):
                    bucket.remove(item)
                    return item
        return None

    # First pass: guarantee a near-even district coverage.
    for round_idx in range(base_quota):
        for district in recognized_districts:
            bucket = district_buckets.get(district, [])
            if district_take_count[district] >= base_quota:
                continue
            picked = pick_from_bucket(bucket, [1300, 1100, 900, 700, 500, 300])
            if picked is None:
                continue
            spread.append(picked)
            district_take_count[district] += 1
            if len(spread) >= limit:
                break
        if len(spread) >= limit:
            break

    # Allocate the remainder to stronger districts while still trying to stay balanced.
    for district in sorted(
        recognized_districts,
        key=lambda district: district_buckets[district][0]["score"]
        if district_buckets[district]
        else -1,
        reverse=True,
    ):
        target = base_quota + district_extra_quota.get(district, 0)
        bucket = district_buckets.get(district, [])
        while (
            len(spread) < limit
            and district_take_count.get(district, 0) < target
            and bucket
        ):
            picked = pick_from_bucket(bucket, [1500, 1200, 900, 700, 500, 300, 0])
            if picked is None:
                break
            spread.append(picked)
            district_take_count[district] = district_take_count.get(district, 0) + 1

    # Second pass: fill remaining slots by district quality while capping concentration.
    district_priority = sorted(
        district_buckets.keys(),
        key=lambda district: district_buckets[district][0]["score"]
        if district_buckets[district]
        else -1,
        reverse=True,
    )

    keep_picking = True
    while keep_picking and len(spread) < limit:
        keep_picking = False
        for district in district_priority:
            bucket = district_buckets.get(district, [])
            if not bucket:
                continue
            if district_take_count.get(district, 0) >= district_max.get(district, base_quota):
                continue
            picked = pick_from_bucket(bucket, [1500, 1200, 900, 700, 500, 300, 0])
            if picked is None:
                continue
            spread.append(picked)
            district_take_count[district] = district_take_count.get(district, 0) + 1
            keep_picking = True
            if len(spread) >= limit:
                break

    # Final pass: if some districts are sparse, relax slightly to complete the list.
    if len(spread) < limit:
        for item in selected:
            if item in spread:
                continue
            district = item["district"]
            if district_take_count.get(district, 0) >= district_max.get(district, base_quota) + 1:
                continue
            if not can_select(item, 250):
                continue
            spread.append(item)
            district_take_count[district] = district_take_count.get(district, 0) + 1
            if len(spread) >= limit:
                break

    # Hard fill to exactly limit if the city data itself is dense enough.
    if len(spread) < limit:
        for item in selected:
            if item in spread:
                continue
            spread.append(item)
            if len(spread) >= limit:
                break

    output = []
    for idx, item in enumerate(spread, start=1):
        spot = item["spot"]
        office_count = sum(
            1
            for v in venues
            if (v.get("tags") or {}).get("building") == "office"
            or (v.get("tags") or {}).get("amenity") == "business_centre"
        )

        bd_lat, bd_lon = wgs84_to_bd09(spot["lat"], spot["lon"])
        gcj_lat, gcj_lon = wgs84_to_gcj02(spot["lat"], spot["lon"])
        output.append(
            {
                "rank": idx,
                "name": spot["name"],
                "address": item["address"],
                "district": item["district"],
                "location": {"lat": gcj_lat, "lng": gcj_lon},
                "location_gcj02": {"lat": gcj_lat, "lng": gcj_lon},
                "location_bd09": {"lat": bd_lat, "lng": bd_lon},
                "coord_type": "gcj02",
                "score": item["score"],
                "scene_type": item["scene_type"],
                "hot_score": spot["hot_score"],
                "office_count": item["office_count"],
                "drink_count": item["drink_count"],
                "food_count": item["food_count"],
                "leisure_count": item["leisure_count"],
                "category_count": item["category_count"],
                "merged_count": item.get("merged_count", 1),
                "merged_candidates": [
                    {
                        **merged_item,
                        "location": {
                            "lat": wgs84_to_gcj02(
                                merged_item["location_wgs84"]["lat"],
                                merged_item["location_wgs84"]["lng"],
                            )[0],
                            "lng": wgs84_to_gcj02(
                                merged_item["location_wgs84"]["lat"],
                                merged_item["location_wgs84"]["lng"],
                            )[1],
                        },
                    }
                    for merged_item in item.get("merged_candidates", [])
                ],
            }
        )

    return {
        "city": "Beijing",
        "brief": {
            "brand": "蒙牛 YogurtDay",
            "page_goal": "基于 brief 规则生成北京 50 个候选长名单点位",
            "store_type": "POP 店型，约 45㎡（含座位区）",
            "radius_m": radius_m,
            "rules": [
                "聚焦北京核心商圈/街区资源丰富区域",
                "优先商住混合且饮品、餐饮、办公复合明显的片区",
                "按 1.5km 生活圈和商圈活跃度做候选点排序",
                "当前结果是长名单，后续还需租金、财测、竞争进一步复核",
            ],
        },
        "candidates": output,
    }


# ---------------- 对外接口 ----------------


@app.get("/analyze_city")
def analyze_city(
    city: str = Query(..., description="城市名，例如 Beijing / 北京 / 惠州"),
    refresh: bool = Query(False, description="是否强制刷新缓存"),
    _user=Depends(get_current_user),
):
    cached_entry = None if refresh else load_cache_entry(city)
    if cached_entry and has_non_empty_hotspots(cached_entry["data"]):
        return cached_entry["data"]

    try:
        data = build_hotspots(city, radius_m=1200, limit=30)
    except Exception:
        log_exception_to_terminal("analyze_city failed", city=city, refresh=refresh)
        raise HTTPException(status_code=502, detail="Overpass API unavailable, please retry")

    output = []
    for group in data:
        spot = group["spot"]
        venues = group["venues"]

        # 统计这个热点范围内的瑞幸门店数量
        luckin_count = sum(
            1
            for v in venues
            if "瑞幸" in (v.get("name") or "")
            or "luckin" in (v.get("name") or "").lower()
        )
        office_count = sum(
            1
            for v in venues
            if (v.get("tags") or {}).get("building") == "office"
            or (v.get("tags") or {}).get("amenity") == "business_centre"
        )

        bd_lat, bd_lon = wgs84_to_bd09(spot["lat"], spot["lon"])
        gcj_lat, gcj_lon = wgs84_to_gcj02(spot["lat"], spot["lon"])
        spot_out = {
            "name": spot["name"],
            "address": format_address(spot["tags"], spot["lat"], spot["lon"]),
            "location": {"lat": bd_lat, "lng": bd_lon},
            "location_bd09": {"lat": bd_lat, "lng": bd_lon},
            "location_gcj02": {"lat": gcj_lat, "lng": gcj_lon},
            "coord_type": "gcj02",
            "hot_score": spot["hot_score"],
        }

        venues_out = []
        for v in venues:
            v_bd_lat, v_bd_lon = wgs84_to_bd09(v["lat"], v["lon"])
            v_gcj_lat, v_gcj_lon = wgs84_to_gcj02(v["lat"], v["lon"])
            venues_out.append(
                {
                    "name": v["name"],
                    "category": v.get("category"),
                    "location": {"lat": v_bd_lat, "lng": v_bd_lon},
                    "location_bd09": {"lat": v_bd_lat, "lng": v_bd_lon},
                    "location_gcj02": {"lat": v_gcj_lat, "lng": v_gcj_lon},
                    "coord_type": "gcj02",
                    "tags": v["tags"],
                }
            )

        output.append(
            {
                "spot": spot_out,
                "venues": venues_out,
                "luckin_count": luckin_count,
                "office_count": office_count,
            }
        )

    resp = {"city": city, "hotspots": output}
    if output:
        save_cache(city, resp)
    return resp


@app.get("/mengniu/beijing_candidates")
def mengniu_beijing_candidates(
    refresh: bool = Query(False, description="是否强制刷新结果"),
    _user=Depends(get_current_user),
):
    cache_key = "mengniu_beijing_candidates"
    cached_entry = None if refresh else load_cache_entry(cache_key)
    if cached_entry:
        cached_data = dict(cached_entry["data"])
        cached_data["source"] = "cache"
        cached_data["cached_at"] = cached_entry["updated_at"]
        return cached_data

    try:
        resp = build_mengniu_beijing_candidates(limit=50, radius_m=1800)
    except Exception:
        log_exception_to_terminal("mengniu_beijing_candidates failed", refresh=refresh)
        raise HTTPException(status_code=502, detail="Failed to build Mengniu Beijing candidates")

    save_cache(cache_key, resp)
    resp = dict(resp)
    resp["source"] = "recomputed"
    resp["cached_at"] = int(time.time())
    return resp


@app.get("/beijing_offices")
def beijing_offices(
    district: str | None = Query(None, description="按北京区域筛选，例如 朝阳区"),
    _user=Depends(get_current_user),
):
    try:
        return fetch_beijing_office_buildings(district=district)
    except sqlite3.Error:
        log_exception_to_terminal("beijing_offices query failed", district=district)
        raise HTTPException(status_code=500, detail="Failed to load Beijing office buildings")


@app.get("/beijing_flow_heatmap")
def beijing_flow_heatmap(
    district: str | None = Query(None, description="鎸夊寳浜尯鍩熺瓫閫夊紑婧愪汉娴佷唬鐞嗙儹鍔涘浘"),
    _user=Depends(get_current_user),
):
    try:
        payload = build_beijing_flow_proxy()
        points = payload.get("points") or []
        if district:
            points = [
                item
                for item in points
                if (item.get("district") or "").strip() == district.strip()
            ]
        category_counts = {}
        for item in points:
            category = item.get("category") or "other"
            category_counts[category] = category_counts.get(category, 0) + 1
        return {
            "source": payload.get("source"),
            "cached_at": payload.get("cached_at"),
            "description": payload.get("description"),
            "district": district,
            "count": len(points),
            "points": points,
            "stats": {
                "total_weight": round(
                    sum(float(item.get("weight") or 0) for item in points), 2
                ),
                "categories": category_counts,
            },
        }
    except HTTPException:
        raise
    except Exception:
        log_exception_to_terminal("beijing_flow_heatmap failed", district=district)
        raise HTTPException(status_code=500, detail="Failed to load Beijing flow heatmap")


@app.get("/evaluate_point")
def evaluate_point(
    city: str = Query(..., description="城市名，例如 Beijing / 北京 / 惠州"),
    lat: float = Query(..., description="纬度（默认 BD-09，可用 coord_type 指定）"),
    lng: float = Query(..., description="经度（默认 BD-09，可用 coord_type 指定）"),
    coord_type: str = Query(
        "bd09", description="坐标类型：bd09（默认）/ gcj02 / wgs84"
    ),
    radius_m: int = Query(1200, ge=100, le=5000, description="分析半径，米"),
    _user=Depends(get_current_user),
):
    """
    针对单个点位，计算半径内的业态/写字楼/瑞幸分布。
    """
    coord_type = (coord_type or "bd09").lower()
    if coord_type == "bd09":
        lat_wgs, lng_wgs = bd09_to_wgs84(lat, lng)
    elif coord_type == "gcj02":
        lat_wgs, lng_wgs = gcj02_to_wgs84(lat, lng)
    elif coord_type == "wgs84":
        lat_wgs, lng_wgs = lat, lng
    else:
        raise HTTPException(status_code=400, detail="coord_type 必须是 bd09/gcj02/wgs84")

    venues = fetch_attractors(city)
    if not venues:
        return {
            "city": city,
            "point": {"lat": lat, "lng": lng, "coord_type": coord_type},
            "note": "未找到业态数据（可能 Overpass 暂不可用或城市不支持）",
            "stats": {},
        }

    nearby = []
    for v in venues:
        d = haversine_distance_m(lat_wgs, lng_wgs, v["lat"], v["lon"])
        if d <= radius_m:
            nearby.append({**v, "distance_m": d})

    # 统计
    luckin = [n for n in nearby if "瑞幸" in (n.get("name") or "") or "luckin" in (n.get("name") or "").lower()]
    offices = [n for n in nearby if n.get("category") in ("写字楼", "商务中心")]
    cat_stats = {}
    for n in nearby:
        cat = n.get("category") or "其他"
        cat_stats[cat] = cat_stats.get(cat, 0) + 1

    nearest_luckin = None
    if luckin:
        nearest_luckin = min(luckin, key=lambda x: x["distance_m"])

    bd_lat, bd_lng = wgs84_to_bd09(lat_wgs, lng_wgs)
    return {
        "city": city,
        "point": {
            "lat": bd_lat,
            "lng": bd_lng,
            "coord_type": "bd09",
            "radius_m": radius_m,
        },
        "counts": {
            "total_venues": len(nearby),
            "luckin": len(luckin),
            "offices": len(offices),
        },
        "categories": cat_stats,
        "nearest_luckin": nearest_luckin,
        "venues": nearby,
    }


if __name__ == "__main__":
    import argparse
    import copy
    import uvicorn
    from uvicorn.config import LOGGING_CONFIG

    parser = argparse.ArgumentParser(description="Run Luckin site analysis service.")
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Run in production mode (host 0.0.0.0, reload off)",
    )
    args = parser.parse_args()

    host = "0.0.0.0" if args.prod else "127.0.0.1"
    reload = not args.prod

    logs_dir = base_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    app_log_path = logs_dir / "server.log"
    access_log_path = logs_dir / "access.log"

    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config["formatters"]["default"]["fmt"] = "%(asctime)s %(levelprefix)s %(message)s"
    log_config["formatters"]["default"]["datefmt"] = "%Y-%m-%d %H:%M:%S"
    log_config["formatters"]["access"]["fmt"] = (
        '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
    )
    log_config["formatters"]["access"]["datefmt"] = "%Y-%m-%d %H:%M:%S"
    log_config["handlers"]["file_default"] = {
        "class": "logging.FileHandler",
        "formatter": "default",
        "filename": str(app_log_path),
        "encoding": "utf-8",
    }
    log_config["handlers"]["file_access"] = {
        "class": "logging.FileHandler",
        "formatter": "access",
        "filename": str(access_log_path),
        "encoding": "utf-8",
    }
    log_config["loggers"]["uvicorn"]["handlers"] = ["default", "file_default"]
    log_config["loggers"]["uvicorn.error"]["handlers"] = ["default", "file_default"]
    log_config["loggers"]["uvicorn.access"]["handlers"] = ["access", "file_access"]
    log_config.setdefault("root", {"handlers": ["default"], "level": "INFO"})
    log_config["root"]["handlers"] = ["default", "file_default"]

    uvicorn.run("main:app", host=host, port=8000, reload=reload, log_config=log_config)
