/* История беседы хранится только на сервере до очистки или истечения. Без HTML модели и localStorage. */
(() => {
  const t = window.lmsT || (value => value);
  const tf = (key, values) => t(key).replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ''));
  const root = document.getElementById('assistant');
  if (!root) return;
  const form = document.getElementById('assistant-form');
  const input = document.getElementById('assistant-question');
  const day = document.getElementById('assistant-date');
  const send = document.getElementById('assistant-send');
  const clear = document.getElementById('assistant-clear');
  const log = document.getElementById('assistant-messages');
  const progress = document.getElementById('assistant-progress');
  const welcome = document.getElementById('assistant-welcome');
  const ack = document.getElementById('assistant-acknowledged');
  const start = document.getElementById('assistant-start');
  const warning = document.getElementById('assistant-warning');
  const startStatus = document.getElementById('assistant-start-status');
  const student = document.getElementById('assistant-student');
  const studentSearch = document.getElementById('assistant-student-search');
  const studentStatus = document.getElementById('assistant-student-status');
  const startPayload = () => ({action:'start', acknowledged:true, student_id:student?.value ? Number(student.value) : null});
  let searchVersion = 0;
  async function loadStudents() {
    const version = ++searchVersion;
    try {
      const response = await fetch(`/s/assistant/students?q=${encodeURIComponent(studentSearch.value)}`, {credentials:'same-origin', redirect:'error'});
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (version !== searchVersion || conversationToken) return;
      const previous = student.value;
      student.replaceChildren(new Option('Только публичные НПА', ''));
      data.students.forEach(row => student.add(new Option(row.name, String(row.id))));
      if (Array.from(student.options).some(option => option.value === previous)) student.value = previous;
      studentStatus.textContent = 'До 30 результатов. Уточните имя для поиска остальных демо-студентов.';
    } catch (_) {studentStatus.textContent = t('Не удалось получить список. Повторите поиск.');}
  }
  if (student) {
    let timer;
    studentSearch.addEventListener('input', () => {clearTimeout(timer); timer = setTimeout(loadStudents, 250);});
    loadStudents();
  }
  let conversationToken = '';
  let pendingQuestion = '';
  let repeatingAfterConsent = false;
  let busy = false;
  const labels = {help:'Помощь ассистента',answer:'Ответ по источникам',partial:'Частичный ответ',clarify:'Нужно уточнение',consent_required:'Разрешение на доступ к оценкам',not_found:'Нет подтверждения',error:'Не удалось проверить ответ',unavailable:'Ассистент недоступен',busy:'Подождите немного'};
  async function action(payload) {
    const response = await fetch('/s/assistant/conversation', {method:'POST', credentials:'same-origin', redirect:'error',
      headers:{'Content-Type':'application/json','X-CSRF-Token':root.dataset.csrf}, body:JSON.stringify(payload)});
    if (!response.ok) throw new Error();
    return response.json();
  }
  function controls() {
    const disabled = busy || !conversationToken;
    if (student) {student.disabled = busy || Boolean(conversationToken); studentSearch.disabled = student.disabled;}
    send.disabled = disabled; input.disabled = disabled; day.disabled = disabled; clear.disabled = busy;
    root.querySelectorAll('.assistant__example').forEach(button => {button.disabled = disabled;});
  }
  ack.addEventListener('change', () => {start.disabled = !ack.checked || root.dataset.enabled !== 'true';});
  start.addEventListener('click', async () => {
    if (!ack.checked || root.dataset.enabled !== 'true') return;
    start.disabled = true;
    try {
      const data = await action(startPayload());
      conversationToken = data.conversation_token; warning.hidden = true; controls(); input.focus();
    } catch (_) {startStatus.textContent = t('Не удалось начать беседу. Проверьте вход в кабинет и повторите.'); start.disabled = false;}
  });
  function el(tag, text, parent, original = false) {
    const node = document.createElement(tag);
    if (text) node.textContent = original ? text : t(text);
    if (parent) parent.append(node);
    return node;
  }
  function source(parent, row) {
    const details = el('details', '', parent);
    const pages = row.page_end && row.page_end !== row.page ? `${row.page}–${row.page_end}` : row.page;
    el('summary', `${row.title} · ${row.clause} · ${t('стр.')} ${pages}`, details);
    const quote = el('blockquote', '', details);
    el('mark', row.text, quote, true);
    // Дополнительная клиентская проверка; сервер формирует ссылку из метаданных корпуса.
    try {
      const url = new URL(row.url);
      if (url.protocol === 'https:' && (url.hostname === 'mgimo.ru' || url.hostname.endsWith('.mgimo.ru')) && !url.username && !url.password) {
        const a = el('a', tf('Открыть оригинал · стр. {page}', {page:row.page}), details);
        a.href = url.href; a.target = '_blank'; a.rel = 'noopener noreferrer';
      }
    } catch (_) { /* непроверяемая ссылка не показывается */ }
    const description = row.verification_status === 'snapshot'
      ? tf('Загруженный снимок от {date}. Действующая редакция и точность OCR полностью не подтверждены.', {date:row.downloaded_at})
      : tf('Редакция: {revision}. Действует с {effective}. Источник проверен {checked}.', {revision:row.revision,effective:row.effective_from,checked:row.checked_at});
    const meta = el('p', description, details);
    meta.className = 'assistant__meta';
  }
  function show(data) {
    const article = el('article', '', log); article.className = 'assistant__message';
    el('h2', labels[data.status] || labels.error, article);
    el('p', data.message || 'Повторите запрос позднее.', article);
    if (data.status === 'error') {
      const a = el('a', 'Открыть разделы кабинета', article); a.href = '/services';
    }
    if (data.translation_notice) el('p', data.translation_notice, article);
    if (data.status === 'consent_required') {
      const requestedQuestion = input.value;
      pendingQuestion = requestedQuestion;
      const allow = el('button', 'Разрешить для этой беседы', article); allow.type = 'button'; allow.className = 'btn btn--primary';
      const deny = el('button', 'Не разрешать', article); deny.type = 'button'; deny.className = 'btn';
      const token = conversationToken;
      allow.addEventListener('click', async () => {
        if (busy || token !== conversationToken) return;
        busy = true; controls(); allow.disabled = true; deny.disabled = true;
        try {
          await action({action:'allow_grades', conversation_token:conversationToken});
          el('p', 'Доступ к оценкам разрешён для этой беседы.', article);
          input.value = requestedQuestion; pendingQuestion = '';
          repeatingAfterConsent = true; busy = false; controls(); form.requestSubmit();
        } catch (_) {
          el('p', 'Не удалось сохранить разрешение. Повторите вопрос.', article);
          busy = false; controls();
        }
      });
      deny.addEventListener('click', () => {
        if (busy) return;
        allow.disabled = true; deny.disabled = true; pendingQuestion = ''; input.value = '';
        el('p', 'Оценки не переданы. Можно задать другой вопрос.', article);
      });
    }
    if (data.facts?.length) {
      const list = el('ul', '', article);
      for (const fact of data.facts) {
        const li = el('li', '', list); el('p', fact.translation || fact.text, li);
        if (fact.translation) {const original = el('details', '', li); el('summary', 'Оригинал на русском', original); el('p', fact.text, original, true);}
        if (/^\/(grades|electives|schedule(?:\?week=\d{4}-\d{2}-\d{2})?|mfc\/certificates(?:\/\d+)?)$/.test(fact.url)) {
          const a = el('a', `${t('Открыть:')} ${t(fact.label)}`, li); a.href = fact.url;
        }
      }
    }
    if (data.personal_checked_at) el('p', tf('Кабинет проверен: {date}. Статусы ограничены временем последней синхронизации.', {date:new Date(data.personal_checked_at).toLocaleString(window.lmsLocale || 'ru')}), article);
    if (data.summary) {
      el('h3', 'Краткий ответ · выдержка', article);
      el('p', data.summary.translation || data.summary.text, article); source(article, data.summary);
    }
    const visibleSteps = (data.steps || []).filter(step => step.id !== data.summary?.id || step.responsible || step.deadline || step.form);
    if (visibleSteps.length) {
      el('h3', 'Шаги по положениям', article);
      const list = el('ol', '', article);
      for (const step of visibleSteps) {
        const li = el('li', '', list);
        const repeatsSummary = data.summary?.id === step.id;
        if (!repeatsSummary) el('p', step.translation || step.text, li);
        for (const [key, label] of [['responsible','Кто'],['deadline','Срок'],['form','Бланк']]) {
          if (step[key]) el('p', `${t(label)}: ${step[key]}`, li);
        }
        if (!repeatsSummary) source(li, step);
      }
    }
    if (data.snapshot) {
      const meta = el('p', tf('Дата запроса: {date}. Снимок корпуса: {snapshot}. Актуальность редакции указана в источниках.', {date:data.as_of,snapshot:data.snapshot.slice(0,12)}), article);
      meta.className = 'assistant__meta';
    }
    if (data.dropped_steps) el('p', tf('Не прошли проверку: {count}. Полнота ответа ограничена.', {count:data.dropped_steps}), article);
    article.scrollIntoView({block:'nearest', behavior:'smooth'});
  }
  root.querySelectorAll('.assistant__example').forEach(button => button.addEventListener('click', () => { input.value = button.dataset.question; input.focus(); }));
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault(); if (!send.disabled) form.requestSubmit();}
  });
  clear.addEventListener('click', async () => {
    if (busy) return;
    if (!conversationToken) {log.replaceChildren(); return;}
    busy = true; controls();
    try {
      await action({action:'clear', conversation_token:conversationToken});
      conversationToken = ''; pendingQuestion = ''; log.replaceChildren(); input.value = ''; progress.textContent = ''; welcome.hidden = false;
      if (student) {
        warning.hidden = false; start.disabled = !ack.checked || root.dataset.enabled !== 'true';
        studentStatus.textContent = 'Выберите демо-студента и начните новую беседу. Разрешение на оценки отозвано.';
      } else {
        const data = await action(startPayload()); conversationToken = data.conversation_token;
      }
    } catch (_) {
      conversationToken = ''; warning.hidden = false; welcome.hidden = false; start.disabled = !ack.checked || root.dataset.enabled !== 'true';
      startStatus.textContent = 'Беседа недоступна. Начните новую; разрешение на оценки нужно будет дать заново.';
    } finally {busy = false; controls();}
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const question = input.value.trim();
    if (busy || !conversationToken || question.length < 3 || send.disabled) return;
    busy = true; send.disabled = true; clear.disabled = true;
    welcome.hidden = true;
    input.disabled = true; day.disabled = true;
    progress.textContent = t('Проверяем вопрос и относящиеся к нему источники. Это может занять до минуты.');
    if (!repeatingAfterConsent) {
      const user = el('article', '', log); user.className = 'assistant__message assistant__message--user';
      el('p', question, user, true);
    }
    repeatingAfterConsent = false;
    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 80000);
    try {
      const response = await fetch('/s/assistant/ask', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':root.dataset.csrf},
        credentials:'same-origin', redirect:'error', signal:controller.signal,
        body:JSON.stringify({question, as_of:day.value || null, conversation_token:conversationToken})});
      if (response.status === 403) {
        conversationToken = ''; warning.hidden = false; welcome.hidden = false; start.disabled = !ack.checked || root.dataset.enabled !== 'true';
        throw new Error();
      }
      if (!response.ok && response.status !== 429) throw new Error();
      const data = await response.json(); show(data);
      if (data.navigation && /^\/(attendance|learning\/tasks|s\/assistant|services#[a-z0-9-]+)$/.test(data.navigation.href)) {
        window.location.assign(data.navigation.href);
      }
      if (!['error','unavailable','busy','consent_required'].includes(data.status)) input.value = '';
    } catch (_) {
      show({status:'error', message:'Не удалось получить ответ. Проверьте соединение и вход в кабинет. Вопрос сохранён в поле ввода.'});
    } finally {
      clearTimeout(timer); busy = false; controls(); progress.textContent = ''; if (conversationToken) input.focus();
    }
  });
})();
