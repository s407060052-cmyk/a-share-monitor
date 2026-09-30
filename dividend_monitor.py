"""930955 v1.3 monitoring. Official history, deterministic rules, no orders."""
from __future__ import annotations

import bisect
import calendar as month_calendar
import csv
import hashlib
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data' / 'dividend'
RULES = ROOT / 'docs' / 'DIVIDEND-RULES-v1.3.md'
VERSION = '1.3'
RETURN6_VERSION = '1.0'
RETURN6_MIN_OBSERVATIONS = 756
SHANGHAI = ZoneInfo('Asia/Shanghai')
CSI_URL = 'https://www.csindex.com.cn/csindex-home/perf/index-perf'
CN_URL = 'https://yield.chinabond.com.cn/cbweb-mn/pgxh/historyQuery'
CALENDAR_URL = 'https://finance.sina.com.cn/realstock/company/klc_td_sh.txt'
REMOVED_BOUNDARIES = {'2013-01-01', '2016-09-18'}

# Exact index identity was checked in exchange disclosures, not inferred from names.
ETF_MEMBERS = [
    {'code': '515100', 'name': '景顺长城红利低波100ETF', 'index': '930955',
     'url': 'https://www.sse.com.cn/disclosure/fund/announcement/c/new/2023-11-17/515100_20231117_TGEZ.pdf'},
    {'code': '515480', 'name': '南方红利低波100ETF', 'index': '930955',
     'url': 'https://www.sse.com.cn/disclosure/announcement/general/jjzssgg/c/c_20260703_10824306.shtml'},
    {'code': '560520', 'name': '大成红利低波100ETF', 'index': '930955',
     'url': 'https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-06-25/560520_20260625_YUOJ.pdf'},
    {'code': '560720', 'name': '工银红利低波100ETF', 'index': '930955',
     'url': 'https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-06-02/560720_20260602_DT0F.pdf'},
]


def finite(x):
    return isinstance(x, (float, int)) and not isinstance(x, bool) and math.isfinite(x)


def number(x):
    try:
        value = float(x)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False,
                              separators=(',', ':')) + '\n', encoding='utf-8')
    tmp.replace(path)


def anniversary(day):
    d = date.fromisoformat(day)
    return d.replace(year=d.year - 1, day=min(d.day, month_calendar.monthrange(d.year - 1, d.month)[1])).isoformat()


def months_before(day, count):
    """Calendar-month lookback; clamp month ends and use no future quotes."""
    d = date.fromisoformat(day)
    year, zero_month = divmod(d.year * 12 + d.month - 1 - count, 12)
    month = zero_month + 1
    return date(year, month, min(d.day, month_calendar.monthrange(year, month)[1])).isoformat()


def return6_signal(percentile, observations):
    """Independent six-month price-position observation, never a v1.3 multiplier."""
    if not finite(percentile) or observations < RETURN6_MIN_OBSERVATIONS:
        return 'unavailable'
    if percentile <= 10:
        return 'strong_buy'
    if percentile <= 20:
        return 'low_buy'
    if percentile >= 90:
        return 'clear'
    if percentile >= 80:
        return 'sell'
    return 'neutral'


def normalize_csi(payload, code):
    if str(payload.get('code')) != '200' or not isinstance(payload.get('data'), list) or not payload['data']:
        raise ValueError(f'{code} 官方响应为空或状态异常')
    result = {}
    for row in payload['data']:
        if row.get('indexCode') != code:
            raise ValueError('指数身份不一致')
        day = datetime.strptime(row['tradeDate'], '%Y%m%d').date().isoformat()
        if day in REMOVED_BOUNDARIES:
            continue
        if date.fromisoformat(day).weekday() >= 5:
            raise ValueError(f'非交易日边界记录: {day}')
        close = number(row.get('close'))
        if close is None or close <= 0:
            raise ValueError(f'{code} {day} 收盘点位无效')
        item = {'date': day, 'close': close, 'pe': number(row.get('peg'))}
        if day in result and result[day] != item:
            raise ValueError(f'{code} 重复日期冲突: {day}')
        result[day] = item
    return result


def paired_quotes(price, tr):
    if price.keys() != tr.keys():
        raise ValueError('930955与H20955日期不一致，保留上次完整快照')
    return [{'date': day, 'price': price[day]['close'], 'tr': tr[day]['close'],
             'pe': price[day]['pe']} for day in sorted(price)]


def rules(row):
    """Pure v1.3 rule evaluation; PE is a percentile, spread is percentage points."""
    bias, pp, spread, dp = (row.get(k) for k in ('price_bias250_pct', 'pe_pct', 'spread', 'dy_pct'))
    ma = (0 if bias >= 10 else .5 if bias >= 5 else 1 if bias > -5 else
          2 if bias > -10 else 3 if bias > -15 else 4) if finite(bias) else 1
    reasons = [f'MA250偏离{bias:+.2f}%，基础节奏{ma:g}倍' if finite(bias) else 'MA250不足250日，回退基础1倍']
    ready = bool(row.get('ready'))
    pe = (0 if pp >= 80 else .5 if pp >= 60 else 1 if pp >= 40 else
          2 if pp >= 20 else 3 if pp >= 10 else 4) if ready else 1
    joint = ma
    if ready:
        if pp >= 60:
            joint = min(joint, 1)
            reasons.append('PE分位≥60%，新增上限1倍')
        if pp < 40 and finite(bias) and bias <= -5 and spread >= 1.5:
            joint = max(joint, 3)
            reasons.append('PE分位<40%、偏离≤−5%、代理利差≥1.5个百分点，至少3倍')
        if pp < 20 and finite(bias) and bias <= -10 and spread >= 2.3:
            joint = 4
            reasons.append('PE分位<20%、偏离≤−10%、代理利差≥2.3个百分点，候选4倍')
        if spread < 0 or dp < 20:
            joint = min(joint, 1)
            reasons.append('代理利差<0或分红代理分位<20%，新增上限1倍')
        if pp >= 80:
            joint = 0
            reasons.append('PE分位≥80%，暂停新增，优先于其他条件')
    else:
        reasons.append('PE/分红代理分位不足756个有效观察，联合规则回退MA250规则')
    # A stop from the MA staircase cannot be overridden by another condition.
    if ma == 0:
        joint = 0
    if not finite(row.get('pe')) or row['pe'] <= 0:
        pe = joint = 0
        reasons.append('当前PE缺失或非正：暂停新增，PE待核')
    slope = row.get('price_ma250_change20_pct')
    trend = min(joint, 2) if finite(slope) and slope < 0 else joint
    old = (0 if spread < 1.5 else 1 if spread < 2.3 else 1.5 if spread < 3 else 2) if finite(spread) else None
    if old is not None and ready and (dp < 20 or pp > 80):
        old = min(old, 1)
    if not finite(row.get('pe')) or row['pe'] <= 0:
        old = 0
    return {'joint': joint, 'trend': trend, 'ma': ma, 'pe_only': pe, 'old': old,
            'fixed': 1, 'reasons': reasons, 'trend_note':
            'MA250较20交易日前下降，趋势保护版最多2倍' if finite(slope) and slope < 0 else
            'MA250方向尚未成熟' if not finite(slope) else 'MA250方向未触发保护'}


def calculate(quotes, bonds):
    days = [r['date'] for r in quotes]
    if days != sorted(set(days)):
        raise ValueError('行情日期必须唯一且递增')
    if any(not finite(r.get(k)) or r[k] <= 0 for r in quotes for k in ('price', 'tr')):
        raise ValueError('指数收盘点位无效')
    bd = sorted(bonds)
    pe_samples, dy_samples, return6_samples, rows = [], [], [], []
    sums = {'price': 0., 'tr': 0.}
    for i, quote in enumerate(quotes):
        row = dict(quote)
        day = row['date']
        before = bisect.bisect_right(days, anniversary(day)) - 1
        proxy = None
        if before >= 0:
            past = quotes[before]
            proxy = 100 * ((row['tr'] / row['price']) / (past['tr'] / past['price']) - 1)
        six_month_anchor = months_before(day, 6)
        six_month_index = bisect.bisect_right(days, six_month_anchor) - 1
        if six_month_index >= 0 and (date.fromisoformat(six_month_anchor) -
                                     date.fromisoformat(days[six_month_index])).days > 14:
            six_month_index = -1  # Missing history must not masquerade as a six-month return.
        return6 = 100 * (row['price'] / quotes[six_month_index]['price'] - 1) if six_month_index >= 0 else None
        if finite(return6):
            bisect.insort(return6_samples, return6)
        return6_n = len(return6_samples)
        return6_rank = (100 * (bisect.bisect_left(return6_samples, return6) +
                        .5 * (bisect.bisect_right(return6_samples, return6) - bisect.bisect_left(return6_samples, return6))) /
                        return6_n) if finite(return6) else None
        row.update(price_return_6m_pct=return6,
                   price_return_6m_base_date=quotes[six_month_index]['date'] if six_month_index >= 0 else None,
                   price_return_6m_rank_pct=return6_rank,
                   price_return_6m_rank_n=return6_n,
                   price_return_6m_signal=return6_signal(return6_rank, return6_n))
        bi = bisect.bisect_right(bd, day) - 1
        bond_day = bd[bi] if bi >= 0 else None
        bond = bonds[bond_day] if bond_day else None
        row.update(dy_proxy=proxy, cn10y=bond, bond_date=bond_day,
                   spread=proxy - bond if finite(proxy) and finite(bond) else None)
        for name, value, samples in [('pe', row.get('pe'), pe_samples), ('dy', proxy, dy_samples)]:
            valid = finite(value) and (name != 'pe' or value > 0)
            if valid:
                bisect.insort(samples, value)
            row[name + '_n'] = len(samples)
            row[name + '_pct'] = (100 * (bisect.bisect_left(samples, value) +
                .5 * (bisect.bisect_right(samples, value) - bisect.bisect_left(samples, value))) /
                len(samples)) if valid else None
        row['ready'] = row['pe_n'] >= 756 and row['dy_n'] >= 756 and finite(row['pe_pct']) and finite(row['dy_pct']) and finite(row['spread'])
        for key, prefix in [('price', 'price'), ('tr', 'tr')]:
            sums[key] += row[key]
            if i >= 250:
                sums[key] -= quotes[i - 250][key]
            ma = sums[key] / 250 if i >= 249 else None
            previous = rows[i - 20].get(prefix + '_ma250') if i >= 20 else None
            row[prefix + '_ma250'] = ma
            row[prefix + '_bias250_pct'] = 100 * (row[key] / ma - 1) if ma else None
            row[prefix + '_ma250_change20_pct'] = 100 * (ma / previous - 1) if ma and previous else None
        row['signals'] = rules(row)
        rows.append(row)
    return rows


def expected_session(sessions, now=None):
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is not None:
        now = now.astimezone(SHANGHAI)
    cutoff = now.date() - (timedelta(days=1) if now.hour < 18 else timedelta())
    if not sessions or sessions[-1] < cutoff.isoformat():
        return None  # Calendar coverage is unknown; don't infer holidays from weekdays.
    pos = bisect.bisect_right(sessions, cutoff.isoformat()) - 1
    return sessions[pos] if pos >= 0 else None


def monthly_reference(rows):
    if not rows:
        return None
    month = rows[-1]['date'][:7]
    i = next(i for i, r in enumerate(rows) if r['date'].startswith(month))
    if i == 0:
        return None
    previous = rows[i - 1]
    return {'execution_date': rows[i]['date'], 'signal_date': previous['date'],
            'joint': previous['signals']['joint'], 'trend': previous['signals']['trend']}


def build_snapshot(history, now=None, etf=None):
    rows = calculate(history['quotes'], history['bonds'])
    if not rows:
        raise ValueError('红利历史为空')
    latest = rows[-1]
    expected = expected_session(history.get('calendar', []), now)
    current = expected is not None and latest['date'] >= expected and latest['bond_date'] is not None and latest['bond_date'] >= expected
    cut = anniversary(latest['date'])
    sessions = history.get('calendar', [])
    next_month = (date.fromisoformat(latest['date']).replace(day=1) + timedelta(days=32)).strftime('%Y-%m')
    next_monthly = next((d for d in sessions if d.startswith(next_month)), None)
    sources = [
        {'title': '930955价格与滚动PE', 'url': CSI_URL + '?indexCode=930955', 'date': latest['date'], 'estimate': False},
        {'title': 'H20955全收益指数', 'url': CSI_URL + '?indexCode=H20955', 'date': latest['date'], 'estimate': False},
        {'title': '中债国债10年曲线', 'url': 'https://yield.chinabond.com.cn/cbweb-mn/pgxh/showHistory', 'date': latest['bond_date'], 'estimate': False},
        {'title': '交易日历（新浪）', 'url': CALENDAR_URL, 'date': history.get('calendar_retrieved'), 'estimate': False},
    ]
    value = {'code': '930955', 'total_return_code': 'H20955', 'name': '中证红利低波动100',
             'version': VERSION, 'strategy_status': '迭代观察', 'latest': latest,
             'return_6m_observation': {'version': RETURN6_VERSION, 'index': '930955',
                 'lookback': 'six calendar months; previous actual trading close on or before anchor, at most 14 days earlier',
                 'rank': 'expanding midrank including the observation date',
                 'minimum_observations': RETURN6_MIN_OBSERVATIONS,
                 'thresholds_pct': {'strong_buy_lte': 10, 'low_buy_lte': 20, 'sell_gte': 80, 'clear_gte': 90},
                 'role': 'independent observation; v1.3 contribution multiplier unchanged'},
             'history': [r for r in rows if r['date'] >= cut], 'sources': sources,
             'expected_date': expected, 'current': current,
             'calendar': [d for d in sessions if d >= cut],
             'monthly_reference': monthly_reference(rows), 'next_monthly_date': next_monthly,
             'change': {'previous_date': rows[-2]['date'], 'previous': rows[-2]['signals']['joint'],
                        'current': latest['signals']['joint']} if len(rows) > 1 else None,
             'etf': etf, 'provenance': {'input_sha256': sha(DATA / 'history.json') if (DATA / 'history.json').exists() else None,
                                      'rules_sha256': sha(RULES), 'history_rows': len(rows),
                                      'classification': '官方数据本地计算；分红为代理指标；发布前历史仅作预热'}}
    return {'value': value, 'source': '中证指数＋中债官方曲线', 'date': latest['date'],
            'is_estimate': True, 'stale': not current,
            'note': '分红代理不等于现金股息率；PE采用中证历史peg字段；0倍仅暂停新增'}


def etf_subset(now_rows, old_rows, prices, date_now, date_old, price_source):
    members, records = {r['code']: r for r in ETF_MEMBERS}, []
    old = {str(r['基金代码']).zfill(6): number(r['基金份额']) for r in old_rows}
    found = set()
    for raw in now_rows:
        code = str(raw['基金代码']).zfill(6)
        if code not in members:
            continue
        found.add(code)
        shares, previous, price = number(raw['基金份额']), old.get(code), number(prices.get(code))
        ok = shares is not None and previous is not None and price is not None and price > 0
        records.append({**members[code], 'shares_now': shares, 'shares_before': previous,
                        'price': price, 'change_pct': 100 * (shares / previous - 1) if ok and previous > 0 else None,
                        'change_yi': (shares - previous) * price / 1e8 if ok else None,
                        'scale_yi': shares * price / 1e8 if shares is not None and price and price > 0 else None,
                        'status': '完整窗口' if ok else '窗口份额或现价缺失，未计入变化'})
    missing = sorted(members.keys() - found)
    complete = bool(records) and not missing and all(r['change_yi'] is not None for r in records)
    comparable = [r for r in records if r['change_yi'] is not None]
    now_complete = bool(records) and not missing and all(r['scale_yi'] is not None for r in records)
    return {'value': {'members': records, 'verified_members': ETF_MEMBERS, 'missing_codes': missing,
                      'latest_date': date_now, 'prev_date': date_old,
                      'change_yi': sum(r['change_yi'] for r in records) if complete else None,
                      'comparable_change_yi': sum(r['change_yi'] for r in comparable) if comparable else None,
                      'comparable_count': len(comparable), 'coverage_complete': complete,
                      'price_retrieved_at': datetime.now(SHANGHAI).isoformat(),
                      'scale_yi': sum(r['scale_yi'] for r in records) if now_complete else None,
                      'coverage': '已核验4只沪市ETF；不含深市、场外联接；原风格因子的子集'},
            'source': '上交所份额×' + price_source + '现价', 'date': date_now,
            'is_estimate': True, 'stale': not now_complete,
            'note': '份额变化按现价计值，不含价格涨跌；缺失不作为零流入'}


def fetch_history():
    import requests
    import py_mini_racer
    from akshare.tool.trade_date_hist import hk_js_decode
    history_path = DATA / 'history.json'
    history = json.loads(history_path.read_text()) if history_path.exists() else {'quotes': [], 'bonds': {}}
    today = datetime.now(SHANGHAI).date().isoformat()
    quotes = history['quotes']
    # Start on a known actual session; closed-day starts can cause synthetic boundary rows.
    cutoff = (date.fromisoformat(quotes[-1]['date']) - timedelta(days=30)).isoformat() if quotes else '2013-01-04'
    start = next((r['date'] for r in quotes if r['date'] >= cutoff), cutoff)
    new = {}
    requests_meta = []
    for code in ('930955', 'H20955'):
        response = requests.get(CSI_URL, params={'indexCode': code, 'startDate': start.replace('-', ''),
                                 'endDate': today.replace('-', '')}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        new[code] = normalize_csi(payload, code)
        if any(not start <= day <= today for day in new[code]):
            raise ValueError(f'{code} 响应超出请求日期范围，拒绝未来或额外记录')
        raw_path = DATA / 'raw' / f'{code}_{start}_{today}.json'
        atomic_json(raw_path, payload)
        requests_meta.append({'url': response.url, 'file': str(raw_path.relative_to(DATA)), 'sha256': sha(raw_path)})
    merged = {r['date']: r for r in quotes}
    merged.update({r['date']: r for r in paired_quotes(new['930955'], new['H20955'])})
    bonds = dict(history['bonds'])
    bond_start = start if quotes else '2013-01-04'
    while bond_start <= today:
        end = min((date.fromisoformat(bond_start) + timedelta(days=179)).isoformat(), today)
        response = requests.post(CN_URL, params={'startDate': bond_start, 'endDate': end,
                                 'gjqx': '10', 'locale': 'zh_CN'}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            raise ValueError('中债曲线为空')
        for row in payload:
            value, day = number(row.get('tenYear')), row.get('workTime')
            if row.get('qxmc') != '中债国债收益率曲线' or not day or not bond_start <= day <= end or value is None or not 0 < value < 20:
                raise ValueError('中债曲线口径或值异常')
            bonds[day] = value
        raw_path = DATA / 'raw' / f'cn10y_{bond_start}_{end}.json'
        atomic_json(raw_path, payload)
        requests_meta.append({'url': response.url, 'file': str(raw_path.relative_to(DATA)), 'sha256': sha(raw_path)})
        bond_start = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    # Bounded read of the same calendar used by AKShare; decoded locally.
    response = requests.get(CALENDAR_URL, timeout=20)
    response.raise_for_status()
    decoder = py_mini_racer.MiniRacer()
    decoder.eval(hk_js_decode)
    days = decoder.call('d', response.text.split('=')[1].split(';')[0].replace('"', ''))
    sessions = sorted({str(d)[:10] for d in days})
    if not sessions or sessions[-1] < today:
        raise ValueError('交易日历未覆盖当前日期')
    updated = {'quotes': [merged[d] for d in sorted(merged)], 'bonds': bonds,
               'calendar': sessions, 'calendar_retrieved': today}
    calendar_set = set(sessions)
    if any(r['date'] not in calendar_set for r in updated['quotes'] if r['date'] >= start):
        raise ValueError('新增指数记录包含非交易日边界补值')
    calculate(updated['quotes'], bonds)  # Validate before replacing the last complete input.
    atomic_json(history_path, updated)
    atomic_json(DATA / 'refresh-manifest.json', {'retrieved_at': datetime.now(SHANGHAI).isoformat(),
                 'requests': requests_meta, 'history_sha256': sha(history_path), 'rules_sha256': sha(RULES)})
    return updated


def bootstrap(source):
    """One-time explicit import; runtime never reads the source project."""
    source = Path(source)
    validation = json.loads((source / 'output/v1.3/validation.json').read_text())
    manifest = json.loads((source / 'output/v1.3/manifest.json').read_text())
    if validation['status'] != 'passed' or sha(source / 'output/daily_signals_ma250.csv') != manifest['input_sha256']:
        raise ValueError('上游核验或输入哈希不通过')
    mappings, imported = {}, []
    for code in ('930955', 'H20955'):
        mappings[code] = {}
        for suffix in ('warmup', 'perf'):
            path = source / 'data/raw' / f'csi_{code}_{suffix}.json'
            raw = path.read_bytes()
            dest = DATA / 'raw' / path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            mappings[code].update(normalize_csi(json.loads(raw), code))
            imported.append({'file': str(dest.relative_to(DATA)), 'sha256': sha(dest)})
    src_bond = source / 'data/cn10y.csv'
    dest_bond = DATA / 'cn10y-seed.csv'
    dest_bond.write_bytes(src_bond.read_bytes())
    with dest_bond.open() as f:
        bonds = {r['date']: float(r['cn10y_yield']) for r in csv.DictReader(f)}
    history = {'quotes': paired_quotes(mappings['930955'], mappings['H20955']), 'bonds': bonds, 'calendar': []}
    atomic_json(DATA / 'history.json', history)
    imported.append({'file': dest_bond.name, 'sha256': sha(dest_bond)})
    atomic_json(DATA / 'seed-manifest.json', {'imported_at': datetime.now(SHANGHAI).isoformat(), 'files': imported,
                 'source_validation': validation['status'], 'source_signal_sha256': manifest['input_sha256'],
                 'removed_boundary_dates': sorted(REMOVED_BOUNDARIES), 'as_of': manifest['data_asof']})
    return build_snapshot(history)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--bootstrap', type=Path)
    args = parser.parse_args()
    snapshot = bootstrap(args.bootstrap) if args.bootstrap else build_snapshot(fetch_history())
    print(json.dumps({'date': snapshot['date'], 'current': snapshot['value']['current'],
                      'joint': snapshot['value']['latest']['signals']['joint']}, ensure_ascii=False))
