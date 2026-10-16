# -*- coding: utf-8 -*-
"""
永久投资组合回测 — 基于 finshare + akshare
资产: 30年国债, 黄金, 沪深300, 货币基金(年化2%)
起始: 2006-03-01  结束: 2026-04-22
初始资金100万, 各25%; 任一资产到15%或35%触发再平衡

数据来源:
  30年国债: akshare 30年收益率→久期模型 (2006.3-2023.6) + finshare 511090 ETF (2023.6+)
  黄金: COMEX XAU USD × 汇率→RMB (2006.3-2008.1)
        + 上海金AU0期货 (2008.1-2013.7)
        + finshare 518880 ETF (2013.7+)
  沪深300: akshare 000300指数 (2006.3-2012.5) + finshare 510300 ETF (2012.5+)
  货币基金: 固定年化2%
"""
import os
import sys
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
import os, json, time, warnings
warnings.filterwarnings("ignore")

def fetch_tushare_fund_adjusted(ts_code, start_date, end_date):
    """
    分块获取 Tushare 的 fund_daily 和 fund_adj，计算前复权价格
    解决单次查询只有 1200-2000 行的限量问题
    """
    sd = pd.to_datetime(start_date)
    ed = pd.to_datetime(end_date)
    
    daily_frames = []
    adj_frames = []
    
    curr_start = sd
    while curr_start <= ed:
        curr_end = curr_start + timedelta(days=1000) # 按 ~3 年分块
        if curr_end > ed:
            curr_end = ed
            
        sd_str = curr_start.strftime("%Y%m%d")
        ed_str = curr_end.strftime("%Y%m%d")
        
        # 获取基础日线
        df_d = pro.query("fund_daily", ts_code=ts_code, start_date=sd_str, end_date=ed_str)
        if df_d is not None and not df_d.empty:
            daily_frames.append(df_d)
            
        # 获取复权因子
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
        
    # 计算复权收盘价: close * adj_factor / 最新adj_factor (前复权)
    df = df.sort_values("trade_date").reset_index(drop=True)
    latest_adj = df["adj_factor"].iloc[-1]
    df["close"] = df["close"] * df["adj_factor"] / latest_adj
    
    return df

# ─────────────────── 参数 ───────────────────
INITIAL_CAPITAL = 1_000_000
TARGET_WEIGHT = 0.25
LOWER_BOUND = 0.15
UPPER_BOUND = 0.35
MONEY_FUND_ANNUAL_RATE = 0.02

START_DATE = "2006-03-01"
END_DATE   = datetime.now().strftime("%Y-%m-%d")

# ─────────────────── 30年国债数据 ───────────────────
def fetch_bond_30y_proxy():
    """
    获取30年国债代理净值序列:
    2006.3-2023.6: 中债30年国债到期收益率 → 久期模型
    2023.6+: 511090.SH ETF 前复权
    """
    print("  [30年国债] 步骤1: 获取中债收益率数据 (按年分段) ...")
    # bond_china_yield 限制: start/end间距 < 1年, 分段获取
    yield_frames = []
    y_start = 2006
    y_end = 2023
    for y in range(y_start, y_end + 1):
        sd = f"{y}0101" if y > y_start else "20060301"
        ed = f"{y}1231"
        df_y = None
        for attempt in range(5):
            try:
                df_y = ak.bond_china_yield(start_date=sd, end_date=ed)
                break
            except Exception as e:
                print(f"    {y} (attempt {attempt+1}/5) failed: {e}")
                if attempt == 4:
                    raise e
                time.sleep(1.0)
        try:
            df_y = df_y[df_y.iloc[:, 0].str.contains("国债", na=False)]
            df_y = df_y[["日期", "30年"]].dropna()
            df_y.columns = ["date", "yield_30y"]
            df_y["date"] = pd.to_datetime(df_y["date"])
            df_y["yield_30y"] = pd.to_numeric(df_y["yield_30y"], errors="coerce")
            df_y = df_y.dropna()
            yield_frames.append(df_y)
            print(f"    {y}: {len(df_y)} rows")
        except Exception as e:
            print(f"    {y}: processing failed ({e})")
            raise e
        time.sleep(0.3)
    
    yields = pd.concat(yield_frames).drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    print(f"  [30年国债] 收益率数据: {yields['date'].iloc[0].date()} ~ {yields['date'].iloc[-1].date()}, {len(yields)} rows")
    
    # 久期模型: 每日价格回报 ≈ -修正久期 × Δ收益率 + 票息/365
    MODIFIED_DURATION = 18.0
    COUPON_RATE = 0.035  # 平均票息约3.5%
    
    nav = [1.0]
    for i in range(1, len(yields)):
        dy = (yields["yield_30y"].iloc[i] - yields["yield_30y"].iloc[i-1]) / 100.0
        days_gap = (yields["date"].iloc[i] - yields["date"].iloc[i-1]).days
        price_ret = -MODIFIED_DURATION * dy + COUPON_RATE * days_gap / 365.0
        nav.append(nav[-1] * (1 + price_ret))
    
    bond_proxy = pd.DataFrame({"date": yields["date"], "nav": nav})
    
    # 拼接 ETF 真实数据 (2023.6+)
    print("  [30年国债] 步骤2: 获取 511090.SH ETF 数据 ...")
    etf_df = fetch_tushare_fund_adjusted("511090.SH", START_DATE, END_DATE)
    if etf_df is not None and not etf_df.empty:
        etf_df["trade_date"] = pd.to_datetime(etf_df["trade_date"])
        etf_df = etf_df.set_index("trade_date").sort_index()
        etf_series = etf_df["close"]
        etf_nav = etf_series / etf_series.iloc[0]  # 归一化
        
        # 找拼接点: proxy 最后一天在 ETF 数据范围内的日期
        splice_date = etf_series.index[0]
        proxy_at_splice = bond_proxy[bond_proxy["date"] <= splice_date]["nav"].iloc[-1]
        
        # 校准: ETF 净值 * (proxy在拼接点的值)
        etf_part = pd.DataFrame({
            "date": etf_series.index,
            "nav": (etf_nav * proxy_at_splice).values
        })
        etf_part = etf_part[etf_part["date"] > bond_proxy["date"].iloc[-1]]
        bond_proxy = pd.concat([bond_proxy, etf_part]).reset_index(drop=True)
    
    print(f"  [30年国债] 完成: {bond_proxy['date'].iloc[0].date()} ~ {bond_proxy['date'].iloc[-1].date()}, {len(bond_proxy)} rows")
    return bond_proxy.set_index("date")["nav"]


# ─────────────────── 黄金数据 ───────────────────
def fetch_gold_proxy():
    """
    获取黄金代理净值序列:
    2006.3-2008.1: COMEX XAU (USD) × USD/CNY 汇率 → RMB金价
    2008.1-2013.7: 上海黄金期货 AU0 (已是人民币/克)
    2013.7+: 518880.SH 黄金ETF 前复权
    """
    # ── Part 1: COMEX 国际金价 (USD) 2006-2008 ──
    print("  [黄金] 步骤1: 获取COMEX XAU国际金价 (USD) ...")
    try:
        xau = ak.futures_foreign_hist(symbol="XAU")
        xau["date"] = pd.to_datetime(xau["date"])
        xau = xau[["date", "close"]].dropna().sort_values("date").reset_index(drop=True)
        xau.columns = ["date", "gold_usd"]
        print(f"    XAU: {xau['date'].iloc[0].date()} ~ {xau['date'].iloc[-1].date()}, {len(xau)} rows")
    except Exception as e:
        print(f"    XAU failed: {e}")
        xau = pd.DataFrame(columns=["date", "gold_usd"])
    
    # 获取汇率
    print("  [黄金] 步骤2: 获取 USD/CNY 汇率 ...")
    fx_frames = []
    for y in range(2006, 2009):
        try:
            sd = f"{y}0101" if y > 2006 else "20060301"
            ed = f"{y}1231"
            fx_y = ak.currency_boc_sina(symbol="美元", start_date=sd, end_date=ed)
            fx_y = fx_y.iloc[:, [0, 4]]  # 日期, 中间价
            fx_y.columns = ["date", "usdcny"]
            fx_y["date"] = pd.to_datetime(fx_y["date"])
            fx_y["usdcny"] = pd.to_numeric(fx_y["usdcny"], errors="coerce") / 100.0  # 分→元
            fx_y = fx_y.dropna()
            fx_frames.append(fx_y)
            print(f"    FX {y}: {len(fx_y)} rows")
        except Exception as e:
            print(f"    FX {y}: failed ({e})")
        time.sleep(0.3)
    
    if fx_frames:
        fx = pd.concat(fx_frames).drop_duplicates(subset="date").sort_values("date")
        # 合并金价与汇率
        gold_usd_part = pd.merge(xau, fx, on="date", how="inner")
        gold_usd_part["gold_cny"] = gold_usd_part["gold_usd"] * gold_usd_part["usdcny"]
        # 转换为人民币/克 (1盎司=31.1035克)
        gold_usd_part["gold_cny_per_g"] = gold_usd_part["gold_cny"] / 31.1035
        part1 = gold_usd_part[["date", "gold_cny_per_g"]].copy()
        part1.columns = ["date", "price"]
    else:
        part1 = pd.DataFrame(columns=["date", "price"])
    
    # ── Part 2: 上海黄金期货 AU0 (2008+) ──
    print("  [黄金] 步骤3: 获取上海黄金期货 AU.SHF ...")
    try:
        au0 = pro.query("fut_daily", ts_code="AU.SHF", start_date="20080101", end_date="20130729")
        au0["trade_date"] = pd.to_datetime(au0["trade_date"])
        au0 = au0[["trade_date", "close"]].dropna().sort_values("trade_date").reset_index(drop=True)
        au0.columns = ["date", "price"]
        print(f"    AU0: {au0['date'].iloc[0].date()} ~ {au0['date'].iloc[-1].date()}, {len(au0)} rows")
    except Exception as e:
        print(f"    AU0 failed: {e}")
        au0 = pd.DataFrame(columns=["date", "price"])
    
    # ── Part 3: 黄金ETF 518880 (2013.7+) ──
    print("  [黄金] 步骤4: 获取 518880.SH 黄金ETF ...")
    etf_df = fetch_tushare_fund_adjusted("518880.SH", START_DATE, END_DATE)
    if etf_df is not None and not etf_df.empty:
        etf_df["trade_date"] = pd.to_datetime(etf_df["trade_date"])
        etf_df = etf_df.set_index("trade_date").sort_index()
        part3 = pd.DataFrame({"date": etf_df.index, "price": etf_df["close"].values})
    else:
        part3 = pd.DataFrame(columns=["date", "price"])
    
    # ── 拼接: 用比例因子对齐 ──
    segments = []
    
    # Part1: COMEX金价 (截断到AU0开始之前)
    if not part1.empty and not au0.empty:
        part1 = part1[part1["date"] < au0["date"].iloc[0]]
    if not part1.empty:
        segments.append(part1)
    
    # Part2: AU0 期货
    if not au0.empty:
        if segments:
            prev = segments[-1]
            scale = prev["price"].iloc[-1] / au0["price"].iloc[0]
            au0_scaled = au0.copy()
            au0_scaled["price"] = au0_scaled["price"] * scale
        else:
            au0_scaled = au0
        segments.append(au0_scaled)
    
    # Part3: ETF
    if not part3.empty:
        if segments:
            prev = pd.concat(segments)
            last_before_etf = prev[prev["date"] < part3["date"].iloc[0]]
            if not last_before_etf.empty:
                scale = last_before_etf["price"].iloc[-1] / part3["price"].iloc[0]
                part3_scaled = part3.copy()
                part3_scaled["price"] = part3_scaled["price"] * scale
            else:
                part3_scaled = part3
        else:
            part3_scaled = part3
        segments.append(part3_scaled)
    
    gold_all = pd.concat(segments).drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    gold_nav = gold_all["price"] / gold_all["price"].iloc[0]
    result = pd.DataFrame({"date": gold_all["date"], "nav": gold_nav.values})
    
    print(f"  [黄金] 完成: {result['date'].iloc[0].date()} ~ {result['date'].iloc[-1].date()}, {len(result)} rows")
    return result.set_index("date")["nav"]


# ─────────────────── 沪深300数据 ───────────────────
def fetch_csi300_proxy():
    """
    沪深300代理净值:
    2006.3-2012.5: 沪深300指数 (000300)
    2012.5+: 510300.SH ETF 前复权
    """
    # Part 1: 沪深300指数
    print("  [沪深300] 步骤1: 获取沪深300指数 ...")
    idx = pro.query("index_daily", ts_code="000300.SH", start_date="20060301", end_date="20120601")
    idx = idx[["trade_date", "close"]].copy()
    idx.columns = ["date", "close"]
    idx["date"] = pd.to_datetime(idx["date"])
    idx["close"] = pd.to_numeric(idx["close"], errors="coerce")
    idx = idx.dropna().sort_values("date").reset_index(drop=True)
    print(f"    Index: {idx['date'].iloc[0].date()} ~ {idx['date'].iloc[-1].date()}, {len(idx)} rows")
    
    # Part 2: ETF
    print("  [沪深300] 步骤2: 获取 510300.SH ETF ...")
    etf_df = fetch_tushare_fund_adjusted("510300.SH", START_DATE, END_DATE)
    etf_df["trade_date"] = pd.to_datetime(etf_df["trade_date"])
    etf_df = etf_df.set_index("trade_date").sort_index()
    part2 = pd.DataFrame({"date": etf_df.index, "close": etf_df["close"].values})
    
    # 拼接: 截断 index 到 ETF开始之前, 用比例因子对齐
    idx = idx[idx["date"] < part2["date"].iloc[0]]
    scale = idx["close"].iloc[-1] / part2["close"].iloc[0]
    part2_scaled = part2.copy()
    part2_scaled["close"] = part2_scaled["close"] * scale
    
    csi_all = pd.concat([idx, part2_scaled]).drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    csi_nav = csi_all["close"] / csi_all["close"].iloc[0]
    result = pd.DataFrame({"date": csi_all["date"], "nav": csi_nav.values})
    
    print(f"  [沪深300] 完成: {result['date'].iloc[0].date()} ~ {result['date'].iloc[-1].date()}, {len(result)} rows")
    return result.set_index("date")["nav"]


# ─────────────────── 合并所有资产数据 ───────────────────
def fetch_all_data():
    """获取四大资产净值序列, 合并到同一日期轴"""
    print("=" * 60)
    print("获取 30年国债 数据 ...")
    bond = fetch_bond_30y_proxy()
    print()
    
    print("获取 黄金 数据 ...")
    gold = fetch_gold_proxy()
    print()
    
    print("获取 沪深300 数据 ...")
    csi = fetch_csi300_proxy()
    print()
    
    # 合并
    navs = pd.DataFrame({
        "30年国债": bond,
        "黄金": gold,
        "沪深300": csi,
    })
    navs = navs.dropna()  # 取交集日期
    
    # 货币基金: 按日历日复利
    dates = navs.index
    money_nav = [1.0]
    for i in range(1, len(dates)):
        days = (dates[i] - dates[i-1]).days
        r = (1 + MONEY_FUND_ANNUAL_RATE) ** (days / 365.0) - 1
        money_nav.append(money_nav[-1] * (1 + r))
    navs["货币基金"] = money_nav
    
    print(f"{'='*60}")
    print(f"合并后交易日数: {len(navs)}")
    print(f"日期范围: {navs.index[0].date()} ~ {navs.index[-1].date()}")
    for col in navs.columns:
        print(f"  {col}: 净值 {navs[col].iloc[0]:.4f} → {navs[col].iloc[-1]:.4f}")
    print(f"{'='*60}")
    
    return navs



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

# ─────────────────── HTML 报告 ───────────────────

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
<script src="https://cdn.bootcdn.net/ajax/libs/plotly.js/2.35.0/plotly.min.js"></script>
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
