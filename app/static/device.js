// Customer phone emulator for channels that are not sent live. Talks to the same customer API as the bank app.
const $ = id => document.getElementById(id);
const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clock = value => new Date(value).toLocaleTimeString('en-SG', {hour:'2-digit', minute:'2-digit'});
const SUBTITLE = {WHATSAPP:'Official business account', RCS:'Verified business · RCS chat', SMS:'Text message'};
const pinned = new URLSearchParams(location.search).get('journey');
let journey = null, busy = false, signature = '', seenEmail = null, notificationTimer = null, pending = null;
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

function notify(email){
  const [subject, body] = [email.subject || 'KENA Bank', email.body.split('\n')[0]];
  $('notification-title').textContent = subject;
  $('notification-text').textContent = body;
  $('notification').hidden = false;
  clearTimeout(notificationTimer);
  notificationTimer = setTimeout(() => { $('notification').hidden = true; }, 7000);
}

function buttonsFor(msg, active){
  if(!msg.buttons?.length) return '';
  const app = journey.channel;
  if(app === 'SMS') return '';
  if(app === 'RCS' && active) return ''; // RCS shows live suggestions as chips under the thread.
  return `<div class="bubble-buttons">${msg.buttons.map(b => `<button type="button" data-button="${escape(b.id)}" ${active ? '' : 'disabled'}>${escape(b.title)}</button>`).join('')}</div>`;
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
  const messages = journey.messages.filter(m => m.channel !== 'SES' && m.status !== 'failed');
  const emails = journey.messages.filter(m => m.channel === 'SES');
  const lastEmail = emails[emails.length - 1];
  if(seenEmail === null) seenEmail = lastEmail?.id || '';
  else if(lastEmail && lastEmail.id !== seenEmail){ seenEmail = lastEmail.id; notify(lastEmail); }

  const lastOutbound = [...messages].reverse().find(m => m.direction === 'outbound');
  const waiting = messages.length && messages[messages.length - 1].direction === 'inbound' && journey.journey_status !== 'protected';
  // The agent can take several seconds: show the customer's message at once and a typing indicator until KENA answers.
  const typing = Boolean(pending) || waiting;
  $('subtitle').textContent = typing ? 'typing…' : SUBTITLE[app];
  const next = JSON.stringify([messages.map(m => [m.id, m.status]), journey.proposal?.ref, typing, pending, app]);
  if(next === signature) return;
  signature = next;
  let day = '';
  $('thread').innerHTML = messages.map(m => {
    const label = new Date(m.at).toLocaleDateString('en-SG', {day:'numeric', month:'short'});
    const divider = label !== day ? `<div class="day">${escape(label)}</div>` : '';
    day = label;
    const mine = m.direction === 'inbound';
    const ticks = mine ? `<span class="ticks">${app === 'WHATSAPP' ? '✓✓' : 'Delivered'}</span>` : '';
    const active = m === lastOutbound && !(m.buttons || []).some(b => b.id.startsWith('confirm:') && !journey.proposal);
    // Google Messages shows RCS and SMS in one thread; label anything that came over a different channel.
    const via = m.channel !== app ? `<span class="via">${m.channel === 'WHATSAPP' ? 'WhatsApp' : escape(m.channel)}</span>` : '';
    return `${divider}<div class="msg ${mine ? 'mine' : 'theirs'}"><div class="bubble">${via}${escape(m.body)}<span class="meta">${clock(m.at)}${ticks}</span></div>${mine ? '' : buttonsFor(m, active)}</div>`;
  }).join('')
    + (pending ? `<div class="msg mine"><div class="bubble">${escape(pending)}<span class="meta">${clock(Date.now())}<span class="ticks sending">✓</span></span></div></div>` : '')
    + (typing ? TYPING : '');
  $('thread').scrollTop = $('thread').scrollHeight;
  const chips = app === 'RCS' && !typing && lastOutbound?.buttons?.length ? lastOutbound.buttons : [];
  $('chips').innerHTML = chips.map(b => `<button type="button" data-button="${escape(b.id)}">${escape(b.title)}</button>`).join('');
  document.querySelectorAll('[data-button]').forEach(el => el.onclick = () => tap(el.dataset.button, el.textContent));
}

async function act(fn, shown){
  if(busy || !journey) return;
  busy = true;
  pending = shown || null;
  render();
  try { await fn(); pending = null; await refresh(); }
  catch(error){ notify({subject:'KENA Bank', body:error.message}); }
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
