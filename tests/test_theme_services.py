import re
import unittest

from theme_services import (
    DEFAULT_THEME_NAME,
    REQUIRED_THEME_KEYS,
    THEME_PALETTES,
    get_theme_palette,
    normalize_theme_name,
)


def _rgb(value):
    return tuple(int(value[index:index + 2], 16) / 255 for index in (1, 3, 5))


def _luminance(value):
    def channel(component):
        return component / 12.92 if component <= 0.04045 else ((component + 0.055) / 1.055) ** 2.4

    red, green, blue = _rgb(value)
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def _contrast(first, second):
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


class ThemeServiceTests(unittest.TestCase):
    def test_five_complete_palettes_are_available(self):
        self.assertEqual(
            tuple(THEME_PALETTES),
            ("graphite", "cloud_indigo", "ocean_cyan", "jade_green", "warm_amber"),
        )
        for palette in THEME_PALETTES.values():
            self.assertEqual(set(palette.colors), set(REQUIRED_THEME_KEYS))
            self.assertTrue(palette.display_name)
            self.assertTrue(palette.description)
            for value in palette.colors.values():
                self.assertRegex(value, re.compile(r"^#[0-9A-F]{6}$"))

    def test_core_readability_contrast_is_preserved(self):
        for palette in THEME_PALETTES.values():
            colors = palette.colors
            self.assertGreaterEqual(
                _contrast(colors["TEXT_MAIN"], colors["BG_MAIN"]),
                4.5,
                palette.name,
            )
            self.assertGreaterEqual(
                _contrast(colors["TEXT_ON_INK"], colors["INK_ELEVATED"]),
                4.5,
                palette.name,
            )

    def test_unknown_configuration_safely_falls_back_to_graphite(self):
        self.assertEqual(normalize_theme_name("missing"), DEFAULT_THEME_NAME)
        self.assertEqual(get_theme_palette("missing").name, DEFAULT_THEME_NAME)
        self.assertEqual(normalize_theme_name(" OCEAN_CYAN "), "ocean_cyan")


if __name__ == "__main__":
    unittest.main()
