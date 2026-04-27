/**
 * footer.js — загрузка общего подвала на всех страницах сайта
 *
 * Использование:
 *   1. Добавьте в HTML: <div id="footer-placeholder"></div>
 *   2. Подключите скрипт в конце <body>: <script src="footer.js"></script>
 *
 * Скрипт автоматически найдёт #footer-placeholder и вставит footer.html.
 * Если placeholder отсутствует — скрипт ничего не делает.
 */

(function () {
    'use strict';

    /**
     * Загружает footer.html и вставляет его в #footer-placeholder.
     * После вставки применяет текущие настройки доступности (тема, эффекты),
     * чтобы футер сразу отображался в нужном стиле.
     */
    function loadFooter() {
        const placeholder = document.getElementById('footer-placeholder');
        if (!placeholder) return; // placeholder не найден — ничего не делаем

        fetch('footer.html')
            .then(function (response) {
                if (!response.ok) {
                    throw new Error('Ошибка загрузки footer.html: ' + response.status);
                }
                return response.text();
            })
            .then(function (html) {
                placeholder.innerHTML = html;
                applyFooterTheme(); // применяем сохранённые настройки
            })
            .catch(function (err) {
                console.error('[footer.js]', err);
            });
    }

    /**
     * Читает настройки доступности из localStorage и применяет нужные классы к <body>.
     * Вызывается после вставки футера, чтобы тема отразилась и на нём.
     * (Дублирует логику header.js — оба модуля независимы и могут работать отдельно.)
     */
    function applyFooterTheme() {
        var theme      = localStorage.getItem('a11y_theme')     || 'original';
        var noEffects  = localStorage.getItem('a11y_noEffects') === 'true';

        // Тема
        document.body.classList.remove('dark-theme', 'blue-theme');
        if (theme === 'dark') {
            document.body.classList.add('dark-theme');
        } else if (theme === 'blue') {
            document.body.classList.add('blue-theme');
        }

        // Анимации
        if (noEffects) {
            document.body.classList.add('no-effects');
        } else {
            document.body.classList.remove('no-effects');
        }
    }

    // Запуск после загрузки DOM
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', loadFooter);
    } else {
        loadFooter();
    }
})();
