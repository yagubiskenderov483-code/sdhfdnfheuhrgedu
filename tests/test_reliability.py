"""Dependency-free policy tests; never load production credentials or Telegram."""
import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


links = load("_tested_links", "utils/links.py")
policy = load("_tested_policy", "tracker/reliability.py")


class DeliveryPolicyTests(unittest.TestCase):
    def test_canonical_gift_link(self):
        self.assertEqual(links.gift_link("LolPop-123"), "https://t.me/nft/LolPop-123")
        self.assertEqual(links.gift_link("https://t.me/nft/LolPop-123?x=1"), "https://t.me/nft/LolPop-123")

    def test_invalid_gift_links(self):
        for value in (None, "", "Lol Pop", "https://evil.example/nft/LolPop-123", "../wrong", "LolPop-0"):
            with self.subTest(value=value):
                self.assertIsNone(links.gift_link(value))

    def test_missing_link_recovers_from_slug(self):
        item = types.SimpleNamespace(gift_slug="LolPop-123", gift_link=None)
        self.assertEqual(links.listing_gift_link(item), "https://t.me/nft/LolPop-123")

    def test_usernames_are_not_fabricated(self):
        self.assertEqual(links.normalize_username(" @seller_123 "), "seller_123")
        for value in (None, "", "John Doe", "https://t.me/seller", "a/b"):
            self.assertIsNone(links.normalize_username(value))
        self.assertIsNone(links.user_id_deeplink(-123))

    def test_lookup_does_not_reset_age(self):
        item = types.SimpleNamespace(first_seen="1970-01-01T00:00:10+00:00", t_found=100)
        self.assertEqual(policy.age_seconds(item, now_wall=90, now_mono=130), 80)

    def test_bad_and_future_timestamps_fail_closed(self):
        for stamp in (None, "bad", "2030-01-01T00:00:00+00:00"):
            item = types.SimpleNamespace(first_seen=stamp)
            self.assertEqual(policy.age_seconds(item, now_wall=100, now_mono=100), float("inf"))

    def test_legacy_age_settings_are_capped(self):
        conf = types.SimpleNamespace(max_lot_age=1800, lookup_max_age=900, strict_new_lots=True)
        self.assertEqual(policy.age_limit(conf), 180)
        conf.lookup_max_age = 60
        self.assertEqual(policy.age_limit(conf, lookup=True), 60)

    def test_price_and_rarity_rules(self):
        config = types.ModuleType("config")
        config.settings = types.SimpleNamespace(
            rare_attribute_percent=1.5, allow_ton_only=True, collection_min_floor=0,
            min_stars=0, min_ton=0, premium_over_floor_percent=333, max_above_floor=500)
        models = types.ModuleType("database.models")
        models.Listing = object
        with patch.dict(sys.modules, {"config": config, "database.models": models}):
            price = load("_tested_price", "filters/price_filter.py")
            item = types.SimpleNamespace(stars_price=4330, ton_price=None, ton_only=False,
                floor_stars=1000, model="Ordinary", backdrop="Ordinary", model_rarity="2%",
                backdrop_rarity="2%", symbol_rarity="0.1%")
            self.assertFalse(price.PriceFilter().check(item).passed)
            item.stars_price = 4329
            self.assertTrue(price.PriceFilter().check(item).passed)
            item.stars_price, item.model_rarity = 4330, "1,5%"
            self.assertTrue(price.PriceFilter().check(item).passed)
            item.model_rarity, item.backdrop = "2%", "Monochrome"
            self.assertTrue(price.PriceFilter().check(item).passed)
            for value in (None, "NaN%", "Infinity%", "0%", "-1%", "2%"):
                self.assertFalse(price._rare(value))
            item.stars_price = 0
            self.assertFalse(price.PriceFilter().check(item).passed)

    def test_source_syntax_and_retained_cores(self):
        for path in ("main.py", "app_core.py", "config.py", "bot/resilience.py", "bot/formatter.py",
                     "bot/formatter_core.py", "tracker/coordinator.py", "tracker/coordinator_core.py",
                     "tracker/lookup.py", "tracker/lookup_core.py", "tracker/queue.py", "tracker/queue_core.py"):
            with self.subTest(path=path):
                ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=path)


if __name__ == "__main__":
    unittest.main()
