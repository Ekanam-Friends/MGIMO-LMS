"""Представление проверенного ответа для продолжения серверной беседы."""
import json


def answer_context(result: dict) -> str:
    def shorten(value, limit):
        return value if not value or len(value) <= limit else value[:limit] + " [сокращено для истории]"

    def text(row, limit):
        return shorten(row.get("translation") or row.get("text", ""), limit)

    summary = result.get("summary")
    # Порядок совпадает с видимыми шагами в assistant.js.
    steps = [step for step in result.get("steps", []) if not summary or step["id"] != summary["id"]
             or any(step.get(key) for key in ("responsible", "deadline", "form"))]
    # Сокращаем выдержки равномерно: обрезка строки JSON теряла поздние пункты.
    # Это только память беседы; тексты доказательств в ответе не меняются.
    for excerpt_size in (700, 350, 175, 80, 32, 0):
        encoded = json.dumps({
            "status": result["status"], "message": shorten(result["message"], excerpt_size),
            "summary": text(summary, excerpt_size) if summary else None,
            "steps": [{"number": i, "text": text(step, excerpt_size),
                       **{key: shorten(step.get(key), min(100, excerpt_size))
                          for key in ("responsible", "deadline", "form")}}
                      for i, step in enumerate(steps, 1)],
            "facts": [text(fact, excerpt_size) for fact in result.get("facts", [])],
        }, ensure_ascii=False)
        if len(encoded) <= 12000:
            return encoded
    return json.dumps({"status": result["status"], "message": "Предыдущий ответ слишком велик для контекста; уточните предмет вопроса."},
                      ensure_ascii=False)
