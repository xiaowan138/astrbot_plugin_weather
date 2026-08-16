# ☀️ 天气查询 · astrbot_plugin_weather

![天气查询](logo.png)

一个面向 **AstrBot v4**（≥ 3.5.19 的 Star 架构，已在 v4.27 验证）的天气查询插件，适用于个人 / 群聊场景。

## ✨ 功能

- **命令查询**：发送 `/天气 北京` 或 `/weather Beijing`，返回该城市的实时天气（温度、湿度、风力、气压）和今日预报。
- **AI 工具调用**：注册了 `get_weather` LLM 工具，AI 收到"北京天气怎么样"这类问题时会自动调用工具查询，无需手动输入命令。
- **多数据源自动切换**（按优先级）：
  1. **和风天气**：配置 API Key 后启用，数据最全（推荐）。
  2. **中国天气网**：零 API Key、国内服务器直连 —— **国内部署开箱即用**。
  3. **Open-Meteo**：兜底，适合海外服务器（国内服务器可能无法访问该域名）。
- **原生插件配置**：提供 `_conf_schema.json`，可在 AstrBot Web 面板的插件配置页直接编辑（无需改文件）。
- **零第三方依赖**：只用 Python 标准库，安装即用。

## 📦 安装

### 方式一：Web 界面上传 zip（推荐）

1. 打开 AstrBot Web 面板 → **插件管理** → **安装插件** → 上传 `astrbot_plugin_weather.zip`。
2. 安装完成后启用插件。

> ⚠️ 如果上传报 `FileNotFoundError: .../astrbot_plugin_weather`（zip 解压后找不到插件目录）：
> 这是 AstrBot v4 对"文件夹包裹型"zip 的解压兼容问题（服务器端把反斜杠路径当成普通文件名）。
> 本仓库提供的 `astrbot_plugin_weather.zip` 已改为**平铺结构**（文件直接在 zip 根目录，无外层文件夹），
> 走 v4 的"平铺"安装分支，**不会触发该错误**。请务必使用本 zip，不要自行重新压缩。

### 方式二：手动复制（最稳，绕过 zip 解压逻辑）

1. 把 `astrbot_plugin_weather` **整个文件夹**复制到 AstrBot 服务器的 `data/plugins/` 目录下：
   ```bash
   # 在服务器上（AstrBot 目录下）
   cp -r astrbot_plugin_weather /AstrBot/data/plugins/
   ```
2. 在 Web 面板重启 AstrBot（或重载插件）。
3. 插件管理中找到并启用。

### 服务器上自检 zip（可选）

```bash
unzip -l astrbot_plugin_weather.zip
# 期望看到 main.py / metadata.yaml / _conf_schema.json / config.json / README.md
# 直接列在根目录（正斜杠、无外层文件夹、无反斜杠）
```

## 🚀 使用方法

| 操作 | 示例 |
| --- | --- |
| 查询天气 | `/天气 北京`、`/天气 上海`、`/weather Tokyo`、`/weather New York` |
| 直接问 AI | "北京今天天气怎么样？"（需在 AstrBot 中开启工具调用，且模型支持） |

> 命令前缀 `/` 是 AstrBot 默认值（`wake_prefix`），可在设置中修改，改后命令自动跟随新前缀。
> v4 中命令名与参数之间**必须有空格**：`/天气 北京` ✅，`/天气北京` ❌。

### 示例输出（中国天气网，国内默认数据源）

```
🌧️ 北京 · 当前天气
天气：小雨转晴
温度：22.8°C
湿度：88%
风向：北风 0级
气压：1002 hPa
今日：小雨转晴，20℃ ~ 27℃
数据来源：中国天气网
```

## ⚙️ 配置

Web 面板 → 插件管理 → 该插件的 **配置** 页（基于 `_conf_schema.json` 自动生成）：

| 配置项 | 说明 |
| --- | --- |
| `qweather_api_key` | 可选（推荐）。和风天气 API Key（免费申请：[和风天气控制台](https://console.qweather.com/)）。填写后优先使用和风天气；留空则用中国天气网（零 Key）。 |
| `default_city` | 可选。默认城市，如 `北京`。发送 `/天气`（不带城市）时返回默认城市天气。 |

> 兜底：插件目录下的 `config.json` 也可配置（格式同上），仅在没有 v4 原生配置时生效。

## 📁 文件结构

```
astrbot_plugin_weather/
├── main.py              # 插件主代码（v4 装饰器风格）
├── metadata.yaml        # 插件元数据（v4 必填 name/desc/version/author）
├── _conf_schema.json    # v4 原生配置 schema（Web 面板可编辑）
├── config.json          # 兜底配置文件
├── logo.png             # 插件图标
└── README.md            # 本说明

astrbot_plugin_weather.zip   # 安装包：以上文件平铺在 zip 根目录（无外层文件夹）
```

## ❓ 常见问题

- **装好插件发 `/天气` 没反应/很慢**：国内服务器访问 Open-Meteo（国外域名）通常超时。本插件已默认使用**中国天气网**（国内直连），请升级到 v1.1.0 及以上；若仍慢，检查服务器到 `d1.weather.com.cn` 的连通性。
- **AI 不自动调用工具**：确认 AstrBot 中已启用"工具调用 / Function Calling"，且当前模型支持。
- **想用和风天气**：到[和风天气控制台](https://console.qweather.com/)免费注册，创建项目拿到 Key，填入插件配置后重启 AstrBot。
- **查询不到城市**：使用标准城市名（如"北京""上海"），偏远地区可试试附近大城市；海外城市请配置和风天气或使用 Open-Meteo 可达的服务器。

## 📄 协议

MIT License — 随意修改使用。
