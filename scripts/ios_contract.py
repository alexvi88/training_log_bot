"""Манифест контракта «сервер ↔ iOS-приложение»: что Swift-модели ждут от ответов /v1.

Читает исходники приложения (репозиторий `training_log_bot_ios`) и строит
`tests/ios_contract.json`:

* `types` — каждый `Decodable`/`Codable` тип приложения: поля, их JSON-ключи
  (после `keyDecodingStrategy = .convertFromSnakeCase` и `CodingKeys`), тип и
  опциональность. Обязательное поле — то, чьё отсутствие или `null` роняет
  разбор ВСЕГО ответа: `let x: T`, а также `var x: T = значение` (синтезатор
  Swift пропускает только `let` со значением, `var` со значением ключ всё
  равно требует). Необязательное — `T?`, `decodeIfPresent`, `try?`, `?? значение`
  в ручном `init(from:)`.
* `endpoints` — какой метод клиента какой путь зовёт и каким корневым типом
  разбирает ответ (`request`/`requestOptional` в `APIClient*.swift`).

Манифест — снимок: CI бота iOS-репозитория не видит, поэтому его обновляют
руками этой командой и коммитят вместе с причиной:

    python scripts/ios_contract.py --ios ../training_log_bot_ios

`--check` ничего не пишет и падает (код 1), если манифест устарел относительно
указанного чекаута. Парсер Swift — регулярки плюс стек фигурных скобок, не
компилятор: всё, чего он не понимает, он не угадывает, а останавливается и
просит дописать `MANUAL_TYPES`/`MANUAL_ENDPOINTS`/`FIELD_OVERRIDES` ниже.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "tests" / "ios_contract.json"
DEFAULT_IOS = ROOT.parent / "training_log_bot_ios"

# Где искать модели — всё приложение: `Decodable` бывают и во вьюхах
# (`WorkoutVisits`), и локальными структурами внутри метода клиента.
MODEL_GLOBS = ["TrainingLog/**/*.swift"]
# Где искать вызовы API.
CLIENT_GLOB = "TrainingLog/Networking/APIClient*.swift"

# Типы, чей `init(from:)` читает значение целиком (singleValueContainer), а не
# по ключам: разбирать их по полям нечего, форма задаётся руками.
#   scalar — принимает перечисленные JSON-типы; lenient — принимает что угодно.
MANUAL_TYPES: dict[str, dict] = {
    "FlexibleID": {
        "kind": "scalar", "accepts": ["str", "int"],
        "reason": "init(from:) через singleValueContainer: строка или целое",
    },
    "LenientCount": {
        "kind": "lenient",
        "reason": "init(from:) через singleValueContainer: число, список или что угодно (иначе nil)",
    },
}

# Поправки к полям, которые парсер прочитать не может (ручной `init(from:)`
# с нестандартным чтением). Формат: {"Тип": {"jsonKey": {"optional": True, "reason": "..."}}}.
FIELD_OVERRIDES: dict[str, dict[str, dict]] = {}

# Вызовы API, где путь или тип ответа не вытаскиваются разбором. Формат:
# {"swift": "APIClient.метод", "method": "GET", "path": "/x/{}", "root": "Type", "array": False, "nullable": False}
MANUAL_ENDPOINTS: list[dict] = []

# Встроенные типы Swift → «вид» для проверки в тесте.
PRIMITIVES = {
    "String": "string", "Substring": "string", "URL": "string", "UUID": "string", "Data": "string",
    "Int": "int", "Int8": "int", "Int16": "int", "Int32": "int", "Int64": "int",
    "UInt": "int", "UInt8": "int", "UInt16": "int", "UInt32": "int", "UInt64": "int",
    "Double": "double", "Float": "double", "CGFloat": "double", "Decimal": "double", "TimeInterval": "double",
    "Bool": "bool",
    "Date": "date",
}


# --------------------------------------------------------------------------
# Предобработка исходника: комментарии и строки.
# --------------------------------------------------------------------------

def preprocess(src: str) -> tuple[str, str]:
    """(clean, masked): без комментариев; и ещё без содержимого строк.

    Обе строки той же длины, что исходник, переводы строк на местах — поэтому
    индексы одной годятся для другой."""
    n = len(src)
    clean = list(src)
    masked = list(src)

    def blank(a: int, b: int, *, only_masked: bool = False) -> None:
        for k in range(a, b):
            if src[k] != "\n":
                masked[k] = " "
                if not only_masked:
                    clean[k] = " "

    def skip_string(i: int) -> int:
        """i — на открывающей кавычке. Возвращает индекс после закрывающей."""
        triple = src.startswith('"""', i)
        j = i + (3 if triple else 1)
        while j < n:
            c = src[j]
            if c == "\\":
                if src.startswith("\\(", j):
                    j = skip_interpolation(j + 2)
                    continue
                j += 2
                continue
            if triple and src.startswith('"""', j):
                return j + 3
            if not triple and c == '"':
                return j + 1
            if not triple and c == "\n":  # незакрытая строка — не зависаем
                return j
            j += 1
        return n

    def skip_interpolation(j: int) -> int:
        depth = 1
        while j < n and depth:
            c = src[j]
            if c == '"':
                j = skip_string(j)
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            j += 1
        return j

    i = 0
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif src.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth += 1
                    j += 2
                elif src.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            blank(i, j)
            i = j
        elif src[i] == '"':
            j = skip_string(i)
            quotes = 3 if src.startswith('"""', i) else 1
            blank(i + quotes, max(i + quotes, j - quotes), only_masked=True)
            i = j
        else:
            i += 1
    return "".join(clean), "".join(masked)


def brace_matches(masked: str) -> dict[int, int]:
    stack, match = [], {}
    for i, c in enumerate(masked):
        if c == "{":
            stack.append(i)
        elif c == "}" and stack:
            match[stack.pop()] = i
    return match


def balanced(text: str, open_idx: int, open_ch: str = "(", close_ch: str = ")") -> int:
    """Индекс закрывающей скобки для открывающей в `open_idx` (по masked-тексту)."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1


# --------------------------------------------------------------------------
# Дерево объявлений.
# --------------------------------------------------------------------------

DECL_RE = re.compile(
    r"\b(?P<kw>struct|class|enum|extension|actor)\s+(?P<name>[A-Za-z_][\w.]*)(?P<rest>[^{;}]*)\{"
)
FUNC_RE = re.compile(r"\bfunc\s+(?P<name>[A-Za-z_]\w*)\s*(?:<[^>(]*>)?\s*\(")


class Node:
    def __init__(self, kind: str, name: str, open_idx: int, close_idx: int, rest: str, file: str):
        self.kind, self.name, self.open, self.close = kind, name, open_idx, close_idx
        self.rest, self.file = rest, file
        self.parent: Node | None = None
        self.children: list[Node] = []
        self.ret: str | None = None  # у func — тип результата

    @property
    def scope(self) -> list[str]:
        parts = []
        node: Node | None = self
        while node:
            parts.append(node.name + ("()" if node.kind == "func" else ""))
            node = node.parent
        return list(reversed(parts))

    @property
    def qname(self) -> str:
        return ".".join(self.scope)


def build_tree(masked: str, match: dict[int, int], file: str) -> list[Node]:
    nodes: list[Node] = []
    for m in DECL_RE.finditer(masked):
        open_idx = m.end() - 1
        if open_idx in match and not re.search(r"\bcase\b", masked[max(0, m.start() - 8):m.start()]):
            nodes.append(Node(m.group("kw"), m.group("name"), open_idx, match[open_idx], m.group("rest"), file))
    for m in FUNC_RE.finditer(masked):
        close_paren = balanced(masked, m.end() - 1)
        if close_paren < 0:
            continue
        j, depth = close_paren + 1, 0
        while j < len(masked):
            c = masked[j]
            if c in "([<":
                depth += 1
            elif c in ")]>":
                depth -= 1
            elif c == "{" and depth <= 0:
                break
            elif c == ";" or (c == "\n" and re.match(r"\s*(func|var|let|init|case|static|\})\b", masked[j + 1:j + 12])):
                j = len(masked)  # протокол или объявление без тела
                break
            j += 1
        if j >= len(masked) or j not in match:
            continue
        node = Node("func", m.group("name"), j, match[j], "", file)
        sig = masked[close_paren + 1:j]
        arrow = sig.find("->")
        if arrow >= 0:
            ret = sig[arrow + 2:]
            ret = re.split(r"\bwhere\b", ret)[0].strip()
            node.ret = re.sub(r"\s+", " ", ret)
        nodes.append(node)
    nodes.sort(key=lambda x: (x.open, -x.close))
    stack: list[Node] = []
    for nd in nodes:
        while stack and not (stack[-1].open < nd.open < stack[-1].close):
            stack.pop()
        if stack:
            nd.parent = stack[-1]
            stack[-1].children.append(nd)
        stack.append(nd)
    return nodes


def top_level_text(masked: str, node: Node) -> str:
    """Тело узла без содержимого вложенных `{ }` (сами скобки остаются —
    по ним видно вычисляемое свойство)."""
    chars = list(masked[node.open + 1:node.close])
    base = node.open + 1
    for ch in node.children:
        for k in range(ch.open + 1 - base, ch.close - base):
            if chars[k] != "\n":
                chars[k] = " "
    # Вычисляемые свойства и замыкания — не узлы дерева; гасим любые `{…}` глубже первого уровня.
    text = "".join(chars)
    out, depth = [], 0
    for c in text:
        if c == "{":
            out.append(c)
            depth += 1
        elif c == "}":
            depth -= 1
            out.append(c)
        else:
            out.append(c if depth == 0 or c == "\n" else " ")
    return "".join(out).replace(";", "\n")


# --------------------------------------------------------------------------
# Типы Swift.
# --------------------------------------------------------------------------

def split_top(text: str, sep: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for c in text:
        if c in "[(<":
            depth += 1
        elif c in "])>":
            depth -= 1
        if c == sep and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    parts.append(cur)
    return parts


def unwrap_optional(t: str) -> tuple[str, bool]:
    t = t.strip()
    optional = False
    while True:
        if t.endswith("?") or t.endswith("!"):
            t, optional = t[:-1].strip(), True
        elif t.startswith("Optional<") and t.endswith(">"):
            t, optional = t[len("Optional<"):-1].strip(), True
        else:
            return t, optional


def normalize_type(t: str, resolve) -> str:
    """Swift-тип → строка с разрешёнными именами и без внешнего `?`:
    `[Trend]` → `[ExerciseProgressSeries.Trend]`, `[String: [Int]]` → `[String:[Int]]`.
    Внутренние `?` (`[T?]`) сохраняются."""
    t, _ = unwrap_optional(t)
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1]
        kv = split_top(inner, ":")
        if len(kv) == 2:
            return f"[{kv[0].strip()}:{normalize_inner(kv[1], resolve)}]"
        return f"[{normalize_inner(inner, resolve)}]"
    if t.startswith("Array<") and t.endswith(">"):
        return f"[{normalize_inner(t[6:-1], resolve)}]"
    if t.startswith("Dictionary<") and t.endswith(">"):
        k, v = split_top(t[11:-1], ",")
        return f"[{k.strip()}:{normalize_inner(v, resolve)}]"
    return resolve(t)


def normalize_inner(t: str, resolve) -> str:
    base, optional = unwrap_optional(t)
    return normalize_type(base, resolve) + ("?" if optional else "")


# --------------------------------------------------------------------------
# Разбор моделей.
# --------------------------------------------------------------------------

PROP_RE = re.compile(
    r"^\s*(?P<attrs>(?:@\w+(?:\([^)]*\))?\s+)*)"
    r"(?P<mods>(?:(?:public|internal|private|fileprivate|open)(?:\(set\))?\s+)*)"
    r"(?P<kw>let|var)\s+(?P<name>[A-Za-z_]\w*)\s*"
    r"(?::\s*(?P<type>[^={\n]+?))?\s*"
    r"(?:=\s*(?P<default>[^\n{]*?))?\s*(?P<brace>\{[^\n]*)?$"
)
NON_STORED_PREFIX = re.compile(r"^\s*(?:@\w+\s+)*(?:(?:public|internal|private|fileprivate|open)\s+)*(static|lazy|class|weak|unowned|override)\b")
CASE_RE = re.compile(r"\bcase\s+([^\n]+)")


def infer_default_type(default: str) -> str | None:
    d = default.strip()
    if d in ("true", "false"):
        return "Bool"
    if re.fullmatch(r"-?\d+", d):
        return "Int"
    if re.fullmatch(r"-?\d+\.\d+", d):
        return "Double"
    if d.startswith('"'):
        return "String"
    return None


def parse_coding_keys(body_top: str, node: Node, masked_full: str, clean_full: str) -> dict[str, str] | None:
    """property → JSON-ключ (после convertFromSnakeCase) по `enum CodingKeys`, либо None."""
    for ch in node.children:
        if ch.kind == "enum" and ch.name == "CodingKeys":
            body = clean_full[ch.open + 1:ch.close]
            keys: dict[str, str] = {}
            for cm in CASE_RE.finditer(body):
                for item in split_top(cm.group(1), ","):
                    item = item.strip()
                    if not item:
                        continue
                    raw = re.match(r'(\w+)\s*=\s*"([^"]*)"', item)
                    if raw:
                        keys[raw.group(1)] = raw.group(2)
                    else:
                        keys[item.split()[0]] = item.split()[0]
            return keys
    return None


def parse_struct(node: Node, masked: str, clean: str, resolve_in) -> dict:
    top = top_level_text(masked, node)
    coding_keys = parse_coding_keys(top, node, masked, clean)
    custom = re.search(r"\binit\s*\(\s*from\s+\w+\s*:\s*Decoder\s*\)", top)
    props = []
    for line in top.split("\n"):
        if NON_STORED_PREFIX.match(line) or re.match(r"^\s*(init|func|enum|struct|class|extension|typealias|case|subscript)\b", line):
            continue
        m = PROP_RE.match(line)
        if not m or m.group("brace"):
            continue  # вычисляемое свойство / замыкание
        kw, name = m.group("kw"), m.group("name")
        typ, default = m.group("type"), m.group("default")
        if typ is None and default is not None:
            typ = infer_default_type(default)
        if typ is None:
            raise SystemExit(f"{node.file}: {node.qname}.{name}: не понял тип свойства: {line.strip()!r}")
        props.append({"name": name, "kw": kw, "type": typ.strip(), "default": default is not None})

    entry: dict = {"file": node.file, "fields": {}}
    scope_node = node
    flatten: list[str] = []
    init_body = ""
    if custom:
        entry["custom_init"] = True
        im = re.search(r"\binit\s*\(\s*from\s+\w+\s*:\s*Decoder\s*\)[^{]*\{", clean[node.open:node.close])
        if im:
            start = node.open + im.end() - 1
            end = balanced(masked, start, "{", "}")
            init_body = clean[start:end]
        for fm in re.finditer(r"(\w+)\s*=\s*try\s+([\w.]+)\(\s*from:\s*decoder\s*\)", init_body):
            flatten.append(resolve_in(scope_node, fm.group(2)))
            props = [p for p in props if p["name"] != fm.group(1)]
    for p in props:
        if p["kw"] == "let" and p["default"]:
            continue  # `let x = 1` синтезатор не декодирует
        base, optional = unwrap_optional(p["type"])
        if coding_keys is not None:
            if p["name"] not in coding_keys:
                continue  # без ключа в CodingKeys поле не декодируется
            key = coding_keys[p["name"]]
        else:
            key = p["name"]
        field = {"type": normalize_type(base, lambda n, nd=scope_node: resolve_in(nd, n)), "optional": optional}
        if p["default"]:
            field["default"] = True
        if custom:
            stmt = re.search(r"[^\n;]*forKey:\s*\.%s\b[^\n;]*" % re.escape(p["name"]), init_body)
            if stmt is None:
                raise SystemExit(
                    f"{node.file}: {node.qname}.{p['name']}: ручной init(from:) не читает это поле по ключу — "
                    f"добавь тип/поле в MANUAL_TYPES или FIELD_OVERRIDES"
                )
            text = stmt.group(0)
            if re.search(r"decodeIfPresent|try\?|\?\?|decodeNil", text):
                field["optional"] = True
            if "try?" in text:
                field["lenient"] = True
        entry["fields"][key] = field
    if flatten:
        entry["flatten"] = flatten
    for key, patch in FIELD_OVERRIDES.get(node.qname, {}).items():
        entry["fields"].setdefault(key, {"type": "Any", "optional": True}).update(
            {k: v for k, v in patch.items() if k != "reason"}
        )
    return entry


def collect_models(ios: Path) -> tuple[dict, dict[str, Node], dict[str, tuple[str, str, str]]]:
    """(types, nodes by qname, файлы) по всем Swift-файлам приложения."""
    files = sorted({p for g in MODEL_GLOBS for p in ios.glob(g)})
    parsed = []
    all_nodes: dict[str, Node] = {}
    decodable: set[str] = set()
    for path in files:
        rel = str(path.relative_to(ios))
        clean, masked = preprocess(path.read_text(encoding="utf-8"))
        match = brace_matches(masked)
        nodes = build_tree(masked, match, rel)
        parsed.append((rel, clean, masked, nodes))
        for nd in nodes:
            if nd.kind in ("struct", "enum", "class"):
                all_nodes.setdefault(nd.qname, nd)
    # `extension X: Decodable` тоже делает X декодируемым.
    for _, _, _, nodes in parsed:
        for nd in nodes:
            if nd.kind == "extension" and re.search(r"\b(Decodable|Codable)\b", nd.rest):
                decodable.add(nd.qname)
    for q, nd in all_nodes.items():
        if re.search(r"\b(Decodable|Codable)\b", nd.rest):
            decodable.add(q)

    top_names = {q for q in all_nodes if "." not in q}

    def resolve_in(scope_node: Node, name: str) -> str:
        """Имя типа из `scope_node` → квалифицированное имя; встроенные и
        неизвестные остаются как есть."""
        name = name.strip()
        if name in PRIMITIVES or name in ("Any", "AnyCodable", "JSONValue"):
            return name
        head, *tail = name.split(".")
        scope = scope_node.scope
        for depth in range(len(scope), -1, -1):
            cand = ".".join(scope[:depth] + [head])
            if cand in all_nodes or (not scope[:depth] and head in top_names):
                full = ".".join([cand] + tail)
                if full in all_nodes:
                    return full
        return name

    types: dict[str, dict] = {}
    for rel, clean, masked, nodes in parsed:
        for nd in nodes:
            q = nd.qname
            if nd.kind not in ("struct", "enum", "class") or q not in decodable or types.get(q) is not None:
                continue
            if nd.name in MANUAL_TYPES and nd.parent is None:
                types[q] = {"file": rel, **MANUAL_TYPES[nd.name]}
                continue
            if nd.kind == "enum":
                raw = re.search(r":\s*(String|Int)\b", nd.rest)
                if not raw:
                    types[q] = {"_error": f"{rel}: enum {q} без raw-типа — добавь в MANUAL_TYPES"}
                    continue
                types[q] = {"file": rel, "kind": "scalar", "accepts": ["str" if raw.group(1) == "String" else "int"]}
                continue
            try:
                types[q] = parse_struct(nd, masked, clean, resolve_in)
            except SystemExit as exc:  # ошибка важна, только если тип достижим из эндпоинта
                types[q] = {"_error": str(exc)}
    return types, all_nodes, {}


# --------------------------------------------------------------------------
# Эндпоинты клиента.
# --------------------------------------------------------------------------

CALL_RE = re.compile(r"\b(request|requestOptional|rawResponse|rawData)\s*(?:<[^>]*>)?\s*\(")
LET_TYPE_RE = re.compile(r"(?:let|var)\s+(?:\([^)]*\)|\w+)\s*:\s*([^=\n]+?)\s*=\s*(?:try\s+)?(?:await\s+)?(?:try\s+)?$")
STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def path_template(literal: str) -> str:
    """`/workouts/\\(id)/sets?x=\\(y)` → `/workouts/{}/sets`."""
    out, depth, i = "", 0, 0
    while i < len(literal):
        if literal.startswith("\\(", i):
            depth, i = 1, i + 2
            while i < len(literal) and depth:
                depth += {"(": 1, ")": -1}.get(literal[i], 0)
                i += 1
            out += "{}"
        else:
            out += literal[i]
            i += 1
    return out.split("?")[0]


def find_path_for(arg: str, body: str, statics: dict[str, str]) -> str | None:
    arg = arg.strip()
    lit = STRING_RE.search(arg)
    if arg.startswith('"') or (lit and arg.startswith(lit.group(0))):
        return path_template(lit.group(1)) if lit else None
    ident = re.fullmatch(r"[A-Za-z_]\w*", arg)
    if ident:
        for m in re.finditer(r"(?:var|let)\s+%s\s*(?::\s*String)?\s*=\s*\"((?:[^\"\\]|\\.)*)\"" % re.escape(arg), body):
            if m.group(1).startswith("/"):
                return path_template(m.group(1))
        for m in re.finditer(r"\b%s\s*=\s*\"((?:[^\"\\]|\\.)*)\"" % re.escape(arg), body):
            if m.group(1).startswith("/"):
                return path_template(m.group(1))
        return None
    fn = re.match(r"(?:Self|APIClient)\.(\w+)\(", arg)
    if fn and fn.group(1) in statics:
        return statics[fn.group(1)]
    if lit:  # "/workouts/visits" + Self.query(...)
        return path_template(lit.group(1))
    return None


def collect_endpoints(ios: Path, types: dict[str, dict], all_nodes: dict[str, Node]) -> list[dict]:
    endpoints: list[dict] = []
    statics: dict[str, str] = {}
    sources = []
    for path in sorted(ios.glob(CLIENT_GLOB)):
        rel = str(path.relative_to(ios))
        clean, masked = preprocess(path.read_text(encoding="utf-8"))
        nodes = build_tree(masked, brace_matches(masked), rel)
        sources.append((rel, clean, masked, nodes))
        for nd in nodes:
            if nd.kind == "func" and nd.name.endswith("Path"):
                m = STRING_RE.search(clean[nd.open:nd.close])
                if m and m.group(1).startswith("/"):
                    statics[nd.name] = path_template(m.group(1))

    for rel, clean, masked, nodes in sources:
        for fn in (n for n in nodes if n.kind == "func" and n.parent and n.parent.qname.startswith("APIClient")):
            # Локальные функции (вложенные в другие функции) пропускаем: они сами не вызываются как методы.
            if fn.parent.kind == "func":
                continue
            body_masked = masked[fn.open:fn.close]
            body = clean[fn.open:fn.close]
            for cm in CALL_RE.finditer(body_masked):
                kind = cm.group(1)
                open_paren = fn.open + cm.end() - 1
                close_paren = balanced(masked, open_paren)
                inner = clean[open_paren + 1:close_paren]
                args = split_top(inner, ",")
                if len(args) < 2 or not re.match(r'\s*"(GET|POST|PUT|PATCH|DELETE)"', args[0]):
                    continue
                method = args[0].strip().strip('"')
                path = find_path_for(args[1], body, statics)
                # Тип ответа: аннотация `let x: T = try await request(` или тип результата функции.
                abs_start = fn.open + cm.start()
                prefix = clean[clean.rfind("\n", 0, abs_start) + 1:open_paren]
                prefix = re.sub(r"\b(request|requestOptional)\s*$", "", prefix)
                tm = LET_TYPE_RE.search(prefix + "")
                nullable = False
                swift_ret = None
                if kind in ("rawResponse", "rawData"):
                    dm = re.search(r"decoder\.decode\(\s*([\w.\[\]]+)\.self", body)
                    if not dm:
                        continue  # байты (CSV, фото) — не JSON-модель
                    swift_ret = dm.group(1)
                elif tm:
                    swift_ret = tm.group(1).strip()
                else:
                    swift_ret = fn.ret
                if kind == "requestOptional":
                    nullable = True
                if not swift_ret or swift_ret in ("Void", "()", "EmptyResponse", "APIClient.EmptyResponse"):
                    continue
                if "EmptyResponse" in swift_ret:
                    continue
                if swift_ret.startswith("(") or swift_ret == "Data":
                    continue  # кортеж/сырые байты — не модель
                t, opt = unwrap_optional(swift_ret)
                if opt:
                    nullable = True
                norm = normalize_type(t, lambda n, nd=fn: _resolve_scoped(nd, n, all_nodes))
                if path is None:
                    raise SystemExit(f"{rel}: {fn.qname}: не вытащил путь из вызова {kind}({inner.strip()[:80]}…) — добавь в MANUAL_ENDPOINTS")
                endpoints.append({
                    "swift": f"{fn.parent.name}.{fn.name}", "method": method, "path": path,
                    "root": norm, "nullable": nullable,
                })
    for manual in MANUAL_ENDPOINTS:
        endpoints.append(manual)
    seen, unique = set(), []
    for e in endpoints:
        k = (e["swift"], e["method"], e["path"], e["root"])
        if k not in seen:
            seen.add(k)
            unique.append(e)
    unique.sort(key=lambda e: (e["path"], e["method"], e["swift"]))
    return unique


def _resolve_scoped(scope_node: Node, name: str, all_nodes: dict[str, Node]) -> str:
    name = name.strip()
    if name in PRIMITIVES or name in ("Any", "AnyCodable", "JSONValue"):
        return name
    head, *tail = name.split(".")
    scope = scope_node.scope
    for depth in range(len(scope), -1, -1):
        cand = ".".join(scope[:depth] + [head])
        full = ".".join([cand] + tail)
        if full in all_nodes:
            return full
    return name


# --------------------------------------------------------------------------
# Сборка манифеста.
# --------------------------------------------------------------------------

def check_decoder(ios: Path) -> dict:
    client = (ios / "TrainingLog/Networking/APIClient.swift").read_text(encoding="utf-8")
    clean, _ = preprocess(client)
    key = re.search(r"decoder\.keyDecodingStrategy\s*=\s*\.(\w+)", clean)
    if not key or key.group(1) != "convertFromSnakeCase":
        raise SystemExit("APIClient.decoder больше не convertFromSnakeCase — переделай сопоставление ключей в тесте и скрипте")
    date = re.search(r"decoder\.dateDecodingStrategy\s*=\s*\.(\w+)", clean)
    return {"keys": key.group(1), "dates": date.group(1) if date else "deferredToDate"}


def build_manifest(ios: Path) -> dict:
    types, all_nodes, _ = collect_models(ios)
    endpoints = collect_endpoints(ios, types, all_nodes)
    # Каждый корневой тип обязан быть разобран.
    def named(t: str):
        t = t.rstrip("?")
        if t.startswith("["):
            inner = t[1:-1]
            kv = split_top(inner, ":")
            yield from named(kv[-1])
        elif t not in PRIMITIVES and t not in ("Any", "AnyCodable", "JSONValue"):
            yield t

    needed = set()
    for e in endpoints:
        needed.update(named(e["root"]))
    # Замыкание достижимости: в манифесте только то, что приходит из API (не кэши и не локальные снимки).
    reachable: set[str] = set()
    queue = sorted(needed)
    while queue:
        name = queue.pop()
        if name in reachable:
            continue
        reachable.add(name)
        tdef = types.get(name)
        if tdef is None:
            raise SystemExit(f"тип {name} не найден среди Decodable приложения")
        if "_error" in tdef:
            raise SystemExit(tdef["_error"])
        for f in tdef.get("fields", {}).values():
            queue.extend(named(f["type"]))
        queue.extend(tdef.get("flatten", []))
    types = {k: v for k, v in types.items() if k in reachable}
    return {
        "_about": "Снимок Swift-моделей приложения. Генерируется scripts/ios_contract.py — руками не править.",
        "decoder": check_decoder(ios),
        "types": dict(sorted(types.items())),
        "endpoints": endpoints,
    }


def ios_commit(ios: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(ios), "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        return out.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def without_provenance(manifest: dict) -> dict:
    return {k: v for k, v in manifest.items() if k != "_ios_commit"}


def render(manifest: dict) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ios", type=Path, default=DEFAULT_IOS, help="чекаут training_log_bot_ios")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="не писать; код 1, если манифест устарел")
    args = ap.parse_args(argv)
    if not (args.ios / "TrainingLog").is_dir():
        print(f"нет чекаута iOS: {args.ios} (передай --ios /путь/к/training_log_bot_ios)", file=sys.stderr)
        return 2
    manifest = build_manifest(args.ios)
    # Коммит iOS — справка, откуда снимок; в сравнение `--check` не входит (иначе любой
    # коммит приложения, не тронувший модели, делал бы снимок «устаревшим»).
    manifest["_ios_commit"] = ios_commit(args.ios)
    text = render(manifest)
    if args.check:
        try:
            current = json.loads(args.out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = None
        if current is None or render(without_provenance(current)) != render(without_provenance(manifest)):
            print(
                f"{args.out} устарел относительно {args.ios}. Обнови: "
                f"python scripts/ios_contract.py --ios {args.ios}",
                file=sys.stderr,
            )
            return 1
        print("манифест актуален")
        return 0
    args.out.write_text(text, encoding="utf-8")
    print(f"записан {args.out}: типов {len(manifest['types'])}, эндпоинтов {len(manifest['endpoints'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
