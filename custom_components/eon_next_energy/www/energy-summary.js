/* Read-only energy view. All readings remain inside Home Assistant. */
const TZ = "Europe/London";
const dateKey = (time) => new Intl.DateTimeFormat("en-CA", {timeZone: TZ, year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date(time));
const shiftDay = (day, amount) => new Date(Date.parse(day+"T12:00:00Z")+amount*86400000).toISOString().slice(0,10);
const monthStart = (day) => day.slice(0,7)+"-01";
const londonMidnight = (day) => {
  let time=Date.parse(day+"T00:00:00Z");
  const target=time;
  for(let i=0;i<3;i++) {
    const p=Object.fromEntries(new Intl.DateTimeFormat("en-GB",{timeZone:TZ,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"}).formatToParts(time).map(v=>[v.type,v.value]));
    const represented=Date.parse(`${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}Z`);
    time+=target-represented;
  }
  return time;
};
const number=(value)=>value===null?"—":value.toLocaleString("en-GB",{maximumFractionDigits:2});
const money=(value)=>value===null?"—":value.toLocaleString("en-GB",{style:"currency",currency:"GBP"});
const escapeText=(value)=>String(value).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const displayDate=(day)=>new Intl.DateTimeFormat("en-GB",{day:"numeric",month:"short",year:"numeric",timeZone:TZ}).format(new Date(day+"T12:00:00Z"));
const londonHour=(time)=>Number(new Intl.DateTimeFormat("en-GB",{timeZone:TZ,hour:"2-digit",hourCycle:"h23"}).format(new Date(time)));
function summarize(data, ids, from, to) {
  const start=londonMidnight(from), end=londonMidnight(shiftDay(to,1));
  let sum=0,count=0,latest=null;
  const days=new Map();
  const peakDays=new Map(),offpeakDays=new Map();
  const hours=new Set();
  for(const id of ids) for(const row of data[id]||[]) {
    if(row.start<start || row.start>=end || !Number.isFinite(row.state)) continue;
    sum+=row.state; count++;
    latest=Math.max(latest||0,row.end||row.start+3600000);
    hours.add(row.start);
    const key=dateKey(row.start);
    days.set(key,(days.get(key)||0)+row.state);
    const tariffDays=londonHour(row.start)<7?offpeakDays:peakDays;
    tariffDays.set(key,(tariffDays.get(key)||0)+row.state);
  }
  return {sum:count?sum:null,count,latest,days,peakDays,offpeakDays,hours:hours.size};
}
function summarizeChargeHistory(rows=[]) {
  const days=new Map();let previous=null;
  for(const row of rows) {
    const value=Number(row.s);if(!Number.isFinite(value))continue;
    const increment=previous===null?0:value>=previous?value-previous:value;
    if(increment>0){const key=dateKey(row.lu*1000);days.set(key,(days.get(key)||0)+increment/1000);}
    previous=value;
  }
  return days;
}
class EonEnergySummary extends HTMLElement {
  setConfig(config) {this._config=config;this._from=monthStart(dateKey(Date.now()));this._to=dateKey(Date.now());this._render();}
  getCardSize(){return 11;}
  set hass(hass){
    this._hass=hass;
    const states=Object.values(hass.states);
    const signature=states.filter(s=>s.attributes?.consumption_statistics?.startsWith("eon_next_energy:")).map(s=>s.last_updated).join();
    const liveSignature=states.filter(s=>/^(climate\.|sensor\.|binary_sensor\.)/.test(s.entity_id)&&/(thermostat|hypervolt)/i.test(`${s.entity_id} ${s.attributes?.friendly_name||""}`)).map(s=>`${s.entity_id}:${s.state}:${s.last_updated}`).join();
    if(signature!==this._signature){this._signature=signature;this._liveSignature=liveSignature;this._refresh();}
    else if(liveSignature!==this._liveSignature){this._liveSignature=liveSignature;this._render();}
  }
  connectedCallback(){this._timer=setInterval(()=>this._refresh(),300000);if(this._hass)this._refresh();}
  disconnectedCallback(){clearInterval(this._timer);}
  async _refresh(){
    if(!this._hass||this._busy)return;
    this._busy=true;this._error="";this._render();
    try{
      const ids={electricity:[],gas:[],electricityCost:[],gasCost:[]};
      const freshness=[];
      for(const state of Object.values(this._hass.states)) {
        const attr=state.attributes||{};
        if(!String(attr.consumption_statistics||"").startsWith("eon_next_energy:"))continue;
        const consumption=attr.consumption_statistics.split(/,\s*/).filter(Boolean);
        const costs=String(attr.cost_statistics||"").split(/,\s*/).filter(Boolean);
        const gas=consumption.some(id=>id.startsWith("eon_next_energy:gas_"));
        ids[gas?"gas":"electricity"].push(...consumption.filter(id=>!id.endsWith("_volume")));
        ids[gas?"gasCost":"electricityCost"].push(...costs);
        freshness.push({gas,time:state.state,cv:attr.gas_calorific_value});
      }
      this._ids=ids;this._freshness=freshness;
      const all=[...new Set(Object.values(ids).flat())];
      if(!all.length)throw Error("No E.ON statistics available yet.");
      const now=dateKey(Date.now());
      const first=this._from<monthStart(now)?this._from:monthStart(now);
      const last=this._to>now?this._to:now;
      this._data=await this._hass.callWS({type:"recorder/statistics_during_period",start_time:new Date(londonMidnight(first)).toISOString(),end_time:new Date(londonMidnight(shiftDay(last,1))).toISOString(),statistic_ids:all,period:"hour",types:["state"]});
      this._chargeDays=new Map();
      const session=this._state("sensor","hypervolt session energy","sensor.hypervolt_216454544628214346_hypervolt_session_energy");
      if(session){
        try{
          const history=await this._hass.callWS({type:"history/history_during_period",start_time:new Date(londonMidnight(first)).toISOString(),end_time:new Date(londonMidnight(shiftDay(last,1))).toISOString(),entity_ids:[session.entity_id],minimal_response:true,no_attributes:true,significant_changes_only:false});
          this._chargeDays=summarizeChargeHistory(history[session.entity_id]);
        }catch(_error){/* Energy data remains usable if recorder history is unavailable. */}
      }
      this._loaded={from:this._from,to:this._to};
    }catch(error){this._error="Energy data could not be loaded. Check the E.ON integration, then refresh.";this._data=null;}
    finally{this._busy=false;this._render();}
  }
  _range(from,to){if(this._busy)return;this._from=from;this._to=to;this._refresh();}
  _totals(from,to){return Object.fromEntries(Object.entries(this._ids).map(([key,ids])=>[key,summarize(this._data,ids,from,to)]));}
  _state(domain, phrase, exactId){
    if(!this._hass)return undefined;
    if(exactId&&this._hass.states[exactId])return this._hass.states[exactId];
    return Object.values(this._hass.states).find(state=>state.entity_id.startsWith(domain+".")&&`${state.entity_id} ${state.attributes?.friendly_name||""}`.toLowerCase().includes(phrase));
  }
  _live(){
    const climate=this._state("climate","thermostat","climate.thermostat");
    const temperature=this._state("sensor","thermostat temperature","sensor.thermostat_temperature");
    const humidity=this._state("sensor","thermostat humidity","sensor.thermostat_humidity");
    const power=this._state("sensor","hypervolt ev power","sensor.hypervolt_216454544628214346_hypervolt_ev_power");
    const session=this._state("sensor","hypervolt session energy","sensor.hypervolt_216454544628214346_hypervolt_session_energy");
    const plugged=this._state("sensor","hypervolt car plugged")||this._state("binary_sensor","hypervolt car plugged");
    const readiness=this._state("sensor","hypervolt charging readiness");
    return {climate,temperature,humidity,power,session,plugged,readiness};
  }
  _chart(totals){
    const days=[];for(let d=this._from;d<=this._to;d=shiftDay(d,1))days.push(d);
    const width=900,height=260,left=48,bottom=220,plot=180,step=830/days.length;
    const max=Math.max(1,...days.map(d=>(totals.electricity.days.get(d)||0)+(totals.gas.days.get(d)||0)))*1.1;
    let svg=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Daily electricity and estimated gas consumption stacked in kWh">`;
    for(let i=0;i<=4;i++){const y=bottom-plot*i/4;svg+=`<line x1="${left}" x2="880" y1="${y}" y2="${y}" class="grid"/><text x="40" y="${y+4}" text-anchor="end">${number(max*i/4)}</text>`;}
    days.forEach((day,i)=>{
      const e=totals.electricity.days.get(day),offpeak=totals.electricity.offpeakDays.get(day)||0,peak=totals.electricity.peakDays.get(day)||0,g=totals.gas.days.get(day),ev=this._chargeDays?.get(day)||0,x=left+i*step+step*.15,bw=step*.7,oh=offpeak/max*plot,ph=peak/max*plot,gh=(g||0)/max*plot;
      const title=`${displayDate(day)} · Off-peak electricity ${number(offpeak)} kWh · Peak electricity ${number(peak)} kWh · Gas ${g===undefined?"no data":number(g)+" kWh (estimated)"}${ev?` · EV charging ${number(ev)} kWh`:""}`;
      if(e!==undefined){svg+=`<rect x="${x}" y="${bottom-oh}" width="${bw}" height="${oh}" class="electricity-offpeak"><title>${title}</title></rect>`;svg+=`<rect x="${x}" y="${bottom-oh-ph}" width="${bw}" height="${ph}" class="electricity-peak"><title>${title}</title></rect>`;}
      if(g!==undefined)svg+=`<rect x="${x}" y="${bottom-oh-ph-gh}" width="${bw}" height="${gh}" class="gas"><title>${title}</title></rect>`;
      if(ev)svg+=`<circle cx="${x+bw/2}" cy="${Math.max(7,bottom-oh-ph-gh-7)}" r="4" class="ev-marker"><title>${displayDate(day)} · EV charging ${number(ev)} kWh</title></circle>`;
      if(e===undefined&&g===undefined)svg+=`<circle cx="${x+bw/2}" cy="${bottom-3}" r="2" class="missing"><title>${title}</title></circle>`;
      if(i%Math.max(1,Math.ceil(days.length/12))===0||i===days.length-1)svg+=`<text x="${x+bw/2}" y="242" text-anchor="middle">${day.slice(8)}/${day.slice(5,7)}</text>`;
    });
    return svg+"</svg>";
  }
  _render(){
    if(!this.shadowRoot)this.attachShadow({mode:"open"});
    if(!this._from)return;
    const ready=this._data&&this._ids&&this._loaded?.from===this._from&&this._loaded?.to===this._to;
    const totals=ready?this._totals(this._from,this._to):null;
    const now=dateKey(Date.now()),mtd=ready?this._totals(monthStart(now),now):null;
    const combined=(t)=>t.electricityCost.sum!==null&&t.gasCost.sum!==null?t.electricityCost.sum+t.gasCost.sum:null;
    const cv=this._freshness?.find(f=>f.gas)?.cv;
    const live=this._live();
    const currentTemperature=live.temperature?.state??live.climate?.attributes?.current_temperature;
    const targetTemperature=live.climate?.attributes?.temperature;
    const humidity=live.humidity?.state??live.climate?.attributes?.current_humidity;
    const heatingState=live.climate?.attributes?.hvac_action||live.climate?.state;
    const reading=(state)=>state&&state.state!=="unknown"&&state.state!=="unavailable"?`${escapeText(state.state)}${state.attributes?.unit_of_measurement?" "+escapeText(state.attributes.unit_of_measurement):""}`:"—";
    const freshness=(this._freshness||[]).map(f=>`${f.gas?"Gas":"Electricity"}: ${Number.isNaN(Date.parse(f.time))?"waiting for data":new Intl.DateTimeFormat("en-GB",{timeZone:TZ,dateStyle:"medium",timeStyle:"short"}).format(new Date(f.time))}`).join(" · ");
    this.shadowRoot.innerHTML=`<style>
    :host{display:block;color:var(--primary-text-color);font-family:var(--paper-font-body1_-_font-family,system-ui)}ha-card{padding:24px;border-radius:18px}h1{font-size:25px;margin:0 0 4px}h2{font-size:18px;margin:26px 0 12px}.muted{color:var(--secondary-text-color);font-size:13px;line-height:1.6}.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:end;margin:20px 0 12px}label{font-size:12px;display:grid;gap:5px}input,button{font:inherit;color:inherit;background:var(--secondary-background-color);border:1px solid var(--divider-color);border-radius:8px;padding:9px 12px}button{cursor:pointer}button:disabled{opacity:.45;cursor:wait}.chips{display:flex;gap:8px;flex-wrap:wrap}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:12px 0}.live-cards{grid-template-columns:repeat(2,1fr)}.metric{padding:16px;border:1px solid var(--divider-color);border-radius:12px}.metric strong{display:block;font-size:26px;margin-top:8px}.range{font-size:16px;font-weight:600;margin:18px 0}.legend{display:flex;gap:20px;flex-wrap:wrap;font-size:13px}.dot{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px}.electricity-peak,.peak-dot{fill:#53a9f7;background:#53a9f7}.electricity-offpeak,.offpeak-dot{fill:#3657d6;background:#3657d6}.gas,.gas-dot{fill:#f1a55b;background:#f1a55b}.ev-marker,.ev-dot{fill:#b66cff;background:#b66cff}.ev-dot{border-radius:50%}.missing{fill:var(--disabled-text-color)}svg{width:100%;height:auto}svg text{fill:var(--secondary-text-color);font-size:11px}.grid{stroke:var(--divider-color);stroke-width:1}table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:12px 4px;border-bottom:1px solid var(--divider-color);text-align:right}td:first-child,th:first-child{text-align:left}tfoot{font-weight:700}a{color:var(--primary-color)}.error{padding:18px;color:var(--error-color)}@media(max-width:600px){ha-card{padding:16px}.cards{grid-template-columns:1fr}.metric strong{font-size:23px}.controls input{max-width:145px}}</style>
    <ha-card><h1>Home energy</h1><div class="muted">Electricity + estimated gas · Europe/London</div>
    <div class="controls"><label>From<input id="from" type="date" value="${this._from}" max="${now}"></label><label>To<input id="to" type="date" value="${this._to}" max="${now}"></label><button id="apply" ${this._busy?"disabled":""}>Apply dates</button><button id="refresh" ${this._busy?"disabled":""}>Refresh</button></div>
    <div class="chips"><button data-preset="today">Today</button><button data-preset="yesterday">Yesterday</button><button data-preset="month">Month so far</button><button data-preset="lastmonth">Last month</button></div>
    <div class="range">${displayDate(this._from)}${this._from===this._to?"":" – "+displayDate(this._to)}</div>
    ${this._error?`<div class="error">${escapeText(this._error)}</div>`:""}
    ${!ready?'<p class="muted">Loading recorded energy…</p>':`
    <div class="legend"><span><i class="dot offpeak-dot"></i>Electricity · off-peak 00:00–07:00</span><span><i class="dot peak-dot"></i>Electricity · peak</span><span><i class="dot gas-dot"></i>Gas (estimated)</span><span><i class="dot ev-dot"></i>EV charge day</span><span>kWh per day</span></div>
    ${this._chart(totals)}
    <h2>Totals · selected dates</h2><table><thead><tr><th>Source</th><th>Consumption</th><th>Cost</th></tr></thead><tbody>
    <tr><td>Electricity</td><td>${number(totals.electricity.sum)} kWh</td><td>${money(totals.electricityCost.sum)}</td></tr>
    <tr><td>Gas · estimated</td><td>${number(totals.gas.sum)} kWh</td><td>${money(totals.gasCost.sum)}</td></tr></tbody>
    <tfoot><tr><td>Total · estimated</td><td>${number(totals.electricity.sum!==null&&totals.gas.sum!==null?totals.electricity.sum+totals.gas.sum:null)} kWh</td><td>${money(combined(totals))}</td></tr></tfoot></table>
    <h2>Month so far · ${new Intl.DateTimeFormat("en-GB",{month:"long",year:"numeric",timeZone:TZ}).format(new Date())}</h2>
    <div class="muted">${displayDate(monthStart(now))} – ${displayDate(now)} · independent of selected dates</div>
    <div class="cards"><div class="metric"><span>Electricity</span><strong>${money(mtd.electricityCost.sum)}</strong><span class="muted">${number(mtd.electricity.sum)} kWh</span></div><div class="metric"><span>Gas · estimated</span><strong>${money(mtd.gasCost.sum)}</strong><span class="muted">${number(mtd.gas.sum)} kWh</span></div><div class="metric"><span>Total · estimated</span><strong>${money(combined(mtd))}</strong><span class="muted">Includes recorded standing charges</span></div></div>
    <div class="muted">Recorded hours this month: electricity ${mtd.electricity.hours}; gas ${mtd.gas.hours}. Delayed or missing readings make totals partial. A dash means no data, not zero.</div>`}
    <h2>Live home</h2><div class="cards live-cards">
      <div class="metric"><span>Nest · Kitchen</span><strong>${currentTemperature==null?"—":escapeText(currentTemperature)+" °C"}</strong><span class="muted">Target ${targetTemperature==null?"—":escapeText(targetTemperature)+" °C"} · ${heatingState?escapeText(String(heatingState).replace(/^./,c=>c.toUpperCase())):"—"} · Humidity ${humidity==null?"—":escapeText(humidity)+"%"}</span></div>
      <div class="metric"><span>Hypervolt</span><strong>${reading(live.power)}</strong><span class="muted">${live.plugged?escapeText(live.plugged.attributes?.friendly_name?.replace(/^Hypervolt /,"")||"Car plugged")+": "+escapeText(live.plugged.state):"Car connection —"} · Charging ${live.readiness?escapeText(live.readiness.state):"—"} · Session ${reading(live.session)}</span></div>
    </div>
    <p class="muted">${escapeText(freshness)}</p><p class="muted">Gas estimate: m³ × 1.02264 × ${escapeText(cv||"calorific value")} ÷ 3.6. Supplier billing may differ. Change the calorific value in E.ON integration settings. Hypervolt is included in electricity and is not added again.</p>
    <a href="/energy/overview">Open standard Energy dashboard →</a></ha-card>`;
    this.shadowRoot.querySelector('#apply').onclick=()=>{
      const from=this.shadowRoot.querySelector('#from').value,to=this.shadowRoot.querySelector('#to').value;
      if(!from||!to||from>to||to>now||Date.parse(to)-Date.parse(from)>92*86400000){this._error="Choose a valid range of up to 93 days, ending today or earlier.";this._render();return;}
      this._range(from,to);
    };
    this.shadowRoot.querySelector('#refresh').onclick=()=>this._refresh();
    this.shadowRoot.querySelectorAll('[data-preset]').forEach(button=>{button.disabled=this._busy;button.onclick=()=>{
      const preset=button.dataset.preset;
      if(preset==='today')this._range(now,now);
      if(preset==='yesterday')this._range(shiftDay(now,-1),shiftDay(now,-1));
      if(preset==='month')this._range(monthStart(now),now);
      if(preset==='lastmonth'){const end=shiftDay(monthStart(now),-1);this._range(monthStart(end),end);}
    };});
  }
}
if(!customElements.get('eon-energy-summary'))customElements.define('eon-energy-summary',EonEnergySummary);
window.customCards=window.customCards||[];
window.customCards.push({type:"eon-energy-summary",name:"E.ON energy summary",description:"Stacked energy, selected-period costs and month-to-date totals."});
export {londonMidnight, dateKey, shiftDay, summarize, summarizeChargeHistory};
