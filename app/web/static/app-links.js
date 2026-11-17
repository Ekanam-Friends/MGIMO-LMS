// Ссылки на официальное приложение МГИМО: магазин под устройство студента.
// iPhone, iPad и Mac — App Store; Android и остальные — Google Play.
(function () {
  var ua = navigator.userAgent || "";
  var apple = /iPhone|iPad|iPod|Macintosh|Mac OS X/.test(ua) && !/Android/.test(ua);
  var store = apple ? "ios" : "android";
  // одна ссылка («Популярные сервисы» на главной): подменить адрес
  document.querySelectorAll(".js-app-link").forEach(function (a) {
    if (a.dataset[store]) a.href = a.dataset[store];
  });
  // две кнопки (страница «Сервисы»): магазин устройства — красный и первым
  document.querySelectorAll(".js-app-choice").forEach(function (box) {
    box.querySelectorAll("[data-store]").forEach(function (btn) {
      var mine = btn.dataset.store === store;
      btn.classList.toggle("btn--primary", mine);
      btn.classList.toggle("btn--ghost", !mine);
      if (mine) box.insertBefore(btn, box.firstChild);
    });
  });
})();
