"""环境传感器（P2）：天气 / 空气质量 / 日出日落——纯只读网络，不需要任何硬件。

三条线（沿用 P0/P1 立的规矩）：

1. **位置由用户给定**（config 里的经纬度），不猜、更不偷偷拿 IP 去查——"不问本机信息"这条
   对网络传感器同样成立；
2. **只发 GET，失败就说失败**：拿不到就返回带 ``error`` 的结果；有旧读数时会带上
   ``stale``／`fetched_at`，**绝不用旧值冒充新值**；
3. **本阶段只落在桌宠**（气泡 + ``pet_log.txt``），不进群、不写 AstrBot。

数据源是 Open-Meteo（免 API Key、免注册，实测从本机可达）：
``api.open-meteo.com`` 天气、``air-quality-api.open-meteo.com`` 空气质量、
``geocoding-api.open-meteo.com`` 地名查经纬度。

踩过的坑（实测，不是猜的）：**中文城市名不能盲取第一个搜索结果**——
搜"开封"返回的是四川的两个村子（`population` 为空），正主是"开封市"；
抽样查了约 28 个城市，**有 11 个都这样**（吉林、开封、宜昌、桂林、大理、绍兴、九江、
岳阳、保定、佛山、东莞），而"杭州/苏州/成都/西安/哈尔滨"这类不带"市"就能命中。
所以 `search_place` 会先按原名查、查不到**有人口的**结果再补一个"市"重查，
并且**只认 population > 0 的结果**，一个都没有就老老实实返回空——
宁可让用户换个说法，也不把他定位到别的省。
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import paths

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

CACHE = paths.BASE / "weather.json"
TIMEOUT_SECONDS = 8
# 天气 15 分钟一更；空气质量本来就是一小时一更，问勤了也是同一份数据
WEATHER_TTL_SECONDS = 900
AIR_TTL_SECONDS = 3600

CURRENT_FIELDS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
    "weather_code,wind_speed_10m,is_day"
)
DAILY_FIELDS = (
    "sunrise,sunset,temperature_2m_max,temperature_2m_min,"
    "precipitation_probability_max,uv_index_max"
)

# WMO 天气码表——**逐行抄自 Open-Meteo 官方文档页**（https://open-meteo.com/en/docs 的
# "WMO Weather interpretation codes" 表），不是凭记忆写的：实测记忆里漏了 97（Heavy
# thunderstorm）。表里没有的码一律说"未知天气（码 N）"，不硬编一个像样的说法。
WMO_TEXT = {
    0: "晴",
    1: "晴间多云",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "冻毛毛雨（轻）",
    57: "冻毛毛雨（重）",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨（轻）",
    67: "冻雨（重）",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "中阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "大阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴小冰雹",
    97: "强雷雨",
    99: "雷阵雨伴大冰雹",
}


def wmo_text(code) -> str:  # noqa: ANN001 - 可能是 None
    """Turn a WMO weather code into Chinese.

    Args:
        code: The ``weather_code`` value (may be missing or a string).

    Returns:
        A Chinese label, or a "unknown code" label when the code is not in the table.
    """
    try:
        number = int(code)
    except (TypeError, ValueError):
        return "天气未知"
    return WMO_TEXT.get(number, f"未知天气（码 {number}）")


@dataclass
class Place:
    """One geocoding candidate."""

    name: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    admin1: str = ""
    country: str = ""
    population: int = 0
    timezone: str = ""

    def describe(self) -> str:
        """One-line label for the bubble.

        Returns:
            Chinese summary including province and country.
        """
        where = " ".join(part for part in (self.country, self.admin1) if part)
        return f"{self.name}（{where}）" if where else self.name


@dataclass
class Weather:
    """One weather reading (plus whatever air-quality values came with it)."""

    place: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    fetched_at: float = 0.0
    temperature: float | None = None
    apparent: float | None = None
    humidity: float | None = None
    precipitation: float | None = None
    code: int | None = None
    wind: float | None = None
    is_day: bool | None = None
    temp_max: float | None = None
    temp_min: float | None = None
    precip_prob: float | None = None
    uv: float | None = None
    sunrise: str = ""
    sunset: str = ""
    aqi: float | None = None
    pm25: float | None = None
    pm10: float | None = None
    error: str = ""
    stale: bool = False
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """One-line summary for the bubble.

        Returns:
            Chinese summary; the error text when there is nothing else to show.
        """
        if self.error and self.temperature is None:
            return f"天气没拿到：{self.error}"
        bits = [self.place or f"{self.latitude:.2f},{self.longitude:.2f}"]
        if self.temperature is not None:
            line = f"{self.temperature:.1f}°C"
            if self.apparent is not None and abs(self.apparent - self.temperature) >= 1:
                line += f"（体感 {self.apparent:.1f}°C）"
            bits.append(line)
        bits.append(wmo_text(self.code))
        if self.humidity is not None:
            bits.append(f"湿度 {self.humidity:.0f}%")
        if self.wind is not None:
            bits.append(f"风 {self.wind:.0f} km/h")
        if self.temp_min is not None and self.temp_max is not None:
            bits.append(f"{self.temp_min:.0f}~{self.temp_max:.0f}°C")
        if self.precip_prob is not None:
            bits.append(f"降水概率 {self.precip_prob:.0f}%")
        if self.aqi is not None:
            bits.append(f"AQI {self.aqi:.0f}")
        if self.sunrise and self.sunset:
            bits.append(f"日出 {self.sunrise[11:]} 日落 {self.sunset[11:]}")
        if self.stale:
            bits.append(f"（这是 {time.strftime('%H:%M', time.localtime(self.fetched_at))} 的读数）")
        return "　".join(bits)

    def summary(self) -> str:
        """A slightly longer multi-line block for the log.

        Returns:
            Chinese multi-line text with everything we actually got.
        """
        rows = [
            f"地点 {self.place or '（未设）'}　坐标 {self.latitude:.4f},{self.longitude:.4f}",
            f"读数时间 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.fetched_at)) if self.fetched_at else '（无）'}",
        ]
        if self.error:
            rows.append(f"错误 {self.error}")
        rows.append(
            "温度 "
            + (
                f"{self.temperature}°C（体感 {self.apparent}°C）"
                if self.temperature is not None
                else "（缺）"
            )
            + f"　天气 {wmo_text(self.code)}"
        )
        rows.append(
            f"湿度 {self.humidity}%　降水 {self.precipitation}mm　风 {self.wind}km/h　白天 {self.is_day}"
        )
        rows.append(
            f"今日 {self.temp_min}~{self.temp_max}°C　降水概率 {self.precip_prob}%　UV {self.uv}"
        )
        rows.append(f"日出 {self.sunrise or '（缺）'}　日落 {self.sunset or '（缺）'}")
        rows.append(f"AQI {self.aqi}　PM2.5 {self.pm25}　PM10 {self.pm10}")
        return "\n".join(rows)


def _get_json(url: str, params: dict, timeout: float = TIMEOUT_SECONDS) -> dict:
    """Issue one GET and decode JSON.

    Args:
        url: Endpoint.
        params: Query parameters.
        timeout: Socket timeout in seconds.

    Returns:
        The decoded JSON object.

    Raises:
        OSError: Network or HTTP failure.
        ValueError: The response was not a JSON object.
    """
    query = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{query}", timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("返回的不是 JSON 对象")
    return payload


def parse_candidates(payload: dict) -> list[Place]:
    """Turn a geocoding response into candidates, best first.

    Only entries that carry a real population are kept: a hit with no population is
    usually a village that happens to share the name (搜"开封"会撞上四川的村子).

    Args:
        payload: Decoded geocoding response.

    Returns:
        Candidates sorted by population, descending.
    """
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    places = []
    for item in results:
        if not isinstance(item, dict):
            continue
        population = item.get("population") or 0
        if not isinstance(population, (int, float)) or population <= 0:
            continue
        places.append(
            Place(
                name=str(item.get("name") or ""),
                latitude=float(item.get("latitude") or 0.0),
                longitude=float(item.get("longitude") or 0.0),
                admin1=str(item.get("admin1") or ""),
                country=str(item.get("country") or ""),
                population=int(population),
                timezone=str(item.get("timezone") or ""),
            )
        )
    places.sort(key=lambda place: place.population, reverse=True)
    return places


def search_place(name: str, timeout: float = TIMEOUT_SECONDS) -> list[Place]:
    """Look up a place name, trying the bare name and then a "市"-suffixed retry.

    Args:
        name: What the user typed (``"开封"``, ``"开封市"``, ``"杭州"``).
        timeout: Socket timeout in seconds.

    Returns:
        Candidates, best first; empty when nothing with a real population matched.

    Raises:
        OSError: Network or HTTP failure on every attempt.
    """
    text = str(name or "").strip()
    if not text:
        return []
    attempts = [text]
    if not text.endswith(("市", "省", "县", "区")):
        attempts.append(text + "市")
    last_error: OSError | None = None
    for attempt in attempts:
        try:
            payload = _get_json(
                GEO_URL,
                {"name": attempt, "count": 8, "language": "zh", "format": "json"},
                timeout,
            )
        except OSError as exc:
            last_error = exc
            continue
        places = parse_candidates(payload)
        if places:
            return places
    if last_error is not None:
        raise last_error
    return []


def parse_forecast(payload: dict) -> dict:
    """Pick the fields we show out of a forecast response.

    Args:
        payload: Decoded forecast response.

    Returns:
        A flat dict; missing values stay ``None`` (never guessed).
    """
    current = payload.get("current") if isinstance(payload, dict) else None
    daily = payload.get("daily") if isinstance(payload, dict) else None
    current = current if isinstance(current, dict) else {}
    daily = daily if isinstance(daily, dict) else {}

    def one(source: dict, key: str):  # noqa: ANN202 - 取值可能缺失
        values = source.get(key)
        if isinstance(values, list):
            values = values[0] if values else None
        return values

    code = one(current, "weather_code")
    return {
        "temperature": one(current, "temperature_2m"),
        "apparent": one(current, "apparent_temperature"),
        "humidity": one(current, "relative_humidity_2m"),
        "precipitation": one(current, "precipitation"),
        "code": int(code) if isinstance(code, (int, float)) else None,
        "wind": one(current, "wind_speed_10m"),
        "is_day": None if one(current, "is_day") is None else bool(one(current, "is_day")),
        "temp_max": one(daily, "temperature_2m_max"),
        "temp_min": one(daily, "temperature_2m_min"),
        "precip_prob": one(daily, "precipitation_probability_max"),
        "uv": one(daily, "uv_index_max"),
        "sunrise": str(one(daily, "sunrise") or ""),
        "sunset": str(one(daily, "sunset") or ""),
    }


def parse_air(payload: dict) -> dict:
    """Pick the air-quality fields we show.

    Args:
        payload: Decoded air-quality response.

    Returns:
        A flat dict with ``aqi`` / ``pm25`` / ``pm10`` (missing stays ``None``).
    """
    current = payload.get("current") if isinstance(payload, dict) else None
    current = current if isinstance(current, dict) else {}
    return {
        "aqi": current.get("us_aqi"),
        "pm25": current.get("pm2_5"),
        "pm10": current.get("pm10"),
    }


def read(lat: float, lon: float, place: str = "", now: float | None = None) -> Weather:
    """Fetch weather and air quality for one location.

    Weather and air quality are fetched separately: **either one failing does not
    throw away the other** — the failing half just stays empty and is noted.

    Args:
        lat: Latitude.
        lon: Longitude.
        place: Display name for the location.
        now: Timestamp override (for tests).

    Returns:
        The reading; ``error`` is set when nothing came back at all.
    """
    stamp = time.time() if now is None else now
    result = Weather(place=place, latitude=lat, longitude=lon, fetched_at=stamp)
    common = {"latitude": lat, "longitude": lon, "timezone": "auto"}
    try:
        forecast = parse_forecast(
            _get_json(
                WEATHER_URL,
                {**common, "current": CURRENT_FIELDS, "daily": DAILY_FIELDS, "forecast_days": 1},
            )
        )
        for key, value in forecast.items():
            setattr(result, key, value)
    except (OSError, ValueError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    try:
        air = parse_air(_get_json(AIR_URL, {**common, "current": "us_aqi,pm2_5,pm10"}))
        result.aqi = air["aqi"]
        result.pm25 = air["pm25"]
        result.pm10 = air["pm10"]
    except (OSError, ValueError) as exc:
        result.notes.append(f"空气质量没拿到：{type(exc).__name__}: {exc}")
    if result.error and not result.notes:
        result.notes.append(result.error)
    return result


def is_fresh(fetched_at: float, now: float | None = None, ttl: float = WEATHER_TTL_SECONDS) -> bool:
    """Is a cached reading still inside its time-to-live?

    Args:
        fetched_at: When the reading was taken (0 means "never").
        now: Timestamp override.
        ttl: Time-to-live in seconds.

    Returns:
        True when the reading is recent enough to show without re-fetching.
    """
    if not fetched_at:
        return False
    stamp = time.time() if now is None else now
    age = stamp - fetched_at
    return 0 <= age < ttl


def write_cache(weather: Weather) -> bool:
    """Persist the last reading (atomic write: tmp + ``os.replace``).

    Args:
        weather: The reading to store.

    Returns:
        True when the file was written.
    """
    payload = {
        "place": weather.place,
        "latitude": weather.latitude,
        "longitude": weather.longitude,
        "fetched_at": weather.fetched_at,
        "temperature": weather.temperature,
        "apparent": weather.apparent,
        "humidity": weather.humidity,
        "code": weather.code,
        "temp_max": weather.temp_max,
        "temp_min": weather.temp_min,
        "precip_prob": weather.precip_prob,
        "aqi": weather.aqi,
        "pm25": weather.pm25,
        "sunrise": weather.sunrise,
        "sunset": weather.sunset,
    }
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(f"{CACHE}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, CACHE)
        return True
    except OSError:
        return False


def read_cache() -> Weather | None:
    """Load the last reading.

    Returns:
        The cached reading, or ``None`` when there is nothing usable.
    """
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("fetched_at"):
        return None
    weather = Weather(
        place=str(data.get("place") or ""),
        latitude=float(data.get("latitude") or 0.0),
        longitude=float(data.get("longitude") or 0.0),
        fetched_at=float(data.get("fetched_at") or 0.0),
    )
    for key in ("temperature", "apparent", "humidity", "temp_max", "temp_min", "precip_prob", "aqi", "pm25"):
        value = data.get(key)
        setattr(weather, key, float(value) if isinstance(value, (int, float)) else None)
    code = data.get("code")
    weather.code = int(code) if isinstance(code, (int, float)) else None
    weather.sunrise = str(data.get("sunrise") or "")
    weather.sunset = str(data.get("sunset") or "")
    weather.stale = True
    return weather
