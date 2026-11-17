// Проверяется поведение формы при замене календаря, а не текст реализации.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const script = readFileSync(require("node:path").join(__dirname, "../app/web/static/language-return.js"), "utf8");

function setup(path, param = "week") {
  let calendar = { dataset: { week: "2026-11-16", dateParam: param } };
  const field = { value: path };
  let submit;
  const form = { querySelector: () => field, addEventListener: (_, fn) => { submit = fn; } };
  vm.runInNewContext(script, {
    URL, window: { location: { origin: "http://localhost" } },
    document: { querySelector: () => form, getElementById: () => calendar },
  });
  return { field, submit: () => submit(), replace: week => { calendar = { dataset: { week, dateParam: param } }; } };
}

test("расписание возвращается на перелистанную неделю с сохранением прочих параметров", () => {
  const page = setup("/schedule?week=2026-11-16&view=all");
  page.replace("2026-11-23");
  page.submit();
  assert.equal(page.field.value, "/schedule?week=2026-11-23&view=all");
});

test("главная использует d и учитывает несколько перелистываний", () => {
  const page = setup("/?d=2026-11-17", "d");
  page.replace("2026-11-23");
  page.replace("2026-11-30");
  page.submit();
  assert.equal(page.field.value, "/?d=2026-11-30");
});

test("дата внутри исходной недели не меняется после перерисовки календаря", () => {
  const page = setup("/?d=2026-11-17", "d");
  page.replace("2026-11-16");
  page.submit();
  assert.equal(page.field.value, "/?d=2026-11-17");
});

test("переход на Сегодня читает последнюю отображаемую неделю", () => {
  const page = setup("/schedule?week=2026-11-16");
  page.replace("2026-11-09");
  page.submit();
  assert.equal(page.field.value, "/schedule?week=2026-11-09");
});
