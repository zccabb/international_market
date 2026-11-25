# 国际行情采集与自选看板

一个可在本机独立运行的 Python 行情采集器。一次启动后台轮询和中文网页，浏览最新价格、相对昨收的涨跌幅、行情时间、数据源、分钟走势和日线走势。在页面添加或移除标的即可调整采集范围。

## 快速启动

需要 Python 3.10 或更新版本，以及访问所选行情源的网络连接。首次启动会建立 SQLite 数据库并加入七个公开示例标的。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python market.py
```

macOS / Linux：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python market.py
```

打开 [本地看板](http://127.0.0.1:8765)。服务只监听本机，按 Ctrl+C 停止。可用 `python market.py --port 8780 --interval 90` 修改监听端口和单票轮询间隔。表格每 10 秒读取一次本地数据，采集器默认每票每轮完成后等待 60 秒，最多同时处理 4 个标的。日线首次加入时抓取，以后约每 30 分钟更新一次。

## 标的与数据源

| 市场 | 输入示例 | 行情来源 |
| --- | --- | --- |
| 美股 | `AAPL`、`BRK-B` | Yahoo Finance 图表、TradingView 常规时段报价和支持标的的全时段分钟成交 |
| 港股 | `700`、`00700`、`0700.HK` | 腾讯报价、Yahoo Finance 分钟线和日线 |
| 日股 | `7203`、`7203.T` | Yahoo Finance 公开图表 |
| 韩股 | `005930.KS`、`247540.KQ` | NAVER 报价、Yahoo Finance 图表，裸代码默认为 KOSPI |
| 台股 | `2330.TW`、`6488.TWO` | Yahoo 台湾报价及图表，裸代码默认为上市 |
| 国际指数 | `^GSPC`、`^IXIC`、`^HSI` | Yahoo Finance 图表 |
| 美国期货 | `ES1!`、`NQ1!`、`YM1!`、`RTY1!`、`VX1!` 或 `ES=F` | 五个连续合约使用 TradingView，`=F` 代码使用 Yahoo Finance，各自独立入库 |

选择市场后输入代码。点清单中的代码切换走势图，将指针移到图上可查看该根 K 线的开高低收和成交量。移除操作会停止采集并删除该标的在本地的行情记录，重启保留自选清单，包括空清单。

公开接口的延迟、可用时段和历史范围取决于市场与标的。Yahoo Finance 初始请求当前可用的一天分钟线和一个月日线，之后逐轮累积。台股使用台湾接口返回的可用区间。TradingView 连续期货按交易所时段和假期修正划分日线交易日。图表显示各源返回的 OHLC，未做跨源复权。美股扩展时段使用有成交量的分钟收盘价；缺少新成交时保留最近行情及其原时间。港韩台的首选报价不可用时尝试 Yahoo 图表快照，页面显示实际报价源。部分数据失败时保留上次成功的数据，并在清单标记受影响的部分。

## 采集代理

代理是可选项，使用者自行提供地址。行情 HTTP 请求、备用数据源和 TradingView WebSocket 均使用所属市场的策略。默认 `system` 跟随系统代理设置，包括 `HTTPS_PROXY`、`ALL_PROXY` 和 `NO_PROXY`。

| 设置 | 作用 |
| --- | --- |
| `MARKET_PROXY` | 全局策略。可以填 `system`、`direct`（强制直连），或一个自有代理 URL |
| `MARKET_PROXY_<市场>` | 覆盖该市场的全局策略。市场可取 `US`、`HK`、`JP`、`KR`、`TW`、`INDEX`、`FUTURES` |

例如在 PowerShell 中让美股和期货使用你本机已运行的代理，其余市场直连。示例端口需要替换为你自己的监听端口。

```powershell
$env:MARKET_PROXY = "direct"
$env:MARKET_PROXY_US = "http://127.0.0.1:7890"
$env:MARKET_PROXY_FUTURES = $env:MARKET_PROXY_US
.\.venv\Scripts\python market.py
```

也可以把代理 URL 放在 `MARKET_PROXY`，单独设置 `MARKET_PROXY_HK=direct` 等例外。macOS / Linux 使用同名环境变量和 `export`。支持 HTTP CONNECT、SOCKS5（`socks5://`）和代理端解析域名的 SOCKS5（`socks5h://`）。需要认证时可以在本机环境变量中使用 `协议://用户名:密码@主机:端口`，用户名和密码里的特殊字符须进行 URL 编码。

修改后重启采集器生效。显式指定的代理连接失败时，该轮请求记为失败，后备数据源仍执行同一策略。看板和数据库连接不经过采集代理。仓库不含代理池、订阅或代理凭据。

## 存储

默认数据库在启动目录的 `data/market.sqlite3`。设置 `MARKET_SQLITE_PATH` 可以指定其他文件位置。

使用 MySQL 时，先准备独立的空库和专用用户。应用会建三张表，需要该库的 `CREATE`、`REFERENCES`、`SELECT`、`INSERT`、`UPDATE`、`DELETE` 权限。请在启动进程的环境中配置以下变量。连接参数仅在运行时读取。

| 环境变量 | MySQL 模式 |
| --- | --- |
| `MARKET_DB` | 设置为 `mysql`，默认值是 `sqlite` |
| `MARKET_MYSQL_HOST` | 数据库主机 |
| `MARKET_MYSQL_PORT` | 端口，默认 3306 |
| `MARKET_MYSQL_DATABASE` | 已创建的数据库名称 |
| `MARKET_MYSQL_USER` | 数据库用户名 |
| `MARKET_MYSQL_PASSWORD` | 数据库密码 |

配置后用同一条启动命令运行。切换后端会使用各自独立的自选清单和历史数据。两个后端都按标的、周期和时间更新已有 K 线，重复采集同一根不会产生多条记录。

## 测试

```sh
python -m unittest discover -s tests -v
```

测试使用临时 SQLite 文件和固定样本。运行一个采集进程即可同时提供本地 API 和网页，每个数据库请只启动一个采集进程。
