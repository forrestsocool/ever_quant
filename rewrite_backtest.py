import re
import os

with open('backtest.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Replace run_backtest
new_run_backtest = '''
# ─────────────────── 回测引擎 ───────────────────
def run_backtest(navs: pd.DataFrame, rebalance_type="threshold"):
    """
    navs: 各资产归一化净值 DataFrame (index=date)
    rebalance_type: "threshold" 或 "annual"
    返回: daily_records DataFrame, rebalance_log list
    """
    asset_names = list(navs.columns)
    dates = navs.index
    
    # 初始化: 各25%, 用净值=1 的"份额"概念
    init_alloc = INITIAL_CAPITAL * TARGET_WEIGHT
    units = {name: init_alloc / navs[name].iloc[0] for name in asset_names}
    
    daily_records = []
    rebalance_log = []
    
    for i, date in enumerate(dates):
        # 各资产市值
        values = {name: units[name] * navs[name].iloc[i] for name in asset_names}
        total = sum(values.values())
        weights = {name: values[name] / total for name in asset_names}
        
        record = {"date": date, "total_value": total}
        for name in asset_names:
            record[f"{name}_value"] = values[name]
            record[f"{name}_weight"] = weights[name]
        daily_records.append(record)
        
        # 再平衡检查 (跳过首日)
        if i == 0:
            continue
            
        should_rebalance = False
        trigger_reason = ""
        
        if rebalance_type == "threshold":
            if any(w <= LOWER_BOUND or w >= UPPER_BOUND for w in weights.values()):
                should_rebalance = True
                deviations = sorted(asset_names, key=lambda n: abs(weights[n] - TARGET_WEIGHT), reverse=True)
                trigger_reason = ", ".join(f"{n}={weights[n]*100:.1f}%" for n in deviations[:2])
        elif rebalance_type == "annual":
            if date.year != dates[i-1].year:
                should_rebalance = True
                trigger_reason = "年度定投/再平衡"
                
        if not should_rebalance:
            continue
        
        # 再平衡: 所有资产回到25%
        target_values = {name: total * TARGET_WEIGHT for name in asset_names}
        
        rebal_detail = {}
        for name in asset_names:
            rebal_detail[name] = {
                "before": values[name], "after": target_values[name],
                "change": target_values[name] - values[name],
                "before_weight": weights[name],
            }
            units[name] = target_values[name] / navs[name].iloc[i]
        
        rebalance_log.append({
            "date": date,
            "trigger": trigger_reason,
            "total_value": total,
            "detail": rebal_detail,
        })
    
    return pd.DataFrame(daily_records), rebalance_log
'''

content = re.sub(r'# ─────────────────── 回测引擎 ───────────────────[\s\S]*?# ─────────────────── HTML 报告 ───────────────────', new_run_backtest + '\n# ─────────────────── HTML 报告 ───────────────────', content)

# 2. Replace generate_html_report
new_generate_html_report = '''
def generate_html_report(results: dict, navs: pd.DataFrame, output_path: str):
    """生成独立 HTML 报告, 带有策略切换功能"""
    asset_names = list(navs.columns)
    dates_str_all = [d.strftime("%Y-%m-%d") for d in navs.index]
    total_days = (navs.index[-1] - navs.index[0]).days
    total_years = total_days / 365.0
    
    # ─── 单资产指标 ───
    single_asset_navs = {}
    single_asset_metrics = {}
    
    for name in asset_names:
        sa_nav = (navs[name] / navs[name].iloc[0]).tolist()
        sa_final = INITIAL_CAPITAL * navs[name].iloc[-1] / navs[name].iloc[0]
        single_asset_navs[name] = sa_nav
        
        sa_ret = (sa_final / INITIAL_CAPITAL - 1) * 100
        sa_ann = ((sa_final / INITIAL_CAPITAL) ** (1 / total_years) - 1) * 100
        sa_s = pd.Series(sa_nav)
        sa_dd = ((sa_s - sa_s.cummax()) / sa_s.cummax()).min() * 100
        sa_dr = sa_s.pct_change().dropna()
        sa_vol = sa_dr.std() * np.sqrt(252) * 100
        sa_sharpe = (sa_ann - MONEY_FUND_ANNUAL_RATE * 100) / sa_vol if sa_vol > 0 else 0
        
        single_asset_metrics[name] = {
            "final": sa_final, "total_ret": sa_ret, "annual_ret": sa_ann,
            "max_dd": sa_dd, "vol": sa_vol, "sharpe": sa_sharpe,
        }
    
    # 构建所有策略的数据 JS 对象
    strategy_data_json = {}
    strategy_metrics_html = ""
    colors = ["#e74c3c", "#f39c12", "#3498db", "#2ecc71"]
    
    for strat_key, (df, rebal_log) in results.items():
        final_value = df["total_value"].iloc[-1]
        total_return = (final_value / INITIAL_CAPITAL - 1) * 100
        annual_return = ((final_value / INITIAL_CAPITAL) ** (1 / total_years) - 1) * 100
        cummax = df["total_value"].cummax()
        max_dd = ((df["total_value"] - cummax) / cummax).min() * 100
        daily_ret = df["total_value"].pct_change().dropna()
        annual_vol = daily_ret.std() * np.sqrt(252) * 100
        sharpe = (annual_return - MONEY_FUND_ANNUAL_RATE * 100) / annual_vol if annual_vol > 0 else 0
        
        nav = (df["total_value"] / INITIAL_CAPITAL).tolist()
        rebal_dates = [r["date"].strftime("%Y-%m-%d") for r in rebal_log]
        rebal_navs = []
        for r in rebal_log:
            idx = df[df["date"] == r["date"]].index
            rebal_navs.append(df.loc[idx[0], "total_value"] / INITIAL_CAPITAL if len(idx) > 0 else None)
            
        weight_traces = []
        value_traces = []
        for idx_a, name in enumerate(asset_names):
            weight_traces.append({
                "x": dates_str_all, "y": (df[f"{name}_weight"] * 100).round(2).tolist(),
                "name": name, "type": "scatter", "mode": "lines",
                "line": {"color": colors[idx_a], "width": 1.5}, "stackgroup": None,
            })
            value_traces.append({
                "x": dates_str_all, "y": (df[f"{name}_value"] / 10000).round(2).tolist(),
                "name": name, "type": "scatter", "mode": "lines",
                "line": {"color": colors[idx_a], "width": 1.5},
            })
            
        rebal_rows_html = ""
        for idx_r, r in enumerate(rebal_log, 1):
            det = r["detail"]
            changes = []
            for name in asset_names:
                d = det[name]
                arrow = chr(8593) if d["change"] > 0 else chr(8595)
                changes.append(f"{name}: {d['before_weight']*100:.1f}%->25% ({arrow}{abs(d['change'])/10000:.1f}w)")
            rebal_rows_html += f"<tr><td>{idx_r}</td><td>{r['date'].strftime('%Y-%m-%d')}</td><td>{r['trigger']}</td><td>{r['total_value']/10000:.2f}w</td><td style='text-align:left'>{'<br>'.join(changes)}</td></tr>"
            
        strategy_data_json[strat_key] = {
            "metrics": {
                "finalValue": final_value, "totalReturn": total_return, "annualReturn": annual_return,
                "maxDd": max_dd, "vol": annual_vol, "sharpe": sharpe, "rebalCount": len(rebal_log)
            },
            "nav": nav,
            "rebalDates": rebal_dates,
            "rebalNavs": rebal_navs,
            "weightTraces": weight_traces,
            "valueTraces": value_traces,
            "rebalHtml": rebal_rows_html
        }
        strat_name_cn = "阈值限制(15/35%)" if strat_key == "threshold" else "年度触发(每逢1/1)"
        strategy_metrics_html += f"""<tr style="background:rgba(255,255,255,0.05)"><td style="color:#fff"><b>{strat_name_cn}</b></td><td>{final_value/10000:.2f}万</td><td>{total_return:+.2f}%</td><td>{annual_return:+.2f}%</td><td>{max_dd:.2f}%</td><td>{annual_vol:.2f}%</td><td>{sharpe:.3f}</td></tr>"""

    compare_rows_html = strategy_metrics_html
    for name in asset_names:
        m = single_asset_metrics[name]
        compare_rows_html += f"<tr><td>{name}全仓</td><td>{m['final']/10000:.2f}万</td><td>{m['total_ret']:+.2f}%</td><td>{m['annual_ret']:+.2f}%</td><td>{m['max_dd']:.2f}%</td><td>{m['vol']:.2f}%</td><td>{m['sharpe']:.3f}</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>永久投资组合回测报告</title>
<script src="https://cdn.plot.ly/plotly-2.35.0.min.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; background: #0f0f23; color: #e0e0e0; }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
  h1 {{ text-align: center; font-size: 28px; margin: 30px 0 10px; 
        background: linear-gradient(90deg, #e74c3c, #f39c12, #3498db, #2ecc71);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
  .subtitle {{ text-align: center; color: #888; margin-bottom: 20px; font-size: 14px; }}
  .ctrl-panel {{ display: flex; justify-content: center; align-items: center; margin-bottom: 30px; gap: 15px; background: rgba(255,255,255,0.05); padding: 15px; border-radius: 12px; }}
  select {{ padding: 8px 16px; font-size: 16px; border-radius: 8px; background: #1a1a3a; color: #fff; border: 1px solid #444; outline: none; cursor: pointer; }}
  .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 30px; }}
  .metric {{ background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
             border-radius: 12px; padding: 20px; text-align: center; }}
  .metric .label {{ font-size: 13px; color: #888; margin-bottom: 6px; }}
  .metric .value {{ font-size: 24px; font-weight: 700; }}
  .metric .value.green {{ color: #2ecc71; }}
  .metric .value.red {{ color: #e74c3c; }}
  .metric .value.blue {{ color: #3498db; }}
  .metric .value.gold {{ color: #f39c12; }}
  .chart-box {{ background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08);
                border-radius: 12px; padding: 16px; margin-bottom: 24px; }}
  .chart-box h2 {{ font-size: 16px; margin-bottom: 12px; color: #ccc; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: rgba(255,255,255,0.08); padding: 10px 8px; text-align: center; }}
  td {{ padding: 8px; border-bottom: 1px solid rgba(255,255,255,0.06); vertical-align: top; }}
  tr:hover {{ background: rgba(255,255,255,0.03); }}
  .footer {{ text-align: center; color: #555; font-size: 12px; margin-top: 40px; padding: 20px; }}
</style>
</head>
<body>
<div class="container">
  <h1>永久投资组合回测报告</h1>
  <p class="subtitle">30年国债 · 黄金 · 沪深300 · 货币基金(年化2%)  &nbsp;|&nbsp; {dates_str_all[0]} ~ {dates_str_all[-1]} &nbsp;|&nbsp; 前复权 + 久期模型</p>
  
  <div class="ctrl-panel">
    <label for="stratSelect" style="font-weight:bold; color:#fff">选择再平衡策略：</label>
    <select id="stratSelect">
      <option value="threshold">阈值触发 (15% 或 35%)</option>
      <option value="annual">年度定投 (每年首个交易日)</option>
    </select>
  </div>

  <div class="metrics">
    <div class="metric"><div class="label">初始资金</div><div class="value">100万</div></div>
    <div class="metric"><div class="label">期末资产</div><div class="value" id="val-end-value">-</div></div>
    <div class="metric"><div class="label">总收益率</div><div class="value" id="val-total-ret">-</div></div>
    <div class="metric"><div class="label">年化收益率</div><div class="value" id="val-ann-ret">-</div></div>
    <div class="metric"><div class="label">最大回撤</div><div class="value red" id="val-max-dd">-</div></div>
    <div class="metric"><div class="label">年化波动率</div><div class="value blue" id="val-vol">-</div></div>
    <div class="metric"><div class="label">Sharpe 比率</div><div class="value gold" id="val-sharpe">-</div></div>
    <div class="metric"><div class="label">再平衡次数</div><div class="value" id="val-rebal-count">-</div></div>
  </div>

  <div class="chart-box">
    <h2>净值对比曲线 (基准=1.0)</h2>
    <div id="nav_chart"></div>
  </div>

  <div class="chart-box">
    <h2>各资产占比变化 (%)</h2>
    <div id="weight_chart"></div>
  </div>

  <div class="chart-box">
    <h2>各资产市值变化 (万元)</h2>
    <div id="value_chart"></div>
  </div>

  <div class="chart-box">
    <h2>再平衡记录表</h2>
    <table>
      <thead><tr><th>#</th><th>日期</th><th>触发条件</th><th>总资产</th><th>调整详情</th></tr></thead>
      <tbody id="rebal-table-body"></tbody>
    </table>
  </div>

  <div class="chart-box">
    <h2>策略全局对比表</h2>
    <table>
      <tr><th>策略</th><th>期末资产</th><th>总收益率</th><th>年化收益率</th><th>最大回撤</th><th>年化波动率</th><th>Sharpe</th></tr>
      {compare_rows_html}
    </table>
  </div>

  <div class="footer">
    <p>数据来源: sxsc_tushare + akshare</p>
  </div>
</div>

<script>
// ---------- 数据传递区块 ----------
const rawDates = {json.dumps(dates_str_all)};
const strategyData = {json.dumps(strategy_data_json)};
const assetNames = {json.dumps(asset_names)};
const singleAssetNavs = {json.dumps(single_asset_navs)};

const plotLayout = {{
  paper_bgcolor: 'rgba(0,0,0,0)',
  plot_bgcolor: 'rgba(0,0,0,0)',
  font: {{ color: '#ccc', size: 12 }},
  xaxis: {{ gridcolor: 'rgba(255,255,255,0.06)', type: 'date', rangeslider: {{ visible: true }} }},
  yaxis: {{ gridcolor: 'rgba(255,255,255,0.06)' }},
  legend: {{ orientation: 'h', x: 0.5, xanchor: 'center', y: -0.15 }},
  margin: {{ l: 50, r: 20, t: 10, b: 60 }},
  hovermode: 'x unified',
}};

const sC = {{'30年国债':'#e74c3c','黄金':'#f39c12','沪深300':'#3498db','货币基金':'#2ecc71'}};

let currentStrat = "threshold";

function formatNum(n, decimals, plus) {{
    var s = n.toFixed(decimals);
    if (plus && n > 0) s = '+' + s;
    return s;
}}

function renderStrategy(stratKey) {{
    const data = strategyData[stratKey];
    const m = data.metrics;
    
    // 1. Update metric cards
    document.getElementById('val-end-value').innerHTML = (m.finalValue/10000).toFixed(2) + '万';
    document.getElementById('val-end-value').className = 'value ' + (m.finalValue > 1000000 ? 'green' : 'red');
    document.getElementById('val-total-ret').innerHTML = formatNum(m.totalReturn, 2, true) + '%';
    document.getElementById('val-total-ret').className = 'value ' + (m.totalReturn > 0 ? 'green' : 'red');
    document.getElementById('val-ann-ret').innerHTML = formatNum(m.annualReturn, 2, true) + '%';
    document.getElementById('val-ann-ret').className = 'value ' + (m.annualReturn > 0 ? 'green' : 'red');
    document.getElementById('val-max-dd').innerHTML = formatNum(m.maxDd, 2, false) + '%';
    document.getElementById('val-vol').innerHTML = formatNum(m.vol, 2, false) + '%';
    document.getElementById('val-sharpe').innerHTML = formatNum(m.sharpe, 3, false);
    document.getElementById('val-rebal-count').innerHTML = m.rebalCount;
    
    // 2. Render NAV Chart
    var navTraces = [
        {{ x: rawDates, y: data.nav, type: 'scatter', mode: 'lines', name: '当前组合', line: {{ color: '#fff', width: 2.5 }} }},
        {{ x: data.rebalDates, y: data.rebalNavs, type: 'scatter', mode: 'markers', name: '再平衡', marker: {{ color: '#e74c3c', size: 6, symbol: 'triangle-up' }} }},
    ];
    assetNames.forEach(function(name) {{
        navTraces.push({{ x: rawDates, y: singleAssetNavs[name], type: 'scatter', mode: 'lines', name: '100% '+name, line: {{ color: sC[name], width: 1.2, dash: 'dot' }} }});
    }});
    Plotly.react('nav_chart', navTraces, {{...plotLayout, yaxis: {{...plotLayout.yaxis, title: '净值' }}}});
    
    // 3. Render Weight Chart
    Plotly.react('weight_chart', data.weightTraces, {{
        ...plotLayout,
        xaxis: {{ gridcolor: 'rgba(255,255,255,0.06)', type: 'date' }}, // no rangeslider
        yaxis: {{...plotLayout.yaxis, title: '占比 (%)', range: [0, 50] }},
        shapes: [
          {{ type: 'line', y0: 15, y1: 15, x0: 0, x1: 1, xref: 'paper', line: {{ dash: 'dash', color: '#e74c3c', width: 1 }} }},
          {{ type: 'line', y0: 35, y1: 35, x0: 0, x1: 1, xref: 'paper', line: {{ dash: 'dash', color: '#e74c3c', width: 1 }} }},
          {{ type: 'line', y0: 25, y1: 25, x0: 0, x1: 1, xref: 'paper', line: {{ dash: 'dash', color: '#888', width: 1 }} }},
        ]
    }});
    
    // 4. Render Value Chart
    Plotly.react('value_chart', data.valueTraces, {{...plotLayout, xaxis: {{ gridcolor: 'rgba(255,255,255,0.06)', type: 'date' }}, yaxis: {{...plotLayout.yaxis, title: '市值 (万元)' }}}});
    
    // 5. Update rebalance table
    document.getElementById('rebal-table-body').innerHTML = data.rebalHtml;
}}

// Initialize
renderStrategy(currentStrat);

// Listen to dropdown change
document.getElementById('stratSelect').addEventListener('change', function(e) {{
    currentStrat = e.target.value;
    renderStrategy(currentStrat);
}});

</script>
</body>
</html>"""
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\\n[OK] Report saved: {output_path}")
'''

content = re.sub(r'def generate_html_report.*?# ─────────────────── Main ───────────────────', new_generate_html_report + '\n\n# ─────────────────── Main ───────────────────', content, flags=re.DOTALL)

# 3. Replace __main__
new_main = '''
# ─────────────────── Main ───────────────────
if __name__ == "__main__":
    navs = fetch_all_data()
    
    # 策略1：阈值触发(15/35%)
    print("\\n正在运行 阈值再平衡 策略...")
    df_thresh, rebal_thresh = run_backtest(navs, rebalance_type="threshold")
    
    # 策略2：年度触发(每年首日)
    print("正在运行 年度再平衡 策略...")
    df_annual, rebal_annual = run_backtest(navs, rebalance_type="annual")
    
    results = {
        "threshold": (df_thresh, rebal_thresh),
        "annual": (df_annual, rebal_annual)
    }
    
    print(f"\\n{'='*60}")
    print("阈值策略:", f"{df_thresh['total_value'].iloc[-1]/10000:.2f} wan,", f"Rebal: {len(rebal_thresh)}")
    print("年度策略:", f"{df_annual['total_value'].iloc[-1]/10000:.2f} wan,", f"Rebal: {len(rebal_annual)}")
    print(f"{'='*60}")
    
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_report.html")
    generate_html_report(results, navs, output_path)
'''

content = re.sub(r'# ─────────────────── Main ───────────────────.*', new_main, content, flags=re.DOTALL)

with open('backtest.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Update complete')
