'use strict';
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
let state = null, selected = new Set(), workTapes = [], preview = null, activeJob = null, pollBusy = false, previewVersion = 0, tableKey = '';
const labels = {survey:'Survey carriers',pilot:'Prepare a pilot',review:'Open listening desk',deflutter:'Render corrections',quindar:'Survey Quindars',speed_plan:'Freeze speed curve',retime:'Render retimed audio',complete:'Archive complete'};
const time = value => value ? new Date(value).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : 'Not scanned yet';
const hours = seconds => `${(seconds/3600).toFixed(1)}h`;
function notify(message, error=false) { $('notice').textContent=message; $('notice').classList.toggle('error',error); $('notice').hidden=false; }
async function api(path, value) {
  const options = value === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':state?.token || ''},body:JSON.stringify(value)};
  const response=await fetch('/api/pipeline/'+path,options);
  const result=await response.json();
  if(!response.ok) throw new Error(result.error || 'Request failed');
  return result;
}
function safe(fn){return async(...args)=>{try{await fn(...args);}catch(error){notify(error.message,true);}};}
function badge(s) {
  const symbol = s.status==='complete'?'●':s.status==='partial'?'◐':['blocked','failed','stale'].includes(s.status)?'!':'○';
  const count = s.total>1 ? `${s.done}/${s.total}` : s.status==='complete'?'done':s.status==='partial'?'partial':'—';
  return `<span class="stage-badge ${escapeHTML(s.status)}" title="${escapeHTML(s.reason)}" aria-label="${escapeHTML(s.status)}: ${escapeHTML(s.reason)}">${symbol}<small>${count}</small></span>`;
}
function getVisible(){
  if(!state) return [];
  const search=$('search').value.trim().toLowerCase(), filter=$('filter').value;
  return state.tapes.filter(t=>t.tape.toLowerCase().includes(search) && (filter==='all'||
    filter==='needs-survey'&&t.stages.survey.status!=='complete'||
    filter==='needs-review'&&t.stages.review.status!=='complete'||
    filter==='listening'&&t.listening.judged>0||filter==='exceptions'&&(t.errors.length||t.listening.adverse)||
    filter==='finished'&&t.stages.retimed.status==='complete'));
}
function renderTable(){
  const tapes=getVisible();
  const key=JSON.stringify([tapes,[...selected],$('search').value,$('filter').value]);
  if(key===tableKey)return;tableKey=key;
  $('visible-count').textContent=`${tapes.length} tape${tapes.length===1?'':'s'}`;
  $('tape-head').innerHTML=`<tr><th><input id="select-all" type="checkbox" aria-label="Select all visible tapes"></th><th>TAPE / DURATION</th>${state.stages.map(s=>`<th class="stage-col" title="${escapeHTML(s.label)}">${escapeHTML(s.number)}<br>${escapeHTML(s.label.toUpperCase())}</th>`).join('')}<th></th></tr>`;
  $('select-all').checked=tapes.length>0&&tapes.every(t=>selected.has(t.tape));
  $('select-all').indeterminate=tapes.some(t=>selected.has(t.tape))&&!$('select-all').checked;
  $('select-all').addEventListener('change',event=>{for(const t of tapes){if(event.target.checked)selected.add(t.tape);else selected.delete(t.tape);}renderTable();});
  $('tape-body').innerHTML=tapes.map(t=>`<tr data-tape="${escapeHTML(t.tape)}" class="${selected.has(t.tape)?'selected':''}"><td><input type="checkbox" data-select="${escapeHTML(t.tape)}" aria-label="Select ${escapeHTML(t.tape)}" ${selected.has(t.tape)?'checked':''}></td><td><button class="job-select" data-open="${escapeHTML(t.tape)}"><span class="tape-name">${escapeHTML(t.tape.replace('A15_',''))}${t.errors.length?'<i class="exception-dot" title="Tape has exceptions"></i>':''}</span><div class="tape-meta">${t.duration_seconds?hours(t.duration_seconds):'—'} · ${t.expected_channels||0} speech channels</div></button></td>${state.stages.map(s=>`<td class="stage-cell">${badge(t.stages[s.key])}</td>`).join('')}<td class="tape-arrow">↗</td></tr>`).join('')||`<tr><td colspan="13" class="empty">${state.scanned_at?'No tapes match this view.':'The first archive inventory is running. Status will appear here as soon as it completes.'}</td></tr>`;
  for(const row of $('tape-body').querySelectorAll('tr[data-tape]')) row.addEventListener('click',event=>{if(event.target.closest('input')) return;openTape(row.dataset.tape);});
  for(const input of $('tape-body').querySelectorAll('[data-select]')) input.addEventListener('change',()=>{input.checked?selected.add(input.dataset.select):selected.delete(input.dataset.select);renderTable();});
  $('selection-count').textContent=selected.size?`${selected.size} tape${selected.size===1?'':'s'} selected`:'No tapes selected';
  $('plan-work').disabled=!selected.size;
}
function renderOverview(){
  const tapes=state.tapes, channels=tapes.reduce((n,t)=>n+(t.expected_channels||0),0);
  const den=tapes.reduce((n,t)=>n+t.stages.denoised.done,0), acc=tapes.reduce((n,t)=>n+t.stages.review.done,0), ret=tapes.reduce((n,t)=>n+t.stages.retimed.done,0);
  const metrics=[['Tapes in the archive',tapes.length,'Apollo 15 · 30-track recordings','◉'],['Speech channels prepared',den,`${channels.toLocaleString()} expected · stage 03`,'↳'],['Channels accepted',acc,'Current production acceptance','✓'],['Retimed channels',ret,'Completed final audio · stage 05c','↗']];
  $('metrics').innerHTML=metrics.map(([label,n,caption,mark])=>`<article class="metric"><div class="metric-label">${label}</div><span class="metric-mark">${mark}</span><div class="metric-number">${n.toLocaleString()}${label==='Speech channels prepared'?`<small>/ ${channels.toLocaleString()}</small>`:''}</div><div class="metric-caption">${caption}</div></article>`).join('');
  $('stage-summary').innerHTML=state.stages.map(s=>{const count=tapes.filter(t=>t.stages[s.key].status==='complete').length;return `<div class="stage-node ${count===tapes.length&&tapes.length?'done':''}"><div class="node">${count===tapes.length&&tapes.length?'✓':escapeHTML(s.number)}</div>${escapeHTML(s.label)}<small>${count}/${tapes.length}</small></div>`;}).join('');
  $('nav-count').textContent=tapes.length||'—';
  $('mode').textContent=state.demo?'SYNTHETIC DEMO':'LOCAL ARCHIVE';
  $('scan-time').textContent=state.scanned_at?`Last inventory · ${time(state.scanned_at)}`:'First inventory in progress';
  $('gate-title').textContent=state.methodology.status==='accepted'?'Methodology accepted for the current implementation':'Next milestone: qualify the correction method';
  $('gate-text').textContent=state.methodology.status==='accepted'?'Full-tape planning is eligible. Each production scope still needs its map checks and listening acceptance.':'Carrier qualification, bounded pilots, fresh holdouts and listening evidence are needed before full-tape planning.';
  $('roots').innerHTML=Object.entries(state.roots).map(([k,v])=>`<dt>${escapeHTML(k)}</dt><dd>${escapeHTML(v)}</dd>`).join('');
  const running=state.scan_jobs.find(j=>['queued','running'].includes(j.status));
  $('scan-progress').hidden=!running;
  if(running) $('scan-text').textContent=running.progress||running.name;
  $('refresh').disabled=!!running;
  const failed=state.scan_jobs.filter(j=>j.status==='failed').at(-1);
  if(failed && !running && (!state.scanned_at || failed.started_at>state.scanned_at)) notify('Inventory could not finish: '+failed.error,true);
  if(state.recovery_errors.length) notify(state.recovery_errors.join('\n'),true);
  renderTable();renderJobs();
}
function renderJobs(){
  const jobs=state.jobs, queued=jobs.filter(j=>j.status==='queued'), running=jobs.find(j=>['running','stopping'].includes(j.status));
  $('queue-state').textContent=running?'RUNNING':state.paused?'PAUSED':'READY';
  $('nav-work').textContent=queued.length+(running?1:0);
  $('start-queue').disabled=!queued.length||!state.paused;
  $('pause-queue').disabled=state.paused;
  if(!jobs.length) return;
  $('job-list').innerHTML=[...jobs].reverse().map(j=>`<div class="job-row ${j.id===activeJob?'active':''}"><button class="job-select" data-job="${j.id}"><b>${escapeHTML(j.tape.replace('A15_',''))} · ${escapeHTML(state.actions.find(a=>a.id===j.action)?.label||j.action)}</b><small>${time(j.created_at)}${j.options.channel?' · CH'+String(j.options.channel).padStart(2,'0'):''}</small></button><span class="job-state ${escapeHTML(j.status)}">${escapeHTML(j.status)}</span>${['queued','running'].includes(j.status)?`<button class="icon-button" data-cancel="${j.id}" title="${j.status==='running'?'Stop this job and pause the queue':'Remove from queue'}" aria-label="Stop ${escapeHTML(j.tape)} job">×</button>`:''}</div>`).join('');
  for(const btn of $('job-list').querySelectorAll('[data-job]'))btn.addEventListener('click',safe(async()=>{activeJob=btn.dataset.job;renderJobs();await loadLog();}));
  for(const btn of $('job-list').querySelectorAll('[data-cancel]'))btn.addEventListener('click',safe(async()=>{await api('cancel',{id:btn.dataset.cancel});await refresh();}));
  if(!activeJob&&running){activeJob=running.id;loadLog().catch(e=>notify(e.message,true));}
}
async function loadLog(){
  if(!activeJob)return;
  const j=await api('log?id='+encodeURIComponent(activeJob));
  $('log-title').textContent=j.tape.replace('A15_','')+' / '+(state.actions.find(a=>a.id===j.action)?.label||j.action);
  $('log-status').textContent=j.status.toUpperCase();
  const el=$('log-output'), bottom=el.scrollHeight-el.scrollTop-el.clientHeight<35;
  el.textContent=(j.error?j.error+'\n\n':'')+(j.tail||'Command prepared:\n\n'+j.command.map(quote).join(' ')+'\n\nWaiting for the queue to start.');
  if(bottom)el.scrollTop=el.scrollHeight;
}
function statusText(status){return status==='complete'?'<span class="complete">● Complete</span>':status==='excluded'?'<span class="muted">Timing only</span>':`<span class="${status==='stale'||status==='failed'?'partial':'muted'}">${escapeHTML(status)}</span>`;}
function detailHTML(t){
  const listening=t.listening, next=t.next_action;
  return `<div class="detail-header"><div><h2>${escapeHTML(t.tape.replace('A15_',''))}</h2><p class="detail-subtitle">${hours(t.duration_seconds||0)} · ${t.expected_channels||0} speech channels · ${escapeHTML((t.heads||[]).join(' / '))}</p></div><div class="detail-actions"><a class="button secondary" href="/review?pipeline_tape=${encodeURIComponent(t.tape)}#sampling">Listening desk ↗</a><button class="button primary" id="tape-plan">${escapeHTML(labels[next]||'Plan work')} →</button></div></div><div class="detail-stages">${state.stages.map(s=>{const v=t.stages[s.key];return `<div class="detail-stage ${escapeHTML(v.status)}" title="${escapeHTML(v.reason)}"><div class="number">${escapeHTML(s.number)}</div><b>${escapeHTML(s.label)}</b><span>${escapeHTML(v.status)}${v.total>1?` · ${v.done}/${v.total}`:''}</span></div>`;}).join('')}</div>${t.errors.length?`<div class="detail-section">${t.errors.map(e=>`<p class="alert">${escapeHTML(e)}</p>`).join('')}</div>`:''}<div class="detail-section"><h3>Channel register</h3><div class="channel-scroll"><table class="channel-table"><thead><tr><th>CHANNEL</th><th>HEAD / SOURCE</th><th>DENOISED</th><th>ACCEPTANCE</th><th>DEFLUTTERED</th><th>RETIMED</th></tr></thead><tbody>${t.channels.map(c=>`<tr><td><b>CH${String(c.channel).padStart(2,'0')}</b>${c.role==='timing_only'?'<div class="tape-meta">IRIG</div>':''}</td><td><div>${escapeHTML(c.head)}</div><div class="channel-source">${escapeHTML(c.source)}</div></td><td>${statusText(c.denoised?'complete':'pending')}</td><td title="${escapeHTML(c.approval_error)}">${c.role==='timing_only'?'—':statusText(c.accepted?'complete':'pending')}</td><td title="${escapeHTML(c.render_error)}">${statusText(c.defluttered)}</td><td title="${escapeHTML(c.retime_error)}">${statusText(c.retimed)}</td></tr>`).join('')}</tbody></table></div></div><div class="detail-columns"><div class="detail-section"><h3>Listening & acceptance</h3><div class="review-counts"><div><b>${listening.judged}<span> / ${listening.comparisons}</span></b><span>comparisons judged</span></div><div><b>${listening.adverse}</b><span>adverse findings retained</span></div></div><p class="detail-copy">Listening totals include saved review history. The acceptance column validates current production evidence. Older judgments remain in the ledger when the method or plans change.</p><a class="button secondary" href="/review?pipeline_tape=${encodeURIComponent(t.tape)}#review">Resume listening ↗</a><h3>Evidence on disk</h3><p class="detail-path">${escapeHTML(t.source_folder||'')}</p>${(t.pilots||[]).map(p=>`<p class="detail-path">Pilot: ${escapeHTML(p.path)}</p>`).join('')}<details class="stage-reasons"><summary>Stage prerequisites and status details</summary>${state.stages.map(s=>`<p><b>${escapeHTML(s.label)}:</b> ${escapeHTML(t.stages[s.key].reason)}</p>`).join('')}</details></div><div class="detail-section"><h3>Tape journal</h3><p class="detail-copy">Record handoff notes, listening observations or work still needed.</p><form id="note-form" class="note-form"><label>New note<textarea id="tape-note" maxlength="4000" placeholder="What should you remember about this tape?" required></textarea></label><button class="button secondary" type="submit">Save note</button></form><div id="tape-notes">${notesHTML(t.notes)}</div></div></div>`;
}
function notesHTML(notes){return notes.length?notes.map(n=>`<div class="note"><small>${time(n.created_at)}</small>${escapeHTML(n.text)}</div>`).join(''):'<p class="detail-copy">No notes yet.</p>';}
function openTape(tape){
  const t=state.tapes.find(t=>t.tape===tape);if(!t)return;
  $('tape-detail').innerHTML=detailHTML(t);$('tape-dialog').showModal();
  $('tape-plan').addEventListener('click',()=>{if(t.next_action==='review'){location.href='/review?pipeline_tape='+encodeURIComponent(tape)+'#sampling';return;}openWork([tape],t.next_action==='complete'?'retime':t.next_action);});
  $('note-form').addEventListener('submit',safe(async event=>{event.preventDefault();const input=$('tape-note');const text=input.value;const button=event.target.querySelector('button');button.disabled=true;try{const n=await api('note',{tape,text});t.notes.unshift(n);$('tape-notes').innerHTML=notesHTML(t.notes);input.value='';}finally{button.disabled=false;}}));
}
function openWork(tapes,action){
  workTapes=tapes;preview=null;
  if($('tape-dialog').open)$('tape-dialog').close();
  $('work-scope').textContent=tapes.length===1?tapes[0]:`${tapes.length} tapes · ${tapes.map(t=>t.replace('A15_','')).join(', ')}`;
  $('action').innerHTML=state.actions.map(a=>`<option value="${a.id}">${escapeHTML(a.number)} · ${escapeHTML(a.label)}</option>`).join('');
  const first=state.tapes.find(t=>t.tape===tapes[0]);
  $('action').value=action||(['survey','pilot','deflutter','quindar','speed_plan','retime'].includes(first?.next_action)?first.next_action:'survey');
  $('channel').innerHTML='<option value="">All applicable channels</option>'+Array.from({length:30},(_,i)=>`<option value="${i+1}">CH${String(i+1).padStart(2,'0')}</option>`).join('');
  updateAction();$('work-dialog').showModal();
}
function updateAction(){
  previewVersion++;preview=null;$('work-preview').innerHTML='';$('enqueue').hidden=true;
  const action=$('action').value;
  $('action-description').textContent=state.actions.find(a=>a.id===action)?.description||'';
  $('pilot-fields').hidden=action!=='pilot';
  $('channel-field').hidden=!['denoise','deflutter','retime'].includes(action);
  $('workers-field').hidden=!['denoise','pilot','plan','quindar'].includes(action);
}
function quote(s){return /[\s"']/.test(s)?'"'+s.replace(/"/g,'\\"')+'"':s;}
async function previewWork(event){
  event.preventDefault();preview=null;$('enqueue').hidden=true;
  const version=++previewVersion;
  const button=$('preview-work');button.disabled=true;button.textContent='Checking the selected tapes…';
  const options={workers:Number($('workers').value)};
  if(!$('channel-field').hidden&&$('channel').value)options.channel=Number($('channel').value);
  if($('action').value==='pilot'){options.start=Number($('pilot-start').value);options.duration=Number($('pilot-duration').value);}
  try{
    const result=await api('preview',{tapes:workTapes,action:$('action').value,options});
    if(version!==previewVersion)return;preview=result;
    $('work-preview').innerHTML=`<p class="detail-copy">${preview.ready} ready · ${preview.blocked} blocked. Only ready jobs can be added.</p>`+preview.jobs.map(j=>`<div class="preview-row"><b>${escapeHTML(j.tape)}<span class="${j.ready?'complete':'partial'}">${j.ready?'READY':'BLOCKED'}</span></b>${j.ready?`<pre>${escapeHTML(j.command.map(quote).join(' '))}</pre>`:`<p class="blocked-message">${escapeHTML(j.error)}</p>`}</div>`).join('');
    $('enqueue').hidden=!preview.ready;$('enqueue').textContent=`Add ${preview.ready} ready job${preview.ready===1?'':'s'} to queue`;
  }catch(e){$('work-preview').innerHTML=`<div class="preview-row"><p class="blocked-message">${escapeHTML(e.message)}</p></div>`;}
  finally{button.disabled=false;button.textContent='Preview commands & prerequisites →';}
}
async function refresh(){
  state=await api('overview');$('connection').textContent='Connected · loopback only';renderOverview();
}
async function poll(){
  if(pollBusy)return;pollBusy=true;
  try{await refresh();if(activeJob)await loadLog();}
  catch(e){$('connection').textContent='Connection lost · retrying';}
  finally{pollBusy=false;}
}
for(const b of document.querySelectorAll('[data-close]'))b.addEventListener('click',()=>$(b.dataset.close).close());
$('search').addEventListener('input',()=>state&&renderTable());$('filter').addEventListener('change',()=>state&&renderTable());
$('plan-work').addEventListener('click',()=>openWork([...selected]));$('action').addEventListener('change',updateAction);
$('work-form').addEventListener('submit',safe(previewWork));
for(const id of ['channel','workers','pilot-start','pilot-duration'])$(id).addEventListener('input',()=>{previewVersion++;preview=null;$('enqueue').hidden=true;$('work-preview').innerHTML='';});
$('enqueue').addEventListener('click',safe(async()=>{if(!preview)return;const b=$('enqueue');b.disabled=true;try{const result=await api('enqueue',{preview_id:preview.id});$('work-dialog').close();notify(`${result.added.length} job(s) added.${result.paused?' The queue is paused; start it when ready.':''}`);await refresh();$('work').scrollIntoView({behavior:'smooth'});}finally{b.disabled=false;}}));
$('refresh').addEventListener('click',safe(async()=>{await api('scan',{});await refresh();}));
$('start-queue').addEventListener('click',safe(async()=>{await api('queue',{action:'start'});await refresh();}));
$('pause-queue').addEventListener('click',safe(async()=>{await api('queue',{action:'pause'});notify('Queue paused. The current job will finish; waiting jobs stay queued.');await refresh();}));
refresh().catch(e=>notify('Unable to load the archive: '+e.message,true));
setInterval(poll,3000);
