(function () {
    'use strict';

    const STORAGE_PREFIX = 'ytquid_form_state_v1';
    const EXPIRY_MS = 7 * 24 * 60 * 60 * 1000;
    const IGNORED_TYPES = new Set(['button', 'hidden', 'image', 'password', 'reset', 'submit', 'file']);
    const FORM_SELECTOR = 'form:not([data-state-persistence="off"])';
    const pendingSaves = new WeakMap();
    const restoredFields = new WeakSet();
    let notice;
    let clearButton;

    function identityFor(form, index) {
        const formName = form.dataset.stateKey || form.id || form.getAttribute('name') || form.getAttribute('action') || `form-${index}`;
        return `${STORAGE_PREFIX}:${document.body.dataset.userId || 'anonymous'}:${location.pathname}:${formName}:${index}`;
    }

    function isExcludedForm(form) {
        if (form.matches('[data-delete-confirm], .inline')) return true;
        const actionPath = new URL(form.getAttribute('action') || location.href, location.href).pathname;
        return /\/(delete|cancel|retry|dismiss|disconnect|set-default|toggle)(\/|$)/i.test(actionPath);
    }

    function getFields(form) {
        const occurrences = new Map();
        return Array.from(form.elements).flatMap((field, index) => {
            const type = (field.type || '').toLowerCase();
            const descriptor = `${field.name || ''} ${field.id || ''} ${field.autocomplete || ''}`;
            const sensitive = /password|token|secret|api[_-]?key|csrf|authorization|one-time-code/i.test(descriptor);
            if (field.disabled || IGNORED_TYPES.has(type) || sensitive || field.matches('[data-state-ignore]')) return [];

            const name = field.name || field.id || `field-${index}`;
            const occurrence = occurrences.get(name) || 0;
            occurrences.set(name, occurrence + 1);
            return [{ field, key: `${name}:${occurrence}`, type }];
        });
    }

    function storageKey(form) {
        const forms = Array.from(document.querySelectorAll(FORM_SELECTOR));
        return identityFor(form, forms.indexOf(form));
    }

    function showNotice(message, showClear) {
        if (!notice) {
            notice = document.createElement('div');
            notice.className = 'fixed bottom-4 right-4 z-[100] flex max-w-[calc(100vw-2rem)] items-center gap-3 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-xs text-zinc-200 shadow-xl';
            notice.setAttribute('role', 'status');
            notice.setAttribute('aria-live', 'polite');
            const text = document.createElement('span');
            text.dataset.noticeText = 'true';
            clearButton = document.createElement('button');
            clearButton.type = 'button';
            clearButton.className = 'shrink-0 font-semibold text-emerald-400 hover:text-emerald-300';
            clearButton.textContent = 'Clear page draft';
            clearButton.addEventListener('click', async function () {
                const confirmed = window.AppDialog?.confirm
                    ? await window.AppDialog.confirm({
                        title: 'Clear saved work?',
                        message: 'Remove the saved form values for this page?',
                        confirmText: 'Clear draft',
                        cancelText: 'Keep',
                        type: 'danger',
                    })
                    : window.confirm('Clear the saved form values for this page?');
                if (!confirmed) return;
                document.querySelectorAll(FORM_SELECTOR).forEach(form => localStorage.removeItem(storageKey(form)));
                window.location.reload();
            });
            notice.append(text, clearButton);
            document.body.appendChild(notice);
        }
        notice.querySelector('[data-notice-text]').textContent = message;
        clearButton.hidden = !showClear;
        notice.classList.remove('hidden');
    }

    function snapshotForm(form) {
        const values = {};
        getFields(form).forEach(({ field, key, type }) => {
            if (type === 'checkbox' || type === 'radio') {
                values[key] = { type, checked: field.checked };
            } else if (field instanceof HTMLSelectElement && field.multiple) {
                values[key] = { type: 'select-multiple', value: Array.from(field.selectedOptions, option => option.value) };
            } else if (typeof field.value === 'string' && field.value.length <= 200000) {
                values[key] = { type, value: field.value };
            }
        });
        return { savedAt: Date.now(), values };
    }

    function saveForm(form, quiet) {
        if (!form || isExcludedForm(form)) return;
        const fields = getFields(form);
        if (!fields.length) return;
        try {
            const snapshot = snapshotForm(form);
            const serialized = JSON.stringify(snapshot);
            if (serialized.length > 1500000) return;
            localStorage.setItem(storageKey(form), serialized);
            if (!quiet) showNotice('Form work is saved on this device.', true);
        } catch (error) {
            console.warn('Could not save this page state:', error);
        }
    }

    function restoreForm(form) {
        if (!form || isExcludedForm(form)) return;
        const fields = getFields(form);
        if (!fields.length) return;

        try {
            const key = storageKey(form);
            const raw = localStorage.getItem(key);
            if (!raw) return;
            const snapshot = JSON.parse(raw);
            if (!snapshot || Date.now() - snapshot.savedAt > EXPIRY_MS) {
                localStorage.removeItem(key);
                return;
            }

            let restored = false;
            fields.forEach(({ field, key: fieldKey, type }) => {
                const saved = snapshot.values?.[fieldKey];
                if (!saved || restoredFields.has(field) || saved.type !== type && saved.type !== 'select-multiple') return;

                if (type === 'checkbox' || type === 'radio') {
                    field.checked = Boolean(saved.checked);
                } else if (saved.type === 'select-multiple' && field instanceof HTMLSelectElement && field.multiple) {
                    const selected = new Set(saved.value || []);
                    Array.from(field.options).forEach(option => { option.selected = selected.has(option.value); });
                } else if (typeof saved.value === 'string' && field.value !== saved.value) {
                    field.value = saved.value;
                }

                restoredFields.add(field);
                field.dispatchEvent(new Event('input', { bubbles: true }));
                field.dispatchEvent(new Event('change', { bubbles: true }));
                restored = true;
            });

            if (restored && form.dataset.stateRestored !== 'true') {
                form.dataset.stateRestored = 'true';
                showNotice('Unsaved form work restored for this page.', true);
            }
        } catch (error) {
            console.warn('Could not restore this page state:', error);
        }
    }

    function restoreAllForms() {
        document.querySelectorAll(FORM_SELECTOR).forEach(restoreForm);
    }

    document.addEventListener('input', function (event) {
        const form = event.target.closest?.('form');
        if (!form || !form.matches(FORM_SELECTOR)) return;
        clearTimeout(pendingSaves.get(form));
        pendingSaves.set(form, setTimeout(() => saveForm(form, false), 250));
    }, true);

    document.addEventListener('change', function (event) {
        const form = event.target.closest?.('form');
        if (form && form.matches(FORM_SELECTOR)) saveForm(form, false);
    }, true);

    document.addEventListener('submit', function (event) {
        saveForm(event.target, true);
    }, true);

    window.addEventListener('pagehide', function () {
        document.querySelectorAll(FORM_SELECTOR).forEach(form => saveForm(form, true));
    });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', restoreAllForms, { once: true });
    } else {
        restoreAllForms();
    }

    const observer = new MutationObserver(mutations => {
        mutations.forEach(mutation => mutation.addedNodes.forEach(node => {
            if (!(node instanceof Element)) return;
            if (node.matches(FORM_SELECTOR)) restoreForm(node);
            node.querySelectorAll?.(FORM_SELECTOR).forEach(restoreForm);
            const form = node.closest('form');
            if (form) restoreForm(form);
        }));
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
})();
