// header.js
(function () {
    function getInitials(name) {
        if (!name) return '?';
        return name
            .trim()
            .split(/\s+/)
            .map(part => part[0])
            .join('')
            .toUpperCase()
            .substring(0, 2);
    }

    function getDefaultAvatarColor(name) {
        const colors = ['#2ea3d0', '#3a5fa0', '#4caf50', '#ff9800', '#e91e63', '#9c27b0'];
        let hash = 0;
        const source = name || 'user';
        for (let i = 0; i < source.length; i++) {
            hash = source.charCodeAt(i) + ((hash << 5) - hash);
        }
        return colors[Math.abs(hash) % colors.length];
    }

    function loadHeader() {
        const placeholder = document.getElementById('header-placeholder');
        if (!placeholder) return Promise.resolve();

        return fetch('header.html')
            .then(res => {
                if (!res.ok) throw new Error('Ошибка загрузки header.html');
                return res.text();
            })
            .then(html => {
                placeholder.innerHTML = html;
            })
            .catch(error => {
                console.error(error);
            });
    }

    function showElement(element, displayValue) {
        if (element) element.style.display = displayValue;
    }

    function hideElement(element) {
        if (element) element.style.display = 'none';
    }

    function clearUserSession() {
        // Не очищаем весь localStorage, чтобы не сбрасывать настройки доступности сайта.
        const authKeys = [
            'auth',
            'password',
            'profile_photo'
        ];

        // Логин, email, роль, имя и цвет аватара оставляем: login.html сможет подставить их снова.
        authKeys.forEach(key => localStorage.removeItem(key));
    }

    function initHeader() {
        const accessibilityBtn = document.getElementById('accessibilityBtn');
        const panel = document.getElementById('accessibilityPanel');
        const closeBtn = document.getElementById('closeAccessibilityPanel');

        if (accessibilityBtn && panel) {
            accessibilityBtn.onclick = () => panel.classList.toggle('active');
        }

        if (closeBtn && panel) {
            closeBtn.onclick = () => panel.classList.remove('active');
        }

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
        const savedProfilePhoto = localStorage.getItem('profile_photo');

        if (isAuth && savedLogin) {
            hideElement(loginBtn);
            showElement(logoutBtn, 'inline-flex');
            showElement(userBlock, 'flex');

            const displayName = savedFullName || savedLogin;

            if (userName) userName.textContent = displayName;
            if (userRole) userRole.textContent = savedRole || 'Пользователь';

            if (userAvatar) {
                userAvatar.textContent = '';
                userAvatar.style.backgroundImage = '';

                if (savedProfilePhoto) {
                    userAvatar.style.background = `url("${savedProfilePhoto}") center / cover no-repeat`;
                } else {
                    userAvatar.style.background = savedAvatarColor || getDefaultAvatarColor(displayName);
                    userAvatar.textContent = getInitials(displayName);
                }
            }

            if (userBlock) {
                userBlock.onclick = () => {
                    window.location.href = 'lk.html';
                };
            }
        } else {
            // Гарантированно скрываем пустой аватар и показываем переход на login.html.
            hideElement(userBlock);
            hideElement(logoutBtn);
            let activeLoginBtn = loginBtn;
            if (!activeLoginBtn) {
                const icons = document.querySelector('.header-icons');
                if (icons) {
                    activeLoginBtn = document.createElement('a');
                    activeLoginBtn.id = 'loginBtn';
                    activeLoginBtn.className = 'login-link';
                    activeLoginBtn.href = 'login.html';
                    activeLoginBtn.title = 'Войти в профиль';
                    activeLoginBtn.setAttribute('aria-label', 'Войти в профиль');
                    activeLoginBtn.innerHTML = '<i class="fa-solid fa-user"></i>';
                    const logout = document.getElementById('logoutBtn');
                    icons.insertBefore(activeLoginBtn, logout || panel || null);
                }
            }
            showElement(activeLoginBtn, 'inline-flex');

            if (activeLoginBtn) {
                activeLoginBtn.setAttribute('href', 'login.html');
                activeLoginBtn.onclick = null;
                activeLoginBtn.addEventListener('click', (event) => {
                    event.preventDefault();
                    window.location.assign('login.html');
                }, { once: false });
            }
        }

        if (logoutBtn) {
            logoutBtn.onclick = () => {
                if (confirm('Вы уверены, что хотите выйти?')) {
                    clearUserSession();
                    window.location.href = 'index.html';
                }
            };
        }

        if (typeof applySettings === 'function') applySettings();
    }

    function loadFooter() {
        const footer = document.getElementById('footer-placeholder');
        if (!footer) return;

        fetch('footer.html')
            .then(res => res.text())
            .then(html => {
                footer.innerHTML = html;
            })
            .catch(() => {});
    }

    document.addEventListener('DOMContentLoaded', () => {
        loadHeader().then(() => {
            initHeader();
            loadFooter();
        });
    });
})();
