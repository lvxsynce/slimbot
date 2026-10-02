"""P3.2 — CPU-bound работа в `.q` не должна блокировать event loop.

Один `.q` держит в RAM до 40 RGBA-кадров (bot.log: 188 КБ входа → 7.4 МБ
на выходе). rlottie и Pillow считались прямо в корутине, то есть на всё
время декодирования ни один Telethon-клиент в процессе не мог прочитать
сокет — то есть не отвечал ни одному пользователю.
"""

import asyncio
import ast
import inspect
import pathlib

import pytest

from handlers.commands import quote

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Синхронные CPU-bound функции, которые обязаны уходить в поток.
CPU_FUNCS = ("tgs_to_rgba_frames", "frames_to_gif_bytes",
             "rgba_frame_to_png_bytes", "_pillow_first_frame")


def _async_calls_in_function(name: str):
    """Множество имён, вызываемых через await, внутри функции `name`."""
    tree = ast.parse(inspect.getsource(quote))
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != name:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Await) and isinstance(sub.value, ast.Call):
                fn = sub.value.func
                if isinstance(fn, ast.Name):
                    out.add(fn.id)
                elif isinstance(fn, ast.Attribute):
                    out.add(fn.attr)
    return out


def test_to_thread_helper_exists():
    assert inspect.iscoroutinefunction(quote._to_thread)


def test_to_thread_does_not_block_the_loop():
    """Пока «идёт Pillow», тикер event loop продолжает крутиться."""
    import time

    ticks = []

    def slow(*args):
        time.sleep(0.25)
        return "done"

    async def scenario():
        async def ticker():
            for _ in range(40):
                ticks.append(1)
                await asyncio.sleep(0.01)

        t = asyncio.create_task(ticker())
        result = await quote._to_thread(slow, b"x")
        t.cancel()
        return result

    assert asyncio.run(scenario()) == "done"
    assert len(ticks) >= 5, f"loop stalled during CPU work (ticks={len(ticks)})"


@pytest.mark.parametrize("fn", CPU_FUNCS)
def test_cpu_functions_are_imported_somewhere_in_quote(fn):
    src = inspect.getsource(quote)
    assert fn in src, f"{fn} больше не используется в quote.py — проверь мусор"


def test_no_bare_cpu_call_in_async_code():
    """Статический инвариант: CPU-функции нельзя вызывать без `await _to_thread`."""
    tree = ast.parse((ROOT / "handlers" / "commands" / "quote.py").read_text(encoding="utf-8"))

    async_defs = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            awaited = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Await) and isinstance(sub.value, ast.Call):
                    fn = sub.value.func
                    awaited.add(fn.id if isinstance(fn, ast.Name)
                                else getattr(fn, "attr", None))
            # имена, определённые в этой же функции (локальные импорты)
            local = {
                t.id for n in ast.walk(node) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)
            }
            local |= {
                a.asname or a.name
                for n in ast.walk(node) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in n.names
            }
            async_defs[node.name] = awaited | local

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        allowed = async_defs.get(node.name, set())
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            fn = sub.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name in CPU_FUNCS and name not in allowed:
                offenders.append(f"quote.py:{sub.lineno} {node.name}(): {name}() без await")
    assert not offenders, "CPU-bound вне потока:\n" + "\n".join(offenders)


def test_legacy_sync_helper_stays_sync():
    """`_decode_emoji_frame` — синхронный legacy для тестов, он не в потоке."""
    assert not inspect.iscoroutinefunction(quote._decode_emoji_frame)
    tree = ast.parse(inspect.getsource(quote._decode_emoji_frame))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            assert name != "_to_thread", "legacy-хелпер не должен уходить в поток"


def test_decode_emoji_frame_still_works():
    """Регрессия: правка не должна была сломать сам helper."""
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(buf, format="PNG")
    frame = quote._decode_emoji_frame(buf.getvalue(), "image/png")
    assert frame is not None
