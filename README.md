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
- 为 AI 客户端（如 Claude Desktop、Cursor、Codex 等）提供标准的 MCP 工具与资源：
  - `get_fund_pulse_snapshot`：一站式全景快照，突出重点，一键返回最新资金脉搏定性、沪深300对比与核心决策摘要。
  - `get_speculation_signals`：**专为 AI 择时投机打造**。输出 0-100 量化评分、操作建议（`STRONG_SPECULATIVE_BUY` 等）、具体建仓仓位建议、量价底背离托底检测、止损与止盈点位参考。
  - `get_profit_taking_signals`：**专为 AI 获利了结打造**。评估赎回压力与顶背离风险，输出出局紧迫度评分、操作指令（`STRONG_TAKE_PROFIT` 等）、建议减仓比例与移动止盈保护规则。
  - `get_csi300_correlation_analysis`：沪深300指数与资金脉搏协同分析（滚动相关系数、底背离与顶背离统计）。
  - `get_market_timing_brief` & `get_radar_overview`：观察条件核验、最新披露与预警。
  - `get_daily_series` & `get_etf_detail`：逐日历史流与单基穿透。
  - `get_data_quality` & `get_methodology`：数据质量与计算方法论。
  - **Prompt 模板**：`timing_speculation_strategy` 和 `profit_taking_strategy` 一键调用。

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
