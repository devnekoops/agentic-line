document.addEventListener('click', event => {
  const copyCode = event.target.closest('[data-copy-code]');
  if (copyCode) {
    const status = document.querySelector('[data-copy-status]');
    const copied = navigator.clipboard?.writeText(copyCode.dataset.copyCode);
    if (copied) copied.then(() => {
      if (status) status.textContent = 'コードをコピーしました。GitHubの画面に貼り付けてください。';
    }).catch(() => {
      if (status) status.textContent = 'コピーできませんでした。表示されたコードを手動で入力してください。';
    });
    else if (status) status.textContent = '表示されたコードを手動で入力してください。';
  }
  const open = event.target.closest('[data-open]');
  if (open) document.getElementById(open.dataset.open).showModal();
  if (event.target.closest('[data-close]')) event.target.closest('dialog').close();
  const adopt = event.target.closest('[data-adopt]');
  if (adopt) {
    const editor = document.getElementById('spec-body');
    editor.value = JSON.parse(document.getElementById(adopt.dataset.adopt).textContent);
    editor.focus();
    editor.scrollIntoView({behavior:'smooth',block:'center'});
  }
});
document.addEventListener('change', event => {
  if (event.target.matches('[data-repo-select]')) location.href='/?repository='+encodeURIComponent(event.target.value);
});
document.addEventListener('htmx:beforeSwap', event => {
  if (event.detail.xhr.status >= 400 && event.detail.xhr.status < 500) {
    event.detail.shouldSwap = true;
    event.detail.isError = false;
  }
});
document.addEventListener('specSaved', event => {
  const version = document.querySelector('#spec-form [name=version]');
  if (version) version.value=event.detail.version;
});
document.addEventListener('htmx:sendError', () => {
  const notice=document.getElementById('notice');
  if (notice) { notice.textContent='接続が切れました。入力を保持したまま、接続後に再度操作してください。'; notice.className='notice error'; }
});
