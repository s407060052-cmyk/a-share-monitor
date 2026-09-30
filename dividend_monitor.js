/* Dedicated page state. Never changes the original dashboard data or decisions. */
window.DividendMonitor = {
  wrapper:null, date:null, sequence:0, failed:false,
  fmt(v, suffix='', digits=2){return Number.isFinite(v)?v.toFixed(digits)+suffix:'—';},
  text(id, value){document.getElementById(id).textContent=value;},
  safeLink(url){try{const u=new URL(url);return u.protocol==='https:'?esc(u.href):'#';}catch(e){return'#';}},
  async refresh(){
    const sequence=++this.sequence;
    let wrapper=null;
    for(const url of ['arisk_data.json?_div='+Date.now(),'http://localhost:8899/dividend']){
      try{
        const response=await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(6000)});
        if(!response.ok)continue;
        const payload=await response.json();
        wrapper=payload.dividend_lowvol100||('value' in payload?payload:null);
        if(wrapper)break;
      }catch(e){}
    }
    if(sequence!==this.sequence)return;
    this.failed=!wrapper;
    if(wrapper){
      const oldDate=this.wrapper?.value?.latest?.date;
      this.wrapper=wrapper;
      if(!this.date||this.date===oldDate)this.date=wrapper.value?.latest?.date||null;
    }
    this.render();
  },
  selectDate(day){this.date=day;this.render();},
  latestDate(){this.date=this.wrapper?.value?.latest?.date||null;this.render();},
  expected(sessions){
    const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',hourCycle:'h23'}).formatToParts(new Date()).map(p=>[p.type,p.value]));
    const day=parts.year+'-'+parts.month+'-'+parts.day;
    const cutoff=new Date(day+'T00:00:00Z');
    if(+parts.hour<18)cutoff.setUTCDate(cutoff.getUTCDate()-1);
    const iso=cutoff.toISOString().slice(0,10);
    if(!sessions?.length||sessions[sessions.length-1]<iso)return null;
    return sessions.filter(d=>d<=iso).at(-1)||null;
  },
  render(){
    const value=this.wrapper?.value;
    if(!value?.latest){
      this.text('div-status','红利数据尚未建立或暂不可读取，当前不提供定投建议。');
      this.text('div-joint','—');this.text('div-trend','—');
      this.text('div-action','等待完整指数与国债数据');
      document.getElementById('div-status').dataset.state='unavailable';
      this.renderReturn6(null,false,false);
      this.renderMacro();return;
    }
    const all=value.history||[], last=value.latest;
    const selected=all.find(r=>r.date===this.date)||last;
    this.date=selected.date;
    const historical=selected.date!==last.date;
    const expected=this.expected(value.calendar);
    const current=!this.failed&&!this.wrapper.stale&&!!expected&&last.date>=expected&&last.bond_date>=expected;
    const usable=current||historical;
    const selector=document.getElementById('div-date');
    const options=all.map(r=>r.date).reverse();
    if(selector.dataset.version!==options.join(',')){
      selector.innerHTML=options.map(d=>'<option value="'+d+'">'+d+(d===last.date?' · 最新':' · 历史')+'</option>').join('');
      selector.dataset.version=options.join(',');
    }
    selector.value=selected.date;
    this.text('div-status',historical?'历史观察 · '+selected.date+'，以下倍数只解释该日规则，不作为当前行动建议。':
      current?'当前观察 · '+last.date+'，联合规则为候选参考；执行前需核对预算。':
      '历史观察 · 数据停留在 '+last.date+'，'+(expected?'应有交易日为 '+expected:'交易日历覆盖待核')+'。当前定投建议已撤下。'+(value.update_error?' 更新失败：'+value.update_error:''));
    document.getElementById('div-status').dataset.state=current&&!historical?'current':'history';
    document.getElementById('div-decision-title').textContent=historical?'该日定投规则 · 历史观察':'当前定投参考';
    const signal=selected.signals;
    this.text('div-joint-label',historical?'历史联合规则 · 主候选':'联合规则 · 主候选');
    this.text('div-joint',usable?this.fmt(signal.joint,'倍',1).replace('.0倍','倍'):'—');
    this.text('div-trend',usable?this.fmt(signal.trend,'倍',1).replace('.0倍','倍'):'—');
    const action=!Number.isFinite(selected.pe)||selected.pe<=0?'暂停新增，PE待核':signal.joint===0?'暂停新增':signal.joint<1?'减慢新增节奏':signal.joint===1?'基础节奏':'加倍候选，需核对储备';
    this.text('div-action',usable?(historical?'历史规则：':'')+action+' · 0倍不卖出底仓':'数据待更新，仅保留历史指标与图表');
    this.text('div-trend-note',signal.trend_note);
    document.getElementById('div-reasons').innerHTML=(signal.reasons||[]).map(s=>'<li>'+esc(s)+'</li>').join('');
    const change=value.change;
    this.text('div-change',historical?'历史选择不改变其他分页的数据和判断。':change?
      '档位比较：'+change.previous_date+' '+this.fmt(change.previous,'倍',1)+' → '+last.date+' '+this.fmt(change.current,'倍',1)+(change.previous===change.current?'，档位未变。':'，档位变化，需复核触发条件。'):'');
    const monthly=value.monthly_reference;
    this.text('div-monthly',historical?'该日信号最早下一交易日参考；月度定投使用月初首个交易日前一交易日信号。':
      monthly?'本月规则留档：'+monthly.execution_date+' 定投参考 '+monthly.signal_date+' 收盘信号（联合'+this.fmt(monthly.joint,'倍',1)+'）；仅为历史参考，不表示已执行。'+
        (value.next_monthly_date?'下次月度复核：'+value.next_monthly_date+'。':'下次交易日待日历更新。'):'月度参考尚未成熟。');
    this.renderReturn6(selected,usable,historical);
    const indicators=[
      ['930955价格指数',this.fmt(selected.price,'点'),'中证指数 · '+selected.date],
      ['H20955全收益指数',this.fmt(selected.tr,'点'),'含分红再投资 · '+selected.date],
      ['滚动PE',this.fmt(selected.pe,'倍'),'中证历史peg字段 · '+selected.date],
      ['PE历史分位',this.fmt(selected.pe_pct,'%'),'有效样本 '+selected.pe_n+' · '+(selected.pe_n>=756?'成熟':'不足756，不用于分位规则')],
      ['分红代理',this.fmt(selected.dy_proxy,'%'),'由两指数计算 · 非现金股息率'],
      ['代理利差',this.fmt(selected.spread,'个百分点'),'中债10年 '+this.fmt(selected.cn10y,'%')+' · '+(selected.bond_date||'日期待核')],
      ['价格MA250偏离',this.fmt(selected.price_bias250_pct,'%'),'MA250 '+this.fmt(selected.price_ma250,'点')],
      ['MA250方向',this.fmt(selected.price_ma250_change20_pct,'%'),'较20个交易日前 · '+(Number.isFinite(selected.price_ma250_change20_pct)?selected.price_ma250_change20_pct<0?'下降':'未下降':'不足270日')],
    ];
    document.getElementById('div-metrics').innerHTML=indicators.map(([label,reading,note])=>'<div class="div-metric"><div class="div-metric-label">'+esc(label)+'</div><div class="div-metric-value">'+esc(reading)+'</div><div class="div-metric-meta">'+esc(note)+'</div></div>').join('');
    document.getElementById('div-sources').innerHTML='<div>最新采集来源（所选历史指标日期见上方卡片）</div>'+(value.sources||[]).map(s=>'<div><a href="'+this.safeLink(s.url)+'" target="_blank" rel="noopener">'+esc(s.title)+'</a> · '+esc(s.date||'日期待核')+'</div>').join('')+
      '<div>历史观察 '+selected.date+' · PE分位样本 '+selected.pe_n+' · 分红代理分位样本 '+selected.dy_n+'（'+this.fmt(selected.dy_pct,'%')+'）</div>'+
      '<div>规则版本 '+esc(value.version)+' · 历史 '+value.provenance.history_rows+' 日；输入哈希 '+esc(value.provenance.input_sha256||'待核')+'</div>';
    this.renderETF(value.etf, expected);
    this.renderMacro();
    if(document.getElementById('tab-dividend').classList.contains('active'))this.renderCharts(all.filter(r=>r.date<=selected.date),selected);
  },
  renderReturn6(row, usable, historical){
    const reading=row?.price_return_6m_pct;
    const rank=row?.price_return_6m_rank_pct;
    const n=row?.price_return_6m_rank_n||0;
    const code=row?.price_return_6m_signal||'unavailable';
    const names={strong_buy:'强烈买入信号',low_buy:'低估买入信号',neutral:'中性观察',sell:'高估卖出信号',clear:'清仓信号'};
    const mature=n>=756&&Number.isFinite(rank);
    const active=usable&&mature&&names[code];
    this.text('div-return6-value',Number.isFinite(reading)?(reading>0?'+':'')+this.fmt(reading,'%'):'—');
    this.text('div-return6-base',row?.price_return_6m_base_date?'930955价格 · '+row.price_return_6m_base_date+' → '+row.date:'不足六个月的价格历史');
    this.text('div-return6-rank',this.fmt(rank,'%'));
    this.text('div-return6-samples','截至该日有效样本 '+n+' 个 · '+(mature?'达到756个判定门槛':'未达到756个判定门槛'));
    this.text('div-return6-signal',active?(historical?'历史观察 · ':'')+names[code]:
      !usable?'当前信号待核':mature?'观察信号待核':'样本不足，暂不判定');
    document.getElementById('div-return6-signal').dataset.state=active?code:'unavailable';
    const marker=document.getElementById('div-return6-marker');
    const hasRank=Number.isFinite(rank);
    marker.style.display=hasRank?'block':'none';
    if(hasRank){
      marker.style.left=Math.max(0,Math.min(100,rank))+'%';
      marker.dataset.rank=this.fmt(rank,'%');
      marker.dataset.edge=rank<10?'start':rank>90?'end':'middle';
      marker.title='历次半年涨跌幅中的位置：'+this.fmt(rank,'%');
    }
    this.text('div-return6-note',(!usable?'数据陈旧，仅保留历史数值；':historical?'所选为历史日期，不代表当前操作；':'当日收盘后的独立观察；')+
      '分位≤10%为强烈买入，≤20%为低估买入，≥90%为清仓，≥80%为高估卖出。'+
      '仅衡量930955近半年价格涨跌幅在当时已知历史中的位置，不能单独证明内在估值高低；不改变v1.3定投倍数，也不自动生成交易。');
  },
  renderCharts(rows, selected){
    const period=rows.length?rows[0].date+' 至 '+selected.date+' · '+rows.length+'个交易观察日':'暂无历史';
    this.text('div-history-period',period);
    if(typeof Chart==='undefined'){this.text('div-chart-note','图表库暂不可用；指标与定投规则仍可读取。');return;}
    this.text('div-chart-note','图中均为各日收盘后的观察信号，最早下一交易日参考；不代表每日执行定投。');
    const make=(id,datasets,min,max)=>mkChart(id,{type:'line',data:{labels:rows.map(r=>r.date),datasets},options:{responsive:true,maintainAspectRatio:false,animation:false,
      interaction:{mode:'index',intersect:false},plugins:{legend:{labels:{font:{size:11},boxWidth:14}},tooltip:{callbacks:{label:c=>c.dataset.label+'：'+(Number.isFinite(c.parsed.y)?c.parsed.y.toFixed(2):'—')}}},
      scales:{x:{ticks:{maxTicksLimit:5,font:{size:10}},grid:{display:false}},y:{min,max,ticks:{font:{size:10}},grid:{color:'rgba(0,0,0,.05)'}}}}});
    const series=(name,key,color,extra={})=>({label:name,data:rows.map(r=>key.startsWith('signals.')?r.signals[key.slice(8)]:r[key]),borderColor:color,backgroundColor:color,pointRadius:0,borderWidth:1.8,spanGaps:false,...extra});
    make('div-price-chart',[series('930955价格','price','#1d5fd4'),series('价格MA250','price_ma250','#d97706',{borderDash:[5,3]})]);
    make('div-rank-chart',[series('PE分位','pe_pct','#1d5fd4'),series('分红代理分位','dy_pct','#0a7c52'),
      series('近半年涨跌幅分位','price_return_6m_rank_pct','#7c3aed')],0,100);
    make('div-signal-chart',[series('联合候选','signals.joint','#1d5fd4',{stepped:true}),series('趋势保护','signals.trend','#0a7c52',{stepped:true,borderDash:[5,3]})],0,4);
  },
  renderETF(wrapper,expected){
    const value=wrapper?.value;
    if(!value){this.text('div-etf-summary','ETF份额暂不可用；不影响红利温度计。');document.getElementById('div-etf-rows').innerHTML='';return;}
    const stale=wrapper.stale||!expected||wrapper.date<expected;
    this.text('div-etf-summary',value.latest_date+' 对比 '+(value.prev_date||'窗口日期缺失')+' · '+value.coverage+'。'+
      (stale?' 份额陈旧或抓取不完整，当前变化合计待核。':value.coverage_complete?' 现价计值变化 '+this.fmt(value.change_yi,'亿元')+'。':
        '窗口覆盖不完整：'+value.comparable_count+'只可比产品变化 '+this.fmt(value.comparable_change_yi,'亿元')+'；全品类变化合计不输出。')+
      ' 来源：'+wrapper.source+'。'+(this.date!==this.wrapper.value.latest.date?'当前选择为历史指标日；本表仍是最新ETF快照。':'')+
      (value.missing_codes?.length?'缺失产品：'+value.missing_codes.join('、')+'。':'')+
      (value.price_retrieved_at?'现价抓取于 '+value.price_retrieved_at.slice(0,16).replace('T',' ')+'，价格日期未逐只核验，变化为近似计值。':''));
    document.getElementById('div-etf-rows').innerHTML=value.members.map(r=>'<tr><td><a href="'+this.safeLink(r.url)+'" target="_blank" rel="noopener">'+esc(r.code)+' '+esc(r.name)+'</a></td><td>'+this.fmt(r.scale_yi)+'</td><td>'+this.fmt(r.change_pct,'%')+'</td><td>'+this.fmt(r.change_yi)+'</td><td>'+esc(r.status)+(stale?' · 快照待核':'')+'</td></tr>').join('');
  },
  renderMacro(){
    const d=window._D;
    const bad=!d||DM_INPUTS.some(k=>!d.meta?.[k]?.source||!d.meta?.[k]?.date||isStaleMeta(k,d.meta[k]))||d.meta?.credit_yoy?.source==='M2兜底';
    const read=id=>document.getElementById(id)?.textContent||'—';
    const cards=bad?[['大盘状态','数据待核'],['拥挤度','暂不引用'],['原模型仓位区间','暂不引用']]:
      [['信用环境',read('l1-result-val')],['大盘拥挤度',read('pca-score-dm')+'分'],['原模型仓位区间',read('dm-pos-val')]];
    document.getElementById('div-macro').innerHTML=cards.map(([label,value])=>'<div class="div-metric"><div class="div-metric-label">'+esc(label)+'</div><div class="div-metric-value">'+esc(value)+'</div></div>').join('');
    this.text('div-macro-source',bad?'大盘输入缺失、陈旧或口径无效，不能据此确认总预算。红利候选倍数保持独立。':
      '引用现有决策模型：'+DM_INPUTS.map(k=>META_LABEL[k]+' '+d.meta[k].source+' '+d.meta[k].date).join('；')+'。'+(this.wrapper?.value&&this.date!==this.wrapper.value.latest.date?'历史红利观察与当前大盘状态分别展示。':''));
  }
};
window.addEventListener('hashchange',()=>{const n=location.hash.slice(1);if(['classic','decision','dividend'].includes(n))switchTab(n);});
window.addEventListener('load',()=>{
  const n=location.hash.slice(1);
  if(['classic','decision','dividend'].includes(n))switchTab(n);
  DividendMonitor.refresh();
});
setInterval(()=>{if(document.visibilityState==='visible')DividendMonitor.render();},60000);
