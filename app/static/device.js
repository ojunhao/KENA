// Customer phone emulator for channels that are not sent live. Talks to the same customer API as the bank app.
const $ = id => document.getElementById(id);
const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clock = value => new Date(value).toLocaleTimeString('en-SG', {hour:'2-digit', minute:'2-digit'});
const SUBTITLE = {WHATSAPP:'Official business account', RCS:'Verified business · RCS chat', SMS:'Text message'};
const pinned = new URLSearchParams(location.search).get('journey');
let journey = null, busy = false, signature = '', seen = null, notificationTimer = null, pending = null;
// Which channels each messaging app shows in its thread (Google Messages shows RCS and SMS together).
const THREAD = {WHATSAPP:['WHATSAPP'], RCS:['RCS','SMS'], SMS:['SMS','RCS']};
const APP_NAME = {WHATSAPP:'WhatsApp', RCS:'Messages', SMS:'Messages', SES:'Mail'};
const TYPING = '<div class="msg theirs"><div class="bubble typing" role="status"><span class="typing-label">KENA Bank is typing</span><i></i><i></i><i></i></div></div>';

async function api(path, body){
  const response = await fetch(path, {method: body ? 'POST' : 'GET', headers: {'Content-Type':'application/json','X-Demo-Customer':'demo-customer'}, body: body ? JSON.stringify(body) : undefined});
  const result = await response.json();
  if(!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Request failed');
  return result;
}

async function refresh(){
  const dashboard = await api('/api/dashboard');
  const flagged = dashboard.journeys.filter(j => j.journey_status !== 'not_flagged');
  const target = (pinned && flagged.find(j => j.id === pinned)) || flagged[0];
  journey = target ? await api('/api/customer/' + target.id) : null;
  render();
}

function notify(message, app = 'SES'){
  const [subject, body] = [message.subject || 'KENA Bank', message.body.split('\n')[0]];
  document.querySelector('.notification-app').innerHTML = `<span class="mail-icon" aria-hidden="true">${app === 'SES' ? '✉' : '💬'}</span> ${APP_NAME[app] || 'Messages'} · now`;
  $('notification-title').textContent = subject;
  $('notification-text').textContent = body;
  $('notification').hidden = false;
  clearTimeout(notificationTimer);
  notificationTimer = setTimeout(() => { $('notification').hidden = true; }, 7000);
}

// A consent button works while its proposal is current; reply buttons work on the newest message that has buttons.
function buttonActive(msg, button, latest){
  if(button.id.startsWith('confirm:')) return Boolean(journey.proposal) && journey.proposal.ref === button.id.slice(8);
  if(journey.transaction_status !== 'pending' || msg !== latest) return false;
  return button.id !== 'reply:cancel' || Boolean(journey.proposal);
}

function buttonHtml(msg, button, latest){
  return `<button type="button" data-button="${escape(button.id)}" ${buttonActive(msg, button, latest) ? '' : 'disabled'}>${escape(button.title)}</button>`;
}

function buttonsFor(msg, latest){
  if(!msg.buttons?.length) return '';
  const app = journey.channel;
  if(app === 'SMS') return '';
  if(app === 'RCS' && msg === latest) return ''; // RCS shows the current suggestions as chips under the thread.
  return `<div class="bubble-buttons">${msg.buttons.map(b => buttonHtml(msg, b, latest)).join('')}</div>`;
}

function render(){
  if(!journey){
    $('thread').innerHTML = '<div class="empty">No messages yet</div>';
    $('chips').innerHTML = '';
    $('caption').textContent = 'Waiting for a flagged transfer…';
    return;
  }
  const app = ['WHATSAPP', 'RCS', 'SMS'].includes(journey.channel) ? journey.channel : 'SMS';
  $('phone').dataset.app = app;
  $('caption').textContent = `${app === 'WHATSAPP' ? 'WhatsApp' : app} · ${journey.id}`;
  const messages = journey.messages.filter(m => THREAD[app].includes(m.channel) && m.status !== 'failed');
  // Email and messages on other apps arrive as notifications, not in this chat.
  const elsewhere = journey.messages.filter(m => m.direction === 'outbound' && !THREAD[app].includes(m.channel) && m.status !== 'failed');
  if(seen === null) seen = new Set(elsewhere.map(m => m.id));
  for(const m of elsewhere) if(!seen.has(m.id)){ seen.add(m.id); notify(m, m.channel); }

  const latest = [...messages].reverse().find(m => m.direction === 'outbound' && m.buttons?.length);
  const waiting = messages.length && messages[messages.length - 1].direction === 'inbound' && journey.journey_status !== 'protected';
  // The agent can take several seconds: show the customer's message at once and a typing indicator until KENA answers.
  const typing = Boolean(pending) || waiting;
  $('subtitle').textContent = typing ? 'typing…' : SUBTITLE[app];
  const next = JSON.stringify([messages.map(m => [m.id, m.status]), journey.proposal?.ref, journey.transaction_status, typing, pending, app]);
  if(next === signature) return;
  signature = next;
  let day = '';
  $('thread').innerHTML = messages.map(m => {
    const label = new Date(m.at).toLocaleDateString('en-SG', {day:'numeric', month:'short'});
    const divider = label !== day ? `<div class="day">${escape(label)}</div>` : '';
    day = label;
    const mine = m.direction === 'inbound';
    const ticks = mine ? `<span class="ticks">${app === 'WHATSAPP' ? '✓✓' : 'Delivered'}</span>` : '';
    // Google Messages shows RCS and SMS in one thread; label anything that came over a different channel.
    const via = m.channel !== app ? `<span class="via">${m.channel === 'WHATSAPP' ? 'WhatsApp' : escape(m.channel)}</span>` : '';
    return `${divider}<div class="msg ${mine ? 'mine' : 'theirs'}"><div class="bubble">${via}${escape(m.body)}<span class="meta">${clock(m.at)}${ticks}</span></div>${mine ? '' : buttonsFor(m, latest)}</div>`;
  }).join('')
    + (pending ? `<div class="msg mine"><div class="bubble">${escape(pending)}<span class="meta">${clock(Date.now())}<span class="ticks sending">✓</span></span></div></div>` : '')
    + (typing ? TYPING : '');
  $('thread').scrollTop = $('thread').scrollHeight;
  const chips = app === 'RCS' && !typing && latest ? latest.buttons.filter(b => buttonActive(latest, b, latest)) : [];
  $('chips').innerHTML = chips.map(b => buttonHtml(latest, b, latest)).join('');
  document.querySelectorAll('[data-button]').forEach(el => el.onclick = () => tap(el.dataset.button, el.textContent));
}

async function act(fn, shown){
  if(busy || !journey) return;
  busy = true;
  pending = shown || null;
  render();
  try { await fn(); pending = null; await refresh(); }
  catch(error){ notify({subject:'KENA Bank', body:error.message}, journey?.channel || 'SMS'); }
  finally { busy = false; pending = null; render(); }
}

function tap(id, title){
  act(async () => {
    if(id.startsWith('confirm:')){
      const proposal = journey.proposal;
      if(!proposal || proposal.ref !== id.slice(8)) throw new Error('This confirmation is no longer valid. Reply "not me" to request a new one.');
      await api(`/api/customer/${journey.id}/confirm`, {command_id:crypto.randomUUID(), token:proposal.token,
        approved_actions:['pause_transfer','create_scam_case'], channel:journey.channel});
    } else {
      await api(`/api/customer/${journey.id}/replies`, {message_id:crypto.randomUUID(), text:title, button_id:id});
    }
  }, id.startsWith('confirm:') ? 'Tapped: ' + title : title);
}

$('composer').onsubmit = event => {
  event.preventDefault();
  const text = $('text').value.trim();
  if(!text) return;
  $('text').value = '';
  act(() => api(`/api/customer/${journey.id}/replies`, {message_id:crypto.randomUUID(), text}), text);
};
const tick = () => { $('clock').textContent = new Date().toLocaleTimeString('en-SG', {hour:'numeric', minute:'2-digit', hour12:false}); };
tick();
setInterval(tick, 1000);
refresh().catch(() => {});
setInterval(() => { if(!busy) refresh().catch(() => {}); }, 1500);
