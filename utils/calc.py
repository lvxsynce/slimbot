import ast
import math
import operator

# Лимиты против CPU/memory DoS (`.calc 10**10**10` → 40GB bignum, OOM).
_MAX_EXPR_LEN = 512            # длина выражения
_MAX_INT_BITS = 4096           # ~1.2KB на число — достаточно для калькулятора
_MAX_POW_EXP = 1024            # 10**1024 ≈ 3.4KB; экспоненты больше — отказ
_MAX_SHIFT = 4096              # сдвиг влево больше этого — гигантский int
_MAX_FACTORIAL = 5000          # factorial(10**7) убивает процесс

_BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Invert: operator.invert}

_NAMES = {
    "pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf, "nan": math.nan,
}
_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow,
    "sqrt": math.sqrt, "log": math.log, "log2": math.log2, "log10": math.log10,
    "exp": math.exp, "ceil": math.ceil, "floor": math.floor, "trunc": math.trunc,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "degrees": math.degrees, "radians": math.radians,
    "gcd": math.gcd, "factorial": math.factorial,
    "hex": lambda x: hex(int(x)), "bin": lambda x: bin(int(x)), "oct": lambda x: oct(int(x)),
}


def _check_int(v, op_name: str):
    """Пост-проверка: результат любой операции не должен быть гигантским int."""
    if isinstance(v, int) and v.bit_length() > _MAX_INT_BITS:
        raise ValueError(f"{op_name}: результат слишком большой")


def _safe_binop(op, left, right):
    # Пре-проверки ДО вычисления: pow/lshift с огромными операндами аллоцируют
    # гигабайты ещё до того, как пост-проверка успеет сработать.
    if op is operator.pow and isinstance(right, int) and abs(right) > _MAX_POW_EXP:
        raise ValueError("pow: степень слишком большая")
    if op is operator.lshift and isinstance(right, int) and abs(right) > _MAX_SHIFT:
        raise ValueError("сдвиг: слишком большой")
    res = op(left, right)
    _check_int(res, "pow" if op is operator.pow else "сдвиг" if op is operator.lshift else "операция")
    return res


def _safe_func(name: str, args: list):
    if name == "factorial" and args:
        n = args[0]
        if isinstance(n, int) and abs(n) > _MAX_FACTORIAL:
            raise ValueError("factorial: слишком большое число")
    if name == "pow" and len(args) == 2:
        # Пре-проверка как у оператора **: pow(9, pow(9,9)) иначе начнёт
        # аллоцировать гигабайтный int ДО пост-проверки.
        exp = args[1]
        if isinstance(exp, int) and abs(exp) > _MAX_POW_EXP:
            raise ValueError("pow: степень слишком большая")
    res = _FUNCS[name](*args)
    _check_int(res, name)
    return res


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, complex)):
            return node.value
        raise ValueError("only numbers")
    if isinstance(node, ast.BinOp):
        op = _BIN.get(type(node.op))
        if not op:
            raise ValueError(f"op {type(node.op).__name__}")
        return _safe_binop(op, _eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _UNARY.get(type(node.op))
        if not op:
            raise ValueError(f"unary {type(node.op).__name__}")
        return op(_eval(node.operand))
    if isinstance(node, ast.Name):
        if node.id in _NAMES:
            return _NAMES[node.id]
        raise ValueError(f"name {node.id}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise ValueError("func not allowed")
        args = [_eval(a) for a in node.args]
        return _safe_func(node.func.id, args)
    if isinstance(node, ast.Tuple):
        return tuple(_eval(e) for e in node.elts)
    raise ValueError(f"node {type(node).__name__}")


def calc(expr: str) -> str:
    """Вычисляет выражение. При любой проблеме поднимает ValueError с понятным
    сообщением (caller'ы обязаны ловить Exception и показывать юзеру ошибку)."""
    cleaned = (expr or "").strip()
    if not cleaned:
        raise ValueError("пусто")
    if len(cleaned) > _MAX_EXPR_LEN:
        raise ValueError(f"выражение слишком длинное (> {_MAX_EXPR_LEN} символов)")
    try:
        tree = ast.parse(cleaned, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as e:
        raise ValueError(f"не удалось разобрать выражение ({type(e).__name__})")
    try:
        val = _eval(tree)
    except RecursionError:
        raise ValueError("слишком глубокое выражение")
    except (MemoryError, OverflowError):
        raise ValueError("результат слишком большой")
    if isinstance(val, float):
        if val.is_integer():
            return str(int(val))
        return f"{val:.10g}"
    return str(val)
