"""Тесты валидаторов настроек в config.py: _bot_list, _lookup_bot_username, _bot_cmds.
Защищают от регрессий в парсинге env.

Запуск: PYTHONPATH=. python tests/test_config_validators.py
"""
import os
import sys

sys.path.insert(0, os.getcwd())

ok_count = 0


def check(cond, what):
    global ok_count
    if not cond:
        print("FAIL:", what)
        sys.exit(1)
    ok_count += 1
    print("OK", what)


def test_bot_list():
    from config import _bot_list
    check(_bot_list("funstat") == ["funstat"], "одно имя")
    check(_bot_list("@funstat") == ["funstat"], "с @")
    check(_bot_list("funstat, infostat") == ["funstat", "infostat"], "два через запятую с пробелом")
    check(_bot_list("funstat,funstat") == ["funstat"], "дубликаты убираются")
    check(_bot_list("FUNSTAT,funstat") == ["FUNSTAT"], "case-insensitive dedup сохраняет первый")
    check(_bot_list("funstat;infostat") == ["funstat", "infostat"], "точка с запятой как разделитель")
    check(_bot_list("") == [], "пустой")
    check(_bot_list(None) == [], "None")
    check(_bot_list("  ") == [], "whitespace")
    # Валидация
    check(_bot_list("abc") == [], "слишком короткий (3 символа)")
    check(_bot_list("1funstat") == [], "начинается с цифры")
    check(_bot_list("fun stat") == [], "с пробелом")
    check(_bot_list("fun-stat") == [], "с дефисом")
    check(_bot_list("funstat_bot") == ["funstat_bot"], "с подчёркиванием OK")
    check(_bot_list("a" * 33) == [], "слишком длинный")
    check(_bot_list("funstat,bad name,infostat") == ["funstat", "infostat"], "валидные берём, невалидные пропускаем")


def test_lookup_bot_username():
    from config import _lookup_bot_username
    check(_lookup_bot_username("funstat") == "funstat", "одно имя")
    check(_lookup_bot_username("@funstat") == "funstat", "с @")
    check(_lookup_bot_username("") == "", "пусто")
    check(_lookup_bot_username(None) == "", "None")
    check(_lookup_bot_username("fun stat") == "", "с пробелом невалиден")
    check(_lookup_bot_username("1funstat") == "", "с цифры невалиден")
    check(_lookup_bot_username("abc") == "", "короткий невалиден")
    check(_lookup_bot_username("funstat_bot") == "funstat_bot", "подчёркивание OK")


def test_bot_cmds():
    from config import _bot_cmds
    check(_bot_cmds("funstat:/human") == {"funstat": "/human {name}"},
          "один per-bot cmd")
    check(_bot_cmds("funstat:/human,infostat:/find") ==
          {"funstat": "/human {name}", "infostat": "/find {name}"},
          "два per-bot")
    check(_bot_cmds("@funstat:/human") == {"funstat": "/human {name}"},
          "@ в имени убирается")
    check(_bot_cmds("FUNSTAT:/human") == {"funstat": "/human {name}"},
          "case → lower")
    check(_bot_cmds("funstat:/find {name}") == {"funstat": "/find {name}"},
          "уже с {name}")
    check(_bot_cmds("") == {}, "пусто")
    check(_bot_cmds(None) == {}, "None")
    check(_bot_cmds("malformed") == {}, "без двоеточия игнорируется")
    check(_bot_cmds("funstat:") == {}, "пустая команда игнорируется")
    check(_bot_cmds(":cmd") == {}, "пустое имя бота игнорируется")


def main():
    test_bot_list()
    test_lookup_bot_username()
    test_bot_cmds()
    print(f"\n=== {ok_count} tests PASSED ===")


if __name__ == "__main__":
    main()
