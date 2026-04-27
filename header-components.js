class SiteHeader extends HTMLElement {
    connectedCallback() {
      this.innerHTML = `
        <header class="header">
          <div class="container header-content">
  
            <div class="logo">
              <img src="logo.png" alt="Логотип">
            </div>
  
            <div class="title">
              <h1>МКОУ Буерак-Поповская СКШ</h1>
              <p>
                Муниципальное казенное общеобразовательное учреждение<br>
                Буерак-Поповская средняя казачья школа
              </p>
            </div>
  
            <div class="header-icons">
              <i class="fa-solid fa-eye" id="accessibilityBtn"></i>
  
              <div class="user-block" id="userBlock">
                <div class="user-name" id="userName"></div>
                <div class="user-role" id="userRole"></div>
              </div>
  
              <i class="fa-solid fa-user" id="loginBtn"></i>
              <i class="fa-solid fa-right-from-bracket" id="logoutBtn" style="display:none;"></i>
              <i class="fa-solid fa-magnifying-glass"></i>
  
              <div class="accessibility-panel" id="accessibilityPanel">
                <button class="close-panel" id="closeAccessibilityPanel">&times;</button>
                <h4><i class="fa-solid fa-universal-access"></i> Специальные возможности</h4>
  
                <div class="option-group">
                  <label>Цветовая тема</label>
                  <div class="option-buttons" data-group="theme">
                    <button class="option-btn active" data-value="original">Оригинальная</button>
                    <button class="option-btn" data-value="dark">Тёмная</button>
                    <button class="option-btn" data-value="blue">Синяя</button>
                  </div>
                </div>
  
                <div class="option-group">
                  <label>Размер шрифта</label>
                  <div class="option-buttons" data-group="fontSize">
                    <button class="option-btn active" data-value="1">1x</button>
                    <button class="option-btn" data-value="2">2x</button>
                    <button class="option-btn" data-value="3">3x</button>
                    <button class="option-btn" data-value="4">4x</button>
                  </div>
                </div>
  
                <div class="option-group">
                  <label>Эффекты</label>
                  <div class="checkbox-group">
                    <input type="checkbox" id="noEffectsCheckbox">
                    <label>Отключить анимацию</label>
                  </div>
                </div>
  
                <div class="option-group">
                  <label>Изображения</label>
                  <div class="option-buttons" data-group="imageMode">
                    <button class="option-btn active" data-value="original">Оригинал</button>
                    <button class="option-btn" data-value="grayscale">Ч/Б</button>
                    <button class="option-btn" data-value="hide">Скрыть</button>
                  </div>
                </div>
              </div>
            </div>
  
          </div>
        </header>
  
        <nav class="nav">
          <div class="container">
            <ul class="menu">
              <li><a href="main.html">Главная</a></li>
              <li><a href="#">Сведения</a></li>
              <li><a href="#">Информация</a></li>
              <li>
                <a href="#">Сервисы</a>
                <ul class="dropdown">
                  <li><a href="news.html">Новости</a></li>
                  <li><a href="gallery.html">Галерея</a></li>
                  <li><a href="warnings.html">Объявления</a></li>
                  <li><a href="rasp.html">Расписание</a></li>
                </ul>
              </li>
              <li><a href="contacts.html">Контакты</a></li>
            </ul>
          </div>
        </nav>
      `;
  
      this.initHeader();
    }
  
    initHeader() {
      const btn = this.querySelector('#accessibilityBtn');
      const panel = this.querySelector('#accessibilityPanel');
  
      btn.onclick = () => {
        panel.classList.toggle('active');
      };
    }
  }
  
  customElements.define('site-header', SiteHeader);