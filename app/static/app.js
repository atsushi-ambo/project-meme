'use strict';
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
        xhr.onload = () => { if (xhr.status>=200 && xhr.status<300) resolve(); else { let message='添付を保存できませんでした。'; try { message=JSON.parse(xhr.responseText).detail || message; } catch (_) {} reject(new Error(message)); } };
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
