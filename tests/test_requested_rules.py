"""Правила, которые просил админ (ноябрь 2026). Запуск: python -m pytest tests/test_requested_rules.py
(или просто python tests/test_requested_rules.py). Telegram-библиотеки не нужны."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings                      # noqa: E402
from database.models import Listing              # noqa: E402
from filters.price_filter import PriceFilter     # noqa: E402
from filters.seller_filter import SellerFilter   # noqa: E402
from utils.links import gift_slug                # noqa: E402
from utils.names import letter_count, search_name  # noqa: E402


def _lot(**kw) -> Listing:
    l = Listing(listing_id="t", gift_unique_id=1, gift_id=1, gift_name="Lol Pop")
    l.model, l.backdrop, l.symbol = "Wild Mango", "Cappuccino", "Celtic Cross"
    for k, v in kw.items():
        setattr(l, k, v)
    return l


def _setup():
    settings.min_stars, settings.collection_min_floor = 400, 0
    settings.premium_over_floor_percent, settings.rare_attribute_percent = 333, 1.5
    settings.max_above_floor = 450
    settings.max_seller_level, settings.require_free_messages = 4, True
    settings.russian_only, settings.allow_hidden_owner = True, True
    settings.hidden_name_strict = False      # dj: мягкий режим скрытых владельцев по умолчанию


def test_premium_over_floor_only_rare_or_mono():
    _setup()
    f = PriceFilter()
    common = dict(floor_stars=500, model_rarity="3%", backdrop_rarity="3%")
    assert f.check(_lot(stars_price=650, **common)).passed                         # +30% — ок
    assert not f.check(_lot(stars_price=2250, **common)).passed                    # +350% обычный — нет
    assert f.check(_lot(stars_price=2250, floor_stars=500, model_rarity="1.2%",
                        backdrop_rarity="3%")).passed                                # редкая модель — ок
    assert f.check(_lot(stars_price=2250, floor_stars=500, model_rarity="легендарная",
                        backdrop_rarity="3%")).passed                                # редкость словом — ок
    assert f.check(_lot(stars_price=2250, backdrop="Monochrome", **common)).passed  # монохром — ок


def test_hard_cap_450_even_for_rare():
    _setup()
    f = PriceFilter()
    # редкий лот: потолок MAX_ABOVE_FLOOR намеренно НЕ действует (см. price_filter: 8000⭐ на флоре 929
    # с редкой моделью раньше падал по cap и дорогие лоты не шли вообще)
    assert f.check(_lot(stars_price=2760, floor_stars=500, model_rarity="1%",
                        backdrop_rarity="1%")).passed
    # обычный лот выше потолка — по-прежнему нет
    assert not f.check(_lot(stars_price=2760, floor_stars=500, model_rarity="3%",
                            backdrop_rarity="3%")).passed


def test_fancy_names_and_arabic():
    _setup()
    sf = SellerFilter(Path(tempfile.mkdtemp()))

    def code(name):
        return sf.check(_lot(seller_name=name)).code
    # === мягкий режим (dj, по умолчанию) ===
    assert code("Д̷̛м̶и̵т̸р̷и̶й̵") == "NAME_RU_HIDDEN"                # «размытый» — русское имя
    assert code("𝕬𝖓𝖉𝖗𝖊𝖎") == "OWNER_HIDDEN"                       # вычурный шрифт → Andrei (латиница) — пропускаем
    assert code("VANYA") == "OWNER_HIDDEN"                           # латиница, читается как рус — пропускаем
    assert code("꧁Ⓐⓛⓔⓧ꧂") == "OWNER_HIDDEN"                        # Alex (4 буквы) — короткое больше НЕ режем
    assert code("Яна ö") == "NAME_RU_HIDDEN"                          # кириллица есть → наш (ö больше не режет)
    assert code("") == "OWNER_HIDDEN"                                # пустое имя — пропускаем
    assert code("明美さん") == "FOREIGN_NAME_HIDDEN"                  # иероглифы — заведомо чужой скрипт, режем
    assert code("Дмитрий محمد") == "ARABIC_SCRIPT"                    # арабица — режем всегда
    assert code("ﻣﺤﻤﺪ ✨") == "ARABIC_SCRIPT"                         # «красивые» арабские формы
    # === строгий режим (HIDDEN_NAME_STRICT=true) — старое поведение сохраняется ===
    settings.hidden_name_strict = True
    assert code("꧁Ⓐⓛⓔⓧ꧂") == "SHORT_NAME_HIDDEN"                   # Alex = 4 буквы → режем
    settings.hidden_name_strict = False


def test_seller_level_and_paid_dm():
    _setup()
    sf = SellerFilter(Path(tempfile.mkdtemp()))
    base = dict(seller_type="user", seller_id=5, seller_name="Иван Петров")
    assert sf.check(_lot(level=4, messages="бесплатно", **base)).passed
    assert sf.check(_lot(level=5, messages="бесплатно", **base)).code == "LEVEL_TOO_HIGH"
    assert sf.check(_lot(level=1, messages="платно (10 ⭐)", **base)).code == "PAID_MESSAGES"


def test_name_helpers():
    assert search_name("🔥Александр Касьяненко") == "Александр Касьяненко"
    assert search_name("ᴀʟᴇxᴀɴᴅᴇʀ") == "alexander"
    assert letter_count(".𖥔 ݁𝑀𝒾𝓀𝓊 .𖥔 ݁") == 4
    assert gift_slug(None, "Jack-in-the-Box", 5) == "JackInTheBox-5"
    assert gift_slug(None, "Durov's Cap", 7) == "DurovsCap-7"


def test_route_below_lowest_goes_to_lowest_topic():
    old = settings.price_routes
    settings.price_routes = [(400, 1000, -100, 2), (1000, None, -100, 3)]
    try:
        assert settings.route_chat(350) == (-100, 2)
        assert settings.route_chat(999) == (-100, 2)
        assert settings.route_chat(1500) == (-100, 3)
    finally:
        settings.price_routes = old


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            fn()
            print("✔", n)
