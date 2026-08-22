"""
天气查询插件 astrbot_plugin_weather
====================================

AstrBot v4 插件。功能：

1. 命令查询：发送 `/天气 北京` 或 `/weather Shanghai`，返回实时天气、空气质量、
   未来三天预报；有气象预警时一并提示。
2. AI 工具调用：注册 get_weather 工具，AI 收到"北京天气怎么样"时自动查询。

数据源（按优先级自动切换）：
1. 和风天气 —— 在插件配置中填写 API Key 后启用，数据最全（推荐）。
   注意：和风官方公告公共 API 地址（devapi/geoapi.qweather.com）2026 年起逐步停服，
   2024 年后新注册用户必须在配置中填写控制台分配的专属 API Host。
2. 中国天气网 —— 零 API Key、国内服务器直连，国内部署默认数据源。
3. Open-Meteo —— 兜底，适合海外服务器（国内服务器可能无法访问）。

说明：
- 本插件面向 AstrBot v4（>= 3.5.19，无需 @register 装饰器，类继承 Star 即被自动识别）。
- 零第三方依赖，仅使用 Python 标准库。
"""

import asyncio
import gzip
import json
import logging
import os
import re
import time
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

# 中国天气网天气代码（fa/fb 字段）-> 中文描述
CN_WEATHER_CODES: Dict[str, str] = {
    "00": "晴",
    "01": "多云",
    "02": "阴",
    "03": "阵雨",
    "04": "雷阵雨",
    "05": "雷阵雨伴冰雹",
    "06": "雨夹雪",
    "07": "小雨",
    "08": "中雨",
    "09": "大雨",
    "10": "暴雨",
    "11": "大暴雨",
    "12": "特大暴雨",
    "13": "阵雪",
    "14": "小雪",
    "15": "中雪",
    "16": "大雪",
    "17": "暴雪",
    "18": "雾",
    "19": "冻雨",
    "20": "沙尘暴",
    "21": "小到中雨",
    "22": "中到大雨",
    "23": "大到暴雨",
    "24": "暴雨到大暴雨",
    "25": "大暴雨到特大暴雨",
    "26": "小到中雪",
    "27": "中到大雪",
    "28": "大到暴雪",
    "29": "浮尘",
    "30": "扬沙",
    "31": "强沙尘暴",
    "32": "飓风",
    "49": "强浓雾",
    "53": "霾",
    "54": "中度霾",
    "55": "重度霾",
    "56": "严重霾",
    "99": "无",
}

# 预警颜色 -> 标记
ALARM_MARKS = {"蓝色": "🔵", "黄色": "⚠️", "橙色": "🟠", "红色": "🔴"}

# Open-Meteo
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

# 和风天气：旧公共地址（官方公告 2026 年起逐步停服，新用户须配置专属 Host）
QWEATHER_GEO_URL = "https://geoapi.qweather.com/v2/city/lookup"
QWEATHER_V7_BASE = "https://devapi.qweather.com"

# 中国天气网（零 Key、国内直连）。weather_index 一次返回实况/今日预报/预警/生活指数/五日预报
CN_SEARCH_URL = "http://toy1.weather.com.cn/search"
CN_INDEX_URL = "http://d1.weather.com.cn/weather_index/{}.html"
CN_REFERER = "http://www.weather.com.cn/"

DEFAULT_CONFIG = {
    "qweather_api_key": "",   # 和风天气 API Key（可选）。留空则使用中国天气网（零 Key）
    "qweather_api_host": "",  # 和风天气专属 API Host（可选，2024 年后新注册用户必填，如 abc123.qweatherapi.com）
    "default_city": "",       # 默认城市（可选），如 "北京"；/天气 不带城市时使用
}

# 查询结果缓存 TTL（秒），避免同一城市短时间重复请求外部接口
CACHE_TTL = 600.0
CACHE_MAX_ENTRIES = 128

UA = "Mozilla/5.0 (AstrBot-Weather-Plugin/1.2)"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _http_get_bytes(
    url: str,
    params: Dict[str, Any] | None = None,
    timeout: float = 8.0,
    referer: str | None = None,
) -> bytes:
    """同步 HTTP GET 返回原始字节（兼容 GBK 等非 UTF-8 响应）。

    和风天气新版 API Host 可能强制 Gzip 压缩，检测魔数并解压。
    """
    full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(full_url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


async def _aget_bytes(
    url: str,
    params: Dict[str, Any] | None = None,
    timeout: float = 8.0,
    referer: str | None = None,
) -> bytes:
    """异步包装同步请求，不阻塞事件循环。"""
    return await asyncio.to_thread(_http_get_bytes, url, params, timeout, referer)


def _decode(raw: bytes) -> str:
    """优先 UTF-8，失败回退 GBK（中国天气网历史响应为 GBK 编码）。"""
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
    return json.loads(_decode(raw))


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
    if "沙" in text or "尘" in text:
        return "🌪️"
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


def _extract_json_var(text: str, var: str) -> Dict[str, Any]:
    """从 JS 变量赋值文本中提取 JSON 对象，如 `var dataSK = {...};`。

    用 raw_decode 从首个 { 起解析，可容忍等号两侧空格及字符串内出现的分号；
    解析失败返回空 dict。
    """
    m = re.search(rf"var\s+{re.escape(var)}\s*=", text)
    if not m:
        return {}
    start = text.find("{", m.end())
    if start == -1:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _aqi_category(aqi: Any) -> str:
    """AQI 数值 -> 等级描述（HJ 633-2012）。"""
    try:
        v = int(float(str(aqi)))
    except (TypeError, ValueError):
        return ""
    if v <= 50:
        return "优"
    if v <= 100:
        return "良"
    if v <= 150:
        return "轻度污染"
    if v <= 200:
        return "中度污染"
    if v <= 300:
        return "重度污染"
    return "严重污染"


def _deg_to_dir(deg: Any) -> str:
    """风向角度 -> 八方位中文。"""
    try:
        d = float(deg)
    except (TypeError, ValueError):
        return ""
    names = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")
    return names[int((d + 22.5) // 45) % 8]


def _cn_code_text(code: Any) -> str:
    """中国天气网天气代码 -> 中文描述。"""
    return CN_WEATHER_CODES.get(str(code or "").zfill(2), "未知")


def _combine_day_night(day_text: str, night_text: str) -> str:
    if not night_text or day_text == night_text:
        return day_text or "未知"
    if not day_text:
        return night_text
    return f"{day_text}转{night_text}"


# ---------------------------------------------------------------------------
# 插件主体（AstrBot v4：继承 Star 即自动注册，元数据来自 metadata.yaml）
# ---------------------------------------------------------------------------

class WeatherPlugin(Star):
    """天气查询插件。

    发送 `/天气 城市` 或 `/weather 城市` 查询实时天气、空气质量与未来三天预报。
    也可以直接问 AI："北京天气怎么样"（需在 AstrBot 中开启工具调用）。
    国内服务器默认使用中国天气网（零 Key），无需任何配置即可使用。
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

        # 优先使用 v4 原生配置（Web 面板可编辑，数据来自 _conf_schema.json）
        cfg = config if isinstance(config, dict) else {}
        self.qweather_key: str = str(cfg.get("qweather_api_key") or "").strip()
        self.qweather_host: str = str(cfg.get("qweather_api_host") or "").strip()
        self.default_city: str = str(cfg.get("default_city") or "").strip()

        # 兜底：v4 面板中留空的项，用插件目录下 config.json 的对应项补齐
        manual = self._load_manual_config()
        if not self.qweather_key:
            self.qweather_key = manual["qweather_api_key"]
        if not self.qweather_host:
            self.qweather_host = manual["qweather_api_host"]
        if not self.default_city:
            self.default_city = manual["default_city"]

        # 专属 Host 只填域名即可，容忍误带协议头/末尾斜杠
        self.qweather_host = (
            self.qweather_host.removeprefix("https://").removeprefix("http://").rstrip("/")
        )

        # 查询结果缓存：{城市小写: (monotonic 时间戳, 报告文本)}
        self._cache: Dict[str, Tuple[float, str]] = {}

        logger.info(
            "天气插件已加载。数据源: %s%s，默认城市: %s",
            "和风天气" if self.qweather_key else "中国天气网（零 Key）→ Open-Meteo 兜底",
            f"（Host: {self.qweather_host}）" if self.qweather_key and self.qweather_host else "",
            self.default_city or "未设置",
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
        city = self._extract_city(event) or self.default_city
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
        """查询指定城市或地区的当前天气（温度、湿度、风力、空气质量）以及未来三天天气趋势，如有气象预警会一并返回。

        Args:
            city(string): 要查询天气的城市或地区名称，例如：北京、上海、杭州、广州。不填则查询默认城市
        """

        city = (city or "").strip() or self.default_city
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
        """数据源优先级：和风天气（配置了 Key）→ 中国天气网（零 Key）→ Open-Meteo。

        每个数据源设整体超时（避免单源多次请求超时叠加拖垮总耗时），
        成功结果缓存 CACHE_TTL 秒。
        """
        cache_key = city.strip().lower()
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < CACHE_TTL:
            return cached[1]

        sources: List[Tuple[str, Any, float]] = []
        if self.qweather_key:
            sources.append(("和风天气", self._fetch_qweather, 20.0))
        sources.append(("中国天气网", self._fetch_cn, 13.0))
        sources.append(("Open-Meteo", self._fetch_open_meteo, 16.0))

        errors: List[str] = []
        for name, fetch, budget in sources:
            try:
                report = await asyncio.wait_for(fetch(city), timeout=budget)
            except Exception as e:
                errors.append(f"{name}: {e}")
                continue
            if len(self._cache) >= CACHE_MAX_ENTRIES:
                now = time.monotonic()
                self._cache = {
                    k: v for k, v in self._cache.items() if now - v[0] < CACHE_TTL
                }
            self._cache[cache_key] = (time.monotonic(), report)
            return report
        raise RuntimeError("所有数据源均不可用。\n" + "\n".join(errors))

    @staticmethod
    def _append_alarm_lines(lines: List[str], alarms: List[Dict[str, Any]]) -> None:
        """把预警列表（最多 2 条）追加到输出。"""
        for a in alarms[:2]:
            title = str(a.get("title") or a.get("w13") or "").strip()
            if not title:
                title = f"{a.get('w1', '')}发布{a.get('w5', '')}{a.get('w7', '')}色预警"
            color = str(a.get("level") or a.get("w7") or "")
            mark = ALARM_MARKS.get(color, "⚠️")
            lines.append(f"{mark} 预警：{title}")
            body = str(a.get("text") or a.get("w9") or "").strip()
            if body:
                lines.append(f"　{body[:100]}{'…' if len(body) > 100 else ''}")

    # ---------- 数据源 1：和风天气 ----------

    def _qw_geo_url(self) -> str:
        if self.qweather_host:
            return f"https://{self.qweather_host}/geo/v2/city/lookup"
        return QWEATHER_GEO_URL

    def _qw_v7_url(self, path: str) -> str:
        if self.qweather_host:
            return f"https://{self.qweather_host}{path}"
        return QWEATHER_V7_BASE + path

    async def _fetch_qweather(self, city: str) -> str:
        # 1. 城市查找 -> LocationID
        geo = await _aget_json(
            self._qw_geo_url(), {"location": city, "key": self.qweather_key, "number": 1}
        )
        if geo.get("code") != "200" or not (geo.get("location") or []):
            raise RuntimeError(f"和风天气未找到城市“{city}”")
        loc = geo["location"][0]
        loc_id = loc["id"]
        name = loc.get("name") or city
        adm1 = loc.get("adm1") or ""
        country = loc.get("country") or ""

        # 2. 实时天气
        now = await _aget_json(
            self._qw_v7_url("/v7/weather/now"), {"location": loc_id, "key": self.qweather_key}
        )
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

        # 3. 空气质量（尽力而为，失败不影响整体）
        try:
            air = await _aget_json(
                self._qw_v7_url("/v7/air/now"),
                {"location": loc_id, "key": self.qweather_key},
                timeout=5.0,
            )
            a = air.get("now") or {}
            if air.get("code") == "200" and a.get("aqi"):
                lines.append(f"空气质量：{a.get('category') or _aqi_category(a.get('aqi'))}（AQI {a.get('aqi')}）")
        except Exception:
            pass

        # 4. 三日预报（3d 接口）
        d3 = await _aget_json(
            self._qw_v7_url("/v7/weather/3d"), {"location": loc_id, "key": self.qweather_key}
        )
        daily = d3.get("daily") or [] if d3.get("code") == "200" else []
        if daily:
            day = daily[0]
            dtext = _combine_day_night(day.get("textDay") or "", day.get("textNight") or "")
            demoji = _qweather_emoji(dtext)
            sun = ""
            if day.get("sunrise") or day.get("sunset"):
                sun = f"（日出 {day.get('sunrise', '?')} · 日落 {day.get('sunset', '?')}）"
            lines.append(
                f"今日：{demoji} {dtext}，{day.get('tempMin')}°C ~ {day.get('tempMax')}°C{sun}"
            )
            for label, day in zip(("明天", "后天"), daily[1:3]):
                dtext = _combine_day_night(day.get("textDay") or "", day.get("textNight") or "")
                demoji = _qweather_emoji(dtext)
                lines.append(
                    f"{label}：{demoji} {dtext}，{day.get('tempMin')}°C ~ {day.get('tempMax')}°C"
                )

        # 5. 气象预警（尽力而为）
        try:
            warn = await _aget_json(
                self._qw_v7_url("/v7/warning/now"),
                {"location": loc_id, "key": self.qweather_key},
                timeout=5.0,
            )
            if warn.get("code") == "200":
                self._append_alarm_lines(lines, warn.get("warning") or [])
        except Exception:
            pass

        lines.append("数据来源：和风天气")
        return "\n".join(lines)

    # ---------- 数据源 2：中国天气网（零 Key，国内直连） ----------

    async def _fetch_cn(self, city: str) -> str:
        # 1. 城市名 -> 站号（toy1.weather.com.cn 搜索接口）
        raw = await _aget_bytes(CN_SEARCH_URL, {"cityname": city}, timeout=5.0, referer=CN_REFERER)
        stations = self._parse_cn_search(_decode(raw))
        if not stations:
            raise RuntimeError(f"未找到城市“{city}”")
        # 优先选择名称与查询完全一致的结果
        station_id, city_name = next(
            (s for s in stations if s[1] == city), stations[0]
        )

        # 2. weather_index 一次返回实况(dataSK)/今日预报(cityDZ)/预警(alarmDZ)/生活指数(dataZS)/五日预报(fc)
        #    海外站点只有 fc（无实况），走仅预报模式
        index_raw = await _aget_bytes(
            CN_INDEX_URL.format(station_id), timeout=6.0, referer=CN_REFERER
        )
        text = _decode(index_raw)
        sk = _extract_json_var(text, "dataSK")
        dz = (_extract_json_var(text, "cityDZ").get("weatherinfo") or {})
        fc_days = _extract_json_var(text, "fc").get("f") or []
        zs = _extract_json_var(text, "dataZS").get("zs") or {}
        alarms = _extract_json_var(text, "alarmDZ").get("w") or []

        if not sk and not fc_days:
            # 数据解析失败说明接口格式变化或站点无数据，抛错以触发后续数据源兜底
            raise RuntimeError("中国天气网数据解析失败")

        if not sk:
            # 海外站点：仅有未来预报，无实况
            first = _combine_day_night(
                _cn_code_text(fc_days[0].get("fa")), _cn_code_text(fc_days[0].get("fb"))
            )
            lines = [f"{_qweather_emoji(first)} {city_name} · 天气预报（暂无实况）"]
            for item in fc_days[:3]:
                label = str(item.get("fj") or "").strip() or str(item.get("fi") or "预报")
                lines.append(self._format_cn_forecast_line(label, item))
            lines.append("数据来源：中国天气网")
            return "\n".join(lines)

        # 实况字段
        weather_now = str(sk.get("weather") or "").strip() or dz.get("weather") or "未知"
        emoji = _qweather_emoji(weather_now)
        temp = str(sk.get("temp") or "?")
        wd = str(sk.get("WD") or "未知")
        ws = str(sk.get("WS") or "")
        sd = str(sk.get("SD") or "未知")
        qy = str(sk.get("qy") or "未知")
        aqi = str(sk.get("aqi") or "").strip()

        lines = [
            f"{emoji} {city_name} · 当前天气",
            f"天气：{weather_now}",
            f"温度：{temp}°C",
            f"湿度：{sd}",
            f"风向：{wd} {ws}".rstrip(),
            f"气压：{qy} hPa",
        ]
        if aqi:
            lines.append(f"空气质量：{_aqi_category(aqi) or '未知'}（AQI {aqi}）")

        # 今日预报：优先 fc 第一天（cityDZ 的温度偶有倒挂/滞后），fc 缺失时用 cityDZ
        if fc_days:
            lines.append(self._format_cn_forecast_line("今日", fc_days[0]))
        elif dz.get("weather") or dz.get("temp") or dz.get("tempn"):
            lines.append(
                f"今日：{dz.get('weather') or '未知'}，{dz.get('tempn') or '?'} ~ {dz.get('temp') or '?'}"
            )

        # 明天起的三日预报
        for item in fc_days[1:4]:
            label = str(item.get("fj") or "").strip() or "预报"
            lines.append(self._format_cn_forecast_line(label, item))

        # 生活指数（穿衣/紫外线/感冒）
        idx_parts = []
        for key, label in (("ct", "穿衣"), ("uv", "紫外线"), ("gm", "感冒")):
            hint = str(zs.get(f"{key}_hint") or "").strip()
            if hint:
                idx_parts.append(f"{label} {hint}")
        if idx_parts:
            lines.append("生活指数：" + " · ".join(idx_parts))

        # 气象预警
        self._append_alarm_lines(lines, alarms)

        lines.append("数据来源：中国天气网")
        return "\n".join(lines)

    @staticmethod
    def _format_cn_forecast_line(label: str, item: Dict[str, Any]) -> str:
        """中国天气网 fc 条目 -> 一行预报文本。"""
        dtext = _combine_day_night(_cn_code_text(item.get("fa")), _cn_code_text(item.get("fb")))
        demoji = _qweather_emoji(dtext)
        low = str(item.get("fd") or "?")
        high = str(item.get("fc") or "?")
        return f"{label}：{demoji} {dtext}，{low} ~ {high}℃"

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
                if len(ref) >= 3 and ref[0] and ref[2]:
                    result.append((ref[0], ref[2]))
            return result
        except Exception:
            return []

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

        # 2. 实时 + 三日预报
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
                "forecast_days": 3,
            },
        )
        cur = w.get("current") or {}
        code = cur.get("weather_code")
        emoji, label = WMO_CODES.get(code, ("🌡️", f"代码 {code}"))

        wind = f"风力：{cur.get('wind_speed_10m')} km/h"
        wdir = _deg_to_dir(cur.get("wind_direction_10m"))
        if wdir:
            wind += f"（{wdir}风）"

        lines = [
            f"{emoji} {place} · 当前天气",
            f"天气：{label}",
            f"温度：{cur.get('temperature_2m')}°C（体感 {cur.get('apparent_temperature')}°C）",
            f"湿度：{cur.get('relative_humidity_2m')}%",
            wind,
            f"气压：{cur.get('pressure_msl')} hPa",
        ]
        daily = w.get("daily") or {}
        dcodes = daily.get("weather_code") or []
        tmaxs = daily.get("temperature_2m_max") or []
        tmins = daily.get("temperature_2m_min") or []
        for i, day_label in enumerate(("今日", "明天", "后天")):
            if i >= len(dcodes):
                break
            demoji, dlabel = WMO_CODES.get(dcodes[i], ("🌡️", "未知"))
            tmax = tmaxs[i] if i < len(tmaxs) else "?"
            tmin = tmins[i] if i < len(tmins) else "?"
            lines.append(f"{day_label}：{demoji} {dlabel}，{tmin}°C ~ {tmax}°C")
        lines.append("数据来源：Open-Meteo")
        return "\n".join(lines)
