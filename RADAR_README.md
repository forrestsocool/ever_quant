# 国家队异动雷达

在本机监测七只宽基 ETF 的份额变化，用真实份额与前一交易日净值估算净申赎，识别大额申购、赎回和跨 ETF 同向异动。

## 启动

```powershell
cd E:\Project\ever_quant
python -m pip install -r radar_requirements.txt
python run_radar.py
```

浏览器打开 http://127.0.0.1:8765 。首次启动自动读取数据；以后先显示本地缓存，再更新。服务仅监听本机。

```powershell
# 仅更新并输出快照、预警，不启动网页
python run_radar.py --once

# 指定历史区间
python run_radar.py --start-date 2025-01-01 --end-date 2026-10-15

# 更换端口或关闭定时刷新
python run_radar.py --port 8766 --no-auto-update

# 单元测试
python -m pytest tests -q
```

## 数据与认证

系统全部使用真实市场数据。默认复用 Tushare 认证（支持 `TUSHARE_TOKEN`、`SXSC_TUSHARE_TOKEN` 或本地 `~/jsdata_tk.csv`）。当 Tushare 接口受限、积分不足或网络异常获取不到数据时，系统具备**自动互联网开源数据备用补全机制**（自动从新浪财经、天天基金网、东方财富、AkShare 等公开权威数据源补充指数收盘曲线、ETF净值、日线行情与交易日历），确保真实数据的高可用与持续更新。认证仅存在于服务端内存，绝不泄露到前端或代码中。

真实接口优先尝试 `etf_share_size`；当前本地代理未提供时，回退到 `fund_share`、`fund_nav` 和 `fund_daily`，交易日使用 `trade_cal`。不以收盘价冒充基金净值，不对缺失份额向前填充，不用 ETF 规模涨跌代替申购。

监控池：510300、510310、510330、159919（沪深300）；510050（上证50）；510500（中证500）；512100（中证1000）。它是固定的宽基观察池，不能据此认定这些产品的每次交易均来自国家队。

## 指标口径

单只 ETF 当日估算净申赎（亿元）：

```text
(当日总份额[万份] − 前一交易日总份额[万份]) × 10000 × 前一交易日单位净值[元] ÷ 100000000
```

组合值为七只 ETF 的同日估算值合计。正数是净申购，负数是净赎回。这是按净值折算的申赎对价估计，可能包含实物证券，不能理解为等额现金转账。

前一日必须是交易日历中紧邻的交易日；缺失不会跨日相减。组合缺少任何一只的有效流量时不展示完整合计；5日和20日累计也要求连续交易日数据完整。

Z-score 以当天之前紧邻的20个交易日估算值为默认基准，不把当天数值纳入基准。窗口内任何交易日缺样本时不借用更早数据，样本不足或标准差为零时显示不可计算。默认单只净申赎绝对值至少10亿元且方向一致的 Z-score 绝对值至少2，视为单只异常；默认至少3只同方向异常，且组合自身也满足金额和 Z 阈值，触发组合异动。只达到同步条件时单列“同步观察”，并展示组合实际净额，避免其他 ETF 的反向流量抵消后漏掉线索。阈值可在页面调整，并非经回测校准的国家队介入概率。

份额变幅超过20%时复核当日净值；当日净值尚未披露，先暂停该日估算。若份额与净值大幅反向变化，且份额比×净值比接近1，系统将疑似拆分/合并隔离，整数和非整数比例均处理，不直接计入净申赎。被隔离日也不会污染后续基准。规则是保守筛查，分红、数据修订和其他基金事件仍需结合公告核查；因此部分真实大额申购会等待净值披露后才能形成信号。

份额和净值通常在清算后、次日分批披露，最新行情日可以领先于最新份额日。页面默认展示“最近完整日”，主指标、趋势和明细使用同一日期；最新披露进度单独显示，可切换到“最新披露”查看缺口，也可选择历史区间。历史查询会排除晚于所选截止日公告的净值，并截断基准行情。历史图仍使用当前能获取的数据修订版本，没有重建份额披露时刻，不是严格的历史时点可见性回测。

“宽基资金脉搏”叠加沪深300指数收盘曲线：左轴是七只 ETF 估算净申赎（亿元），右轴是指数点位。图例中的开关可单独隐藏指数；缺失的指数行情保留空白，不用相邻日价格补齐。两条曲线只是按交易日对照，不表示资金变化导致指数涨跌。

## 供其他 AI 读取的 MCP 服务

雷达提供本机只读的 MCP stdio 服务。AI 客户端启动以下脚本即可读取与网页一致的本地缓存；MCP 进程不会调用 Tushare、触发刷新或发起交易。先运行 `python run_radar.py --once` 建立数据缓存，以后可用网页或定时任务继续更新。常驻 MCP 进程会在缓存更新后重新读取。

```powershell
python E:\Project\ever_quant\run_radar_mcp.py
```

将以下服务配置填入支持 MCP stdio 的客户端；如客户端使用自己的 Python 环境，请把 `command` 换成已安装 `radar_requirements.txt` 依赖的 Python 可执行文件绝对路径：

```json
{
  "mcpServers": {
    "national-team-radar": {
      "command": "python",
      "args": ["E:\\Project\\ever_quant\\run_radar_mcp.py"]
    }
  }
}
```

### 核心决策接口（突出重点，驱动 AI 择时投机与获利了结）

1. **一站式高光速览：`get_fund_pulse_snapshot`**
   - 突出重点，一键返回最新资金脉搏定性（极强托底/大额净流入/均衡/大额赎回预警）、沪深300收盘与涨跌幅、量价背离警示、以及为下游 AI 提炼的最高优先级一句话操作建议。

2. **择时投机决策：`get_speculation_signals(end_date=None, risk_preference='balanced')`**
   - 产出 **0-100 分量化投机评分** 与 **明确操作指令**（`STRONG_SPECULATIVE_BUY` 强烈做多 / `SPECULATIVE_BUY` 逢低试探 / `ACCUMULATE` 温和吸筹 / `AVOID` 规避）。
   - **量价底背离检测**：精准识别“沪深300指数下跌但宽基遭遇大额逆势资金净流入”的典型国家队/机构托底抄底信号。
   - **执行策略**：提供建议建仓仓位比例（如 50% 积极仓位）、优选目标 ETF 榜单（按净申购量与单基 Z 值排序）、沪深300参考支撑位、第一/第二止盈目标线及止损保护点位。

3. **获利了结与防守：`get_profit_taking_signals(end_date=None, holding_return_pct=None, cost_benchmark_level=None)`**
   - 产出 **0-100 分出局紧迫度评分** 与 **明确出局指令**（`STRONG_TAKE_PROFIT` 果断止盈 / `PARTIAL_TAKE_PROFIT` 分批减仓 / `TRAILING_STOP` 移动止盈 / `HOLD_SAFE` 安心持有）。
   - **量价顶背离检测**：捕捉“指数冲高上涨但宽基资金大额撤退减持”的逢高派发风险。
   - **执行策略**：建议减仓比例（如减仓 50%）、移动止盈保护规则、以及赎回压力最大的高危 ETF 明细。

4. **宏观协同分析：`get_csi300_correlation_analysis(limit=60)`**
   - 计算历史区间宽基资金流与沪深300指数的滚动相关系数，统计历史底背离与顶背离发生次数及后续行情特征。

### 辅助穿透工具与资源
- `get_market_timing_brief`：阈值硬性匹配与阻断原因核验。
- `get_radar_overview`：核对最新披露、完整日与预警列表。
- `get_daily_series`：历史日序列与同日沪深300指数（支持 limit/offset 分页）。
- `get_etf_detail`：逐只 ETF 深度穿透历史数据与贡献来源。
- `get_data_quality` & `get_methodology`：数据质量与计算口径说明。
- **资源 (`radar://`)**：`radar://pulse`（脉搏快照）、`radar://speculation`（投机信号）、`radar://profit-taking`（了结信号）、`radar://overview`、`radar://timing`、`radar://universe` 等。
- **内置 Prompt 模板**：`timing_speculation_strategy`（择时投机分析报告模板）、`profit_taking_strategy`（获利了结与风控报告模板）。

## 交叉验证

`national_team_radar/evidence.csv` 可以记录独立证据：

```csv
date,title,url,kind
2025-04-07,已核实的公告标题,https://发布机构/公告地址,official
```

上面仅是格式示例，程序自带文件为空。`kind` 可用 `official`、`holding`、`market`。请填写公告真实发布日期；页面不会将晚于观察日的记录用于该日展示。此版本不会自动抓取公告、龙虎榜或机构身份数据，证据需要手工核实补充。

份额异常、成交额异常和多只 ETF 同步均属于市场行为线索，彼此不能替代买方身份披露。看板不生成“国家队买入金额”或身份置信概率。需要官方增持公告、持有人定期报告等独立材料才能进一步核实。

## 缓存、更新与输出

原始市场数据、来源状态和分析结果保存在 `national_team_radar/data/`，该目录已忽略。刷新失败会保留旧数据并显示错误，不悄悄切换到演示数据。默认查询过去一年，抓取起点会额外提前270个自然日，供历史基准计算使用；网页只显示所选区间。

服务运行期间，按北京时间工作日09:00和18:30自动更新；交易日判断由交易日历负责。电脑休眠、程序退出时不运行；恢复或启动后，会补执行当日最近一个已到时点，繁忙时延后执行，历史日期的时点不会逐一补跑。需要无人值守时可在已有 Windows 任务计划中调用 `python E:\Project\ever_quant\run_radar.py --once`。

- `data/market_data.json`：标准化观察记录、交易日历、来源元数据。
- `data/snapshot.json`：默认阈值下最近一次更新的雷达结果。
- `data/alerts.csv`：默认阈值下的历史预警，可用 Excel 打开。
- 页面“导出 CSV”：所选日期区间及当前阈值下的单只 ETF 明细，含份额、估算值和质量状态。

页面阈值修改只重新计算，不额外请求行情接口；刷新按钮才访问数据源。页面每30秒检查本地服务更新状态，自动更新完成后同步当前视图，历史查询保留所选截止日。查询成功后才提交日期、阈值与导出范围；失败时保留已有结果，避免标签与数据不一致。快照和预警文件在数据更新时写入，页面临时阈值不改写默认输出。

## 依据

- [上交所：ETF 的交易机制](https://etf.sse.com.cn/fund/learning/knowledge/c/5704300.shtml)
- [上交所：ETF 常见问题](https://www.sse.com.cn/assortment/fund/etf/question/)
- [Tushare：基金份额](https://tushare.pro/document/2?doc_id=207)
- [Tushare：ETF 份额规模](https://tushare.pro/document/2?doc_id=408)
