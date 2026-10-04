'use strict';
// 元の日時入力を残して段階的に拡張。送信値は従来どおりISO形式。
for (const source of document.querySelectorAll('input[type="datetime-local"]')) {
  const originalLabel = source.closest('label');
  const title = originalLabel.childNodes[0].textContent.trim();
  const required = source.required;
  const fieldset = document.createElement('fieldset');
  fieldset.className = 'datetime-picker';
  const legend = document.createElement('legend');
  legend.textContent = `${title}（日本時間・24時間表示）`;
  fieldset.append(legend);
  const fields = document.createElement('div');
  fields.className = 'datetime-fields';
  function labelled(text, control) {
    const label = document.createElement('label');
    label.textContent = text;
    control.setAttribute('aria-label', text);
    label.append(control);
    fields.append(label);
    return control;
  }
  const date = labelled('日付', document.createElement('input'));
  date.type = 'date';
  date.className = 'datetime-date';
  function select(label, count, unit) {
    const control = labelled(label, document.createElement('select'));
    control.add(new Option('—', ''));
    for (let n = 0; n < count; n++) {
      const value = String(n).padStart(2, '0');
      control.add(new Option(`${value}${unit}`, value));
    }
    return control;
  }
  const hour = select('時', 24, '時');
  const minute = select('分', 60, '分');
  function sync() {
    const any = Boolean(date.value || hour.value || minute.value);
    for (const control of [date, hour, minute]) control.required = required || any;
    source.value = date.value && hour.value && minute.value ? `${date.value}T${hour.value}:${minute.value}` : '';
    source.dispatchEvent(new Event('change', {bubbles: true}));
  }
  function set(value) {
    const [d, time = ''] = value.split('T');
    const [h = '', m = ''] = time.split(':');
    date.value = d; hour.value = h; minute.value = m;
    sync();
  }
  for (const control of [date, hour, minute]) control.addEventListener('change', sync);
  const actions = document.createElement('div');
  actions.className = 'datetime-actions';
  function action(text, callback) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'secondary'; button.textContent = text;
    button.addEventListener('click', callback); actions.append(button);
  }
  action('今の日時', () => {
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Tokyo', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    }).formatToParts(new Date()).map(p => [p.type, p.value]));
    set(`${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`);
  });
  if (!required) action('空欄に戻す', () => set(''));
  source.validateDateTime = () => [date, hour, minute].every(control => control.reportValidity());
  const value = source.value;
  source.type = 'hidden'; source.required = false;
  fieldset.append(fields, actions, source);
  originalLabel.replaceWith(fieldset);
  set(value);
}

for (const form of document.querySelectorAll('form[data-confirm]')) {
  form.addEventListener('submit', event => { if (!confirm(form.dataset.confirm)) event.preventDefault(); });
}
const panel = document.querySelector('#upload');
if (panel) {
  const input = document.querySelector('#media-file');
  const button = document.querySelector('#upload-button');
  const status = document.querySelector('#upload-status');
  let key = crypto.randomUUID();
  input.addEventListener('change', () => {
    key = crypto.randomUUID();
    const file = input.files[0];
    document.querySelector('#file-info').textContent = file ? `${file.name} · ${(file.size / 1000000).toFixed(1)} MB` : '';
  });
  button.addEventListener('click', async () => {
    const file = input.files[0];
    if (!file) { status.textContent = 'ファイルを選んでください。'; return; }
    if (!document.querySelector('#captured-at').validateDateTime()) return;
    button.disabled = true;
    const csrf = document.querySelector('meta[name="csrf-token"]').content;
    try {
      status.textContent = '容量を確認しています…';
      const response = await fetch('/api/media/reserve', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({parent_kind:panel.dataset.kind,parent_id:Number(panel.dataset.id),original_name:file.name,size:file.size,upload_key:key,captured_at:document.querySelector('#captured-at').value || null})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || '予約に失敗しました。');
      if (result.status === 'ready') { location.reload(); return; }
      if (result.status !== 'pending') { key = crypto.randomUUID(); throw new Error('前回の取り込みは終了しています。もう一度保存を押してください。'); }
      await new Promise((resolve,reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('PUT', `/api/media/${result.id}`);
        xhr.setRequestHeader('Content-Type','application/octet-stream');
        xhr.setRequestHeader('X-CSRF-Token',csrf);
        xhr.timeout = 330000;
        xhr.upload.onprogress = e => { status.textContent = e.lengthComputable ? `送信中 ${Math.round(e.loaded/e.total*100)}% · 検証完了までお待ちください` : '送信中…'; };
        xhr.onload = () => { if (xhr.status>=200 && xhr.status<300) resolve(); else { if ([413,415,422].includes(xhr.status)) key = crypto.randomUUID(); let message='添付を保存できませんでした。'; try { message=JSON.parse(xhr.responseText).detail || message; } catch (_) {} reject(new Error(message)); } };
        xhr.onerror = () => reject(new Error('通信が切れました。本文は保存されています。'));
        xhr.ontimeout = () => reject(new Error('送信時間を超えました。本文は保存されています。'));
        xhr.send(file);
      });
      status.textContent = 'MacBookへの保存が完了しました。';
      location.reload();
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  });
}

const photoButton = document.querySelector('#profile-photo-save');
if (photoButton) photoButton.addEventListener('click', async () => {
  const file = document.querySelector('#profile-photo-input').files[0];
  const status = document.querySelector('#profile-photo-status');
  if (!file) { status.textContent = '写真を選んでください。'; return; }
  if (file.size > 10000000) { status.textContent = '写真は10MB以内で選んでください。'; return; }
  photoButton.disabled = true;
  status.textContent = '写真を保存しています…';
  try {
    const response = await fetch('/api/profile-photo', {method:'PUT', headers:{
      'Content-Type':'application/octet-stream',
      'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content
    }, body:file});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '保存できませんでした。');
    location.reload();
  } catch (error) { status.textContent = error.message; }
  finally { photoButton.disabled = false; }
});
