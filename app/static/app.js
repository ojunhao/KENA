const $ = id => document.getElementById(id);
let selected = null, state = null, customer = null, busy = false, lastChat = '', refreshVersion = 0, deleteTarget = null;
const money = cents => 'S$' + new Intl.NumberFormat('en-SG', {minimumFractionDigits:2,maximumFractionDigits:2}).format(cents/100);
const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const timestamp = value => new Date(value).toLocaleTimeString('en-SG', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
const human = value => value.replaceAll('_',' ');
const statusLabel = value => value==='simulated'?'emulated':value;
const channelName = value => ({WHATSAPP:'WhatsApp',SES:'Email (SES)'}[value] || value);
const seconds = value => value==null?'—':value<120?Math.round(value)+'s':(value/60).toFixed(1)+' min';
// The email carries the full cross-channel case summary; the chat shows its first paragraph.
const preview = msg => msg.channel==='SES'?messageText(msg.body.split('\n\n')[0])+' [+ full case summary]':messageText(msg.body);
const buttonsHtml = msg => msg.buttons?.length?`<div class="wa-buttons" aria-label="Message buttons">${msg.buttons.map(b=>`<span>${escape(b.title)}</span>`).join('')}</div>`:'';
const policyLabel = value => value.replace(/^KENA-DEMO-(\d+)$/, 'Policy v$1');
// Present older stored journeys consistently without rewriting their audit records.
function messageText(value){
  return String(value).replaceAll('KENA • Demo Bank:', 'KENA:')
    .replaceAll('A simulated call-state signal', 'Call context')
    .replaceAll('This is a synthetic demo.', '')
    .replaceAll("the demo's automatic-protection limit", 'the automatic-protection limit')
    .replaceAll(' in this demo', '').replaceAll(' (simulated)', '')
    .replaceAll('Simulated customer RCS unavailable', 'Customer RCS unavailable')
    .replace(/\s+([.,])/g,'$1').replace(/ {2,}/g,' ').trim();
}
function auditText(entry){
  if(entry.kind==='demo_transfer_deleted')return 'Archived from active views';
  const keys=['score','signals','call_state','from','to','reason','intent','agent','trigger','actions','action','priority','channel','query','matches','expires_in_seconds','tool','result','case_id','status','fallback','funds_executed'];
  const details=Object.fromEntries(Object.entries(entry.detail).filter(([key])=>keys.includes(key)));
  if(details.status)details.status=statusLabel(details.status);
  return messageText(JSON.stringify(details));
}
function requestId(){ return crypto.randomUUID(); }
async function api(path, body, asCustomer=false, method=null){
  const headers = {'Content-Type':'application/json'};
  if(asCustomer) headers['X-Demo-Customer']='demo-customer';
  const response = await fetch(path,{method:method || (body?'POST':'GET'),headers,body:body?JSON.stringify(body):undefined});
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
  const noConversation=!customer || customer.journey_status==='not_flagged';
  $('send').disabled=busy||noConversation;
  $('reply').disabled=busy||noConversation;
  document.querySelectorAll('[data-reply]').forEach(el=>el.disabled=busy||noConversation);
  document.querySelectorAll('[data-delete]').forEach(el=>el.disabled=busy);
  $('confirm').disabled=busy||!customer?.proposal||customer.proposal.expires_at<=Date.now()/1000;
  $('cancel').disabled=busy;
}
async function refresh(){
  const version=++refreshVersion;
  const dashboard=await api('/api/dashboard');
  if(version!==refreshVersion)return;
  const target=dashboard.journeys.some(j=>j.id===selected)?selected:dashboard.journeys[0]?.id || null;
  const nextCustomer=target?await api('/api/customer/'+target,undefined,true):null;
  if(version!==refreshVersion)return;
  state=dashboard;
  selected=target;
  customer=nextCustomer;
  $('connection').textContent='● CONNECTED';
  const live=state.mode==='live-aws-cds';
  document.querySelector('.mode').classList.toggle('live',live);
  const modes=state.channel_modes||{}, liveChannels=Object.keys(modes).filter(c=>modes[c]==='live');
  $('mode-label').textContent=!live||!liveChannels.length?'LOCAL CHANNELS':liveChannels.length===Object.keys(modes).length?'LIVE · AWS CDS':'LIVE · '+liveChannels.map(channelName).join(', ')+' · OTHERS EMULATED';
  $('agent-label').textContent=state.agent==='claude-bedrock'?'AGENT · CLAUDE ON BEDROCK':state.agent==='agentcore-runtime'?'AGENT · BEDROCK AGENTCORE':'AGENT · RULES';
  render();
}
function render(){
  const m=state.metrics;
  $('flagged').textContent=m.flagged; $('protected').textContent=m.protected; $('cases').textContent=m.cases; $('value').textContent=money(m.paused_value_cents);
  const o=state.outcomes||{};
  $('response-rate').textContent=o.response_rate==null?'—':Math.round(o.response_rate*100)+'%';
  $('time-to-protect').textContent=seconds(o.median_seconds_to_protect);
  $('queue-count').textContent=state.journeys.length;
  $('queue').innerHTML=state.journeys.length?state.journeys.map(j=>`<div class="queue-row ${j.id===selected?'selected':''}"><button class="queue-item" data-journey="${escape(j.id)}"><div class="queue-top"><strong>${money(j.event.amount_cents)}</strong><span class="status ${escape(j.transaction_status)}">${escape(j.transaction_status)}</span></div><p>${escape(j.event.payee)}</p><small>${escape(human(j.journey_status))} · ${escape(channelName(j.channel))}</small></button><button class="delete-transfer" data-delete="${escape(j.id)}" aria-label="Delete transfer ${money(j.event.amount_cents)}" title="Delete transfer"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7M14 10v7"/></svg></button></div>`).join(''):'<div class="empty">No transfers</div>';
  document.querySelectorAll('[data-journey]').forEach(el=>el.onclick=()=>action(async()=>{selected=el.dataset.journey;lastChat='';}));
  document.querySelectorAll('[data-delete]').forEach(el=>el.onclick=()=>{
    deleteTarget=state.journeys.find(j=>j.id===el.dataset.delete);
    $('delete-detail').textContent=money(deleteTarget.event.amount_cents)+' → '+deleteTarget.event.payee;
    $('delete-dialog').showModal();
  });
  if(!customer){
    $('channel').textContent='WhatsApp / SMS';$('transaction-detail').textContent='No transfer selected';
    $('chat').innerHTML='<div class="empty">No messages</div>';lastChat='';$('proposal').hidden=true;
    $('risk').innerHTML='<div class="empty">No risk assessment</div>';$('case').innerHTML='';
    $('audit').innerHTML='<div class="empty">No activity</div>';$('outbox').innerHTML='<tr><td colspan="4">No outgoing messages</td></tr>';
    controls();return;
  }
  const j=customer;
  $('channel').textContent=channelName(j.channel);
  $('transaction-detail').textContent=`${money(j.event.amount_cents)} → ${j.event.payee} · ${human(j.journey_status)} · ${j.id}`;
  const signature=JSON.stringify(j.messages);
  if(signature!==lastChat){
    $('chat').innerHTML=j.messages.length?j.messages.map(msg=>`<div class="bubble ${msg.direction==='inbound'?'customer':''} ${msg.channel==='SES'?'email':''}">${escape(msg.direction==='outbound'?preview(msg):msg.body)}${buttonsHtml(msg)}<small>${msg.direction==='inbound'?'CUSTOMER':msg.channel==='SES'?'SES FOLLOW-UP':'KENA'} · ${escape(channelName(msg.channel))} · ${escape(statusLabel(msg.status))} · ${timestamp(msg.at)}</small></div>`).join(''):'<div class="empty">No outreach required</div>';
    $('chat').scrollTop=$('chat').scrollHeight; lastChat=signature;
  }
  $('proposal').hidden=!j.proposal;
  const call=j.call_context?.state || 'unknown';
  $('risk').innerHTML=`<div class="risk-score">${escape(j.risk.level)} RISK <strong>${j.risk.score}<small>/100</small></strong></div><div class="meter"><div style="width:${j.risk.score}%"></div></div>${j.risk.signals.map(s=>`<div class="signal">${escape(messageText(s))}</div>`).join('')}<div class="call-state">Call state: ${escape(call)}</div>`;
  $('case').innerHTML=j.case?`<div class="case-card">✓ Transfer paused · case open<br>${escape(j.case.id)}<br>Fraud-operations review</div>`:'';
  $('audit').innerHTML=[...j.audit].reverse().map(a=>`<div class="audit-item"><strong>${escape(human(a.kind==='demo_transfer_deleted'?'transfer_archived':a.kind==='message_dispatched'&&a.detail.status==='simulated'?'message_emulated':a.kind))}</strong><p>${escape(auditText(a))}</p><small>${timestamp(a.at)} · ${escape(policyLabel(a.policy_version))}</small></div>`).join('');
  $('outbox').innerHTML=j.messages.filter(msg=>msg.direction==='outbound').map(msg=>`<tr><td>${escape(channelName(msg.channel))}</td><td>${escape(messageText(msg.body))}</td><td><span class="badge">${escape(statusLabel(msg.status))}</span></td><td>${timestamp(msg.at)}</td></tr>`).join('') || '<tr><td colspan="4">No outgoing messages</td></tr>';
  controls();
}
$('transfer-form').onsubmit=e=>{
  e.preventDefault();
  action(async()=>{
    const input=$('amount').value.trim();
    if(!/^\d+(?:\.\d{1,2})?$/.test(input))throw new Error('Enter an amount with up to two decimal places.');
    const [dollars,cents='']=input.split('.');
    const amount_cents=Number(dollars)*100+Number(cents.padEnd(2,'0'));
    if(!Number.isSafeInteger(amount_cents)||amount_cents<1||amount_cents>100000000)throw new Error('Amount must be between S$0.01 and S$1,000,000.00.');
    const newPayee=$('new-payee').checked;
    const j=await api('/api/events',{event_id:requestId(),amount_cents,new_payee:newPayee,payee:newPayee?'New payee • 4821':'Known payee • 1930',unusual_device:$('unusual-device').checked,recent_payee_count:$('repeated-payees').checked?3:0,active_call:$('active-call').checked,preferred_channel:$('channel-pref').value});
    selected=j.id;lastChat='';
  });
};
$('delete-cancel').onclick=()=>{$('delete-dialog').close();deleteTarget=null;};
$('delete-confirm').onclick=()=>{
  const target=deleteTarget;$('delete-dialog').close();deleteTarget=null;
  if(target)action(async()=>{await api('/api/journeys/'+target.id,undefined,false,'DELETE');if(selected===target.id){selected=null;customer=null;lastChat='';}});
};
// Agent replies can take several seconds; show the message and a typing note straight away.
function showPending(text){
  $('chat').insertAdjacentHTML('beforeend',`<div class="bubble customer">${escape(text)}<small>CUSTOMER · sending</small></div><div class="bubble typing-note" role="status">KENA Bank is typing…</div>`);
  $('chat').scrollTop=$('chat').scrollHeight; lastChat='';
}
async function sendReply(text){showPending(text);await api('/api/customer/'+selected+'/replies',{message_id:requestId(),text},true);}
$('reply-form').onsubmit=e=>{e.preventDefault();const text=$('reply').value.trim();if(text)action(async()=>{await sendReply(text);$('reply').value='';});};
document.querySelectorAll('[data-reply]').forEach(el=>el.onclick=()=>action(()=>sendReply(el.dataset.reply)));
$('cancel').onclick=()=>action(()=>sendReply('cancel'));
$('confirm').onclick=()=>action(()=>api('/api/customer/'+selected+'/confirm',{command_id:requestId(),token:customer.proposal.token,approved_actions:['pause_transfer','create_scam_case']},true));
refresh().catch(showError);
setInterval(()=>{if(!busy)refresh().catch(e=>{$('connection').textContent='● DISCONNECTED';showError(e);});},2500);
