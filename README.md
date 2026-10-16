# EverQuant (量化投资分析平台)

EverQuant 是一个轻量级、面向实战的量化投资与市场流动性分析平台。包含宽基 ETF 资金异动雷达、MCP AI 交互服务以及多资产策略回测框架。

---

## 核心模块

### 1. 国家队异动雷达 / 宽基资金脉搏 (`national_team_radar/`)
- **真实数据监测**：跟踪核心宽基 ETF（510300、510310、510330、159919 沪深300；510050 上证50；510500 中证500；512100 中证1000）一级市场真实份额变动与前日净值估算净申赎。
- **宽基资金脉搏图表**：双 Y 轴可视化，左轴展示七只核心 ETF 估算净申赎（亿元），右轴展示沪深 300 指数收盘点位，支持一键切换显示/隐藏指数。
- **跨基金同向共振与 Z 异常度**：基于滚动基准与严格统计检验评估异常申购/赎回。
- **交互 Web 界面**：本地极速仪表盘（FastAPI + 原生轻量响应式前端）。

### 2. MCP (Model Context Protocol) 智能体分析服务 (`national_team_radar/mcp_server.py`)
- 为 AI 客户端（如 Claude Desktop、Cursor、Codex 等）提供标准的 MCP 只读工具与资源：
  - `get_market_timing_brief`：重点提炼入场支撑 (Support Watch)、获利了结/风险释放 (Exit Watch) 及数据分歧信号。
  - `get_radar_overview`：获取最新资金流、沪深 300 点位、预警事件及完整性评级。
  - `get_daily_series`：获取历史每日资金流与同日沪深 300 指数序列。
  - `get_etf_detail`：逐只 ETF 深度穿透。
  - `get_data_quality` & `get_methodology`：数据血统与质量核验。

### 3. 多资产策略与回测框架
- `backtest.py`：永久投资组合 (Permanent Portfolio) 回测（30年国债、黄金、沪深300、货币基金动态再平衡）。
- `dividend_strategy.py`：红利国债黄金策略回测（红利ETF、30年国债ETF、黄金ETF季度再平衡）。

---

## 快速上手

### 依赖安装

```bash
pip install -r radar_requirements.txt
```

### 配置 Tushare Token

可以通过环境变量或本地文件配置 Tushare Token：

```bash
# 方式 1: 环境变量（推荐）
export TUSHARE_TOKEN="your_tushare_token"   # Linux / macOS
$env:TUSHARE_TOKEN="your_tushare_token"     # Windows PowerShell

# 方式 2: 用户家目录文件
# 在当前用户家目录下创建 jsdata_tk.csv:
# token
# your_tushare_token
```

### 启动雷达 Web 界面

```bash
python run_radar.py
```
浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765) 查看。

### 启动 MCP 服务供 AI 使用

```bash
python run_radar_mcp.py
```

详见 [RADAR_README.md](RADAR_README.md)。

---

## License
MIT License
