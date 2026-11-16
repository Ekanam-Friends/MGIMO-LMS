# Синхронизация GitHub Issues и Project

Project: [MGIMO-LMS — Цифровой деканат](https://github.com/orgs/Ekanam-Friends/projects/4).

## Что уже настроено в GitHub — 16 ноября 2026

- Все 29 Issues репозитория добавлены в Project; дублей и черновиков нет.
- Auto-add to project: репозиторий MGIMO-LMS, фильтр `is:issue`, правило включено.
- Item added to project → Todo (существовавшее правило).
- Auto-close issue: Done → закрыть Issue (существовавшее правило).
- Item closed → Done (существовавшее правило).
- Item reopened → Todo (включено в этой задаче).

Название, описание и исполнители карточки связанного Issue — его собственные поля,
поэтому отдельное копирование этих значений не требуется.

## Подготовленная полная синхронизация

Workflow `.github/workflows/project-sync.yml` запускает стандартный Python и `gh` на
GitHub-hosted runner. При отсутствии переменной `PROJECT_SYNC_ENABLED=true` job пропускается.
Доступ пока не подключён. Локальный скрипт не требует зависимостей приложения.

После включения: каждые пять минут (GitHub может задерживать запуск) скрипт добавляет
отсутствующие Issues и преобразует **все активные черновики этого Project** в Issues
MGIMO-LMS. Отдельной колонки для заметок-черновиков в этом режиме нет.
Преобразование сохраняет карточку, не создавая вторую; повторный запуск читает её уже как Issue.
Архивные карточки, pull requests и Issues других репозиториев не меняются.
Архивный Issue учитывается как существующий участник Project.

| Изменение | Результат |
| --- | --- |
| Карточка → Done | Issue закрывается |
| Карточка → Todo / In Progress после закрытия | Issue переоткрывается |
| Issue закрыт | Карточка → Done |
| Issue переоткрыт | Карточка → Todo |
| Закрытый Issue впервые добавлен в Project | Карточка → Done; Issue остаётся закрытым |

При конфликте сравниваются время последнего события закрытия/переоткрытия Issue и время
изменения поля Status. Более позднее действие побеждает; при равенстве — Issue.
Редактирование описания или комментарий не считаются изменением состояния.
Начальное Todo от Item added не переоткрывает старые закрытые Issues.
Перед изменением существующей карточки скрипт перечитывает её. GitHub не предоставляет
транзакцию между этими двумя объектами: одновременное ручное изменение в момент записи
остаётся ограничением опроса. Запуски workflow сериализованы через concurrency.

## Подключение доступа

1. Добавить workflow и скрипт в **default branch** репозитория: расписание работает от неё.
2. В Settings → Secrets and variables → Actions добавить секрет `PROJECT_SYNC_TOKEN`.
   Для подготовленного варианта нужен токен с доступом к Issues MGIMO-LMS и Project №4:
   classic PAT — `repo` и `project`, с необходимым одобрением организации/SSO.
   Не вставлять токен в файлы, сообщения или параметры запуска.
   Для долговременной эксплуатации GitHub рекомендует GitHub App для Project организации;
   в таком варианте надо дополнить workflow получением installation token.
3. Добавить Actions variable `PROJECT_SYNC_ENABLED` со значением `true`.
4. Запустить workflow вручную с `dry_run=true`, проверить журнал предлагаемых изменений.
5. Запустить вручную с `dry_run=false`, затем проверить результат в Issues и Project.
6. Проверить на специально созданной тестовой задаче закрытие, перенос из Done обратно,
   переоткрытие вручную и преобразование черновика. Такие записи на настоящем Project
   при подготовке не создавались.

Отключение: переменная `PROJECT_SYNC_ENABLED=false`; встроенные правила останутся активны.
Для обратного направления вручную до подключения workflow: меню карточки-черновика →
Convert to issue → MGIMO-LMS. Уже связанные Issues преобразовывать не требуется.

Проверки подготовки: модульные тесты конфликтов статусов и read-only запуск скрипта
`python scripts/sync_github_project.py --dry-run` на настоящем Project.
Запись через новый workflow и доступ будущего токена пока не проверены.

Источники: [встроенная автоматизация](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-built-in-automations),
[GitHub Actions и доступ к Projects](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions),
[GraphQL Projects](https://docs.github.com/en/graphql/reference/projects).
