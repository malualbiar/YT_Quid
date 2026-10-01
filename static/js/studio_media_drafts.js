(function () {
    'use strict';

    const DATABASE_NAME = 'ytquid_studio_media_drafts';
    const STORE_NAME = 'drafts';

    function openDatabase() {
        return new Promise((resolve, reject) => {
            const request = indexedDB.open(DATABASE_NAME, 1);
            request.onupgradeneeded = () => request.result.createObjectStore(STORE_NAME);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    async function readDraft(key) {
        const database = await openDatabase();
        return new Promise((resolve, reject) => {
            const transaction = database.transaction(STORE_NAME, 'readonly');
            const request = transaction.objectStore(STORE_NAME).get(key);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
            transaction.oncomplete = () => database.close();
            transaction.onerror = () => reject(transaction.error);
        });
    }

    async function writeDraft(key, draft) {
        const database = await openDatabase();
        return new Promise((resolve, reject) => {
            const transaction = database.transaction(STORE_NAME, 'readwrite');
            if (draft) transaction.objectStore(STORE_NAME).put(draft, key);
            else transaction.objectStore(STORE_NAME).delete(key);
            transaction.oncomplete = () => {
                database.close();
                resolve();
            };
            transaction.onerror = () => reject(transaction.error);
            transaction.onabort = () => reject(transaction.error);
        });
    }

    async function saveForm(form) {
        const key = form.dataset.mediaDraftKey;
        if (!key) return;

        const files = Array.from(form.querySelectorAll('input[type="file"][id]'))
            .filter(input => input.files && input.files.length)
            .map(input => ({ inputId: input.id, file: input.files[0] }));

        await writeDraft(key, files.length ? { files, savedAt: Date.now() } : null);
    }

    async function restoreForm(form) {
        const key = form.dataset.mediaDraftKey;
        if (!key) return;

        const draft = await readDraft(key);
        if (!draft || !Array.isArray(draft.files)) return;

        draft.files.forEach(saved => {
            const input = document.getElementById(saved.inputId);
            if (!input || input.form !== form || input.files.length || !saved.file) return;

            try {
                const transfer = new DataTransfer();
                transfer.items.add(saved.file);
                input.files = transfer.files;
                const changeEvent = new Event('change', { bubbles: true });
                changeEvent.mediaDraftRestore = true;
                input.dispatchEvent(changeEvent);
            } catch (error) {
                console.warn('Could not restore a studio media draft:', error);
            }
        });
    }

    document.addEventListener('change', event => {
        if (event.mediaDraftRestore) return;
        const input = event.target;
        if (!(input instanceof HTMLInputElement) || input.type !== 'file') return;
        const form = input.closest('form[data-media-draft-key]');
        if (form) saveForm(form).catch(error => {
            console.warn('Could not save a studio media draft:', error);
            window.AppToast?.warning('This media file could not be saved for later in this browser. Keep this editor open while working.');
        });
    });

    function restoreAllForms() {
        document.querySelectorAll('form[data-media-draft-key]').forEach(form => {
            restoreForm(form).catch(error => console.warn('Could not restore a studio media draft:', error));
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', restoreAllForms, { once: true });
    } else {
        restoreAllForms();
    }

    window.StudioMediaDrafts = {
        clear: async key => {
            try {
                await writeDraft(key, null);
            } catch (error) {
                console.warn('Could not clear a studio media draft:', error);
            }
        },
        save: saveForm,
    };
})();