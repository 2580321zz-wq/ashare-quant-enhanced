from __future__ import annotations

import json
import time
from datetime import datetime, date
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

# A股量化增强版：仅模拟交易，不连接券商、不发送真实订单。
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "quant_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "paper_account.json"
TRADES_FILE = DATA_DIR / "paper_trades.csv"
SETTINGS_FILE = DATA_DIR / "settings.json"
MARKET_FILE = DATA_DIR / "market_data.csv"

st.set_page_config(page_title="A股量化增强版", page_icon="📈", layout="wide")
st.title("📈 A股量化增强版")
st.caption("多因子选股 · 模拟自动交易 · 风险控制 · 历史回测 · 本地持久化")
st.warning(
    "仅供研究和模拟交易。不会连接券商或下真实订单。行情是否实时取决于你导入的数据；"
    "示例数据是合成数据，不代表真实股票。回测结果不代表未来收益。"
)

def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default

def save_json(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def empty_account(initial_cash=100000.0):
    return {
        "initial_cash": float(initial_cash),
        "cash": float(initial_cash),
        "holdings": {},
        "trades": [],
        "paused": True,
        "last_processed_date": "",
        "day_start_value": float(initial_cash),
        "day_key": date.today().isoformat(),
        "last_run": "",
    }

def load_account():
    data = load_json(STATE_FILE, None)
    if not isinstance(data, dict) or "cash" not in data or "holdings" not in data:
        data = empty_account()
        save_json(STATE_FILE, data)
    data.setdefault("trades", [])
    data.setdefault("paused", True)
    data.setdefault("last_processed_date", "")
    data.setdefault("day_start_value", data.get("initial_cash", 100000))
    data.setdefault("day_key", date.today().isoformat())
    data.setdefault("last_run", "")
    return data

def persist_account():
    save_json(STATE_FILE, st.session_state.account)
    pd.DataFrame(st.session_state.account.get("trades", [])).to_csv(
        TRADES_FILE, index=False, encoding="utf-8-sig"
    )

if "account" not in st.session_state:
    st.session_state.account = load_account()
if "backtest_result" not in st.session_state:
    st.session_state.backtest_result = None

# Sidebar settings
with st.sidebar:
    st.header("策略与风控")
    min_score = st.slider("最低选股评分", 0, 100, 65)
    stop_loss_pct = st.slider("止损比例 (%)", 1.0, 15.0, 4.0, 0.5) / 100
    take_profit_pct = st.slider("止盈比例 (%)", 2.0, 40.0, 8.0, 0.5) / 100
    max_hold_days = st.slider("最长持有交易日", 1, 60, 10)
    max_positions = st.slider("最多持仓股票数", 1, 30, 5)
    per_stock_pct = st.slider("单只股票资产上限 (%)", 5, 50, 20)
    max_total_position_pct = st.slider("总持仓上限 (%)", 10, 100, 80)
    daily_loss_pct = st.slider("单日亏损警戒线 (%)", 1.0, 20.0, 3.0, 0.5)
    fee_bps = st.number_input("单边佣金/滑点估算（基点）", 0.0, 100.0, 15.0, 1.0)
    sell_stamp_tax_pct = st.number_input("卖出印花税估算 (%)", 0.0, 1.0, 0.05, 0.01)
    min_price = st.number_input("最低股价（元）", 0.0, 10000.0, 3.0, 0.5)
    max_price = st.number_input("最高股价（元）", 0.1, 10000.0, 300.0, 1.0)
    min_amount = st.number_input("最低成交额（元）", 0.0, 100000000000.0, 50000000.0, 10000000.0, format="%.0f")
    exclude_st = st.checkbox("排除 ST/*ST（依赖导入名称）", True)
    auto_mode = st.checkbox("启用自动模拟交易循环", False)
    interval_seconds = st.number_input("自动扫描间隔（秒）", 30, 3600, 300, 30)
    st.caption("自动循环仅在网页进程持续运行且页面定期刷新时有效；云平台休眠、关闭页面或进程重启时不会后台持续交易。")

with st.sidebar:
    st.divider()
    st.subheader("模拟账户")
    initial_cash_input = st.number_input("新账户初始资金（元）", 1000.0, 100000000.0, 100000.0, 10000.0)

# Data handling
ALIASES = {
    "日期":"date", "交易日期":"date", "股票代码":"code", "代码":"code", "名称":"name",
    "开盘":"open", "开盘价":"open", "最高":"high", "最高价":"high", "最低":"low",
    "最低价":"low", "收盘":"close", "收盘价":"close", "成交量":"volume", "成交额":"amount",
    "换手率":"turnover", "涨跌幅":"pct_change"
}
REQUIRED = ["date", "code", "open", "high", "low", "close", "volume"]

def normalize(df):
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.rename(columns={c: ALIASES[c] for c in list(df.columns) if c in ALIASES})
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError("缺少必要列：" + ", ".join(missing))
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["code"] = df["code"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip().str.zfill(6)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    else:
        # 成交量单位可能是股或手，因此该估算值只适合临时查看，建议提供真实成交额列。
        df["amount"] = df["close"] * df["volume"]
        df["amount_estimated"] = True
    if "name" not in df.columns:
        df["name"] = df["code"]
    df["name"] = df["name"].fillna(df["code"]).astype(str)
    df = df.dropna(subset=REQUIRED)
    df = df[(df["close"] > 0) & (df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["volume"] >= 0)]
    df = df.sort_values(["code", "date"]).drop_duplicates(["code", "date"], keep="last")
    df["is_st"] = df["name"].str.contains(r"\*?ST", case=False, regex=True, na=False)
    return df.reset_index(drop=True)

def make_demo():
    rng = np.random.default_rng(2026)
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=360)
    stocks = [
        ("600000", "演示沪市主板A", 0.00025, .018),
        ("000001", "演示深市主板B", 0.00018, .020),
        ("300001", "演示创业板C", 0.00035, .028),
        ("688001", "演示科创板D", 0.00030, .030),
        ("430001", "演示北交所E", 0.00022, .032),
        ("002001", "演示深市主板F", 0.00010, .022),
        ("301001", "演示创业板G", 0.00020, .027),
        ("600001", "演示沪市主板H", -0.00002, .016),
    ]
    rows = []
    for code, name, drift, vol in stocks:
        rets = rng.normal(drift, vol, len(dates))
        close = 15 * np.exp(np.cumsum(rets))
        op = close * (1 + rng.normal(0, .004, len(dates)))
        high = np.maximum(op, close) * (1 + rng.uniform(0, .012, len(dates)))
        low = np.minimum(op, close) * (1 - rng.uniform(0, .012, len(dates)))
        volume = rng.integers(300_000, 3_000_000, len(dates))
        for i, d in enumerate(dates):
            rows.append({"date": d, "code": code, "name": name, "open": op[i], "high": high[i],
                         "low": low[i], "close": close[i], "volume": volume[i], "amount": volume[i]*close[i]})
    return pd.DataFrame(rows)

st.header("1 · 行情数据")
up = st.file_uploader("上传 A 股历史日线 CSV（建议至少 120 个交易日）", type=["csv"])
col_a, col_b, col_c = st.columns([1, 1, 2])
with col_a:
    use_saved = st.button("载入本地行情文件")
with col_b:
    use_demo = st.button("载入合成演示数据")
with col_c:
    st.caption(f"本地行情路径：{MARKET_FILE.name}（应用目录下 quant_data 文件夹）")

raw = None
if up is not None:
    try:
        raw = normalize(pd.read_csv(up, encoding_errors="replace"))
        raw.to_csv(MARKET_FILE, index=False, encoding="utf-8-sig")
        st.success(f"已导入 {len(raw):,} 行、{raw['code'].nunique()} 只股票，并保存到本地文件。")
    except Exception as e:
        st.error(f"CSV 读取失败：{e}")
elif use_saved or (MARKET_FILE.exists() and not use_demo):
    try:
        raw = normalize(pd.read_csv(MARKET_FILE, encoding_errors="replace"))
        st.info(f"已载入本地行情：{len(raw):,} 行，最新日期 {raw['date'].max().date()}。")
    except Exception as e:
        st.error(f"本地行情文件读取失败：{e}")
elif use_demo:
    raw = normalize(make_demo())
    st.info("当前使用合成演示数据。代码、价格和回测表现均非真实行情。")
elif MARKET_FILE.exists():
    try:
        raw = normalize(pd.read_csv(MARKET_FILE, encoding_errors="replace"))
        st.info(f"自动载入本地行情：{len(raw):,} 行。")
    except Exception as e:
        st.error(f"本地行情读取失败：{e}")
else:
    st.info("请上传真实历史日线 CSV，或点击“载入合成演示数据”测试界面。")

def board_of(code):
    code = str(code).zfill(6)
    if code.startswith(("43", "83", "87", "88", "92")):
        return "北交所"
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("600", "601", "603", "605")):
        return "沪市主板"
    if code.startswith(("000", "001", "002", "003")):
        return "深市主板"
    return "其他/待核验"

def add_indicators(g):
    g = g.sort_values("date").copy()
    close = g["close"]
    g["ma5"] = close.rolling(5).mean()
    g["ma20"] = close.rolling(20).mean()
    g["ma60"] = close.rolling(60).mean()
    g["ret5"] = close.pct_change(5)
    g["ret20"] = close.pct_change(20)
    g["vol20"] = g["volume"].rolling(20).mean()
    g["vol_ratio"] = g["volume"] / g["vol20"].replace(0, np.nan)
    prev_close = close.shift(1)
    tr = pd.concat([(g["high"]-g["low"]), (g["high"]-prev_close).abs(), (g["low"]-prev_close).abs()], axis=1).max(axis=1)
    g["atr_pct"] = tr.rolling(14).mean() / close
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    g["macd"] = ema12 - ema26
    g["macd_signal"] = g["macd"].ewm(span=9, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    g["rsi14"] = 100 - 100/(1+rs)
    trend = (close > g["ma20"]).astype(float)*12 + (g["ma20"] > g["ma60"]).astype(float)*13
    momentum = (g["ret5"].clip(-.10,.10).fillna(0)*100).clip(0,10)*1.5 + (g["ret20"].clip(-.20,.20).fillna(0)*100).clip(0,10)
    volume_score = ((g["vol_ratio"]-.8).clip(0,1.7)/1.7*20)
    macd_score = (g["macd"] > g["macd_signal"]).astype(float)*10
    risk_score = 25 - (g["atr_pct"].fillna(.04).clip(0,.08)/.08*25)
    rsi_score = np.where(g["rsi14"].between(45,70), 10, np.where(g["rsi14"].between(35,80), 5, 0))
    g["score"] = (trend + momentum.clip(0,25) + volume_score + macd_score + risk_score + rsi_score).clip(0,100).round(1)
    g["board"] = g["code"].map(board_of)
    g["signal"] = np.where(
        (g["score"] >= min_score) & (close > g["ma20"]) & (g["ret5"] > 0) &
        (g["vol_ratio"] >= 1.0) & (g["macd"] > g["macd_signal"]),
        "买入候选", np.where(g["score"] >= min_score-10, "观察", "不符合")
    )
    eligible = close.between(min_price, max_price) & (g["amount"].fillna(0) >= min_amount)
    if exclude_st:
        eligible &= ~g["is_st"]
    g["eligible"] = eligible
    g.loc[~eligible, "signal"] = "未通过筛选"
    return g

def trade_cost(notional, side, fee_bps, stamp_pct):
    commission = notional * fee_bps / 10000
    stamp = notional * stamp_pct / 100 if side == "卖出" else 0.0
    return commission + stamp

def account_value(account, prices):
    holdings_value = sum(int(h["qty"]) * float(prices.get(code, h.get("last_price", h["entry_price"])))
                         for code, h in account["holdings"].items())
    return float(account["cash"]) + holdings_value, holdings_value

def add_trade(account, action, code, name, price, qty, amount, fee, reason, trade_date=None):
    account["trades"].append({
        "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "交易日期": str(trade_date or date.today().isoformat()),
        "操作": action, "代码": code, "名称": name, "价格": round(float(price), 4),
        "股数": int(qty), "成交金额": round(float(amount), 2), "费用估算": round(float(fee), 2),
        "原因": reason
    })

def process_simulation(latest, account, params):
    if latest.empty:
        return ["没有可用的最新行情，未执行交易。"]
    latest = latest.copy()
    latest["code"] = latest["code"].astype(str).str.zfill(6)
    prices = latest.set_index("code")["close"].to_dict()
    latest_rows = latest.set_index("code").to_dict("index")
    messages = []
    today = str(pd.to_datetime(latest["date"].max()).date())
    # 防止同一份日线数据重复执行多次买卖；只允许每个行情日期处理一次。
    if account.get("last_processed_date") == today:
        return [f"行情日期 {today} 已处理过，本次跳过，防止重复成交。"]
    value, _ = account_value(account, prices)
    today_key = date.today().isoformat()
    if account.get("day_key") != today_key:
        account["day_key"] = today_key
        account["day_start_value"] = value
    daily_change = value / max(float(account.get("day_start_value", value)), 1.0) - 1
    # 更新持仓价格并检查退出条件。真实市场逐日运行时才有实际意义。
    for code, h in list(account["holdings"].items()):
        row = latest_rows.get(code)
        if row is None:
            continue
        price = float(row["close"])
        h["last_price"] = price
        ret = price / max(float(h["entry_price"]), 1e-9) - 1
        age = max(0, (pd.to_datetime(today) - pd.to_datetime(h.get("entry_date", today))).days)
        reason = None
        if ret <= -params["stop_loss"]:
            reason = "模拟止损"
        elif ret >= params["take_profit"]:
            reason = "模拟止盈"
        elif age >= params["max_hold_days"] * 1.4:
            reason = "超期退出"
        if reason:
            qty = int(h["qty"])
            gross = qty * price
            fee = trade_cost(gross, "卖出", params["fee_bps"], params["stamp_pct"])
            account["cash"] += gross - fee
            add_trade(account, "卖出", code, h["name"], price, qty, gross, fee, reason, today)
            del account["holdings"][code]
            messages.append(f"{code} {reason}：{qty} 股 @ {price:.2f}")
    value, holdings_value = account_value(account, prices)
    daily_change = value / max(float(account.get("day_start_value", value)), 1.0) - 1
    if daily_change <= -params["daily_loss_pct"]:
        messages.append(f"触发单日亏损警戒（{daily_change*100:.2f}%），停止新增模拟仓位。")
        account["last_processed_date"] = today
        account["last_run"] = datetime.now().isoformat(timespec="seconds")
        return messages
    total_limit = value * params["max_total_position_pct"]
    current_exposure = holdings_value
    candidates = latest[latest["signal"].eq("买入候选")].sort_values("score", ascending=False)
    for _, row in candidates.iterrows():
        code = str(row["code"]).zfill(6)
        if code in account["holdings"] or len(account["holdings"]) >= params["max_positions"]:
            continue
        price = float(row["close"])
        if not np.isfinite(price) or price <= 0 or board_of(code) == "其他/待核验":
            continue
        max_one = value * params["per_stock_pct"]
        room_total = max(0.0, total_limit - current_exposure)
        budget = min(max_one, float(account["cash"]), room_total)
        # 佣金预留，按 A 股常见 100 股整手买入；北交所交易单位规则可能不同，保守统一按 100 股。
        qty = int(budget / (price * (1 + params["fee_bps"]/10000)) / 100) * 100
        if qty < 100:
            continue
        gross = qty * price
        fee = trade_cost(gross, "买入", params["fee_bps"], params["stamp_pct"])
        if gross + fee > account["cash"]:
            continue
        account["cash"] -= gross + fee
        account["holdings"][code] = {
            "name": str(row["name"]), "qty": int(qty), "entry_price": price,
            "last_price": price, "entry_date": today, "entry_score": float(row["score"])
        }
        current_exposure += gross
        add_trade(account, "买入", code, row["name"], price, qty, gross, fee, "量化候选自动模拟买入", today)
        messages.append(f"{code} 模拟买入：{qty} 股 @ {price:.2f}，评分 {row['score']:.1f}")
    account["last_processed_date"] = today
    account["last_run"] = datetime.now().isoformat(timespec="seconds")
    if not messages:
        messages.append(f"扫描完成（行情日期 {today}），未发现可执行的候选交易。")
    return messages

def run_backtest(data, params):
    # 单股票独立信号、次日开盘入场的简化回测。不是严格的全市场资金组合模拟。
    trades = []
    for code, grp in data.groupby("code"):
        g = grp.sort_values("date").reset_index(drop=True)
        pos = None
        for i in range(61, len(g)-1):
            row, nxt = g.iloc[i], g.iloc[i+1]
            if pos is None:
                if row["signal"] == "买入候选" and bool(row["eligible"]):
                    entry = float(nxt["open"])
                    if entry <= 0 or not np.isfinite(entry):
                        continue
                    pos = {"entry_i": i+1, "entry_date": nxt["date"], "entry": entry,
                           "stop": entry*(1-params["stop_loss"]), "target": entry*(1+params["take_profit"]),
                           "code": str(code), "name": str(nxt["name"])}
            else:
                age = i - pos["entry_i"] + 1
                exit_price, reason = None, None
                # 保守处理：同日触及止损和止盈时，按止损先发生。
                if float(row["low"]) <= pos["stop"]:
                    exit_price, reason = min(float(row["open"]), pos["stop"]), "止损/跳空"
                elif float(row["high"]) >= pos["target"]:
                    exit_price, reason = pos["target"], "止盈"
                elif age >= params["max_hold_days"]:
                    exit_price, reason = float(row["close"]), "到期退出"
                if exit_price is not None and exit_price > 0:
                    buy_gross = pos["entry"]
                    sell_gross = exit_price
                    buy_cost = params["fee_bps"]/10000
                    sell_cost = params["fee_bps"]/10000 + params["stamp_pct"]/100
                    net = (sell_gross*(1-sell_cost))/(buy_gross*(1+buy_cost))-1
                    trades.append({"代码":pos["code"], "名称":pos["name"],
                                   "买入日期":str(pd.to_datetime(pos["entry_date"]).date()),
                                   "卖出日期":str(pd.to_datetime(row["date"]).date()),
                                   "买入价":round(buy_gross,3), "卖出价":round(sell_gross,3),
                                   "净收益%":round(net*100,2), "退出原因":reason})
                    pos = None
    tdf = pd.DataFrame(trades)
    if tdf.empty:
        return tdf, {}
    r = tdf["净收益%"]/100
    eq = (1+r).cumprod()
    dd = eq/eq.cummax()-1
    wins, losses = r[r>0], r[r<=0]
    stats = {
        "交易次数":len(tdf), "胜率":f"{(r>0).mean()*100:.1f}%",
        "平均单笔净收益":f"{r.mean()*100:.2f}%",
        "累计交易序列收益":f"{(eq.iloc[-1]-1)*100:.2f}%",
        "最大回撤（交易序列）":f"{dd.min()*100:.2f}%",
        "盈亏比":f"{wins.mean()/abs(losses.mean()):.2f}" if len(wins) and len(losses) and losses.mean()!=0 else "样本不足"
    }
    tdf["交易序列净值"] = eq.round(4)
    return tdf, stats

if raw is not None and not raw.empty:
    analyzed = pd.concat([add_indicators(g) for _, g in raw.groupby("code")], ignore_index=True)
    latest = analyzed.sort_values("date").groupby("code", as_index=False).tail(1)
    latest = latest[latest["eligible"]].sort_values("score", ascending=False)
    boards = ["沪市主板", "深市主板", "创业板", "科创板", "北交所"]
    selected_boards = st.multiselect("股票板块范围", boards, default=boards)
    latest = latest[latest["board"].isin(selected_boards)]

    st.header("2 · 量化选股候选池")
    if latest.empty:
        st.info("当前筛选条件下没有候选股票。请调整价格/成交额/评分阈值，或导入更多数据。")
    else:
        show = latest[["date","code","name","board","close","ret5","ret20","vol_ratio","rsi14","macd","score","signal"]].copy()
        show["date"] = show["date"].dt.strftime("%Y-%m-%d")
        show["ret5"] = (show["ret5"]*100).round(2).astype(str)+"%"
        show["ret20"] = (show["ret20"]*100).round(2).astype(str)+"%"
        show["vol_ratio"] = show["vol_ratio"].round(2)
        show["rsi14"] = show["rsi14"].round(1)
        show = show.rename(columns={"date":"日期","code":"代码","name":"名称","board":"板块","close":"收盘价",
                                    "ret5":"5日涨幅","ret20":"20日涨幅","vol_ratio":"量比","rsi14":"RSI14",
                                    "macd":"MACD","score":"评分","signal":"信号"})
        st.dataframe(show, use_container_width=True, hide_index=True)
        st.download_button("下载候选股 CSV", show.to_csv(index=False).encode("utf-8-sig"), "candidates.csv", "text/csv")

    st.header("3 · 模拟账户与自动模拟交易")
    account = st.session_state.account
    latest_prices = latest.set_index("code")["close"].to_dict() if not latest.empty else {}
    total_value, holdings_value = account_value(account, latest_prices)
    day_key = date.today().isoformat()
    if account.get("day_key") != day_key:
        account["day_key"] = day_key
        account["day_start_value"] = total_value
        persist_account()
    daily_return = total_value / max(float(account.get("day_start_value", total_value)), 1) - 1
    m1,m2,m3,m4,m5 = st.columns(5)
    m1.metric("模拟总资产", f"¥{total_value:,.2f}")
    m2.metric("可用现金", f"¥{account['cash']:,.2f}")
    m3.metric("持仓市值", f"¥{holdings_value:,.2f}")
    m4.metric("累计盈亏", f"¥{total_value-float(account['initial_cash']):,.2f}")
    m5.metric("当日变化", f"{daily_return*100:.2f}%")
    if daily_return <= -daily_loss_pct/100:
        st.error("单日亏损警戒线已触发：自动模拟交易应停止新增仓位。")
    if account["holdings"]:
        holding_rows = []
        for code, h in account["holdings"].items():
            px = float(latest_prices.get(code, h.get("last_price", h["entry_price"])))
            holding_rows.append({"代码":code,"名称":h["name"],"板块":board_of(code),"股数":h["qty"],
                                 "成本价":h["entry_price"],"最新价":px,"市值":px*h["qty"],
                                 "浮盈亏":(px/h["entry_price"]-1)*100})
        st.dataframe(pd.DataFrame(holding_rows), use_container_width=True, hide_index=True)
    else:
        st.info("当前没有模拟持仓。")

    ctl1, ctl2, ctl3 = st.columns(3)
    with ctl1:
        if st.button("▶ 执行一轮模拟扫描", type="primary", disabled=latest.empty):
            params = {"stop_loss":stop_loss_pct, "take_profit":take_profit_pct, "max_hold_days":max_hold_days,
                      "max_positions":max_positions, "per_stock_pct":per_stock_pct/100,
                      "max_total_position_pct":max_total_position_pct/100, "daily_loss_pct":daily_loss_pct/100,
                      "fee_bps":fee_bps, "stamp_pct":sell_stamp_tax_pct}
            messages = process_simulation(latest, account, params)
            persist_account()
            for msg in messages:
                st.write("• " + msg)
            st.rerun()
    with ctl2:
        if st.button("⏸ 暂停/恢复自动模拟"):
            account["paused"] = not account.get("paused", True)
            persist_account()
            st.rerun()
    with ctl3:
        if st.button("⚠ 重置模拟账户"):
            st.session_state.account = empty_account(initial_cash_input)
            persist_account()
            st.rerun()

    st.write("自动模拟状态：" + ("已暂停" if account.get("paused", True) else "运行中"))
    st.caption("自动模拟不会在单次扫描中重复处理同一行情日期。若希望按交易日自动交易，需每天导入/更新新行情，并保持应用进程运行。重置账户会清空当前模拟账户和成交记录，请先导出备份。")

    # Auto mode is intentionally conservative: one pass per new data date; rerun periodically.
    if auto_mode and not account.get("paused", True):
        params = {"stop_loss":stop_loss_pct, "take_profit":take_profit_pct, "max_hold_days":max_hold_days,
                  "max_positions":max_positions, "per_stock_pct":per_stock_pct/100,
                  "max_total_position_pct":max_total_position_pct/100, "daily_loss_pct":daily_loss_pct/100,
                  "fee_bps":fee_bps, "stamp_pct":sell_stamp_tax_pct}
        messages = process_simulation(latest, account, params)
        if messages and not (len(messages)==1 and "已处理过" in messages[0]):
            persist_account()
            with st.expander("最近自动扫描结果", expanded=True):
                for msg in messages:
                    st.write("• " + msg)
        time.sleep(0.1)
        st.caption(f"自动扫描模式已开启。页面将在 {int(interval_seconds)} 秒后尝试刷新；请确认部署环境支持持续运行。")
        st.markdown(f"<meta http-equiv='refresh' content='{int(interval_seconds)}'>", unsafe_allow_html=True)

    st.header("4 · 历史回测")
    st.caption("当前回测为逐股票简化模型：信号日后一个交易日开盘入场，按日内高低价检查止盈止损；不等于全市场组合回测。")
    if st.button("运行历史回测"):
        params = {"stop_loss":stop_loss_pct, "take_profit":take_profit_pct, "max_hold_days":max_hold_days,
                  "fee_bps":fee_bps, "stamp_pct":sell_stamp_tax_pct}
        trades, stats = run_backtest(analyzed, params)
        st.session_state.backtest_result = (trades, stats)
    if st.session_state.backtest_result is not None:
        trades, stats = st.session_state.backtest_result
        if trades.empty:
            st.info("没有产生回测交易。可导入更长历史数据或调整评分阈值。")
        else:
            a,b,c = st.columns(3)
            a.metric("交易次数", stats["交易次数"])
            b.metric("胜率", stats["胜率"])
            c.metric("交易序列累计收益", stats["累计交易序列收益"])
            d,e,f = st.columns(3)
            d.metric("平均单笔净收益", stats["平均单笔净收益"])
            e.metric("最大回撤", stats["最大回撤（交易序列）"])
            f.metric("盈亏比", stats["盈亏比"])
            st.dataframe(trades, use_container_width=True, hide_index=True)
            st.download_button("下载回测交易 CSV", trades.to_csv(index=False).encode("utf-8-sig"),
                               "backtest_trades.csv", "text/csv")
    st.header("5 · 成交记录、复盘与备份")
    trade_df = pd.DataFrame(account.get("trades", []))
    if not trade_df.empty:
        st.dataframe(trade_df.iloc[::-1], use_container_width=True, hide_index=True)
        st.download_button("下载模拟成交记录 CSV", trade_df.to_csv(index=False).encode("utf-8-sig"),
                           "paper_trades.csv", "text/csv")
        buy_count = int((trade_df["操作"]=="买入").sum()) if "操作" in trade_df else 0
        sell_count = int((trade_df["操作"]=="卖出").sum()) if "操作" in trade_df else 0
        st.caption(f"累计模拟买入 {buy_count} 笔，卖出 {sell_count} 笔。")
    else:
        st.info("暂无成交记录。")
    backup_json = json.dumps(account, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button("备份模拟账户 JSON", backup_json, "paper_account_backup.json", "application/json")
    restore = st.file_uploader("恢复模拟账户 JSON 备份", type=["json"], key="restore_account")
    if restore is not None and st.button("确认恢复备份"):
        try:
            candidate = json.loads(restore.getvalue().decode("utf-8"))
            if not isinstance(candidate, dict) or "cash" not in candidate or "holdings" not in candidate:
                raise ValueError("备份格式不正确")
            st.session_state.account = candidate
            persist_account()
            st.success("账户备份已恢复。")
            st.rerun()
        except Exception as e:
            st.error(f"恢复失败：{e}")

    st.header("6 · 下载分析数据")
    st.download_button("下载完整指标 CSV", analyzed.to_csv(index=False).encode("utf-8-sig"),
                       "quant_indicators.csv", "text/csv")
else:
    st.info("上传行情数据后，选股、模拟账户和回测模块会显示在这里。")

st.header("7 · 数据格式与规则说明")
st.code(
    "date,code,name,open,high,low,close,volume,amount\n"
    "2025-01-02,600000,示例名称,10.1,10.3,10.0,10.2,1200000,12240000",
    language="csv"
)
st.markdown(
    "- 支持沪深主板、创业板、科创板、北交所代码前缀识别；特殊证券代码请人工核验。\n"
    "- 交易单位默认按 100 股整手处理，北交所及特殊证券规则可能不同，模拟结果需谨慎解释。\n"
    "- 当前本地文件持久化适用于本机或持久化磁盘部署。部分云平台重启/重新部署可能清除本地文件，请定期下载 JSON/CSV 备份。\n"
    "- 若要定时自动模拟，必须持续更新真实行情文件；网页自动刷新不是可靠的后台调度器。"
)
st.caption("A股量化增强版 · 研究与模拟用途 · 不连接券商 · 不保证盈利")
