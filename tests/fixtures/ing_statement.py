"""Synthetic ING statement text (fictitious people, accounts and amounts).

Mirrors the layout produced by pdfplumber for real ING Kontoauszug PDFs:
the header block of page 1 appears *after* the booking table, every page ends with
a ``T_...`` marker, and the last page ends the table with ``Neuer Saldo``.
"""

PAGE_1 = """\
Girokonto Nummer 1234567890
Kontoauszug März 2026
Buchung Buchung / Verwendungszweck Betrag (EUR)
Valuta
02.03.2026 Dauerauftrag/Terminueberw. Hausverwaltung Muster -250,00
02.03.2026 Hausgeld Musterstrasse 1
Referenz:HV-2026-03
02.03.2026 Lastschrift VISA REWE MUSTERSTADT -23,45
02.03.2026 NR XXXX 1111 MUSTERSTADT DE KAUFUMSATZ 28.02 23.45 101010
ARN11111111111111111111111 Google Pay
03.03.2026 Lastschrift VISA PAYPAL *VINTED -9,09
03.03.2026 NR XXXX 1111 61049923 FR KAUFUMSATZ 01.03 9.09 111111
ARN22222222222222222222222
03.03.2026 Lastschrift VISA PAYPAL *VINTED -9,09
03.03.2026 NR XXXX 1111 61049923 FR KAUFUMSATZ 01.03 9.09 121212
ARN33333333333333333333333
T_TESTMARKER01
ING-DiBa AG · 60628 Frankfurt am Main
Datum 31.03.2026
Erika Musterfrau Auszugsnummer 3
Eingeräumte Kontoüberziehung 0,00 Euro
Musterstrasse 1
Alter Saldo 1.000,00 Euro
12345 Musterstadt
Neuer Saldo 3.555,87 Euro
IBAN DE12 3456 7890 1234 5678 90
BIC INGDDEFFXXX
Seite 1 von 2
"""

PAGE_2 = """\
Buchung Buchung / Verwendungszweck Betrag (EUR)
Valuta
05.03.2026 Lastschrift PayPal Europe S.a.r.l. et Cie S.C.A -32,50
05.03.2026 1052751695622/. eBay S.a r.l., Ihr Einkauf bei eBay S.a r.l., Artikel-
Mandat:5M3J2256LF2BN
Referenz:1052751695622
10.03.2026 Echtzeitüberweisung Handwerker GmbH -120,00
10.03.2026 Rechnungsnr.: RE-4711
25.03.2026 Gehalt/Rente Beispiel AG 4.000,00
25.03.2026 Lohn/Gehalt 03/26
31.03.2026 Lastschrift Musterfrau -1.000,00
31.03.2026 ING-DIBA AG 9999999999 31.03.2026 ZINSEN 600,00 TILGUNG
400,00
Neuer Saldo 3.555,87
Kunden-Information
Bitte beachten Sie auch die Hinweise auf der Folgeseite.
ING-DiBa AG
T_TESTMARKER01
Girokonto Nummer 1234567890
Kontoauszug März 2026
Datum 31.03.2026
Seite 2 von 2
"""

PAGES = [PAGE_1, PAGE_2]

# 1.000,00 - 250,00 - 23,45 - 2 x 9,09 - 32,50 - 120,00 + 4.000,00 - 1.000,00 = 3.555,87
BOOKING_COUNT = 8
OPENING_CENTS = 100000
CLOSING_CENTS = 355587
