(() => {
  const overlay = document.getElementById('toolbox');
  const openButton = document.getElementById('toolbox-open');
  const closeButton = document.getElementById('toolbox-close');
  const toolButtons = Array.from(document.querySelectorAll('[data-tool-url]'));
  const accountToggle = document.getElementById('account-toggle');
  const accountLabel = document.getElementById('account-label');
  const accountNote = document.getElementById('account-note');
  const armDelayMs = 520;
  let unlockTimer = 0;
  let openPointerId = null;
  let blockNextDocumentClick = false;
  let accountSignedIn = false;

  function setOpen(isOpen) {
    if (!overlay) return;
    window.clearTimeout(unlockTimer);
    overlay.classList.remove('is-ready');

    if (isOpen) {
      overlay.hidden = false;
      overlay.classList.add('is-open', 'is-arming');
      unlockTimer = window.setTimeout(() => {
        overlay.classList.remove('is-arming');
        overlay.classList.add('is-ready');
      }, armDelayMs);
    } else {
      overlay.classList.remove('is-open', 'is-arming');
      unlockTimer = window.setTimeout(() => {
        overlay.hidden = true;
      }, 180);
    }

    overlay.setAttribute('aria-hidden', isOpen ? 'false' : 'true');
    openButton?.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
  }

  function stopEvent(event) {
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation?.();
  }

  function openFromButton(event) {
    stopEvent(event);
    if (event.type === 'pointerdown') {
      openPointerId = event.pointerId;
      event.currentTarget?.setPointerCapture?.(event.pointerId);
    }
    blockNextDocumentClick = true;
    setOpen(true);
  }

  function blockOpeningClick(event) {
    if (event.type === 'pointerup' && openPointerId !== null && event.pointerId !== openPointerId) {
      return;
    }
    stopEvent(event);
    if (event.type === 'pointerup') {
      event.currentTarget?.releasePointerCapture?.(event.pointerId);
      openPointerId = null;
    }
  }

  openButton?.addEventListener('pointerdown', openFromButton, true);
  openButton?.addEventListener('mousedown', (event) => {
    if (!window.PointerEvent) {
      openFromButton(event);
      return;
    }
    blockOpeningClick(event);
  }, true);
  openButton?.addEventListener('pointerup', blockOpeningClick, true);
  openButton?.addEventListener('mouseup', blockOpeningClick, true);
  openButton?.addEventListener('click', blockOpeningClick, true);

  closeButton?.addEventListener('click', (event) => {
    stopEvent(event);
    setOpen(false);
  }, true);

  toolButtons.forEach((button) => {
    button.addEventListener('click', (event) => {
      stopEvent(event);
      if (!overlay?.classList.contains('is-ready')) {
        return;
      }
      const url = button.getAttribute('data-tool-url');
      if (url) {
        window.open(url, '_blank', 'noopener');
      }
    }, true);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      setOpen(false);
    }
  });

  document.addEventListener('click', (event) => {
    if (!overlay?.classList.contains('is-open')) return;
    if (blockNextDocumentClick || overlay.classList.contains('is-arming')) {
      stopEvent(event);
      blockNextDocumentClick = false;
    }
  }, true);

  accountToggle?.addEventListener('click', () => {
    accountSignedIn = !accountSignedIn;
    accountToggle.classList.toggle('is-signed-in', accountSignedIn);
    accountToggle.setAttribute('aria-pressed', accountSignedIn ? 'true' : 'false');
    if (accountLabel) accountLabel.textContent = accountSignedIn ? '退出' : '登录';
    if (accountNote) accountNote.textContent = accountSignedIn ? '当前为占位状态' : '账号体系占位';
  });
})();
