"""Offline tests for the P2 environment sensors (weather / air quality / sunrise).

Needs no network: the fixtures below are **real recorded responses** (开封, 2026-09-25),
including the wrong geocoding hits that the bare name "开封" returns — that case is the
whole reason `parse_candidates` refuses population-less results.
"""

import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import weather  # noqa: E402

NOW = 1_800_000_000.0

# 实测响应：搜"开封"→ 两条全是四川的村子（population 为空）；搜"开封市"才是正主
GEO_WRONG = {
    "results": [
        {"name": "开封", "latitude": 31.76962, "longitude": 105.38522, "admin1": "四川", "country": "中国"},
        {"name": "开封", "latitude": 31.4049, "longitude": 105.475, "admin1": "四川", "country": "中国"},
    ]
}
GEO_RIGHT = {
    "results": [
        {
            "name": "开封市",
            "latitude": 34.7986,
            "longitude": 114.30742,
            "admin1": "河南",
            "admin2": "开封市",
            "country": "中国",
            "population": 1451741,
            "timezone": "Asia/Shanghai",
            "feature_code": "PPL",
        }
    ]
}
FORECAST = {
    "current": {
        "time": "2026-09-25T16:15",
        "temperature_2m": 22.3,
        "apparent_temperature": 24.8,
        "relative_humidity_2m": 81,
        "precipitation": 0.1,
        "weather_code": 51,
        "wind_speed_10m": 4.9,
        "is_day": 1,
    },
    "daily": {
        "sunrise": ["2026-09-25T06:12"],
        "sunset": ["2026-09-25T18:16"],
        "temperature_2m_max": [23.3],
        "temperature_2m_min": [18.8],
        "precipitation_probability_max": [100],
        "uv_index_max": [3.95],
    },
}
AIR = {"current": {"time": "2026-09-25T16:00", "us_aqi": 88, "pm2_5": 22.6, "pm10": 23.0}}


class WmoTableTest(unittest.TestCase):
    """The code table must match the official docs, including the easy-to-forget rows."""

    def test_known_codes(self) -> None:
        """A few codes across the table map to the documented labels."""
        self.assertEqual(weather.wmo_text(0), "晴")
        self.assertEqual(weather.wmo_text(2), "多云")
        self.assertEqual(weather.wmo_text(3), "阴")
        self.assertEqual(weather.wmo_text(45), "雾")
        self.assertEqual(weather.wmo_text(51), "小毛毛雨")
        self.assertEqual(weather.wmo_text(65), "大雨")
        self.assertEqual(weather.wmo_text(77), "雪粒")
        self.assertEqual(weather.wmo_text(95), "雷阵雨")
        # 97 是"凭记忆写必漏"的那条（记忆里 96 之后直接跳 99）
        self.assertEqual(weather.wmo_text(97), "强雷雨")
        self.assertEqual(weather.wmo_text(99), "雷阵雨伴大冰雹")
        print("PASS test_known_codes")

    def test_unknown_and_missing(self) -> None:
        """Unknown codes are labelled as unknown — never dressed up as real weather."""
        self.assertEqual(weather.wmo_text(777), "未知天气（码 777）")
        self.assertEqual(weather.wmo_text(None), "天气未知")
        self.assertEqual(weather.wmo_text("abc"), "天气未知")
        self.assertEqual(weather.wmo_text("3"), "阴")
        print("PASS test_unknown_and_missing")


class CandidateTest(unittest.TestCase):
    """Geocoding must not silently accept a village that shares the city's name."""

    def test_populationless_hits_are_dropped(self) -> None:
        """The two 四川 villages for "开封" are refused outright."""
        self.assertEqual(weather.parse_candidates(GEO_WRONG), [])
        print("PASS test_populationless_hits_are_dropped")

    def test_populated_hit_is_kept(self) -> None:
        """The real 开封市 comes through with its coordinates."""
        places = weather.parse_candidates(GEO_RIGHT)
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0].name, "开封市")
        self.assertAlmostEqual(places[0].latitude, 34.7986, places=4)
        self.assertEqual(places[0].admin1, "河南")
        self.assertIn("河南", places[0].describe())
        print("PASS test_populated_hit_is_kept")

    def test_biggest_population_first(self) -> None:
        """When several real hits come back, the largest city wins."""
        payload = {
            "results": [
                {**GEO_RIGHT["results"][0], "population": 1000},
                {**GEO_RIGHT["results"][0], "name": "小城", "population": 900000},
            ]
        }
        names = [place.name for place in weather.parse_candidates(payload)]
        self.assertEqual(names, ["小城", "开封市"])
        print("PASS test_biggest_population_first")

    def test_junk_payloads(self) -> None:
        """Anything that is not a results list yields no candidates."""
        for payload in ({}, {"results": None}, {"results": "x"}, {"results": [1, 2]}):
            self.assertEqual(weather.parse_candidates(payload), [], repr(payload))
        print("PASS test_junk_payloads")


class SearchRetryTest(unittest.TestCase):
    """search_place retries with a "市" suffix only when the bare name found nothing."""

    def setUp(self) -> None:
        """Remember the real fetcher so each test can restore it."""
        self.real = weather._get_json
        self.calls: list[str] = []

    def tearDown(self) -> None:
        """Restore the real fetcher."""
        weather._get_json = self.real

    def test_retries_with_suffix(self) -> None:
        """Bare "开封" finds no populated hit, so "开封市" is tried and wins."""

        def fake(url: str, params: dict, timeout: float = 0) -> dict:
            self.calls.append(params["name"])
            return GEO_WRONG if params["name"] == "开封" else GEO_RIGHT

        weather._get_json = fake
        places = weather.search_place("开封")
        self.assertEqual(self.calls, ["开封", "开封市"])
        self.assertAlmostEqual(places[0].latitude, 34.7986, places=4)
        print("PASS test_retries_with_suffix")

    def test_no_retry_when_bare_works(self) -> None:
        """杭州-type names already work bare, so only one request goes out."""

        def fake(url: str, params: dict, timeout: float = 0) -> dict:
            self.calls.append(params["name"])
            return GEO_RIGHT

        weather._get_json = fake
        weather.search_place("杭州")
        self.assertEqual(self.calls, ["杭州"])
        print("PASS test_no_retry_when_bare_works")

    def test_suffixed_input_is_not_suffixed_twice(self) -> None:
        """Typing "开封市" must not turn into a "开封市市" request."""

        def fake(url: str, params: dict, timeout: float = 0) -> dict:
            self.calls.append(params["name"])
            return GEO_WRONG

        weather._get_json = fake
        weather.search_place("开封市")
        self.assertEqual(self.calls, ["开封市"])
        print("PASS test_suffixed_input_is_not_suffixed_twice")

    def test_empty_name_never_calls_out(self) -> None:
        """An empty box must not turn into a request."""

        def fake(url: str, params: dict, timeout: float = 0) -> dict:
            self.calls.append(params["name"])
            return GEO_RIGHT

        weather._get_json = fake
        self.assertEqual(weather.search_place("   "), [])
        self.assertEqual(self.calls, [])
        print("PASS test_empty_name_never_calls_out")

    def test_network_error_surfaces(self) -> None:
        """A dead network raises instead of pretending there were no matches."""

        def fake(url: str, params: dict, timeout: float = 0) -> dict:
            raise OSError("network down")

        weather._get_json = fake
        with self.assertRaises(OSError):
            weather.search_place("开封")
        print("PASS test_network_error_surfaces")


class ParseTest(unittest.TestCase):
    """Recorded payloads must parse into exactly the values we saw."""

    def test_forecast(self) -> None:
        """Every field we show is read from the recorded response."""
        got = weather.parse_forecast(FORECAST)
        self.assertEqual(got["temperature"], 22.3)
        self.assertEqual(got["apparent"], 24.8)
        self.assertEqual(got["humidity"], 81)
        self.assertEqual(got["code"], 51)
        self.assertEqual(got["wind"], 4.9)
        self.assertIs(got["is_day"], True)
        self.assertEqual(got["temp_max"], 23.3)
        self.assertEqual(got["temp_min"], 18.8)
        self.assertEqual(got["precip_prob"], 100)
        self.assertEqual(got["sunrise"], "2026-09-25T06:12")
        self.assertEqual(got["sunset"], "2026-09-25T18:16")
        print("PASS test_forecast")

    def test_forecast_missing_stays_none(self) -> None:
        """Empty payloads produce None everywhere — no invented defaults."""
        got = weather.parse_forecast({})
        self.assertTrue(all(value is None or value == "" for value in got.values()), got)
        print("PASS test_forecast_missing_stays_none")

    def test_air(self) -> None:
        """Air-quality fields come through as recorded."""
        got = weather.parse_air(AIR)
        self.assertEqual((got["aqi"], got["pm25"], got["pm10"]), (88, 22.6, 23.0))
        self.assertEqual(weather.parse_air({})["aqi"], None)
        print("PASS test_air")


class ReadTest(unittest.TestCase):
    """One half failing must not throw away the other half."""

    def setUp(self) -> None:
        """Remember the real fetcher."""
        self.real = weather._get_json

    def tearDown(self) -> None:
        """Restore the real fetcher."""
        weather._get_json = self.real

    def test_both_ok(self) -> None:
        """Weather plus air quality land in one reading."""

        def fake(url: str, params: dict, timeout: float = 0) -> dict:
            return AIR if "air-quality" in url else FORECAST

        weather._get_json = fake
        result = weather.read(34.7986, 114.30742, "开封市", now=NOW)
        self.assertEqual(result.error, "")
        self.assertEqual(result.temperature, 22.3)
        self.assertEqual(result.aqi, 88)
        self.assertEqual(result.fetched_at, NOW)
        self.assertIn("开封市", result.describe())
        self.assertIn("小毛毛雨", result.describe())
        print("PASS test_both_ok")

    def test_air_failure_keeps_weather(self) -> None:
        """Air quality failing leaves the weather intact and is noted."""

        def fake(url: str, params: dict, timeout: float = 0) -> dict:
            if "air-quality" in url:
                raise OSError("air down")
            return FORECAST

        weather._get_json = fake
        result = weather.read(34.8, 114.3, "开封市", now=NOW)
        self.assertEqual(result.temperature, 22.3)
        self.assertIsNone(result.aqi)
        self.assertTrue(any("空气质量" in note for note in result.notes), result.notes)
        self.assertEqual(result.error, "")
        print("PASS test_air_failure_keeps_weather")

    def test_weather_failure_keeps_air(self) -> None:
        """The reverse case: no weather, but AQI still shows, with an error recorded."""

        def fake(url: str, params: dict, timeout: float = 0) -> dict:
            if "air-quality" in url:
                return AIR
            raise OSError("forecast down")

        weather._get_json = fake
        result = weather.read(34.8, 114.3, "开封市", now=NOW)
        self.assertEqual(result.aqi, 88)
        self.assertIsNone(result.temperature)
        self.assertIn("forecast down", result.error)
        self.assertIn("天气没拿到", result.describe())
        print("PASS test_weather_failure_keeps_air")

    def test_both_fail(self) -> None:
        """Total failure is reported as failure, with nothing invented."""

        def fake(url: str, params: dict, timeout: float = 0) -> dict:
            raise OSError("offline")

        weather._get_json = fake
        result = weather.read(34.8, 114.3, "开封市", now=NOW)
        self.assertTrue(result.error)
        self.assertIsNone(result.aqi)
        self.assertIsNone(result.temperature)
        print("PASS test_both_fail")


class FreshnessTest(unittest.TestCase):
    """Cache age decides whether we re-fetch or just show what we have."""

    def test_freshness(self) -> None:
        """Never-fetched and exactly-expired readings are both stale."""
        self.assertFalse(weather.is_fresh(0, now=NOW))
        self.assertTrue(weather.is_fresh(NOW - 10, now=NOW))
        self.assertTrue(weather.is_fresh(NOW - weather.WEATHER_TTL_SECONDS + 1, now=NOW))
        self.assertFalse(weather.is_fresh(NOW - weather.WEATHER_TTL_SECONDS, now=NOW))
        # 时钟被往回调过的情形：未来时间戳不算"新鲜"，否则会永远不再刷新
        self.assertFalse(weather.is_fresh(NOW + 600, now=NOW))
        print("PASS test_freshness")

    def test_air_ttl_is_longer(self) -> None:
        """Air quality lasts an hour, so a 30-minute-old AQI is still fine."""
        self.assertTrue(weather.is_fresh(NOW - 1800, now=NOW, ttl=weather.AIR_TTL_SECONDS))
        self.assertLess(weather.WEATHER_TTL_SECONDS, weather.AIR_TTL_SECONDS)
        print("PASS test_air_ttl_is_longer")


class CacheTest(unittest.TestCase):
    """The cached reading round-trips, and a broken file is not fatal."""

    def setUp(self) -> None:
        """Point the cache at a scratch file inside the workspace (tempfile trips WinError 5)."""
        self.real = weather.CACHE
        self.scratch = Path(__file__).resolve().parent / "_weather_test"
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        weather.CACHE = self.scratch / "weather.json"

    def tearDown(self) -> None:
        """Restore the real cache path and clean up."""
        weather.CACHE = self.real
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_round_trip(self) -> None:
        """A written reading comes back marked stale, with its values intact."""
        result = weather.Weather(
            place="开封市",
            latitude=34.7986,
            longitude=114.30742,
            fetched_at=NOW,
            temperature=22.3,
            apparent=24.8,
            humidity=81,
            code=51,
            temp_max=23.3,
            temp_min=18.8,
            precip_prob=100,
            aqi=88,
            pm25=22.6,
            sunrise="2026-09-25T06:12",
            sunset="2026-09-25T18:16",
        )
        self.assertTrue(weather.write_cache(result))
        back = weather.read_cache()
        self.assertIsNotNone(back)
        self.assertEqual(back.place, "开封市")
        self.assertEqual(back.temperature, 22.3)
        self.assertEqual(back.code, 51)
        self.assertEqual(back.aqi, 88)
        self.assertTrue(back.stale)
        self.assertIn("的读数", back.describe())
        # 不留 .tmp 残渣
        self.assertEqual(list(self.scratch.glob("*.tmp")), [])
        print("PASS test_round_trip")

    def test_broken_cache_is_ignored(self) -> None:
        """Missing file, broken JSON, or a payload without a timestamp all give None."""
        self.assertIsNone(weather.read_cache())
        weather.CACHE.write_text("{not json", encoding="utf-8")
        self.assertIsNone(weather.read_cache())
        weather.CACHE.write_text('{"place": "开封市"}', encoding="utf-8")
        self.assertIsNone(weather.read_cache())
        weather.CACHE.write_text('["list"]', encoding="utf-8")
        self.assertIsNone(weather.read_cache())
        print("PASS test_broken_cache_is_ignored")

    def test_bad_value_types_do_not_raise(self) -> None:
        """A hand-edited cache with junk values still loads."""
        weather.CACHE.write_text(
            '{"fetched_at": 10002, "place": "开封市", "temperature": "warm", "code": "x"}',
            encoding="utf-8",
        )
        back = weather.read_cache()
        self.assertIsNotNone(back)
        self.assertIsNone(back.temperature)
        self.assertIsNone(back.code)
        print("PASS test_bad_value_types_do_not_raise")


class SummaryTest(unittest.TestCase):
    """The bubble line and the log block stay readable when values are missing."""

    def test_summary_lines(self) -> None:
        """Every section shows up, with gaps spelled out as missing."""
        result = weather.Weather(place="开封市", fetched_at=NOW)
        text = result.summary()
        self.assertIn("开封市", text)
        self.assertIn("（缺）", text)
        two = weather.Weather(place="开封市", fetched_at=NOW, temperature=22.3, code=51, aqi=88)
        self.assertIn("22.3°C", two.summary())
        self.assertIn("AQI 88", two.describe())
        print("PASS test_summary_lines")

    def test_describe_without_anything(self) -> None:
        """A reading with no data and no error still says something honest."""
        text = weather.Weather(latitude=34.8, longitude=114.3, fetched_at=NOW).describe()
        self.assertIn("34.80,114.30", text)
        print("PASS test_describe_without_anything")


if __name__ == "__main__":
    unittest.main(verbosity=2)
