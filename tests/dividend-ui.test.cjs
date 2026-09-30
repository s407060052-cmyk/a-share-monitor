// Exercise user-visible freshness gates and independent date/refresh state.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const root = require('node:path').resolve(__dirname, '..');
const fixture = JSON.parse(fs.readFileSync(root + '/arisk_data.json')).dividend_lowvol100;
// A deterministic fresh state is needed even when the scheduled live feed is stale.
fixture.stale = false;
fixture.value.current = true;
Object.assign(fixture.value.latest, {price_return_6m_pct:-6.48,price_return_6m_base_date:'2026-03-30',
  price_return_6m_rank_pct:16,price_return_6m_rank_n:3223,price_return_6m_signal:'low_buy'});
Object.assign(fixture.value.history.at(-1), {price_return_6m_pct:-6.48,price_return_6m_base_date:'2026-03-30',
  price_return_6m_rank_pct:16,price_return_6m_rank_n:3223,price_return_6m_signal:'low_buy'});
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {textContent:'',innerHTML:'',dataset:{},style:{},value:'',classList:{contains:()=>false}});
  return elements.get(id);
}
const context = vm.createContext({
  window:{addEventListener(){}}, document:{getElementById:element},
  esc:String, URL, Date, Intl, Number, AbortSignal, DM_INPUTS:[], setInterval(){},
});
vm.runInContext(fs.readFileSync(root + '/dividend_monitor.js', 'utf8'), context);
const monitor = context.window.DividendMonitor;
monitor.expected = () => fixture.date;
monitor.wrapper = structuredClone(fixture);
monitor.render();
assert.equal(element('div-status').dataset.state, 'current');
assert.equal(element('div-joint').textContent, fixture.value.latest.signals.joint.toFixed(1).replace('.0','')+'倍');
assert.match(element('div-return6-signal').textContent, /信号|观察/);
assert.equal(element('div-return6-signal').dataset.state, 'low_buy');
assert.equal(element('div-return6-signal').textContent, '低估买入信号');
monitor.wrapper.stale = true;
monitor.render();
assert.equal(element('div-status').dataset.state, 'history');
assert.equal(element('div-joint').textContent, '—');
assert.match(element('div-status').textContent, /当前定投建议已撤下/);
assert.equal(element('div-return6-signal').textContent, '当前信号待核');
monitor.selectDate(fixture.value.history[0].date);
assert.match(element('div-status').textContent, /历史观察/);
assert.match(element('div-return6-signal').textContent, /历史观察|样本不足/);
assert.notEqual(element('div-joint').textContent, '—');
assert.equal(context.window._D, undefined);
monitor.wrapper = structuredClone(fixture);
monitor.wrapper.value.latest.pe = null;
monitor.wrapper.value.latest.signals.joint = 0;
monitor.wrapper.value.history.at(-1).pe = null;
monitor.wrapper.value.history.at(-1).signals.joint = 0;
monitor.latestDate();
assert.equal(element('div-joint').textContent, '0倍');
assert.match(element('div-action').textContent, /暂停新增，PE待核/);
(async () => {
  monitor.wrapper = structuredClone(fixture);
  monitor.selectDate(fixture.value.history[0].date);
  const selected = monitor.date;
  context.fetch = async () => ({ok:true,json:async()=>({dividend_lowvol100:structuredClone(fixture)})});
  await monitor.refresh();
  assert.equal(monitor.date, selected);
  monitor.latestDate();
  context.fetch = async () => {throw new Error('timeout');};
  await monitor.refresh();
  assert.equal(element('div-joint').textContent, '—');
  assert.equal(element('div-status').dataset.state, 'history');
  assert.equal(monitor.wrapper.value.latest.date, fixture.date);
  console.log('Dividend UI: freshness, historical selection, PE guard and refresh checks passed.');
})().catch(error => {console.error(error);process.exitCode = 1;});
