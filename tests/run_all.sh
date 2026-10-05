#!/bin/sh
# Все проверки разом (без Telegram). Запуск из корня проекта:  sh tests/run_all.sh
set -e
export PYTHONPATH=.
python tests/test_tracker.py | tail -1
python tests/test_subscription.py | grep -c "OK$" | sed 's/^/subscription groups OK: /'
python tests/test_watchdog.py | tail -1
python tests/bot_audit.py | tail -1
