"""Synthetic ING Baufinanzierung yearly statement (fictitious data).

Mirrors the pdfplumber text of the real PDF: one value date per booking, continuation
lines without date, the page-1 header block after the ``T_`` marker.
"""

PAGE_1 = """\
Ihre Baufinanzierung Kontonummer 9988776655
Kontoauszug 2025
Valuta Vorgang Euro
Saldovortrag -200.000,00
30.01.2025 Zinsen -500,00
30.01.2025 Lastschrifteinzug Musterfrau 500,00
ING-DIBA AG 9988776655 30.01.2025 ZINSEN 500,00
28.04.2025 Auszahlung Musterfrau -10.000,00
ING-DiBa AG VNR 0000000001
30.04.2025 Zinsen -520,00
30.04.2025 Bereitstellungszinsen -5,00
30.04.2025 Lastschrifteinzug Musterfrau 525,00
ING-DIBA AG 9988776655 30.04.2025 ZINSEN 520,00
BEREITST-ZINSEN 5,00
T_TESTMARKER03
Datum 30.12.2025
ING-DiBa AG · 60628 Frankfurt am Main
Darlehensbetrag -210.000,00 Euro
Neuer Saldo -209.000,00 Euro
Erika Musterfrau IBAN DE11 5001 0517 9988 7766 55
BIC INGDDEFFXXX
Seite 1 von 2
"""

PAGE_2 = """\
Valuta Vorgang Euro
30.12.2025 Zinsen -510,00
30.12.2025 Lastschrifteinzug Musterfrau 1.510,00
ING-DIBA AG 9988776655 30.12.2025 ZINSEN 510,00 TILGUNG
1.000,00
Neuer Saldo -209.000,00
Abschluss für Konto 9988776655
T_TESTMARKER03
Baufinanzierung Kontonummer 9988776655
Kontoauszug 2025
Datum 30.12.2025
Seite 2 von 2
"""

PAGES = [PAGE_1, PAGE_2]
IBAN = "DE11500105179988776655"
CLOSING_CENTS = -209_000_00
BOOKING_COUNT = 8
