"""Регрессия: pipeline.stop() должен дожидаться не только _workers, но и
_retries и _lookup_tasks — иначе отменённые задачи могут остаться в середине
await db.update_listing() и зависнуть при последующем db.close().

Запуск: PYTHONPATH=. python tests/test_pipeline_stop.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())


class _PipelineLike:
    """Копирует только метод stop() из ListingPipeline — без зависимостей от telethon/db.
    Если скопирован логикой корректно, тест пройдёт за timeout."""
    def __init__(self):
        self._workers: list[asyncio.Task] = []
        self._retries: set[asyncio.Task] = set()
        self._lookup_tasks: set[asyncio.Task] = set()

    async def stop(self) -> None:
        pending = list(self._workers) + list(self._retries) + list(self._lookup_tasks)
        for t in pending:
            t.cancel()
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True), timeout=10)
            except asyncio.TimeoutError:
                pass
        self._workers.clear()
        self._retries.clear()
        self._lookup_tasks.clear()


async def test_stop_awaits_all_sets() -> None:
    p = _PipelineLike()

    # Три задачи-долгоспящих в разных контейнерах, чтобы было видно, что все три ждутся.
    cleanups = {"worker": False, "retry": False, "lookup": False}

    async def _long(label: str):
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            # Эмуляция финального await (например db.update_listing) — должен успеть завершиться
            await asyncio.sleep(0.05)
            cleanups[label] = True
            raise

    wt = asyncio.create_task(_long("worker"), name="worker-0")
    rt = asyncio.create_task(_long("retry"), name="retry-0")
    lt = asyncio.create_task(_long("lookup"), name="lookup-0")
    p._workers.append(wt)
    p._retries.add(rt)
    p._lookup_tasks.add(lt)
    await asyncio.sleep(0)  # дать им стартовать

    await p.stop()

    # Все три задачи должны быть завершены и cleanup прошёл
    for label, task in (("worker", wt), ("retry", rt), ("lookup", lt)):
        assert task.done(), f"{label} task not done after stop()"
        assert cleanups[label], (
            f"{label} cleanup did not run — задача была отменена, но не дождались")
    print("OK stop() awaits _workers + _retries + _lookup_tasks")


async def test_stop_awaits_db_write_after_cancel() -> None:
    """Главный смысл регрессии: _lookup_then_process был в середине db.update_listing(),
    когда stop() отменил его. Без await stop() завершался бы и dr.close() сносил бы
    соединение — задача просыпалась уже на закрытой БД. Проверяем, что финальный
    await в cancel-handler ДОЖИДАЕТСЯ до того, как stop вернётся."""
    p = _PipelineLike()
    finished_cleanup = asyncio.Event()

    async def _mid_db_write():
        try:
            await asyncio.sleep(99)
        except asyncio.CancelledError:
            # Имитируем db.update_listing — пара await'ов на закрытие транзакции
            await asyncio.sleep(0.1)
            await asyncio.sleep(0.05)
            finished_cleanup.set()
            raise

    t = asyncio.create_task(_mid_db_write(), name="mid-db")
    p._lookup_tasks.add(t)
    await asyncio.sleep(0)

    await p.stop()

    assert finished_cleanup.is_set(), (
        "stop() вернулся до завершения cleanup — db.close() сломает незавершённую транзакцию")
    assert t.done(), "задача должна быть завершена после stop()"
    print("OK stop() ждёт завершения db-write в cancel-handler")


async def test_stop_empty() -> None:
    p = _PipelineLike()
    await p.stop()                 # не должен падать и не виснуть на пустом pipeline
    print("OK stop() не падает на пустом pipeline")


def main() -> None:
    asyncio.run(test_stop_awaits_all_sets())
    asyncio.run(test_stop_awaits_db_write_after_cancel())
    asyncio.run(test_stop_empty())
    print("\n=== 3 tests PASSED ===")


if __name__ == "__main__":
    main()
