"""Уточнения маршрута до чтения личных данных; нормы остаются в корпусе."""
import re

def normalize_scopes(question, scopes):
    q = question.lower().replace("ё", "е")
    personal = bool(re.search(r"\b(?:я|мне|меня|мои|моих|моим|моя|мой|мое|наши|наших)\b|положен|полагает|имею ли право", q))
    homework = bool(re.search(r"домашн.{0,12}(?:задан|работ)|\bдз\b", q))
    rules = bool(re.search(r"правил|порядок|критери|условия|академ.{0,15}отпуск|отчислен|как.{0,15}(?:оценива|сдава|оформ|получ)", q))
    grades = bool(re.search(r"оценк|балл|зачетк|успеваем|долг", q))
    schedule = bool(re.search(r"расписан|когда у (?:меня|нас)|как зовут|кто (?:у меня )?(?:ведет|преподает)|имя преподавател", q))
    requests = bool(re.search(r"статус.{0,30}(?:заяв|справ)|(?:заяв|справ).{0,30}статус|мои заяв|моя заяв", q))
    scholarship = bool(re.search(r"стипенд|стипух", q))
    absence = bool(re.search(r"прогул|пропущ|пропуск.{0,15}(?:пар|занят)|не (?:приду|пойду)", q))
    procedure = (bool(re.search(r"академ.{0,15}отпуск|отчислен|справк", q))
                 and bool(re.search(r"как.{0,20}(?:оформ|получ|подать)|правил|порядок|условия|основан", q))
                 and not re.search(r"заказ|статус|мо[юя] справк", q))
    notice_clauses = [clause for clause in re.split(
        r"[,;.!?]|\bа также\b|\bи\b(?=\s+(?:мои|покажи|прочитай|проверь|какие|что)\b)", q)
        if re.search(r"уведомлен|объявлен|сообщени|написал", clause)]
    notice_rules = [clause for clause in notice_clauses if re.search(
        r"правил|поряд(?:ок|к)|услови|регламент|как.{0,25}(?:публиков|опубликов|размест|настро)", clause)]
    notifications_request = any(
        clause not in notice_rules and re.search(
            r"мои\b|мне\b|у меня|покажи|прочитай|проверь|новые|непрочитан|кто.{0,20}написал|какие|есть ли",
            clause)
        for clause in notice_clauses)
    public_rule = (bool(notice_rules) or (scholarship and not personal) or
                   (absence and not re.search(r"сколько|мои|у меня", q)) or
                   (homework and rules and not personal) or procedure)
    if public_rule:
        # Общая норма не открывает кабинет; независимые части вопроса сохраняем.
        homework_request = homework and (personal or not rules or
            bool(re.search(r"(?:и|а также)\s+(?:мои\s+)?(?:домашние (?:задания|работы)|дз)\b", q)))
        return (["public"] + (["homework"] if homework_request else [])
                + (["schedule"] if schedule else []) + (["requests"] if requests else [])
                + (["notifications"] if notifications_request else [])
                + (["grades"] if re.search(r"(?:покажи|мои|наши).{0,30}(?:балл|оценк)|(?:и|а также)\s+(?:мои\s+)?(?:балл|оценк)", q)
                   or (grades and "grades" in scopes and re.search(r"\s(?:и|а также)\s", q)) else []))
    result = list(dict.fromkeys(scopes))
    if notifications_request and "notifications" not in result:
        result.append("notifications")
    if homework:
        result = [scope for scope in result if scope not in {"grades", "public"}]
        result = ["homework"] + [scope for scope in result if scope != "homework"]
        if grades:
            result.append("grades")
        if rules or scholarship:
            result.append("public")
    if schedule and "schedule" not in result:
        result.append("schedule")
    if requests and "requests" not in result:
        result.append("requests")
    return list(dict.fromkeys(result))
