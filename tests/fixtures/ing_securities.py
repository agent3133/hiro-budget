"""Synthetic ING Extra-Konto (yearly) and Depotauszug text (fictitious data).

Mirrors the layout pdfplumber produces for the real PDFs. The Extra-Konto is the cash
account of the depot: securities purchases/sales, distributions, interest and taxes.
"""

EXTRA_KONTO_PAGE = """\
Extra-Konto Nummer 9876543210
Kontoauszug 2025
Buchung Buchung / Verwendungszweck Betrag (EUR)
Valuta
20.01.2025 Wertpapiergutschrift 1.000,00
22.01.2025 WP-ABRECHNUNG0000000001001VerkaufISINIE0000000001MUSTER
WORLDETF
24.02.2025 Zins/Dividende WP 50,00
21.02.2025 Zins/DividendeISINIE0000000001MUSTER
25.02.2025 Wertpapierkauf -300,00
27.02.2025 WP-ABRECHNUNG0000000002001KaufISINIE0000000001MUSTER
26.05.2025 Lastschrift-Einzug Erika Musterfrau 500,00
26.05.2025
30.12.2025 Kapitalertragsteuer -5,00
30.12.2025
30.12.2025 Solidaritätszuschlag -0,27
30.12.2025
30.12.2025 Zinsertrag 20,00
30.12.2025
Neuer Saldo 1.364,73
Abschluss für Konto 9876543210
T_TESTMARKER02
ING-DiBa AG · 60628 Frankfurt am Main
Datum 30.12.2025
Erika Musterfrau Auszugsnummer 1
Alter Saldo 100,00 Euro
Neuer Saldo 1.364,73 Euro
IBAN DE98 7654 3210 9876 5432 10
BIC INGDDEFFXXX
Seite 1 von 1
"""

EXTRA_KONTO_IBAN = "DE98765432109876543210"
EXTRA_KONTO_BOOKINGS = 7

DEPOT_PAGE = """\
Depotinhaber: Erika Musterfrau
Direkt-Depot Nr.: 1122334455
Datum: 12.10.2026
Seite: 1 von 1
Depotauszug per 30.09.2026
Stücke/Nominale Wertpapier-Informationen Kurs Kurswert
Devisenkurs
100 Stück MUSTER WORLD UCITS ETF 50,00 EUR 5.000,00 EUR
Registered Shares o.N.
ISIN (WKN): IE0000000001 (A0AAA1)
Verwahrart: Wertpapierrechnung
1.234,56789 Stück MUSTER EUROPE ETF 10,123 EUR 12.497,53 EUR
ISIN (WKN): LU0000000002 (A0AAA2)
Anzahl Posten: 2 Gesamtkurswert 17.497,53 EUR
ING-DiBa AG 60628 Frankfurt am Main
"""

DEPOT_NO = "1122334455"
DEPOT_TOTAL_CENTS = 17_497_53
