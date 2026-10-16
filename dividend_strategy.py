# -*- coding: utf-8 -*-
"""
红利国债黄金 策略回测
资产: 红利ETF(510880.SH) 40%, 30年国债ETF(511090.SH) 40%, 黄金ETF(518880.SH) 20%
再平衡: 季度再平衡
"""
import sys
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_LOCAL_TUSHARE = _ROOT / 'tushare'
if _LOCAL_TUSHARE.is_dir() and str(_LOCAL_TUSHARE) not in sys.path:
    sys.path.insert(0, str(_LOCAL_TUSHARE))

import sxsc_tushare as ts

_token = os.environ.get('TUSHARE_TOKEN') or os.environ.get('SXSC_TUSHARE_TOKEN')
if not _token:
    _tk_file = Path.home() / 'jsdata_tk.csv'
    if _tk_file.exists():
        try:
            import pandas as _pd
            _token = str(_pd.read_csv(_tk_file, dtype=str).iloc[0]['token']).strip()
        except Exception:
            pass
if _token:
    ts.set_token(_token)
pro = ts.get_api(env='prd')
import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time, warnings, json
warnings.filterwarnings("ignore")

def fetch_tushare_fund_adjusted(ts_code, start_date, end_date):
    sd = pd.to_datetime(start_date)
    ed = pd.to_datetime(end_date)
    daily_frames = []
    adj_frames = []
    curr_start = sd
    while curr_start <= ed:
        curr_end = curr_start + timedelta(days=1000)
        if curr_end > ed:
            curr_end = ed
        sd_str = curr_start.strftime("%Y%m%d")
        ed_str = curr_end.strftime("%Y%m%d")
        df_d = pro.query("fund_daily", ts_code=ts_code, start_date=sd_str, end_date=ed_str)
        if df_d is not None and not df_d.empty:
            daily_frames.append(df_d)
        df_a = pro.query("fund_adj", ts_code=ts_code, start_date=sd_str, end_date=ed_str)
        if df_a is not None and not df_a.empty:
            adj_frames.append(df_a)
        curr_start = curr_end + timedelta(days=1)
        time.sleep(0.3)
    if not daily_frames:
        return pd.DataFrame()
    df_daily = pd.concat(daily_frames).drop_duplicates(subset="trade_date")
    if adj_frames:
        df_adj = pd.concat(adj_frames).drop_duplicates(subset="trade_date")
        df = pd.merge(df_daily, df_adj[["trade_date", "adj_factor"]], on="trade_date", how="left")
        df["adj_factor"] = df["adj_factor"].fillna(1.0)
    else:
        df = df_daily
        df["adj_factor"] = 1.0
    df = df.sort_values("trade_date").reset_index(drop=True)
    latest_adj = df["adj_factor"].iloc[-1]
    df["close"] = df["close"] * df["adj_factor"] / latest_adj
    return df

# ─────────────────── 参数 ───────────────────
INITIAL_CAPITAL = 1_000_000
TARGET_WEIGHTS = {
    "红利ETF": 0.4,
    "30年国债ETF": 0.4,
    "黄金ETF": 0.2
}

START_DATE = "2006-03-01"
END_DATE   = datetime.now().strftime("%Y-%m-%d")

# ─────────────────── 30年国债数据代理 ───────────────────
def fetch_bond_30y_proxy():
    print("  [30年国债] 步骤1: 获取中债收益率数据 (按年分段) ...")
    yield_frames = []
    y_start = 2006
    y_end = 2023
    for y in range(y_start, y_end + 1):
        sd = f"{y}0101" if y > y_start else "20060301"
        ed = f"{y}1231"
        try:
            df_y = ak.bond_china_yield(start_date=sd, end_date=ed)
            df_y = df_y[df_y.iloc[:, 0].str.contains("国债", na=False)]
            df_y = df_y[["日期", "30年"]].dropna()
            df_y.columns = ["date", "yield_30y"]
            df_y["date"] = pd.to_datetime(df_y["date"])
            df_y["yield_30y"] = pd.to_numeric(df_y["yield_30y"], errors="coerce")
            df_y = df_y.dropna()
            yield_frames.append(df_y)
        except Exception as e:
            print(f"    {y}: failed ({e})")
        time.sleep(0.3)
    
    yields = pd.concat(yield_frames).drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    
    MODIFIED_DURATION = 18.0
    COUPON_RATE = 0.035
    
    nav = [1.0]
    for i in range(1, len(yields)):
        dy = (yields["yield_30y"].iloc[i] - yields["yield_30y"].iloc[i-1]) / 100.0
        days_gap = (yields["date"].iloc[i] - yields["date"].iloc[i-1]).days
        price_ret = -MODIFIED_DURATION * dy + COUPON_RATE * days_gap / 365.0
        nav.append(nav[-1] * (1 + price_ret))
    
    bond_proxy = pd.DataFrame({"date": yields["date"], "nav": nav})
    
    print("  [30年国债] 步骤2: 获取 511090.SH ETF 数据 ...")
    etf_df = fetch_tushare_fund_adjusted("511090.SH", START_DATE, END_DATE)
    if etf_df is not None and not etf_df.empty:
        etf_df["trade_date"] = pd.to_datetime(etf_df["trade_date"])
        etf_df = etf_df.set_index("trade_date").sort_index()
        etf_series = etf_df["close"]
        etf_nav = etf_series / etf_series.iloc[0]
        
        splice_date = etf_series.index[0]
        proxy_at_splice = bond_proxy[bond_proxy["date"] <= splice_date]["nav"].iloc[-1]
        
        etf_part = pd.DataFrame({
            "date": etf_series.index,
            "nav": (etf_nav * proxy_at_splice).values
        })
        etf_part = etf_part[etf_part["date"] > bond_proxy["date"].iloc[-1]]
        bond_proxy = pd.concat([bond_proxy, etf_part]).reset_index(drop=True)
    
    return bond_proxy.set_index("date")["nav"]

# ─────────────────── 获取数据 ───────────────────
def fetch_all_data():
    print("=" * 60)
    print("获取 红利ETF (510880.SH) 数据 ...")
    df_hongli = fetch_tushare_fund_adjusted("510880.SH", START_DATE, END_DATE)
    df_hongli["trade_date"] = pd.to_datetime(df_hongli["trade_date"])
    df_hongli = df_hongli.set_index("trade_date")["close"]

    print("获取 30年国债ETF (511090.SH) 数据及代理序列 ...")
    df_bond = fetch_bond_30y_proxy()

    print("获取 黄金ETF (518880.SH) 数据 ...")
    df_gold = fetch_tushare_fund_adjusted("518880.SH", START_DATE, END_DATE)
    df_gold["trade_date"] = pd.to_datetime(df_gold["trade_date"])
    df_gold = df_gold.set_index("trade_date")["close"]

    navs = pd.DataFrame({
        "红利ETF": df_hongli,
        "30年国债ETF": df_bond,
        "黄金ETF": df_gold
    })
    navs = navs.dropna()
    
    # 归一化
    for col in navs.columns:
        navs[col] = navs[col] / navs[col].iloc[0]

    print(f"{'='*60}")
    print(f"合并后交易日数: {len(navs)}")
    print(f"日期范围: {navs.index[0].date()} ~ {navs.index[-1].date()}")
    print(f"{'='*60}")
    return navs

# ─────────────────── 回测引擎 ───────────────────
def run_backtest(navs: pd.DataFrame):
    asset_names = list(navs.columns)
    dates = navs.index
    
    units = {name: (INITIAL_CAPITAL * TARGET_WEIGHTS[name]) / navs[name].iloc[0] for name in asset_names}
    
    daily_records = []
    rebalance_log = []
    
    for i, date in enumerate(dates):
        values = {name: units[name] * navs[name].iloc[i] for name in asset_names}
        total = sum(values.values())
        weights = {name: values[name] / total for name in asset_names}
        
        record = {"date": date, "total_value": total}
        for name in asset_names:
            record[f"{name}_value"] = values[name]
            record[f"{name}_weight"] = weights[name]
        daily_records.append(record)
        
        if i == 0:
            continue
            
        # 季度再平衡逻辑：判断当前月是否是1,4,7,10月，且上一个交易日不是同一个月
        prev_date = dates[i-1]
        should_rebalance = False
        if date.month != prev_date.month and date.month in [1, 4, 7, 10]:
            should_rebalance = True
            trigger_reason = "季度再平衡"
                
        if not should_rebalance:
            continue
        
        target_values = {name: total * TARGET_WEIGHTS[name] for name in asset_names}
        
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

# ─────────────────── HTML 报告 ───────────────────
def generate_html_report(df, rebal_log, navs, output_path: str):
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
        sa_sharpe = (sa_ann - 2.0) / sa_vol if sa_vol > 0 else 0
        
        single_asset_metrics[name] = {
            "final": sa_final, "total_ret": sa_ret, "annual_ret": sa_ann,
            "max_dd": sa_dd, "vol": sa_vol, "sharpe": sa_sharpe,
        }
        
    final_value = df["total_value"].iloc[-1]
    total_return = (final_value / INITIAL_CAPITAL - 1) * 100
    annual_return = ((final_value / INITIAL_CAPITAL) ** (1 / total_years) - 1) * 100
    cummax = df["total_value"].cummax()
    max_dd = ((df["total_value"] - cummax) / cummax).min() * 100
    daily_ret = df["total_value"].pct_change().dropna()
    annual_vol = daily_ret.std() * np.sqrt(252) * 100
    sharpe = (annual_return - 2.0) / annual_vol if annual_vol > 0 else 0
    
    nav = (df["total_value"] / INITIAL_CAPITAL).tolist()
    rebal_dates = [r["date"].strftime("%Y-%m-%d") for r in rebal_log]
    rebal_navs = []
    for r in rebal_log:
        idx = df[df["date"] == r["date"]].index
        rebal_navs.append(df.loc[idx[0], "total_value"] / INITIAL_CAPITAL if len(idx) > 0 else None)
        
    weight_traces = []
    value_traces = []
    colors = ["#e74c3c", "#3498db", "#f39c12"]
    
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
            tw = TARGET_WEIGHTS[name] * 100
            changes.append(f"{name}: {d['before_weight']*100:.1f}%->{tw:.0f}% ({arrow}{abs(d['change'])/10000:.1f}w)")
        rebal_rows_html += f"<tr><td>{idx_r}</td><td>{r['date'].strftime('%Y-%m-%d')}</td><td>{r['trigger']}</td><td>{r['total_value']/10000:.2f}w</td><td style='text-align:left'>{'<br>'.join(changes)}</td></tr>"

    strategy_metrics_html = f"""<tr style="background:rgba(255,255,255,0.05)"><td style="color:#fff"><b>季度再平衡组合</b></td><td>{final_value/10000:.2f}万</td><td>{total_return:+.2f}%</td><td>{annual_return:+.2f}%</td><td>{max_dd:.2f}%</td><td>{annual_vol:.2f}%</td><td>{sharpe:.3f}</td></tr>"""
    compare_rows_html = strategy_metrics_html
    for name in asset_names:
        m = single_asset_metrics[name]
        compare_rows_html += f"<tr><td>{name}全仓</td><td>{m['final']/10000:.2f}万</td><td>{m['total_ret']:+.2f}%</td><td>{m['annual_ret']:+.2f}%</td><td>{m['max_dd']:.2f}%</td><td>{m['vol']:.2f}%</td><td>{m['sharpe']:.3f}</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>季度再平衡组合回测</title>
<script src="https://cdn.plot.ly/plotly-2.35.0.min.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; background: #0f0f23; color: #e0e0e0; }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
  h1 {{ text-align: center; font-size: 28px; margin: 30px 0 10px; 
        background: linear-gradient(90deg, #e74c3c, #3498db, #f39c12);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
  .subtitle {{ text-align: center; color: #888; margin-bottom: 20px; font-size: 14px; }}
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
  <h1>季度再平衡组合回测 (40/40/20)</h1>
  <p class="subtitle">红利ETF(40%) · 30年国债ETF(40%) · 黄金ETF(20%)  &nbsp;|&nbsp; {dates_str_all[0]} ~ {dates_str_all[-1]}</p>

  <div class="metrics">
    <div class="metric"><div class="label">初始资金</div><div class="value">100万</div></div>
    <div class="metric"><div class="label">期末资产</div><div class="value { 'green' if final_value>1000000 else 'red' }">{(final_value/10000):.2f}万</div></div>
    <div class="metric"><div class="label">总收益率</div><div class="value { 'green' if total_return>0 else 'red' }">{total_return:+.2f}%</div></div>
    <div class="metric"><div class="label">年化收益率</div><div class="value { 'green' if annual_return>0 else 'red' }">{annual_return:+.2f}%</div></div>
    <div class="metric"><div class="label">最大回撤</div><div class="value red">{max_dd:.2f}%</div></div>
    <div class="metric"><div class="label">年化波动率</div><div class="value blue">{annual_vol:.2f}%</div></div>
    <div class="metric"><div class="label">Sharpe 比率</div><div class="value gold">{sharpe:.3f}</div></div>
    <div class="metric"><div class="label">再平衡次数</div><div class="value">{len(rebal_log)}</div></div>
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
    <h2>策略全局对比表</h2>
    <table>
      <tr><th>策略</th><th>期末资产</th><th>总收益率</th><th>年化收益率</th><th>最大回撤</th><th>年化波动率</th><th>Sharpe</th></tr>
      {compare_rows_html}
    </table>
  </div>

  <div class="chart-box">
    <h2>再平衡记录表</h2>
    <table>
      <thead><tr><th>#</th><th>日期</th><th>触发条件</th><th>总资产</th><th>调整详情</th></tr></thead>
      <tbody>{rebal_rows_html}</tbody>
    </table>
  </div>
</div>

<script>
const rawDates = {json.dumps(dates_str_all)};
const nav = {json.dumps(nav)};
const rebalDates = {json.dumps(rebal_dates)};
const rebalNavs = {json.dumps(rebal_navs)};
const assetNames = {json.dumps(asset_names)};
const singleAssetNavs = {json.dumps(single_asset_navs)};
const weightTraces = {json.dumps(weight_traces)};
const valueTraces = {json.dumps(value_traces)};

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

const sC = {{'红利ETF':'#e74c3c','30年国债ETF':'#3498db','黄金ETF':'#f39c12'}};

var navTraces = [
    {{ x: rawDates, y: nav, type: 'scatter', mode: 'lines', name: '当前组合', line: {{ color: '#fff', width: 2.5 }} }},
    {{ x: rebalDates, y: rebalNavs, type: 'scatter', mode: 'markers', name: '再平衡', marker: {{ color: '#e74c3c', size: 6, symbol: 'triangle-up' }} }},
];
assetNames.forEach(function(name) {{
    navTraces.push({{ x: rawDates, y: singleAssetNavs[name], type: 'scatter', mode: 'lines', name: '100% '+name, line: {{ color: sC[name], width: 1.2, dash: 'dot' }} }});
}});
Plotly.react('nav_chart', navTraces, {{...plotLayout, yaxis: {{...plotLayout.yaxis, title: '净值' }}}});

Plotly.react('weight_chart', weightTraces, {{
    ...plotLayout,
    xaxis: {{ gridcolor: 'rgba(255,255,255,0.06)', type: 'date' }},
    yaxis: {{...plotLayout.yaxis, title: '占比 (%)', range: [0, 60] }},
    shapes: [
      {{ type: 'line', y0: 40, y1: 40, x0: 0, x1: 1, xref: 'paper', line: {{ dash: 'dash', color: '#e74c3c', width: 1 }} }},
      {{ type: 'line', y0: 20, y1: 20, x0: 0, x1: 1, xref: 'paper', line: {{ dash: 'dash', color: '#f39c12', width: 1 }} }},
    ]
}});

Plotly.react('value_chart', valueTraces, {{...plotLayout, xaxis: {{ gridcolor: 'rgba(255,255,255,0.06)', type: 'date' }}, yaxis: {{...plotLayout.yaxis, title: '市值 (万元)' }}}});
</script>
</body>
</html>"""
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\\n[OK] Report saved: {output_path}")

if __name__ == "__main__":
    navs = fetch_all_data()
    print("\\n正在运行 季度再平衡 策略...")
    df, rebal = run_backtest(navs)
    
    print(f"\\n{'='*60}")
    print("季度策略:", f"{df['total_value'].iloc[-1]/10000:.2f} wan,", f"Rebal: {len(rebal)}")
    print(f"{'='*60}")
    
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dividend_report.html")
    generate_html_report(df, rebal, navs, output_path)
