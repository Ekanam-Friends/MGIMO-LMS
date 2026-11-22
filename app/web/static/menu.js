// Раскрытые группы меню остаются раскрытыми при переходе между разделами, пока человек
// сам их не свернёт. Состояние — только удобство в этом браузере (localStorage).
// Ключ группы — data-group из nav.py ("study", "office"…), а не название: название
// переводится, и при смене языка состояние терялось бы.
// Подключается синхронно сразу после <nav>: группы раскрываются до первой отрисовки.
(function () {
  var KEY = "lms-menu-open";
  var saved = {};
  try {
    var parsed = JSON.parse(localStorage.getItem(KEY) || "{}");
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) saved = parsed;
  } catch (e) { /* приватный режим или битое значение — начинаем с пустого */ }

  function store() {
    try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) { /* без хранилища — просто не запоминаем */ }
  }

  document.querySelectorAll(".menu__group[data-group]").forEach(function (group) {
    var name = group.dataset.group;
    // Группу текущего раздела раскрывает сервер — она считается раскрытой: уйдя в другой
    // раздел, человек увидит её такой же. Свернуть её можно кликом, это запомнится.
    if (group.open) saved[name] = true;
    else if (saved[name]) group.open = true;
    // toggle — после переключения и при любом способе (мышь, клавиатура, скрипт).
    group.addEventListener("toggle", function () {
      saved[name] = group.open;
      store();
    });
  });
  store();
})();
