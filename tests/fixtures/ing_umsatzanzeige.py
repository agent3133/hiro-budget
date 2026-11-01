"""Synthetic ING Umsatzanzeige (fictitious data), newest booking first.

Mirrors the pdfplumber text of the real PDF: labels and values of the page-1 header in
separate blocks, two lines per booking (booking line with balance after the booking and
amount, then value date with booking text), purposes without spaces in places.
"""

PAGE_1 = """\
Umsatzanzeige
Erstellt am
Letztes Konto Update
Bank
Kontoname
IBAN
Saldo
Zeitraum
Seite
Erika Musterfrau 21.10.2026, 13:54 Uhr
Musterweg 1
12345 Musterstadt ING
Girokonto
DE12 3456 7890 1234 5678 90
1.007,95 €
01.10.2026 bis 21.10.2026 - / Datum absteigend 6
Buchung Auftraggeber/Empfänger Saldo Betrag
Wertstellun Buchungstext
gsdatum Verwendungszweck
Notiz
21.10.2026 VISA REWE MUSTERSTADT 1.007,95 € -18,84 €
21.10.2026 Lastschrift
NR XXXX 1234 MUSTERSTADT DEKAUFUMSATZ19.10
18.84000000ARN00000000000000000000003Google Pay
20.10.2026 Musterfirma GmbH 1.026,79 € +30,00 €
20.10.2026 Gutschrift Echtzeitüberweisung
RNr. 2609
06.10.2026 Sportverein Muster e.V. 996,79 € -17,50 €
06.10.2026 Lastschrift
26/04008
1/2
"""

PAGE_2 = """\
Umsatzanzeige
Seite
Erika Musterfrau Erstellt am 21.10.2026, 13:54 Uhr
Letztes Konto Update
Bank ING
Kontoname Girokonto
Buchung Auftraggeber/Empfänger Saldo Betrag
Wertstellun Buchungstext
gsdatum Verwendungszweck
Notiz
05.10.2026 Max Muster 1.014,29 € +7,20 €
05.10.2026 Gutschrift aus Dauerauftrag
Spotify
02.10.2026 ING 1.007,09 € -1,49 €
02.10.2026 Entgelt
GIROCARD 290000429613MONATLICHES ENTGELTGIROCARD (DEBITKARTE)
01.10.2026 Hausverwaltung Muster 1.008,58 € -991,42 €
01.10.2026 Dauerauftrag / Terminueberweisung
2/2
"""

PAGES = [PAGE_1, PAGE_2]
IBAN = "DE12345678901234567890"
OPENING_CENTS = 2_000_00
CLOSING_CENTS = 1_007_95
BOOKING_COUNT = 6
