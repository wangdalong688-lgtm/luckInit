import math
import os
import sqlite3
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "cache.sqlite3"
TABLE_NAME = "beijing_office_buildings"
AMAP_WEB_KEY = os.getenv("AMAP_WEB_KEY", "32a6ad4d5a62f38f90ac4674457058ff").strip()
REQUEST_INTERVAL_SECONDS = 0.12
REQUEST_TIMEOUT_SECONDS = 20
REQUEST_RETRY_COUNT = 2
REQUEST_RETRY_DELAY_SECONDS = 1.2
BEIJING_CITY = "北京市"
DISTRICT_CITY_ALIASES = {
    "广阳区": "廊坊市",
    "三河市": "廊坊市",
}

ADDRESS_ALIASES = {
    "总部基地18区西区": ["北京市丰台区总部基地18区西区", "北京市丰台区总部基地18区"],
    "北京方向": ["北京市丰台区北京方向"],
    "金融街·万科丰科中心": ["北京市丰台区金融街·万科丰科中心", "北京市丰台区丰科中心"],
    "盈坤世纪区域1": ["北京市丰台区盈坤世纪区域1", "北京市丰台区盈坤世纪"],
    "时代财富天地CDE座": ["北京市丰台区时代财富天地CDE座", "北京市丰台区时代财富天地"],
    "财富广场": ["北京市丰台区国投财富广场", "北京市丰台区财富广场"],
}

PI = math.pi
AXIS = 6378245.0
EE = 0.00669342162296594323

SCHEMA_UPDATES = [
    ("gcj02_longitude", "REAL"),
    ("gcj02_latitude", "REAL"),
    ("geo_confidence", "TEXT"),
    ("geo_method", "TEXT"),
    ("geo_query", "TEXT"),
    ("geo_match_address", "TEXT"),
    ("geo_match_level", "TEXT"),
    ("geo_score", "REAL"),
]

AMBIGUOUS_TOKENS = (
    "区域",
    "西区",
    "东区",
    "南区",
    "北区",
    "园区",
    "周边",
    "合并",
    "理论客群",
    "店客群",
    "客群",
    "A座",
    "B座",
    "C座",
    "D座",
    "T1",
    "T2",
    "T3",
)

GOOD_POI_TYPES = (
    "商务写字楼",
    "商住两用楼宇",
    "楼宇",
    "产业园区",
    "园区",
)

BAD_POI_TYPES = (
    "停车场",
    "公交",
    "地铁",
    "餐饮",
    "银行",
    "公司",
    "购物",
    "酒店",
    "培训",
    "生活服务",
    "体育休闲",
)


def _out_of_china(lat: float, lon: float) -> bool:
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x: float, y: float) -> float:
    ret = (
        -100.0
        + 2.0 * x
        + 3.0 * y
        + 0.2 * y * y
        + 0.1 * x * y
        + 0.2 * math.sqrt(abs(x))
    )
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * PI) + 40.0 * math.sin(y / 3.0 * PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * PI) + 320 * math.sin(y * PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = (
        300.0
        + x
        + 2.0 * y
        + 0.1 * x * x
        + 0.1 * x * y
        + 0.1 * math.sqrt(abs(x))
    )
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * PI) + 40.0 * math.sin(x / 3.0 * PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * PI) + 300.0 * math.sin(x / 30.0 * PI)) * 2.0 / 3.0
    return ret


def gcj02_to_wgs84(lat: float, lon: float) -> tuple[float, float]:
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


def normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    for token in ("·", ".", "（", "）", "(", ")", "-", "_", " "):
        text = text.replace(token, "")
    return text


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(a=a, b=b).ratio()


def looks_like_good_poi_type(type_text: str | None) -> bool:
    text = str(type_text or "")
    return any(token in text for token in GOOD_POI_TYPES)


def looks_like_bad_poi_type(type_text: str | None) -> bool:
    text = str(type_text or "")
    return any(token in text for token in BAD_POI_TYPES)


def is_ambiguous_name(name: str) -> bool:
    return any(token in name for token in AMBIGUOUS_TOKENS)


def ensure_schema() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        existing = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()
        }
        for column_name, column_type in SCHEMA_UPDATES:
            if column_name not in existing:
                conn.execute(
                    f"ALTER TABLE {TABLE_NAME} ADD COLUMN {column_name} {column_type}"
                )
        conn.commit()


def fetch_rows(only_missing: bool = True) -> list[sqlite3.Row]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        where_clause = (
            """
            WHERE longitude IS NULL
               OR latitude IS NULL
               OR gcj02_longitude IS NULL
               OR gcj02_latitude IS NULL
            """
            if only_missing else ""
        )
        return conn.execute(
            f"""
            SELECT id, district, location_name
            FROM {TABLE_NAME}
            {where_clause}
            ORDER BY id
            """
        ).fetchall()


def request_json_with_retry(session: requests.Session, url: str, params: dict) -> dict:
    last_exc = None
    for attempt in range(REQUEST_RETRY_COUNT + 1):
        try:
            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_exc = exc
            if attempt >= REQUEST_RETRY_COUNT:
                raise
            time.sleep(REQUEST_RETRY_DELAY_SECONDS * (attempt + 1))
    raise last_exc


def build_address_variants(district: str | None, location_name: str) -> list[str]:
    district = (district or "").strip()
    name = location_name.strip()
    if name in ADDRESS_ALIASES:
        return ADDRESS_ALIASES[name]

    city = resolve_city(district)
    values = []
    for item in (
        f"{city}{district}{name}",
        f"{city}{name}",
        f"北京{district}{name}",
        f"北京{name}",
    ):
        if item and item not in values:
            values.append(item)
    return values


def resolve_city(district: str | None) -> str:
    normalized = (district or "").strip()
    if not normalized:
        return BEIJING_CITY
    return DISTRICT_CITY_ALIASES.get(normalized, BEIJING_CITY)


def geocode_address(session: requests.Session, address: str, district: str | None = None) -> dict | None:
    city = resolve_city(district)
    payload = request_json_with_retry(
        session,
        "https://restapi.amap.com/v3/geocode/geo",
        {
            "key": AMAP_WEB_KEY,
            "address": address,
            "city": city,
            "output": "json",
        },
    )
    if payload.get("status") != "1":
        raise RuntimeError(f"{payload.get('info')} ({payload.get('infocode')})")
    geocodes = payload.get("geocodes") or []
    if not geocodes:
        return None
    result = dict(geocodes[0])
    result["_count"] = int(payload.get("count") or 0)
    result["_query"] = address
    return result


def search_place_candidates(session: requests.Session, district: str | None, location_name: str) -> list[dict]:
    keywords = f"{(district or '').strip()}{location_name.strip()}"
    city = resolve_city(district)
    payload = request_json_with_retry(
        session,
        "https://restapi.amap.com/v3/place/text",
        {
            "key": AMAP_WEB_KEY,
            "keywords": keywords,
            "city": city,
            "citylimit": "true",
            "offset": 10,
            "page": 1,
            "extensions": "all",
            "output": "json",
        },
    )
    if payload.get("status") != "1":
        raise RuntimeError(f"{payload.get('info')} ({payload.get('infocode')})")
    return payload.get("pois") or []


def score_place_candidate(location_name: str, district: str | None, candidate: dict) -> float:
    target = normalize_text(location_name)
    district_norm = normalize_text(district)
    name_norm = normalize_text(candidate.get("name"))
    address_norm = normalize_text(candidate.get("address"))
    adname_norm = normalize_text(candidate.get("adname"))
    type_text = candidate.get("type") or ""

    name_similarity = similarity(target, name_norm)
    address_similarity = similarity(target, address_norm)
    score = 0.0
    score += name_similarity * 0.65
    score += address_similarity * 0.10

    if target and target in name_norm:
        score += 0.24
    if district_norm and district_norm == adname_norm:
        score += 0.12
    elif district_norm and district_norm in address_norm:
        score += 0.06

    if looks_like_good_poi_type(type_text):
        score += 0.22
    if looks_like_bad_poi_type(type_text):
        score -= 0.45
    if target and target not in name_norm and name_similarity < 0.78:
        score -= 0.18
    if is_ambiguous_name(location_name):
        score -= 0.08

    return max(0.0, min(score, 0.99))


def choose_place_candidate(location_name: str, district: str | None, candidates: list[dict]) -> dict | None:
    best = None
    best_score = -1.0
    for candidate in candidates:
        location = candidate.get("location") or ""
        if "," not in location:
            continue
        score = score_place_candidate(location_name, district, candidate)
        if score > best_score:
            best = dict(candidate)
            best_score = score
    if not best:
        return None
    best["_score"] = best_score
    return best


def geocode_row(session: requests.Session, district: str | None, location_name: str) -> dict | None:
    place_candidates = search_place_candidates(session, district, location_name)
    place_best = choose_place_candidate(location_name, district, place_candidates)
    if place_best and float(place_best.get("_score") or 0) >= 0.55:
        return {
            "location": place_best["location"],
            "_query": f"{(district or '').strip()}{location_name.strip()}",
            "formatted_address": f"{place_best.get('adname') or ''}{place_best.get('address') or ''}",
            "level": place_best.get("type"),
            "_count": len(place_candidates),
            "_method": "amap_place_text",
            "_poi_name": place_best.get("name"),
            "_poi_type": place_best.get("type"),
            "_poi_score": place_best.get("_score"),
        }

    last_result = None
    for address in build_address_variants(district, location_name):
        result = geocode_address(session, address, district)
        if result and result.get("location"):
            result["_method"] = "amap_geocode"
            return result
        last_result = result
        time.sleep(REQUEST_INTERVAL_SECONDS)
    return last_result


def compute_confidence(location_name: str, district: str | None, match: dict) -> tuple[str, float]:
    method = match.get("_method") or "amap_geocode"
    if method == "amap_place_text":
        score = float(match.get("_poi_score") or 0.0)
        type_text = str(match.get("_poi_type") or "")
        if looks_like_bad_poi_type(type_text):
            score = min(score, 0.45)
        elif looks_like_good_poi_type(type_text):
            score = min(0.99, score + 0.08)
        if score >= 0.86:
            return "high", score
        if score >= 0.66:
            return "medium", score
        return "low", score

    query = normalize_text(match.get("_query"))
    formatted = normalize_text(match.get("formatted_address"))
    target = normalize_text(location_name)
    district_norm = normalize_text(district)
    level = normalize_text(match.get("level"))
    count = int(match.get("_count") or 0)

    score = 0.35
    score += max(similarity(target, formatted), similarity(target, query)) * 0.35

    if target and target in formatted:
        score += 0.18
    if district_norm and district_norm in formatted:
        score += 0.12
    if level in {"兴趣点", "楼栋", "门牌号", "商务住宅"}:
        score += 0.12
    elif level in {"道路", "区县", "乡镇", "开发区"}:
        score -= 0.18

    if count > 1:
        score -= 0.08
    if is_ambiguous_name(location_name):
        score -= 0.12
    if not district_norm or district_norm not in formatted:
        score -= 0.08

    score = max(0.0, min(score, 0.99))
    if score >= 0.82:
        return "high", score
    if score >= 0.62:
        return "medium", score
    return "low", score


def save_match(conn: sqlite3.Connection, row_id: int, match: dict, confidence: str, score: float) -> None:
    gcj_lon_s, gcj_lat_s = match["location"].split(",", 1)
    gcj_lon = float(gcj_lon_s)
    gcj_lat = float(gcj_lat_s)
    wgs_lat, wgs_lon = gcj02_to_wgs84(gcj_lat, gcj_lon)

    conn.execute(
        f"""
        UPDATE {TABLE_NAME}
        SET
            longitude = ?,
            latitude = ?,
            gcj02_longitude = ?,
            gcj02_latitude = ?,
            geo_confidence = ?,
            geo_method = ?,
            geo_query = ?,
            geo_match_address = ?,
            geo_match_level = ?,
            geo_score = ?
        WHERE id = ?
        """,
        (
            wgs_lon,
            wgs_lat,
            gcj_lon,
            gcj_lat,
            confidence,
            match.get("_method") or "amap_geocode",
            match.get("_query"),
            match.get("formatted_address"),
            match.get("level"),
            round(score, 4),
            row_id,
        ),
    )


def summarize(conn: sqlite3.Connection) -> None:
    total = conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0]
    filled = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE longitude IS NOT NULL AND latitude IS NOT NULL"
    ).fetchone()[0]
    print(f"total_rows={total}")
    print(f"filled_rows={filled}")
    for confidence in ("high", "medium", "low"):
        count = conn.execute(
            f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE geo_confidence = ?",
            (confidence,),
        ).fetchone()[0]
        print(f"{confidence}_confidence={count}")


def main() -> None:
    if not AMAP_WEB_KEY:
        raise RuntimeError("AMAP_WEB_KEY is required")

    ensure_schema()
    rows = fetch_rows(only_missing=True)
    session = requests.Session()
    session.trust_env = False
    updated = 0
    failed = 0
    started_at = time.time()

    with sqlite3.connect(DB_PATH) as conn:
        for index, row in enumerate(rows, start=1):
            try:
                match = geocode_row(session, row["district"], row["location_name"])
                if not match or not match.get("location"):
                    failed += 1
                    print(f"[{index}/{len(rows)}] no_match id={row['id']} name={row['location_name']}")
                    continue

                confidence, score = compute_confidence(
                    row["location_name"], row["district"], match
                )
                save_match(conn, row["id"], match, confidence, score)
                conn.commit()
                updated += 1
                print(
                    f"[{index}/{len(rows)}] matched id={row['id']} "
                    f"name={row['location_name']} conf={confidence} score={score:.3f} "
                    f"query={match.get('_query','')} formatted={match.get('formatted_address','')}"
                )
            except Exception as exc:
                failed += 1
                print(
                    f"[{index}/{len(rows)}] error id={row['id']} "
                    f"name={row['location_name']} error={exc}"
                )
            time.sleep(REQUEST_INTERVAL_SECONDS)

        summarize(conn)

    print(f"updated_total={updated}")
    print(f"failed_total={failed}")
    print(f"elapsed_seconds={round(time.time() - started_at, 1)}")


if __name__ == "__main__":
    main()
