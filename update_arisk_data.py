#!/usr/bin/env python3
"""update_arisk_data.py — 每日收盘后生成 arisk_data.json

由 launchd/cron 在交易日收盘后调用。
所有 section 独立 try/except；某段失败时复用旧 JSON 对应字段（标记 stale），整体不退出。

输出结构：除 generated_at / generated_date 两个元信息外，每个数据字段统一为
    {"value": ..., "source": "央行", "date": "2026-09-25", "is_estimate": false, "stale": false}
  · source      实际命中的那一级数据源（中文短名）
  · date        数据本身的日期（不是抓取时间）；月频为 YYYY-MM
  · is_estimate 是否为推算/估算值
  · stale       本次抓取失败、复用了旧值时为 true（date 保留旧值原来的日期）
  · note        可选，口径补充说明
"""
import json, os, sys, time, traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

OUT_PATH = os.environ.get('ARISK_OUT') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'arisk_data.json')
MX_KEY = os.environ.get('MX_APIKEY', '')

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ── 老 JSON 兜底 ─────────────────────────────────────────────
def load_prev():
    try:
        with open(OUT_PATH) as f: return json.load(f)
    except Exception: return {}
PREV = load_prev()

def field(value, source, date, is_estimate=False, stale=False, note=None):
    """统一的数据字段包装。date 为数据本身的日期（不是抓取时间）。"""
    out = {"value": value, "source": source, "date": date,
           "is_estimate": bool(is_estimate), "stale": bool(stale)}
    if note: out["note"] = note
    return out

def _is_wrapped(v):
    return isinstance(v, dict) and 'value' in v and 'stale' in v

def prev_value(key, legacy_key=None):
    """旧 JSON 中某字段的 value（兼容升级前未包装的旧结构 / 旧字段名）"""
    v = PREV.get(key)
    if v is None and legacy_key:
        v = PREV.get(legacy_key)
    return v.get('value') if _is_wrapped(v) else v

def _legacy_date(v):
    """从升级前的旧结构值里尽量找回数据日期（找不到返回 None）"""
    if isinstance(v, dict):
        return v.get('date') or v.get('latest_date')
    if isinstance(v, list) and v and isinstance(v[-1], dict):
        return v[-1].get('date')
    return None

def fallback(key, legacy_key=None):
    """本次抓取失败 → 复用旧值，stale=True，保留旧值原来的 date/source。
    legacy_key：字段改名前的旧名，旧 JSON 里只有旧名时从它迁移。"""
    v = PREV.get(key)
    if v is None and legacy_key:
        v = PREV.get(legacy_key)
    if v is None or (_is_wrapped(v) and v.get('value') is None):
        log(f"  ✗ {key} 无旧值可复用")
        return field(None, None, None, stale=True)
    out = dict(v) if _is_wrapped(v) else field(v, '旧版缓存', None)   # 旧结构：来源未知
    if not out.get('date'):
        out['date'] = _legacy_date(out['value'])
    out['stale'] = True
    if key == 'dividend_lowvol100' and isinstance(out['value'], dict):
        out['value'] = {**out['value'], 'current': False}
    log(f"  ↩ {key} 复用旧值（stale，数据停留在 {out.get('date')}）")
    return out

_DF_CACHE = {}
def _cached_df(key, loader):
    if key not in _DF_CACHE:
        _DF_CACHE[key] = loader()
    return _DF_CACHE[key]

def _pe300_df():
    import akshare as ak
    return _cached_df('pe300', lambda: ak.stock_index_pe_lg(symbol='沪深300'))

def _bond_df():
    import akshare as ak
    return _cached_df('bond', lambda: ak.bond_zh_us_rate())

def _bond_col(df):
    return next(c for c in df.columns if '中国' in c and '10年' in c and '差' not in c)

# ── 1. 社融存量同比 ──────────────────────────────────────────
# 主源：央行（PBoC）官网『社会融资规模存量统计表』，其“增速（%）”列即社融存量同比。
#       央行口径、最权威，每月约15日发布上月数据，比商务部镜像（AKShare shrzgm）更及时。
# 回退：① 东财妙想（可选，需 MX_APIKEY）② 商务部镜像增量累计（旧法，口径偏高约1pp且滞后）
#       ③ M2 同比（IC≈0，占位）。每一级都写入对应的 source。
TSF_BASELINE_201412 = 1228600   # 122.86 万亿 = 1,228,600 亿（央行 2015-01 货政报告，旧法基准）

PBOC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120 Safari/537.36")
PBOC_HOST = "https://www.pbc.gov.cn"

def _pboc_decode(b):
    for enc in ("utf-8", "gbk", "gb18030"):
        try: return b.decode(enc)
        except Exception: continue
    return b.decode("utf-8", "ignore")

def _pboc_cells(row_html):
    import re
    cs = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S | re.I)
    return [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").replace("\xa0", " ").strip()
            for c in cs]

def _pboc_tsf_index_url(year):
    """某年『社会融资规模』栏目页 URL。当年是 {year}ntjsj/shrzgm/，往年会被归档到数字 ID 路径
    （如 2025 → 5570903/5570885/），故从「统计数据」总索引按「YYYY年统计数据」→「社会融资规模」查找。"""
    import re, requests
    h = {"User-Agent": PBOC_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    base = f"{PBOC_HOST}/diaochatongjisi/116219/116319/"
    direct = f"{base}{year}ntjsj/shrzgm/index.html"
    try:
        top = _pboc_decode(requests.get(base + "index.html", headers=h, timeout=20).content)
        m = re.search(r"href=['\"]([^'\"]+)['\"][^>]*>\s*" + str(year) + r"年统计数据", top)
        if m:
            ypage = _pboc_decode(requests.get(PBOC_HOST + m.group(1), headers=h, timeout=20).content)
            m2 = re.search(r"href=['\"]([^'\"]+/index\.html)['\"][^>]*>\s*社会融资规模\s*<", ypage)
            if m2:
                return PBOC_HOST + m2.group(1)
    except Exception as e:
        log(f"  · PBoC {year} 栏目发现失败，改用默认路径: {e}")
    return direct

def pboc_year_tsf(year):
    """抓央行某年『社会融资规模存量统计表』，返回 [{'m':'YY-MM','g':同比,'s':'pboc'}]，仅含已发布月份。"""
    import re, requests
    h = {"User-Agent": PBOC_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    idx = _pboc_decode(requests.get(_pboc_tsf_index_url(year), headers=h, timeout=20).content)
    pos = idx.find("社会融资规模存量统计表")          # 标签后第一个 attachDir htm 即该表
    if pos < 0:
        raise RuntimeError("年度索引未见『社会融资规模存量统计表』")
    m = re.search(r"/diaochatongjisi/attachDir/\d{4}/\d{2}/\d+\.htm", idx[pos:pos + 600])
    if not m:
        raise RuntimeError("未找到存量统计表 htm 链接")
    tbl = _pboc_decode(requests.get(PBOC_HOST + m.group(0), headers=h, timeout=20).content)
    months = re.findall(r"(20\d\d)\.(\d{1,2})", tbl)          # 表头月份，按序
    total = None                                              # 合计行：首格含“社会融资规模存量”+“AFRE”
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S | re.I):
        c = _pboc_cells(row)
        if c and c[0].startswith("社会融资规模存量") and "AFRE" in c[0]:
            total = c; break
    if not total:
        raise RuntimeError("未找到社会融资规模存量合计行")
    pairs = list(zip(total[1::2], total[2::2]))               # (存量, 增速) ×12
    out = []
    for (yy, mm), (_stock, yoy) in zip(months, pairs):
        try: g = float(yoy)
        except Exception: continue
        if 0 < g < 30:
            out.append({"m": f"{yy[2:]}-{int(mm):02d}", "g": round(g, 2), "s": "pboc"})
    return out

def _merge_pboc(fresh):
    """新抓的央行月份并入历史中已是央行口径(s=pboc)的月份，按月去重排序取后12个。
    刻意不混入旧累计法的月份，避免在口径接缝处产生虚假的一阶导跳变。"""
    cached = {e['m']: e for e in (prev_value('credit_yoy', 'm2_monthly') or [])
              if isinstance(e, dict) and e.get('s') == 'pboc'}
    for e in fresh:
        cached[e['m']] = e
    return sorted(cached.values(), key=lambda e: e['m'])[-12:]

def _ym(m):
    """'26-08' → '2026-08'"""
    return f"20{m[:2]}-{m[3:5]}"

# 东财妙想（可选，需 MX_APIKEY）：与 proxy.py /mx 相同的接口与请求体，
# 解析逻辑与页面原 parseMXMonthly 一致（dataTableDTOList[0].table 的 headName + 首个指标列）。
MX_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"

def mx_query(query):
    import requests
    r = requests.post(MX_URL, json={"toolQuery": query},
                      headers={"Content-Type": "application/json", "apikey": MX_KEY}, timeout=30)
    r.raise_for_status()
    return r.json()

def parse_mx_monthly(result):
    """妙想查询结果 → [{'m':'YY-MM','g':同比,'s':'mx'}]（按月升序，去重）"""
    dto_list = (((result or {}).get('data') or {}).get('data') or {}) \
        .get('searchDataResultDTO', {}).get('dataTableDTOList') or []
    if not dto_list:
        return []
    dto = dto_list[0]
    table = dto.get('table') or {}
    heads = table.get('headName') or []
    order = dto.get('indicatorOrder') or []
    key = str(order[0]) if order else next((k for k in table if k != 'headName'), '')
    vals = table.get(key) or []
    out = {}
    for h, v in zip(heads, vals):
        raw = str(h)[:10]
        try:
            y, m = int(raw[:4]), int(raw[5:7])
            g = float(v)
        except Exception:
            continue
        if 0 < g < 30:
            out[f"{str(y)[2:]}-{m:02d}"] = {"m": f"{str(y)[2:]}-{m:02d}", "g": round(g, 2), "s": "mx"}
    return [out[k] for k in sorted(out)]

def fetch_credit_yoy():
    # ── 主源：央行官方社融存量同比（最权威、最及时）──
    try:
        from datetime import date
        yr = date.today().year
        # 上年 + 当年各自独立抓：1 月当年表尚未发布时不至于整级失败，图上也有完整 12 个月
        rows = []
        for y in (yr - 1, yr):
            try: rows += pboc_year_tsf(y)
            except Exception as e: log(f"  · PBoC {y} 年表不可得: {e}")
        if not rows:
            raise RuntimeError("PBoC 上年与当年表均不可得")
        rows = _merge_pboc(rows)
        if len(rows) >= 3:
            log(f"  ✓ 社融存量同比[央行口径] ({len(rows)} 月) 最新 {rows[-1]}  · PBoC 直连")
            return field(rows, '央行', _ym(rows[-1]['m']))
        log(f"  ✗ PBoC 社融存量同比 点数不足: {len(rows)} 月")
    except Exception as e:
        log(f"  ✗ PBoC 社融存量同比 失败: {e}")
        traceback.print_exc()
    # ── 回退1：东财妙想（仅当配置了 MX_APIKEY）──
    if MX_KEY:
        try:
            rows = parse_mx_monthly(mx_query('社融存量同比增速最近12个月'))
            if len(rows) >= 6:
                log(f"  ⚠ 社融存量同比[回退·妙想] ({len(rows)} 月) 最新 {rows[-1]}")
                return field(rows[-12:], '妙想', _ym(rows[-1]['m']))
            log(f"  ✗ 妙想 社融存量同比 点数不足: {len(rows)} 月")
        except Exception as e:
            log(f"  ✗ 妙想 社融存量同比 失败: {e}")
    else:
        log("  · 妙想 MX_APIKEY 未配置，跳过")
    # ── 回退2：商务部镜像 社融增量累计（旧法，口径偏高约1pp且滞后）──
    try:
        import akshare as ak
        df = ak.macro_china_shrzgm()
        df = df.copy()
        df['月份'] = df['月份'].astype(str)
        df = df[df['月份'].str.match(r'^\d{6}$')].sort_values('月份').reset_index(drop=True)
        # 累计存量 = 基准 + cumsum 增量
        df['增量'] = df['社会融资规模增量'].astype(float)
        df['存量'] = TSF_BASELINE_201412 + df['增量'].cumsum()
        # 12 月同比
        df['stock_lag12'] = df['存量'].shift(12)
        df['yoy'] = (df['存量'] / df['stock_lag12'] - 1) * 100
        df = df.dropna(subset=['yoy'])
        out = []
        for _, r in df.tail(14).iterrows():
            ym = r['月份']
            out.append({"m": f"{ym[2:4]}-{ym[4:6]}", "g": round(float(r['yoy']), 2)})
        if len(out) >= 6:
            log(f"  ⚠ 社融存量同比[回退·商务部镜像累计] ({len(out)} 月) 最新 {out[-1]}  · 口径偏高~1pp且滞后")
            return field(out[-12:], '商务部镜像累计', _ym(out[-1]['m']),
                         note='增量累计推算的存量同比，口径偏高约1pp且滞后')
        log(f"  ✗ 社融存量同比[回退·累计] 数据不足: {len(out)} 月")
    except Exception as e:
        log(f"  ✗ 社融存量同比[回退·累计] 失败: {e}")
        traceback.print_exc()
    # 回退：M2 同比（IC≈0，仅占位）
    try:
        import akshare as ak
        df = ak.macro_china_money_supply()
        date_col = next(c for c in df.columns if '月' in c or 'date' in c.lower())
        val_col = next(c for c in df.columns if 'M2' in c and '同比' in c)
        df = df.sort_values(date_col)
        out = []
        for _, row in df.tail(14).iterrows():
            raw = str(row[date_col]).strip()
            digits = ''.join(c for c in raw if c.isdigit())
            if len(digits) < 6: continue
            yy, mm = digits[2:4], digits[4:6]
            try: g = round(float(str(row[val_col]).replace('%','')), 2)
            except Exception: continue
            if 0 < g < 30: out.append({"m": f"{yy}-{mm}", "g": g})
        if len(out) >= 6:
            log(f"  ⚠ M2 同比兜底（信号 IC≈0）最新 {out[-1]}")
            return field(out[-12:], 'M2兜底', _ym(out[-1]['m']),
                         note='M2 同比替代社融，信号无效（IC≈0）')
    except Exception as e:
        log(f"  ✗ M2 也失败: {e}")
    return fallback('credit_yoy', 'm2_monthly')

# ── 2. 10Y 国债 ──────────────────────────────────────────
def fetch_bond10y():
    try:
        df = _bond_df()
        col = _bond_col(df)
        df = df[['日期', col]].dropna().sort_values('日期').tail(30)
        hist = [{"d": f"{d.month}/{d.day}", "v": round(float(v), 4)}
                for d, v in zip(__import__('pandas').to_datetime(df['日期']), df[col])]
        latest = round(float(df[col].iloc[-1]), 2)
        d = str(df['日期'].iloc[-1])[:10]
        log(f"  ✓ bond10y latest={latest}% ({len(hist)} 天) 日期 {d}")
        return field({"latest": latest, "hist": hist}, '东财', d)
    except Exception as e:
        log(f"  ✗ bond10y 失败: {e}")
        return fallback('bond10y')

# ── 3. 沪深300 PE ─────────────────────────────────────────
def fetch_pe_300():
    try:
        df = _pe300_df()
        pe = round(float(df.iloc[-1]['滚动市盈率']), 2)
        d = str(df.iloc[-1]['日期'])[:10]
        log(f"  ✓ pe_300 = {pe}（{d}）")
        return field(pe, '乐咕乐股', d, note='沪深300 滚动市盈率(TTM)')
    except Exception as e:
        log(f"  ✗ pe_300 失败: {e}")
        return fallback('pe_300')

# ── 3b. ERP 近 5 年真实分位（仅展示，不参与打分）─────────────────
# ERP = 1/PE(沪深300 TTM) − r10Y。乐咕乐股的沪深300 历史 PE 为「月末点 + 最新一日」，
# 故序列为月度；每个 PE 日期向前对齐最近一个有 10Y 国债数据的交易日。
def fetch_erp_history():
    try:
        import pandas as pd
        pe = _pe300_df()[['日期', '滚动市盈率']].copy()
        pe['日期'] = pd.to_datetime(pe['日期'])
        pe = pe.dropna().sort_values('日期')
        bd = _bond_df()
        col = _bond_col(bd)
        bd = bd[['日期', col]].dropna().copy()
        bd['日期'] = pd.to_datetime(bd['日期'])
        bd = bd.sort_values('日期')
        m = pd.merge_asof(pe, bd, on='日期', direction='backward',
                          tolerance=pd.Timedelta(days=10)).dropna()
        m['erp'] = 100 / m['滚动市盈率'] - m[col]
        w = m[m['日期'] >= m['日期'].iloc[-1] - pd.DateOffset(years=5)]
        cur = float(w['erp'].iloc[-1])
        pct = round(float((w['erp'] <= cur).mean()) * 100)
        d = w['日期'].iloc[-1].strftime('%Y-%m-%d')
        series = [{"d": x.strftime('%Y-%m-%d'), "v": round(float(v), 2)}
                  for x, v in zip(w['日期'], w['erp'])]
        log(f"  ✓ erp_history 当前 {cur:.2f}% 5年真实分位 {pct}%（{len(w)} 个月度样本，{d}）")
        return field({"current": round(cur, 2), "pct": pct, "n": len(w), "freq": "月",
                      "series": series}, '乐咕乐股+东财', d,
                     note='沪深300 滚动PE（乐咕乐股，月末点）与中国10Y国债（东财）按日期对齐，近5年')
    except Exception as e:
        log(f"  ✗ erp_history 失败: {e}")
        traceback.print_exc()
        return fallback('erp_history')

# ── 3c. 破净率（全部A股）─────────────────────────────────────
# 乐咕乐股 stock_a_below_net_asset_statistics：列 date / below_net_asset / total_company /
# below_net_asset_ratio，其中 ratio 为小数（0.0814 = 8.14%），日频，自 2005 年起。
def fetch_below_net_asset():
    try:
        import akshare as ak, pandas as pd
        df = ak.stock_a_below_net_asset_statistics(symbol="全部A股")
        df = df[['date', 'below_net_asset', 'total_company', 'below_net_asset_ratio']].dropna().copy()
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date')
        df['pct'] = df['below_net_asset_ratio'].astype(float) * 100
        last = df.iloc[-1]
        w = df[df['date'] >= last['date'] - pd.DateOffset(years=5)]
        cur = float(last['pct'])
        pct5y = round(float((w['pct'] <= cur).mean()) * 100)
        wk = w.iloc[::5]                                    # 画图用，约每周一个点
        if wk['date'].iloc[-1] != last['date']:
            wk = pd.concat([wk, w.tail(1)])
        series = [{"d": x.strftime('%Y-%m-%d'), "v": round(float(v), 2)}
                  for x, v in zip(wk['date'], wk['pct'])]
        d = last['date'].strftime('%Y-%m-%d')
        log(f"  ✓ below_net_asset {cur:.2f}%（{int(last['below_net_asset'])}/{int(last['total_company'])}）"
            f" 5年分位 {pct5y}% 日期 {d}")
        return field({"ratio": round(cur, 2), "count": int(last['below_net_asset']),
                      "total": int(last['total_company']), "pct5y": pct5y, "series": series},
                     '乐咕乐股', d, note='全部A股，按乐咕乐股统计口径（股价低于每股净资产的公司占比）')
    except Exception as e:
        log(f"  ✗ below_net_asset 失败: {e}")
        traceback.print_exc()
        return fallback('below_net_asset')

# ── 4. HS300 HV30 ──────────────────────────────────────────
def fetch_hv30():
    try:
        import akshare as ak, math
        # Sina 源稳定，东财接口偶尔 RemoteDisconnected
        df = ak.stock_zh_index_daily(symbol="sh000300")
        df = df.sort_values('date').reset_index(drop=True)
        closes = df['close'].astype(float).tolist()
        dates = df['date'].astype(str).tolist()
        # 30 日年化 HV
        hv_series = []
        for i in range(30, len(closes)):
            window = closes[i-30:i+1]
            log_rets = [math.log(window[j]/window[j-1]) for j in range(1, len(window))]
            mean = sum(log_rets)/len(log_rets)
            var = sum((r-mean)**2 for r in log_rets)/len(log_rets)
            hv_series.append(round(math.sqrt(var)*math.sqrt(252)*100, 1))
        hist_dates = dates[30:]
        # 仅保留最近 30 天作 hist
        out_hist = [{"d": f"{int(d[5:7])}/{int(d[8:10])}", "v": v}
                    for d, v in zip(hist_dates[-30:], hv_series[-30:])]
        latest = hv_series[-1]
        # 5 年分位（用最近 ~1250 个交易日的 HV 分布）
        recent5y = hv_series[-1250:] if len(hv_series) >= 1250 else hv_series
        below = sum(1 for v in recent5y if v <= latest)
        pct = round(below / len(recent5y) * 100)
        log(f"  ✓ hv30 latest={latest}% pct={pct}% (基于 {len(recent5y)} 个交易日) 日期 {dates[-1][:10]}")
        return field({"latest": latest, "pct": pct, "hist": out_hist}, '新浪K线', dates[-1][:10])
    except Exception as e:
        log(f"  ✗ hv30 失败: {e}")
        traceback.print_exc()
        return fallback('hv30')

# ── 5. 两融 daily + monthly ─────────────────────────────────
MARGIN_SZ_RATIO_DEFAULT = 1.95   # 深/沪两融余额比的兜底值（2026-09 实测 0.947 → 合计 ≈ 沪 × 1.95）

def _margin_sz_series():
    """深交所两融余额日序列 {YYYYMMDD: 元}。
    历史用 macro_china_market_margin_sz（深交所数据，东财转载，一次拿全历史）；
    最新一日用深交所官方 stock_margin_szse(date) 核对（单位亿元）。"""
    import akshare as ak, pandas as pd
    df = ak.macro_china_market_margin_sz()
    df = df[['日期', '融资融券余额']].dropna().copy()
    df['date'] = pd.to_datetime(df['日期']).dt.strftime('%Y%m%d')
    ser = dict(zip(df['date'], df['融资融券余额'].astype(float)))
    last = max(ser)
    try:
        off = ak.stock_margin_szse(date=last)
        v = float(off['融资融券余额'].iloc[0]) * 1e8
        diff = abs(v / ser[last] - 1)
        if diff > 0.005:
            log(f"  ⚠ 深市两融 {last} 转载值 {ser[last]/1e8:.0f} 亿与深交所官方 {v/1e8:.0f} 亿差 {diff:.1%}，以官方为准")
            ser[last] = v
    except Exception as e:
        log(f"  · 深交所官方单日核对不可得（{e}），沿用转载值")
    return ser

def fetch_margin():
    try:
        import akshare as ak
        from datetime import timedelta
        today = datetime.now()
        start = (today - timedelta(days=400)).strftime('%Y%m%d')
        end = today.strftime('%Y%m%d')
        sse = ak.stock_margin_sse(start_date=start, end_date=end)
        sse_col = next((c for c in sse.columns if '余额' in c and '融资融券' in c), None) or '融资融券余额'
        date_col = next(c for c in sse.columns if '日期' in c)
        sse = sse[[date_col, sse_col]].rename(columns={date_col:'date', sse_col:'sse'})
        sse['date'] = sse['date'].astype(str).str[:8]
        sse_all = sse.sort_values('date').reset_index(drop=True)
        sse_all['sse'] = sse_all['sse'].astype(float)

        # 深市：真实数据优先；两所按日期取交集（任一侧缺的日子不出合计，避免"半个市场"）
        try:
            sz = _margin_sz_series()
            sse_all = sse_all[sse_all['date'].isin(sz)].reset_index(drop=True)
            if sse_all.empty:
                raise RuntimeError("沪深两融无共同日期")
            sse_all['sz'] = sse_all['date'].map(sz)
            sse_all['total'] = sse_all['sse'] + sse_all['sz']
            ratio = round(float(sse_all['total'].iloc[-1] / sse_all['sse'].iloc[-1]), 4)
            src, est, note = '沪深交易所', False, '上交所 + 深交所融资融券余额（深市历史为东财转载的深交所数据，最新日经深交所官方核对）'
        except Exception as e:
            ratio = (prev_value('margin') or {}).get('sz_ratio') or MARGIN_SZ_RATIO_DEFAULT
            log(f"  ⚠ 深市两融不可得（{e}），按最近实测比例 ×{ratio} 估算")
            sse_all['total'] = sse_all['sse'] * ratio
            src, est, note = '上交所', True, f'深市不可得，两市合计 = 上交所 × {ratio}（最近一次实测比例）'

        # daily 取最近 30 天
        tail = sse_all.tail(30)
        daily = [{"d": f"{int(d[4:6])}/{int(d[6:8])}", "v": int(round(float(v)/1e8))}
                 for d, v in zip(tail['date'].tolist(), tail['total'].tolist())]
        # monthly 用全部数据按 YYYY-MM 分组取月内最后一日
        # 关键：剔除"当月未完成"月份，避免月中值当"月末"用，导致 Z 分数失真
        sse_all['ym'] = sse_all['date'].str[:6]
        current_ym = today.strftime('%Y%m')
        last_by_month = sse_all.groupby('ym').last().reset_index()
        completed = last_by_month[last_by_month['ym'] < current_ym]
        monthly = [{"m": f"{r['ym'][2:4]}-{r['ym'][4:6]}", "v": int(round(r['total']/1e8))}
                   for _, r in completed.iterrows()][-12:]
        # 单独把"当月至今"记录到 current_month（不参与 Z 计算，但方便 dashboard 显示）
        cur_row = last_by_month[last_by_month['ym'] == current_ym]
        current_month = None
        if not cur_row.empty:
            r = cur_row.iloc[0]
            current_month = {"m": f"{r['ym'][2:4]}-{r['ym'][4:6]}",
                             "v": int(round(r['total']/1e8)),
                             "partial": True,
                             "as_of": daily[-1]['d'] if daily else None}
        last = sse_all.iloc[-1]
        ld = last['date']
        log(f"  ✓ margin[{src}{'·估算' if est else ''}] daily={len(daily)} monthly={len(monthly)}(已完成) "
            f"{'+当月未完成 ' + current_month['m'] + ' ' if current_month else ''}"
            f"最新 {ld} 合计 {daily[-1]['v']} 亿（沪 {last['sse']/1e8:.0f}，比例 ×{ratio}）")
        out = {"daily": daily, "monthly": monthly, "sh_yi": int(round(last['sse']/1e8)),
               "sz_ratio": ratio}
        if not est:
            out["sz_yi"] = int(round(last['sz']/1e8))
        if current_month: out["current_month"] = current_month
        return field(out, src, f"{ld[:4]}-{ld[4:6]}-{ld[6:8]}", is_estimate=est, note=note)
    except Exception as e:
        log(f"  ✗ margin 失败: {e}")
        return fallback('margin')

# ── 6. 近 7 日成交额 ────────────────────────────────────────
# 主源：fetch_turnover() 里交易所的 amount_yi 序列（沪主板A+科创板 + 深主板A+创业板，
#       不含北交所），驱动「连续7日>2万亿」逃顶信号。
# 兜底：仅当本次交易所数据完全拿不到时，才用新浪指数成交量 × 经验系数估算（is_estimate）。
def fetch_vol_7d(turnover):
    series = ((turnover or {}).get('value') or {}).get('series') or []
    if series and not turnover.get('stale'):
        out = [{"d": x['label'], "date": x['date'], "v": x['amount_yi']} for x in series]
        log(f"  ✓ vol_7d {len(out)} 天（沪深交易所），最新 {out[-1]['date']} {out[-1]['v']} 亿")
        return field(out, '沪深交易所', out[-1]['date'],
                     note='沪主板A+科创板 + 深主板A+创业板，不含北交所')
    log("  ⚠ vol_7d 本次交易所数据不可得，尝试新浪估算兜底")
    try:
        return _vol_7d_sina_estimate()
    except Exception as e:
        log(f"  ✗ vol_7d 新浪估算也失败: {e}")
    return fallback('vol_7d')

def _vol_7d_sina_estimate():
    """新浪 stock_zh_index_daily 成交量 × 经验系数（元/股）估算成交额，仅作兜底。"""
    import akshare as ak
    AMT_FACTOR = {"sh000001": 19.1, "sz399001": 20.8}
    def _tail7(sym):
        df = ak.stock_zh_index_daily(symbol=sym).sort_values('date').tail(7).reset_index(drop=True)
        df['date'] = df['date'].astype(str)
        return df
    sh, sz = _tail7("sh000001"), _tail7("sz399001")
    n = min(len(sh), len(sz))
    out = []
    for i in range(n):
        d = sh.iloc[i]['date'][:10]
        total = (float(sh.iloc[i]['volume']) * AMT_FACTOR['sh000001']
                 + float(sz.iloc[i]['volume']) * AMT_FACTOR['sz399001']) / 1e8
        out.append({"d": f"{int(d[5:7])}/{int(d[8:10])}", "date": d, "v": int(round(total))})
    log(f"  ⚠ vol_7d {len(out)} 天（新浪估算兜底），最新 {out[-1]['v']} 亿")
    return field(out, '新浪估算', out[-1]['date'], is_estimate=True,
                 note='交易所数据不可得时的兜底：指数成交量 × 经验系数，非真实成交额')

# ── 7. 近 5 日涨跌停 ─────────────────────────────────────
def fetch_limit_7d():
    try:
        import akshare as ak
        from datetime import timedelta
        # 取近 10 个自然日找出 5 个交易日
        out, dt = [], datetime.now()
        for _ in range(15):
            ds = dt.strftime('%Y%m%d')
            try:
                up = len(ak.stock_zt_pool_em(date=ds))
                dn = len(ak.stock_zt_pool_dtgc_em(date=ds))
                if up > 0 or dn > 0:
                    out.append({"date": dt.strftime('%Y-%m-%d'), "up": up, "down": dn})
                    if len(out) >= 5: break
            except Exception: pass
            dt -= timedelta(days=1)
        out.reverse()
        log(f"  ✓ limit_7d {len(out)} 天，今 up/dn={out[-1]['up']}/{out[-1]['down']}")
        return field(out, '东财', out[-1]['date'])
    except Exception as e:
        log(f"  ✗ limit_7d 失败: {e}")
        return fallback('limit_7d')

# ── 8. 申万一级 60 日涨跌 ────────────────────────────────────
def fetch_sector_live():
    try:
        import akshare as ak
        info = ak.sw_index_first_info()
        items = [(row['行业代码'].split('.')[0], row['行业名称']) for _, row in info.iterrows()]

        def _one(code, name):
            try:
                df = ak.index_hist_sw(symbol=code, period='day')
                df = df.sort_values('日期').reset_index(drop=True)
                closes = df['收盘'].astype(float).tolist()
                if len(closes) < 61: return None
                ret60 = (closes[-1]/closes[-61]-1)*100
                today = (closes[-1]/closes[-2]-1)*100
                return {"n": name, "code": code,
                        "today": round(today, 2), "ret60": round(ret60, 2),
                        "date": str(df['日期'].iloc[-1])[:10]}
            except Exception: return None

        out = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_one, c, n): (c, n) for c, n in items}
            for f in as_completed(futs):
                r = f.result()
                if r: out.append(r)
        out.sort(key=lambda x: x['ret60'], reverse=True)
        d = max(x['date'] for x in out)
        log(f"  ✓ sector_live {len(out)}/{len(items)} 行业，最强 {out[0]['n']} {out[0]['ret60']}% 日期 {d}")
        return field(out, '申万一级指数', d, note='today=最新收盘日涨跌幅，ret60=60日累计涨跌幅')
    except Exception as e:
        log(f"  ✗ sector_live 失败: {e}")
        return fallback('sector_live')

# ── 9. 全 A 换手率（沪+深合并）─────────────────────────────
# 用于 dashboard L2 pcaCrowding，之前 dashboard 只能靠 estimateTurn(volYuan) 粗估
def _turnover_one_day(d):
    """单日全A换手率。d = 'YYYYMMDD'。失败抛异常。"""
    import akshare as ak
    sh = ak.stock_sse_deal_daily(date=d)
    sz = ak.stock_szse_summary(date=d)
    if sh.empty or sz.empty:
        raise ValueError("empty frame")
    # 沪：主板 A + 科创板（单位已是亿元）
    sh_amt_row = sh[sh['单日情况'] == '成交金额']
    sh_cap_row = sh[sh['单日情况'] == '流通市值']
    sh_amt = float(sh_amt_row['主板A'].iloc[0]) + float(sh_amt_row['科创板'].iloc[0])
    sh_cap = float(sh_cap_row['主板A'].iloc[0]) + float(sh_cap_row['科创板'].iloc[0])
    # 深：主板 A + 创业板 A（单位是元，需 /1e8）
    sz_a = sz[sz['证券类别'].isin(['主板A股', '创业板A股'])]
    sz_amt = float(sz_a['成交金额'].sum()) / 1e8
    sz_cap = float(sz_a['流通市值'].sum()) / 1e8
    # 收盘后两所发布时间不同：深交所当日汇总可能晚于上交所。任一侧缺失时整天作废，
    # 否则会写入只含半个市场的成交额/换手率（实测：沪 8053 亿 + 深 0）。
    if min(sh_amt, sh_cap, sz_amt, sz_cap) <= 0:
        raise ValueError(f"{d} 交易所数据不完整：沪 {sh_amt:.0f}/{sh_cap:.0f} 深 {sz_amt:.0f}/{sz_cap:.0f} 亿")
    total_amt, total_cap = sh_amt + sz_amt, sh_cap + sz_cap
    return {
        "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
        "label": f"{int(d[4:6])}/{int(d[6:8])}",
        "sh_amount_yi": round(sh_amt),
        "sz_amount_yi": round(sz_amt),
        "sh_mktcap_yi": round(sh_cap),
        "sz_mktcap_yi": round(sz_cap),
        "amount_yi": round(total_amt),
        "mktcap_yi": round(total_cap),
        "pct": round(total_amt / total_cap * 100, 3),
    }

def fetch_turnover():
    """全A换手率最近 7 个交易日序列 = 沪深合计成交额 / 沪深合计A股流通市值 × 100。

    返回 {..最新日字段.., 'avg_pct': 最新值, 'series': [{label,date,pct,amount_yi}, ...×7]}
    series 供 dashboard 主面板"两市成交额×换手率"图表使用（此前该图只能用
    硬编码 9e13 流通市值估算，与真实流通市值有约 8% 偏差）。
    """
    try:
        from datetime import timedelta
        days, cursor = [], datetime.now()
        # 往回扫最多 20 个自然日，凑齐 7 个交易日
        for _ in range(20):
            d = cursor.strftime('%Y%m%d')
            try:
                days.append(_turnover_one_day(d))
                if len(days) >= 7: break
            except Exception:
                pass
            cursor -= timedelta(days=1)
        if not days:
            log("  ✗ turnover 20 天内无可用交易日数据")
            return fallback('turnover')
        days.reverse()                       # 由旧到新
        latest = days[-1]
        out = dict(latest)
        out['avg_pct'] = latest['pct']       # 向后兼容旧字段名
        out['series'] = [{"label": x['label'], "date": x['date'],
                          "pct": x['pct'], "amount_yi": x['amount_yi']} for x in days]
        log(f"  ✓ turnover {latest['date']} = {latest['pct']}% "
            f"（{latest['amount_yi']}亿/{latest['mktcap_yi']}亿），序列 {len(days)} 日")
        return field(out, '沪深交易所', latest['date'])
    except Exception as e:
        log(f"  ✗ turnover 失败: {e}")
        traceback.print_exc()
    return fallback('turnover')

# ── 10. 偏股基金新发 ───────────────────────────────────────
def fetch_fund_issuance():
    try:
        import akshare as ak
        df = ak.fund_new_found_em()
        col_date = next(c for c in df.columns if '成立' in c or '日期' in c)
        col_share = next(c for c in df.columns if '份额' in c)
        col_type = next((c for c in df.columns if '类型' in c), None)
        if col_type:
            # 「基金类型」实际取值（2026-09 实测）：股票型、混合型-偏股/灵活/平衡/偏债、指数型-股票、
            # 指数型-海外股票、QDII-普通股票/混合偏股/混合平衡/混合灵活/混合债、债券型-混合一级/二级 …
            # 取含「股票」「混合」的类型，排除偏债类：混合型-偏债、QDII-混合债、债券型-混合一级/二级
            # （后两者含「混合」二字但属于债券基金，旧筛选 '股票|混合' 会把它们算进偏股新发）。
            t = df[col_type].astype(str)
            df = df[t.str.contains('股票|混合', na=False)
                    & ~t.str.contains('偏债|混合债', na=False)
                    & ~t.str.startswith('债券型')]
        df = df.dropna(subset=[col_date, col_share]).copy()
        import pandas as pd
        df[col_date] = pd.to_datetime(df[col_date], errors='coerce')
        df = df.dropna(subset=[col_date])
        df['ym'] = df[col_date].dt.strftime('%y-%m')
        df[col_share] = pd.to_numeric(df[col_share], errors='coerce')
        agg = df.groupby('ym')[col_share].sum().sort_index().tail(12)
        out = [{"m": ym, "v": round(float(v), 1)} for ym, v in agg.items()]
        log(f"  ✓ fund_issuance {len(out)} 月，最新 {out[-1] if out else '空'}")
        if not out: return fallback('fund_issuance')
        return field(out, '东财', _ym(out[-1]['m']))
    except Exception as e:
        log(f"  ✗ fund_issuance 失败: {e}")
        return fallback('fund_issuance')

# ── 11. ETF 资金分类流向（沪市，60交易日净流入）───────────────
# 数据源：上交所 ETF 份额（ak.fund_etf_scale_sse，按 STAT_DATE 取快照），
#   现价来自 ak.fund_etf_spot_em。净流入 ≈ (份额_now − 份额_60d前) × 现价。
#   旧份额按现价计值以隔离价格因素，change_pct = 净流入/旧市值。
#   分类由基金简称关键词判定，行业主题优先于宽基，「其他」占比约 0~2%。
ETF_RULES = [
    ("增强指数", ["增强"]),
    ("跨境",     ["恒生","恒指","恒","中概","港股","H股","HK","HKC","纳指","纳斯达克","标普","日经",
                  "德国","DAX","道琼斯","海外","美股","东南亚","亚太","越南","印度","法国","沙特",
                  "新兴市场","全球","中韩","日本","欧洲","香港","东证","NA股","沪港深","港科","巴西",
                  "新兴亚洲","亚洲","中金优","MSCI中国","富时中国"]),
    ("商品",     ["黄金","白银","原油","豆粕","能源化工","农产品","大宗","饲料","生猪期","有色金属期",
                  "上海金","金ETF","商品"]),
    ("债券货币", ["可转债","转债","国债","政金债","信用债","城投债","货币","债ETF","短融","国开","地方债",
                  "现金基金","现金指数","现金ETF","中银现金","活期","添益","现金添","债"]),
    ("半导体芯片",["芯片","半导","存储","封测","集成电路","科创芯","芯","科创材料","科创新材","电子"]),
    ("AI算力",   ["人工智能","算力","云计算","大数据","数字经济","机器人","数据中心","AI","数据","数字"]),
    ("软件通信", ["软件","计算机","信创","网络安全","通信","5G","物联网","游戏","传媒","互联网","网络",
                  "云","信息","TMT","科技","文娱","影视","电信"]),
    ("医药生物", ["医药","医疗","创新药","疫苗","中药","基因","生物","疫","CXO","器械","医",
                  "新药","生科","保健","养老","药"]),
    ("新能源电力",["新能源","电池","锂电","光伏","储能","风电","电网","电力","绿电","核电","氢能",
                  "新能车","碳中和","新能","双碳","公用","能源","低碳","绿色"]),
    ("汽车交运", ["汽车","整车","零部件","物流","运输","港口","航运","铁路","公路","交通","交运","车",
                  "智能驾驶","驾驶"]),
    ("消费",     ["白酒","食品","饮料","家电","家居","旅游","免税","零售","纺织","服装","农业","养殖",
                  "消费","畜牧","乳","酒","农牧","宠物","美容","农林牧渔","教育","消服","消电","国货"]),
    ("金融地产", ["银行","证券","券商","保险","地产","REIT","金融","房","不动产"]),
    ("军工制造", ["军工","国防","航空","航天","卫星","船舶","机械","装备","专精特新","工业母机",
                  "高端制造","兵","导弹","国防军工","智能制造","智造","通航"]),
    ("周期资源", ["有色","煤炭","钢铁","化工","矿","稀土","稀有","石油","石化","建材","水泥","资源",
                  "材料","钢","煤","油气","电解铝","锂","环保","金属","新材","基建"]),
    ("风格因子", ["红利","低波","价值","成长","质量","动量","ESG","自由现金流","现金流","现金自由",
                  "自由现金","基本面","央企","国企","国资","央创","龙头","蓝筹","分红","股息","价值回报",
                  "可持续","央调","央视"]),
    ("宽基",     ["沪深300","中证500","上证50","中证1000","中证2000","A500","中证A50","科创50",
                  "科创100","科创综","创业板","上证综指","上证指数","深证","中证100","中证800","双创",
                  "国证","巨潮","中证全指","上证180","上证380","MSCI","富时","综指","规模",
                  "治理","超大","中盘","大盘","小盘","上证","中证","沪深","全指","战略新兴","产业升级",
                  "长三角","湾区","G60","之江","综合","龙头股","央视50","长江","张江","科创","科综","科200","A股",
                  "300","500","1000","2000","50","800","180","100","380","225","580"]),
]

def _etf_classify(name):
    for cat, kws in ETF_RULES:
        for kw in kws:
            if kw in name:
                return cat
    return "其他"

def _sse_scale_on(date_str):
    """某交易日沪市 ETF 份额 DataFrame；空/失败返回 None。date_str='YYYYMMDD'"""
    try:
        import akshare as ak
        df = ak.fund_etf_scale_sse(date=date_str)
        return df if (df is not None and len(df) > 100) else None
    except Exception:
        return None

def _sse_find_valid(anchor, back_days):
    """从 anchor 向前最多 back_days 天找一个 SSE 有数的日子，返回 (df, 'YYYY-MM-DD')"""
    from datetime import timedelta
    cur = anchor
    for _ in range(back_days):
        df = _sse_scale_on(cur.strftime('%Y%m%d'))
        if df is not None:
            return df, cur.strftime('%Y-%m-%d')
        cur -= timedelta(days=1)
    return None, None

def _etf_prices():
    """ETF 现价 {6位代码: 价格}, 来源名。东财 fund_etf_spot_em 为主，失败时用新浪
    fund_etf_category_sina（列：代码/名称/最新价…，代码带 sh/sz 前缀，取后 6 位）。"""
    import akshare as ak
    def _collect(df):
        out = {}
        for code, p in zip(df['代码'].astype(str), df['最新价']):
            try: p = float(p)
            except Exception: continue
            if p > 0: out[code[-6:]] = p
        return out
    try:
        price = _collect(ak.fund_etf_spot_em())
        if len(price) > 100:
            return price, '东财'
        log(f"  · 东财 ETF 现价仅 {len(price)} 只，改用新浪")
    except Exception as e:
        log(f"  · 东财 ETF 现价失败，改用新浪: {e}")
    price = _collect(ak.fund_etf_category_sina(symbol="ETF基金"))
    if len(price) <= 100:
        raise RuntimeError(f"新浪 ETF 现价也不可用（{len(price)} 只）")
    return price, '新浪'

DIVIDEND_ETF = None

def fetch_etf_categories():
    global DIVIDEND_ETF
    try:
        import akshare as ak
        from datetime import timedelta
        from collections import defaultdict
        # 现价（东财 → 新浪）
        price, price_src = _etf_prices()
        # 最新 + 约60交易日前 两个份额快照
        df_now, date_now = _sse_find_valid(datetime.now(), 8)
        if df_now is None:
            log("  ✗ etf_categories: SSE 最新份额不可得")
            return fallback('etf_categories')
        anchor_old = datetime.strptime(date_now, '%Y-%m-%d') - timedelta(days=88)
        df_old, date_old = _sse_find_valid(anchor_old, 12)
        old_share = ({str(r['基金代码']): float(r['基金份额']) for _, r in df_old.iterrows()}
                     if df_old is not None else {})
        # A separate exact-index subset; never change the original classification/totals.
        try:
            from dividend_monitor import DATA, atomic_json, etf_subset, sha
            now_rows = df_now.to_dict('records')
            old_rows = df_old.to_dict('records') if df_old is not None else []
            DIVIDEND_ETF = etf_subset(now_rows, old_rows, price, date_now, date_old, price_src)
            codes = {r['code'] for r in DIVIDEND_ETF['value']['verified_members']}
            inputs = {'latest_date': date_now, 'prev_date': date_old, 'price_source': price_src,
                      'price_retrieved_at': DIVIDEND_ETF['value']['price_retrieved_at'],
                      'now': [{'基金代码': str(r['基金代码']), '基金份额': float(r['基金份额'])}
                              for r in now_rows if str(r['基金代码']) in codes],
                      'before': [{'基金代码': str(r['基金代码']), '基金份额': float(r['基金份额'])}
                                 for r in old_rows if str(r['基金代码']) in codes],
                      'prices': {c: price[c] for c in codes if c in price}}
            path = DATA / 'raw' / f'etf_{date_now}_{date_old}.json'
            atomic_json(path, inputs)
            DIVIDEND_ETF['value']['input_sha256'] = sha(path)
        except Exception as e:
            # A new subset failure must not alter the existing ETF classification.
            DIVIDEND_ETF = None
            log(f"  · 红利ETF子集不可用（原分类继续）: {e}")

        agg = defaultdict(lambda: {"count": 0, "scale_now": 0.0, "scale_old": 0.0, "flow": 0.0})
        for _, r in df_now.iterrows():
            code = str(r['基金代码']); name = str(r['基金简称'])
            p = price.get(code)
            if not p:
                continue
            sh_now = float(r['基金份额'])
            sh_old = old_share.get(code, sh_now)      # 新上市无旧值 → 计 0 流入
            scale_now = sh_now * p / 1e8              # 亿元
            scale_old = sh_old * p / 1e8              # 旧份额按现价计值
            a = agg[_etf_classify(name)]
            a["count"] += 1; a["scale_now"] += scale_now
            a["scale_old"] += scale_old; a["flow"] += (scale_now - scale_old)

        cats = []
        for cat, a in agg.items():
            pct = (a["flow"] / a["scale_old"] * 100) if a["scale_old"] > 0 else 0.0
            cats.append({"category": cat, "count_now": a["count"],
                         "scale_yi": round(a["scale_now"], 1),
                         "change_yi": round(a["flow"], 1),
                         "change_pct": round(pct, 2)})
        cats.sort(key=lambda x: x["change_yi"], reverse=True)
        total = sum(c["scale_yi"] for c in cats) or 1
        other = next((c["scale_yi"] for c in cats if c["category"] == "其他"), 0)
        log(f"  ✓ etf_categories now={date_now} vs {date_old} 现价={price_src} "
            f"{len(cats)}类/{sum(c['count_now'] for c in cats)}只，其他占比 {other/total*100:.1f}%")
        return field({"latest_date": date_now, "prev_date": date_old, "price_source": price_src,
                      "categories": cats},
                     f'上交所份额×{price_src}现价', date_now,
                     note='旧份额按当前价格计值：反映份额变化带来的资金进出，不含价格涨跌')
    except Exception as e:
        log(f"  ✗ etf_categories 失败: {e}")
        traceback.print_exc()
        return fallback('etf_categories')

def fetch_dividend_lowvol100():
    from dividend_monitor import build_snapshot, fetch_history
    old = prev_value('dividend_lowvol100') or {}
    etf = DIVIDEND_ETF
    if etf is None:
        etf = old.get('etf')
        if etf:
            etf = {**etf, 'stale': True}
    try:
        result = build_snapshot(fetch_history(), etf=etf)
        log(f"  ✓ 红利低波100 {result['date']} 联合{result['value']['latest']['signals']['joint']:g}倍 当前可用={result['value']['current']}")
        return result
    except Exception as e:
        log(f"  ✗ 红利低波100更新失败: {e}")
        result = fallback('dividend_lowvol100')
        if result['value'] is not None:
            result['value'] = {**result['value'], 'current': False, 'etf': etf,
                               'update_error': str(e)[:220]}
        return result

# ── 单段超时保护 ─────────────────────────────────────────────
# AKShare 多数接口不带超时，东财偶发"连上但不返回"会让整次更新无限挂起
# （实测 stock_zt_pool_em 卡住 >13 分钟，launchd 下一小时再叠一个进程）。
# 每段放进守护线程跑，超出预算即放弃（线程随进程退出），该字段走 fallback → stale。
SECTION_TIMEOUT = int(os.environ.get('ARISK_SECTION_TIMEOUT', '180'))

def run_section(key, fn, timeout=SECTION_TIMEOUT):
    import threading
    box = {}
    def _run():
        try: box['v'] = fn()
        except Exception as e: box['e'] = e
    t = threading.Thread(target=_run, daemon=True)
    t.start(); t.join(timeout)
    if t.is_alive():
        log(f"  ✗ {key} 超过 {timeout}s 未返回，放弃本段")
        return fallback(key)
    if 'e' in box:
        log(f"  ✗ {key} 未捕获异常: {box['e']}")
        return fallback(key)
    return box['v']

# ── 主流程 ────────────────────────────────────────────────
def main():
    t0 = time.time()
    log("=== update_arisk_data.py 开始 ===")
    log(f"  MX_APIKEY: {'已配置（社融降级链启用妙想）' if MX_KEY else '未配置，社融降级链跳过妙想'}")

    out = {
        "generated_at": datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        "generated_date": datetime.now().strftime('%Y-%m-%d'),
    }

    log("[1/14] 抓 社融存量同比 ...")
    out['credit_yoy'] = run_section('credit_yoy', fetch_credit_yoy)
    log("[2/14] 抓 10Y 国债 ...")
    out['bond10y'] = run_section('bond10y', fetch_bond10y)
    log("[3/14] 抓 沪深300 PE ...")
    out['pe_300'] = run_section('pe_300', fetch_pe_300)
    log("[4/14] 算 ERP 近5年真实分位 ...")
    out['erp_history'] = run_section('erp_history', fetch_erp_history)
    log("[5/14] 抓 破净率 ...")
    out['below_net_asset'] = run_section('below_net_asset', fetch_below_net_asset)
    log("[6/14] 算 HV30 ...")
    out['hv30'] = run_section('hv30', fetch_hv30)
    log("[7/14] 抓 两融 ...")
    out['margin'] = run_section('margin', fetch_margin)
    log("[8/14] 抓 全A 换手率 / 成交额（交易所）...")
    out['turnover'] = run_section('turnover', fetch_turnover)
    log("[9/14] 整理 近7日成交额 ...")
    out['vol_7d'] = run_section('vol_7d', lambda: fetch_vol_7d(out['turnover']))
    log("[10/14] 抓 近5日涨跌停 ...")
    out['limit_7d'] = run_section('limit_7d', fetch_limit_7d)
    log("[11/14] 抓 申万31行业60日 ...")
    out['sector_live'] = run_section('sector_live', fetch_sector_live)
    log("[12/14] 抓 偏股基金新发 ...")
    out['fund_issuance'] = run_section('fund_issuance', fetch_fund_issuance)
    log("[13/14] 抓 ETF 资金分类流向（沪市60日）...")
    out['etf_categories'] = run_section('etf_categories', fetch_etf_categories)
    log("[14/14] 更新 红利低波100 独立温度计 ...")
    out['dividend_lowvol100'] = run_section('dividend_lowvol100', fetch_dividend_lowvol100)

    fields = {k: v for k, v in out.items() if _is_wrapped(v)}
    missing = [k for k, v in fields.items() if v['value'] is None]
    stale = [f"{k}({v.get('date')})" for k, v in fields.items() if v['stale'] and v['value'] is not None]
    if missing: log(f"⚠ 以下字段缺失: {missing}")
    if stale: log(f"⚠ 以下字段本次抓取失败，复用旧值: {stale}")

    # 写入
    tmp = OUT_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT_PATH)
    log(f"=== 完成（{time.time()-t0:.1f}s），输出 {OUT_PATH} ===")
    # 数据已落盘。超时放弃的段可能留下卡在网络读上的线程（如电脑睡眠后连接失效）；
    # 线程池的工作线程会让解释器退出时一直等它们，进程挂住后 launchd 不再启动下一次更新。
    # 所以这里直接结束进程，不等残留线程。
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0)

if __name__ == '__main__':
    sys.exit(main())
