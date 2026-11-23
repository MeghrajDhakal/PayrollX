// ════════════════════════════════════════════
// PayrollX — Client-Side JavaScript
// ════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
    initFlashMessages();
    initModals();
    initSidebarActive();
    initAnimations();
});

// ── Auto-dismiss flash messages ──
function initFlashMessages() {
    const flashes = document.querySelectorAll('.flash-msg');
    flashes.forEach(flash => {
        setTimeout(() => {
            flash.style.opacity = '0';
            flash.style.transform = 'translateY(-8px)';
            setTimeout(() => flash.remove(), 300);
        }, 4000);
    });
}

// ── Modal System ──
function initModals() {
    // Open modal
    document.querySelectorAll('[data-modal]').forEach(trigger => {
        trigger.addEventListener('click', () => {
            const modalId = trigger.getAttribute('data-modal');
            const modal = document.getElementById(modalId);
            if (modal) modal.classList.add('show');
        });
    });

    // Close modal
    document.querySelectorAll('.modal-close, .modal-cancel').forEach(btn => {
        btn.addEventListener('click', () => {
            btn.closest('.modal-overlay').classList.remove('show');
        });
    });

    // Close on overlay click
    document.querySelectorAll('.modal-overlay').forEach(overlay => {
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) overlay.classList.remove('show');
        });
    });
}

// ── Highlight active sidebar item ──
function initSidebarActive() {
    const currentPath = window.location.pathname;
    document.querySelectorAll('.nav-item').forEach(item => {
        const href = item.getAttribute('href');
        if (href === currentPath) {
            item.classList.add('active');
        }
    });
}

// ── Animate elements on scroll ──
function initAnimations() {
    const observer = new IntersectionObserver(
        (entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.classList.add('animate-in');
                    observer.unobserve(entry.target);
                }
            });
        },
        { threshold: 0.1 }
    );

    document.querySelectorAll('.stat-card, .feature-card, .report-card, .data-card').forEach(el => {
        observer.observe(el);
    });
}

// ── Format currency ──
function formatCurrency(amount) {
    return '₹' + new Intl.NumberFormat('en-IN').format(Math.round(amount));
}

// ── Confirm actions ──
function confirmAction(message) {
    return confirm(message);
}
