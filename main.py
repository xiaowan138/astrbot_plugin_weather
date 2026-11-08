"""
天气查询插件 astrbot_plugin_weather
====================================

AstrBot v4 插件（建议 v4.27+）。功能：

1. 命令查询：
   - /天气 [城市]      实时天气 + 空气质量 + 今日趋势 + 气象预警（可渲染图片卡片）
   - /预报 [城市]      未来 7 天预报
   - /空气质量 [城市]  AQI 与污染物详情
   - /降雨 [城市]      未来 24 小时逐小时降水趋势
   - /对比 城市1 城市2  多城市天气对比（最多 5 个）
   - /绑定 [城市]      绑定个人默认城市（按发送者存储，重名城市可交互选择）
   - /解绑             解除绑定
   - /订阅 [城市] [时间] 每日定时推送（如 /订阅 北京 07:30）
   - /退订             取消本会话的订阅
2. AI 工具调用：get_weather / get_weather_forecast / get_air_quality。

数据源（按优先级自动切换）：
1. 和风天气 —— 需在配置中同时填写 API Key 与专属 API Host。
   注意：和风 WebAPI v7 已全线弃用（空气质量 2026-06 停服、预警 2026-10 停服、
   城市天气 2027-06 停服），本插件已全部迁移到 v1 新接口
   （/weather/v1、/airquality/v1、/weatheralert/v1，按经纬度查询，X-QW-Api-Key 认证）。
2. 中国天气网 —— 零 API Key、国内服务器直连，国内部署默认数据源。
3. Open-Meteo —— 兜底（含独立空气质量接口），适合海外服务器。

说明：
- 零第三方依赖，仅使用 Python 标准库。
- 绑定/订阅数据优先存放在 AstrBot 插件 KV 存储，旧版本自动回退本地 JSON 文件。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

try:
    from zoneinfo import ZoneInfo
except Exception:  # Python < 3.9 无 zoneinfo
    ZoneInfo = None  # type: ignore

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

# GreedyStr：让命令的最后一个参数吞掉整段剩余文本（支持 “New York” 这类多词城市）
try:
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # 旧版本 AstrBot 无此类型，退化为普通 str（仅失去多词城市支持）
    GreedyStr = str

# 会话控制器：用于重名城市交互选择
try:
    from astrbot.core.utils.session_waiter import (
        SessionController,
        SessionFilter,
        session_waiter,
    )
except Exception:
    session_waiter = None
    SessionController = None
    SessionFilter = None

CityInput = Union[str, Dict[str, Any]]
# 定位结果：{"source": "qw"|"cn"|"om", "name", "display", "place",
#            "lat"/"lon"（qw、om）或 "station_id"（cn）}

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

# 预警颜色 -> 标记（和风 v1 color.code / 中国天气网预警文本）
ALARM_MARKS = {
    "blue": "🔵",
    "蓝色": "🔵",
    "yellow": "⚠️",
    "amber": "⚠️",
    "黄色": "⚠️",
    "orange": "🟠",
    "橙色": "🟠",
    "red": "🔴",
    "红色": "🔴",
    "purple": "🟣",
    "white": "⚪",
    "gray": "⚪",
    "green": "🟢",
    "绿色": "🟢",
    "black": "🟣",
    "黑色": "🟣",
}

# 和风降水类型 -> 中文
QW_PRECIP_TYPES = {
    "rain": "雨",
    "snow": "雪",
    "ice": "冰粒",
    "mixed": "雨夹雪",
    "none": "无",
    "unknown": "未知",
}

# AQI 等级 -> 卡片颜色
AQI_COLORS = [
    (50, "优", "#35c25e"),
    (100, "良", "#e6d306"),
    (150, "轻度污染", "#ff7e33"),
    (200, "中度污染", "#ff3b30"),
    (300, "重度污染", "#9b1b6b"),
    (9999, "严重污染", "#7e0023"),
]

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# Open-Meteo
OM_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
OM_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OM_AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# 中国天气网（零 Key、国内直连）。weather_index 一次返回实况/今日预报/预警/生活指数/多日预报
CN_SEARCH_URL = "http://toy1.weather.com.cn/search"
CN_INDEX_URL = "http://d1.weather.com.cn/weather_index/{}.html"
CN_REFERER = "http://www.weather.com.cn/"

DEFAULT_CONFIG = {
    "qweather_api_key": "",     # 和风天气 API Key
    "qweather_api_host": "",    # 和风天气专属 API Host（使用 v1 新接口时必填）
    "default_city": "",         # 全局默认城市
    "image_card": True,         # 天气/预报渲染图片卡片（失败自动回退纯文本）
    "interactive_choice": True, # 重名城市时交互式选择
    "push_time_default": "07:30",  # 订阅推送默认时间
    "timezone": "",             # 推送/时间判定所用时区，留空=服务器本地
}

CACHE_TTL = 600.0          # 查询结果缓存（秒）
CACHE_MAX_ENTRIES = 128    # 缓存条目上限
CACHE_STALE_WINDOW = 3.0   # 软过期窗口：超过 TTL 但不足 TTL*该倍数的旧数据仍可秒回并后台刷新
LOCATE_TTL = 21600.0       # 城市定位结果缓存（秒，6 小时）
PUSH_LOOP_INTERVAL = 25    # 订阅推送检查间隔（秒）
DISAMBIG_TIMEOUT = 45      # 重名城市选择超时（秒）
MAX_COMPARE_CITIES = 5

PLUGIN_VERSION = "2.2.0"

UA = "Mozilla/5.0 (AstrBot-Weather-Plugin/2.2)"

STORE_KEYS = ("user_city", "push_subs")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _http_get_bytes(
    url: str,
    params: Dict[str, Any] | None = None,
    timeout: float = 8.0,
    referer: str | None = None,
    headers: Dict[str, str] | None = None,
) -> bytes:
    """同步 HTTP GET 返回原始字节（兼容 GBK 等非 UTF-8 响应）。

    和风天气新版 API Host 强制 Gzip 压缩，检测魔数并解压。
    """
    full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    req_headers = {"User-Agent": UA}
    if referer:
        req_headers["Referer"] = referer
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(full_url, headers=req_headers)
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
    headers: Dict[str, str] | None = None,
) -> bytes:
    """异步包装同步请求，不阻塞事件循环。"""
    return await asyncio.to_thread(_http_get_bytes, url, params, timeout, referer, headers)


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
    headers: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    """GET 并解析 JSON（用于 Open-Meteo / 和风天气）。"""
    raw = await _aget_bytes(url, params, timeout, headers=headers)
    return json.loads(_decode(raw))


def _num(v: Any) -> str:
    """数值 -> 紧凑字符串（31.0 -> "31"，31.71 -> "31.7"）；无法解析返回空串。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    r = round(f, 1)
    return str(int(r)) if r == int(r) else str(r)


def _frac_pct(v: Any) -> str:
    """和风 v1 的 [0,1] 小数 -> 百分数字符串。"""
    try:
        return str(int(round(float(v) * 100)))
    except (TypeError, ValueError):
        return ""


def _pct(v: Any) -> str:
    """已是百分数值（0-100）-> 带百分号字符串。"""
    s = _num(v)
    return f"{s}%" if s else ""


def _pct_from_frac(v: Any) -> str:
    """和风 v1 的 [0,1] 小数（湿度等）-> 带百分号字符串。

    Bug 修复：原实现只返回数字（"63"），与中国天气网返回的 "63%" 不一致，
    导致同一行“湿度”在不同数据源下显示不同。
    """
    s = _frac_pct(v)
    return f"{s}%" if s else ""


def _vis_km(vis: Any) -> str:
    """能见度 -> 公里字符串。

    Bug 修复：和风 v1 的 visibility.value 单位是「米」（文档示例 29020 = 29 公里），
    原实现直接当公里用，卡片会显示成 "29020 km"（放大 1000 倍）。
    """
    if not isinstance(vis, dict):
        return ""
    v = vis.get("value")
    unit = str(vis.get("unit") or "").strip().lower()
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if unit in ("m", "meter", "meters", "米"):
        f = f / 1000.0
    km = round(f, 1)
    return str(int(km)) if km == int(km) else str(km)


def _iso_dt(s: Any) -> datetime | None:
    """解析 ISO 时间（兼容尾部 Z），失败返回 None。"""
    text = str(s or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _alarm_time_text(s: Any) -> str:
    """预警失效时间 -> "MM-DD HH:MM"，解析失败返回空串。"""
    dt = _iso_dt(s)
    return dt.strftime("%m-%d %H:%M") if dt else ""


def _alarm_expired(expire: Any, now: datetime) -> bool:
    """预警是否已过失效时间。时区不一致时统一换算后再比。"""
    dt = _iso_dt(expire)
    if dt is None:
        return False
    if now.tzinfo is None:
        # now 是 naive（服务器本地时间），把带时区的失效时间换算成本地时间再比
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt <= now
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now.tzinfo)
    return dt <= now


def _parse_subscribe_args(text: Any) -> Tuple[str, str | None, bool]:
    """解析 /订阅 参数 -> (城市, 推送时间, 是否仅工作日)。

    Bug 修复：原实现把「纯数字小时」的判断绑在 `not city` 上，导致
    `/订阅 北京 7`、`/订阅 北京 7点` 里的 7 被拼进城市名（变成「北京 7」）。
    现在无论城市是否已出现，`7`、`7点`、`7时` 都识别为时间。
    """
    tokens = _city_tokens(str(text or ""))
    hour_only = None
    push_time: str | None = None
    weekdays_only = False
    city_parts: List[str] = []

    for tok in tokens:
        low = tok.lower()
        if low in ("工作日", "仅工作日", "weekday", "weekdays"):
            weekdays_only = True
            continue
        if low in ("每天", "每日", "daily", "每天推送"):
            weekdays_only = False
            continue
        m = re.fullmatch(r"(\d{1,2})[:：时点](\d{1,2})分?", tok)
        if m and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
            push_time = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
            continue
        m2 = re.fullmatch(r"(\d{1,2})[点时]?", tok)
        if m2 and int(m2.group(1)) <= 23:
            hour_only = int(m2.group(1))
            continue
        city_parts.append(tok)

    if push_time is None and hour_only is not None:
        push_time = f"{hour_only:02d}:00"

    return " ".join(city_parts).strip(), push_time, weekdays_only


def _first_number(v: Any) -> float | None:
    """从任意值里提取第一个数字（"西南风 6级" -> 6.0），失败返回 None。"""
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or ""))
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def _yesterday_text(data: Dict[str, Any]) -> str:
    """昨日温差描述；数据源不支持时返回空串。"""
    yd = data.get("yesterday") or {}
    diff = yd.get("diff")
    if diff is None:
        return ""
    try:
        d = float(diff)
    except (TypeError, ValueError):
        return ""
    high = yd.get("high", "?")
    if d > 0:
        return f"最高温比昨天高 {d:g}℃（昨日 {high}℃）"
    if d < 0:
        return f"最高温比昨天低 {abs(d):g}℃（昨日 {high}℃）"
    return f"最高温与昨天持平（{high}℃）"


def _travel_advice(data: Dict[str, Any]) -> str:
    """按天气数据生成简短出行建议（纯规则推导，不依赖具体数据源）。"""
    tips: List[str] = []
    today = data.get("today") or {}
    high = _first_number(today.get("high"))
    low = _first_number(today.get("low"))
    pop = _first_number(today.get("pop"))
    aqi = _first_number((data.get("aqi") or {}).get("value"))
    uv = _first_number(data.get("uv"))
    wind = _first_number(data.get("wind"))

    if high is not None and high >= 33:
        tips.append("高温防暑，多补水")
    elif low is not None and low <= 0:
        tips.append("气温低，注意保暖")
    if pop is not None and pop >= 60:
        tips.append("降水概率高，记得带伞")
    if aqi is not None and aqi >= 150:
        tips.append("空气质量差，建议戴口罩")
    if uv is not None and uv >= 7:
        tips.append("紫外线强，注意防晒")
    if wind is not None and wind >= 6:
        tips.append("风力较大，注意防风")
    if (data.get("alarms") or []):
        tips.append("有气象预警，关注最新通知")
    return " · ".join(tips[:4]) if tips else "天气适宜出行"


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
    if name and admin and (admin.startswith(name) or name.startswith(admin)):
        if len(admin) >= len(name):
            name = ""
        else:
            admin = ""
    parts = [p for p in (country, admin, name) if p]
    return " ".join(parts)


def _extract_json_var(text: str, var: str) -> Dict[str, Any]:
    """从 JS 变量赋值文本中提取 JSON 对象，如 `var dataSK = {...};`。"""
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


def _aqi_color(aqi: Any, category: str = "") -> str:
    """AQI 数值/等级 -> 卡片颜色。"""
    try:
        v = int(float(str(aqi)))
    except (TypeError, ValueError):
        v = -1
    if v >= 0:
        for threshold, _, color in AQI_COLORS:
            if v <= threshold:
                return color
    cat = category or ""
    if "严重" in cat or "Hazardous" in cat:
        return "#7e0023"
    if "重度" in cat or "Very" in cat:
        return "#9b1b6b"
    if "中度" in cat or cat in ("Unhealthy",):
        return "#ff3b30"
    if "轻度" in cat or "敏感" in cat or "Sensitive" in cat:
        return "#ff7e33"
    if "良" in cat or "Moderate" in cat:
        return "#e6d306"
    return "#35c25e"


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


def _weekday_of(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return WEEKDAYS[d.weekday()]
    except ValueError:
        return ""


def _md_of(date_str: str) -> str:
    s = (date_str or "")[:10]
    return s[5:].replace("-", "/") if len(s) >= 10 else s


def _hm_of(iso_time: str) -> str:
    """ISO 时间（本地时）-> "HH:MM"。"""
    s = iso_time or ""
    if "T" in s and len(s) >= 16:
        return s[11:16]
    return ""


def _disp_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(1, width - _disp_width(s))


def _city_tokens(text: str) -> List[str]:
    return [t for t in re.split(r"[\s,，、]+", (text or "").strip()) if t]


# ---------------------------------------------------------------------------
# 图片卡片模板（Jinja2 + 内联 CSS，无外部资源依赖）
# ---------------------------------------------------------------------------

REALTIME_CARD_TMPL = """
<div style="width:640px; box-sizing:border-box; font-family:'PingFang SC','Microsoft YaHei',sans-serif;
 background:linear-gradient(160deg,#3d7ec9 0%,#4a90d9 40%,#7db8e8 100%);
 border-radius:22px; padding:26px 28px; color:#ffffff;">
  <div style="display:flex; justify-content:space-between; align-items:flex-start;">
    <div>
      <div style="font-size:30px; font-weight:700;">{{ city }}</div>
      <div style="font-size:13px; opacity:.85; margin-top:5px;">{{ place }} · {{ update_time }}</div>
    </div>
    <div style="font-size:52px; line-height:1;">{{ emoji }}</div>
  </div>
  <div style="display:flex; align-items:flex-end; gap:16px; margin-top:16px;">
    <div style="font-size:62px; font-weight:200; line-height:1;">{{ temp }}°</div>
    <div style="padding-bottom:8px;">
      <div style="font-size:18px;">{{ cond }}</div>
      {% if feels %}<div style="font-size:12px; opacity:.85; margin-top:2px;">体感 {{ feels }}°C</div>{% endif %}
    </div>
    {% if aqi %}
    <div style="padding-bottom:10px; margin-left:auto; background:{{ aqi.color }}; color:#222222;
     border-radius:12px; padding:7px 14px; font-size:13px; font-weight:600;">
      AQI {{ aqi.value }} · {{ aqi.category }}</div>
    {% endif %}
  </div>
  {% if metrics %}
  <div style="display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-top:18px;">
    {% for m in metrics %}
    <div style="background:rgba(255,255,255,.16); border-radius:12px; padding:9px 12px;">
      <div style="opacity:.75; font-size:11px;">{{ m.label }}</div>
      <div style="font-size:15px; margin-top:2px;">{{ m.value }}</div>
    </div>
    {% endfor %}
  </div>
  {% endif %}
  {% if today %}
  <div style="margin-top:16px; background:rgba(255,255,255,.13); border-radius:14px;
   padding:11px 16px; font-size:14px; display:flex; align-items:center; gap:10px;">
    <span>今日 {{ today.emoji }} {{ today.text }}</span>
    <span style="margin-left:auto;">{{ today.low }}° ~ {{ today.high }}°C</span>
    {% if today.sun %}<span style="opacity:.8; font-size:11px; margin-left:10px;">{{ today.sun }}</span>{% endif %}
  </div>
  {% endif %}
  {% if days %}
  <div style="margin-top:10px; display:grid; grid-template-columns:repeat({{ days|length }},1fr); gap:8px; text-align:center;">
    {% for d in days %}
    <div style="background:rgba(255,255,255,.10); border-radius:10px; padding:8px 4px; font-size:12px;">
      <div style="opacity:.8;">{{ d.label }}</div>
      <div style="font-size:20px; margin:3px 0;">{{ d.emoji }}</div>
      <div>{{ d.low }}°/{{ d.high }}°</div>
    </div>
    {% endfor %}
  </div>
  {% endif %}
  {% if alarms %}
  <div style="margin-top:14px; border-radius:12px; background:rgba(255,59,48,.30);
   border:1px solid rgba(255,59,48,.55); padding:10px 14px; font-size:13px;">
    {% for a in alarms %}<div style="font-weight:600;">{{ a.mark }} {{ a.title }}</div>{% endfor %}
  </div>
  {% endif %}
  {% if advice %}
  <div style="margin-top:14px; background:rgba(255,255,255,.13); border-radius:12px;
   padding:9px 14px; font-size:13px;">💡 出行建议：{{ advice }}</div>
  {% endif %}
  <div style="margin-top:14px; font-size:11px; opacity:.75; display:flex; justify-content:space-between;">
    <span>数据来源：{{ source }}</span>
    {% if tips %}<span>{{ tips }}</span>{% endif %}
  </div>
</div>
"""

FORECAST_CARD_TMPL = """
<div style="width:640px; box-sizing:border-box; font-family:'PingFang SC','Microsoft YaHei',sans-serif;
 background:linear-gradient(165deg,#2f5d8f 0%,#3d7ec9 55%,#6aa7dd 100%);
 border-radius:22px; padding:26px 28px; color:#ffffff;">
  <div style="display:flex; justify-content:space-between; align-items:center;">
    <div>
      <div style="font-size:26px; font-weight:700;">{{ city }} · 未来预报</div>
      <div style="font-size:13px; opacity:.85; margin-top:5px;">{{ place }}</div>
    </div>
    <div style="font-size:34px;">📅</div>
  </div>
  {% for d in days %}
  <div style="margin-top:12px; background:rgba(255,255,255,{{ '0.18' if loop.first else '0.10' }});
   border-radius:14px; padding:11px 16px; display:flex; align-items:center; gap:12px; font-size:14px;">
    <div style="width:96px;">
      <div style="font-weight:600;">{{ d.label }}</div>
      <div style="font-size:11px; opacity:.8;">{{ d.date }}{% if d.weekday %} {{ d.weekday }}{% endif %}</div>
    </div>
    <div style="font-size:24px;">{{ d.emoji }}</div>
    <div style="flex:1;">{{ d.text }}{% if d.pop %} <span style="opacity:.85; font-size:12px;">☔ {{ d.pop }}</span>{% endif %}</div>
    <div style="font-size:16px; font-weight:600;">{{ d.low }}° ~ {{ d.high }}°</div>
  </div>
  {% endfor %}
  <div style="margin-top:14px; font-size:11px; opacity:.75;">数据来源：{{ source }}</div>
</div>
"""


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------

class WeatherPlugin(Star):
    """天气查询插件。

    发送 /天气 城市查询实时天气，/预报 查看 7 天预报，/空气质量 /降雨 /对比 查看细分数据；
    /绑定城市 设置个人默认城市，/订阅 开启每日定时推送。
    也可以直接问 AI：“北京天气怎么样”（需在 AstrBot 中开启工具调用）。
    """

    name = "astrbot_plugin_weather"
    author = "xiaowan138"

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

        # 优先使用 v4 原生配置（Web 面板可编辑，数据来自 _conf_schema.json）
        cfg = config if isinstance(config, dict) else {}
        self.qweather_key: str = str(cfg.get("qweather_api_key") or "").strip()
        self.qweather_host: str = str(cfg.get("qweather_api_host") or "").strip()
        self.default_city: str = str(cfg.get("default_city") or "").strip()
        self.image_card: bool = bool(cfg.get("image_card", True))
        self.interactive_choice: bool = bool(cfg.get("interactive_choice", True))
        self.push_time_default: str = str(cfg.get("push_time_default") or "").strip()
        self.timezone: str = str(cfg.get("timezone") or "").strip()

        # 兜底：v4 面板中留空的项，用插件目录下 config.json 的对应项补齐
        manual = self._load_manual_config()
        if not self.qweather_key:
            self.qweather_key = manual["qweather_api_key"]
        if not self.qweather_host:
            self.qweather_host = manual["qweather_api_host"]
        if not self.default_city:
            self.default_city = manual["default_city"]
        if not self.push_time_default:
            self.push_time_default = str(manual.get("push_time_default") or "07:30").strip()
        if not self.timezone:
            self.timezone = str(manual.get("timezone") or "").strip()

        # 时区：留空用服务器本地时区；配置后订阅推送与“当前时间”提示都按该时区计算
        self._tz: Any = None
        if self.timezone:
            try:
                self._tz = ZoneInfo(self.timezone)
            except Exception as e:
                logger.warning(
                    "天气插件：时区 %s 无效（%s），已回退服务器本地时区。"
                    "可用值如 Asia/Shanghai、Asia/Tokyo、UTC。",
                    self.timezone, e,
                )
                self.timezone = ""

        # 专属 Host 只填域名即可，容忍误带协议头/末尾斜杠
        self.qweather_host = (
            self.qweather_host.removeprefix("https://").removeprefix("http://").rstrip("/")
        )

        # Bug 修复：和风 v1 新接口只在专属 API Host 上提供。只填 Key 不填 Host 时
        # 明确告警并跳过和风数据源（避免拿着 Key 去请求早已停服的公共地址、
        # 再静默回退，让用户误以为 Key 已生效）。
        self._qw_ready = bool(self.qweather_key and self.qweather_host)
        if self.qweather_key and not self.qweather_host:
            logger.warning(
                "天气插件：已配置和风天气 API Key，但未配置专属 API Host（qweather_api_host）。"
                "和风 WebAPI v7 已弃用（空气质量已停服、预警 2026-10 停服），"
                "新 v1 接口仅支持专属 Host，已跳过和风数据源。"
                "请在控制台-设置 中查看 Host（形如 abc123.qweatherapi.com）并填入插件配置。"
            )

        # 查询结果缓存：{缓存键: (monotonic 时间戳, 归一化数据)}
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

        # 城市定位结果缓存：{小写城市名: (monotonic 时间戳, 定位结果)}
        # 修性能问题：原实现每次查询都要重新打一次城市搜索接口（国内该接口可能耗时数秒）。
        self._loc_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

        # 正在后台刷新的缓存键（避免软过期时同一键重复起刷新任务）
        self._refreshing: set[str] = set()

        # 持久化存储的「读-改-写」互斥锁。绑定/订阅/推送都是读 dict → 改 → 写回，
        # 没有锁时多人同时操作会互相覆盖，直接丢掉一条绑定或订阅。
        self._store_lock = asyncio.Lock()

        # 持久化存储（绑定/订阅）：优先 AstrBot 插件 KV，回退本地 JSON
        self._kv_ok = callable(getattr(self, "put_kv_data", None)) and callable(
            getattr(self, "get_kv_data", None)
        )
        self._store_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "store.json"
        )

        self._push_task: asyncio.Task | None = None

        logger.info(
            "天气插件已加载。数据源: %s%s，默认城市: %s%s%s",
            "和风天气(v1)" if self._qw_ready else "中国天气网（零 Key）→ Open-Meteo 兜底",
            f"（Host: {self.qweather_host}）" if self._qw_ready else "",
            self.default_city or "未设置",
            "，卡片" if self.image_card else "，纯文本",
            "，交互选择" if self.interactive_choice else "",
        )

    # ---------------- 时间 ----------------

    def _now(self) -> datetime:
        """当前时间：配置了 timezone 就按该时区，否则用服务器本地时区。

        用于订阅推送的时间判定与“当前时间”提示，避免 UTC 服务器上
        /订阅 07:30 实际却在北京时间 15:30 推送。
        """
        if self._tz is not None:
            return datetime.now(self._tz)
        return datetime.now()

    def _tz_label(self) -> str:
        if self._tz is not None:
            return self.timezone
        return "服务器本地时区（未配置 timezone）"

    # ---------------- 生命周期 ----------------

    async def initialize(self):
        """插件激活时启动订阅推送后台任务。"""
        if self._push_task is None:
            self._push_task = asyncio.create_task(self._push_loop())
            logger.info("天气插件：订阅推送任务已启动（默认推送时间 %s）", self.push_time_default)

    async def terminate(self):
        if self._push_task is not None:
            self._push_task.cancel()
            self._push_task = None

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

    # ---------------- 持久化存储 ----------------

    async def _store_get(self, key: str, default: Any = None) -> Any:
        if self._kv_ok:
            try:
                return await self.get_kv_data(key, default)
            except Exception as e:
                logger.warning("天气插件 KV 读取失败，回退本地文件：%s", e)
        return self._json_store().get(key, default)

    async def _store_put(self, key: str, value: Any) -> None:
        if self._kv_ok:
            try:
                await self.put_kv_data(key, value)
                return
            except Exception as e:
                logger.warning("天气插件 KV 写入失败，回退本地文件：%s", e)
        data = self._json_store()
        data[key] = value
        try:
            os.makedirs(os.path.dirname(self._store_path), exist_ok=True)
            with open(self._store_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("天气插件本地存储写入失败：%s", e)

    def _json_store(self) -> Dict[str, Any]:
        try:
            if os.path.isfile(self._store_path):
                with open(self._store_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    # ---------------- 命令 ----------------

    @filter.command("天气", alias={"weather", "Weather", "WEATHER", "tq"})
    async def weather(self, event: AstrMessageEvent, city: GreedyStr):
        """查询实时天气：温度、湿度、风力、空气质量、今日趋势与气象预警。用法：/天气 北京"""
        city = self._clean_city(city)
        located = await self._city_or_none(event, city)
        if located is None:
            yield event.plain_result(self._no_city_hint())
            return
        try:
            data = await self._get_realtime(located, event)
        except Exception as e:
            logger.exception("查询天气失败: %s", located)
            yield event.plain_result(f"天气查询失败：{e}")
            return
        url = await self._render_card(event, "realtime", data)
        if url:
            yield event.image_result(url)
        else:
            yield event.plain_result(self._format_realtime_text(data))

    @filter.command("预报", alias={"天气预报", "forecast", "Forecast", "7天"})
    async def forecast_cmd(self, event: AstrMessageEvent, city: GreedyStr):
        """查询未来 7 天天气预报。用法：/预报 北京"""
        city = self._clean_city(city)
        located = await self._city_or_none(event, city)
        if located is None:
            yield event.plain_result(self._no_city_hint(cmd="/预报"))
            return
        try:
            data = await self._get_forecast(located, 7, event)
        except Exception as e:
            logger.exception("查询预报失败: %s", located)
            yield event.plain_result(f"预报查询失败：{e}")
            return
        url = await self._render_card(event, "forecast", data)
        if url:
            yield event.image_result(url)
        else:
            yield event.plain_result(self._format_forecast_text(data))

    @filter.command("空气质量", alias={"aqi", "AQI", "air", "空气"})
    async def air_cmd(self, event: AstrMessageEvent, city: GreedyStr):
        """查询空气质量：AQI、等级、首要污染物与污染物浓度。用法：/空气质量 北京"""
        city = self._clean_city(city)
        located = await self._city_or_none(event, city)
        if located is None:
            yield event.plain_result(self._no_city_hint(cmd="/空气质量"))
            return
        try:
            data = await self._get_air(located, event)
        except Exception as e:
            logger.exception("查询空气质量失败: %s", located)
            yield event.plain_result(f"空气质量查询失败：{e}")
            return
        yield event.plain_result(self._format_air_text(data))

    @filter.command("降雨", alias={"小时降雨", "rain", "降水"})
    async def rain_cmd(self, event: AstrMessageEvent, city: GreedyStr):
        """查询未来 24 小时逐小时降水趋势。用法：/降雨 北京"""
        city = self._clean_city(city)
        located = await self._city_or_none(event, city)
        if located is None:
            yield event.plain_result(self._no_city_hint(cmd="/降雨"))
            return
        try:
            data = await self._get_rain(located, event)
        except Exception as e:
            logger.exception("查询小时降雨失败: %s", located)
            yield event.plain_result(f"小时降雨查询失败：{e}")
            return
        yield event.plain_result(self._format_rain_text(data))

    @filter.command("对比", alias={"天气对比", "compare", "duibi"})
    async def compare_cmd(self, event: AstrMessageEvent, cities: GreedyStr):
        """多城市天气对比（2-5 个）。用法：/对比 北京 上海 广州"""
        tokens = _city_tokens(str(cities or ""))
        if len(tokens) < 2:
            yield event.plain_result(
                "请提供 2-5 个城市名进行对比，例如：\n/对比 北京 上海 广州"
            )
            return
        tokens = tokens[:MAX_COMPARE_CITIES]

        async def fetch_one(name: str) -> Dict[str, Any] | None:
            try:
                return await self._get_realtime(name)
            except Exception as e:
                logger.warning("对比查询失败 %s: %s", name, e)
                return None

        # Bug 修复：原实现逐个 await，5 个城市最坏要等 5 × 2 个数据源 × 超时 ≈ 100 秒
        # 才回复（用户会以为机器人挂了）。改为并发，总耗时收敛到单个城市的水位。
        results = await asyncio.gather(*(fetch_one(c) for c in tokens))
        rows: List[Tuple[str, Dict[str, Any] | None]] = list(zip(tokens, results))
        yield event.plain_result(self._format_compare_text(tokens, rows))

    @filter.command("绑定", alias={"绑定城市", "bind", "setcity"})
    async def bind_cmd(self, event: AstrMessageEvent, city: GreedyStr):
        """绑定个人默认城市（重名城市会列出候选供选择）。用法：/绑定 北京"""
        city = self._clean_city(city)
        if not city:
            current = (await self._store_get("user_city", {})).get(
                event.get_sender_id(), {}
            )
            if current:
                yield event.plain_result(
                    f"你当前绑定的城市：{current.get('display') or current.get('name')}\n"
                    "如需更换：/绑定 新城市名"
                )
            else:
                yield event.plain_result(
                    "请提供城市名，例如：/绑定 北京\n"
                    "绑定后发送 /天气（不带城市）即可直接查询。"
                )
            return
        try:
            located = await self._locate(city, event)
        except Exception as e:
            logger.exception("绑定城市定位失败: %s", city)
            yield event.plain_result(f"城市定位失败：{e}")
            return
        if located is None:
            yield event.plain_result(f"未找到城市“{city}”，请检查城市名后重试。")
            return
        async with self._store_lock:
            users = await self._store_get("user_city", {})
            users[event.get_sender_id()] = located
            await self._store_put("user_city", users)
        yield event.plain_result(
            f"✅ 已绑定默认城市：{located['display']}\n之后发送 /天气 即可直接查询；"
            "/解绑 可取消绑定。"
        )

    @filter.command("解绑", alias={"解绑城市", "unbind", "unsetcity"})
    async def unbind_cmd(self, event: AstrMessageEvent):
        """解除个人默认城市绑定。用法：/解绑"""
        uid = event.get_sender_id()
        async with self._store_lock:
            users = await self._store_get("user_city", {})
            if uid not in users:
                removed = None
            else:
                removed = users.pop(uid)
                await self._store_put("user_city", users)
        if removed is None:
            yield event.plain_result("你尚未绑定默认城市。可用 /绑定 北京 进行绑定。")
            return
        yield event.plain_result(f"✅ 已解绑城市：{removed.get('display') or removed.get('name')}")

    @filter.command("订阅", alias={"天气订阅", "subscribe", "push"})
    async def subscribe_cmd(self, event: AstrMessageEvent, args: GreedyStr):
        """订阅每日天气定时推送。用法：/订阅 [城市] [时间]，如 /订阅 北京 07:30"""
        # 解析城市 / 时间 / 是否仅工作日（见 _parse_subscribe_args 的 bug 修复说明）
        city, parsed_time, weekdays_only = _parse_subscribe_args(args)
        push_time = parsed_time
        if not _city_tokens(str(args or "")):
            subs = [
                s for s in await self._store_get("push_subs", [])
                if s.get("umo") == event.unified_msg_origin
            ]
            lines = ["用法：/订阅 [城市] [时间]，如 /订阅 北京 07:30（时间可省略）"]
            if subs:
                lines.append("当前会话的订阅：")
                for s in subs:
                    when = "工作日" if s.get("weekdays") else "每日"
                    lines.append(f"· {s.get('display')} {when} {s.get('time')}")
            else:
                lines.append("当前会话暂无订阅。")
            yield event.plain_result("\n".join(lines))
            return

        located = await self._city_or_none(event, city)
        if located is None:
            yield event.plain_result(self._no_city_hint(cmd="/订阅"))
            return
        if isinstance(located, str):
            # 显式城市/默认城市是字符串：先定位成结构化结果，订阅展示与去重才可靠
            try:
                located = await self._locate(located, event)
            except Exception as e:
                logger.exception("订阅城市定位失败: %s", city)
                yield event.plain_result(f"城市定位失败：{e}")
                return
            if located is None:
                yield event.plain_result(f"未找到城市“{city}”，请检查城市名后重试。")
                return
        push_time = push_time or self._parse_push_time(self.push_time_default) or "07:30"

        display = located.get("display") or located.get("name")
        when = f"每{'工作日' if weekdays_only else '日'} {push_time}"

        # 存储是「读-改-写」，加锁避免与其它绑定/订阅/推送写入并发覆盖而丢数据
        updated = False
        async with self._store_lock:
            subs = await self._store_get("push_subs", [])

            def _sub_city_name(s: Dict[str, Any]) -> str:
                c = s.get("city")
                return str(c.get("name") or "") if isinstance(c, dict) else str(c or "")

            for s in subs:
                if s.get("umo") == event.unified_msg_origin and _sub_city_name(
                    s
                ) == str(located.get("name") or ""):
                    s["time"] = push_time
                    s["last_sent"] = ""
                    s["city"] = located
                    s["weekdays"] = weekdays_only
                    updated = True
                    break
            else:
                subs.append(
                    {
                        "umo": event.unified_msg_origin,
                        "sender": event.get_sender_id(),
                        "display": display,
                        "city": located,
                        "time": push_time,
                        "weekdays": weekdays_only,
                        "last_sent": "",
                    }
                )
            await self._store_put("push_subs", subs)

        if updated:
            yield event.plain_result(f"✅ 已更新订阅：{display} {when}")
            return
        yield event.plain_result(
            f"✅ 订阅成功：{when} 推送 {display} 的天气。\n"
            "（部分平台对主动消息有限制，若收不到请检查平台主动消息配置；/退订 可取消）"
        )

    @filter.command("退订", alias={"退订天气", "unsubscribe", "unpush"})
    async def unsubscribe_cmd(self, event: AstrMessageEvent):
        """取消当前会话的天气定时推送。用法：/退订"""
        async with self._store_lock:
            subs = await self._store_get("push_subs", [])
            remaining = [s for s in subs if s.get("umo") != event.unified_msg_origin]
            removed = len(subs) - len(remaining)
            if removed:
                await self._store_put("push_subs", remaining)
        if removed == 0:
            yield event.plain_result("当前会话没有天气订阅。可用 /订阅 北京 07:30 添加。")
            return
        yield event.plain_result(f"✅ 已取消 {removed} 条天气订阅。")

    @filter.command("预警", alias={"气象预警", "alarm", "alerts", "warning"})
    async def alarm_cmd(self, event: AstrMessageEvent, city: GreedyStr):
        """查看当前生效的气象预警（含完整预警正文）。用法：/预警 北京"""
        city = self._clean_city(city)
        located = await self._city_or_none(event, city)
        if located is None:
            yield event.plain_result(self._no_city_hint(cmd="/预警"))
            return
        try:
            data = await self._get_realtime(located, event)
        except Exception as e:
            logger.exception("查询气象预警失败: %s", located)
            yield event.plain_result(f"预警查询失败：{e}")
            return
        yield event.plain_result(self._format_alarm_text(data))

    @filter.command("设置", alias={"状态", "status", "info", "自检"})
    async def status_cmd(self, event: AstrMessageEvent):
        """查看插件运行状态：生效数据源、绑定城市、订阅列表、缓存与时区。用法：/设置"""
        users = await self._store_get("user_city", {})
        bound = users.get(event.get_sender_id())
        subs = [
            s for s in await self._store_get("push_subs", [])
            if s.get("umo") == event.unified_msg_origin
        ]
        lines = ["⚙️ 天气插件状态"]
        lines.append(f"版本：{PLUGIN_VERSION}")
        if self._qw_ready:
            lines.append(f"数据源：和风天气 v1（Host: {self.qweather_host}）")
            lines.append("　失败时自动回退中国天气网 → Open-Meteo")
        elif self.qweather_key:
            lines.append("数据源：中国天气网（零 Key）→ Open-Meteo 兜底")
            lines.append("　⚠️ 已填和风 Key 但缺少专属 Host，和风数据源未启用")
        else:
            lines.append("数据源：中国天气网（零 Key）→ Open-Meteo 兜底")
        lines.append(f"默认城市：{self.default_city or '未设置'}")
        lines.append(
            f"我的绑定城市：{bound.get('display') or bound.get('name') if isinstance(bound, dict) else '未绑定'}"
        )
        if subs:
            lines.append(f"本会话订阅：{len(subs)} 条")
            for s in subs[:5]:
                when = "工作日" if s.get("weekdays") else "每日"
                lines.append(f"　· {s.get('display')} {when} {s.get('time')}")
        else:
            lines.append("本会话订阅：无")
        lines.append(f"图片卡片：{'开启' if self.image_card else '关闭'}")
        lines.append(f"重名交互选择：{'开启' if self.interactive_choice else '关闭'}")
        lines.append(f"推送默认时间：{self.push_time_default}")
        lines.append(f"时区：{self._tz_label()}")
        lines.append(f"当前时间：{self._now().strftime('%Y-%m-%d %H:%M')}")
        lines.append(
            f"缓存：结果 {len(self._cache)} 条（上限 {CACHE_MAX_ENTRIES} 条，"
            f"有效期 {int(CACHE_TTL // 60)} 分钟）· 定位 {len(self._loc_cache)} 条"
        )
        yield event.plain_result("\n".join(lines))

    # ---------------- LLM 工具 ----------------

    @filter.llm_tool(name="get_weather")
    async def get_weather(self, event: AstrMessageEvent, city: str = ""):
        """查询指定城市或地区的当前天气（温度、湿度、风力、空气质量）以及未来三天天气趋势，如有气象预警会一并返回。

        Args:
            city(string): 要查询天气的城市或地区名称，例如：北京、上海、杭州、广州。不填则查询默认城市
        """
        city = self._clean_city(city)
        located = await self._city_or_none(event, city)
        if located is None:
            yield event.plain_result("请提供要查询天气的城市名称。")
            return
        try:
            data = await self._get_realtime(located, event)
        except Exception as e:
            logger.exception("LLM 工具查询天气失败: %s", located)
            yield event.plain_result(f"天气查询失败：{e}")
            return
        yield event.plain_result(self._format_realtime_text(data))

    @filter.llm_tool(name="get_weather_forecast")
    async def get_weather_forecast(self, event: AstrMessageEvent, city: str = "", days: float = 7):
        """查询指定城市未来数天的天气预报（每天天气现象、最低/最高气温、降水概率）。

        Args:
            city(string): 要查询预报的城市或地区名称，例如：北京、上海。不填则查询默认城市
            days(number): 预报天数，3-10 天，默认 7
        """
        city = self._clean_city(city)
        located = await self._city_or_none(event, city)
        if located is None:
            yield event.plain_result("请提供要查询天气预报的城市名称。")
            return
        try:
            n = max(3, min(10, int(float(days or 7))))
            data = await self._get_forecast(located, n, event)
        except Exception as e:
            logger.exception("LLM 工具查询预报失败: %s", located)
            yield event.plain_result(f"天气预报查询失败：{e}")
            return
        yield event.plain_result(self._format_forecast_text(data))

    @filter.llm_tool(name="get_air_quality")
    async def get_air_quality(self, event: AstrMessageEvent, city: str = ""):
        """查询指定城市当前的空气质量：AQI 数值、等级（优/良/轻度污染等）、首要污染物和各污染物浓度。

        Args:
            city(string): 要查询空气质量的的城市或地区名称，例如：北京、石家庄。不填则查询默认城市
        """
        city = self._clean_city(city)
        located = await self._city_or_none(event, city)
        if located is None:
            yield event.plain_result("请提供要查询空气质量的城市名称。")
            return
        try:
            data = await self._get_air(located, event)
        except Exception as e:
            logger.exception("LLM 工具查询空气质量失败: %s", located)
            yield event.plain_result(f"空气质量查询失败：{e}")
            return
        yield event.plain_result(self._format_air_text(data))

    # ---------------- 订阅推送 ----------------

    async def _push_loop(self):
        await asyncio.sleep(15)  # 等平台适配器就绪
        while True:
            await asyncio.sleep(PUSH_LOOP_INTERVAL)
            try:
                await self._push_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("天气插件：订阅推送检查失败")

    async def _push_tick(self):
        # 用 _now()（按配置时区）而非服务器本地时间：UTC 服务器上原实现会让
        # /订阅 07:30 实际在北京时间 15:30 才推送。
        now = self._now()
        hhmm = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")
        is_weekend = now.weekday() >= 5  # 5=周六 6=周日

        # 先在锁内挑出到点的订阅并标记，避免与用户 /订阅 /退订 并发写入互相覆盖；
        # 真正的查询与发送放到锁外，不长时间占用存储锁。
        due: List[Dict[str, Any]] = []
        async with self._store_lock:
            subs = await self._store_get("push_subs", [])
            if not subs:
                return
            for sub in subs:
                if sub.get("time") != hhmm or sub.get("last_sent") == today:
                    continue
                if sub.get("weekdays") and is_weekend:
                    continue  # 「仅工作日」订阅在周末跳过
                sub["last_sent"] = today
                due.append(dict(sub))
            if due:
                await self._store_put("push_subs", subs)

        for sub in due:
            try:
                data = await self._get_realtime(sub.get("city") or sub.get("display", ""))
                text = f"⏰ 每日天气 · {data.get('city', '')}\n" + self._format_realtime_text(data)
            except Exception as e:
                logger.warning("天气插件：订阅推送查询失败 %s: %s", sub.get("display"), e)
                text = f"⏰ 每日天气 · {sub.get('display', '')}\n天气查询失败：{e}"
            try:
                if self.context is not None:
                    await self.context.send_message(
                        sub.get("umo", ""), MessageChain().message(text)
                    )
            except Exception as e:
                logger.warning("天气插件：订阅推送发送失败 %s: %s", sub.get("umo"), e)

    @staticmethod
    def _parse_push_time(text: str) -> str | None:
        m = re.fullmatch(r"(\d{1,2})[:：时点](\d{1,2})分?", (text or "").strip())
        if m and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
            return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
        return None

    # ---------------- 城市解析 ----------------

    @staticmethod
    def _clean_city(city: Any) -> str:
        return str(city or "").strip().strip("'\"“”‘’")

    async def _city_or_none(self, event: AstrMessageEvent, city: str) -> CityInput | None:
        """显式城市 > 用户绑定 > 全局默认。返回 None 表示没有任何可用城市。"""
        if city:
            return city
        users = await self._store_get("user_city", {})
        bound = users.get(event.get_sender_id())
        if bound and isinstance(bound, dict) and bound.get("name"):
            return bound
        if self.default_city:
            return self.default_city
        return None

    def _no_city_hint(self, cmd: str = "/天气") -> str:
        lines = [f"请提供城市名，例如：\n{cmd} 北京"]
        if self.default_city:
            lines.append(f"（当前默认城市：{self.default_city}）")
        lines.append("发送 /绑定 城市 可设置个人默认城市，之后无需每次输入。")
        return "\n".join(lines)

    async def _locate(self, city: str, event: AstrMessageEvent | None = None) -> Dict[str, Any] | None:
        """在可用数据源中查找城市，返回定位结果（可能为 None）。

        结果按城市名缓存 LOCATE_TTL，避免每次查询都重打一次城市搜索接口
        （国内搜索接口实测可达数秒，属于固定开销）。
        """
        city = str(city or "").strip()
        ckey = city.lower()
        if ckey:
            hit = self._loc_cache.get(ckey)
            if hit and time.monotonic() - hit[0] < LOCATE_TTL:
                logger.debug("城市定位命中缓存：%s", city)
                return dict(hit[1])

        errors: List[str] = []
        found: Dict[str, Any] | None = None

        if self._qw_ready:
            try:
                cands = await self._qw_lookup(city)
                if cands:
                    found = await self._pick_candidate(city, cands, event)
                else:
                    errors.append("和风天气未找到")
            except Exception as e:
                errors.append(f"和风天气: {e}")

        if found is None:
            try:
                cands = await self._cn_lookup(city)
                if cands:
                    found = await self._pick_candidate(city, cands, event)
                else:
                    errors.append("中国天气网未找到")
            except Exception as e:
                errors.append(f"中国天气网: {e}")

        if found is None:
            try:
                cands = await self._om_lookup(city)
                if cands:
                    found = await self._pick_candidate(city, cands, event)
                else:
                    errors.append("Open-Meteo 未找到")
            except Exception as e:
                errors.append(f"Open-Meteo: {e}")

        if found is not None:
            if ckey:
                if len(self._loc_cache) >= CACHE_MAX_ENTRIES:
                    now = time.monotonic()
                    self._loc_cache = {
                        k: v for k, v in self._loc_cache.items()
                        if now - v[0] < LOCATE_TTL
                    }
                self._loc_cache[ckey] = (time.monotonic(), dict(found))
            return found

        if errors:
            logger.debug("城市定位失败 %s: %s", city, "; ".join(errors))
        return None

    async def _pick_candidate(
        self,
        city: str,
        cands: List[Dict[str, Any]],
        event: AstrMessageEvent | None,
        state: Dict[str, bool] | None = None,
    ) -> Dict[str, Any]:
        """从候选中选择：精确名称命中 > 交互选择 > 第一个。

        state 在各数据源之间共享：某一源已经问过用户后置 asked=True，
        后续源直接取候选，不再重复弹选择菜单（否则用户要等两轮 45 秒超时）。
        """
        low = city.strip().lower()
        # Bug 修复：原实现“找到第一个同名候选就返回”，而重名城市的候选
        # name 全都相同（如两个“朝阳”），于是永远直接取第一个，
        # 交互选择形同虚设。现在只有同名候选唯一时才直接命中。
        exact = [c for c in cands if str(c.get("name", "")).strip().lower() == low]
        if len(exact) == 1:
            return exact[0]

        # 拼音/英文输入（如 /天气 beijing）时候选名是中文，精确匹配必然为空。
        # 若首个候选的 display 与 name 相同（说明它本身就是该城市、不是下辖区），
        # 直接采用，否则会被误判成“重名城市”而弹出选择菜单。
        if not exact and cands:
            first = cands[0]
            if str(first.get("display") or "") == str(first.get("name") or ""):
                return first

        pool = exact or cands

        already_asked = bool((state or {}).get("asked"))
        if (
            len(pool) > 1
            and event is not None
            and not already_asked
            and self.interactive_choice
            and session_waiter is not None
        ):
            if state is not None:
                state["asked"] = True
            picked = await self._disambiguate(event, city, pool)
            if picked is not None:
                return picked
        return pool[0]

    async def _disambiguate(
        self, event: AstrMessageEvent, city: str, cands: List[Dict[str, Any]]
    ) -> Dict[str, Any] | None:
        """重名城市：列出候选，等待用户回复序号。"""
        lines = [f"找到多个名为“{city}”的城市："]
        for i, c in enumerate(cands[:8], 1):
            lines.append(f"{i}. {c['display']}")
        lines.append("请回复序号选择（回复 0 或“取消”放弃）")
        await event.send(event.plain_result("\n".join(lines)))

        picked: Dict[str, Any] | None = None

        @session_waiter(timeout=DISAMBIG_TIMEOUT, record_history_chains=False)
        async def waiter(controller, ev: AstrMessageEvent):
            nonlocal picked
            text = (ev.message_str or "").strip()
            if text in ("0", "取消", "q", "Q", "退出"):
                await ev.send(ev.plain_result("已取消本次查询。"))
                controller.stop()
                return
            if text.isdigit() and 1 <= int(text) <= len(cands[:8]):
                picked = cands[int(text) - 1]
                controller.stop()
                return
            await ev.send(ev.plain_result(f"请回复 1-{min(len(cands), 8)} 的序号，或 0 取消。"))
            controller.keep(timeout=DISAMBIG_TIMEOUT, reset_timeout=True)

        try:
            await waiter(event)
        except TimeoutError:
            await event.send(event.plain_result("选择超时，已取消本次查询。"))
            return None
        except Exception as e:
            logger.warning("城市选择交互异常：%s", e)
            return None
        return picked

    # ---------------- 查询编排（带缓存） ----------------

    def _cache_get(self, key: str) -> Dict[str, Any] | None:
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        self._cache.pop(key, None)
        return None

    def _cache_put(self, key: str, data: Dict[str, Any]) -> None:
        # Bug 修复：原实现过滤过期条目后若仍超限，缓存会无限增长。
        # 现在先过滤过期项，仍超限则按最旧时间戳逐出。
        if key not in self._cache and len(self._cache) >= CACHE_MAX_ENTRIES:
            now = time.monotonic()
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < CACHE_TTL}
            while len(self._cache) >= CACHE_MAX_ENTRIES:
                oldest = min(self._cache.items(), key=lambda kv: kv[1][0])[0]
                self._cache.pop(oldest, None)
        self._cache[key] = (time.monotonic(), data)

    def _cache_key(self, kind: str, located: CityInput, extra: str = "") -> str:
        if isinstance(located, dict):
            name = str(located.get("display") or located.get("name") or "").lower()
        else:
            name = str(located).strip().lower()
        return f"{kind}:{name}:{extra}"

    async def _run_source_chain(
        self,
        kind: str,
        located: CityInput,
        event: AstrMessageEvent | None,
        budget: float,
        days: int = 7,
        allow_interact: bool = True,
    ) -> Dict[str, Any]:
        """按优先级尝试各数据源；每个数据源独立定位城市（除非传入定位结果）。

        days 只对 forecast 生效（原实现把它丢在缓存键里，任何天数都返回 7 天）。
        allow_interact=False 表示本轮已询问过用户，不再重复弹出重名城市选择。
        """
        # 各数据源共享该状态：一旦某个源询问过用户，后面的源就不再重复询问
        state: Dict[str, bool] = {"asked": not allow_interact}

        sources: List[Tuple[str, Callable, float]] = []
        if kind == "rain":
            # 中国天气网无小时级降水接口，降水只走 和风 → Open-Meteo
            if self._qw_ready:
                sources.append(("和风天气", self._qw_pipeline(kind, days, state), 20.0))
            sources.append(("Open-Meteo", self._om_pipeline(kind, days, state), 24.0))
        else:
            if self._qw_ready:
                sources.append(("和风天气", self._qw_pipeline(kind, days, state), 30.0))
            sources.append(("中国天气网", self._cn_pipeline(kind, days, state), 26.0))
            sources.append(("Open-Meteo", self._om_pipeline(kind, days, state), 22.0))

        errors: List[str] = []
        for name, fetch, budget_each in sources:
            # 已定位的结果只发给兼容的数据源（cn 定位带站号，geo 定位带经纬度）
            if isinstance(located, dict):
                src = located.get("source")
                compatible = {
                    "qw": ("和风天气", "Open-Meteo"),
                    "om": ("和风天气", "Open-Meteo"),
                    "cn": ("中国天气网",),
                }.get(src, ())
                if name not in compatible:
                    continue
            try:
                data = await asyncio.wait_for(
                    fetch(located, event), timeout=budget_each
                )
            except Exception as e:
                reason = "超时" if isinstance(e, (asyncio.TimeoutError, TimeoutError)) else str(e)
                errors.append(f"{name}: {reason}")
                logger.debug("天气插件 %s 数据源 %s 失败：%s", kind, name, e)
                continue
            if data:
                return data
            errors.append(f"{name}: 无数据")
        if kind == "rain":
            raise RuntimeError("暂无小时降雨数据。\n" + "\n".join(errors[:3]))
        raise RuntimeError(
            "所有数据源均不可用。\n" + "\n".join(errors[:4]) + (
                "\n提示：国内服务器请优先使用中国天气网（默认零 Key）；"
                "若配置了和风天气，请确认已同时填写专属 API Host。" if len(errors) >= 3 else ""
            )
        )

    def _schedule_refresh(
        self,
        key: str,
        kind: str,
        located: CityInput,
        budget: float,
        days: int = 7,
    ) -> None:
        """软过期：后台重新拉取并更新缓存，不阻塞当前回复、也不弹交互选择。"""
        if key in self._refreshing:
            return
        self._refreshing.add(key)

        async def worker():
            try:
                data = await self._run_source_chain(
                    kind, located, None, budget, days, allow_interact=False
                )
                self._cache_put(key, data)
                logger.debug("天气插件后台刷新完成：%s", key)
            except Exception as e:
                logger.debug("天气插件后台刷新失败 %s：%s", key, e)
            finally:
                self._refreshing.discard(key)

        try:
            asyncio.create_task(worker())
        except RuntimeError:  # 无运行中的事件循环（极少见）
            self._refreshing.discard(key)

    async def _cached_query(
        self,
        cache_kind: str,
        kind: str,
        located: CityInput,
        event: AstrMessageEvent | None,
        budget: float,
        days: int = 7,
    ) -> Dict[str, Any]:
        """带软过期的缓存查询。

        新鲜（<CACHE_TTL）直接返回；处于软过期窗口时先秒回旧数据并在后台刷新，
        避免每次缓存过期后都要重新等数据源（国内城市搜索接口曾实测 ~9 秒）。
        """
        key = self._cache_key(cache_kind, located, str(days) if kind == "forecast" else "")
        hit = self._cache.get(key)
        if hit and hit[1]:
            age = time.monotonic() - hit[0]
            if age < CACHE_TTL:
                return hit[1]
            if age < CACHE_TTL * CACHE_STALE_WINDOW:
                self._schedule_refresh(key, kind, located, budget, days)
                return hit[1]
        self._cache.pop(key, None)
        data = await self._run_source_chain(
            kind, located, event, budget, days, allow_interact=True
        )
        self._cache_put(key, data)
        return data

    async def _get_realtime(
        self, located: CityInput, event: AstrMessageEvent | None = None
    ) -> Dict[str, Any]:
        return await self._cached_query("rt", "realtime", located, event, 20.0)

    async def _get_forecast(
        self, located: CityInput, days: int = 7, event: AstrMessageEvent | None = None
    ) -> Dict[str, Any]:
        # Bug 修复：days 之前只进了缓存键，从没传到数据源，导致任何天数都返回 7 天
        return await self._cached_query("fc", "forecast", located, event, 20.0, days)

    async def _get_air(
        self, located: CityInput, event: AstrMessageEvent | None = None
    ) -> Dict[str, Any]:
        return await self._cached_query("air", "air", located, event, 18.0)

    async def _get_rain(
        self, located: CityInput, event: AstrMessageEvent | None = None
    ) -> Dict[str, Any]:
        return await self._cached_query("rain", "rain", located, event, 24.0)

    # ---------------- 文本格式化 ----------------

    def _format_realtime_text(self, data: Dict[str, Any]) -> str:
        lines = [f"{data.get('emoji', '🌡️')} {data.get('place') or data.get('city', '')} · 当前天气"]
        if data.get("cond"):
            lines.append(f"天气：{data['cond']}")
        temp = data.get("temp")
        feels = data.get("feels")
        if temp:
            line = f"温度：{temp}°C"
            if feels:
                line += f"（体感 {feels}°C）"
            lines.append(line)
        if data.get("humidity"):
            hum = str(data["humidity"])
            lines.append(f"湿度：{hum}{'' if '%' in hum else '%'}")
        if data.get("wind"):
            lines.append(f"风向：{data['wind']}")
        if data.get("pressure"):
            lines.append(f"气压：{data['pressure']} hPa")
        aqi = data.get("aqi")
        if aqi and aqi.get("value"):
            lines.append(
                f"空气质量：{aqi.get('category') or _aqi_category(aqi['value'])}（AQI {aqi['value']}）"
            )
        today = data.get("today")
        if today:
            line = f"今日：{today.get('emoji', '')} {today.get('text', '')}，{today.get('low', '?')} ~ {today.get('high', '?')}℃"
            if today.get("sun"):
                line += f"（{today['sun']}）"
            lines.append(line)
        yd_text = _yesterday_text(data)
        if yd_text:
            lines.append(f"较昨日：{yd_text}")
        for d in data.get("days") or []:
            lines.append(
                f"{d.get('label', '')}：{d.get('emoji', '')} {d.get('text', '')}，"
                f"{d.get('low', '?')} ~ {d.get('high', '?')}℃"
            )
        if data.get("tips"):
            lines.append(f"生活指数：{data['tips']}")
        lines.append(f"出行建议：{_travel_advice(data)}")
        for a in data.get("alarms") or []:
            lines.append(f"{a.get('mark', '⚠️')} 预警：{a.get('title', '')}")
            body = str(a.get("body") or "").strip()
            if body:
                lines.append(f"　{body[:110]}{'…' if len(body) > 110 else ''}")
        lines.append(f"数据来源：{data.get('source', '')}")
        return "\n".join(lines)

    def _format_forecast_text(self, data: Dict[str, Any]) -> str:
        lines = [f"📅 {data.get('place') or data.get('city', '')} · 天气预报"]
        for d in data.get("days") or []:
            label = d.get("label", "")
            date = d.get("date", "")
            weekday = d.get("weekday", "")
            head = label
            if date or weekday:
                head += f"（{' '.join(p for p in (date, weekday) if p)}）"
            line = f"{head}：{d.get('emoji', '')} {d.get('text', '')}，{d.get('low', '?')} ~ {d.get('high', '?')}℃"
            if d.get("pop"):
                line += f" 降水{d['pop']}"
            lines.append(line)
        lines.append(f"数据来源：{data.get('source', '')}")
        return "\n".join(lines)

    def _format_alarm_text(self, data: Dict[str, Any]) -> str:
        place = data.get("place") or data.get("city", "")
        alarms = data.get("alarms") or []
        if not alarms:
            return (
                f"✅ {place} 当前无生效气象预警。\n"
                f"数据来源：{data.get('source', '')}"
            )
        lines = [f"⚠️ {place} · 气象预警（{len(alarms)} 条）"]
        for a in alarms:
            lines.append(f"{a.get('mark', '⚠️')} {a.get('title', '')}")
            meta = []
            if a.get("sender"):
                meta.append(f"发布：{a['sender']}")
            if a.get("expire"):
                meta.append(f"失效：{a['expire']}")
            if meta:
                lines.append("　" + " · ".join(meta))
            body = str(a.get("body") or "").strip()
            if body:
                lines.append(f"　{body}")
            ins = str(a.get("instruction") or "").strip()
            if ins:
                lines.append("　防御指南：")
                for row in [x.strip() for x in ins.splitlines() if x.strip()][:6]:
                    lines.append(f"　　{row}")
        lines.append(f"数据来源：{data.get('source', '')}")
        return "\n".join(lines)

    def _format_air_text(self, data: Dict[str, Any]) -> str:
        lines = [f"🍃 {data.get('place') or data.get('city', '')} · 空气质量"]
        lines.append(f"AQI：{data.get('value', '?')}（{data.get('category', '未知')}）")
        if data.get("primary"):
            lines.append(f"首要污染物：{data['primary']}")
        if data.get("pollutants"):
            lines.append(f"污染物：{data['pollutants']}")
        if data.get("advice"):
            lines.append(f"健康建议：{data['advice']}")
        if data.get("standard"):
            lines.append(f"评价标准：{data['standard']}")
        lines.append(f"数据来源：{data.get('source', '')}")
        return "\n".join(lines)

    def _format_rain_text(self, data: Dict[str, Any]) -> str:
        lines = [f"☔ {data.get('place') or data.get('city', '')} · 未来24小时降水"]
        if data.get("summary"):
            lines.append(data["summary"])
        for e in data.get("entries") or []:
            mm = f" {e['mm']}mm" if e.get("mm") else ""
            lines.append(f"{e.get('time', '')} {e.get('text', '')} 概率{e.get('pop', '?')}{mm}")
        lines.append(f"数据来源：{data.get('source', '')}")
        return "\n".join(lines)

    def _format_compare_text(
        self, tokens: List[str], rows: List[Tuple[str, Dict[str, Any] | None]]
    ) -> str:
        name_w = max(_disp_width(t) for t in tokens) + 2
        lines = ["📊 天气对比"]
        lines.append("─" * 28)
        temps: List[Tuple[str, float]] = []
        aqis: List[Tuple[str, int]] = []
        for name, data in rows:
            if not data:
                lines.append(f"{_pad(name, name_w)}❌ 查询失败")
                continue
            aqi = data.get("aqi") or {}
            aqi_txt = f"{aqi.get('category', '—')}({aqi.get('value', '—')})" if aqi.get("value") else "—"
            lines.append(
                f"{_pad(name, name_w)}{data.get('emoji', '')} {_pad(data.get('cond', '—'), 12)}"
                f"{_pad(str(data.get('temp', '—')) + '°C', 8)}{aqi_txt}"
            )
            try:
                temps.append((name, float(data.get("temp"))))
            except (TypeError, ValueError):
                pass
            try:
                if aqi.get("value"):
                    aqis.append((name, int(float(aqi["value"]))))
            except (TypeError, ValueError):
                pass
        lines.append("─" * 28)
        if temps:
            hot = max(temps, key=lambda kv: kv[1])
            cold = min(temps, key=lambda kv: kv[1])
            lines.append(f"最热：{hot[0]} {hot[1]:g}°C · 最凉：{cold[0]} {cold[1]:g}°C")
        if aqis:
            best = min(aqis, key=lambda kv: kv[1])
            lines.append(f"空气最好：{best[0]}（AQI {best[1]}）")
        lines.append("数据来源：各城市实际命中的数据源")
        return "\n".join(lines)

    # ---------------- 图片卡片 ----------------

    async def _render_card(
        self, event: AstrMessageEvent, kind: str, data: Dict[str, Any]
    ) -> Optional[str]:
        """渲染图片卡片，失败（或未开启）返回 None 以回退纯文本。"""
        if not self.image_card:
            return None
        try:
            if kind == "realtime":
                payload = self._rt_card_payload(data)
                tmpl = REALTIME_CARD_TMPL
            elif kind == "forecast":
                payload = self._fc_card_payload(data)
                tmpl = FORECAST_CARD_TMPL
            else:
                return None
            return await self.html_render(tmpl, payload)
        except Exception as e:
            logger.warning("天气卡片渲染失败，回退纯文本：%s", e)
            return None

    def _rt_card_payload(self, data: Dict[str, Any]) -> Dict[str, Any]:
        metrics = []
        if data.get("humidity"):
            metrics.append({"label": "湿度", "value": str(data["humidity"]) + ("%" if "%" not in str(data["humidity"]) else "")})
        if data.get("wind"):
            metrics.append({"label": "风向", "value": str(data["wind"])})
        if data.get("pressure"):
            metrics.append({"label": "气压", "value": f"{data['pressure']} hPa"})
        if data.get("uv"):
            metrics.append({"label": "紫外线", "value": f"{data['uv']} 级"})
        if data.get("vis"):
            metrics.append({"label": "能见度", "value": f"{data['vis']} km"})
        if data.get("precip"):
            metrics.append({"label": "降水(1h)", "value": str(data["precip"])})
        yd_text = _yesterday_text(data)
        if yd_text and (data.get("yesterday") or {}).get("diff") is not None:
            d = float((data.get("yesterday") or {}).get("diff") or 0)
            metrics.append({
                "label": "较昨日最高温",
                "value": f"{'+' if d > 0 else ''}{d:g}℃",
            })
        aqi = data.get("aqi")
        aqi_payload = None
        if aqi and aqi.get("value"):
            aqi_payload = {
                "value": aqi.get("value"),
                "category": aqi.get("category") or _aqi_category(aqi.get("value")),
                "color": _aqi_color(aqi.get("value"), aqi.get("category") or ""),
            }
        return {
            "city": data.get("city", ""),
            "place": data.get("place", ""),
            "update_time": data.get("update_time") or self._now().strftime("%m-%d %H:%M"),
            "emoji": data.get("emoji", "🌡️"),
            "temp": data.get("temp", "--"),
            "feels": data.get("feels", ""),
            "cond": data.get("cond", ""),
            "metrics": metrics,
            "aqi": aqi_payload,
            "today": data.get("today"),
            "days": data.get("days") or [],
            "alarms": data.get("alarms") or [],
            "source": data.get("source", ""),
            "tips": data.get("tips", ""),
            "advice": _travel_advice(data),
        }

    def _fc_card_payload(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "city": data.get("city", ""),
            "place": data.get("place", ""),
            "days": data.get("days") or [],
            "source": data.get("source", ""),
        }

    # ---------------- 数据源 1：和风天气（v1 新接口，需专属 Host） ----------------

    def _qw_pipeline(
        self, kind: str, days: int = 7, state: Dict[str, bool] | None = None
    ) -> Callable:
        async def run(located: CityInput, event: AstrMessageEvent | None) -> Dict[str, Any]:
            if isinstance(located, dict):
                if "lat" not in located or "lon" not in located:
                    raise RuntimeError("该城市定位不含经纬度，无法使用和风数据源")
                loc = located
            else:
                cands = await self._qw_lookup(located)
                if not cands:
                    raise RuntimeError(f"和风天气未找到城市“{located}”")
                loc = await self._pick_candidate(located, cands, event, state)
            if kind == "realtime":
                return await self._qw_realtime(loc)
            if kind == "forecast":
                return await self._qw_forecast(loc, days)
            if kind == "air":
                return await self._qw_air(loc)
            if kind == "rain":
                return await self._qw_rain(loc)
            raise RuntimeError(f"未知查询类型 {kind}")

        return run

    async def _qw_get(self, path: str, params: Dict[str, Any] | None = None, timeout: float = 6.0):
        """和风 v1 请求：X-QW-Api-Key 请求头认证（新接口不接受 key 查询参数）。"""
        url = f"https://{self.qweather_host}{path}"
        raw = await _aget_bytes(
            url, params, timeout, headers={"X-QW-Api-Key": self.qweather_key}
        )
        data = json.loads(_decode(raw))
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"和风天气错误：{data.get('error')}")
        return data

    async def _qw_lookup(self, city: str, number: int = 6) -> List[Dict[str, Any]]:
        geo = await self._qw_get(
            "/geo/v2/city/lookup",
            {"location": city, "number": number, "lang": "zh"},
        )
        if str(geo.get("code")) != "200":
            raise RuntimeError(f"和风城市查找失败（code={geo.get('code')}）")
        out: List[Dict[str, Any]] = []
        for loc in geo.get("location") or []:
            try:
                name = loc.get("name") or city
                adm1 = loc.get("adm1") or ""
                adm2 = loc.get("adm2") or ""
                country = loc.get("country") or ""
                parts = [name]
                if adm2 and adm2 != name:
                    parts.append(adm2)
                if adm1 and adm1 not in (name, adm2):
                    parts.append(adm1)
                if country and country != "中国":
                    parts.append(country)
                place_parts = [p for p in (adm1, country) if p and p != name]
                out.append(
                    {
                        "source": "qw",
                        "name": name,
                        "display": " · ".join(parts),
                        "place": f"{name}（{' · '.join(place_parts)}）" if place_parts else name,
                        "lat": round(float(loc["lat"]), 2),
                        "lon": round(float(loc["lon"]), 2),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def _qw_realtime(self, loc: Dict[str, Any]) -> Dict[str, Any]:
        lat, lon = loc["lat"], loc["lon"]

        async def safe(coro, default):
            """可选数据（空气/预报/预警）失败不影响主流程。"""
            try:
                return await coro
            except Exception:
                return default

        # 性能优化：原本 current → air → daily → alert 逐个 await，延迟叠加约 4 倍。
        # 这四个请求互不依赖，改为并发后总耗时接近单次请求。
        cur, air, daily, alerts = await asyncio.gather(
            safe(
                self._qw_get(
                    f"/weather/v1/current/{lat}/{lon}",
                    {"lang": "zh", "localTime": "true"},
                ),
                {},
            ),
            safe(self._qw_air_detail(lat, lon), None),
            safe(
                self._qw_get(
                    f"/weather/v1/daily/{lat}/{lon}",
                    {"days": "3", "lang": "zh", "localTime": "true"},
                ),
                {},
            ),
            safe(
                self._qw_get(f"/weatheralert/v1/current/{lat}/{lon}", {"lang": "zh"}),
                {},
            ),
        )
        if not cur:
            raise RuntimeError("和风天气未返回实时天气数据")

        cond = cur.get("condition") or {}
        text = cond.get("text") or "未知"
        wind = cur.get("wind") or {}
        wind_dir = _deg_to_dir((wind.get("direction") or {}).get("degree"))
        wind_scale = wind.get("scale")
        wind_txt = ""
        if wind_scale is not None:
            wind_txt = f"{wind_dir}风 {wind_scale} 级" if wind_dir else f"{wind_scale} 级"
        precip = cur.get("precipitation") or {}

        data: Dict[str, Any] = {
            "city": loc.get("name", ""),
            "place": loc.get("place") or loc.get("name", ""),
            "cond": text,
            "emoji": _qweather_emoji(text),
            "temp": _num((cur.get("temperature") or {}).get("value")),
            "feels": _num((cur.get("feelsLike") or {}).get("value")),
            "humidity": _pct_from_frac(cur.get("humidity")),
            "wind": wind_txt,
            "pressure": _num((cur.get("pressure") or {}).get("value")),
            "vis": _vis_km(cur.get("visibility")),
            "uv": _num(cur.get("uvIndex")),
            "precip": _num((precip.get("amount") or {}).get("value")),
            "update_time": self._now().strftime("%m-%d %H:%M"),
            "aqi": air,
            "today": None,
            "days": [],
            "alarms": [],
            "tips": "",
            "source": "和风天气",
        }

        # 今日 + 后两天
        days = daily.get("days") or []
        if days:
            data["today"] = self._qw_day_summary(days[0])
            for i, day in enumerate(days[1:3], 1):
                item = self._qw_day_summary(day)
                item["label"] = "明天" if i == 1 else "后天"
                data["days"].append(item)

        # 气象预警：过滤「已取消」和「已过失效时间」的条目，
        # 否则 /天气 与 /预警 会把已经取消的预警继续报给用户。
        now = self._now()
        for a in alerts.get("alerts") or []:
            mtype = str((a.get("messageType") or {}).get("code") or "").lower()
            if mtype == "cancel":
                continue
            if _alarm_expired(a.get("expireTime"), now):
                continue
            color = str((a.get("color") or {}).get("code") or "").lower()
            data["alarms"].append(
                {
                    "mark": ALARM_MARKS.get(color, "⚠️"),
                    "title": a.get("headline") or "气象预警",
                    "body": str(a.get("description") or ""),
                    "sender": str(a.get("senderName") or ""),
                    "expire": _alarm_time_text(a.get("expireTime")),
                    "instruction": str(a.get("instruction") or ""),
                }
            )
        return data

    @staticmethod
    def _qw_day_summary(day: Dict[str, Any]) -> Dict[str, Any]:
        daytime = day.get("daytime") or {}
        nighttime = day.get("nighttime") or {}
        dtext = _combine_day_night(
            (daytime.get("condition") or {}).get("text") or "",
            (nighttime.get("condition") or {}).get("text") or "",
        )
        astro = day.get("astro") or {}
        sun = ""
        sunrise, sunset = _hm_of(astro.get("sunrise") or ""), _hm_of(astro.get("sunset") or "")
        if sunrise or sunset:
            sun = f"日出 {sunrise or '?'} · 日落 {sunset or '?'}"
        return {
            "label": "今日",
            "date": _md_of(day.get("forecastStartTime") or ""),
            "weekday": _weekday_of(day.get("forecastStartTime") or ""),
            "text": dtext,
            "emoji": _qweather_emoji(dtext),
            "low": _num((day.get("temperatureMin") or {}).get("value")),
            "high": _num((day.get("temperatureMax") or {}).get("value")),
            "pop": _frac_pct((daytime.get("precipitation") or {}).get("probability")),
            "sun": sun,
        }

    async def _qw_forecast(self, loc: Dict[str, Any], days: int = 7) -> Dict[str, Any]:
        lat, lon = loc["lat"], loc["lon"]
        d = await self._qw_get(
            f"/weather/v1/daily/{lat}/{lon}",
            {"days": str(days), "lang": "zh", "localTime": "true"},
        )
        items = []
        for i, day in enumerate(d.get("days") or []):
            item = self._qw_day_summary(day)
            if i == 0:
                item["label"] = "今日"
            elif i == 1:
                item["label"] = "明天"
            elif i == 2:
                item["label"] = "后天"
            elif not item.get("weekday"):
                item["label"] = f"第{i + 1}天"
            else:
                item["label"] = item["weekday"]
            items.append(item)
        if not items:
            raise RuntimeError("和风天气未返回预报数据")
        return {
            "city": loc.get("name", ""),
            "place": loc.get("place") or loc.get("name", ""),
            "days": items,
            "source": "和风天气",
        }

    async def _qw_air_detail(self, lat: float, lon: float) -> Dict[str, Any] | None:
        air = await self._qw_get(
            f"/airquality/v1/current/{lat}/{lon}", {"lang": "zh"}
        )
        indexes = air.get("indexes") or []
        if not indexes:
            return None
        idx = next(
            (i for i in indexes if i.get("code") == "cn-mee"),
            next(
                (i for i in indexes if i.get("code") == "us-epa"),
                next((i for i in indexes if i.get("code") == "qaqi"), indexes[0]),
            ),
        )
        value = str(idx.get("aqiDisplay") or "")
        category = str(idx.get("category") or "") or _aqi_category(value)
        primary = (idx.get("primaryPollutant") or {}).get("name") or ""
        parts = []
        for p in (air.get("pollutants") or [])[:4]:
            conc = (p.get("concentration") or {})
            v = _num(conc.get("value"))
            if v:
                parts.append(f"{(p.get('name') or p.get('code', '')).replace(' ', '')} {v}{conc.get('unit', '')}")
        advice = ""
        health = (idx.get("health") or {}).get("advice") or {}
        advice = str(health.get("generalPopulation") or health.get("effect") or "")[:60]
        return {
            "value": value,
            "category": category,
            "primary": primary,
            "pollutants": " · ".join(parts),
            "advice": advice,
            "standard": f"{idx.get('name') or 'AQI'}（{idx.get('code', '')}）",
            "source": "和风天气",
        }

    async def _qw_air(self, loc: Dict[str, Any]) -> Dict[str, Any]:
        detail = await self._qw_air_detail(loc["lat"], loc["lon"])
        if not detail:
            raise RuntimeError("和风天气未返回空气质量数据")
        detail["place"] = loc.get("place") or loc.get("name", "")
        return detail

    async def _qw_rain(self, loc: Dict[str, Any]) -> Dict[str, Any]:
        lat, lon = loc["lat"], loc["lon"]
        h = await self._qw_get(
            f"/weather/v1/hourly/{lat}/{lon}",
            {"hours": "24", "lang": "zh", "localTime": "true"},
        )
        hours = h.get("hours") or []
        if not hours:
            raise RuntimeError("和风天气未返回小时预报数据")
        entries = []
        first_hit = None
        for item in hours:
            t = _hm_of(item.get("forecastTime") or "")
            precip = item.get("precipitation") or {}
            prob = precip.get("probability")
            amount = (precip.get("amount") or {}).get("value")
            cond_text = (item.get("condition") or {}).get("text") or ""
            ptype = QW_PRECIP_TYPES.get(precip.get("type"), "")
            meaningful = (prob is not None and prob >= 0.25) or (
                amount is not None and amount >= 0.05
            )
            if meaningful and first_hit is None:
                first_hit = t
            if meaningful:
                entries.append(
                    {
                        "time": f"{t[:2]}时" if t else "?",
                        "text": cond_text or (ptype or "降水"),
                        "pop": _pct(prob * 100 if prob is not None else ""),
                        "mm": _num(amount),
                    }
                )
        if first_hit:
            summary = f"降水将从约 {first_hit} 开始，未来 24 小时共 {len(entries)} 个时段有降水。"
        else:
            summary = "未来 24 小时无明显降水，适合安排出行 ☀️"
        return {
            "city": loc.get("name", ""),
            "place": loc.get("place") or loc.get("name", ""),
            "summary": summary,
            "entries": entries[:10],
            "source": "和风天气",
        }

    # ---------------- 数据源 2：中国天气网（零 Key，国内直连） ----------------

    def _cn_pipeline(
        self, kind: str, days: int = 7, state: Dict[str, bool] | None = None
    ) -> Callable:
        async def run(located: CityInput, event: AstrMessageEvent | None) -> Dict[str, Any]:
            if isinstance(located, dict):
                if located.get("source") != "cn" or not located.get("station_id"):
                    raise RuntimeError("该城市定位不适用于中国天气网数据源")
                loc = located
            else:
                cands = await self._cn_lookup(located)
                if not cands:
                    raise RuntimeError(f"未找到城市“{located}”")
                loc = await self._pick_candidate(located, cands, event, state)
            if kind == "realtime":
                return await self._cn_realtime(loc)
            if kind == "forecast":
                return await self._cn_forecast(loc, days)
            if kind == "air":
                return await self._cn_air(loc)
            raise RuntimeError("中国天气网不支持该查询类型")

        return run

    async def _cn_lookup(self, city: str) -> List[Dict[str, Any]]:
        raw = await _aget_bytes(CN_SEARCH_URL, {"cityname": city}, timeout=10.0, referer=CN_REFERER)
        stations = self._parse_cn_search(_decode(raw))
        if not stations:
            raise RuntimeError(f"未找到城市“{city}”")
        out = []
        for station_id, name, region in stations:
            display = f"{name}（{region}）" if region and region != name else name
            out.append(
                {
                    "source": "cn",
                    "station_id": station_id,
                    "name": name,
                    "display": display,
                    "place": display,
                }
            )
        return out

    async def _cn_index(self, loc: Dict[str, Any]) -> Dict[str, Any]:
        """weather_index 一次返回实况(dataSK)/今日预报(cityDZ)/预警(alarmDZ)/生活指数(dataZS)/多日预报(fc)。"""
        raw = await _aget_bytes(
            CN_INDEX_URL.format(loc["station_id"]), timeout=13.0, referer=CN_REFERER
        )
        text = _decode(raw)
        return {
            "sk": _extract_json_var(text, "dataSK"),
            "dz": (_extract_json_var(text, "cityDZ").get("weatherinfo") or {}),
            "fc": _extract_json_var(text, "fc").get("f") or [],
            "zs": (_extract_json_var(text, "dataZS").get("zs") or {}),
            "alarms": _extract_json_var(text, "alarmDZ").get("w") or [],
        }

    async def _cn_realtime(self, loc: Dict[str, Any]) -> Dict[str, Any]:
        idx = await self._cn_index(loc)
        sk, fc, dz = idx["sk"], idx["fc"], idx["dz"]
        if not sk and not fc:
            # 数据解析失败说明接口格式变化或站点无数据，抛错以触发后续数据源兜底
            raise RuntimeError("中国天气网数据解析失败")
        if not sk:
            # 海外站点：仅有未来预报，无实况
            first = self._cn_day_item(fc[0], "今日") if fc else None
            data: Dict[str, Any] = {
                "city": loc.get("name", ""),
                "place": loc.get("place") or loc.get("name", ""),
                "cond": first.get("text", "—") if first else "—",
                "emoji": _qweather_emoji(first.get("text", "")) if first else "🌡️",
                "temp": "",
                "feels": "",
                "humidity": "",
                "wind": "",
                "pressure": "",
                "vis": "",
                "uv": "",
                "precip": "",
                "update_time": self._now().strftime("%m-%d %H:%M"),
                "aqi": None,
                "today": first,
                "days": [
                    self._cn_day_item(item, str(item.get("fj") or "").strip() or "预报")
                    for item in fc[1:3]
                ],
                "alarms": [],
                "tips": "",
                "source": "中国天气网",
            }
            return data

        weather_now = str(sk.get("weather") or "").strip() or dz.get("weather") or "未知"
        aqi_val = str(sk.get("aqi") or "").strip()
        data = {
            "city": loc.get("name", ""),
            "place": loc.get("place") or loc.get("name", ""),
            "cond": weather_now,
            "emoji": _qweather_emoji(weather_now),
            "temp": str(sk.get("temp") or "").strip(),
            "feels": "",
            "humidity": str(sk.get("SD") or "").strip(),
            "wind": f"{sk.get('WD') or ''} {sk.get('WS') or ''}".strip(),
            "pressure": str(sk.get("qy") or "").strip(),
            "vis": "",
            "uv": "",
            "precip": "",
            "update_time": str(sk.get("time") or self._now().strftime("%m-%d %H:%M")),
            "aqi": None,
            "today": self._cn_day_item(fc[0], "今日") if fc else None,
            "days": [
                self._cn_day_item(item, str(item.get("fj") or "").strip() or "预报")
                for item in fc[1:3]
            ],
            "alarms": [],
            "tips": self._cn_tips(idx["zs"]),
            "source": "中国天气网",
        }
        if aqi_val:
            data["aqi"] = {
                "value": aqi_val,
                "category": _aqi_category(aqi_val),
                "primary": "",
                "pollutants": "",
                "advice": "",
                "standard": "中国（HJ 633-2012）",
                "source": "中国天气网",
            }
        self._append_cn_alarms(data, idx["alarms"])
        return data

    async def _cn_forecast(self, loc: Dict[str, Any], days: int = 7) -> Dict[str, Any]:
        idx = await self._cn_index(loc)
        fc = idx["fc"]
        if not fc:
            raise RuntimeError("中国天气网未返回预报数据")
        items = []
        for i, item in enumerate(fc[:days]):
            if i == 0:
                label = "今日"
            elif i == 1:
                label = "明天"
            elif i == 2:
                label = "后天"
            else:
                label = str(item.get("fj") or "").strip() or f"第{i + 1}天"
            items.append(self._cn_day_item(item, label))
        return {
            "city": loc.get("name", ""),
            "place": loc.get("place") or loc.get("name", ""),
            "days": items,
            "source": "中国天气网",
        }

    async def _cn_air(self, loc: Dict[str, Any]) -> Dict[str, Any]:
        idx = await self._cn_index(loc)
        sk = idx["sk"]
        aqi_val = str((sk or {}).get("aqi") or "").strip()
        if not aqi_val:
            raise RuntimeError("中国天气网未返回该站点 AQI 数据（海外站点无实况）")
        return {
            "place": loc.get("place") or loc.get("name", ""),
            "value": aqi_val,
            "category": _aqi_category(aqi_val),
            "primary": "",
            "pollutants": "",
            "advice": "",
            "standard": "中国（HJ 633-2012）",
            "source": "中国天气网",
        }

    @staticmethod
    def _cn_day_item(item: Dict[str, Any], label: str) -> Dict[str, Any]:
        dtext = _combine_day_night(_cn_code_text(item.get("fa")), _cn_code_text(item.get("fb")))
        return {
            "label": label,
            "date": "",
            "weekday": "",
            "text": dtext,
            "emoji": _qweather_emoji(dtext),
            "low": str(item.get("fd") or "?"),
            "high": str(item.get("fc") or "?"),
            "pop": "",
            "sun": "",
        }

    @staticmethod
    def _cn_tips(zs: Dict[str, Any]) -> str:
        idx_parts = []
        for key, label in (("ct", "穿衣"), ("uv", "紫外线"), ("gm", "感冒")):
            hint = str(zs.get(f"{key}_hint") or "").strip()
            if hint:
                idx_parts.append(f"{label} {hint}")
        return " · ".join(idx_parts)

    @staticmethod
    def _append_cn_alarms(data: Dict[str, Any], alarms: List[Dict[str, Any]]) -> None:
        for a in alarms:
            title = str(a.get("title") or a.get("w13") or "").strip()
            if not title:
                title = f"{a.get('w1', '')}发布{a.get('w5', '')}{a.get('w7', '')}色预警"
            color = str(a.get("level") or a.get("w7") or "")
            mark = ALARM_MARKS.get(color, "⚠️")
            data["alarms"].append(
                {
                    "mark": mark,
                    "title": title,
                    "body": str(a.get("text") or a.get("w9") or "").strip(),
                }
            )

    @staticmethod
    def _parse_cn_search(text: str) -> List[Tuple[str, str, str]]:
        """解析搜索接口：([{"ref":"站号~拼音~中文名~英文名~...~省市"}])

        只保留 9 位数字站号的城市级结果（排除景点/街道级条目）。
        """
        try:
            m = re.search(r"\((\[.*\])\)", text, re.DOTALL)
            if not m:
                return []
            items = json.loads(m.group(1))
            result = []
            seen = set()
            for item in items:
                ref = (item.get("ref") or "").split("~")
                if len(ref) < 3 or not ref[0] or not ref[2]:
                    continue
                if not (len(ref[0]) == 9 and ref[0].isdigit()):
                    continue  # 城市级站号固定 9 位
                region = ref[9].strip() if len(ref) > 9 and ref[9] else ""
                key = (ref[0], ref[2])
                if key in seen:
                    continue
                seen.add(key)
                result.append((ref[0], ref[2], region))
            return result
        except Exception:
            return []

    # ---------------- 数据源 3：Open-Meteo（兜底，海外服务器） ----------------

    def _om_pipeline(
        self, kind: str, days: int = 7, state: Dict[str, bool] | None = None
    ) -> Callable:
        async def run(located: CityInput, event: AstrMessageEvent | None) -> Dict[str, Any]:
            if isinstance(located, dict):
                if "lat" not in located or "lon" not in located:
                    raise RuntimeError("该城市定位不含经纬度，无法使用 Open-Meteo 数据源")
                loc = located
            else:
                cands = await self._om_lookup(located)
                if not cands:
                    raise RuntimeError(f"未找到城市“{located}”")
                loc = await self._pick_candidate(located, cands, event, state)
            if kind == "realtime":
                return await self._om_realtime(loc)
            if kind == "forecast":
                return await self._om_forecast(loc, days)
            if kind == "air":
                return await self._om_air(loc)
            if kind == "rain":
                return await self._om_rain(loc)
            raise RuntimeError(f"未知查询类型 {kind}")

        return run

    async def _om_lookup(self, city: str) -> List[Dict[str, Any]]:
        geo = await _aget_json(
            OM_GEO_URL, {"name": city, "count": 8, "language": "zh", "format": "json"}
        )
        out = []
        for loc in geo.get("results") or []:
            try:
                place = _format_place(loc)
                out.append(
                    {
                        "source": "om",
                        "name": loc.get("name") or city,
                        "display": place or loc.get("name") or city,
                        "place": place or loc.get("name") or city,
                        "lat": round(float(loc["latitude"]), 2),
                        "lon": round(float(loc["longitude"]), 2),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def _om_fetch(self, loc: Dict[str, Any], daily_fields: str = "", extra: Dict[str, Any] | None = None):
        params: Dict[str, Any] = {
            "latitude": loc["lat"],
            "longitude": loc["lon"],
            "current": (
                "temperature_2m,relative_humidity_2m,apparent_temperature,"
                "weather_code,wind_speed_10m,wind_direction_10m,pressure_msl,"
                "precipitation"
            ),
            "timezone": "auto",
        }
        if daily_fields:
            params["daily"] = daily_fields
            params["forecast_days"] = 7
        if extra:
            params.update(extra)
        return await _aget_json(OM_FORECAST_URL, params)

    async def _om_yesterday(self, loc: Dict[str, Any]) -> Dict[str, Any] | None:
        """取昨日最高/最低温（Open-Meteo 的 past_days）。

        注意：不能和主请求共用（加 past_days 会让 daily 数组整体偏移，
        破坏 today/days 的下标），所以单独发一次，并与主请求并发。
        """
        try:
            y = await _aget_json(
                OM_FORECAST_URL,
                {
                    "latitude": loc["lat"],
                    "longitude": loc["lon"],
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "past_days": 1,
                    "forecast_days": 1,
                    "timezone": "auto",
                },
            )
            d = y.get("daily") or {}
            highs = d.get("temperature_2m_max") or []
            lows = d.get("temperature_2m_min") or []
            if not highs:
                return None
            # past_days=1 时 daily 的第 0 项就是昨天
            return {"high": _num(highs[0]), "low": _num(lows[0]) if lows else ""}
        except Exception:
            return None

    async def _om_realtime(self, loc: Dict[str, Any]) -> Dict[str, Any]:
        w, yesterday = await asyncio.gather(
            self._om_fetch(
                loc,
                daily_fields=(
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max,sunrise,sunset"
                ),
            ),
            self._om_yesterday(loc),
        )
        cur = w.get("current") or {}
        code = cur.get("weather_code")
        emoji, label = WMO_CODES.get(code, ("🌡️", f"代码 {code}"))
        wind_dir = _deg_to_dir(cur.get("wind_direction_10m"))
        try:
            wind_kmh = float(cur.get("wind_speed_10m"))
        except (TypeError, ValueError):
            wind_kmh = None
        wind_txt = ""
        if wind_kmh is not None:
            wind_txt = f"{wind_dir}风" if wind_dir else ""
            wind_txt = f"{wind_txt} {_beaufort(wind_kmh)} 级".strip()

        data: Dict[str, Any] = {
            "city": loc.get("name", ""),
            "place": loc.get("place") or loc.get("name", ""),
            "cond": label,
            "emoji": emoji,
            "temp": _num(cur.get("temperature_2m")),
            "feels": _num(cur.get("apparent_temperature")),
            "humidity": _pct(cur.get("relative_humidity_2m")),
            "wind": wind_txt,
            "pressure": _num(cur.get("pressure_msl")),
            "vis": "",
            "uv": "",
            "precip": _num(cur.get("precipitation")),
            "update_time": self._now().strftime("%m-%d %H:%M"),
            "aqi": None,
            "today": None,
            "days": [],
            "alarms": [],
            "tips": "",
            "source": "Open-Meteo",
        }
        daily = w.get("daily") or {}
        dcodes = daily.get("weather_code") or []
        tmaxs = daily.get("temperature_2m_max") or []
        tmins = daily.get("temperature_2m_min") or []
        pops = daily.get("precipitation_probability_max") or []
        sunrises = daily.get("sunrise") or []
        sunsets = daily.get("sunset") or []
        for i, day_label in enumerate(("今日", "明天", "后天")):
            if i >= len(dcodes):
                break
            demoji, dlabel = WMO_CODES.get(dcodes[i], ("🌡️", "未知"))
            sr = _hm_of(str(sunrises[i]) if i < len(sunrises) else "")
            ss = _hm_of(str(sunsets[i]) if i < len(sunsets) else "")
            item = {
                "label": day_label,
                "date": "",
                "weekday": "",
                "text": dlabel,
                "emoji": demoji,
                "low": _num(tmins[i] if i < len(tmins) else None),
                "high": _num(tmaxs[i] if i < len(tmaxs) else None),
                "pop": _pct(pops[i] if i < len(pops) else None),
                "sun": f"日出 {sr or '?'} · 日落 {ss or '?'}" if (sr or ss) else "",
            }
            # Bug 修复：原实现写成了 `data["days" if i else "today" if False else "days"]`，
            # 恒等于 days，导致 today 永远为空（实时天气缺“今日”行、出行建议也拿不到温度）。
            if i == 0:
                data["today"] = item
            else:
                data["days"].append(item)

        # 昨日对比：今日最高温与昨日最高温的差
        if yesterday and data.get("today"):
            try:
                diff = round(float(data["today"]["high"]) - float(yesterday["high"]), 1)
                data["yesterday"] = {**yesterday, "diff": diff}
            except (TypeError, ValueError):
                pass
        return data

    async def _om_forecast(self, loc: Dict[str, Any], days: int = 7) -> Dict[str, Any]:
        w = await self._om_fetch(
            loc,
            daily_fields="weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        )
        daily = w.get("daily") or {}
        dcodes = daily.get("weather_code") or []
        tmaxs = daily.get("temperature_2m_max") or []
        tmins = daily.get("temperature_2m_min") or []
        pops = daily.get("precipitation_probability_max") or []
        dates = daily.get("time") or []
        items = []
        for i in range(min(days, len(dcodes))):
            demoji, dlabel = WMO_CODES.get(dcodes[i], ("🌡️", "未知"))
            date = str(dates[i]) if i < len(dates) else ""
            label = ("今日", "明天", "后天")[i] if i < 3 else _weekday_of(date) or f"第{i + 1}天"
            items.append(
                {
                    "label": label,
                    "date": _md_of(date),
                    "weekday": _weekday_of(date),
                    "text": dlabel,
                    "emoji": demoji,
                    "low": _num(tmins[i] if i < len(tmins) else None),
                    "high": _num(tmaxs[i] if i < len(tmaxs) else None),
                    "pop": _pct(pops[i] if i < len(pops) else None),
                    "sun": "",
                }
            )
        if not items:
            raise RuntimeError("Open-Meteo 未返回预报数据")
        return {
            "city": loc.get("name", ""),
            "place": loc.get("place") or loc.get("name", ""),
            "days": items,
            "source": "Open-Meteo",
        }

    async def _om_air(self, loc: Dict[str, Any]) -> Dict[str, Any]:
        w = await _aget_json(
            OM_AIR_URL,
            {
                "latitude": loc["lat"],
                "longitude": loc["lon"],
                "current": "us_aqi,pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,sulphur_dioxide,ozone",
                "timezone": "auto",
            },
        )
        cur = w.get("current") or {}
        value = _num(cur.get("us_aqi"))
        if not value:
            raise RuntimeError("Open-Meteo 未返回空气质量数据")
        parts = []
        for key, label in (
            ("pm2_5", "PM2.5"),
            ("pm10", "PM10"),
            ("ozone", "O3"),
            ("nitrogen_dioxide", "NO2"),
            ("sulphur_dioxide", "SO2"),
        ):
            v = _num(cur.get(key))
            if v:
                parts.append(f"{label} {v}μg/m³")
        return {
            "place": loc.get("place") or loc.get("name", ""),
            "value": value,
            "category": _aqi_category(value),
            "primary": "",
            "pollutants": " · ".join(parts),
            "advice": "",
            "standard": "US EPA（Open-Meteo CAMS 模型）",
            "source": "Open-Meteo",
        }

    async def _om_rain(self, loc: Dict[str, Any]) -> Dict[str, Any]:
        w = await _aget_json(
            OM_FORECAST_URL,
            {
                "latitude": loc["lat"],
                "longitude": loc["lon"],
                "hourly": "precipitation,precipitation_probability,weather_code",
                "forecast_hours": 24,
                "timezone": "auto",
            },
        )
        hourly = w.get("hourly") or {}
        times = hourly.get("time") or []
        mms = hourly.get("precipitation") or []
        pops = hourly.get("precipitation_probability") or []
        codes = hourly.get("weather_code") or []
        entries = []
        first_hit = None
        for i in range(min(24, len(times))):
            mm = mms[i] if i < len(mms) else None
            pop = pops[i] if i < len(pops) else None
            code = codes[i] if i < len(codes) else None
            _, cond_label = WMO_CODES.get(code, ("🌡️", ""))
            meaningful = (mm is not None and mm >= 0.05) or (pop is not None and pop >= 30)
            if meaningful and first_hit is None:
                first_hit = str(times[i])[11:16]
            if meaningful:
                entries.append(
                    {
                        "time": f"{str(times[i])[11:13]}时",
                        "text": cond_label or "降水",
                        "pop": _pct(pop),
                        "mm": _num(mm),
                    }
                )
        if first_hit:
            summary = f"降水将从约 {first_hit} 开始，未来 24 小时共 {len(entries)} 个时段有降水。"
        else:
            summary = "未来 24 小时无明显降水，适合安排出行 ☀️"
        return {
            "city": loc.get("name", ""),
            "place": loc.get("place") or loc.get("name", ""),
            "summary": summary,
            "entries": entries[:10],
            "source": "Open-Meteo",
        }


def _beaufort(kmh: float) -> int:
    """风速 km/h -> 蒲福风级（近似）。bounds 为各级下界，故命中时级数为 i-1。"""
    bounds = (1, 6, 12, 20, 29, 39, 50, 62, 75, 89, 103, 117)
    for i, b in enumerate(bounds, 1):
        if kmh < b:
            return i - 1
    return 12
