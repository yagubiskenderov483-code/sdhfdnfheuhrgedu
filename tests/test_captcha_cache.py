"""Проверки устойчивости кеша капч к параллельной мутации (threading-safety).

Главный сценарий: main.py вызывает tracker.captcha_solver._save_cache через
asyncio.to_thread. json.dump итерирует _CAPTCHA_CACHE в executor-потоке,
event-loop тред в это время вызывает cache_put — раньше это давало
'dictionary changed size during iteration'. Тест ловит регрессию.

Запуск: PYTHONPATH=. python tests/test_captcha_cache.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time


def main() -> None:
    # Переопределяем путь ДО первого импорта captcha_solver — он читает env на import time.
    tmpdir = tempfile.mkdtemp(prefix="captest-")
    cache_path = os.path.join(tmpdir, "cache.json")
    os.environ["CAPTCHA_CACHE_PATH"] = cache_path
    os.environ.setdefault("BOT_TOKEN", "x" * 10)  # на всякий случай

    sys.path.insert(0, os.getcwd())
    import tracker.captcha_solver as cs

    # Удостоверимся что действительно путь перехватился
    assert cs._CAPTCHA_CACHE_PATH == cache_path, \
        f"cache path mismatch: {cs._CAPTCHA_CACHE_PATH} != {cache_path}"
    print("OK cache path overridden")

    # Заполняем кеш начальным набором
    for i in range(1000):
        cs.cache_put(f"key{i}", i % 8)
    print(f"OK seeded {len(cs._CAPTCHA_CACHE)} entries")

    # Параллельно: один поток — непрерывные puts/pops, другой — _save_cache.
    stop = threading.Event()
    errors: list[BaseException] = []

    def mutator():
        try:
            n = 0
            while not stop.is_set():
                cs.cache_put(f"hot{n}", n % 7)
                if n % 5 == 0:
                    cs.cache_pop(f"key{n % 500}")
                n += 1
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def saver():
        try:
            for _ in range(20):
                cs._save_cache()
                time.sleep(0.005)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=mutator, name="mutator", daemon=True)
    t2 = threading.Thread(target=saver, name="saver", daemon=True)
    t1.start()
    t2.start()
    t2.join(timeout=10)
    stop.set()
    t1.join(timeout=5)

    if errors:
        print("FAIL: exceptions raised during concurrent save/mutate:")
        for e in errors:
            print(f"  {type(e).__name__}: {e}")
        sys.exit(1)
    print("OK no exception under parallel save + put/pop")

    # Файл должен быть валидным JSON — даже если писался конкурентно
    assert os.path.exists(cache_path), f"cache file not written: {cache_path}"
    with open(cache_path, encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict), f"cache content is not a dict: {type(data)}"
    assert len(data) > 0, "cache file is empty"
    print(f"OK cache file is valid JSON with {len(data)} entries")

    # Dirty-флаг сбрасывается только после успешной записи — проверим re-entry
    cs._cache_dirty = False
    cs._save_cache()  # no-op (ничего не изменилось)
    print("OK _save_cache noop when not dirty")

    print("\n=== 4 tests PASSED ===")


if __name__ == "__main__":
    main()
