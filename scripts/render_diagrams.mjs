// Редактируемые SVG-схемы документации. Запуск: node scripts/render_diagrams.mjs.
// Зависимости и сетевые запросы не нужны.
import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const output = fileURLToPath(new URL('../docs/diagrams/', import.meta.url));
mkdirSync(output, { recursive: true });
const C = { ink: '#122844', muted: '#536780', blue: '#2463ad', line: '#7c96b4', pale: '#f2f6fc',
  border: '#d8e3f0', green: '#176d64', greenBg: '#edf8f4', amber: '#925c13', amberBg: '#fff7e8' };
const esc = s => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
let parts;
function text(x, y, value, size = 18, fill = C.ink, weight = 400) {
  parts.push(`<text x="${x}" y="${y}" font-size="${size}" fill="${fill}" font-weight="${weight}">${esc(value)}</text>`);
}
function rect(x, y, w, h, fill = 'white', stroke = C.border, radius = 18, dash = '') {
  parts.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${radius}" fill="${fill}" stroke="${stroke}" stroke-width="1.5" ${dash ? `stroke-dasharray="${dash}"` : ''}/>`);
}
function start(id, height, eyebrow, title, subtitle, description) {
  parts = [`<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="${height}" viewBox="0 0 1200 ${height}" role="img" aria-labelledby="title desc">
<title id="title">${esc(title)}</title><desc id="desc">${esc(description)}</desc>
<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="${C.line}"/></marker></defs>
<g font-family="Segoe UI, Arial, sans-serif"><rect width="1200" height="${height}" rx="24" fill="#fff"/>
<path d="M50 40h44" stroke="${C.blue}" stroke-width="5" stroke-linecap="round"/>`];
  text(110, 47, eyebrow, 16, C.blue, 700);
  text(50, 104, title, 35, C.ink, 700);
  text(50, 143, subtitle, 20, C.muted);
  return () => {
    parts.push('</g></svg>');
    writeFileSync(`${output}${id}.svg`, parts.join('\n') + '\n');
  };
}
function card(x, y, w, h, title, lines = [], kind = 'normal', tag = '') {
  parts.push('<g data-card="true">');
  const fill = kind === 'data' ? C.greenBg : kind === 'plan' ? C.amberBg : 'white';
  const color = kind === 'data' ? C.green : kind === 'plan' ? C.amber : C.ink;
  rect(x, y, w, h, fill, kind === 'plan' ? '#d4a45b' : C.border, 17, kind === 'plan' ? '7 5' : '');
  if (tag) text(x + 22, y + 27, tag, 13, kind === 'plan' ? C.amber : C.blue, 700);
  const headingY = y + (tag ? 57 : 37);
  text(x + 22, headingY, title, 23, color, 650);
  lines.forEach((s, i) => text(x + 22, headingY + 29 + i * 25, s, 18, C.muted));
  parts.push('</g>');
}
function arrow(d, { both = false, dashed = false } = {}) {
  parts.push(`<path d="${d}" fill="none" stroke="${C.line}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round" marker-end="url(#arrow)" ${both ? 'marker-start="url(#arrow)"' : ''} ${dashed ? 'stroke-dasharray="7 6"' : ''}/>`);
}
function label(x, y, s) {
  const w = [...s].length * 9 + 20;
  rect(x - w / 2, y - 19, w, 27, '#fff', '#fff', 6);
  parts.push(`<text x="${x}" y="${y}" text-anchor="middle" font-size="16" fill="${C.muted}">${esc(s)}</text>`);
}
function panel(x, y, w, h, title) {
  rect(x, y, w, h, C.pale, C.border, 22);
  text(x + 25, y + 36, title, 16, C.blue, 700);
}

// 1. Архитектура: браузер и Telegram имеют разные серверные точки входа.
{
  const finish = start('architecture', 1120, '01 / АРХИТЕКТУРА', 'Один кабинет. Общая база.',
    'Каналы доступа, серверные сервисы и внешние интеграции',
    'Браузер через Caddy обращается к FastAPI. Telegram обслуживается отдельным aiogram-ботом. Оба работают с PostgreSQL. Импортёр получает расписание РУЗ. Pyrus согласует справки. Реализованный ассистент использует GigaChat и читает разрешённые данные кабинета; оценки доступны после согласия. Заявки через чат не создаются.');
  card(50, 200, 300, 115, 'Веб-кабинет', ['Студент · преподаватель', 'Деканат · Jinja2 / HTMX']);
  card(440, 200, 300, 115, 'Pyrus', ['Согласование справок', 'Задачи и статусы']);
  card(830, 200, 320, 115, 'Telegram', ['Команды, расписание', 'Статусы и уведомления']);
  panel(30, 400, 1140, 425, 'СЕРВЕР ПРИЛОЖЕНИЯ / VPS · DOCKER COMPOSE');
  card(60, 470, 240, 110, 'Caddy', ['HTTPS / reverse proxy']);
  card(410, 470, 300, 110, 'FastAPI', ['Сессии · роли · доступы']);
  card(840, 470, 300, 110, 'aiogram', ['Отдельный процесс бота']);
  card(410, 670, 300, 110, 'PostgreSQL', ['Учёба · заявки · события'], 'data');
  card(840, 670, 300, 110, 'Импорт расписания', ['ФИО → вымышленные']);
  card(60, 670, 240, 110, 'Защита данных', ['Пароли — argon2', 'Секреты — в .env']);
  card(50, 900, 300, 115, 'ИИ-ассистент', ['Публичный RAG · кабинет', 'Оценки — после согласия']);
  card(440, 900, 300, 115, 'GigaChat API', ['Выбор и аудит выдержек', 'Перевод RU / EN / FR / ZH']);
  card(840, 900, 300, 115, 'Открытый РУЗ', ['Источник расписания']);
  arrow('M200 315 V360 H45 V525 H60', { both: true }); label(140, 350, 'HTTPS');
  arrow('M590 315 V365 H790 V525 H710', { both: true }); label(690, 352, 'API / вебхук');
  arrow('M990 315 V470', { both: true }); label(990, 370, 'Bot API');
  arrow('M300 525 H410', { both: true });
  arrow('M560 580 V670', { both: true });
  arrow('M990 580 V625 H600 V670', { both: true });
  arrow('M840 725 H710');
  arrow('M990 900 V780'); label(990, 865, 'JSON API');
  arrow('M410 550 H360 V860 H200 V900');
  arrow('M350 955 H440');
  text(50, 1070, 'Ассистент читает разрешённые данные. Создание заявок через чат не реализовано.', 18, C.muted);
  finish();
}

// 2. Реализованный поиск по снимку: аудит модели не заменяет сверку OCR и редакций.
{
  const finish = start('rag-pipeline', 1060, '02 / РЕАЛИЗОВАННАЯ RAG-ЦЕПОЧКА', 'От документа — к проверяемому ответу',
    'Поиск по снимку корпуса, выбор выдержек и проверка через GigaChat',
    'Реализованная цепочка: публичные PDF, извлечение текста и OCR, снимок фрагментов, поиск BM25, выбор выдержек GigaChat и отдельный аудит применимости. Эмбеддинги доступны как опция, по умолчанию выключены. Непроверенный снимок даёт только частичный справочный ответ; OCR и действие редакций требуют отдельной сверки.');
  panel(30, 185, 1140, 190, '01 / ПОДГОТОВКА КОРПУСА');
  card(50, 240, 330, 110, 'Официальные PDF', ['Положения МГИМО', 'Источник и реквизиты']);
  card(435, 240, 330, 110, 'Извлечение текста', ['OCR для сканов', 'Разбиение на фрагменты']);
  card(820, 240, 330, 110, 'Снимок корпуса', ['Фрагменты · BM25', 'Эмбеддинги — опция'], 'data');
  panel(30, 460, 1140, 190, '02 / ОБРАБОТКА ВОПРОСА');
  card(50, 515, 330, 110, 'Вопрос студента', ['Запрос в кабинете', 'Контекст ситуации']);
  card(435, 515, 330, 110, 'Поиск фрагментов', ['BM25 · доступные редакции', 'Снимок и даты источника']);
  card(820, 515, 330, 110, 'Выбор выдержек', ['GigaChat API', 'Вызов только с бэкенда']);
  panel(30, 735, 1140, 190, '03 / ПРОВЕРКА И РЕЗУЛЬТАТ');
  card(50, 790, 330, 110, 'Аудит применимости', ['Второй вызов GigaChat', 'Ссылки и дословные поля']);
  card(435, 790, 330, 110, 'Ответ в кабинете', ['Выдержки и ограничения', 'Снимок: ответ частичный'], 'data');
  card(820, 790, 330, 110, 'Документ-источник', ['Ссылка · пункт / страница', 'Редакция документа']);
  for (const y of [295, 570, 845]) { arrow(`M380 ${y} H435`); arrow(`M765 ${y} H820`); }
  arrow('M985 350 V410 H600 V515'); label(790, 410, 'контекст из корпуса');
  arrow('M985 625 V685 H405 V780 H215 V790'); label(600, 685, 'ответ и использованные фрагменты');
  text(50, 980, 'Недостаточно оснований → уточнение или отказ. Историческая редакция требует подтверждения.', 18, C.muted);
  text(50, 1012, 'Точность OCR и действие норм проверяются отдельно; аудит модели не заменяет ручную сверку.', 18, C.muted);
  finish();
}

// 3. Справки: сервер проверяет вебхук и перечитывает задачу Pyrus по API.
{
  const finish = start('certificate-flow', 1080, '03 / СПРАВКИ И СОГЛАСОВАНИЕ', 'Заявка без очереди в деканат',
    'Кабинет принимает запрос, Pyrus ведёт согласование, результат возвращается студенту',
    'Студент создаёт заявку в FastAPI, которая отправляется в Pyrus. После проверки в деканате и подписи декана подписанный вебхук запускает сверку через API. Статус сохраняется в PostgreSQL; кабинет показывает PDF, бот отправляет уведомление.');
  card(50, 230, 270, 110, 'Студент', ['Вид справки и назначение']);
  card(420, 230, 300, 110, 'Личный кабинет', ['FastAPI · проверка доступа']);
  card(880, 230, 270, 110, 'Задача в Pyrus', ['Очередь деканата']);
  card(420, 420, 300, 110, 'PostgreSQL', ['Вымышленный контингент', 'Заявка и история событий'], 'data');
  card(880, 440, 270, 100, 'Проверка в деканате', ['Первый шаг маршрута']);
  card(880, 620, 270, 100, 'Подпись декана', ['Второй шаг маршрута']);
  card(420, 620, 300, 120, 'Сверка с Pyrus', ['Проверка подписи вебхука', 'Перечитывание по API']);
  card(50, 620, 270, 120, 'Результат в ЛК', ['Статус и комментарий', 'PDF по шаблону'], 'data');
  card(420, 860, 300, 110, 'Событие заявки', ['Изменение статуса в БД']);
  card(50, 860, 270, 110, 'Telegram', ['Уведомление студенту']);
  arrow('M320 285 H420'); label(370, 270, 'заказ');
  arrow('M720 285 H880'); label(800, 270, 'API');
  arrow('M570 340 V420', { both: true });
  arrow('M1015 340 V440');
  arrow('M1015 540 V620');
  arrow('M880 300 H800 V680 H720'); label(800, 398, 'вебхук');
  arrow('M720 705 H775 V320 H880', { dashed: true });
  arrow('M570 620 V530');
  arrow('M420 680 H320');
  arrow('M570 740 V860');
  arrow('M420 915 H320');
  text(50, 1020, 'Пунктир — фоновая сверка. Оба шага Pyrus в демо назначены одному сотруднику.', 18, C.muted);
  finish();
}

// 4. Развёртывание: общий Caddy и proxy для бота; у приложений собственные базы.
{
  const finish = start('deployment', 1340, '04 / РАЗВЁРТЫВАНИЕ', 'Один VPS. Изолированные приложения.',
    'Топология compose; серверный override — по отчёту 16 ноября',
    'Общий Caddy направляет запросы в MGIMO-LMS через сеть edge или в Ikanam через его сеть. Web и bot работают с собственной PostgreSQL primary. Необязательный standby на том же VPS включается отдельным compose, получает WAL и не обслуживает запросы приложения. Бот использует proxy Ikanam, не его базу. Доступ команды — SSH-туннель. Схема описывает конфигурацию, не новую проверку production.');
  card(370, 200, 460, 110, 'Браузер / интернет', ['lms-university.ekanamfriends.ru']);
  panel(30, 370, 1140, 840, 'ОБЩИЙ VPS');
  card(400, 435, 400, 110, 'Общий Caddy', ['ikanam-caddy-1 · HTTPS / 443']);
  panel(50, 630, 710, 510, 'MGIMO-LMS / СВОЯ DOCKER-СЕТЬ И БАЗА');
  card(80, 695, 280, 100, 'FastAPI / web', ['lms-web:8000']);
  card(80, 855, 280, 100, 'aiogram / bot', ['Telegram long polling']);
  card(430, 790, 280, 120, 'PostgreSQL primary', ['mgimo-lms-db-1', 'Данные только MGIMO-LMS'], 'data');
  card(430, 965, 280, 110, 'Реплика (опция)', ['Streaming · отдельный том', 'Переключение — вручную'], 'data');
  card(830, 695, 300, 150, 'Проект Ikanam', ['Свои web и PostgreSQL', 'Proxy для Telegram-бота', 'Общий HTTPS-вход']);
  card(830, 915, 300, 100, 'Доступ команды', ['SSH → 127.0.0.1:5433']);
  arrow('M600 310 V435', { both: true }); label(600, 350, 'HTTPS');
  arrow('M600 545 V600 H720 V680 H220 V695', { both: true }); label(650, 600, 'сеть edge');
  arrow('M800 490 H980 V695', { both: true }); label(980, 600, 'сеть Ikanam');
  arrow('M360 745 H570 V790', { both: true });
  arrow('M360 905 H400 V875 H430', { both: true });
  arrow('M830 965 H790 V850 H710', { both: true });
  arrow('M570 910 V965', { dashed: true }); label(570, 945, 'WAL');
  arrow('M220 955 V1180 H1150 V860 H980 V845', { dashed: true });
  label(650, 1180, 'бот → proxy Ikanam');
  text(50, 1260, 'База не публикуется в интернет. Серверные секреты и override остаются вне репозитория.', 18, C.muted);
  text(50, 1287, 'Пунктир — WAL в standby и выход бота к Telegram. Реплика включается отдельным compose.', 16, C.muted);
  text(50, 1320, 'Реплика на том же VPS не защищает от отказа VPS или диска и не заменяет внешние бэкапы.', 16, C.muted);
  finish();
}
console.log('Обновлены 4 SVG-схемы в docs/diagrams/');
