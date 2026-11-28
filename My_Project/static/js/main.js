// PayrollX UI Engine — Progressive Enhancement & Accessible Controls
'use strict';

(() => {
  const COLOR_SCHEME_KEY = 'color-scheme';
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
  let supportsDisplayTransition;

  // ─── 1. Color Scheme Toggle & Tab Sync (dark-mode-toggle) ───
  function getSystemScheme() {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function getStoredScheme() {
    try {
      const v = localStorage.getItem(COLOR_SCHEME_KEY);
      return v === 'light' || v === 'dark' ? v : null;
    } catch {
      return null;
    }
  }

  function applyColorSchemeMeta(contentValue) {
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) {
      meta.setAttribute('content', contentValue);
    }
  }

  function setColorSchemePreference(choice) {
    // choice: 'light dark' | 'light' | 'dark'
    try {
      if (choice === 'light dark') {
        localStorage.removeItem(COLOR_SCHEME_KEY);
      } else {
        localStorage.setItem(COLOR_SCHEME_KEY, choice);
      }
    } catch {
      // Proceed even if storage is restricted
    }
    applyColorSchemeMeta(choice);
    syncSchemeRadios();
  }

  function toggleTwoStateColorScheme() {
    const current = getStoredScheme() ?? getSystemScheme();
    const target = current === 'dark' ? 'light' : 'dark';
    const nextContent = target === getSystemScheme() ? 'light dark' : target;
    setColorSchemePreference(nextContent);
  }

  function syncSchemeRadios() {
    const stored = getStoredScheme();
    const effectiveChoice = stored ?? 'light dark';
    document.querySelectorAll('input[name="appearance_scheme"]').forEach((radio) => {
      radio.checked = radio.value === effectiveChoice;
    });
    const sysHint = document.getElementById('systemSchemeCurrentLabel');
    if (sysHint) {
      sysHint.textContent = `System (Currently ${getSystemScheme()})`;
    }
  }

  function initColorSchemeControls() {
    syncSchemeRadios();

    document.addEventListener('click', (e) => {
      if (e.target.closest('[data-action="toggle-color-scheme"]')) {
        toggleTwoStateColorScheme();
      }
    });

    document.addEventListener('change', (e) => {
      const radio = e.target.closest('input[name="appearance_scheme"]');
      if (radio) {
        setColorSchemePreference(radio.value);
        showToast(`Theme updated to ${radio.value === 'light dark' ? 'System default' : `${radio.value} mode`}.`);
      }
    });

    window.addEventListener('storage', (e) => {
      if (e.key === COLOR_SCHEME_KEY) {
        applyColorSchemeMeta(e.newValue ?? 'light dark');
        syncSchemeRadios();
      }
    });

    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      // Never retroactively delete stored preference on system change; only refresh display text
      syncSchemeRadios();
    });
  }

  // ─── 2. Native <dialog> Top-Layer Exit & Light-Dismiss Fallbacks ───
  function canTransitionDisplay() {
    if (supportsDisplayTransition !== undefined) return supportsDisplayTransition;
    if (!window.CSS?.supports?.('transition-behavior', 'allow-discrete') || !document.body) {
      return false;
    }
    const probe = document.createElement('div');
    probe.style.cssText = 'transition: display 1s allow-discrete !important; display: block;';
    document.body.appendChild(probe);
    getComputedStyle(probe).display;
    probe.style.display = 'none';
    supportsDisplayTransition = getComputedStyle(probe).display === 'block';
    probe.remove();
    return supportsDisplayTransition;
  }

  function supportsTopLayerExit() {
    return window.CSS?.supports?.('overlay', 'auto') && canTransitionDisplay();
  }

  async function closeTopLayer(element, returnValue = '') {
    if (!element) return;
    if (!supportsTopLayerExit() && element.hasAttribute('open')) {
      element.setAttribute('data-closing', '');
      const animations = element.getAnimations({ subtree: true });
      if (animations.length > 0) {
        await Promise.race([
          Promise.allSettled(animations.map((a) => a.finished)),
          new Promise((r) => setTimeout(r, 400)),
        ]);
      }
      if (!element.hasAttribute('data-closing')) return;
      element.removeAttribute('data-closing');
    }
    if (typeof element.close === 'function' && element.open) {
      element.close(returnValue);
    }
  }

  function initDialogs() {
    // Invoker commands fallback for show-modal / close / request-close
    if (!('commandForElement' in HTMLButtonElement.prototype)) {
      document.addEventListener('click', (e) => {
        const btn = e.target.closest('button[commandfor][command]');
        if (!btn) return;
        const cmd = btn.getAttribute('command');
        const targetId = btn.getAttribute('commandfor');
        const target = document.getElementById(targetId);
        if (!target || target.tagName !== 'DIALOG') return;
        if (cmd === 'show-modal' && typeof target.showModal === 'function') {
          target.showModal();
        } else if (cmd === 'close') {
          closeTopLayer(target, btn.value || '');
        } else if (cmd === 'request-close') {
          const ev = new Event('cancel', { cancelable: true });
          if (target.dispatchEvent(ev)) {
            closeTopLayer(target, btn.value || '');
          }
        }
      });
    }

    document.querySelectorAll('dialog.app-dialog').forEach((dialog) => {
      // Ensure returnValue can be read by specific close handlers first, then reset asynchronously
      dialog.addEventListener('close', () => {
        dialog.removeAttribute('data-closing');
        queueMicrotask(() => {
          dialog.returnValue = '';
        });
      });

      // Graceful animated cancel fallback
      dialog.addEventListener('cancel', (event) => {
        if (!supportsTopLayerExit() && event.cancelable) {
          event.preventDefault();
          closeTopLayer(dialog, '');
        }
      });

      // Fallback for browsers missing closedby="any"
      if (!('closedBy' in HTMLDialogElement.prototype) && dialog.getAttribute('closedby') === 'any') {
        const isOutside = (e) => {
          const r = dialog.getBoundingClientRect();
          return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
        };
        let pressedOutside = false;
        dialog.addEventListener('pointerdown', (e) => {
          pressedOutside = isOutside(e);
        });
        dialog.addEventListener('pointerup', (e) => {
          if (pressedOutside && isOutside(e)) {
            closeTopLayer(dialog, '');
          }
        });
      }
    });

    // Global keyboard shortcut Cmd/Ctrl+K -> Quick Jump dialog
    const quickJump = document.getElementById('quickJumpDialog');
    if (quickJump) {
      document.addEventListener('keydown', (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
          e.preventDefault();
          if (quickJump.open) {
            closeTopLayer(quickJump);
          } else {
            quickJump.showModal();
            quickJump.querySelector('input[type="search"]')?.focus();
          }
        }
      });

      const quickInput = quickJump.querySelector('#quickJumpInput');
      const quickItems = quickJump.querySelectorAll('[data-jump-item]');
      const quickEmpty = quickJump.querySelector('#quickJumpEmpty');
      if (quickInput) {
        quickInput.addEventListener('input', () => {
          const q = quickInput.value.trim().toLowerCase();
          let matches = 0;
          quickItems.forEach((item) => {
            const hay = item.textContent.toLowerCase();
            const show = !q || hay.includes(q);
            item.hidden = !show;
            if (show) matches += 1;
          });
          if (quickEmpty) quickEmpty.hidden = matches > 0;
        });
      }
    }
  }

  // Confirmation dialog controller replacing blocking window.confirm()
  function initConfirmForms() {
    const confirmDialog = document.getElementById('confirmActionDialog');
    if (!confirmDialog) return;
    const msgEl = confirmDialog.querySelector('#confirmActionMessage');
    const titleEl = confirmDialog.querySelector('#confirmActionTitle');
    const okBtn = confirmDialog.querySelector('#confirmActionProceed');
    let pendingForm = null;

    document.addEventListener('submit', (e) => {
      const form = e.target;
      if (!(form instanceof HTMLFormElement)) return;
      const confirmMsg = form.dataset.confirm;
      if (!confirmMsg || form.dataset.confirmed === 'true') return;
      e.preventDefault();
      pendingForm = form;
      if (titleEl) titleEl.textContent = form.dataset.confirmTitle || 'Confirm Action';
      if (msgEl) msgEl.textContent = confirmMsg;
      if (okBtn) okBtn.textContent = form.dataset.confirmCta || 'Confirm';
      confirmDialog.showModal();
    });

    confirmDialog.addEventListener('close', () => {
      if (confirmDialog.returnValue === 'confirm' && pendingForm) {
        pendingForm.dataset.confirmed = 'true';
        const submitBtn = pendingForm.querySelector('button[type="submit"]');
        if (submitBtn) submitBtn.disabled = true;
        pendingForm.submit();
      }
      pendingForm = null;
      confirmDialog.returnValue = '';
    });
  }

  // Keep global signature intact in case external scripts reference it
  window.confirmAction = (message) => window.confirm(message || 'Are you sure you want to proceed?');

  // ─── 3. Swipeable Mobile Navigation Drawer (navigation-drawer) ───
  function initNavigationDrawer() {
    const drawer = document.getElementById('app-drawer');
    const openBtn = document.getElementById('drawer-open');
    const closeBtn = document.getElementById('drawer-close');
    if (!drawer || !openBtn) return;

    const scroller = drawer.querySelector('.Drawer-scroller');
    const sheet = drawer.querySelector('.Drawer-sheet');
    if (!scroller || !sheet) return;

    async function openDrawer() {
      if (drawer.open) return;
      drawer.showModal();
      if (!CSS.supports('scroll-initial-target', 'nearest')) {
        scroller.scrollTo({ left: scroller.offsetWidth, behavior: 'instant' });
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
      }
      scroller.scrollTo({ left: 0, behavior: 'auto' });
    }

    function closeDrawer() {
      scroller.scrollTo({ left: scroller.offsetWidth, behavior: 'auto' });
    }

    if (!CSS.supports('animation-timeline: scroll()')) {
      scroller.addEventListener('scroll', () => {
        const width = sheet.offsetWidth || 1;
        const ratio = Math.max(0, Math.min(1, 1 - scroller.scrollLeft / width));
        drawer.style.setProperty('--drawer-backdrop', String(ratio));
      });
    }

    const visibleThreshold = 1 / Math.max(window.innerWidth, 320);
    const observer = new IntersectionObserver(
      (entries) => {
        const entry = entries.at(-1);
        if (!entry) return;
        if (entry.intersectionRatio < visibleThreshold && drawer.open) {
          drawer.close();
          openBtn.setAttribute('aria-expanded', 'false');
        }
        if (entry.intersectionRatio === 1) {
          openBtn.setAttribute('aria-expanded', 'true');
        }
      },
      { root: drawer, threshold: [visibleThreshold, 1] },
    );
    observer.observe(sheet);

    openBtn.addEventListener('click', openDrawer);
    if (closeBtn) closeBtn.addEventListener('click', closeDrawer);

    drawer.addEventListener('click', (event) => {
      if (!sheet.contains(event.target)) {
        closeDrawer();
      }
    });

    drawer.addEventListener('cancel', (event) => {
      if (event.cancelable) {
        event.preventDefault();
        closeDrawer();
      }
    });
  }

  // ─── 4. Popover Fallback for User Menu & Notifications ───
  function initPopoverFallback() {
    if (Object.hasOwn(HTMLElement.prototype, 'popover')) return;
    document.addEventListener('click', (e) => {
      const trigger = e.target.closest('[popovertarget]');
      if (trigger) {
        const target = document.getElementById(trigger.getAttribute('popovertarget'));
        if (!target) return;
        const isOpen = target.classList.contains('is-open-fallback');
        document.querySelectorAll('[popover].is-open-fallback').forEach((el) => {
          el.classList.remove('is-open-fallback');
        });
        if (!isOpen) target.classList.add('is-open-fallback');
        return;
      }
      if (!e.target.closest('[popover]')) {
        document.querySelectorAll('[popover].is-open-fallback').forEach((el) => {
          el.classList.remove('is-open-fallback');
        });
      }
    });
  }

  // ─── 5. Interactive Toast Notifications & Clipboard Copy ───
  let lastTriggerBeforeToast = null;

  function showToast(message, tone = 'info') {
    const region = document.getElementById('toast-region');
    if (!region) return;
    lastTriggerBeforeToast = document.activeElement instanceof HTMLElement ? document.activeElement : null;

    const toast = document.createElement('div');
    toast.className = `toast-card ${tone}`;
    toast.setAttribute('popover', 'manual');

    const textSpan = document.createElement('span');
    textSpan.textContent = message;

    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'flash-close';
    closeBtn.setAttribute('aria-label', 'Dismiss notification');
    closeBtn.innerHTML = '<i class="fas fa-times" aria-hidden="true"></i>';

    toast.append(textSpan, closeBtn);
    region.appendChild(toast);

    const dismiss = () => {
      const next = toast.nextElementSibling || toast.previousElementSibling;
      const hadFocus = toast.contains(document.activeElement);
      if (typeof toast.hidePopover === 'function' && toast.matches(':popover-open')) {
        toast.hidePopover();
      } else {
        toast.classList.remove('is-open-fallback');
      }
      toast.remove();
      if (hadFocus) {
        if (next instanceof HTMLElement) {
          next.querySelector('button')?.focus();
        } else if (lastTriggerBeforeToast && document.contains(lastTriggerBeforeToast)) {
          lastTriggerBeforeToast.focus();
        }
      }
    };

    closeBtn.addEventListener('click', dismiss);

    if (typeof toast.showPopover === 'function') {
      toast.showPopover();
    } else {
      toast.classList.add('is-open-fallback');
    }

    if (typeof navigator !== 'undefined' && typeof document.ariaNotify === 'function') {
      document.ariaNotify(message);
    }

    let timer = setTimeout(dismiss, 4500);
    toast.addEventListener('focusin', () => clearTimeout(timer));
    toast.addEventListener('pointerenter', () => clearTimeout(timer));
    toast.addEventListener('focusout', () => {
      timer = setTimeout(dismiss, 2500);
    });
    toast.addEventListener('pointerleave', () => {
      timer = setTimeout(dismiss, 2500);
    });
  }

  window.showToast = showToast;

  function initFlashCloseButtons() {
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('.flash-close[data-dismiss-flash]');
      if (!btn) return;
      btn.closest('.flash-msg')?.remove();
    });
  }

  function initCopyControls() {
    document.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-copy-text]');
      if (!btn) return;
      const text = btn.dataset.copyText;
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        showToast(btn.dataset.copySuccess || `Copied "${text}" to clipboard.`, 'success');
      } catch {
        showToast('Copy not supported in this context — please select and copy manually.', 'error');
      }
    });
  }

  // ─── 6. Sortable Tables, Scroll Shadows, Search Filtering & CSV Export ───
  function reorderRows(container, orderedNodes) {
    const move = Object.hasOwn(Element.prototype, 'moveBefore')
      ? (node) => container.moveBefore(node, null)
      : (node) => container.append(node);
    for (const node of orderedNodes) move(node);
  }

  function reorderWithTransition(table, tbody, rows) {
    const update = () => reorderRows(tbody, rows);
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!reduced && rows.length <= 150) {
      if (typeof table.startViewTransition === 'function') {
        table.startViewTransition(update);
        return;
      }
      if (typeof document.startViewTransition === 'function') {
        document.startViewTransition(update);
        return;
      }
    }
    update();
  }

  function initSortableTables() {
    document.querySelectorAll('table[data-sortable]').forEach((table) => {
      const tbody = table.tBodies[0];
      const theadRow = table.tHead?.rows[0];
      if (!tbody || !theadRow) return;

      let statusEl = document.getElementById(`${table.id}-sort-status`);
      if (!statusEl) {
        statusEl = document.createElement('p');
        statusEl.id = `${table.id}-sort-status`;
        statusEl.className = 'visually-hidden';
        statusEl.setAttribute('role', 'status');
        table.after(statusEl);
      }

      const sortBy = (button) => {
        const th = button.closest('th');
        if (!th) return;
        const direction = th.getAttribute('aria-sort') === 'ascending' ? 'descending' : 'ascending';
        for (const header of theadRow.cells) {
          header.removeAttribute('aria-sort');
        }
        th.setAttribute('aria-sort', direction);

        const idx = th.cellIndex;
        const type = th.dataset.type || 'text';
        const factor = direction === 'ascending' ? 1 : -1;
        const allRows = [...tbody.rows];
        const pinnedTotal = allRows.filter((r) => r.classList.contains('table-total-row'));
        const sortableRows = allRows.filter((r) => !r.classList.contains('table-total-row'));

        const readValue = (row) => {
          const cell = row.cells[idx];
          if (!cell) return '';
          if (cell.dataset.value !== undefined) return cell.dataset.value;
          return cell.textContent.trim();
        };

        sortableRows.sort((a, b) => {
          const va = readValue(a);
          const vb = readValue(b);
          if (type === 'number') {
            const na = Number.parseFloat(String(va).replace(/[^0-9.-]/g, '')) || 0;
            const nb = Number.parseFloat(String(vb).replace(/[^0-9.-]/g, '')) || 0;
            return factor * (na - nb);
          }
          return factor * collator.compare(va, vb);
        });

        reorderWithTransition(table, tbody, [...sortableRows, ...pinnedTotal]);
        statusEl.textContent = `Sorted by ${button.textContent.trim()}, ${direction}, ${sortableRows.length} rows`;
      };

      if ('commandForElement' in HTMLButtonElement.prototype) {
        table.addEventListener('command', (event) => {
          if (event.command === '--sort' && event.source instanceof HTMLButtonElement) {
            sortBy(event.source);
          }
        });
      } else {
        theadRow.querySelectorAll('button[command="--sort"]').forEach((btn) => {
          btn.addEventListener('click', () => sortBy(btn));
        });
      }
    });
  }

  function initScrollabilityFallbacks() {
    if (CSS.supports('container-type', 'scroll-state')) return;
    document.querySelectorAll('.table-wrapper').forEach((scroller) => {
      const table = scroller.querySelector('table');
      if (!table) return;
      const [startSentinel, endSentinel] = [document.createElement('div'), document.createElement('div')];
      [startSentinel, endSentinel].forEach((s) => {
        s.setAttribute('aria-hidden', 'true');
        s.style.cssText = 'min-inline-size: 1px; min-block-size: 1px; pointer-events: none;';
      });
      scroller.prepend(startSentinel);
      scroller.append(endSentinel);

      const observer = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) {
            if (entry.target === startSentinel) {
              scroller.classList.toggle('more-start', !entry.isIntersecting);
            } else if (entry.target === endSentinel) {
              scroller.classList.toggle('more-end', !entry.isIntersecting);
            }
          }
        },
        { root: scroller, threshold: 0 },
      );
      observer.observe(startSentinel);
      observer.observe(endSentinel);
    });
  }

  function initTableFilters() {
    document.querySelectorAll('[data-table-filter]').forEach((input) => {
      const targetId = input.dataset.tableFilter;
      const table = document.getElementById(targetId);
      const tbody = table?.tBodies[0];
      if (!tbody) return;
      const statusEl = document.getElementById(`${targetId}-filter-status`);
      let timer;

      input.addEventListener('input', () => {
        const query = input.value.trim().toLowerCase();
        const rows = [...tbody.rows].filter((r) => !r.classList.contains('table-total-row'));
        let visible = 0;
        for (const row of rows) {
          const match = !query || row.textContent.toLowerCase().includes(query);
          row.hidden = !match;
          if (match) visible += 1;
        }
        clearTimeout(timer);
        if (statusEl) {
          timer = setTimeout(() => {
            statusEl.textContent = `${visible} of ${rows.length} rows matching`;
          }, 250);
        }
      });
    });
  }

  function initCsvExports() {
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-export-csv]');
      if (!btn) return;
      const table = document.getElementById(btn.dataset.exportCsv);
      if (!table) return;
      const rows = [...table.querySelectorAll('tr')].filter((tr) => !tr.hidden);
      const csv = rows
        .map((row) =>
          [...row.cells]
            .map((cell) => {
              const raw = cell.textContent.replace(/\s+/g, ' ').trim().replaceAll('"', '""');
              return `"${raw}"`;
            })
            .join(','),
        )
        .join('\n');
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = btn.dataset.exportFilename || 'payrollx-export.csv';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      showToast('CSV export downloaded.', 'success');
    });
  }

  // ─── 7. Accessible ARIA Validation Sync & Password Toggles ───
  function initAccessibleValidation() {
    const linkError = (el, shown) => {
      const errorId = el.dataset.error;
      if (!errorId) return;
      const ids = (el.getAttribute('aria-describedby') || '').split(/\s+/).filter((id) => id && id !== errorId);
      if (shown) ids.push(errorId);
      if (ids.length) el.setAttribute('aria-describedby', ids.join(' '));
      else el.removeAttribute('aria-describedby');
    };

    const announcer = document.createElement('div');
    announcer.setAttribute('aria-live', 'polite');
    announcer.className = 'visually-hidden';
    document.body.appendChild(announcer);
    let announceTimer;
    let announcedField;

    const spokenText = (node) => {
      const copy = node.cloneNode(true);
      copy.querySelectorAll('[aria-hidden="true"]').forEach((n) => n.remove());
      copy.querySelectorAll('[role="img"][aria-label]').forEach((n) => n.replaceWith(`${n.getAttribute('aria-label')}: `));
      return copy.textContent.replace(/\s+/g, ' ').trim();
    };

    const announceError = (el) => {
      const error = document.getElementById(el.dataset.error);
      if (!error) return;
      const label = el.labels?.[0] ? `${spokenText(el.labels[0])}: ` : '';
      clearTimeout(announceTimer);
      announcer.textContent = '';
      announcedField = el;
      announceTimer = setTimeout(() => {
        announcer.textContent = label + spokenText(error);
      }, 450);
    };

    const supportsUserInvalid = CSS.supports('selector(:user-invalid)');
    const dirtyState = new WeakMap();
    const flagged = new WeakSet();
    const announced = new WeakSet();

    const syncAria = (el, announce = false, forceInvalid = false) => {
      if (!el.matches?.('input, select, textarea') || el.type === 'hidden') return;
      const invalid = supportsUserInvalid
        ? el.matches(':user-invalid')
        : forceInvalid || (dirtyState.get(el)?.hasBlurred && !el.validity.valid);

      if (!supportsUserInvalid && (forceInvalid || dirtyState.get(el)?.hasBlurred)) {
        el.classList.toggle('user-invalid-fallback', !el.validity.valid);
        el.classList.toggle('user-valid-fallback', el.validity.valid);
      }

      if (invalid) {
        if (announce && !announced.has(el)) {
          announceError(el);
          announced.add(el);
        }
        el.setAttribute('aria-invalid', 'true');
        linkError(el, true);
        flagged.add(el);
      } else if (flagged.has(el) || el.getAttribute('aria-invalid') !== 'true') {
        el.setAttribute('aria-invalid', 'false');
        linkError(el, false);
        flagged.delete(el);
        announced.delete(el);
        if (announcedField === el) {
          clearTimeout(announceTimer);
          announcer.textContent = '';
        }
      }
    };

    document.querySelectorAll('input, select, textarea').forEach((el) => syncAria(el));

    document.addEventListener(
      'blur',
      (e) => {
        const el = e.target;
        const st = dirtyState.get(el) || { hasInteracted: false, hasBlurred: false };
        st.hasBlurred = true;
        dirtyState.set(el, st);
        syncAria(el, true);
      },
      true,
    );

    document.addEventListener('input', (e) => {
      const el = e.target;
      const st = dirtyState.get(el) || { hasInteracted: false, hasBlurred: false };
      st.hasInteracted = true;
      dirtyState.set(el, st);
      if (el.hasAttribute('aria-invalid')) syncAria(el);
    });

    document.addEventListener(
      'change',
      (e) => {
        const el = e.target;
        const st = dirtyState.get(el) || { hasInteracted: false, hasBlurred: false };
        st.hasInteracted = true;
        st.hasBlurred = true;
        dirtyState.set(el, st);
        syncAria(el, true);
      },
      true,
    );

    document.addEventListener(
      'invalid',
      (e) => {
        const el = e.target;
        dirtyState.set(el, { hasInteracted: true, hasBlurred: true });
        announced.add(el);
        syncAria(el, false, true);
      },
      true,
    );

    document.addEventListener(
      'reset',
      (e) => {
        setTimeout(() => {
          const elements = e.target?.elements || [];
          for (const el of elements) {
            dirtyState.delete(el);
            el.classList?.remove('user-invalid-fallback', 'user-valid-fallback');
            syncAria(el);
          }
        });
      },
      true,
    );

    // Prevent accidental double submissions after valid submit without mutating disabled synchronously
    document.addEventListener('submit', (e) => {
      const form = e.target;
      if (!(form instanceof HTMLFormElement) || e.defaultPrevented) return;
      if (form.dataset.confirm && form.dataset.confirmed !== 'true') return;
      if (form.method.toLowerCase() === 'post') {
        if (form.dataset.submitting === 'true') {
          e.preventDefault();
          return;
        }
        form.dataset.submitting = 'true';
        const submitter = e.submitter || form.querySelector('button[type="submit"]');
        if (submitter && !submitter.hasAttribute('formnovalidate')) {
          submitter.setAttribute('aria-disabled', 'true');
        }
      }
    });
  }

  function initPasswordToggles() {
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-toggle-password]');
      if (!btn) return;
      const input = document.getElementById(btn.dataset.togglePassword);
      if (!input) return;
      const showing = input.type === 'text';
      input.type = showing ? 'password' : 'text';
      btn.setAttribute('aria-pressed', String(!showing));
      btn.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
      const icon = btn.querySelector('i');
      if (icon) {
        icon.className = showing ? 'fas fa-eye' : 'fas fa-eye-slash';
      }
    });
  }

  // ─── 8. Accessible ARIA Tabs (Login Admin / Employee Toggle) ───
  function initTabs() {
    document.querySelectorAll('[role="tablist"]').forEach((tablist) => {
      const tabs = [...tablist.querySelectorAll('[role="tab"]')];
      const activateTab = (targetTab, moveFocus = false) => {
        tabs.forEach((tab) => {
          const isSelected = tab === targetTab;
          tab.setAttribute('aria-selected', String(isSelected));
          tab.tabIndex = isSelected ? 0 : -1;
          tab.toggleAttribute('focusgroupstart', isSelected);

          const panelId = tab.dataset.panel;
          const panel = panelId ? document.getElementById(panelId) : null;
          if (panel) {
            panel.hidden = !isSelected;
            panel.querySelectorAll('input, select').forEach((field) => {
              if (isSelected && field.dataset.required === 'true') {
                field.required = true;
                field.disabled = false;
              } else if (!isSelected) {
                field.required = false;
                field.disabled = true;
              }
            });
          }
        });

        // Update login_mode hidden field & copy when present
        if (targetTab.dataset.loginMode) {
          const modeField = document.getElementById('login_mode');
          if (modeField) modeField.value = targetTab.dataset.loginMode;
        }

        if (moveFocus) targetTab.focus();
      };

      const initial = tabs.find((t) => t.getAttribute('aria-selected') === 'true') || tabs[0];
      if (initial) activateTab(initial);

      tablist.addEventListener('click', (e) => {
        const tab = e.target.closest('[role="tab"]');
        if (tab) activateTab(tab, true);
      });

      tablist.addEventListener('keydown', (e) => {
        const current = tabs.indexOf(document.activeElement);
        if (current === -1) return;
        let nextIndex = null;
        if (e.key === 'ArrowRight') nextIndex = (current + 1) % tabs.length;
        else if (e.key === 'ArrowLeft') nextIndex = (current - 1 + tabs.length) % tabs.length;
        else if (e.key === 'Home') nextIndex = 0;
        else if (e.key === 'End') nextIndex = tabs.length - 1;
        if (nextIndex !== null) {
          e.preventDefault();
          activateTab(tabs[nextIndex], true);
        }
      });
    });

    // Quick-fill demo credentials on Login page
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-demo-mode]');
      if (!btn) return;
      const mode = btn.dataset.demoMode;
      const tab = document.querySelector(`[role="tab"][data-login-mode="${mode}"]`);
      if (tab) tab.click();
      const idInput = document.getElementById(mode === 'employee' ? 'employee_id' : 'username');
      const pwInput = document.getElementById('password');
      if (idInput) {
        idInput.value = btn.dataset.demoUser || '';
        idInput.dispatchEvent(new Event('input', { bubbles: true }));
      }
      if (pwInput) {
        pwInput.value = btn.dataset.demoPass || '';
        pwInput.dispatchEvent(new Event('input', { bubbles: true }));
      }
      showToast(`Filled ${mode} demo credentials (${btn.dataset.demoUser}). Ready to sign in.`, 'info');
    });
  }

  // ─── 9. Attendance Page Helpers & Documents Preview Dialog ───
  function initPageEnhancements() {
    // Attendance prefill buttons (no inline onclick)
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-prefill-attendance]');
      if (!btn) return;
      const form = document.getElementById('attendanceForm');
      if (!form) return;
      document.getElementById('att_emp_id').value = btn.dataset.empId || '';
      document.getElementById('att_working').value = btn.dataset.working || 22;
      document.getElementById('att_present').value = btn.dataset.present || 22;
      document.getElementById('att_absent').value = btn.dataset.absent || 0;
      document.getElementById('att_lop').value = btn.dataset.lop || 0;
      document.getElementById('att_ot').value = btn.dataset.ot || 0;
      form.scrollIntoView({ behavior: 'smooth', block: 'center' });
      document.getElementById('att_present')?.focus();
    });

    const presentField = document.getElementById('att_present');
    const workingField = document.getElementById('att_working');
    const absentField = document.getElementById('att_absent');
    if (presentField && workingField && absentField) {
      const syncAbsent = () => {
        const w = Number.parseInt(workingField.value, 10) || 0;
        const p = Number.parseInt(presentField.value, 10) || 0;
        absentField.value = String(Math.max(0, w - p));
      };
      presentField.addEventListener('input', syncAbsent);
      workingField.addEventListener('input', syncAbsent);
    }

    // Printable payslip trigger
    const printBtn = document.getElementById('printPayslipBtn');
    if (printBtn) {
      printBtn.addEventListener('click', () => window.print());
    }

    // Documents download preview
    document.addEventListener('click', (e) => {
      const docBtn = e.target.closest('[data-doc-download]');
      if (!docBtn) return;
      const title = docBtn.dataset.docTitle || 'PayrollX Document';
      const meta = docBtn.dataset.docMeta || 'Policy Summary';
      const content = `${title}\n${meta}\nGenerated by PayrollX Document Vault\n----------------------------------------\nThis summary document is ready for internal HR & employee distribution.`;
      const blob = new Blob([content], { type: 'text/plain;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${title.toLowerCase().replace(/[^a-z0-9]+/g, '-')}.txt`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      showToast(`Downloaded summary for "${title}".`, 'success');
    });

    // Live CTC & Statutory Preview Calculator on Landing Pages
    const ctcSlider = document.getElementById('ctcCalcInput');
    if (ctcSlider) {
      const inr = new Intl.NumberFormat('en-IN', {
        style: 'currency',
        currency: 'INR',
        maximumFractionDigits: 0,
      });

      const updateCalc = () => {
        const annualCtc = Number.parseInt(ctcSlider.value, 10) || 1200000;
        const pct = ((annualCtc - 300000) / (4000000 - 300000)) * 100;
        ctcSlider.style.setProperty('--progress', `${pct}%`);

        const monthlyGross = Math.round(annualCtc / 12);
        const monthlyBasic = Math.round(monthlyGross * 0.4);
        const epf = Math.round(Math.min(monthlyBasic, 15000) * 0.12);
        const esi = monthlyGross <= 21000 ? Math.round(monthlyGross * 0.0075) : 0;
        const pt = monthlyGross > 20000 ? 200 : monthlyGross > 15000 ? 150 : 0;
        // Simplified New Regime estimate (FY 2025-26 ₹75,000 standard deduction & ₹12L rebate)
        const annualTaxable = Math.max(0, annualCtc - epf * 12 - 75000);
        let annualTax = 0;
        if (annualTaxable > 1200000) {
          if (annualTaxable > 400000) annualTax += Math.min(annualTaxable - 400000, 400000) * 0.05;
          if (annualTaxable > 800000) annualTax += Math.min(annualTaxable - 800000, 400000) * 0.1;
          if (annualTaxable > 1200000) annualTax += Math.min(annualTaxable - 1200000, 400000) * 0.15;
          if (annualTaxable > 1600000) annualTax += Math.min(annualTaxable - 1600000, 400000) * 0.2;
          if (annualTaxable > 2000000) annualTax += Math.min(annualTaxable - 2000000, 400000) * 0.25;
          if (annualTaxable > 2400000) annualTax += (annualTaxable - 2400000) * 0.3;
          annualTax *= 1.04; // 4% cess
        }
        const tds = Math.round(annualTax / 12);
        const net = Math.max(0, monthlyGross - epf - esi - pt - tds);

        document.getElementById('ctcCalcLabel').textContent = `${inr.format(annualCtc)} / yr`;
        document.getElementById('calcGrossVal').textContent = inr.format(monthlyGross);
        document.getElementById('calcStatVal').textContent = `${inr.format(epf + esi + pt)} (EPF+ESI+PT)`;
        document.getElementById('calcTdsVal').textContent = inr.format(tds);
        document.getElementById('calcNetVal').textContent = inr.format(net);
      };

      ctcSlider.addEventListener('input', updateCalc);
      updateCalc();
    }
  }

  // ─── Boot ───
  document.addEventListener('DOMContentLoaded', () => {
    initColorSchemeControls();
    initDialogs();
    initConfirmForms();
    initNavigationDrawer();
    initPopoverFallback();
    initFlashCloseButtons();
    initCopyControls();
    initSortableTables();
    initScrollabilityFallbacks();
    initTableFilters();
    initCsvExports();
    initAccessibleValidation();
    initPasswordToggles();
    initTabs();
    initPageEnhancements();
  });
})();
