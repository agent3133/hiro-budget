# CSV-Import

*English version: [csv-import.en.md](csv-import.en.md). Ein Format mit Hilfe eines
KI-Assistenten einrichten: [csv-formats.llm.md](csv-formats.llm.md).*

Viele Banken bieten ihre Buchungen als CSV-Datei an. Die App importiert solche Dateien wie
PDF-Kontoauszüge: derselbe Importdialog, dieselbe Duplikatprüfung, dieselbe Kategorisierung.
Was die Spalten der CSV einer Bank bedeuten, steht einmal je Bank in einem *Format*. Formate
für drei Exporte sind in der App dabei; für jede andere Bank legst du selbst eins an, als
kleines Plugin in `config/plugins/`.

| Mitgeliefertes Format | Datei | Hinweis |
|---|---|---|
| ING Umsatzanzeige | Online-Banking → Umsätze → als CSV exportieren | Dieselben Buchungen wie die Umsatzanzeige als PDF: eins von beiden importieren, beide werden als dieselben erkannt |
| Revolut | App → Konto → Kontoauszug → Excel/CSV, **auf Englisch** | Schon aus Revolut-PDFs importierte Buchungen werden erkannt |
| PayPal | Aktivitäten → herunterladen, CSV, alle Transaktionen | Braucht ein PayPal-Konto in der App, siehe unten |

Die mitgelieferten Formate kommen mit der App und werden mit ihr aktualisiert. Deine eigenen
Formate in `config/plugins/` gehören dir: die App ändert sie nie. Ein eigenes Format mit dem
Namen eines mitgelieferten Plugins ersetzt es. (Früher standen Formate in
`config/csv_formats.yaml`: beim ersten Start einer neueren Version verschiebt die App deine
eigenen Formate von dort nach `config/plugins/` und benennt die Datei in
`csv_formats.yaml.migrated` um.)

---

## 1. Eine CSV-Datei importieren

1. Die Datei in den Import-Ordner legen (Standard `input/`) oder „Importieren“ →
   „Dateien hinzufügen…“.
2. Vor dem Import die Zeile der Datei in der Liste prüfen:
   - **Konto**: wohin die Buchungen gehen; „(neu)“ heißt, die App legt das Konto an.
   - **Buchungen / Neu / Duplikate**: wie viele Zeilen Buchungen wurden und wie viele schon
     gespeichert sind.
   - **Saldo**: ✓ und der Endsaldo, wenn die Datei eine Saldospalte hat und jede Zeile aufgeht;
     ✗ und die Differenz, wenn nicht (nicht importieren, das Format stimmt wahrscheinlich
     nicht).
   - **Status**: „bereit“, „nichts Neues“, eine Fehlermeldung oder „überschneidet …
     gespeicherte Buchungen“ (du wirst gefragt: **„Fehlende ergänzen“** wählen, wenn dieselben
     Buchungen schon aus einer anderen Quelle kamen, z. B. aus PDFs).
3. „N Dateien importieren“.

CSV-Exporte dürfen sich überschneiden: keine Buchung wird zweimal importiert. Wie jeder Import
lässt sich ein CSV-Import unter „Datei → Importverlauf…“ rückgängig machen.

### PayPal

PayPal-Zahlungen laufen meist über das Girokonto (Debitkarte oder Lastschrift), dort stehen sie
also schon als „VISA PAYPAL *…“ oder „PayPal Europe“. Sie werden **nicht** ein zweites Mal
importiert. Stattdessen bekommt die Buchung auf dem Girokonto den Empfänger und Artikel von
PayPal („Zahlungsdetails“ in den Buchungsdetails; Regeln können das Feld „Zahlungsdetails“
nutzen). Nur was das Girokonto nicht berührt, wird eine Buchung des PayPal-Kontos: Zahlungen
aus dem PayPal-Guthaben, erhaltenes oder gesendetes Geld.

Einmal vor dem ersten Import: „Konten“ → „Neues Konto“, Art „Bezahldienst (PayPal)“,
„E-Mail“ = die Adresse des PayPal-Kontos, „Konten speichern“. Ein App-Konto je PayPal-Konto.

Das Girokonto bucht eine PayPal-Zahlung einige Tage später. Ist diese Buchung noch nicht
importiert, wartet die Zahlung und wird nach dem nächsten Import oder Abruf verknüpft. Die
Meldung nach dem Import sagt, wie viele warten.

---

## 2. Ein Format für deine Bank anlegen

Du brauchst die CSV-Datei, einen Texteditor und etwa 15 Minuten. Mit einem KI-Assistenten geht
es schneller: siehe [csv-formats.llm.md](csv-formats.llm.md).

### 2.1 Die Datei ansehen

Die CSV in einem **Texteditor** öffnen (Editor, VS Code), nicht in Excel: Excel zeigt Zahlen
und Datumsangaben auf seine Art und kann die Datei beim Speichern verändern. Notieren:

| Frage | Typische Antworten |
|---|---|
| Welches Zeichen trennt die Spalten? | `;` (deutsche Banken), `,`, Tabulator |
| Stehen Zeilen über den Spaltennamen? | ING: Konto, IBAN, Zeitraum als `Schlüssel;Wert` |
| Wie heißen die Spalten? | die Zeile mit „Datum“, „Betrag“, … |
| Wie sind Datumsangaben geschrieben? | `31.12.2026`, `2026-12-31`, `2026-12-31 14:05:00` |
| Wie sind Beträge geschrieben? | `-1.234,56` (Dezimalkomma) oder `-1234.56` (Dezimalpunkt) |
| Gibt es **eine** Betragsspalte mit Vorzeichen? | nötig: negativ = Geld geht raus |
| Steht nach jeder Zeile ein Saldo? | ermöglicht die Saldoprüfung |
| Hat jede Zeile eine eindeutige Transaktionsnummer? | beste Duplikatprüfung |
| Sind Umlaute kaputt (`Ã¤` oder `�`)? | dann ist die Kodierung nicht UTF-8: `latin-1` versuchen |

### 2.2 Das Format schreiben

Ein Format ist ein kleines Plugin: ein Ordner in `config/plugins/` mit zwei Dateien. Das
ähnlichste mitgelieferte Format kopieren (sie stehen in [csv-formats.llm.md](csv-formats.llm.md),
Abschnitt 7) und anpassen. Ein vollständiges Beispiel, `config/plugins/musterbank_csv/`:

`plugin.yaml`:

```yaml
name: musterbank_csv                      # = der Ordnername: Kleinbuchstaben, Ziffern, _
version: 1.0.0
api: 1
description: Musterbank, Umsätze als CSV
formats: [format.csv.yaml]
```

`format.csv.yaml`:

```yaml
name: Musterbank                          # beliebiger Name, eindeutig
institution: Musterbank                   # Bank von Konten, die der Import anlegt
kind: giro                                # Kontoart von Konten, die der Import anlegt
detect:
  header_has: [Buchungstag, Betrag, Empfänger]   # Spaltennamen, an denen die Datei erkannt wird
encoding: latin-1                         # Standard utf-8-sig
delimiter: ";"                            # Standard ","
date_format: "%d.%m.%Y"                   # Standard; 2026-12-31 ist "%Y-%m-%d"
decimal_comma: true                       # Standard; false für 1234.56
account:
  name: Musterbank Girokonto              # oder iban_from: IBAN (eine Zeile „IBAN;…“ über den Spaltennamen)
columns:
  booking_date: Buchungstag
  value_date: Valuta
  counterparty: Empfänger
  booking_type: Buchungsart
  purpose: [Verwendungszweck, Kundenreferenz]
  amount: Betrag
  balance: Saldo
skip:
  - {column: Status, equals: vorgemerkt}
```

Ein Plugin nur mit Formaten enthält keinen Programmcode und muss nicht eingeschaltet werden.
Alle Einstellungen stehen in [csv-formats.llm.md](csv-formats.llm.md), Abschnitt 4. Die
wichtigsten Regeln:

- **Spaltennamen** genau wie in der Datei schreiben (Groß- und Kleinschreibung, Leerzeichen).
  Heißen zwei Spalten gleich, gilt die erste; sonst ihre Position angeben (`0` ist die erste
  Spalte).
- **`detect.header_has`** nennt einige Spalten, die nur der Export dieser Bank hat. Die App
  liest die ersten 60 Zeilen einer Datei und nimmt das erste Format, dessen Namen alle in einer
  Zeile vorkommen.
- **`account`** legt fest, zu welchem App-Konto die Buchungen gehören. Mit einer IBAN in der
  Datei (`iban_from`) wird das Konto an der IBAN gefunden; mit `name` am Namen, der genau der
  Name des Kontos in der App sein muss, wenn es schon existiert.
- **`amount`** muss eine Spalte mit Vorzeichen sein. Eine Spalte `fee`, wenn die Bank Gebühren
  getrennt ausweist, wird davon abgezogen.
- **`only`** und **`skip`** lassen Zeilen weg: `only` behält Zeilen, auf die *alle* Filter
  passen (z. B. `State` = `COMPLETED`, eine Währung); `skip` lässt Zeilen weg, auf die
  *irgendein* Filter passt (z. B. vorgemerkte Buchungen). Ein Filter ist
  `{column: …, equals: …}`, `{column: …, in: [a, b]}` oder
  `{column: …, matches: <regulärer Ausdruck>}`.
- **`values`** benennt Werte von `booking_type` oder `counterparty` um, z. B. in die
  Schreibweise deiner PDF-Auszüge, damit deine Regeln weiter greifen.

### 2.3 Testen

1. Beide Dateien speichern, dann „Einstellungen → Plugins…“ → „Neu einlesen“ (oder die App
   neu starten). Die Liste zeigt dein Plugin als „aktiv“ oder den Fehler, wegen dem es
   weggelassen wird: er nennt Datei und Einstellung. Auch der Importdialog nennt
   weggelassene Plugins.
2. „Importieren“ → „Dateien hinzufügen…“ → deine CSV. Noch wird nichts importiert.
3. Die Zeile prüfen: das richtige Konto, eine plausible Zahl Buchungen, der Zeitraum, ✓ unter
   „Saldo“ (wenn das Format eine Saldospalte hat), kein Fehler unter „Status“. Die Zeile
   auswählen: die Tabelle darunter zeigt die Buchungen, wie sie importiert würden. Datum,
   Vorzeichen, Empfänger prüfen.
4. Falsch? „Abbrechen“, Format korrigieren, neu starten, noch einmal. Richtig? Importieren.

---

## 3. Wie Duplikate erkannt werden

Eine schon gespeicherte Buchung wird nie noch einmal importiert. „Gespeichert“ wird in dieser
Reihenfolge entschieden:

1. **Transaktionsnummer** (`columns.id`): hat die Datei eine, ist eine Buchung mit derselben
   Nummer auf demselben Konto ein Duplikat. Der zuverlässigste Weg; PayPal hat sie.
2. **Fingerabdruck**: sonst Datum, Betrag, Buchungsart, Empfänger und Verwendungszweck
   (Leerzeichen und Groß-/Kleinschreibung zählen nicht). Die ING-CSV und die ING-PDFs ergeben
   dieselben Fingerabdrücke, erkennen sich also gegenseitig.
3. **Datum und Betrag** (`match_by: date_amount`): auch ein Duplikat, wenn das Konto eine
   importierte Buchung mit demselben Datum und Betrag hat, jede gespeicherte Buchung einmal
   gezählt. Für eine CSV, deren Buchungen vorher aus einer anderen Quelle mit anderen Texten
   kamen (Revolut: PDF und CSV beschreiben dieselbe Buchung unterschiedlich).

Überschneidet sich der Zeitraum der Datei mit gespeicherten Buchungen, fragt der Dialog:
**„Fehlende ergänzen“** importiert nur Neues; „Überschreiben“ löscht die gespeicherten
Buchungen des Zeitraums und importiert stattdessen die Datei. „Überschreiben“ nur nehmen, wenn
die gespeicherten Buchungen falsch sind: manuelle Kategorien bleiben nur bei Buchungen, deren
Fingerabdruck in beiden gleich ist.

---

## 4. Grenzen

- **Eine Betragsspalte mit Vorzeichen.** Dateien mit getrennten Soll- und Haben-Spalten oder
  mit einer Spalte „S/H“ für das Vorzeichen lassen sich noch nicht importieren.
- **Beträge** dürfen Vorzeichen, Tausenderpunkte und Leerzeichen enthalten, aber kein
  Währungszeichen (`12,34 €`), keine Klammern für negative Zahlen und kein nachgestelltes Minus
  (`12,34-`).
- **Ein Konto je Datei.** Eine Datei mit mehreren Konten oder Währungen: mit `only` eins davon
  lesen; die anderen lassen sich aus demselben Export nicht importieren.
- **Datumsangaben** müssen genau zu `date_format` passen; eine Uhrzeit nach dem Datum gehört
  ins Format (`"%Y-%m-%d %H:%M:%S"`).
- Das Verknüpfen wie bei PayPal (`funding`) braucht eine Spalte mit Transaktionsnummer und eine
  Spalte, die die verknüpfte Zahlung nennt.

## 5. Fehlersuche

| Meldung | Ursache |
|---|---|
| Fehler: format format.csv.yaml: … Extra inputs are not permitted (Einstellungen → Plugins…) | eine falsch geschriebene Einstellung, genannt in der Zeile darüber, z. B. `delimeter` |
| Fehler: plugin.yaml: … / name '…' differs from its folder (Einstellungen → Plugins…) | das Manifest: eine Einstellung falsch geschrieben, oder `name` ist nicht der Ordnername |
| no importer recognizes … | `header_has` keines Formats passt: Namen und `delimiter` prüfen |
| header row with … not found | wie oben, oder die Spaltennamen stehen unter Zeile 60 |
| column '…' not found | ein Name in `columns`, `only` oder `skip` ist anders geschrieben als in der Datei |
| line N: time data … does not match format … | `date_format` passt nicht, oder `booking_date` zeigt auf die falsche Spalte |
| line N: not an amount | falsche Spalte, `decimal_comma` falsch oder ein Währungszeichen in den Zellen |
| balance check failed (difference …) | Vorzeichen oder Gebühr falsch, Zeilen fehlen (durch `only`/`skip` gefiltert) oder die Saldospalte gehört zu einem anderen Konto |
| line N: amount … does not match the balance change | dieselben Ursachen für eine Zeile; Zeilen in unerwarteter Reihenfolge |
| no own account with e-mail … | PayPal: das PayPal-Konto mit dieser E-Mail unter „Konten“ anlegen |
| Umlaute in der Vorschau kaputt | falsches `encoding` (`latin-1` statt `utf-8-sig` oder umgekehrt) |
