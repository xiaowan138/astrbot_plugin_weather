"""
天气查询插件 astrbot_plugin_weather
====================================

AstrBot v4 插件。功能：

1. 命令查询：发送 `/天气 北京` 或 `/weather Shanghai`，返回实时天气与今日预报。
2. AI 工具调用：注册 get_weather 工具，AI 收到"北京天气怎么样"时自动查询。

数据源（按优先级自动切换）：
1. 和风天气 —— 在插件配置中填写 API Key 后启用，数据最全（推荐）。
2. 中国天气网 —— 零 API Key、国内服务器直连，国内部署默认数据源。
3. Open-Meteo —— 兜底，适合海外服务器（国内服务器可能无法访问）。

说明：
- 本插件面向 AstrBot v4（>= 3.5.19，无需 @register 装饰器，类继承 Star 即被自动识别）。
- 零第三方依赖，仅使用 Python 标准库。
"""

import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Tuple

from astrbot.api.all import *

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量与映射
# ---------------------------------------------------------------------------

# Open-Meteo WMO 天气代码 -> (emoji, 中文描述)
WMO_CODES: Dict[int, tuple] = {
    0: ("☀️", "晴"),
    1: ("🌤️", "基本晴朗"),
    2: ("⛅", "局部多云"),
    3: ("☁️", "阴"),
    45: ("🌫️", "雾"),
    48: ("🌫️", "雾凇"),
    51: ("🌦️", "毛毛雨"),
    53: ("🌦️", "毛毛雨"),
    55: ("🌧️", "强毛毛雨"),
    56: ("🌧️", "冻毛毛雨"),
    57: ("🌧️", "强冻毛毛雨"),
    61: ("🌧️", "小雨"),
    63: ("🌧️", "中雨"),
    65: ("🌧️", "大雨"),
    66: ("🌧️", "冻雨"),
    67: ("🌧️", "强冻雨"),
    71: ("🌨️", "小雪"),
    73: ("🌨️", "中雪"),
    75: ("❄️", "大雪"),
    77: ("❄️", "米雪"),
    80: ("🌦️", "阵雨"),
    81: ("🌧️", "强阵雨"),
    82: ("⛈️", "暴阵雨"),
    85: ("🌨️", "阵雪"),
    86: ("❄️", "强阵雪"),
    95: ("⛈️", "雷暴"),
    96: ("⛈️", "雷暴伴小冰雹"),
    99: ("⛈️", "雷暴伴大冰雹"),
}

# Open-Meteo
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

# 和风天气
QWEATHER_GEO_URL = "https://geoapi.qweather.com/v2/city/lookup"
QWEATHER_NOW_URL = "https://devapi.qweather.com/v7/weather/now"
QWEATHER_3D_URL = "https://devapi.qweather.com/v7/weather/3d"

# 中国天气网（零 Key、国内直连）
CN_SEARCH_URL = "http://toy1.weather.com.cn/search"
CN_SK_URL = "http://d1.weather.com.cn/sk_2d/{}.html"
CN_DZ_URL = "http://d1.weather.com.cn/dingzhi/{}.html"
CN_REFERER = "http://www.weather.com.cn/"

DEFAULT_CONFIG = {
    "qweather_api_key": "",  # 和风天气 API Key（可选）。留空则使用中国天气网（零 Key）
    "default_city": "",      # 默认城市（可选），如 "北京"；/天气 不带城市时使用
}

UA = "Mozilla/5.0 (AstrBot-Weather-Plugin/1.1)"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _http_get_bytes(
    url: str,
    params: Dict[str, Any] | None = None,
    timeout: float = 8.0,
    referer: str | None = None,
) -> bytes:
    """同步 HTTP GET 返回原始字节（兼容 GBK 等非 UTF-8 响应）。"""
    full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(full_url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


async def _aget_bytes(
    url: str,
    params: Dict[str, Any] | None = None,
    timeout: float = 8.0,
    referer: str | None = None,
) -> bytes:
    """异步包装同步请求，不阻塞事件循环。"""
    return await asyncio.to_thread(_http_get_bytes, url, params, timeout, referer)


def _decode(raw: bytes) -> str:
    """优先 UTF-8，失败回退 GBK（中国天气网接口为 GBK 编码）。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


async def _aget_json(
    url: str,
    params: Dict[str, Any] | None = None,
    timeout: float = 8.0,
) -> Dict[str, Any]:
    """GET 并解析 UTF-8 JSON（用于 Open-Meteo / 和风天气）。"""
    raw = await _aget_bytes(url, params, timeout)
    return json.loads(raw.decode("utf-8", errors="replace"))


def _qweather_emoji(text: str) -> str:
    """天气文本 -> emoji。"""
    if "雷" in text:
        return "⛈️"
    if "雪" in text:
        return "❄️"
    if "雨" in text:
        return "🌧️"
    if "雾" in text or "霾" in text:
        return "🌫️"
    if "阴" in text:
        return "☁️"
    if "云" in text:
        return "⛅"
    if "晴" in text:
        return "☀️"
    return "🌡️"


def _format_place(loc: Dict[str, Any]) -> str:
    name = loc.get("name") or ""
    admin = loc.get("admin1") or ""
    country = loc.get("country") or ""
    # 去除冗余：admin1="北京市" 与 name="北京" 重复时保留较长者
    if name and admin and (admin.startswith(name) or name.startswith(admin)):
        if len(admin) >= len(name):
            name = ""
        else:
            admin = ""
    parts = [p for p in (country, admin, name) if p]
    return " ".join(parts)


# ---------------------------------------------------------------------------
# 插件主体（AstrBot v4：继承 Star 即自动注册，元数据来自 metadata.yaml）
# ---------------------------------------------------------------------------

class WeatherPlugin(Star):
    """天气查询插件。

    发送 `/天气 城市` 或 `/weather 城市` 查询实时天气与今日预报。
    也可以直接问 AI："北京天气怎么样"（需在 AstrBot 中开启工具调用）。
    国内服务器默认使用中国天气网（零 Key），无需任何配置即可使用。
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

        # 优先使用 v4 原生配置（Web 面板可编辑，数据来自 _conf_schema.json）
        cfg = config if isinstance(config, dict) else {}
        self.qweather_key: str = str(cfg.get("qweather_api_key") or "").strip()
        self.default_city: str = str(cfg.get("default_city") or "").strip()

        # 兜底：未提供 v4 配置时，读取插件目录下 config.json
        if not self.qweather_key and not self.default_city:
            manual = self._load_manual_config()
            self.qweather_key = self.qweather_key or manual["qweather_api_key"]
            self.default_city = self.default_city or manual["default_city"]

        logger.info(
            "天气插件已加载。数据源: %s",
            "和风天气" if self.qweather_key else "中国天气网（零 Key）→ Open-Meteo 兜底",
        )

    # ---------------- 配置 ----------------

    def _load_manual_config(self) -> Dict[str, Any]:
        """读取插件目录下的 config.json，失败时回退默认配置。"""
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        config = dict(DEFAULT_CONFIG)
        try:
            if os.path.isfile(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for key in DEFAULT_CONFIG:
                        if key in data and data[key] not in (None, ""):
                            config[key] = data[key]
        except Exception as e:
            logger.warning("读取插件配置失败，使用默认配置：%s", e)
        return config

    # ---------------- 命令 ----------------

    @command("天气", alias={"weather", "Weather"})
    async def weather(self, event: AstrMessageEvent):
        """查询天气。用法：/天气 城市，如 /天气 北京、/weather Shanghai"""
        city = self._extract_city(event)
        if not city:
            city = self.default_city
        if not city:
            yield event.plain_result(
                "请提供城市名，例如：\n/天气 北京\n/weather Shanghai\n"
                "（也可在插件配置中设置默认城市）"
            )
            return
        try:
            report = await self._build_report(city)
        except Exception as e:
            logger.exception("查询天气失败: %s", city)
            yield event.plain_result(f"天气查询失败：{e}\n请检查城市名是否正确，或稍后重试。")
            return
        yield event.plain_result(report)

    def _extract_city(self, event: AstrMessageEvent) -> str:
        """从消息中提取城市名。

        v4 的唤醒阶段已剥离命令前缀（如 "/"），message_str 形如 "天气 北京" 或
        "weather Beijing"。这里手动解析，兼容带/不带命令名、有无空格多种写法，
        且支持多词城市名（如 "New York"、"成都 都江堰"）。
        """
        text = (event.message_str or "").strip()
        m = re.match(r"^[/!！]?\s*(天气|weather)\s*(.*)$", text, re.IGNORECASE | re.DOTALL)
        if m:
            city = m.group(2).strip()
            if city:
                return city
        return ""

    # ---------------- LLM 工具 ----------------

    @llm_tool(name="get_weather")
    async def get_weather(self, event: AstrMessageEvent, city: str = "", **kwargs):
        """查询指定城市或地区的当前天气（温度、体感、湿度、风力、气压）以及今日天气趋势。

        Args:
            city(string): 要查询天气的城市或地区名称，例如：北京、上海、杭州、广州
        """
        city = (city or "").strip()
        if not city:
            yield event.plain_result("请提供要查询天气的城市名称。")
            return
        try:
            report = await self._build_report(city)
        except Exception as e:
            logger.exception("LLM 工具查询天气失败: %s", city)
            yield event.plain_result(f"天气查询失败：{e}")
            return
        yield event.plain_result(report)

    # ---------------- 核心逻辑 ----------------

    async def _build_report(self, city: str) -> str:
        """数据源优先级：和风天气（配置了 Key）→ 中国天气网（零 Key）→ Open-Meteo。"""
        errors: List[str] = []
        if self.qweather_key:
            try:
                return await self._fetch_qweather(city)
            except Exception as e:
                errors.append(f"和风天气: {e}")
        try:
            return await self._fetch_cn(city)
        except Exception as e:
            errors.append(f"中国天气网: {e}")
        try:
            return await self._fetch_open_meteo(city)
        except Exception as e:
            errors.append(f"Open-Meteo: {e}")
        raise RuntimeError("所有数据源均不可用。\n" + "\n".join(errors))

    # ---------- 数据源 1：和风天气 ----------

    async def _fetch_qweather(self, city: str) -> str:
        # 1. 城市查找 -> LocationID
        geo = await _aget_json(
            QWEATHER_GEO_URL, {"location": city, "key": self.qweather_key, "number": 1}
        )
        if geo.get("code") != "200" or not (geo.get("location") or []):
            raise RuntimeError(f"和风天气未找到城市“{city}”")
        loc = geo["location"][0]
        loc_id = loc["id"]
        name = loc.get("name") or city
        adm1 = loc.get("adm1") or ""
        country = loc.get("country") or ""

        # 2. 实时天气
        now = await _aget_json(QWEATHER_NOW_URL, {"location": loc_id, "key": self.qweather_key})
        if now.get("code") != "200":
            raise RuntimeError(f"和风天气查询失败（code={now.get('code')}）")
        n = now.get("now") or {}
        text = n.get("text") or "未知"
        emoji = _qweather_emoji(text)

        lines = [
            f"{emoji} {name}（{adm1} · {country}）· 当前天气",
            f"天气：{text}",
            f"温度：{n.get('temp')}°C（体感 {n.get('feelsLike')}°C）",
            f"湿度：{n.get('humidity')}%",
            f"风向：{n.get('windDir')} {n.get('windScale')} 级",
            f"气压：{n.get('pressure')} hPa",
        ]

        # 3. 今日预报（3 天接口取第一天）
        d3 = await _aget_json(QWEATHER_3D_URL, {"location": loc_id, "key": self.qweather_key})
        if d3.get("code") == "200" and (d3.get("daily") or []):
            day = d3["daily"][0]
            dtext = day.get("textDay") or "未知"
            demoji = _qweather_emoji(dtext)
            lines.append(
                f"今日：{demoji} {dtext}，{day.get('tempMin')}°C ~ {day.get('tempMax')}°C"
            )
        lines.append("数据来源：和风天气")
        return "\n".join(lines)

    # ---------- 数据源 2：中国天气网（零 Key，国内直连） ----------

    async def _fetch_cn(self, city: str) -> str:
        # 1. 城市名 -> 站号（toy1.weather.com.cn 搜索接口）
        raw = await _aget_bytes(
            CN_SEARCH_URL, {"cityname": city}, timeout=6.0, referer=CN_REFERER
        )
        stations = self._parse_cn_search(_decode(raw))
        if not stations:
            raise RuntimeError(f"未找到城市“{city}”")
        # 优先选择名称与查询完全一致的结果
        station_id, city_name = next(
            (s for s in stations if s[1] == city), stations[0]
        )

        # 2. 实时天气（d1 sk_2d 接口，GBK，需 Referer）
        sk_raw = await _aget_bytes(
            CN_SK_URL.format(station_id), timeout=6.0, referer=CN_REFERER
        )
        sk = self._parse_cn_data_sk(_decode(sk_raw))
        temp = str(sk.get("temp") or "?")
        wd = str(sk.get("WD") or "未知")
        ws = str(sk.get("WS") or "")
        sd = str(sk.get("SD") or "未知")
        qy = str(sk.get("qy") or "未知")

        # 3. 今日预报（dingzhi 接口，GBK，需 Referer）
        dz_raw = await _aget_bytes(
            CN_DZ_URL.format(station_id), timeout=6.0, referer=CN_REFERER
        )
        dz = self._parse_cn_dingzhi(_decode(dz_raw))

        dweather = (dz or {}).get("weather") or ""
        demoji = _qweather_emoji(dweather) if dweather else "🌡️"
        dtemp = (dz or {}).get("temp") or ""
        dtempn = (dz or {}).get("tempn") or ""

        lines = [
            f"{demoji} {city_name} · 当前天气",
            f"天气：{dweather or '未知'}",
            f"温度：{temp}°C",
            f"湿度：{sd}",
            f"风向：{wd} {ws}".rstrip(),
            f"气压：{qy} hPa",
        ]
        if dtemp or dtempn:
            lines.append(f"今日：{dweather or ''}，{dtempn} ~ {dtemp}".strip(" ，"))
        lines.append("数据来源：中国天气网")
        return "\n".join(lines)

    @staticmethod
    def _parse_cn_search(text: str) -> List[Tuple[str, str]]:
        """解析搜索接口：([{"ref":"站号~拼音~中文名~..."},...])"""
        try:
            m = re.search(r"\((\[.*\])\)", text, re.DOTALL)
            if not m:
                return []
            items = json.loads(m.group(1))
            result = []
            for item in items:
                ref = (item.get("ref") or "").split("~")
                if len(ref) >= 3 and ref[0]:
                    result.append((ref[0], ref[2]))
            return result
        except Exception:
            return []

    @staticmethod
    def _parse_cn_data_sk(text: str) -> Dict[str, Any]:
        """解析实时接口：var dataSK={...}"""
        try:
            json_text = text.split("var dataSK=", 1)[1].split(";", 1)[0].strip()
            return json.loads(json_text)
        except Exception:
            return {}

    @staticmethod
    def _parse_cn_dingzhi(text: str) -> Dict[str, Any]:
        """解析预报接口：var cityDZ101010100 ={...};var alarmDZ..."""
        try:
            head = text.split("var alarmDZ")[0]
            json_text = head.split("=", 1)[1].strip().rstrip(";").strip()
            data = json.loads(json_text)
            return data.get("weatherinfo") or {}
        except Exception:
            return {}

    # ---------- 数据源 3：Open-Meteo（兜底，海外服务器） ----------

    async def _fetch_open_meteo(self, city: str) -> str:
        # 1. 地理编码：城市名 -> 经纬度
        geo = await _aget_json(GEO_URL, {"name": city, "count": 1, "language": "zh", "format": "json"})
        results = geo.get("results") or []
        if not results:
            raise RuntimeError(f"未找到城市“{city}”")
        loc = results[0]
        lat, lon = loc["latitude"], loc["longitude"]
        place = _format_place(loc)

        # 2. 实时 + 今日预报
        w = await _aget_json(
            WEATHER_URL,
            {
                "latitude": lat,
                "longitude": lon,
                "current": (
                    "temperature_2m,relative_humidity_2m,apparent_temperature,"
                    "weather_code,wind_speed_10m,wind_direction_10m,pressure_msl"
                ),
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
                "forecast_days": 1,
            },
        )
        cur = w.get("current") or {}
        code = cur.get("weather_code")
        emoji, label = WMO_CODES.get(code, ("🌡️", f"代码 {code}"))

        lines = [
            f"{emoji} {place} · 当前天气",
            f"天气：{label}",
            f"温度：{cur.get('temperature_2m')}°C（体感 {cur.get('apparent_temperature')}°C）",
            f"湿度：{cur.get('relative_humidity_2m')}%",
            f"风力：{cur.get('wind_speed_10m')} km/h",
            f"气压：{cur.get('pressure_msl')} hPa",
        ]
        daily = w.get("daily") or {}
        dcodes = daily.get("weather_code") or []
        if dcodes:
            demoji, dlabel = WMO_CODES.get(dcodes[0], ("🌡️", "未知"))
            tmax = (daily.get("temperature_2m_max") or [None])[0]
            tmin = (daily.get("temperature_2m_min") or [None])[0]
            lines.append(f"今日：{demoji} {dlabel}，{tmin}°C ~ {tmax}°C")
        lines.append("数据来源：Open-Meteo")
        return "\n".join(lines)
