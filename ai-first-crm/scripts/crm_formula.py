#!/usr/bin/env python3
"""Safe formula language for CRM workflow CODE steps and formula fields.

Server CRMs run CODE steps as JavaScript in a sandbox. The skill has no
sandbox and never executes code, so a CODE step evaluates small expressions
written in a Python-shaped formula language instead:

    round(coalesce(amountMicros, 0) * coalesce(probability, 0) / 100)
    concat(firstName, " ", lastName)
    date_add_days(today(), -7)
    get(find(answers, "type", "email"), "email", "")

The expression is parsed with ``ast`` and checked against a whitelist before
anything is evaluated: arithmetic, comparisons, and/or/not, the conditional
expression, list and dict literals, indexing, and calls of the listed
functions. Attribute access, imports, comprehensions, lambdas, f-strings,
assignments and every other construct are refused with an explanation.
Evaluation runs under a step and time budget; texts, lists and numbers are
size-limited, so a formula cannot exhaust memory or loop.

Numbers are computed as Decimal so amounts stay exact (12500.50 * 40 / 100
is 5000.2, not 5000.200000000001). ``today()`` and ``now()`` read the moment
of the workflow run, never the wall clock, so a plan is reproducible.
"""

from __future__ import annotations

import ast
import json
import time
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext
from typing import Any, Callable, Optional

from crm_contract import DATE_RE, format_instant, parse_instant

MAX_SOURCE_CHARS = 2000
MAX_AST_NODES = 400
MAX_AST_DEPTH = 40
MAX_STEPS = 50_000
MAX_SECONDS = 0.5
MAX_TEXT = 100_000
MAX_ITEMS = 10_000
MAX_EXPONENT = 30  # numbers up to 10**30
MAX_POWER = 100
MAX_DAYS = 100_000

DECIMAL_CONTEXT = Context(prec=34, rounding=ROUND_HALF_UP, Emax=999, Emin=-999, traps=[InvalidOperation, DivisionByZero, Overflow])
LITERAL_NAMES = {"true": True, "false": False, "null": None, "True": True, "False": False, "None": None}

_ALLOWED = {
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
    ast.Mod, ast.Pow, ast.UnaryOp, ast.USub, ast.UAdd, ast.Not, ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
    ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.IfExp, ast.Call, ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple,
    ast.Dict, ast.Subscript, ast.Slice,
}
if hasattr(ast, "Index"):  # Python 3.8 wraps subscripts; 3.9+ no longer emits it
    _ALLOWED.add(getattr(ast, "Index"))
_REFUSED = {
    "Attribute": "attribute access (a.b) is not allowed; use get(value, \"path\")",
    "Lambda": "lambda is not allowed",
    "ListComp": "comprehensions are not allowed",
    "SetComp": "comprehensions are not allowed",
    "DictComp": "comprehensions are not allowed",
    "GeneratorExp": "comprehensions are not allowed",
    "JoinedStr": "f-strings are not allowed; use concat() or +",
    "FormattedValue": "f-strings are not allowed; use concat() or +",
    "NamedExpr": "assignments are not allowed",
    "Starred": "* unpacking is not allowed",
    "Set": "set literals are not allowed; use a list",
    "Await": "await is not allowed",
    "Yield": "yield is not allowed",
    "YieldFrom": "yield is not allowed",
    "BitAnd": "bitwise operators are not allowed",
    "BitOr": "bitwise operators are not allowed; use or",
    "BitXor": "bitwise operators are not allowed",
    "LShift": "bitwise operators are not allowed",
    "RShift": "bitwise operators are not allowed",
    "Invert": "bitwise operators are not allowed; use not",
    "MatMult": "the @ operator is not allowed",
    "Is": "is / is not are not allowed; use == or !=",
    "IsNot": "is / is not are not allowed; use == or !=",
}


class FormulaError(ValueError):
    """A formula is not allowed or cannot be evaluated."""


# ---------------------------------------------------------------------------
# compilation


def compile_formula(source: Any) -> ast.Expression:
    """Parse and whitelist-check one expression; raise FormulaError with the reason."""
    if not isinstance(source, str) or not source.strip():
        raise FormulaError("a formula must be a non-empty text")
    if len(source) > MAX_SOURCE_CHARS:
        raise FormulaError(f"a formula may have at most {MAX_SOURCE_CHARS} characters")
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"syntax error at column {exc.offset or 0}: {exc.msg}") from exc
    except (RecursionError, MemoryError, ValueError) as exc:
        raise FormulaError("the formula is nested too deeply") from exc
    count = 0

    def walk(node: ast.AST, depth: int) -> None:
        nonlocal count
        count += 1
        if count > MAX_AST_NODES:
            raise FormulaError(f"the formula is too long (more than {MAX_AST_NODES} parts)")
        if depth > MAX_AST_DEPTH:
            raise FormulaError("the formula is nested too deeply")
        kind = type(node).__name__
        if type(node) not in _ALLOWED:
            raise FormulaError(_REFUSED.get(kind, f"{kind} is not allowed in a formula"))
        if isinstance(node, ast.Constant) and not (node.value is None or isinstance(node.value, (bool, int, float, str))):
            raise FormulaError("only numbers, texts, true, false and null are allowed as literals")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise FormulaError("only the listed functions can be called, by name")
            if node.func.id not in FUNCTIONS:
                raise FormulaError(f"unknown function {node.func.id}(); allowed: {', '.join(sorted(FUNCTIONS))}")
            if node.keywords:
                raise FormulaError("keyword arguments are not allowed")
        if isinstance(node, ast.Slice) and node.step is not None:
            raise FormulaError("slices with a step are not allowed")
        for child in ast.iter_child_nodes(node):
            walk(child, depth + 1)

    walk(tree, 0)
    return tree


def formula_names(tree: ast.Expression) -> set[str]:
    """Variable names a compiled formula reads (function names excluded)."""
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    return {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and id(node) not in called and node.id not in LITERAL_NAMES
    }


def check_formula(source: Any, known_names: Optional[set[str]] = None) -> list[str]:
    """Problems of one formula; with known_names, unknown variables are reported too."""
    try:
        tree = compile_formula(source)
    except FormulaError as exc:
        return [str(exc)]
    if known_names is None:
        return []
    unknown = sorted(formula_names(tree) - set(known_names))
    return [f"unknown name {name}" for name in unknown]


# ---------------------------------------------------------------------------
# value conversion


def from_json_value(value: Any) -> Any:
    """JSON value -> formula value: numbers become Decimal, containers are converted deeply."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (list, tuple)):
        return [from_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): from_json_value(item) for key, item in value.items()}
    return str(value)


def to_json_value(value: Any) -> Any:
    """Formula value -> JSON value: integral Decimals become int, others float when exact, else text."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise FormulaError("the result is not a finite number")
        if value == value.to_integral_value():
            return int(value)
        as_float = float(value)
        if Decimal(repr(as_float)) == value:
            return as_float
        return format(value.normalize(), "f")
    if isinstance(value, (list, tuple)):
        return [to_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_json_value(item) for key, item in value.items()}
    if isinstance(value, float):
        return value
    return value


def _is_number(value: Any) -> bool:
    return isinstance(value, (Decimal, int, float)) and not isinstance(value, bool)


def _number(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise FormulaError("true/false is not a number; use if_(flag, 1, 0)")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if value is None:
        raise FormulaError("a value is missing (null) in a calculation; use coalesce(value, 0)")
    raise FormulaError(f"{type_name(value)} is not a number; use number(value)")


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true/false"
    if _is_number(value):
        return "a number"
    if isinstance(value, str):
        return "a text"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "an object"
    return type(value).__name__


def text(value: Any) -> str:
    """The text form of a value as concat() and + with texts use it."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if _is_number(value):
        number = _number(value)
        if number == number.to_integral_value():
            return str(int(number))
        return format(number.normalize(), "f")
    if isinstance(value, str):
        return value
    return json.dumps(to_json_value(value), ensure_ascii=False, sort_keys=True)


def _as_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if DATE_RE.fullmatch(stripped):
            try:
                return date.fromisoformat(stripped)
            except ValueError as exc:
                raise FormulaError(f"{value!r} is not a valid date") from exc
        moment = parse_instant(stripped)
        if moment is not None:
            return moment.date()
    raise FormulaError(f"{text(value)!r} is not a date (expected YYYY-MM-DD or an ISO instant)")


def _equal(left: Any, right: Any) -> bool:
    if _is_number(left) and _is_number(right):
        return _number(left) == _number(right)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_equal(left[key], right[key]) for key in left)
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return type(left) is type(right) and left == right


# ---------------------------------------------------------------------------
# evaluation


class _Evaluator:
    def __init__(self, variables: dict[str, Any], now: datetime):
        self.variables = variables
        self.now = now
        self.steps = 0
        self.deadline = time.monotonic() + MAX_SECONDS

    def tick(self) -> None:
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise FormulaError("the formula needs too many steps")
        if self.steps % 64 == 0 and time.monotonic() > self.deadline:
            raise FormulaError("the formula took too long")

    def check_size(self, value: Any) -> Any:
        if isinstance(value, str) and len(value) > MAX_TEXT:
            raise FormulaError(f"a text in the formula would exceed {MAX_TEXT} characters")
        if isinstance(value, (list, dict)) and len(value) > MAX_ITEMS:
            raise FormulaError(f"a list in the formula would exceed {MAX_ITEMS} items")
        if isinstance(value, Decimal) and value.is_finite() and value != 0 and value.adjusted() > MAX_EXPONENT:
            raise FormulaError("a number in the formula is too large")
        return value

    def eval(self, node: ast.AST) -> Any:
        self.tick()
        method = getattr(self, "eval_" + type(node).__name__, None)
        if method is None:
            raise FormulaError(f"{type(node).__name__} is not allowed in a formula")
        return self.check_size(method(node))

    def eval_Expression(self, node: ast.Expression) -> Any:
        return self.eval(node.body)

    def eval_Constant(self, node: ast.Constant) -> Any:
        return from_json_value(node.value)

    def eval_Name(self, node: ast.Name) -> Any:
        if node.id in self.variables:
            return self.variables[node.id]
        if node.id in LITERAL_NAMES:
            return LITERAL_NAMES[node.id]
        if node.id in FUNCTIONS:
            raise FormulaError(f"{node.id} is a function; call it as {node.id}(...)")
        raise FormulaError(f"unknown name {node.id}")

    def eval_List(self, node: ast.List) -> Any:
        return [self.eval(item) for item in node.elts]

    def eval_Tuple(self, node: ast.Tuple) -> Any:
        return [self.eval(item) for item in node.elts]

    def eval_Dict(self, node: ast.Dict) -> Any:
        result: dict[str, Any] = {}
        for key_node, value_node in zip(node.keys, node.values):
            if key_node is None:
                raise FormulaError("** unpacking is not allowed")
            key = self.eval(key_node)
            if not isinstance(key, str):
                raise FormulaError("object keys must be texts")
            result[key] = self.eval(value_node)
        return result

    def eval_BoolOp(self, node: ast.BoolOp) -> Any:
        value: Any = None
        for item in node.values:
            value = self.eval(item)
            if isinstance(node.op, ast.And) and not value:
                return value
            if isinstance(node.op, ast.Or) and value:
                return value
        return value

    def eval_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.eval(node.operand)
        if isinstance(node.op, ast.Not):
            return not operand
        number = _number(operand)
        return -number if isinstance(node.op, ast.USub) else +number

    def eval_IfExp(self, node: ast.IfExp) -> Any:
        return self.eval(node.body) if self.eval(node.test) else self.eval(node.orelse)

    def eval_BinOp(self, node: ast.BinOp) -> Any:
        left = self.eval(node.left)
        right = self.eval(node.right)
        op = node.op
        if isinstance(op, ast.Add):
            if isinstance(left, str) or isinstance(right, str):
                if len(text(left)) + len(text(right)) > MAX_TEXT:
                    raise FormulaError(f"a text in the formula would exceed {MAX_TEXT} characters")
                return text(left) + text(right)
            if isinstance(left, list) and isinstance(right, list):
                if len(left) + len(right) > MAX_ITEMS:
                    raise FormulaError(f"a list in the formula would exceed {MAX_ITEMS} items")
                return left + right
        if isinstance(op, ast.Mult) and (isinstance(left, (str, list)) or isinstance(right, (str, list))):
            sequence, count = (left, right) if isinstance(left, (str, list)) else (right, left)
            times = _number(count)
            if times != times.to_integral_value() or times < 0:
                raise FormulaError("a text or list can only be repeated a whole, non-negative number of times")
            limit = MAX_TEXT if isinstance(sequence, str) else MAX_ITEMS
            if len(sequence) * int(times) > limit:
                raise FormulaError("the repeated text or list would be too large")
            return sequence * int(times)
        a, b = _number(left), _number(right)
        with localcontext(DECIMAL_CONTEXT):
            try:
                if isinstance(op, ast.Add):
                    return a + b
                if isinstance(op, ast.Sub):
                    return a - b
                if isinstance(op, ast.Mult):
                    return a * b
                if isinstance(op, ast.Div):
                    return a / b
                if isinstance(op, ast.FloorDiv):
                    return (a / b).to_integral_value(rounding=ROUND_FLOOR)
                if isinstance(op, ast.Mod):
                    return a - b * (a / b).to_integral_value(rounding=ROUND_FLOOR)
                if isinstance(op, ast.Pow):
                    if b != b.to_integral_value() or abs(b) > MAX_POWER:
                        raise FormulaError(f"the exponent must be a whole number between -{MAX_POWER} and {MAX_POWER}")
                    if a != 0 and a.adjusted() * abs(int(b)) > MAX_EXPONENT * 2:
                        raise FormulaError("a number in the formula is too large")
                    return a ** int(b)
            except DivisionByZero as exc:
                raise FormulaError("division by zero") from exc
            except (InvalidOperation, Overflow) as exc:
                raise FormulaError("the calculation is not defined (for example 0/0) or too large") from exc
        raise FormulaError(f"{type(op).__name__} is not allowed")

    def eval_Compare(self, node: ast.Compare) -> Any:
        left = self.eval(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self.eval(comparator)
            if not self.compare(op, left, right):
                return False
            left = right
        return True

    def compare(self, op: ast.cmpop, left: Any, right: Any) -> bool:
        if isinstance(op, ast.Eq):
            return _equal(left, right)
        if isinstance(op, ast.NotEq):
            return not _equal(left, right)
        if isinstance(op, (ast.In, ast.NotIn)):
            found = _contains(right, left)
            return found if isinstance(op, ast.In) else not found
        if _is_number(left) and _is_number(right):
            a, b = _number(left), _number(right)
        elif isinstance(left, str) and isinstance(right, str):
            a, b = left, right
        else:
            raise FormulaError(f"cannot compare {type_name(left)} with {type_name(right)}")
        if isinstance(op, ast.Lt):
            return a < b
        if isinstance(op, ast.LtE):
            return a <= b
        if isinstance(op, ast.Gt):
            return a > b
        return a >= b

    def eval_Subscript(self, node: ast.Subscript) -> Any:
        container = self.eval(node.value)
        index_node = node.slice
        if hasattr(ast, "Index") and isinstance(index_node, getattr(ast, "Index")):
            index_node = index_node.value  # type: ignore[attr-defined]
        if isinstance(index_node, ast.Slice):
            if not isinstance(container, (str, list)):
                return None
            lower = None if index_node.lower is None else self.integer(self.eval(index_node.lower))
            upper = None if index_node.upper is None else self.integer(self.eval(index_node.upper))
            return container[lower:upper]
        key = self.eval(index_node)
        if isinstance(container, dict):
            return container.get(text(key)) if not isinstance(key, str) else container.get(key)
        if isinstance(container, (list, str)):
            if isinstance(key, str) and key.lstrip("-").isdigit():
                key = Decimal(key)
            position = self.integer(key)
            if -len(container) <= position < len(container):
                return container[position]
            return None
        return None

    def integer(self, value: Any) -> int:
        number = _number(value)
        if number != number.to_integral_value():
            raise FormulaError("an index must be a whole number")
        return int(number)

    def eval_Call(self, node: ast.Call) -> Any:
        name = node.func.id  # type: ignore[attr-defined]
        function, minimum, maximum = FUNCTIONS[name]
        if len(node.args) < minimum or (maximum is not None and len(node.args) > maximum):
            expected = str(minimum) if minimum == maximum else (f"{minimum} to {maximum}" if maximum is not None else f"at least {minimum}")
            raise FormulaError(f"{name}() takes {expected} arguments, not {len(node.args)}")
        arguments = [self.eval(argument) for argument in node.args]
        return function(self, *arguments)


def _contains(container: Any, item: Any) -> bool:
    if container is None:
        return False
    if isinstance(container, str):
        return text(item) in container
    if isinstance(container, list):
        return any(_equal(element, item) for element in container)
    if isinstance(container, dict):
        return isinstance(item, str) and item in container
    raise FormulaError(f"cannot look inside {type_name(container)}")


# ---------------------------------------------------------------------------
# functions: (callable, minimum arguments, maximum arguments or None)


def _round(ev: _Evaluator, value: Any, places: Any = Decimal(0)) -> Any:
    if value is None:
        return None
    digits = ev.integer(places)
    if not -12 <= digits <= 12:
        raise FormulaError("round() keeps between -12 and 12 decimal places")
    with localcontext(DECIMAL_CONTEXT):
        return _number(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def _values(arguments: tuple) -> list:
    if len(arguments) == 1 and isinstance(arguments[0], list):
        return list(arguments[0])
    return list(arguments)


def _extreme(pick: Callable, arguments: tuple) -> Any:
    values = [value for value in _values(arguments) if value is not None]
    if not values:
        return None
    if all(_is_number(value) for value in values):
        return pick(_number(value) for value in values)
    if all(isinstance(value, str) for value in values):
        return pick(values)
    raise FormulaError("min() and max() compare numbers with numbers or texts with texts")


def _len(ev: _Evaluator, value: Any) -> Any:
    if value is None:
        return Decimal(0)
    if isinstance(value, (str, list, dict)):
        return Decimal(len(value))
    raise FormulaError(f"len() needs a text, list or object, not {type_name(value)}")


def _string_function(transform: Callable[[str], str]) -> Callable:
    def function(ev: _Evaluator, value: Any) -> Any:
        if value is None:
            return None
        return transform(text(value))
    return function


def _number_function(ev: _Evaluator, value: Any) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return Decimal(1 if value else 0)
    if _is_number(value):
        return _number(value)
    if isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation as exc:
            raise FormulaError(f"{value!r} is not a number (use a dot as decimal separator)") from exc
        if not number.is_finite():
            raise FormulaError(f"{value!r} is not a finite number")
        return number
    raise FormulaError(f"number() cannot convert {type_name(value)}")


def _coalesce(ev: _Evaluator, *values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _date_add_days(ev: _Evaluator, value: Any, days: Any) -> Any:
    if value is None or value == "":
        return None
    count = ev.integer(days)
    if abs(count) > MAX_DAYS:
        raise FormulaError(f"date_add_days() moves at most {MAX_DAYS} days")
    if isinstance(value, str) and DATE_RE.fullmatch(value.strip()):
        return (_as_date(value) + timedelta(days=count)).isoformat()  # type: ignore[operator]
    moment = parse_instant(value) if isinstance(value, str) else None
    if moment is None:
        raise FormulaError(f"{text(value)!r} is not a date (expected YYYY-MM-DD or an ISO instant)")
    return format_instant(moment + timedelta(days=count))


def _days_between(ev: _Evaluator, start: Any, end: Any) -> Any:
    first, second = _as_date(start), _as_date(end)
    if first is None or second is None:
        return None
    return Decimal((second - first).days)


def _get(ev: _Evaluator, value: Any, path: Any, default: Any = None) -> Any:
    if isinstance(path, list):
        parts = [text(part) for part in path]
    elif _is_number(path):
        parts = [text(path)]
    elif isinstance(path, str):
        parts = [part for part in path.split(".") if part != ""]
    else:
        raise FormulaError("get() needs a path such as \"0.text\" or a list of keys")
    current = value
    for part in parts:
        ev.tick()
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.lstrip("-").isdigit():
            position = int(part)
            current = current[position] if -len(current) <= position < len(current) else None
        else:
            current = None
        if current is None:
            return default
    return current


def _find(ev: _Evaluator, items: Any, key: Any, wanted: Any) -> Any:
    if items is None:
        return None
    if not isinstance(items, list):
        raise FormulaError("find() needs a list")
    for item in items:
        ev.tick()
        if isinstance(item, dict) and _equal(item.get(text(key)), wanted):
            return item
    return None


def _split(ev: _Evaluator, value: Any, separator: Any = " ") -> Any:
    if value is None:
        return []
    sep = text(separator)
    if not sep:
        raise FormulaError("split() needs a non-empty separator")
    parts = text(value).split(sep)
    if len(parts) > MAX_ITEMS:
        raise FormulaError(f"split() would create more than {MAX_ITEMS} parts")
    return parts


def _join(ev: _Evaluator, items: Any, separator: Any = "") -> Any:
    if items is None:
        return ""
    if not isinstance(items, list):
        raise FormulaError("join() needs a list")
    return text(separator).join(text(item) for item in items if item is not None)


def _parse_json(ev: _Evaluator, value: Any) -> Any:
    if isinstance(value, (list, dict)) or value is None:
        return value
    if not isinstance(value, str):
        raise FormulaError("parse_json() needs a text")
    if len(value) > MAX_TEXT:
        raise FormulaError(f"parse_json() reads at most {MAX_TEXT} characters")
    try:
        return from_json_value(json.loads(value))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise FormulaError(f"parse_json(): not valid JSON ({exc})") from exc


def _contains_function(ev: _Evaluator, container: Any, item: Any) -> bool:
    return _contains(container, item)


def _sum(ev: _Evaluator, *arguments: Any) -> Any:
    total = Decimal(0)
    with localcontext(DECIMAL_CONTEXT):
        for value in _values(arguments):
            if value is not None:
                total += _number(value)
    return total


def _rounding(mode: str) -> Callable:
    def function(ev: _Evaluator, value: Any) -> Any:
        if value is None:
            return None
        return _number(value).to_integral_value(rounding=mode)
    return function


FUNCTIONS: dict[str, tuple[Callable, int, Optional[int]]] = {
    "round": (_round, 1, 2),
    "abs": (lambda ev, value: None if value is None else abs(_number(value)), 1, 1),
    "min": (lambda ev, *values: _extreme(min, values), 1, None),
    "max": (lambda ev, *values: _extreme(max, values), 1, None),
    "len": (_len, 1, 1),
    "lower": (_string_function(str.lower), 1, 1),
    "upper": (_string_function(str.upper), 1, 1),
    "trim": (_string_function(str.strip), 1, 1),
    "concat": (lambda ev, *values: "".join(text(value) for value in values), 0, None),
    "if_": (lambda ev, condition, when_true, when_false=None: when_true if condition else when_false, 2, 3),
    "coalesce": (_coalesce, 1, None),
    "date_add_days": (_date_add_days, 2, 2),
    "days_between": (_days_between, 2, 2),
    "today": (lambda ev: ev.now.astimezone(timezone.utc).date().isoformat(), 0, 0),
    "now": (lambda ev: format_instant(ev.now), 0, 0),
    "number": (_number_function, 1, 1),
    "text": (lambda ev, value: text(value), 1, 1),
    "contains": (_contains_function, 2, 2),
    "get": (_get, 2, 3),
    "find": (_find, 3, 3),
    "split": (_split, 1, 2),
    "join": (_join, 1, 2),
    "parse_json": (_parse_json, 1, 1),
    "floor": (_rounding(ROUND_FLOOR), 1, 1),
    "ceil": (_rounding(ROUND_CEILING), 1, 1),
    "sum": (_sum, 1, None),
}


def evaluate(formula: Any, variables: Optional[dict[str, Any]] = None, *, now: Any = None) -> Any:
    """Evaluate one formula with JSON variables; return a JSON value or raise FormulaError.

    ``now`` is the moment of the run (datetime or ISO instant); today() and now()
    read it. Without it the current UTC time is used.
    """
    tree = formula if isinstance(formula, ast.Expression) else compile_formula(formula)
    if now is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    else:
        moment = parse_instant(now)
        if moment is None:
            raise FormulaError(f"invalid moment {now!r}")
    converted = {str(key): from_json_value(value) for key, value in (variables or {}).items()}
    evaluator = _Evaluator(converted, moment)
    try:
        result = evaluator.eval(tree)
    except RecursionError as exc:
        raise FormulaError("the formula is nested too deeply") from exc
    return to_json_value(result)


def evaluate_many(formulas: dict[str, Any], variables: Optional[dict[str, Any]] = None, *, now: Any = None) -> dict[str, Any]:
    """Evaluate named formulas in order; a later formula may read an earlier result by name.

    A dotted name such as ``contact.email`` nests the result ({"contact": {"email": ...}})
    and cannot be read by later formulas.
    """
    scope = dict(variables or {})
    output: dict[str, Any] = {}
    for name, source in formulas.items():
        try:
            value = evaluate(source, scope, now=now)
        except FormulaError as exc:
            raise FormulaError(f"formula {name}: {exc}") from exc
        parts = name.split(".")
        target = output
        for part in parts[:-1]:
            existing = target.get(part)
            if not isinstance(existing, dict):
                existing = {}
                target[part] = existing
            target = existing
        target[parts[-1]] = value
        if len(parts) == 1:
            scope[name] = value
    return output
