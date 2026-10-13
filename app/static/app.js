const $ = id => document.getElementById(id);
let selected = null, state = null, customer = null, busy = false, lastChat = '', refreshVersion = 0;
const money = cents => 'S$' + new Intl.NumberFormat('en-SG', {minimumFractionDigits:2,maximumFractionDigits:2}).format(cents/100);
const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const timestamp = value => new Date(value).toLocaleTimeString('en-SG', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
const human = value => value.replaceAll('_',' ');
function requestId(){ return crypto.randomUUID(); }
async function api(path, body, asCustomer=false){
  const headers = {'Content-Type':'application/json'};
  if(asCustomer) headers['X-Demo-Customer']='demo-customer';
  const response = await fetch(path,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined});
  const result=await response.json();
  if(!response.ok) throw new Error(typeof result.detail==='string'?result.detail:JSON.stringify(result.detail));
  return result;
}
function showError(e){ $('error').textContent=e.message; $('error').hidden=false; }
async function action(fn){
  if(busy)return;
  busy=true; refreshVersion++; $('error').hidden=true; controls();
  try{await fn(); await refresh();}catch(e){showError(e);}finally{busy=false;controls();}
}
function controls(){
  $('simulate').disabled=busy;
  $('send').disabled=busy||!selected;
  $('reply').disabled=busy||!selected;
  document.querySelectorAll('[data-reply]').forEach(el=>el.disabled=busy||!selected);
  $('confirm').disabled=busy||!customer?.proposal||customer.proposal.expires_at<=Date.now()/1000;
  $('cancel').disabled=busy;
}
async function refresh(){
  const version=++refreshVersion;
  const dashboard=await api('/api/dashboard');
  if(version!==refreshVersion)return;
  const target=selected || dashboard.journeys[0]?.id || null;
  const nextCustomer=target?await api('/api/customer/'+target,undefined,true):null;
  if(version!==refreshVersion)return;
  state=dashboard;
  selected=target;
  customer=nextCustomer;
  $('connection').textContent='● CONNECTED';
  render();
}
function render(){
  const m=state.metrics;
  $('flagged').textContent=m.flagged; $('protected').textContent=m.protected; $('cases').textContent=m.cases; $('value').textContent=money(m.paused_value_cents);
  $('queue-count').textContent=state.journeys.length;
  $('queue').innerHTML=state.journeys.length?state.journeys.map(j=>`<button class="queue-item ${j.id===selected?'selected':''}" data-journey="${escape(j.id)}"><div class="queue-top"><strong>${money(j.event.amount_cents)}</strong><span class="status ${escape(j.transaction_status)}">${escape(j.transaction_status)}</span></div><p>${escape(j.event.payee)}</p><small>${escape(human(j.journey_status))} · ${escape(j.channel)}</small></button>`).join(''):'<div class="empty">No risk events yet.<br>Simulate a transfer to begin.</div>';
  document.querySelectorAll('[data-journey]').forEach(el=>el.onclick=()=>action(async()=>{selected=el.dataset.journey;lastChat='';}));
  if(!customer){controls();return;}
  const j=customer;
  $('channel').textContent=j.channel+' · SIMULATED';
  $('transaction-detail').textContent=`${money(j.event.amount_cents)} → ${j.event.payee} · ${human(j.journey_status)} · ${j.id}`;
  const signature=JSON.stringify(j.messages);
  if(signature!==lastChat){
    $('chat').innerHTML=j.messages.map(msg=>`<div class="bubble ${msg.direction==='inbound'?'customer':''} ${msg.channel==='SES'?'email':''}">${escape(msg.body)}<small>${msg.direction==='inbound'?'CUSTOMER':msg.channel==='SES'?'SES FOLLOW-UP':'KENA'} · ${escape(msg.status)} · ${timestamp(msg.at)}</small></div>`).join('');
    $('chat').scrollTop=$('chat').scrollHeight; lastChat=signature;
  }
  $('proposal').hidden=!j.proposal;
  $('risk').innerHTML=`<div class="risk-score">${escape(j.risk.level)} RISK <strong>${j.risk.score}<small>/100</small></strong></div><div class="meter"><div style="width:${j.risk.score}%"></div></div>${j.risk.signals.map(s=>`<div class="signal">${escape(s)}</div>`).join('')}`;
  $('case').innerHTML=j.case?`<div class="case-card">✓ Transfer paused · case open<br>${escape(j.case.id)}<br>Mock fraud-operations review</div>`:'';
  $('audit').innerHTML=[...j.audit].reverse().map(a=>`<div class="audit-item"><strong>${escape(human(a.kind))}</strong><p>${escape(JSON.stringify(a.detail))}</p><small>${timestamp(a.at)} · ${escape(a.policy_version)}</small></div>`).join('');
  $('outbox').innerHTML=j.messages.filter(msg=>msg.direction==='outbound').map(msg=>`<tr><td>${escape(msg.channel)}</td><td>${escape(msg.body)}</td><td><span class="badge">${escape(msg.status)}</span></td><td>${timestamp(msg.at)}</td></tr>`).join('');
  controls();
}
$('simulate').onclick=()=>action(async()=>{const j=await api('/api/events',{event_id:requestId(),rcs_available:!$('fallback').checked});selected=j.id;lastChat='';});
async function sendReply(text){await api('/api/customer/'+selected+'/replies',{message_id:requestId(),text},true);}
$('reply-form').onsubmit=e=>{e.preventDefault();const text=$('reply').value.trim();if(text)action(async()=>{await sendReply(text);$('reply').value='';});};
document.querySelectorAll('[data-reply]').forEach(el=>el.onclick=()=>action(()=>sendReply(el.dataset.reply)));
$('cancel').onclick=()=>action(()=>sendReply('cancel'));
$('confirm').onclick=()=>action(()=>api('/api/customer/'+selected+'/confirm',{command_id:requestId(),token:customer.proposal.token,approved_actions:['pause_transfer','create_scam_case']},true));
refresh().catch(showError);
setInterval(()=>{if(!busy)refresh().catch(e=>{$('connection').textContent='● DISCONNECTED';showError(e);});},2500);
