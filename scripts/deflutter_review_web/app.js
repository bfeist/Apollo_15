'use strict';
const $ = id => document.getElementById(id);
const el = (tag, text, cls) => { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; };
let token='', overview=null, queue=null, index=0, finding='', preview=null, lastQueue='', busy=false;
let context=null, buffers=[], sources=[], gains=[], playing=false, offset=0, started=0, version=0, loadedSample='';
let inventorySignature='', tapeSignature='', queueSignature='';
const fmt = seconds => `${Math.floor(seconds/60)}:${String(Math.floor(seconds%60)).padStart(2,'0')}`;
const stamp = seconds => `${Math.floor(seconds/3600)}:${String(Math.floor(seconds/60)%60).padStart(2,'0')}:${String(Math.floor(seconds%60)).padStart(2,'0')}`;
const notice = message => { $('notice').hidden=!message; $('notice').textContent=message||''; };
async function api(path, data){ const response=await fetch(path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':token},body:JSON.stringify(data)}); const result=await response.json(); if(!response.ok)throw Error(result.error||response.statusText); return result; }
function action(id, fn){ $(id).addEventListener('click',()=>{notice('');Promise.resolve().then(fn).catch(e=>notice(e.message));}); }
function selectedTapes(){return [...document.querySelectorAll('[data-tape]:checked')].map(n=>n.dataset.tape);}
function options(){return {budget:Number($('budget').value),seconds:Number($('seconds').value),seed:$('seed').value,kind:$('kind').value,blinded:$('blind').checked,tapes:selectedTapes()};}
function tagList(node, values, missing=false){node.replaceChildren(...values.map(s=>el('span',s,'tag'+(missing?' missing':''))));}
function stateLabel(ok,yes,no){return el('span',ok?yes:no,'state '+(ok?'good':'bad'));}
function metric(title,value,detail){const n=el('div',undefined,'metric');n.append(el('div',title,'eyebrow'),el('div',String(value),'number'),el('small',detail));return n;}
function renderOverview(data){
  overview=data;token=data.token;
  $('mode').textContent=data.demo?'SYNTHETIC DEMO · NEVER PRODUCTION':'LOCAL · READ-ONLY INPUTS'; $('mode').classList.toggle('warn',data.demo);
  $('scanned').textContent=data.scanned_at?'Inventory · '+new Date(data.scanned_at).toLocaleString():'Reading artifact inventory…';
  $('tape-count').textContent=`${data.tapes.length} CANONICAL TAPES`;
  const signature=JSON.stringify([data.tapes,data.scanned_at,data.roots,data.errors]);
  if(signature!==inventorySignature){inventorySignature=signature;
  const total=k=>data.tapes.reduce((a,t)=>a+(t[k]||0),0);
  $('metrics').replaceChildren(metric('CANONICAL INVENTORY',data.tapes.length,'All tapes remain visible'),metric('STAGE 03 READY',`${total('denoised_channels')} / ${total('expected_channels')}`,'Reusable denoised channels'),metric('CARRIER SURVEYS',`${data.tapes.filter(t=>t.survey_complete).length} / ${data.tapes.length}`,'Complete and current'),metric('PRODUCTION ACCEPTANCE',total('production_channels'),'Channels passing the live CLI gate'));
  $('tapes').replaceChildren();
  for(const t of data.tapes){
    const tr=el('tr'), name=el('td',t.tape.replace('A15_',''));if(t.newly_inventoried)name.append(el('small',' + NEW'));
    tr.append(name);let td=el('td');td.append(stateLabel(t.denoised_channels===t.expected_channels&&t.expected_channels>0,`${t.denoised_channels}/${t.expected_channels} ready`,`${t.denoised_channels}/${t.expected_channels} ready`));tr.append(td);
    td=el('td');td.append(stateLabel(t.survey_complete,'Complete','Missing / incomplete'));tr.append(td);
    td=el('td');td.append(stateLabel(t.plan_complete,'Current',t.pilot_plan_available?'Pilot only':'Missing / stale'));tr.append(td);
    td=el('td');td.append(stateLabel(t.map_checks_passed===t.expected_channels&&t.expected_channels>0,`${t.map_checks_passed}/${t.expected_channels} passed`,`${t.map_checks_passed}/${t.expected_channels} passed`));tr.append(td);
    td=el('td');const bar=el('div',undefined,'coverage-bar'), cov=t.coverage||{},sum=Object.values(cov).reduce((a,b)=>a+b,0),colors={direct_numeric:'#528369',transfer_group:'#8d9e75',proposal:'#d7ae68',bypass:'#bec2ba',transition_fades:'#a390a3'};
    for(const [key,color] of Object.entries(colors)){const s=el('span');s.style.width=(sum?(cov[key]||0)/sum*100:0)+'%';s.style.background=color;s.title=`${key}: ${((cov[key]||0)/60).toFixed(1)} channel-minutes`;bar.append(s);}
    td.append(bar,el('div',sum?Object.entries(cov).map(([k,v])=>`${k.replace('_numeric','').replace('transfer_','')} ${Math.round(v/sum*100)}%`).join(' · '):'No current duration evidence','coverage-legend'));tr.append(td,el('td',String(t.production_channels)));
    td=el('td',undefined,'exception');const details=el('details'),summary=el('summary',`${t.errors.length+(t.final_map_failures?.length||0)} missing / failed · ${t.search_budget_exhausted} budget exhausted`);details.append(summary,el('pre',[...t.errors,...(t.final_map_failures||[]).map(f=>`CH${f.channel} final map: ${f.status}`),`${t.excluded_intervals.length} intervals excluded from correction`].join('\n')));td.append(details);tr.append(td);$('tapes').append(tr);
  }
  $('roots').textContent=JSON.stringify({roots:data.roots,blockers:data.errors},null,2);
  }
  const tapeKey=JSON.stringify(data.tapes.map(t=>t.tape));if(tapeKey!==tapeSignature){tapeSignature=tapeKey;
  const old=[...document.querySelectorAll('[data-tape]:checked')].map(n=>n.dataset.tape), existed=$('tape-options').children.length;
  $('tape-options').replaceChildren(...data.tapes.map(t=>{const label=el('label'),check=el('input');check.type='checkbox';check.dataset.tape=t.tape;check.checked=existed?old.includes(t.tape):true;label.append(check,document.createTextNode(t.tape.replace('A15_','')));return label;}));
  }
  const queueKey=JSON.stringify(data.queues);if(queueKey!==queueSignature){queueSignature=queueKey;
  const chosen=$('queue-select').value;$('queue-select').replaceChildren(el('option','Select a queue…'));
  for(const q of data.queues){const opt=el('option',`${new Date(q.created_at).toLocaleString()} · ${q.comparisons} comparisons`);opt.value=q.id;$('queue-select').append(opt);}if(chosen)$('queue-select').value=chosen;
  }
  const job=data.jobs.findLast(j=>['queued','running'].includes(j.status)); busy=!!job;$('job').hidden=!job;
  for(const id of ['create','maps','refresh','followup'])$(id).disabled=busy;
  if(job){$('job-name').textContent=job.name;$('job-progress').textContent=job.progress||'Queued';}
}
let knownJobs=new Set();
async function refresh(){try{
  const data=await api('/api/overview');renderOverview(data);
  for(const job of data.jobs){if(knownJobs.has(job.id))continue;if(job.status==='failed'){notice(`${job.name}: ${job.error}`);knownJobs.add(job.id);}if(job.status==='complete'){knownJobs.add(job.id);if(job.result?.queue_id){await openQueue(job.result.queue_id);notice(`Queue ready: ${job.result.comparisons} comparisons; ${job.result.blocked} blocked.`);}if(job.name==='Refresh mission evidence')await estimate();}}
}catch(e){notice(e.message);}}
async function estimate(){const opts=options();if(!opts.tapes.length){notice('Select at least one tape.');return;}
  const query=new URLSearchParams({...opts,tapes:opts.tapes.join(',')});const result=await api('/api/selection?'+query);
  $('time-estimate').replaceChildren(document.createTextNode(`${Math.ceil(result.estimated_listening_seconds/60)} `),el('small','min'));
  $('estimate-detail').textContent=`${result.comparisons} comparisons, ${opts.seconds} seconds each, both versions. ${result.uncovered_conditions.length} conditions uncovered. Allow extra time for replay and notes.`;
  $('coverage-summary').textContent=`Selection coverage · ${result.covered_conditions.length} covered · ${result.uncovered_conditions.length} uncovered`;
  $('coverage').replaceChildren();for(const [values,missing] of [[result.covered_conditions,false],[result.uncovered_conditions,true]])for(const value of values)$('coverage').append(el('span',(missing?'Missing: ':'')+value,'tag'+(missing?' missing':'')));
}
async function openQueue(id){pause();queue=await api('/api/queue?id='+id);lastQueue=id;localStorage.setItem('apolloReviewQueue',id);index=0;$('kind').value=queue.kind;renderQueue();await showClip(0);}
function renderQueue(){
  if(!queue)return;const latest=new Map(queue.events.map(e=>[e.sample_id,e]));
  $('queue-progress').textContent=`${latest.size} / ${queue.samples.length} RECORDED · SEED ${queue.seed}`;
  $('followup').hidden=!queue.findings.failed_cohorts.length;
  $('queue-list').replaceChildren(...queue.samples.map((s,i)=>{const button=el('button',undefined,'queue-item'+(i===index?' active':'')),event=latest.get(s.id),bad=event&&(event.judgment==='worse'||event.judgment==='uncertain'||Object.values(event.flags).some(Boolean));
    if(event)button.append(el('span',event.judgment,'finding'+(bad?' bad':'')));
    button.append(el('span',`${String(i+1).padStart(2,'0')}  ${s.tape.replace('A15_','')} · CH${String(s.channel).padStart(2,'0')}`,'title'),el('span',`${stamp(s.start_seconds)} · ${s.frames/s.sample_rate}s${s.ready?'':' · BLOCKED'}`,'meta'),el('span',s.selection_reason,'meta'));
    button.addEventListener('click',()=>showClip(i).catch(e=>notice(e.message)));return button;}));
}
async function showClip(i){if(!queue||!queue.samples[i])return;pause();index=i;buffers=[];loadedSample='';offset=0;version=0;renderQueue();const s=queue.samples[i];
  $('listen-empty').hidden=true;$('listen').hidden=false;$('clip-title').textContent=`${s.tape.replace('A15_','')} · Channel ${String(s.channel).padStart(2,'0')}`;
  $('clip-coordinates').textContent=`COMPARISON ${i+1} / ${queue.samples.length} · SOURCE ${stamp(s.start_seconds)} + ${s.frames/s.sample_rate}s`;
  $('clip-evidence').textContent=s.eligible?'SUPPORTED EVIDENCE':'EXCEPTION / CONSERVATIVE BYPASS';
  $('selection-reason').textContent=s.selection_reason;tagList($('clip-tags'),s.conditions);
  $('version-a').textContent=s.labels[0];$('version-b').textContent=s.labels[1];setVersion(0);
  $('evidence').textContent=JSON.stringify({reference:s.reference,support:s.support,numeric:s.numeric_results,evidence:s.evidence,provenance:s.provenance,excerpt:s.excerpt},null,2);
  const event=queue.events.filter(e=>e.sample_id===s.id).at(-1);finding=event?.judgment||'';
  document.querySelectorAll('[data-judgment]').forEach(n=>n.classList.toggle('selected',n.dataset.judgment===finding));
  document.querySelectorAll('[data-flag]').forEach(n=>n.checked=!!event?.flags[n.dataset.flag]);$('notes').value=event?.notes||'';
  $('faint-speech').checked=!!event?.confirmed_conditions?.includes('faint_speech');
  $('finding-status').textContent=event?`Recorded by ${event.reviewer} · ${new Date(event.reviewed_at).toLocaleString()}. Earlier adverse findings remain in the ledger.`:'';
  $('scrub').max=s.frames/s.sample_rate;$('scrub').value=0;$('playhead').textContent=`0:00 / ${fmt(s.frames/s.sample_rate)}`;
  $('save').disabled=!s.ready;$('play').disabled=!s.ready;
  if(!s.ready){$('gain-description').textContent=s.error;$('map').replaceChildren();return;}
  $('gain-description').textContent=`Equal RMS · same denoised input · gain A ${s.excerpt.gain_a.toFixed(3)}, gain B ${s.excerpt.gain_b.toFixed(3)} · exact full-channel map`;
  drawMap(s.excerpt);$('play').textContent='▶ Load & play';
}
function drawMap(meta){const svg=$('map');svg.replaceChildren();const ns='http://www.w3.org/2000/svg',values=meta.displacement_ms,extent=Math.max(.01,...values.map(Math.abs));
  function element(tag,attrs,text){const n=document.createElementNS(ns,tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,String(v));if(text!==undefined)n.textContent=text;return n;}
  for(const y of [20,60,100])svg.append(element('line',{x1:40,x2:630,y1:y,y2:y,stroke:'#dce0d5','stroke-dasharray':'3 3'}));
  svg.append(element('text',{x:3,y:20,fill:'#65727b','font-size':10},extent.toFixed(2)),element('text',{x:3,y:104,fill:'#65727b','font-size':10},(-extent).toFixed(2)));
  const points=values.map((v,i)=>`${40+i/Math.max(1,values.length-1)*590},${60-v/extent*42}`).join(' ');
  svg.append(element('polyline',{points,fill:'none',stroke:'#39765e','stroke-width':1.5}));
}
function time(){if(!playing)return offset;const duration=buffers[0]?.duration||20;const current=Math.max(0,offset+(context.currentTime-started));return $('loop').checked?current%duration:Math.min(current,duration);}
function pause(){if(playing)offset=time();playing=false;for(const source of sources){try{source.stop();}catch{}}sources=[];$('play').textContent='▶ Play';}
async function play(){const s=queue?.samples[index];if(!s?.ready)return;if(playing){pause();return;}
  context||=new AudioContext();await context.resume();
  if(loadedSample!==s.id){$('play').disabled=true;const id=s.id;try{buffers=await Promise.all(s.audio_urls.map(async url=>context.decodeAudioData(await(await fetch(url)).arrayBuffer())));if(queue.samples[index].id!==id)return;loadedSample=id;}finally{$('play').disabled=false;}}
  if(offset>=buffers[0].duration)offset=0;started=context.currentTime+.025;gains=[];sources=[];
  for(let i=0;i<2;i++){const source=context.createBufferSource(),gain=context.createGain();source.buffer=buffers[i];source.loop=$('loop').checked;gain.gain.value=i===version?1:0;source.connect(gain).connect(context.destination);source.start(started,offset);sources.push(source);gains.push(gain);}
  sources[0].onended=()=>{if(playing&&!$('loop').checked&&context.currentTime-started+offset>=buffers[0].duration-.05){playing=false;offset=buffers[0].duration;$('play').textContent='▶ Replay';}};
  playing=true;$('play').textContent='Ⅱ Pause';
}
function setVersion(n){version=n;$('version-a').classList.toggle('selected',n===0);$('version-b').classList.toggle('selected',n===1);gains.forEach((g,i)=>{g.gain.cancelScheduledValues(context.currentTime);g.gain.setTargetAtTime(i===n?1:0,context.currentTime,.003);});}
function seek(t){const was=playing;pause();offset=Math.max(0,Math.min(Number($('scrub').max),t));$('scrub').value=offset;if(was)play().catch(e=>notice(e.message));}
function animate(){if(queue){const duration=queue.samples[index].frames/queue.samples[index].sample_rate;$('playhead').textContent=`${fmt(time())} / ${fmt(duration)}`;if(playing)$('scrub').value=time();}requestAnimationFrame(animate);}requestAnimationFrame(animate);
function chooseFinding(value){finding=value;document.querySelectorAll('[data-judgment]').forEach(n=>n.classList.toggle('selected',n.dataset.judgment===value));}
async function save(){if(!queue||!finding)throw Error('Choose improved, unchanged, worse or uncertain first.');const s=queue.samples[index],reviewer=$('reviewer').value.trim();localStorage.setItem('apolloReviewer',reviewer);
  await api('/api/judgment',{queue_id:queue.id,sample_id:s.id,reviewer,judgment:finding,flags:Object.fromEntries([...document.querySelectorAll('[data-flag]')].map(n=>[n.dataset.flag,n.checked])),notes:$('notes').value,faint_speech_confirmed:$('faint-speech').checked});
  queue=await api('/api/queue?id='+queue.id);renderQueue();$('version-a').textContent=queue.samples[index].labels[0];$('version-b').textContent=queue.samples[index].labels[1];$('finding-status').textContent='Judgment recorded. '+(queue.findings.failed_cohorts.length?'Affected cohorts flagged. A targeted follow-up is available.':'Labels revealed; continue when ready.');
  $('accept').hidden=true;preview=null;
}
function renderPreview(p){const node=$('approval-preview');node.className='';node.replaceChildren();
  node.append(el('h3',`${p.demo?'SIMULATED · ':''}${p.kind==='methodology'?'Methodology':'Production cohort'} · ${p.status.toUpperCase()}`),el('p',`${p.summary.accepted_channels} eligible channels; ${p.summary.excluded_channels} excluded; ${(p.summary.accepted_interval_seconds/60).toFixed(1)} channel-minutes of supported corrections. Reviewer: ${p.reviewer||'missing'}.`));
  if(p.errors.length){const box=el('div',undefined,'approval-errors');box.append(el('b','Acceptance blockers'));const list=el('ul');p.errors.forEach(e=>list.append(el('li',e)));box.append(list);node.append(box);}
  if(p.findings.failed_cohorts.length)node.append(el('p',`${p.findings.failed_holdouts.length} retained adverse holdouts. Affected cohorts: ${p.findings.failed_cohorts.join('; ')}`));
  for(const s of p.scope){const details=el('details',undefined,'approval-scope'+(s.accepted?'':' excluded'));details.append(el('summary',`${s.tape} · CH${String(s.channel).padStart(2,'0')} · ${s.accepted?'ELIGIBLE':'EXCLUDED'} · ${s.intervals.length} intervals`),el('pre',JSON.stringify({blockers:s.blockers,cohorts:s.cohorts,intervals:s.intervals,exclusions:s.exclusions,plan_sha256:s.plan_sha256,continuous_review:s.continuous_review},null,2)));node.append(details);}
  const details=el('details');details.append(el('summary','Complete approval preview / provenance'),el('pre',JSON.stringify(p,null,2)));node.append(details);
  $('accept').hidden=p.status!=='ready';$('accept').textContent=p.demo?'Record simulated acceptance':'Accept the previewed scope';
}
action('refresh',async()=>{await api('/api/scan',{});await refresh();});action('estimate',estimate);
action('create',async()=>{if(!selectedTapes().length)throw Error('Select at least one tape.');await estimate();await api('/api/queue',options());await refresh();});
action('resume',async()=>{if(!$('queue-select').value)throw Error('Select a saved queue.');await openQueue($('queue-select').value);});
action('maps',async()=>{if(!selectedTapes().length)throw Error('Select at least one tape.');await api('/api/maps',{tapes:selectedTapes()});await refresh();});
action('followup',async()=>{await api('/api/followup',{queue_id:queue.id,budget:5});await refresh();});
action('play',play);action('version-a',()=>setVersion(0));action('version-b',()=>setVersion(1));action('next',()=>showClip(Math.min(index+1,queue.samples.length-1)));action('save',save);
$('scrub').addEventListener('input',()=>seek(Number($('scrub').value)));$('loop').addEventListener('change',()=>sources.forEach(s=>s.loop=$('loop').checked));
document.querySelectorAll('[data-judgment]').forEach(n=>n.addEventListener('click',()=>chooseFinding(n.dataset.judgment)));
action('preview',async()=>{if(!queue)throw Error('Open and review a listening queue first.');preview=await api('/api/preview',{queue_id:queue.id,reviewer:$('reviewer').value,kind:$('kind').value});renderPreview(preview);});
action('accept',async()=>{if(!preview)throw Error('Preview approval first.');const result=await api('/api/accept',{preview_id:preview.id});notice(`${result.demo?'Simulated acceptance':'Acceptance'} saved: ${result.certificate}\n${result.channels} channels in scope. No production render was launched.`);$('accept').hidden=true;await refresh();});
document.addEventListener('keydown',e=>{if(['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)||e.ctrlKey||e.metaKey||e.altKey)return;const k=e.key.toLowerCase();let fn;
  if(k===' ')fn=play;else if(k==='a')fn=()=>setVersion(0);else if(k==='b')fn=()=>setVersion(1);else if(k==='l')fn=()=>{$('loop').checked=!$('loop').checked;$('loop').dispatchEvent(new Event('change'));};else if(k==='arrowleft')fn=()=>seek(time()-2);else if(k==='arrowright')fn=()=>seek(time()+2);else if(k==='enter'&&queue)fn=save;else if(['1','2','3','4'].includes(k))fn=()=>chooseFinding(['improved','unchanged','worse','uncertain'][Number(k)-1]);else if(k==='n'&&queue)fn=()=>showClip(Math.min(index+1,queue.samples.length-1));if(fn){e.preventDefault();Promise.resolve().then(fn).catch(err=>notice(err.message));}});
$('reviewer').value=localStorage.getItem('apolloReviewer')||'';
refresh();setInterval(refresh,2500);
