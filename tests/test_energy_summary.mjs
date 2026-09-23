import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
globalThis.HTMLElement=class {};
globalThis.customElements={get:()=>undefined,define:()=>{}};
globalThis.window={};
const {londonMidnight,dateKey,shiftDay,summarize,summarizeChargeHistory,summarizeChargeCostHistory,normalizeHistoricalCharges}=await import(pathToFileURL(process.cwd()+'/custom_components/eon_next_energy/www/energy-summary.js'));
assert.equal(londonMidnight('2026-09-01'),Date.parse('2026-08-31T23:00:00Z'));
assert.equal(londonMidnight('2026-01-01'),Date.parse('2026-01-01T00:00:00Z'));
assert.equal(londonMidnight('2026-03-30')-londonMidnight('2026-03-29'),23*3600000);
assert.equal(londonMidnight('2026-10-26')-londonMidnight('2026-10-25'),25*3600000);
assert.equal(shiftDay('2026-03-01',-1),'2026-02-28');
assert.equal(dateKey(Date.parse('2026-08-31T23:30:00Z')),'2026-09-01');
const rows={energy:[{start:Date.parse('2026-08-31T22:00:00Z'),state:100},{start:Date.parse('2026-08-31T23:00:00Z'),state:2},{start:Date.parse('2026-09-01T00:00:00Z'),state:0},{start:Date.parse('2026-09-01T01:00:00Z'),state:null}]};
assert.equal(summarize(rows,['energy'],'2026-09-01','2026-09-01').sum,2);
assert.equal(summarize(rows,['energy'],'2026-09-01','2026-09-01').hours,2);
assert.equal(summarize(rows,['energy'],'2026-09-01','2026-09-01').offpeakDays.get('2026-09-01'),2);
assert.equal(summarize(rows,['energy'],'2026-09-01','2026-09-01').peakDays.get('2026-09-01'),undefined);
assert.equal(summarize(rows,['missing'],'2026-09-01','2026-09-01').sum,null);
assert.equal(summarize(rows,['energy'],'2026-09-02','2026-09-02').sum,null);
const at=(iso)=>Date.parse(iso)/1000;
const charge=summarizeChargeHistory([
  {s:'0',lu:at('2026-09-21T00:05:00Z')},
  {s:'1000',lu:at('2026-09-21T00:15:00Z')},
  {s:'3000',lu:at('2026-09-21T00:30:00Z')},
  {s:'0',lu:at('2026-09-21T01:00:00Z')},
  {s:'2000',lu:at('2026-09-21T02:00:00Z')},
  {s:'unknown',lu:at('2026-09-21T03:00:00Z')},
]);
assert.equal(charge.get('2026-09-21'),5);
const chargeCost=summarizeChargeCostHistory([
  {s:'0',lu:at('2026-09-21T00:05:00Z')},
  {s:'1000',lu:at('2026-09-21T00:15:00Z')},
  {s:'3000',lu:at('2026-09-21T05:30:00Z')},
  {s:'4000',lu:at('2026-09-21T06:30:00Z')},
],0.069,0.3367);
assert.ok(Math.abs(chargeCost.get('2026-09-21')-(3*0.069+1*0.3367))<1e-9);
const historical=normalizeHistoricalCharges([
  {date:'2026-09-11',energy_kwh:15,rate:'offpeak'},
  {date:'bad',energy_kwh:14,rate:'offpeak'},
  {date:'2026-09-12',energy_kwh:-1,rate:'offpeak'},
]);
assert.deepEqual(historical.get('2026-09-11'),{energy:15,rate:'offpeak'});
assert.equal(historical.size,1);
console.log('Energy summary: London boundaries, DST, month boundaries, and missing-data checks passed.');
