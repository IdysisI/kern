'use strict';
const $ = id => document.getElementById(id);
let socket, sequence = 0, session = sessionStorage.getItem('kern-session'), running = false;
let pending = new Map(), allSessions = {}, live = null, currentTool = null, approval = null, reconnect;
let renderedN = 0, renderedSession = null, calls = new Map(), liveRaf = 0, livePending = '';
const welcome = $('messages').innerHTML;

function notice(message) { $('notice').textContent = message; $('notice').hidden = !message; }
function connected(on) {
  $('connection').textContent = on ? '● Connected locally' : '○ Disconnected · reconnecting';
  $('connection').classList.toggle('offline', !on);
  for (const id of ['new','set-model','undo','fork','history','capabilities']) $(id).disabled = !on;
  $('send').disabled = !on || running;
}
function busy(value) {
  running = value; $('stop').hidden = !value;
  $('send').disabled = value || socket?.readyState !== WebSocket.OPEN;
  $('activity').textContent = value ? 'Kern is working…' : 'Ready to work';
  $('undo').disabled = value; $('fork').disabled = value;
}
function rpc(method, fields = {}) {
  if (socket?.readyState !== WebSocket.OPEN) return Promise.reject(new Error('Connection unavailable'));
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Timeout: ${method}. Check the session before retrying.`)); }, 30000);
    pending.set(id, {resolve, reject, timer});
    socket.send(JSON.stringify({method, req_id:id, ...fields}));
  });
}
function scrollIfNear() {
  const el = $('messages');
  if (el.scrollHeight - el.scrollTop - el.clientHeight < 450) el.scrollTop = el.scrollHeight;
}
function renderText(container, text) {
  container.replaceChildren();
  // Parse a deliberately bounded Markdown subset into text nodes. No HTML
  // interpretation or external image loading, including during partial streams.
  let code = null, paragraph = null, list = null;
  function inline(node, value) {
    const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\(https?:\/\/[^\s)]+\))/g;
    let offset = 0;
    for (const match of value.matchAll(pattern)) {
      node.append(document.createTextNode(value.slice(offset, match.index)));
      const token = match[0];
      const part = document.createElement(token[0] === '`' ? 'code' : token[0] === '*' ? 'strong' : 'a');
      if (part.tagName === 'A') {
        const split = token.indexOf('](');
        part.textContent = token.slice(1, split); part.href = token.slice(split + 2, -1);
        part.target = '_blank'; part.rel = 'noopener noreferrer';
      } else part.textContent = token.slice(token[0] === '*' ? 2 : 1, token[0] === '*' ? -2 : -1);
      node.append(part); offset = match.index + token.length;
    }
    node.append(document.createTextNode(value.slice(offset)));
  }
  for (const line of text.split('\n')) {
    if (/^\s*```/.test(line)) {
      if (code) code = null;
      else { const pre = document.createElement('pre'); code = document.createElement('code'); pre.append(code); container.append(pre); }
      paragraph = list = null; continue;
    }
    if (code) { code.append(document.createTextNode(line + '\n')); continue; }
    if (!line.trim()) { paragraph = list = null; continue; }
    const heading = /^(#{1,4})\s+(.+)$/.exec(line);
    const item = /^\s*(?:([-*])|\d+\.)\s+(.+)$/.exec(line);
    if (heading) {
      const node = document.createElement('h' + Math.min(heading[1].length + 1, 6));
      inline(node, heading[2]); container.append(node); paragraph = list = null;
    } else if (item) {
      const tag = item[1] ? 'ul' : 'ol';
      if (!list || list.tagName.toLowerCase() !== tag) { list = document.createElement(tag); container.append(list); }
      const node = document.createElement('li'); inline(node, item[2]); list.append(node); paragraph = null;
    } else {
      list = null;
      if (!paragraph) { paragraph = document.createElement('p'); container.append(paragraph); }
      else paragraph.append(document.createTextNode('\n'));
      inline(paragraph, line);
    }
  }
}
function bubble(role, text='') {
  if ($('messages').querySelector('.welcome')) $('messages').replaceChildren();
  const article = document.createElement('article'); article.className = `message ${role}`;
  const label = document.createElement('div'); label.className='label'; label.textContent=role==='user'?'Vous':'Kern';
  const content=document.createElement('div'); article.append(label,content); $('messages').append(article);
  renderText(content,text); return {article,content,text};
}
function toolCard(name, args, result='', status='') {
  const el=document.createElement('details'); el.className='tool';
  if (status==='failed' || status==='denied' || /^error|^exit=[1-9]/.test(result)) el.classList.add('failed');
  const head=document.createElement('summary'); head.textContent=`${status==='failed'?'×':'›'} ${name}  ${args.path || args.cmd || args.url || ''}`;
  const pre=document.createElement('pre'); pre.textContent=JSON.stringify(args,null,2)+(result?'\n\n'+result:'\nEn cours…');
  el.append(head,pre); $('messages').append(el); return {el,pre,args};
}
function renderEvents(events) {
  // Incremental append: this used to rebuild the ENTIRE message DOM on every
  // state() call (O(n) per poll -> O(n^2) over a session, audit r3 F2). We keep
  // a watermark (renderedN) and only append events newer than it.
  const total = events.length;
  if (renderedSession === session && renderedN === total) return;
  let slice = events;
  if (renderedSession === session && renderedN > 0 && total > renderedN) slice = events.slice(renderedN);
  if (slice === events) { $('messages').replaceChildren(); calls = new Map(); }
  renderedSession = session; renderedN = total;
  // The in-flight streaming bubble is not in the journal yet: keep it, and put
  // it back at the end after appending newer events (correct chronology).
  const journalledLive = live && slice.some(ev => ev.kind === 'assistant' && ev.text &&
      (ev.text === live.text || ev.text.startsWith(live.text) || live.text.startsWith(ev.text)));
  if (liveRaf) { cancelAnimationFrame(liveRaf); liveRaf = 0; }
  if (journalledLive) { live.article.remove(); live = null; }
  currentTool = null;
  for (const ev of slice) {
    if (ev.kind==='user') bubble('user',ev.text || '');
    if (ev.kind==='assistant') {
      if (ev.text) bubble('assistant',ev.text);
      for (const c of ev.tool_calls || []) calls.set(c.id,toolCard(c.name,c.arguments || {}));
    }
    if (ev.kind==='tool_result' && calls.has(ev.call_id)) {
      const card=calls.get(ev.call_id); card.pre.textContent=JSON.stringify(card.args,null,2)+'\n\n'+ev.text+(ev.diff?'\n\n'+ev.diff:'');
      card.el.classList.toggle('failed', ['failed','denied','uncertain'].includes(ev.status));
    }
    if (ev.kind==='note') { const el=document.createElement('p');el.className='tool';el.textContent=ev.text;$('messages').append(el); }
  }
  if (live) $('messages').append(live.article);
  if (!$('messages').children.length) $('messages').innerHTML=welcome;
}
function renderPlan(items) {
  $('plan').replaceChildren();
  if (!items.length) { const li=document.createElement('li');li.className='muted';li.textContent='Aucun plan pour le moment.';$('plan').append(li); }
  for (const item of items) {
    const li=document.createElement('li');li.className=item.status;
    li.textContent=({done:'✓',active:'●',pending:'○',blocked:'!'}[item.status] || '○')+' '+item.text;$('plan').append(li);
  }
}
async function state() {
  const s=await rpc('state');
  session=s.session;sessionStorage.setItem('kern-session',session);
  $('cwd').textContent=s.cwd;$('model').value=s.model;$('session-id').textContent=`Session ${session}\n${s.total_events} events kept`;
  const user=s.events.find(e=>e.kind==='user');$('title').textContent=user?user.text.slice(0,65):'New session';
  const near=$('messages').scrollHeight-$('messages').scrollTop-$('messages').clientHeight<450;
  renderEvents(s.events); if(near) $('messages').scrollTop=$('messages').scrollHeight;
  renderPlan(s.todo);busy(s.running);
  if(!s.running && s.stop_reason && s.stop_reason!=='done') {
    $('activity').textContent=({blocked:'Work blocked',unverified:'Unverified completion',error:'Turn error',interrupted:'Turn interrupted',step_limit:'Limit reached · incomplete work',output_limit:'Incomplete response'}[s.stop_reason] || 'Ready to work');
  }
  $('mounts').textContent=s.mounts.length?s.mounts.join(' · '):'None · loaded on demand';
  $('usage').textContent=`${s.usage.requests || 0} requests · last turn\n↑ ${s.usage.in || 0} · ↓ ${s.usage.out || 0} tokens`;
  if(s.context?.estimated_tokens)$('usage').textContent+=`\nContexte ≈ ${s.context.estimated_tokens.toLocaleString('fr-CH')} / ${s.context.context_length.toLocaleString('fr-CH')}`;
  $('receipts').replaceChildren();
  for(const r of s.receipts.slice(-7).reverse()) {const d=document.createElement('div');d.textContent=`${r.name} · ${r.status}\n${r.arguments.path || r.arguments.cmd || r.id}`;$('receipts').append(d);}
  if(s.approval) showApproval({id:s.approval[0],desc:s.approval[1],diff:s.approval[2]});
}
function sessions() {
  const q=$('search').value.toLowerCase();$('sessions').replaceChildren();
  for(const [id,s] of Object.entries(allSessions)) {
    if(!`${id} ${s.preview}`.toLowerCase().includes(q))continue;
    const b=document.createElement('button');b.className='session'+(id===session?' active':'');
    const title=document.createElement('span');title.textContent=s.preview || 'New session';
    const date=document.createElement('small');date.textContent=(s.active?'● En cours · ':'')+(s.last_ts?new Date(s.last_ts*1000).toLocaleString('fr-CH',{dateStyle:'short',timeStyle:'short'}):'');
    b.append(title,date);b.onclick=()=>attach(id).catch(e=>notice(e.message));$('sessions').append(b);
  }
}
async function refreshSessions(){const r=await rpc('sessions');allSessions=r.sessions;sessions();}
async function attach(id){notice('');live=null;session=id;await rpc('attach',{session:id});await state();await refreshSessions();document.body.classList.remove('sessions-open');}
async function newSession(){notice('');const r=await rpc('new',{model:$('model').value || undefined});session=r.attached;await state();await refreshSessions();document.body.classList.remove('sessions-open');$('prompt').focus();}
function showApproval(message){
  $('approval').returnValue='deny';
  approval=message.id;$('approval-desc').textContent=message.desc;$('approval-diff').textContent=message.diff || 'Aucun diff disponible pour cette action.';
  $('activity').textContent='Your approval is required';if(!$('approval').open)$('approval').showModal();
}
$('approval').addEventListener('close',()=>{if(approval!==null){rpc('approve',{id:approval,allow:$('approval').returnValue==='allow'}).catch(e=>notice(e.message));approval=null;}});
async function onEvent(m){
  if(m.session && m.session!==session)return;
  if(m.event==='turn_start'){busy(true);live=null;notice('');}
  else if(m.event==='text'){
    if(!live)live=bubble('assistant');
    live.text+=m.text;
    // Coalesce chunk re-renders to one per animation frame: renderText rebuilds
    // the whole assistant message DOM, so per-chunk calls cost O(n^2) on long
    // answers and froze the tab on multi-kB streams (audit r3 F1).
    livePending=live.text;
    if(!liveRaf)liveRaf=requestAnimationFrame(()=>{liveRaf=0;if(live)renderText(live.content,livePending);scrollIfNear();});
  }
  else if(m.event==='tool'){live=null;const t=JSON.parse(m.text);currentTool=toolCard(t.name,t.arguments || {});scrollIfNear();}
  else if(m.event==='result' && currentTool){currentTool.pre.textContent=JSON.stringify(currentTool.args,null,2)+'\n\n'+m.text;currentTool.el.classList.toggle('failed',/^error|^denied|^exit=[1-9]/.test(m.text));}
  else if(m.event==='diff' && currentTool)currentTool.pre.textContent+='\n\n'+m.text;
  else if(m.event==='todo')renderPlan(JSON.parse(m.text));
  else if(m.event==='approve_request')showApproval(m);
  else if(m.event==='turn_end'){busy(false);await state();await refreshSessions();}
  else if(m.event==='error'){busy(false);notice(m.error || 'Error');}
  else if(m.event==='note' || m.event==='summary'){$('activity').textContent=m.text.split('\n')[0].slice(0,100);}
  else if(m.event==='thinking')$('activity').textContent='Thinking…';
}
function connect(){
  clearTimeout(reconnect);socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}`);
  socket.onopen=async()=>{
    connected(true);
    try{await refreshSessions();if(session && allSessions[session])await attach(session);else await newSession();}
    catch(e){notice(e.message);}
    rpc('models').then(r=>{$('models').replaceChildren();for(const m of r.models){const o=document.createElement('option');o.value=m.id;$('models').append(o);}}).catch(()=>notice('Catalog unavailable. Enter a model identifier; check KERN_BASE_URL.'));
  };
  socket.onmessage=e=>{try{const m=JSON.parse(e.data);const p=pending.get(m.req_id);if(p){clearTimeout(p.timer);pending.delete(m.req_id);m.error?p.reject(new Error(m.error)):p.resolve(m.result);return;}onEvent(m).catch(e=>notice(e.message));}catch(e){notice('Message serveur invalide : '+e.message);}};
  socket.onclose=()=>{connected(false);for(const p of pending.values()){clearTimeout(p.timer);p.reject(new Error('Connection lost; delivery potentially completed. No automatic resend.'));}pending.clear();reconnect=setTimeout(connect,2000);};
}
$('composer').onsubmit=async e=>{e.preventDefault();const text=$('prompt').value.trim();if(!text||running)return;notice('');busy(true);bubble('user',text);try{await rpc('chat',{text});$('prompt').value='';sessionStorage.removeItem('kern-draft');$('messages').scrollTop=$('messages').scrollHeight;}catch(err){busy(false);notice(err.message);}};
$('prompt').value=sessionStorage.getItem('kern-draft') || '';
$('prompt').oninput=()=>sessionStorage.setItem('kern-draft',$('prompt').value);
$('prompt').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('composer').requestSubmit();}};
$('new').onclick=()=>newSession().catch(e=>notice(e.message));
$('stop').onclick=()=>rpc('interrupt').catch(e=>notice(e.message));
$('set-model').onclick=()=>rpc('model',{model:$('model').value.trim()}).then(()=>notice('Model selected for next turn.')).catch(e=>notice(e.message));
$('search').oninput=sessions;
$('fork').onclick=()=>rpc('fork').then(r=>{session=r.session;return state();}).then(refreshSessions).catch(e=>notice(e.message));
$('undo').onclick=()=>{if(confirm('Undo tracked writes from the last turn and return to your request?'))rpc('undo').then(state).catch(e=>notice(e.message));};
function details(title,text){$('details-title').textContent=title;$('details-body').textContent=text;$('details').showModal();}
$('history').onclick=async()=>{try{let start=0,events=[],total=1;while(start<total){const r=await rpc('history',{start});events.push(...r.events);total=r.total;start+=200;}details('Journal complet',events.map(e=>JSON.stringify(e,null,2)).join('\n'));}catch(e){notice(e.message);}};
$('capabilities').onclick=()=>rpc('capabilities').then(r=>details('Available capabilities',r.capabilities.join('\n') || 'No capabilities configured.')).catch(e=>notice(e.message));
$('sidebar-toggle').onclick=()=>document.body.classList.toggle('sessions-open');
$('inspector-toggle').onclick=()=>document.body.classList.toggle('state-open');
$('inspector-close').onclick=()=>document.body.classList.remove('state-open');
$('sidebar-close').onclick=()=>document.body.classList.remove('sessions-open');
$('scrim').onclick=()=>document.body.classList.remove('sessions-open');
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.body.classList.remove('sessions-open','state-open');});
document.addEventListener('click',e=>{const b=e.target.closest('[data-prompt]');if(b){$('prompt').value=b.dataset.prompt;$('prompt').focus();}});
connect();
