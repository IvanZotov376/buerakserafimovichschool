// header.js
(function () {
    // Вспомогательные функции для аватарки (как в main.html)
    function getInitials(name) {
        if (!name) return '?';
        return name.split(' ').map(n => n[0]).join('').toUpperCase().substring(0, 2);
    }

    function getDefaultAvatarColor(name) {
        const colors = ['#2ea3d0', '#3a5fa0', '#4caf50', '#ff9800', '#e91e63', '#9c27b0'];
        let hash = 0;
        for (let i = 0; i < name.length; i++) {
            hash = name.charCodeAt(i) + ((hash << 5) - hash);
        }
        return colors[Math.abs(hash) % colors.length];
    }

    // Загрузка HTML хедера
    function loadHeader() {
        return fetch('header.html')
            .then(res => {
                if (!res.ok) throw new Error('Ошибка загрузки header.html');
                return res.text();
            })
            .then(html => {
                const placeholder = document.getElementById('header-placeholder');
                if (placeholder) placeholder.innerHTML = html;
            });
    }

    // Инициализация логики хедера
    function initHeader() {
        const accessibilityBtn = document.getElementById('accessibilityBtn');
        const panel = document.getElementById('accessibilityPanel');
        const closeBtn = document.getElementById('closeAccessibilityPanel');
        
        if (accessibilityBtn && panel) {
            accessibilityBtn.onclick = () => panel.classList.toggle('active');
            closeBtn.onclick = () => panel.classList.remove('active');
        }

        // Логика пользователя и аватарки
        const loginBtn = document.getElementById('loginBtn');
        const logoutBtn = document.getElementById('logoutBtn');
        const userBlock = document.getElementById('userBlock');
        const userName = document.getElementById('userName');
        const userRole = document.getElementById('userRole');
        const userAvatar = document.getElementById('userAvatar');

        const isAuth = localStorage.getItem('auth') === 'true';
        const savedLogin = localStorage.getItem('login');
        const savedFullName = localStorage.getItem('full_name');
        const savedRole = localStorage.getItem('role');
        const savedAvatarColor = localStorage.getItem('avatar_color');

        if (isAuth && savedLogin) {
            if (loginBtn) loginBtn.style.display = 'none';
            if (logoutBtn) logoutBtn.style.display = 'inline-block';
            if (userBlock) {
                userBlock.style.display = 'flex';
                
                // Установка имени и роли
                const displayName = savedFullName || savedLogin;
                if (userName) userName.textContent = displayName;
                if (userRole) userRole.textContent = savedRole || 'Пользователь';

                // Установка аватарки (логика из main.html)
                if (userAvatar) {
                    userAvatar.style.background = savedAvatarColor || getDefaultAvatarColor(displayName);
                    userAvatar.textContent = getInitials(displayName);
                }

                userBlock.onclick = () => window.location.href = 'lk.html';
            }
        } else {
            if (loginBtn) {
                loginBtn.style.display = 'inline-block';
                loginBtn.onclick = () => window.location.href = 'login.html';
            }
            if (userBlock) userBlock.style.display = 'none';
        }

        if (logoutBtn) {
            logoutBtn.onclick = () => {
                if (confirm('Вы уверены, что хотите выйти?')) {
                    localStorage.clear();
                    location.reload();
                }
            };
        }
        
        // Применение настроек доступности (если функция определена)
        if (typeof applySettings === 'function') applySettings();
    }

    // Загрузка футера
    function loadFooter() {
        const footer = document.getElementById('footer-placeholder');
        if (!footer) return;
        fetch('footer.html')
            .then(res => res.text())
            .then(html => { footer.innerHTML = html; })
            .catch(() => {});
    }

    document.addEventListener('DOMContentLoaded', () => {
        loadHeader().then(() => {
            initHeader();
            loadFooter();
        });
    });
})();