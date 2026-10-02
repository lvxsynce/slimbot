"""P3.1/P3.3 — ffmpeg/ffprobe не должны блокировать loop и не должны течь.

- P3.1: `probe_duration` — синхронный `subprocess.run`, вызываемый из async
  функции. Event loop вставал на всё время ffprobe (до 20 с), и в это время
  не мог обслуживать ни один Telethon-клиент.
- P3.3: три ffmpeg-хелпера ловили только `asyncio.TimeoutError`. При
  `CancelledError` (от внешнего `wait_for(..., 300)` в `outgoing_handler`)
  `proc.kill()` не вызывался — процесс оставался сиротой.
"""

import asyncio
import inspect

import pytest

from utils import gif_converter as G


# --------------------------------------------------------------------------
# P3.1 — probe_duration уходит в поток
# --------------------------------------------------------------------------

def test_probe_duration_async_exists():
    assert inspect.iscoroutinefunction(G.probe_duration_async)


def test_video_to_gif_uses_async_probe():
    src = inspect.getsource(G.video_to_gif_bytes)
    assert "await probe_duration_async" in src, "video_to_gif must await the async probe"
    assert "= probe_duration(" not in src, "blocking probe_duration must not be called directly"


def test_async_probe_does_not_block_the_loop(monkeypatch):
    """Пока идёт «ffprobe», loop должен продолжать крутиться."""
    ticks = []

    def slow_probe(path):
        import time as _t
        _t.sleep(0.25)
        return 3.0

    monkeypatch.setattr(G, "probe_duration", slow_probe)

    async def scenario():
        async def ticker():
            for _ in range(20):
                ticks.append(1)
                await asyncio.sleep(0.01)

        t = asyncio.create_task(ticker())
        duration = await G.probe_duration_async("/nonexistent.mp4")
        t.cancel()
        return duration

    duration = asyncio.run(scenario())
    assert duration == 3.0
    assert len(ticks) >= 5, f"loop was blocked during probe (ticks={len(ticks)})"


def test_probe_returns_none_on_error():
    assert G.probe_duration("/definitely/not/a/file.mp4") is None


def test_async_probe_returns_none_on_error():
    assert asyncio.run(G.probe_duration_async("/definitely/not/a/file.mp4")) is None


# --------------------------------------------------------------------------
# P3.3 — kill() на отмене
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "func",
    ["video_to_gif_bytes", "webm_to_rgba_frames", "image_audio_to_video_bytes"],
)
def test_ffmpeg_helpers_kill_on_cancellation(func):
    """Все три хелпера обязаны ловить CancelledError и убивать процесс.

    Проверяем именно структуру except-блока: `kill()` должен находиться
    ПОСЛЕ строки с `except (... CancelledError)`, а не до неё.
    """
    src = inspect.getsource(getattr(G, func))
    assert "CancelledError" in src, f"{func} does not handle CancelledError"
    idx_cancel = src.find("CancelledError")
    assert idx_cancel != -1
    # kill() после except-строки (в теле блока), ближайший следующий
    idx_kill = src.find("kill()", idx_cancel)
    assert idx_kill != -1, f"{func}: no kill() inside the CancelledError handler"


@pytest.mark.parametrize(
    "func",
    ["video_to_gif_bytes", "webm_to_rgba_frames", "image_audio_to_video_bytes"],
)
def test_kill_is_inside_the_cancelled_handler(func):
    """Строгая проверка: между `except (...CancelledError)` и следующей
    строкой `except`/`finally` обязан находиться `kill()`."""
    src = inspect.getsource(getattr(G, func))
    lines = src.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "except" in line and "CancelledError" in line:
            start = i
            break
    assert start is not None, f"{func}: no `except ... CancelledError`"
    body = []
    for line in lines[start + 1:]:
        if line.strip().startswith("except") or line.strip().startswith("finally"):
            break
        body.append(line)
    assert any("kill()" in line for line in body), (
        f"{func}: kill() must be inside the CancelledError handler, got:\n" + "\n".join(body)
    )


class FakeProc:
    """Минимальный subprocess-объект для проверки kill/communicate."""

    def __init__(self, hang=False):
        self.killed = False
        self.returncode = -9
        self._hang = hang

    async def communicate(self, *a, **kw):
        if self._hang:
            await asyncio.sleep(3600)
        return b"", b""

    def kill(self):
        self.killed = True

    async def wait(self):
        return -9


@pytest.fixture
def fake_proc(monkeypatch):
    proc = FakeProc(hang=True)

    async def spawn(*a, **kw):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return proc


def test_video_to_gif_kills_on_external_cancel(fake_proc, monkeypatch):
    """Отмена задачи снаружи (как outgoing_handler) обязана убить ffmpeg."""
    async def scenario():
        task = asyncio.create_task(G.video_to_gif_bytes(b"\x00" * 32, timeout_s=60))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert fake_proc.killed, "ffmpeg process leaked after cancellation"


def test_webm_kills_on_external_cancel(fake_proc):
    async def scenario():
        task = asyncio.create_task(G.webm_to_rgba_frames(b"\x1a\x45\xdf\xa3" * 20, timeout_s=60))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert fake_proc.killed, "ffmpeg process leaked after cancellation (webm)"


def test_image_audio_kills_on_external_cancel(fake_proc, monkeypatch):
    async def scenario():
        task = asyncio.create_task(
            G.image_audio_to_video_bytes(b"\x89PNG" + b"\x00" * 64, b"\x00" * 128, timeout_s=60)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert fake_proc.killed, "ffmpeg process leaked after cancellation (image_audio)"


def test_no_blocking_subprocess_in_async_helpers():
    """Статический инвариант: внутри async-функций не должно быть
    блокирующего subprocess.run (только create_subprocess_exec)."""
    for name, obj in vars(G).items():
        if not inspect.iscoroutinefunction(obj):
            continue
        src = inspect.getsource(obj)
        assert "subprocess.run(" not in src, f"{name} uses blocking subprocess.run"
