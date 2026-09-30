#!/bin/bash
# check_and_update.sh — 每小时被 launchd 调用，判断 arisk_data.json 是否需要重新抓取
# （macOS 版；路径改为脚本所在目录，python 用 venv）
#
# 判据是「数据里的日期」而不是「文件修改时间」。检查所有日频字段的 date / stale：
#   成交额/换手率(turnover, vol_7d)、ETF(etf_categories)、行业(sector_live)、
#   HV30(hv30)、PE(pe_300)、10Y国债(bond10y)、破净率(below_net_asset)、两融(margin)
#   其中两融为 T+1 发布，只要求到上一个交易日
#   1. JSON 不存在                                   → 更新
#   2. 任一日频字段 date 落后于"应有的最新交易日"      → 更新
#   3. 任一日频字段 stale=true（上次抓取失败复用旧值） → 更新
#   4. 全部到位                                       → 跳过（收盘数据不会再变）
# 月频字段（社融 credit_yoy、基金新发 fund_issuance）不参与判断。
#
# "应有的最新交易日" = 最近一个工作日；当天 18:00 之前算前一个工作日。
# 非交易日判定只看交易所成交额/换手率（turnover）：若更新后它仍落后且 $EXP 已过去一天仍抓不到，
# 判定为非交易日记入 .arisk_nontrading_dates，之后自动跳过；若 $EXP 就是今天则不拉黑，留给下小时重试。
# 其它字段（如 PE、ETF 份额）发布较晚导致的落后只触发重试，不会把交易日误判为非交易日。

DIR="$(cd "$(dirname "$0")" && pwd)"
JSON="$DIR/arisk_data.json"
LOG="$DIR/arisk_update.log"
NONTRADING="$DIR/.arisk_nontrading_dates"
UPDATER="$DIR/run_arisk_update.sh"
PY="$DIR/venv/bin/python"

DAILY_FIELDS="turnover vol_7d etf_categories sector_live hv30 pe_300 bond10y below_net_asset margin dividend_lowvol100"
# T+1 发布的字段：两融（交易所次日早上发布），只要求到"上一个交易日"。
# 注：ETF 份额是当天发布、只是时间不固定（历史上 16:26~22:11 都有），不属于 T+1。
T1_FIELDS="margin"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# expected_date [back]：应有的最新交易日；back=1 时返回它的上一个交易日
expected_date() {
    "$PY" - "$NONTRADING" "${1:-0}" <<'PY'
import sys, datetime
try:
    skip = {l.strip() for l in open(sys.argv[1]) if l.strip()}
except FileNotFoundError:
    skip = set()
now = datetime.datetime.now()
d = now.date()
if now.hour < 18:                              # 收盘+发布缓冲之前，今天还不该有数据
    d -= datetime.timedelta(days=1)
def last_trading(d):
    for _ in range(30):
        if d.weekday() < 5 and d.isoformat() not in skip:
            return d
        d -= datetime.timedelta(days=1)
    return d
d = last_trading(d)
for _ in range(int(sys.argv[2])):
    d = last_trading(d - datetime.timedelta(days=1))
print(d.isoformat())
PY
}

# 某个字段的数据日期（缺失输出空）
field_date() {
    "$PY" - "$JSON" "$1" <<'PY'
import sys, json
try:
    print((json.load(open(sys.argv[1])).get(sys.argv[2]) or {}).get('date') or '')
except Exception:
    print('')
PY
}

# 列出落后或 stale 的日频字段，每行一个「字段(日期[,stale])」；全部到位则无输出
# $1 = 应有交易日；$2 = 上一个交易日（T+1 字段的要求）
lagging_fields() {
    "$PY" - "$JSON" "$1" "$2" "$T1_FIELDS" $DAILY_FIELDS <<'PY'
import sys, json
path, exp, exp_prev, t1, keys = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4].split(), sys.argv[5:]
try:
    d = json.load(open(path))
except Exception:
    d = {}
for k in keys:
    f = d.get(k) or {}
    date, stale = f.get('date') or '', bool(f.get('stale'))
    need = exp_prev if k in t1 else exp
    if k == 'dividend_lowvol100':
        # The dividend page has its own session calendar (including long holidays).
        sys.path.insert(0, __import__('os').path.dirname(path))
        from dividend_monitor import expected_session
        value = f.get('value') or {}
        need = expected_session(value.get('calendar', []))
        etf = value.get('etf') or {}
        stale = stale or need is None or bool(etf.get('stale')) or not etf.get('date')
        if need and etf.get('date', '') < need:
            stale = True
        need = need or exp
    if not date or date < need or stale:
        print(f"{k}({date or '缺失'}{',stale' if stale else ''})")
PY
}

if [ ! -f "$JSON" ]; then
    log "check: json 不存在 → 更新"
    exec "$UPDATER"
fi

EXP=$(expected_date)
EXP_PREV=$(expected_date 1)
LAG=$(lagging_fields "$EXP" "$EXP_PREV" | tr '\n' ' ')

if [ -z "$LAG" ]; then
    log "check: skip — 日频字段均已覆盖应有交易日 $EXP 且无 stale"
    exit 0
fi

log "check: 应有交易日 $EXP，以下字段落后或 stale：${LAG}→ 更新"
"$UPDATER"
RC=$?

if [ "$RC" -ne 0 ]; then
    log "check: 更新脚本失败 (exit $RC)，下小时重试"
    exit "$RC"
fi

NEW=$(field_date turnover)
TODAY=$(date +%F)
if [ -n "$NEW" ] && [[ "$NEW" < "$EXP" ]]; then
    if [[ "$EXP" < "$TODAY" ]]; then
        grep -qxF "$EXP" "$NONTRADING" 2>/dev/null || echo "$EXP" >> "$NONTRADING"
        log "check: 更新后交易所数据日期仍为 $NEW，且 $EXP 已过去一天仍抓不到，判定为非交易日，已记入 $(basename $NONTRADING)"
    else
        log "check: 更新后交易所数据日期仍为 $NEW，$EXP 就是今天，判定为数据源尚未发布，下小时重试（不拉黑）"
    fi
else
    LAG=$(lagging_fields "$EXP" "$EXP_PREV" | tr '\n' ' ')
    if [ -n "$LAG" ]; then
        log "check: 更新完成，仍落后或 stale：${LAG}（数据源可能尚未发布，下小时重试）"
    else
        log "check: 更新完成，日频字段均已到 $EXP"
    fi
fi
