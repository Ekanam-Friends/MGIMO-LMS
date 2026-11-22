"""Уточнения маршрута до чтения личных данных; нормы остаются в корпусе."""
import re

SESSION_ADMISSION = re.compile(
    r"\s*(?:а\s+)?(?:есть ли у меня допуск|у меня есть допуск|я допущен[а]?(?: ли)?)"
    r"\s+(?:к|до)\s+[са]ессии[.!?\s]*",
    re.I,
)
_CONDUCT = re.compile(
    r"(?:а\s+)?что\s+будет[, ]+если\s+я\s+"
    r"(?:гол(?:ым|ой)\s+приду\s+на\s+пар[уы]|оскорблю\s+преподавателя)[.!?\s]*"
)
_LEAVE = re.compile(r"(?:а\s+)?как\s+(?:мне\s+)?(?:взять|оформить|получить)\s+академ[.!?\s]*")


def public_topic(question: str) -> str | None:
    q = question.lower().strip()
    if _CONDUCT.fullmatch(q):
        return question
    if _LEAVE.fullmatch(q):
        return "Как оформить академический отпуск?"
    return None


def normalize_scopes(question, scopes):
    q = question.lower().replace("ё", "е")
    personal = bool(re.search(r"\b(?:я|мне|меня|мои|моих|моим|моя|мой|мое|наши|наших)\b|положен|полагает|имею ли право", q))
    homework = bool(re.search(r"домашн.{0,12}(?:задан|работ)|\bдз\b", q))
    rules = bool(re.search(r"правил|порядок|критери|условия|академ.{0,15}отпуск|отчислен|как.{0,15}(?:оценива|сдава|оформ|получ)", q))
    grades = bool(re.search(r"оценк|балл|зачетк|успеваем|долг", q))
    schedule = bool(re.search(r"расписан|когда у (?:меня|нас)|как зовут|кто (?:у меня )?(?:ведет|преподает)|имя преподавател", q))
    requests = bool(re.search(r"статус.{0,30}(?:заяв|справ)|(?:заяв|справ).{0,30}статус|мои заяв|моя заяв", q))
    scholarship = bool(re.search(r"стипенд|стипух", q))
    hypothetical_grade = bool(re.search(r"\bесли\b.{0,60}\b(?:будет|получу)\s+(?:[2-5]|тройк\w*)\b", q))
    absence = bool(re.search(r"прогул|пропущ|пропуск.{0,15}(?:пар|занят)|не (?:приду|пойду)", q))
    procedure = (bool(re.search(r"академ.{0,15}отпуск|отчислен|справк", q))
                 and bool(re.search(r"как.{0,20}(?:оформ|получ|подать)|правил|порядок|условия|основан", q))
                 and not re.search(r"заказ|статус|мо[юя] справк", q))
    public_rule = ((scholarship and (not personal or hypothetical_grade)) or
                   (absence and not re.search(r"сколько|мои|у меня", q)) or
                   (homework and rules and not personal) or procedure)
    if public_rule:
        # Общая норма не открывает кабинет; независимые части вопроса сохраняем.
        homework_request = homework and (personal or not rules or
            bool(re.search(r"(?:и|а также)\s+(?:мои\s+)?(?:домашние (?:задания|работы)|дз)\b", q)))
        return (["public"] + (["homework"] if homework_request else [])
                + (["schedule"] if schedule else []) + (["requests"] if requests else [])
                + (["grades"] if re.search(r"(?:покажи|мои|наши).{0,30}(?:балл|оценк)|(?:и|а также)\s+(?:мои\s+)?(?:балл|оценк)", q)
                   or (grades and "grades" in scopes and re.search(r"\s(?:и|а также)\s", q)) else []))
    result = list(dict.fromkeys(scopes))
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
