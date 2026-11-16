"""Однократная разметка статических надписей Jinja и сбор ключей словаря.

Ничего не переводит через сеть. Сохраняет исходные выражения Jinja, HTML и отступы.
"""
import ast
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from jinja2 import Environment, nodes

ROOT = Path(__file__).resolve().parents[1]
KEYS = set()
JINJA = re.compile(r"({{.*?}}|{%.*?%}|{#.*?#})", re.S)
CYRILLIC = re.compile(r"[А-Яа-яЁё]")


class Marker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.in_script = None

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        def attr(match):
            prefix, quote, value = match.groups()
            if CYRILLIC.search(value) and "I18N_JINJA_" not in value:
                KEYS.add(value)
                return prefix + quote + "{{ _(" + repr(value) + ") }}" + quote
            return match.group()
        raw = re.sub(r'((?:aria-label|title|placeholder|alt)\s*=\s*)([\x22\x27])(.*?)\2', attr, raw, flags=re.S)
        self.parts.append(raw)
        if tag in {"script", "style"}:
            self.in_script = tag

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")
        if tag == self.in_script:
            self.in_script = None

    def handle_data(self, value):
        for part in re.split(r"(I18N_JINJA_\d+_END)", value):
            if not self.in_script and CYRILLIC.search(part):
                text = part.strip()
                KEYS.add(text)
                leading, trailing = part[:len(part)-len(part.lstrip())], part[len(part.rstrip()):]
                self.parts.append(leading + "{{ _(" + repr(text) + ") }}" + trailing)
            else:
                self.parts.append(part)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, value):
        self.parts.append("<!--" + value + "-->")

    def handle_decl(self, value):
        self.parts.append("<!" + value + ">")


def main():
    folder = ROOT / "app/web/templates"
    for path in [*folder.glob("*.html"), *folder.glob("partials/*.html")]:
        if path.name == "_icons.html":
            continue
        original = path.read_text(encoding="utf-8")
        protected = []
        def protect(match):
            protected.append(match.group())
            return f"I18N_JINJA_{len(protected)-1}_END"
        parser = Marker()
        parser.feed(JINJA.sub(protect, original))
        parser.close()
        transformed = "".join(parser.parts)
        for n, value in enumerate(protected):
            transformed = transformed.replace(f"I18N_JINJA_{n}_END", value)
        if transformed != original:
            path.write_text(transformed, encoding="utf-8")
        # Ключи, уже размеченные при предыдущем запуске.
        for literal in re.findall(r"_\((('(?:[^'\\]|\\.)*'|\x22(?:[^\x22\\]|\\.)*\x22))\)", transformed):
            KEYS.add(ast.literal_eval(literal[0]))
        for node in Environment().parse(transformed).find_all(nodes.Const):
            if isinstance(node.value, str) and CYRILLIC.search(node.value) and len(node.value) <= 1500:
                KEYS.add(node.value.strip())
    sources = ["app/web/nav.py", "app/web/calendar_view.py", "app/web/schedule_view.py",
               "app/models/certificates.py", "app/models/grades.py", "app/grades/scale.py", "app/assistant/service.py"]
    for name in sources:
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value.strip()
                if not isinstance(parents.get(node), ast.Expr) and "\\" not in value and CYRILLIC.search(value) and "\n" not in value and len(value) <= 500:
                    KEYS.add(value)
    javascript = (ROOT / "app/web/static/assistant.js").read_text(encoding="utf-8")
    for match in re.finditer(r"'(?:[^'\\]|\\.)*'", javascript):
        try:
            value = ast.literal_eval(match.group())
        except (ValueError, SyntaxError):
            continue
        if CYRILLIC.search(value) and len(value) <= 1500:
            KEYS.add(value)
    output = ROOT / "app/web/locales"
    output.mkdir(exist_ok=True)
    (output / "source.json").write_text(json.dumps(sorted(KEYS), ensure_ascii=False, indent=2), encoding="utf-8")
    for language in ("en", "fr", "zh"):
        path = output / f"{language}.json"
        if path.exists():
            values = json.loads(path.read_text(encoding="utf-8"))
            values = {key: value for key, value in values.items() if key in KEYS}
            path.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Ключей интерфейса: {len(KEYS)}")


if __name__ == "__main__":
    main()
