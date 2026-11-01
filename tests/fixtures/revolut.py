"""Synthetic Revolut EUR account statement (fictitious data).

Mirrors the pdfplumber text of a real German export: two sections (the account moved
from Revolut Payments UAB to Revolut Bank UAB), the move as a booking pair at the
boundary, and a reversed transaction that is not part of the balance.
"""

_FOOTER = """\
Verlorene oder gestohlene Karte melden Revolut ist ein Institut mit Sitz in Litauen.
© 2026 Revolut Seite 1 von 1
"""

PAGE_1 = (
    """\
EUR-Kontoauszug
Generiert am 21.10.2026
Revolut Payments UAB
ERIKA MUSTERFRAU
Musterweg 1 IBAN DE11100101781234567890
12345 BIC REVODEB2
Musterstadt IBAN LT111111111111111111
BIC REVOLT21
Zusammenfassung der Bilanz
Produkt Eröffnungssaldo Geldausgang Geldeingang Schlusssaldo
Konto (E-Geld) 0,00€ 1.043,52€ 1.043,52€ 0,00€
Gesamt 0,00€ 1.043,52€ 1.043,52€ 0,00€
Kontotransaktionen von 16. Januar 2021 bis 30. Juni 2022
Datum Beschreibung Geldausgang Geldeingang Kontostand
16.01.2021 Google Pay Einzahlung von *1234 40,00€ 40,00€
Von: *5678
05.07.2021 Microsoft Store 30,85€ 9,15€
Revolut Kurs: 1,00€ = 145 ISK (ECB-Kurs* 1,00€ = 147 ISK) 4.479 ISK
An: Microsoft*store, Msbill.info
Karte: 535456******1111
25.10.2021 Zahlung von ERIKA MUSTERFRAU.MAX 1.000,00€ 1.009,15€
MUSTERMANN
Referenz: Urlaub
Von: ERIKA MUSTERFRAU.MAX MUSTERMANN, DE96500105171234567890
07.01.2022 Mvideo 3,52€ 1.012,67€
302,00 RUB
Von: Mvideo Atg, Moscow
Karte: 535456******1111
08.01.2022 Nord Wind 130,49€ 882,18€
Gebühr: 1,29€ 129,20€
An: Nord Wind, Moscow
Karte: 535456******1111
30.06.2022 Revolut Bank UAB 882,18€ 0,00€
"""
    + _FOOTER
)

PAGE_2 = (
    """\
EUR-Kontoauszug
Generiert am 21.10.2026
Revolut Bank UAB
ERIKA MUSTERFRAU
Musterweg 1 IBAN DE11100101781234567890
12345 BIC REVODEB2
Zusammenfassung der Bilanz
Produkt Eröffnungssaldo Geldausgang Geldeingang Schlusssaldo
Konto (Girokonto) 0,00€ 826,02€ 882,18€ 56,16€
Gesamt 0,00€ 826,02€ 882,18€ 56,16€
Kontotransaktionen von 30. Juni 2022 bis 21. Oktober 2026
Datum Beschreibung Geldausgang Geldeingang Kontostand
30.06.2022 Revolut Payments UAB 882,18€ 882,18€
22.04.2024 To ERIKA MUSTERFRAU 81,84€ 800,34€
Referenz: Erika Musterfrau
Gebühr: 1,84€ 80,00€
14.09.2026 Cloud Storage Ltd 744,18€ 56,16€
An: Cloud Storage Ltd, example.com, CA
Karte: 416598******2222
"""
    + _FOOTER
)

PAGE_3 = (
    """\
EUR-Kontoauszug
Generiert am 21.10.2026
Revolut Bank UAB
Rückgängig gemacht von 30. Juni 2022 an 21. Oktober 2026
Startdatum Beschreibung Geldausgang Geldeingang
19.08.2026 Brave Browser 0,87€
An: www.brave.com, 16502003351
Karte: 416598******2222
"""
    + _FOOTER
)

PAGES = [PAGE_1, PAGE_2, PAGE_3]
IBAN = "DE11100101781234567890"
CLOSING_CENTS = 56_16
BOOKING_COUNT = 7  # 9 table rows minus the move between the two Revolut entities
