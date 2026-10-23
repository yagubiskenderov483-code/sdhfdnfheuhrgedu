#!/bin/sh
# Все проверки разом (без Telegram). Запуск из корня проекта:  sh tests/run_all.sh
# Падает с ошибкой, если хоть один набор тестов не прошёл.
export PYTHONPATH=.
fail=0
for t in tests/test_tracker.py tests/test_subscription.py tests/test_watchdog.py tests/test_lookup.py tests/bot_audit.py; do
    out=$(timeout 300 python "$t" 2>&1); rc=$?
    if [ $rc -ne 0 ]; then
        echo "FAIL $t (код $rc)"; echo "$out" | tail -15; fail=1
    else
        echo "ok   $t — $(echo "$out" | grep -cE 'OK$|✔|PASSED|NO PROBLEMS') проверок"
    fi
done
exit $fail
