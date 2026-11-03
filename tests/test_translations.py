"""The translation files: the English one repeats the source texts."""

import xml.etree.ElementTree as ET
from pathlib import Path

I18N = Path(__file__).resolve().parents[1] / "src" / "hiro_budget" / "i18n"


def test_english_translations_are_the_source_texts():
    """app_en.ts holds the English source text, never a German translation (53 German
    texts in it showed German in the English UI, #63)."""
    wrong = []
    for context in ET.parse(I18N / "app_en.ts").getroot().iter("context"):
        for message in context.iter("message"):
            if message.get("numerus") == "yes":
                continue  # plurals: "%n booking(s)" becomes "1 booking" / "%n bookings"
            source = message.findtext("source")
            translation = message.findtext("translation")
            if translation != source:
                wrong.append(f"{context.findtext('name')}: {source!r} -> {translation!r}")
    assert not wrong, "\n".join(wrong)
