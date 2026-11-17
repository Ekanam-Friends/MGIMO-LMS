// HTMX заменяет календарь без перезагрузки шапки: берём неделю перед отправкой формы.
(function () {
  var form = document.querySelector(".header-language__menu");
  var initialCalendar = document.getElementById("cal-main");
  if (!form || !initialCalendar) return;
  var initialWeek = initialCalendar.dataset.week;
  form.addEventListener("submit", function () {
    var calendar = document.getElementById("cal-main");
    if (!calendar || calendar.dataset.week === initialWeek) return;
    var field = form.querySelector('[name="return_to"]');
    var url = new URL(field.value, window.location.origin);
    url.searchParams.set(calendar.dataset.dateParam, calendar.dataset.week);
    field.value = url.pathname + url.search;
  });
})();
