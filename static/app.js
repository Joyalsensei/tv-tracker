/* ── Shared app JS: toasts, celebration, CSRF-safe actions, optimistic UI ── */
(function () {
    'use strict';

    // Read the CSRF token from a meta tag injected by base.html (single source of truth)
    function getCsrf() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    // ── Toast Notifications ──
    function showToast(message, type) {
        const container = document.getElementById('toastContainer');
        if (!container) return;
        const icons = { error: '❌', success: '✅', info: 'ℹ️' };
        const toast = document.createElement('div');
        toast.className = 'toast ' + (type || 'info');
        const icon = document.createElement('span');
        icon.className = 'toast-icon';
        icon.setAttribute('aria-hidden', 'true');
        icon.textContent = icons[type] || 'ℹ️';
        const text = document.createElement('span');
        // textContent, NOT innerHTML: server-provided strings (show names etc.)
        // can never become markup — XSS-safe by construction.
        text.textContent = message || '';
        toast.appendChild(icon);
        toast.appendChild(text);
        container.appendChild(toast);

        requestAnimationFrame(function () { toast.classList.add('show'); });

        setTimeout(function () {
            toast.classList.remove('show');
            setTimeout(function () { toast.remove(); }, 400);
        }, 4000);
    }

    // Convert server-side flash messages to toasts
    const flashData = document.getElementById('flashData');
    if (flashData) {
        try {
            const messages = JSON.parse(flashData.dataset.messages);
            messages.forEach(function (msg) { showToast(msg[1], msg[0]); });
        } catch (e) { /* ignore malformed flash payload */ }
    }

    // ── Confirm dialog helper ──
    function confirmAction(msg) {
        return window.confirm(msg);
    }

    // ── Celebration (confetti + finished banner) — shared by shows AND movies ──
    function celebrate(message, sub) {
        const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (!reduced) {
            const container = document.getElementById('confettiContainer');
            if (container) {
                const colors = ['#e50914','#f5c518','#2ecc71','#3498db','#e74c3c','#9b59b6','#f39c12','#1abc9c'];
                for (let i = 0; i < 80; i++) {
                    const piece = document.createElement('div');
                    piece.className = 'confetti-piece';
                    piece.style.cssText = 'left:' + (Math.random() * 100) + '%;width:' + (Math.random() * 8 + 6) + 'px;height:' +
                        (Math.random() * 8 + 6) + 'px;background:' + colors[Math.floor(Math.random() * colors.length)] +
                        ';border-radius:' + (Math.random() > .5 ? '50%' : '2px') + ';animation-delay:' +
                        (Math.random() * 2) + 's;--fall-duration:' + (Math.random() * 2 + 2) + 's';
                    container.appendChild(piece);
                }
                setTimeout(function () { container.innerHTML = ''; }, 6000);
            }
        }
        const banner = document.getElementById('finishedBanner');
        if (banner) {
            if (message) banner.firstChild.textContent = message + ' ';
            if (sub) banner.querySelector('.sub').textContent = sub;
            banner.classList.add('show');
            setTimeout(function () { banner.classList.remove('show'); }, 5000);
        }
        showToast(message || 'Finished!', 'success');
    }

    // ── CSRF-safe POST helper (used by all fetch actions) ──
    function csrfPost(url, extra) {
        const formData = new FormData();
        formData.append('_csrf_token', getCsrf());
        if (extra) {
            Object.keys(extra).forEach(function (k) { formData.append(k, extra[k]); });
        }
        return fetch(url, { method: 'POST', body: formData });
    }

    // ── Add-to-list (replaces old GET /add/<id> links — CSRF-safe POST) ──
    // Any element with [data-add-url] triggers this handler.
    document.addEventListener('click', function (e) {
        const trigger = e.target.closest('[data-add-url]');
        if (!trigger) return;
        e.preventDefault();
        if (trigger.disabled) return;
        const original = trigger.textContent;
        trigger.disabled = true;
        trigger.style.opacity = '.6';
        csrfPost(trigger.dataset.addUrl)
            .then(function (res) { return res.ok ? res.json() : Promise.reject(new Error('Request failed')); })
            .then(function (data) {
                if (data.status === 'ok') {
                    trigger.textContent = trigger.dataset.addedText || '✓ Added';
                    showToast(data.message || 'Added!', 'success');
                } else if (data.status === 'info') {
                    // Already in the user's list — inform, don't error
                    trigger.disabled = false;
                    trigger.style.opacity = '';
                    trigger.textContent = original;
                    showToast(data.message || 'Already in your list.', 'info');
                } else {
                    trigger.disabled = false;
                    trigger.style.opacity = '';
                    trigger.textContent = original;
                    showToast(data.message || 'Could not add.', 'error');
                }
            })
            .catch(function () {
                trigger.disabled = false;
                trigger.style.opacity = '';
                trigger.textContent = original;
                showToast('Could not add. Please try again.', 'error');
            });
    });

    // ── Disable button on form submit to prevent double-clicks ──
    document.addEventListener('submit', function (e) {
        const form = e.target;
        if (!form.checkValidity || form.checkValidity()) {
            const btn = form.querySelector('button[type="submit"]');
            if (btn && !btn.disabled) {
                btn.disabled = true;
                btn.classList.add('loading');
            }
        }
    });

    window.TVTracker = {
        showToast: showToast,
        confirmAction: confirmAction,
        celebrate: celebrate,
        csrfPost: csrfPost,
        getCsrf: getCsrf,
    };
})();
