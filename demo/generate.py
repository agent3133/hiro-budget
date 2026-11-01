"""Generate the demo database: ten years of a fictional family's finances.

    uv run python demo/generate.py [--db PATH] [--start 2016-11] [--months 120]

Two adults (both earning), two children and a dog in Darmstadt. Everything is produced
by code: curated lists of German payees, Faker (de_DE) for people, IBANs and references,
and random distributions with a fixed seed. The simulation renders the monthly
statements as the text of the ING and Revolut PDFs, runs them through the app's own
parsers and import service (which categorizes with the rules, transfer and mortgage
matching of ``demo/config``) and enters cash bookings as manual bookings. Whatever is
still uncategorized afterwards gets a manual category, and the last five years are
budgeted. The database is deleted and recreated on every run.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import random
import sys
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from faker import Faker
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from hiro_budget.app import App
from hiro_budget.config import Settings, load_settings
from hiro_budget.db.models import (
    Account,
    BudgetAssignment,
    Category,
    CategoryGroup,
    ImportBatch,
    Split,
    SplitSource,
    Transaction,
)
from hiro_budget.importers.base import ParsedHolding, ParsedStatement, ParsedTransaction
from hiro_budget.plugins.ing_giro_pdf import importer as ing_giro_pdf
from hiro_budget.plugins.revolut_pdf import importer as revolut_pdf
from hiro_budget.services import budget
from hiro_budget.services.categorization.pipeline import load_transactions, recategorize_all
from hiro_budget.services.import_service import ImportPreview, commit, dedupe_keys
from hiro_budget.services.proposals import round_up
from hiro_budget.services.transactions import (
    ManualEntry,
    SplitInput,
    create_manual,
    set_splits,
)

DEMO_DIR = Path(__file__).resolve().parent
CONFIG_DIR = DEMO_DIR / "config"
SEED = 20161101
FIRST_MONTH = date(2016, 11, 1)
TODAY = date(2026, 10, 23)  # bookings up to the day before; October has no statement yet
BUDGET_MONTHS = 60

# Own accounts (as in demo/config/config.yaml)
# IBANs with check digits 00, which are never valid: they cannot belong to anyone
GIRO_NO, GIRO_IBAN = "5403817296", "DE00500105175403817296"
EXTRA_NO, EXTRA_IBAN = "5627394810", "DE00500105175627394810"
LOAN_NO, LOAN_IBAN = "8061529374", "DE00500105178061529374"
DEPOT_NO = "8015237461"
REVOLUT_IBAN = "DE00100101784920583716"
CARD_A, CARD_B = "4821", "7390"  # Visa debit cards of the Girokonto (Papa, Mama)
REVOLUT_CARD = "416598******2754"
CASH_ACCOUNTS = {"A": "Bargeld Papa", "B": "Bargeld Mama"}
CITY, CITY_UPPER = "Darmstadt", "DARMSTADT"
POSTCODES = ["64283", "64285", "64287", "64289", "64291", "64293", "64295", "64297"]

# Children, dog, school career
KID1_BIRTH, KID2_BIRTH = date(2014, 5, 12), date(2016, 3, 3)
DOG_SINCE = date(2018, 4, 14)
NEW_CAR = date(2022, 6, 10)
REVOLUT_SINCE = date(2017, 9, 1)

# German consumer price inflation (% per year); prices scale with the index
INFLATION = {
    2016: 0.5, 2017: 1.5, 2018: 1.8, 2019: 1.4, 2020: 0.5, 2021: 3.1,
    2022: 6.9, 2023: 5.9, 2024: 2.2, 2025: 2.1, 2026: 2.0,
}  # fmt: skip

MONTH_NAMES = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]  # fmt: skip

# ETF savings plan: (name, ISIN, WKN, start price, yearly returns)
ETFS = [
    (
        "VANGUARD FTSE ALL-WORLD UCITS ETF",
        "IE00B3RBWM25",
        "A1JX52",
        62.10,
        {2016: 0.05, 2017: 0.09, 2018: -0.05, 2019: 0.29, 2020: 0.06, 2021: 0.27,
         2022: -0.13, 2023: 0.17, 2024: 0.25, 2025: 0.06, 2026: 0.08},
    ),
    (
        "ISHARES CORE MSCI EM IMI UCITS ETF",
        "IE00BKM4GZ66",
        "A111X9",
        21.45,
        {2016: 0.03, 2017: 0.20, 2018: -0.10, 2019: 0.20, 2020: 0.08, 2021: 0.07,
         2022: -0.15, 2023: 0.07, 2024: 0.14, 2025: 0.10, 2026: 0.12},
    ),
]  # fmt: skip
COVID_RETURNS = {date(2020, 2, 1): -0.08, date(2020, 3, 1): -0.16, date(2020, 4, 1): 0.10}

# Summer holidays: (year, month, region, country code, currency, rate per EUR)
SUMMER = [
    (2017, 8, "Zeeland", "NL", "EUR", 1.0),
    (2018, 7, "Gardasee", "IT", "EUR", 1.0),
    (2019, 8, "Jütland", "DK", "DKK", 7.46),
    (2020, 8, "Usedom", "DE", "EUR", 1.0),
    (2021, 7, "Bayerischer Wald", "DE", "EUR", 1.0),
    (2022, 8, "Bretagne", "FR", "EUR", 1.0),
    (2023, 7, "Istrien", "HR", "EUR", 1.0),
    (2024, 8, "Südtirol", "IT", "EUR", 1.0),
    (2025, 7, "Fünen", "DK", "DKK", 7.46),
    (2026, 8, "Algarve", "PT", "EUR", 1.0),
]
SKI_YEARS = (2018, 2019, 2023, 2024, 2025, 2026)
SONDERTILGUNG = {y: 5_000_00 for y in range(2017, 2026) if y != 2022}  # 5 % a year allowed

Cat = str | list[tuple[str, int]] | None  # category key, manual split parts, None: transfer


# --- helpers ---------------------------------------------------------------------------


def de(cents: int) -> str:
    """German amount: -1.234,56"""
    s = f"{abs(cents) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"-{s}" if cents < 0 else s


def add_months(d: date, n: int) -> date:
    total = d.year * 12 + d.month - 1 + n
    return date(total // 12, total % 12 + 1, 1)


def month_end(d: date) -> date:
    return add_months(d, 1) - timedelta(days=1)


def bank_day(d: date) -> date:
    """Next weekday (back to Friday at a month end)."""
    if d.weekday() < 5:
        return d
    later = d + timedelta(days=7 - d.weekday())
    return later if later.month == d.month else d - timedelta(days=d.weekday() - 4)


def iban_spaced(iban: str) -> str:
    return " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))


def ym(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


@dataclass
class Ing:
    """A booking on an ING statement (Girokonto or Extra-Konto)."""

    day: date
    cents: int
    btype: str
    party: str
    lines: list[str]  # purpose lines; the first one goes on the value date line
    cat: Cat
    value_day: date | None = None
    seq: int = 0

    def render(self) -> list[str]:
        head = f"{self.day:%d.%m.%Y} {self.btype} {self.party}".rstrip()
        valuta = f"{(self.value_day or self.day):%d.%m.%Y}"
        rest = self.lines[1:]
        if self.lines:
            valuta += f" {self.lines[0]}"
        return [f"{head} {de(self.cents)}", valuta, *rest]


@dataclass
class Rev:
    """A booking on a Revolut statement."""

    day: date
    cents: int
    desc: str
    details: list[tuple[str, str]]
    cat: Cat
    seq: int = 0


@dataclass
class CashEntry:
    day: date
    cents: int
    payee: str
    purpose: str
    cat: str


@dataclass
class Statement:
    """A statement as the importer returns it, plus the intended category per booking."""

    account: str
    name: str
    content: str
    parsed: ParsedStatement
    cats: list[Cat]
    imported_at: datetime
    month: date


@dataclass
class World:
    """The family's life, month by month. Produces bookings per account."""

    seed: int = SEED
    giro: list[Ing] = field(default_factory=list)
    extra: list[Ing] = field(default_factory=list)
    revolut: list[Rev] = field(default_factory=list)
    cash: dict[str, list[CashEntry]] = field(default_factory=lambda: {"A": [], "B": []})
    trades: list[tuple[date, int, float, int]] = field(default_factory=list)  # day, etf, qty, ct
    dividends: list[tuple[date, int]] = field(default_factory=list)
    loan: list[ParsedTransaction] = field(default_factory=list)
    loan_opening: int = -316_400_00

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self.fake = Faker("de_DE")
        self.fake.seed_instance(self.seed)
        f = self.fake
        self.surname = f.last_name()
        self.father, self.mother = f.first_name_male(), f.first_name_female()
        self.kid1, self.kid2 = f.first_name_female(), f.first_name_male()
        self.dog = self.rng.choice(["Bruno", "Balu", "Emma", "Luna", "Paul", "Frieda"])
        self.street = f"{f.street_name()} {self.rng.randint(2, 48)}"
        self.postcode = self.rng.choice(POSTCODES)
        self.holders = f"{self.father} und {self.mother} {self.surname}"
        self.employers = [self.company() for _ in range(3)]  # Papa 1, Papa 2, Mama
        self.people = {
            k: f"{f.first_name()} {f.last_name()}"
            for k in ("babysitter", "buyer", "godchild", "friend")
        }
        self.vet = f"Tierarztpraxis Dr. {f.last_name()}"
        self.dentist = f"Zahnarztpraxis Dr. {f.last_name()}"
        self.orthodontist = f"Dr. med. dent. {f.last_name()} Kieferorthopädie"
        self.heating = f"{f.last_name()} Heizung und Sanitär GmbH"
        self.electrician = f"Elektro {f.last_name()} GmbH"
        self.painter = f"Malerbetrieb {f.last_name()}"
        self.sweep_name = f"Bezirksschornsteinfeger {f.last_name()}"
        self.car_dealer = f"Autohaus {f.last_name()} GmbH"
        self.edeka = f.last_name().upper()
        self.mandates: dict[str, str] = {}
        self.kg_no = f"135FK{self.rng.randint(100000, 999999)}"
        self.cash_balance = {"A": 60_00, "B": 40_00}
        self.revolut_balance = 0
        self.extra_opening = self.extra_balance = 9_800_00
        self.giro_opening = 2_350_00
        self.salary = {"A": 2_900_00, "B": 0}
        self.order_no = 71_000_000
        self._seq = 0
        self._levels = self._price_levels()
        self._etf_prices = [self._etf_path(e) for e in ETFS]
        self._loan_schedule()

    def company(self) -> str:
        """A company name with a legal form ("Krause GmbH & Co. KG")."""
        while True:
            name = self.fake.company()
            if any(form in name for form in (" GmbH", " AG", " KG", " SE")):
                return name

    # -- randomness ----------------------------------------------------------------

    def pois(self, lam: float) -> int:
        if lam <= 0:
            return 0
        limit, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= limit:
                return k
            k += 1

    def eur(self, lo: float, hi: float, d: date, mode: float | None = None) -> int:
        """Random amount in cents between lo and hi euros (2016 prices) at the price level."""
        value = self.rng.triangular(lo, hi, mode if mode is not None else (lo + hi) / 3)
        return max(1, round(value * self.level(d) * 100))

    def day_in(self, first: date, lo: int = 1, hi: int = 31, *, sunday: bool = False) -> date:
        last = month_end(first).day
        d = first.replace(day=self.rng.randint(min(lo, last), min(hi, last)))
        if d.weekday() == 6 and not sunday:
            d -= timedelta(days=1)
        return d

    def digits(self, n: int) -> str:
        return "".join(self.rng.choice("0123456789") for _ in range(n))

    def mandate(self, party: str) -> str:
        if party not in self.mandates:
            self.mandates[party] = self.fake.bothify("??######-####").upper()
        return self.mandates[party]

    def level(self, d: date) -> float:
        return self._levels.get(date(d.year, d.month, 1), 1.0)

    def _price_levels(self) -> dict[date, float]:
        levels, level, m = {}, 1.0, FIRST_MONTH
        while m <= date(2027, 12, 1):
            levels[m] = level
            level *= (1 + INFLATION.get(m.year, 2.0) / 100) ** (1 / 12)
            m = add_months(m, 1)
        return levels

    def _etf_path(self, etf: tuple) -> dict[date, float]:
        """Price at the start of each month: yearly returns spread over noisy months."""
        returns: dict[date, float] = {}
        m = FIRST_MONTH
        while m <= TODAY:
            year_months = [x for x in range(1, 13) if date(m.year, x, 1) >= FIRST_MONTH]
            year_months = [x for x in year_months if date(m.year, x, 1) <= TODAY]
            noise = [self.rng.gauss(0, 0.03) for _ in year_months]
            mean = sum(noise) / len(noise)
            total = math.log(1 + etf[4][m.year])
            for x, n in zip(year_months, noise, strict=True):
                returns[date(m.year, x, 1)] = total / len(year_months) + n - mean
            m = date(m.year + 1, 1, 1)
        for d, r in COVID_RETURNS.items():
            returns[d] = math.log(1 + r)
        prices, price, m = {}, etf[3], FIRST_MONTH
        while m <= add_months(TODAY, 1):
            prices[m] = price
            price *= math.exp(returns.get(m, 0.005))
            m = add_months(m, 1)
        return prices

    def etf_price(self, i: int, d: date) -> float:
        start = date(d.year, d.month, 1)
        a, b = self._etf_prices[i][start], self._etf_prices[i][add_months(start, 1)]
        t = (d.day - 1) / month_end(d).day
        return round(a * (b / a) ** t, 2)

    def _loan_schedule(self) -> None:
        """Annuity loan since 2015: 1.65 % interest, 1,240 EUR per month."""
        self.mortgage: dict[date, tuple[int, int]] = {}  # month -> interest, principal
        balance, m = self.loan_opening, date(2016, 1, 1)
        while m <= TODAY:
            interest = round(-balance * 0.0165 / 12)
            principal = 1_240_00 - interest
            day = bank_day(m.replace(day=min(30, month_end(m).day)))
            self.mortgage[m] = (interest, principal)
            self.loan += [
                ParsedTransaction(day, -interest, "Zinsen", value_date=day, raw_text=""),
                ParsedTransaction(
                    day,
                    1_240_00,
                    "Lastschrifteinzug",
                    self.surname,
                    f"ING-DIBA AG {LOAN_NO} {day:%d.%m.%Y} ZINSEN {de(interest)} "
                    f"TILGUNG {de(principal)}",
                    value_date=day,
                ),
            ]
            balance += principal
            if m.month == 12 and m.year in SONDERTILGUNG:
                extra_day = date(m.year, 12, 15)
                self.loan.append(
                    ParsedTransaction(
                        extra_day,
                        SONDERTILGUNG[m.year],
                        "Sondertilgung",
                        self.surname,
                        f"Sondertilgung Darlehen {LOAN_NO}",
                        value_date=extra_day,
                    )
                )
                balance += SONDERTILGUNG[m.year]
            m = add_months(m, 1)
        self.loan.sort(key=lambda t: t.booking_date)

    # -- booking helpers -------------------------------------------------------------

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def ing(
        self,
        day: date,
        cents: int,
        btype: str,
        party: str,
        lines: list[str],
        cat: Cat,
        *,
        account: str = "giro",
        mandate: bool = False,
        reference: str | None = None,
        value_day: date | None = None,
    ) -> None:
        lines = list(lines)
        if mandate:
            lines.append(f"Mandat:{self.mandate(party)}")
        if reference:
            lines.append(f"Referenz:{reference}")
        entry = Ing(day, cents, btype, party, lines, cat, value_day, self._next())
        (self.giro if account == "giro" else self.extra).append(entry)

    def debit(self, day: date, party: str, cents: int, purpose: str, cat: Cat) -> None:
        """Direct debit (SEPA Lastschrift) with mandate and reference."""
        ref = self.fake.bothify("##########??").upper()
        self.ing(
            bank_day(day), -cents, "Lastschrift", party, [purpose], cat, mandate=True, reference=ref
        )

    def transfer(self, day: date, party: str, cents: int, purpose: str, cat: Cat) -> None:
        """Outgoing bank transfer (Ueberweisung, from 2025 as instant payment)."""
        btype = "Echtzeitüberweisung" if day.year >= 2025 else "Ueberweisung"
        self.ing(bank_day(day), -cents, btype, party, [purpose], cat)

    def credit(
        self, day: date, party: str, cents: int, purpose: str, cat: Cat, btype: str = "Gutschrift"
    ) -> None:
        self.ing(bank_day(day), cents, btype, party, [purpose], cat)

    def card(
        self,
        day: date,
        merchant: str,
        cents: int,
        cat: Cat,
        *,
        who: str = "",
        city: str = CITY_UPPER,
        country: str = "DE",
        refund: bool = False,
    ) -> None:
        """Visa debit card payment on the Girokonto, booked one or two days later."""
        card = CARD_A if (who or self.rng.choice("AAB")) == "A" else CARD_B
        wallet = ""
        if day.year >= 2019 and self.rng.random() < 0.45:
            wallet = " Google Pay" if card == CARD_A else " Apple Pay"
        booked = bank_day(day + timedelta(days=self.rng.choice([1, 1, 2, 3])))
        self.ing(
            booked,
            cents if refund else -cents,
            "Gutschrift" if refund else "Lastschrift",
            f"VISA {merchant}",
            [
                f"NR XXXX {card} {city} {country} KAUFUMSATZ {day:%d.%m} {cents / 100:.2f} "
                f"{self.digits(6)}",
                f"ARN{self.digits(23)}{wallet}",
            ],
            cat,
        )

    def rev(
        self,
        day: date,
        desc: str,
        cents: int,
        cat: Cat,
        *,
        legal: str = "",
        city: str = "Frankfurt",
        country: str = "DE",
        fx: tuple[str, float] | None = None,
    ) -> None:
        """Revolut card payment (Papa); before the account existed, the Visa card."""
        if day < REVOLUT_SINCE:
            self.card(
                day, desc.upper(), cents, cat, who="A", city=city.upper()[:12], country=country
            )
            return
        details = []
        if fx and fx[0] != "EUR":
            rate = f"{fx[1]:.2f}".replace(".", ",")
            ecb = f"{fx[1] * 1.002:.2f}".replace(".", ",")
            foreign = de(round(cents * fx[1]))
            details.append(
                (
                    "Revolut Kurs",
                    f"1,00€ = {rate} {fx[0]} (ECB-Kurs* 1,00€ = {ecb} {fx[0]}) {foreign} {fx[0]}",
                )
            )
        details += [("An", f"{legal or desc}, {city}"), ("Karte", REVOLUT_CARD)]
        self.revolut.append(Rev(day, -cents, desc, details, cat, self._next()))

    def cash_out(self, who: str, day: date, payee: str, cents: int, cat: str, purpose="") -> None:
        self.cash[who].append(CashEntry(day, -cents, payee, purpose, cat))
        self.cash_balance[who] -= cents

    def pick(self, items: list[tuple]) -> tuple:
        """Choose from (weight, ...) tuples."""
        return self.rng.choices(items, weights=[i[0] for i in items])[0]

    # -- life --------------------------------------------------------------------------

    def run(self, until: date = TODAY) -> None:
        m = FIRST_MONTH
        while m < until:
            self.month(m)
            m = add_months(m, 1)
        cutoff = until - timedelta(days=1)
        self.giro = [e for e in self.giro if e.day <= cutoff]
        self.extra = [e for e in self.extra if e.day <= cutoff]
        self.revolut = [e for e in self.revolut if e.day <= cutoff]
        for who in self.cash:
            self.cash[who] = [e for e in self.cash[who] if e.day <= cutoff]

    def month(self, m: date) -> None:
        self.income(m)
        self.housing(m)
        self.contracts(m)
        self.children(m)
        self.pet(m)
        self.car(m)
        self.everyday(m)
        self.leisure(m)
        self.seasons(m)
        self.one_offs(m)
        self.saving(m)
        self.revolut_month(m)
        self.cash_month(m)
        self.sweep(m)

    @staticmethod
    def covid(d: date) -> float:
        """Activity outside the home: lockdowns 2020/21, home office afterwards."""
        if date(2020, 3, 16) <= d < date(2020, 6, 1) or date(2020, 11, 1) <= d < date(2021, 6, 1):
            return 0.25
        return 0.8 if date(2020, 6, 1) <= d < date(2022, 4, 1) else 1.0

    def away(self, m: date) -> float:
        """Share of the month spent at home (less everyday spending during holidays)."""
        summer = any((y, mo) == (m.year, m.month) for y, mo, *_ in SUMMER)
        return 0.6 if summer else 0.85 if (m.month == 2 and m.year in SKI_YEARS) else 1.0

    def income(self, m: date) -> None:
        rng = self.rng
        # Papa: raise every April, new employer in April 2019
        if m.month == 4 and m > FIRST_MONTH:
            self.salary["A"] = round(
                self.salary["A"] * (1.08 if m.year == 2019 else 1 + rng.uniform(0.018, 0.032))
            )
            if m.year == 2023:
                self.salary["A"] = round(self.salary["A"] * 1.02)
        employer_a = self.employers[0 if m < date(2019, 4, 1) else 1]
        pay = self.salary["A"]
        if m.month == 11:
            pay += round(self.salary["A"] * 0.5)  # Weihnachtsgeld
        if m == date(2022, 9, 1):
            pay += 300_00 - 72_00  # Energiepreispauschale (taxed)
        if m == date(2023, 2, 1):
            pay += 1_500_00  # Inflationsausgleichsprämie
        day = bank_day(m.replace(day=27))
        self.credit(
            day, employer_a, pay, f"Lohn/Gehalt {m:%m/%y}", "einkommen.gehalt", btype="Gehalt/Rente"
        )
        # Mama: parental allowance after the second child, then part-time, more hours later
        if m < date(2017, 4, 1):
            self.credit(
                bank_day(m.replace(day=28)),
                "Hessisches Amt fuer Versorgung und Soziales Darmstadt",
                1_084_00,
                f"Elterngeld {self.kid2} {m:%m/%Y} AZ {self.kg_no[-6:]}",
                "einkommen.sonstige_einnahmen",
            )
        else:
            if m == date(2017, 4, 1):
                self.salary["B"] = 1_240_00
            elif m == date(2020, 8, 1):
                self.salary["B"] = round(self.salary["B"] * 1.35)
            elif m.month == 1:
                self.salary["B"] = round(self.salary["B"] * (1 + rng.uniform(0.012, 0.03)))
            pay = self.salary["B"] + (round(self.salary["B"] * 0.4) if m.month == 6 else 0)
            if m == date(2024, 1, 1):
                pay += 1_000_00
            self.credit(
                bank_day(month_end(m)),
                self.employers[2],
                pay,
                f"Lohn/Gehalt {m:%m/%y}",
                "einkommen.gehalt",
                btype="Gehalt/Rente",
            )
        # Kindergeld for two children
        rate = 0
        for since, value in [
            (date(2016, 1, 1), 190_00), (date(2017, 1, 1), 192_00), (date(2018, 1, 1), 194_00),
            (date(2019, 7, 1), 204_00), (date(2021, 1, 1), 219_00), (date(2023, 1, 1), 250_00),
            (date(2025, 1, 1), 255_00), (date(2026, 1, 1), 259_00),
        ]:  # fmt: skip
            if m >= since:
                rate = value
        bonus = {
            date(2020, 9, 1): 200_00,
            date(2020, 10, 1): 100_00,
            date(2021, 5, 1): 150_00,
            date(2022, 7, 1): 100_00,
        }.get(m, 0)
        self.credit(
            bank_day(m.replace(day=9)),
            "Bundesagentur fuer Arbeit - Familienkasse",
            2 * (rate + bonus),
            f"KG-Nr. {self.kg_no} Kindergeld {m:%m/%Y}" + (" inkl. Kinderbonus" if bonus else ""),
            "einkommen.kindergeld",
        )
        if m.month == 8 and m.year >= 2017:
            refund = round(rng.uniform(380, 1650) * 100)
            self.credit(
                bank_day(m.replace(day=rng.randint(5, 25))),
                "Finanzamt Darmstadt",
                refund,
                f"EST {m.year - 1} Steuernr. 007/{self.kg_no[-6:]}/0815 Erstattung",
                "einkommen.erstattungen",
            )

    def housing(self, m: date) -> None:
        rng = self.rng
        interest, principal = self.mortgage[m]
        day = bank_day(m.replace(day=min(30, month_end(m).day)))
        self.ing(
            day,
            -(interest + principal),
            "Lastschrift",
            self.surname,
            [f"ING-DIBA AG {LOAN_NO} {day:%d.%m.%Y} ZINSEN {de(interest)} TILGUNG", de(principal)],
            None,
        )
        # Energy: gas/water with ENTEGA, electricity with LichtBlick, Octopus from April 2023
        gas = self.stepped(
            m,
            [
                (2016, 1, 96),
                (2021, 1, 104),
                (2022, 9, 218),
                (2023, 1, 246),
                (2024, 3, 172),
                (2025, 3, 158),
            ],
        )
        self.debit(
            m.replace(day=15),
            "ENTEGA Plus GmbH",
            gas * 100,
            f"Abschlag Erdgas/Wasser {m:%m/%Y} Vertragskonto 2004{self.kg_no[-6:]}",
            "wohnen.energie",
        )
        power = self.stepped(
            m,
            [
                (2016, 1, 68),
                (2019, 1, 74),
                (2022, 7, 96),
                (2023, 1, 138),
                (2023, 4, 112),
                (2025, 4, 104),
            ],
        )
        supplier = "LichtBlick SE" if m < date(2023, 4, 1) else "Octopus Energy Germany GmbH"
        self.debit(
            m.replace(day=3),
            supplier,
            power * 100,
            f"Abschlag Strom {m:%m/%Y} Kd-Nr. {self.kg_no[-5:]}",
            "wohnen.energie",
        )
        if m.month == 3 and m.year >= 2017:  # yearly bills: refund or additional payment
            diff = round(rng.gauss(-40, 90) * 100) if m.year != 2023 else -312_40
            if diff:
                purpose = f"Jahresabrechnung {m.year - 1} Vertragskonto 2004{self.kg_no[-6:]}"
                if diff > 0:
                    self.credit(
                        m.replace(day=18), "ENTEGA Plus GmbH", diff, purpose, "wohnen.energie"
                    )
                else:
                    self.debit(
                        m.replace(day=18), "ENTEGA Plus GmbH", -diff, purpose, "wohnen.energie"
                    )
        if m.month in (2, 5, 8, 11):  # Grundbesitzabgaben: property tax and municipal fees
            tax = 142_50 if m.year < 2025 else 168_20
            fees = round(118_30 * self.level(m))
            self.debit(
                m.replace(day=15),
                "Wissenschaftsstadt Darmstadt",
                tax + fees,
                f"Grundbesitzabgaben {m.year} Kassenzeichen 6.{self.kg_no[-6:]}.3",
                [("steuern.grundsteuer", -tax), ("wohnen.nebenkosten", -fees)],
            )
        if m.month == 6:
            self.transfer(
                m.replace(day=rng.randint(3, 20)),
                self.sweep_name,
                self.eur(68, 82, m),
                f"Rechnung Feuerstaettenschau {m.year}",
                "wohnen.nebenkosten",
            )
        if m.month == 10:
            self.transfer(
                m.replace(day=rng.randint(10, 28)),
                self.heating,
                self.eur(165, 230, m),
                f"Rechnung {self.fake.bothify('RE-####')} Wartung Gastherme",
                "wohnen.handwerker",
            )
        if m.month == 10:
            self.debit(
                m.replace(day=1),
                "Gothaer Allgemeine Versicherung AG",
                round(
                    self.stepped(
                        m, [(2016, 1, 392), (2023, 1, 468), (2024, 1, 551), (2025, 1, 612)]
                    )
                    * 100
                ),
                f"Wohngebaeudeversicherung {m.year}/{m.year + 1} Vers.-Nr. GO-{self.kg_no[-6:]}",
                "versicherungen.grundeigentum",
            )
        if m.month == 3:
            self.debit(
                m.replace(day=1),
                "Allianz Versicherungs-AG",
                round(self.stepped(m, [(2016, 1, 214), (2022, 1, 236), (2024, 1, 268)]) * 100),
                f"Hausrat/Privathaftpflicht {m.year} Vers.-Nr. AS-{self.digits(9)}",
                "versicherungen.haftpflicht_hausrat",
            )

    @staticmethod
    def stepped(m: date, table: list[tuple[int, int, float]]) -> float:
        value = table[0][2]
        for y, mo, v in table:
            if m >= date(y, mo, 1):
                value = v
        return value

    def contracts(self, m: date) -> None:
        """Telephone, subscriptions, insurances, broadcasting fee, bank fees."""
        tel = self.stepped(m, [(2016, 1, 39.95), (2021, 7, 44.95), (2024, 7, 49.95)])
        self.debit(
            m.replace(day=4),
            "Telekom Deutschland GmbH",
            round(tel * 100),
            f"Rechnung {m:%m/%Y} MagentaZuhause Kd-Nr. {self.kg_no[-6:]}",
            "wohnen.internet_telefon",
        )
        self.debit(
            m.replace(day=12),
            "Vodafone GmbH",
            round(self.stepped(m, [(2016, 1, 29.99), (2022, 4, 34.99)]) * 100),
            f"Mobilfunk {self.father} Rechnung {m:%m/%Y}",
            "wohnen.internet_telefon",
        )
        self.debit(
            m.replace(day=21),
            "congstar",
            1500 if m.year < 2022 else 1700,
            f"congstar Rechnung {m:%m.%Y} {self.mother}",
            "wohnen.internet_telefon",
        )
        self.debit(
            m.replace(day=1),
            "Hannoversche Lebensversicherung AG",
            23_40,
            f"Risikolebensversicherung Vers.-Nr. L{self.kg_no[-7:]}",
            "versicherungen.kranken_leben",
        )
        if m >= date(2019, 1, 1):
            self.debit(
                m.replace(day=1),
                "Barmenia Krankenversicherung AG",
                3_180,
                "Zahnzusatzversicherung Beitrag",
                "versicherungen.kranken_leben",
            )
        self.debit(
            m.replace(day=14),
            "Netflix International B.V.",
            round(
                self.stepped(
                    m, [(2016, 1, 9.99), (2019, 4, 11.99), (2022, 2, 13.99), (2024, 3, 17.99)]
                )
                * 100
            ),
            f"Netflix Monatsabo {self.digits(8)}",
            "freizeit.abos",
        )
        self.debit(
            m.replace(day=19),
            "Spotify AB",
            round(self.stepped(m, [(2016, 1, 14.99), (2023, 8, 17.99), (2025, 1, 19.99)]) * 100),
            "Spotify Premium Family",
            "freizeit.abos",
        )
        if date(2020, 4, 1) <= m < date(2023, 7, 1):
            self.debit(
                m.replace(day=24),
                "Disney Plus",
                699 if m.year < 2021 else 899,
                "Disney+ Abo",
                "freizeit.abos",
            )
        if m >= date(2019, 9, 1):
            self.debit(
                m.replace(day=2),
                "VRM GmbH & Co. KG",
                round(
                    self.stepped(m, [(2019, 1, 14.90), (2022, 1, 17.90), (2024, 1, 19.90)]) * 100
                ),
                "Darmstaedter Echo E-Paper Abo",
                "freizeit.abos",
            )
        if m.month == 2:
            self.debit(
                m.replace(day=9),
                "AMAZON PAYMENTS EUROPE S.C.A.",
                6900 if m < date(2022, 9, 1) else 8990,
                "Amazon Prime Mitgliedschaft",
                "freizeit.abos",
            )
            self.debit(
                m.replace(day=3),
                "ADAC e.V.",
                9400 if m.year < 2023 else 10900,
                f"ADAC Plus-Mitgliedschaft {m.year}",
                "mobilitaet.auto",
            )
        if m.month in (2, 5, 8, 11):
            self.debit(
                m.replace(day=15),
                "Rundfunk ARD, ZDF, DRadio",
                5250 if m < date(2021, 8, 1) else 5508,
                f"Rundfunkbeitrag Beitragsnr. {self.digits(9)}",
                "gebuehren.rundfunkbeitrag",
            )
        if date(2017, 1, 1) <= m < date(2020, 4, 1) or m >= date(2021, 7, 1):
            self.debit(
                m.replace(day=6),
                "FitX Deutschland GmbH",
                1999 if m.year < 2023 else 2499,
                f"Mitgliedsbeitrag {m:%m/%Y}",
                "freizeit.sport_hobby",
            )
        if m >= date(2022, 3, 1):
            self.debit(
                m.replace(day=1),
                "Urban Sports Club GmbH",
                3900 if m.year < 2024 else 4900,
                f"Mitgliedschaft {self.mother} {m:%m/%Y}",
                "freizeit.sport_hobby",
            )
        if m >= date(2023, 5, 1):
            self.debit(
                m.replace(day=1),
                "DB Vertrieb GmbH",
                4900 if m.year < 2025 else 5800,
                f"Deutschlandticket {m:%m/%Y} {self.father}",
                "mobilitaet.oepnv",
            )
        if m >= date(2023, 6, 1):
            self.ing(
                month_end(m),
                -99,
                "Entgelt",
                "",
                [f"Entgelt girocard {m:%m/%Y}"],
                "gebuehren.bankentgelte",
            )
        if m.month == 5 and m > DOG_SINCE:
            self.debit(
                m.replace(day=2),
                "Uelzener Allgemeine Versicherungs-Gesellschaft",
                6840 if m.year < 2023 else 8520,
                f"Hundehalterhaftpflicht {self.dog} {m.year}",
                "haustier.versicherung",
            )

    def children(self, m: date) -> None:
        rng = self.rng
        city = "Wissenschaftsstadt Darmstadt"
        for name, birth, care, school, gym in (
            (self.kid1, KID1_BIRTH, date(2015, 9, 1), date(2020, 8, 1), date(2024, 8, 1)),
            (self.kid2, KID2_BIRTH, date(2017, 8, 1), date(2022, 8, 1), date(2026, 8, 1)),
        ):
            age = (m - birth).days / 365.25
            if care <= m < school:
                fee = 335 if age < 3 else 165 if m < date(2018, 8, 1) else 52
                self.debit(
                    m.replace(day=5),
                    city,
                    round(fee * self.level(m) * 100),
                    f"Kita-Gebuehren {name} {m:%m/%Y} Kassenzeichen 5.{self.digits(6)}",
                    "kinder.betreuung",
                )
            elif school <= m < gym:
                self.debit(
                    m.replace(day=5),
                    city,
                    round(96 * self.level(m) * 100),
                    f"Betreuung Ganztag {name} {m:%m/%Y}",
                    "kinder.betreuung",
                )
            if m >= school:
                days = 6 if m.month == 8 else 10 if m.month in (1, 10, 12) else 16
                self.debit(
                    m.replace(day=20),
                    "Schulverpflegung Kinderkueche GmbH",
                    round(days * 3.6 * self.level(m) * 100),
                    f"Essensgeld {name} {m:%m/%Y}",
                    "kinder.schulessen",
                )
            if m >= gym:
                self.debit(
                    m.replace(day=11),
                    "MEDIONmobile ALDI TALK",
                    799,
                    f"Prepaid-Aufladung {name}",
                    "kinder.handy",
                )
        if m.month == 1:
            self.debit(
                m.replace(day=15),
                "Turn- und Sportverein 1846 Darmstadt e.V.",
                round(self.stepped(m, [(2016, 1, 168), (2021, 1, 204), (2024, 1, 236)]) * 100),
                f"Mitgliedsbeitrag Familie {m.year}",
                "kinder.weiterbildung_sport",
            )
        if m >= date(2021, 9, 1) and m.month != 8:
            self.debit(
                m.replace(day=3),
                "Musikschule am Woog e.V.",
                round(58 * self.level(m)) * 100,
                f"Unterrichtsgebuehr Klavier {self.kid1} {m:%m/%Y}",
                "kinder.weiterbildung_sport",
            )
        if date(2017, 4, 1) <= m < date(2020, 3, 1) and rng.random() < 0.6:
            self.transfer(
                self.day_in(m, 3, 26),
                self.people["babysitter"],
                rng.choice([40, 50, 60, 80]) * 100,
                "Babysitting danke!",
                "kinder.betreuung",
            )
        if m.month in (8, 9) and m >= date(2020, 8, 1):  # school supplies
            for _ in range(rng.randint(1, 2)):
                self.card(
                    self.day_in(m, 10, 28),
                    "MCPAPER",
                    self.eur(12, 60, m),
                    "kinder.weiterbildung_sport",
                )
        for when in (date(2020, 7, 1), date(2022, 7, 1)):
            if m == when:  # school enrolment: satchel and party
                self.debit(
                    self.day_in(m, 2, 12),
                    "PayPal Europe S.a.r.l. et Cie S.C.A",
                    24_990,
                    self.paypal_purpose("ergobag GmbH"),
                    "kinder.kleidung_spielzeug",
                )
                self.card(
                    self.day_in(m, 14, 28),
                    "TORTEN UND MEHR KONDITOREI",
                    6_480,
                    "freizeit.restaurants",
                )
        for year in (2023, 2025):
            if m == date(year, 5, 1):
                self.transfer(
                    m.replace(day=8),
                    "Lichtenbergschule Foerderverein e.V.",
                    28_500,
                    f"Klassenfahrt {self.kid1} 7b",
                    "kinder.weiterbildung_sport",
                )
        if m >= date(2023, 2, 1) and m.month in (2, 5, 8, 11):
            self.transfer(
                m.replace(day=20),
                self.orthodontist,
                self.eur(140, 210, m),
                f"Rechnung KFO {self.kid1} Quartal",
                "gesundheit.aerzte",
            )
            self.credit(
                m.replace(day=27),
                "Barmenia Krankenversicherung AG",
                self.eur(60, 110, m),
                "Leistungsabrechnung KFO Erstattung",
                "einkommen.erstattungen",
            )

    def paypal_purpose(self, shop: str) -> str:
        return f"{self.digits(13)}/. {shop}, Ihr Einkauf bei {shop}, Artikel-"

    def pet(self, m: date) -> None:
        if m < DOG_SINCE.replace(day=1):
            return
        rng = self.rng
        if m == DOG_SINCE.replace(day=1):
            self.transfer(
                DOG_SINCE,
                "Tierschutzverein Darmstadt e.V.",
                350_00,
                f"Schutzgebuehr Hund {self.dog}",
                "haustier.zubehoer",
            )
            self.transfer(
                m.replace(day=20),
                f"Hundeschule {self.fake.last_name()}",
                240_00,
                "Welpenkurs 10er-Karte",
                "haustier.zubehoer",
            )
        for _ in range(self.pois(1.1)):
            self.card(self.day_in(m), "FRESSNAPF", self.eur(22, 80, m, 42), "haustier.futter")
        if rng.random() < 0.25:  # leash, toys, bed and dry food in bulk
            self.debit(
                self.day_in(m),
                "PayPal Europe S.a.r.l. et Cie S.C.A",
                self.eur(15, 80, m, 30),
                self.paypal_purpose("zooplus SE"),
                "haustier.zubehoer",
            )
        if m.month in (5, 10):
            self.card(
                self.day_in(m), "HUNDESALON PFOTENGLUECK", self.eur(45, 70, m), "haustier.zubehoer"
            )
        if m.month == 4 or rng.random() < 0.12:
            self.card(
                self.day_in(m),
                self.vet.upper(),
                self.eur(45, 240, m, 70),
                "haustier.tierarzt",
                who="B",
            )
        if m in (date(2022, 8, 1), date(2024, 11, 1)):
            self.transfer(
                m.replace(day=19),
                self.vet,
                1_380_00 if m.year == 2022 else 1_145_50,
                f"Rechnung {self.fake.bothify('2###-###')} OP {self.dog}",
                "haustier.tierarzt",
            )
        if m.month == 2 and m >= date(2019, 1, 1):
            self.debit(
                m.replace(day=15),
                "Wissenschaftsstadt Darmstadt",
                108_00,
                f"Hundesteuer {m.year} Kassenzeichen 7.{self.digits(6)}",
                "haustier.hundesteuer",
            )

    def car(self, m: date) -> None:
        rng = self.rng
        price = self.stepped(
            m,
            [
                (2016, 1, 1.32),
                (2018, 6, 1.42),
                (2020, 4, 1.16),
                (2021, 3, 1.48),
                (2022, 3, 2.08),
                (2022, 9, 1.86),
                (2023, 1, 1.79),
                (2024, 1, 1.74),
                (2025, 1, 1.69),
                (2026, 1, 1.73),
            ],
        )
        stations = [
            (3, "ARAL STATION {n}"),
            (3, "SHELL {n}"),
            (2, "ESSO STATION {n}"),
            (2, "JET {city}"),
            (1, "TOTALENERGIES {n}"),
        ]
        for _ in range(self.pois(2.0 * max(self.covid(m), 0.6) * self.away(m))):
            name = self.pick(stations)[1].format(n=self.digits(5), city=CITY_UPPER)
            self.card(
                self.day_in(m, sunday=True),
                name,
                round(rng.uniform(30, 52) * price * 100),
                "mobilitaet.auto",
            )
        if m.month in (4, 10):
            self.card(
                self.day_in(m), "ATU AUTO-TEILE-UNGER", self.eur(25, 45, m), "mobilitaet.auto"
            )
        if m.month == 9:
            self.card(
                self.day_in(m),
                self.car_dealer.upper().replace(" GMBH", ""),
                self.eur(260, 720, m, 380),
                "mobilitaet.auto",
                who="A",
            )
        if m.month == 9 and (m.year % 2 == 1 and m.year <= 2021 or m.year == 2025):
            self.card(
                self.day_in(m), "DEKRA AUTOMOBIL GMBH", self.eur(110, 125, m), "mobilitaet.auto"
            )
        if m.month == 1:
            self.debit(
                m.replace(day=2),
                "HUK-COBURG",
                round(
                    self.stepped(
                        m,
                        [
                            (2016, 1, 486),
                            (2019, 1, 452),
                            (2023, 1, 688),
                            (2024, 1, 742),
                            (2026, 1, 798),
                        ],
                    )
                    * 100
                ),
                f"Kfz-Versicherung {m.year} DA-{self.surname[0]}{self.digits(4)}",
                "mobilitaet.auto",
            )
        if m.month == 7:
            self.debit(
                m.replace(day=10),
                "Hauptzollamt Darmstadt",
                24_800 if m.year <= 2022 else 11_600,
                f"Kraftfahrzeugsteuer DA-{self.surname[0]} {m.year}",
                "mobilitaet.auto",
            )
        for _ in range(self.pois(0.3 * self.covid(m))):
            self.card(
                self.day_in(m),
                self.rng.choice(["APCOA PARKING", "CONTIPARK"]),
                self.eur(2, 14, m),
                "mobilitaet.auto",
            )
        for _ in range(self.pois(0.2)):
            self.card(self.day_in(m), "MR. WASH AUTOSERVICE", self.eur(9, 22, m), "mobilitaet.auto")
        if m == NEW_CAR.replace(day=1):  # new car, financed from the Extra-Konto
            self.move_from_extra(m.replace(day=1), 24_000_00, "Autokauf")
            self.transfer(
                NEW_CAR,
                self.car_dealer,
                31_450_00,
                f"Kaufvertrag {self.fake.bothify('KV-#####')} Skoda Octavia Combi",
                "mobilitaet.auto",
            )
            self.credit(
                NEW_CAR + timedelta(days=5),
                self.people["buyer"],
                6_800_00,
                "Kaufpreis VW Touran Bj. 2012",
                "einkommen.sonstige_einnahmen",
            )
        # bicycles
        if m == date(2021, 4, 1):
            self.card(
                m.replace(day=17), "FAHRRAD XXL FRANZ", 2_399_00, "mobilitaet.fahrrad", who="B"
            )
        if m.month == 4 and m.year >= 2018:
            self.card(
                self.day_in(m), "RADHAUS AM MARKT", self.eur(35, 120, m), "mobilitaet.fahrrad"
            )
        if m.month == 12 and m.year in (2019, 2022, 2025):
            self.card(
                self.day_in(m, 1, 20),
                "ROSE BIKES GMBH",
                self.eur(320, 480, m),
                "mobilitaet.fahrrad",
            )
        if m < date(2023, 5, 1):
            for _ in range(self.pois(0.5)):
                self.card(
                    self.day_in(m),
                    "RMV TICKET HEAG MOBILO",
                    self.eur(2.6, 9, m),
                    "mobilitaet.oepnv",
                    who="B",
                )

    def everyday(self, m: date) -> None:
        rng, home = self.rng, self.away(m)
        xmas = 1.25 if m.month == 12 else 1.0
        supermarkets = [
            (30, "REWE {city}", 14, 140),
            (18, "ALDI SUED", 18, 110),
            (14, "LIDL DIENSTLEISTUNG", 12, 95),
            (10, "EDEKA {surname}", 9, 90),
            (6, "TEGUT {n}", 8, 60),
            (6, "ALNATURA {n}", 12, 70),
            (5, "PENNY {n}", 5, 40),
            (4, "KAUFLAND {city}", 20, 120),
            (6, "HOFLADEN GUT KRANICHSTEIN", 8, 45),
            (4, "BIOLADEN SONNENBLUME", 10, 60),
            (4, "NAHKAUF {surname}", 6, 40),
        ]
        for _ in range(self.pois(9.0 * home * xmas)):
            _w, name, lo, hi = self.pick(supermarkets)
            name = name.format(city=CITY_UPPER, n=self.digits(4), surname=self.edeka)
            self.card(self.day_in(m), name, self.eur(lo, hi, m), "lebensmittel.supermarkt")
        bakeries = [
            (4, "BAECKEREI SCHWEIZER"),
            (3, "KAMPS BACKSTUBE"),
            (3, "BROTHAUS AM MARKT"),
            (2, "BAECKEREI GOELDNER"),
            (3, "BACKSTUBE WIENER"),
        ]
        for _ in range(self.pois(2.0 * home)):
            name = self.pick(bakeries)[1]
            self.card(
                self.day_in(m, sunday=True), name, self.eur(3, 16, m, 6), "lebensmittel.baeckerei"
            )
        for _ in range(self.pois(1.5 * home * xmas)):
            name = rng.choice(
                ["DM-DROGERIE MARKT", f"ROSSMANN {self.digits(4)}", f"MUELLER {CITY_UPPER}"]
            )
            self.card(self.day_in(m), name, self.eur(6, 55, m, 18), "lebensmittel.drogerie")
        for _ in range(self.pois(0.8 * home * (1.5 if m.month in (6, 7, 12) else 1))):
            name = rng.choice(["GETRAENKE HOFFMANN", f"TRINKGUT {CITY_UPPER}", "WEINHANDEL KRONE"])
            self.card(self.day_in(m), name, self.eur(12, 45, m), "lebensmittel.getraenke")
        for _ in range(self.pois(0.7)):
            self.card(
                self.day_in(m),
                f"{rng.choice(['LOEWEN', 'STERN', 'ROSEN', 'MARKT'])}-APOTHEKE",
                self.eur(4, 45, m, 12),
                "gesundheit.apotheke",
            )
        if rng.random() < 0.08:
            self.card(self.day_in(m), "FIELMANN AG", self.eur(80, 420, m), "gesundheit.aerzte")
        if m.month in (3, 9):
            for who in (self.father, self.mother):
                if rng.random() < 0.8:
                    self.transfer(
                        self.day_in(m, 5, 25),
                        self.dentist,
                        self.eur(85, 125, m),
                        f"Rechnung PZR {who} {self.fake.bothify('##-####')}",
                        "gesundheit.aerzte",
                    )
        # online shopping, PayPal and card
        online = 1.3 * (1.6 if self.covid(m) < 0.5 else 1.0) * (2.2 if m.month == 12 else 1.0)
        for _ in range(self.pois(online)):
            name = rng.choice(
                [
                    f"AMAZON.DE*{self.fake.bothify('??#?#??#').upper()}",
                    f"AMZN MKTP DE*{self.fake.bothify('#??##??#').upper()}",
                ]
            )
            self.card(self.day_in(m, sunday=True), name, self.eur(8, 95, m, 22), "sonstiges.online")
        if rng.random() < 0.18:
            self.card(
                self.day_in(m),
                f"AMAZON.DE*{self.fake.bothify('??#?#??#').upper()}",
                self.eur(10, 60, m),
                "sonstiges.online",
                refund=True,
            )
        shops = [
            (5, "eBay S.a r.l.", "sonstiges.online", 8, 90),
            (3, "Vinted UAB", "sonstiges.online", 5, 35),
            (4, "Zalando SE", "freizeit.kleidung", 25, 140),
            (2, "Otto GmbH & Co KG", "sonstiges.online", 20, 200),
            (2, "Lieferando.de", "freizeit.restaurants", 22, 55),
            (1, "Etsy Ireland UC", "sonstiges.geschenke", 12, 60),
            (1, "Thomann GmbH", "freizeit.sport_hobby", 15, 160),
            (1, "Tchibo GmbH", "sonstiges.sonstiges", 15, 80),
            (1, "myToys.de GmbH", "kinder.kleidung_spielzeug", 15, 90),
            (1, "Ticketmaster GmbH", "freizeit.sonstiges", 40, 160),
            (1, "Steam Games", "freizeit.medien", 5, 40),
        ]
        for _ in range(self.pois(1.4 * (1.5 if m.month == 12 else 1.0))):
            _w, shop, cat, lo, hi = self.pick(shops)
            self.debit(
                self.day_in(m),
                "PayPal Europe S.a.r.l. et Cie S.C.A",
                self.eur(lo, hi, m),
                self.paypal_purpose(shop),
                cat,
            )
        # clothes and things for the house
        clothes = [
            (3, "H&M {n}", "freizeit.kleidung"),
            (2, "C&A MODE {n}", "freizeit.kleidung"),
            (2, "DEICHMANN {n}", "freizeit.kleidung"),
            (1, "TK MAXX", "freizeit.kleidung"),
            (1, "PEEK UND CLOPPENBURG", "freizeit.kleidung"),
            (2, "JAKO-O", "kinder.kleidung_spielzeug"),
            (2, "TAKKO FASHION", "kinder.kleidung_spielzeug"),
            (1, "DECATHLON {city}", "freizeit.sport_hobby"),
        ]
        for _ in range(self.pois(1.0 * (1.4 if m.month in (3, 4, 9, 10) else 1))):
            _w, name, cat = self.pick(clothes)
            name = name.format(n=self.digits(4), city=CITY_UPPER)
            self.card(self.day_in(m), name, self.eur(15, 110, m, 35), cat)
        diy = 1.4 if m.month in (3, 4, 5, 6) else 0.5
        for _ in range(self.pois(0.8 * diy)):
            name = rng.choice(["HORNBACH BAUMARKT", "OBI MARKT", "BAUHAUS", "TOOM BAUMARKT"])
            self.card(
                self.day_in(m),
                f"{name} {CITY_UPPER}",
                self.eur(8, 160, m, 30),
                "wohnen.instandhaltung",
            )
        for _ in range(self.pois(0.35 * diy)):
            self.card(
                self.day_in(m),
                rng.choice(["IKEA WALLAU", "DEHNER GARTEN-CENTER"]),
                self.eur(15, 220, m, 50),
                "wohnen.instandhaltung",
            )
        misc = [
            (3, "TEDI {n}"),
            (2, "ACTION {n}"),
            (2, "WOOLWORTH"),
            (2, "TCHIBO {n}"),
            (2, "MEDIAMARKT {city}"),
            (1, "SATURN {city}"),
            (2, "FRISEUR HAARWERK"),
            (1, "DEUTSCHE POST {n}"),
        ]
        for _ in range(self.pois(1.2)):
            _w, name = self.pick(misc)
            name = name.format(n=self.digits(4), city=CITY_UPPER)
            cat = "gebuehren.sonstiges" if "POST" in name else "sonstiges.sonstiges"
            self.card(self.day_in(m), name, self.eur(4, 75, m, 18), cat)

    def leisure(self, m: date) -> None:
        rng, out = self.rng, self.covid(m) * self.away(m)
        names = ["BELLA ITALIA", "DA MARIO", "LA STRADA", "SAN REMO", "VENEZIA", "ROMA"]
        rule_places = [
            (4, "PIZZERIA {it}"),
            (3, "RISTORANTE {it}"),
            (2, "RESTAURANT ZUM {de}"),
            (2, "MCDONALDS {n}"),
            (1, "BURGER KING {n}"),
            (2, "CAFE {de}"),
            (1, "VAPIANO {city}"),
            (2, "GASTHAUS ZUR {de}"),
        ]
        other_places = [
            (3, "ASIA WOK {city}"),
            (3, "DOENER PALAST"),
            (2, "THAI ORCHID"),
            (2, "SUSHI CIRCLE"),
            (2, "EISDIELE DOLOMITI"),
            (1, "BIERGARTEN WOOG"),
        ]
        for _ in range(self.pois(2.0 * out)):
            places = rule_places if rng.random() < 0.6 else other_places
            name = self.pick(places)[1].format(
                it=rng.choice(names),
                de=rng.choice(["LINDE", "KRONE", "POST", "MUEHLE"]),
                n=self.digits(5),
                city=CITY_UPPER,
            )
            self.card(
                self.day_in(m, sunday=True), name, self.eur(9, 95, m, 32), "freizeit.restaurants"
            )
        for _ in range(self.pois(0.4)):
            self.card(
                self.day_in(m),
                rng.choice(["THALIA DARMSTADT", "HUGENDUBEL"]),
                self.eur(8, 45, m),
                "freizeit.medien",
            )
        for _ in range(self.pois(0.2 * self.covid(m))):
            self.card(
                self.day_in(m, sunday=True),
                "CINEPLEX DARMSTADT",
                self.eur(18, 52, m),
                "freizeit.sonstiges",
            )
        if m.month in (6, 7, 8) and rng.random() < 0.6 * self.covid(m):
            self.card(
                self.day_in(m, sunday=True),
                "NORDBAD DARMSTADT",
                self.eur(8, 22, m),
                "freizeit.sport_hobby",
            )
        if rng.random() < 0.15 * self.covid(m):
            self.card(
                self.day_in(m, sunday=True),
                rng.choice(
                    [
                        "VIVARIUM DARMSTADT",
                        "OPEL-ZOO KRONBERG",
                        "EXPERIMINTA FRANKFURT",
                        "KLETTERWALD DARMSTADT",
                    ]
                ),
                self.eur(18, 70, m),
                "freizeit.sonstiges",
            )
        for birth in (KID1_BIRTH, KID2_BIRTH):
            if m.month == birth.month:
                self.card(
                    self.day_in(m), "SMYTHS TOYS", self.eur(30, 90, m), "kinder.kleidung_spielzeug"
                )
                if self.covid(m) > 0.5:
                    self.card(
                        self.day_in(m),
                        "JUMP HOUSE TRAMPOLINPARK",
                        self.eur(70, 140, m),
                        "freizeit.sonstiges",
                    )
        if rng.random() < 0.3:
            self.transfer(
                self.day_in(m),
                self.people["godchild"],
                rng.choice([30, 50]) * 100,
                "Geburtstagsgeschenk",
                "sonstiges.geschenke",
            )
        if rng.random() < 0.25:
            self.card(self.day_in(m), "BLUMEN RISSE", self.eur(15, 45, m), "sonstiges.geschenke")
        if rng.random() < 0.15:
            self.transfer(
                self.day_in(m),
                self.people["friend"],
                self.eur(20, 90, m),
                rng.choice(["Anteil Geschenk", "Konzertkarten", "Auslage danke"]),
                "sonstiges.sonstiges",
            )

    def seasons(self, m: date) -> None:
        rng = self.rng
        if m.month == 12:  # Christmas
            for _ in range(rng.randint(1, 3)):
                self.card(
                    self.day_in(m, 1, 20),
                    "SMYTHS TOYS",
                    self.eur(25, 120, m),
                    "kinder.kleidung_spielzeug",
                )
            self.card(
                self.day_in(m, 1, 21), "THALIA DARMSTADT", self.eur(20, 70, m), "freizeit.medien"
            )
            for party, amount in (("Aerzte ohne Grenzen e.V.", 100), ("UNICEF", 50)):
                self.transfer(
                    m.replace(day=rng.randint(10, 22)),
                    party,
                    amount * 100,
                    "Spende Weihnachten",
                    "sonstiges.spenden",
                )
            self.transfer(
                m.replace(day=12),
                self.people["godchild"],
                50_00,
                "Weihnachtsgeschenk",
                "sonstiges.geschenke",
            )
        for year, month, region, country, cur, rate in SUMMER:  # summer holidays
            start = date(year, month, 1)
            if m == add_months(start, -5):
                self.transfer(
                    m.replace(day=rng.randint(5, 20)),
                    self.host(region, country),
                    round(self.eur(420, 520, start)),
                    f"Anzahlung Ferienhaus {region}",
                    "freizeit.sonstiges",
                )
            if m == add_months(start, -1):
                self.transfer(
                    m.replace(day=rng.randint(5, 20)),
                    self.host(region, country),
                    round(self.eur(1100, 1500, start)),
                    f"Restzahlung {region}",
                    "freizeit.sonstiges",
                )
            if m == start:
                self.holiday(m, region, country, (cur, rate))
            if m == date(2026, 1, 1) and year == 2026:
                self.card(
                    m.replace(day=9),
                    "LUFTHANSA",
                    1_684_00,
                    "freizeit.sonstiges",
                    who="A",
                    city="FRANKFURT",
                )
        if m.month == 2 and m.year in SKI_YEARS:  # winter holidays in Montafon
            self.move_from_extra(m.replace(day=1), 1_500_00, "Winterurlaub")
            self.transfer(
                m.replace(day=2),
                "Ferienwohnungen Montafon GmbH",
                self.eur(780, 900, m),
                "Ferienwohnung Schruns Winterferien",
                "sparen.urlaub",
            )
            for _ in range(rng.randint(2, 3)):
                self.rev(
                    m.replace(day=rng.randint(14, 20)),
                    "Silvretta Montafon Bergbahnen",
                    self.eur(120, 260, m),
                    "sparen.urlaub",
                    city="Schruns",
                )
            self.rev(
                m.replace(day=14),
                "Intersport Schruns Skiverleih",
                self.eur(140, 220, m),
                "sparen.urlaub",
                city="Schruns",
            )
            for _ in range(rng.randint(3, 5)):
                self.rev(
                    m.replace(day=rng.randint(14, 21)),
                    "Restaurant Kapell",
                    self.eur(30, 90, m),
                    "freizeit.restaurants",
                    city="Schruns",
                )
            self.card(
                m.replace(day=14),
                "ASFINAG VIGNETTE",
                1_000 if m.year < 2024 else 1_150,
                "mobilitaet.auto",
                city="SALZBURG",
                country="AT",
            )
        if m.month == 10 and m.year >= 2018 and m.year != 2020:  # autumn break: holiday park
            self.debit(
                m.replace(day=rng.randint(1, 8)),
                "PayPal Europe S.a.r.l. et Cie S.C.A",
                self.eur(620, 820, m),
                self.paypal_purpose("Center Parcs Europe N.V."),
                "freizeit.sonstiges",
            )
            for _ in range(rng.randint(2, 4)):
                self.card(
                    self.day_in(m, 10, 20),
                    "CENTER PARCS MARKET DORF",
                    self.eur(25, 80, m),
                    "freizeit.restaurants",
                    city="HOCHSAUERLAND",
                )

    def host(self, region: str, country: str) -> str:
        return {
            "DE": "Ostsee-Ferienhaus Vermittlung GmbH",
            "NL": "Belvilla AG",
            "DK": "NOVASOL AS",
            "IT": "Interhome AG",
            "FR": "Interhome AG",
            "HR": "Novasol AS",
            "PT": "Atraveo GmbH",
        }[country]

    def holiday(self, m: date, region: str, country: str, fx: tuple[str, float]) -> None:
        rng = self.rng
        local = {
            "NL": (["ALBERT HEIJN", "JUMBO"], ["Restaurant De Zeeuwse Stromen", "Strandpaviljoen"]),
            "IT": (["CONAD", "COOP ITALIA"], ["Ristorante Al Porto", "Gelateria Riva", "Pizzeria"]),
            "DK": (["NETTO", "FOETEX"], ["Restaurant Havnen", "Pølsevogn Bjarne"]),
            "DE": (["EDEKA", "REWE"], ["Fischbude Ahlbeck", "Restaurant Seeblick"]),
            "FR": (["CARREFOUR", "INTERMARCHE"], ["Creperie du Port", "Restaurant Le Phare"]),
            "HR": (["KONZUM", "SPAR"], ["Konoba Lanterna", "Restaurant Marina"]),
            "PT": (["PINGO DOCE", "CONTINENTE"], ["Restaurante O Pescador", "Cafe Lagos"]),
        }[country]
        for _ in range(rng.randint(3, 6)):  # on the way: fuel and toll
            self.card(
                self.day_in(m, 1, 20),
                rng.choice(["ENI STATION", "SHELL", "TOTALENERGIES"]),
                self.eur(55, 90, m),
                "mobilitaet.auto",
                city=region.upper()[:12],
                country=country,
            )
        if country in ("IT", "FR", "HR", "PT"):
            self.card(
                m.replace(day=rng.randint(1, 5)),
                "AUTOSTRADE PEDAGGI" if country == "IT" else "PEAGE AUTOROUTE",
                self.eur(25, 70, m),
                "mobilitaet.auto",
                city=region.upper()[:12],
                country=country,
            )
        for _ in range(rng.randint(3, 5)):  # groceries, on the Girokonto
            self.card(
                self.day_in(m, 1, 20),
                rng.choice(local[0]),
                self.eur(30, 120, m),
                "lebensmittel.supermarkt",
                city=region.upper()[:12],
                country=country,
            )
        for _ in range(rng.randint(5, 9)):  # eating out, by Revolut
            place = rng.choice(local[1])
            self.rev(
                self.day_in(m, 1, 20, sunday=True),
                place,
                self.eur(15, 110, m),
                "freizeit.restaurants",
                city=region,
                country=country,
                fx=fx,
            )
        for _ in range(rng.randint(2, 4)):
            self.rev(
                self.day_in(m, 1, 20, sunday=True),
                rng.choice(
                    ["Freizeitpark Ticket", "Bootsverleih", "Museum Shop", "Fahrradverleih"]
                ),
                self.eur(15, 90, m),
                "freizeit.sonstiges",
                city=region,
                country=country,
                fx=fx,
            )

    def one_offs(self, m: date) -> None:
        f = self.fake
        events = {
            date(2017, 5, 1): (
                self.electrician,
                680_00,
                "Rechnung Elektroinstallation Keller",
                "wohnen.handwerker",
            ),
            date(2019, 2, 1): ("MEDIAMARKT DARMSTADT", 649_00, None, "sonstiges.sonstiges"),
            date(2019, 5, 1): (
                f"Kuechenstudio {f.last_name()}",
                9_850_00,
                "Rechnung Einbaukueche Auftrag 2019-118",
                "wohnen.instandhaltung",
            ),
            date(2020, 6, 1): (
                f"Dachdeckerei {f.last_name()} GmbH",
                1_450_00,
                "Rechnung Reparatur Dachrinne",
                "wohnen.handwerker",
            ),
            date(2021, 11, 1): ("SATURN DARMSTADT", 1_099_00, None, "sonstiges.sonstiges"),
            date(2023, 3, 1): (
                f"Gartenbau {f.last_name()}",
                860_00,
                "Rechnung Baumschnitt und Hecke",
                "wohnen.handwerker",
            ),
            date(2024, 4, 1): (
                f"Garten- und Landschaftsbau {f.last_name()}",
                6_420_00,
                "Rechnung Terrasse WPC-Belag",
                "wohnen.handwerker",
            ),
            date(2025, 9, 1): (
                self.painter,
                2_840_00,
                "Rechnung Malerarbeiten Treppenhaus",
                "wohnen.handwerker",
            ),
            date(2026, 3, 1): (
                self.electrician,
                1_265_00,
                "Rechnung Wallbox-Vorbereitung",
                "wohnen.handwerker",
            ),
        }
        if m in events:
            party, cents, purpose, cat = events[m]
            if cents > 3_000_00:
                self.move_from_extra(m.replace(day=2), cents // 100_00 * 100_00, "")
            if purpose is None:
                self.card(m.replace(day=self.rng.randint(5, 25)), party, cents, cat, who="A")
            else:
                self.transfer(m.replace(day=self.rng.randint(5, 25)), party, cents, purpose, cat)

    # -- money between own accounts -----------------------------------------------------

    def extra_entry(
        self,
        day: date,
        cents: int,
        btype: str,
        party: str,
        lines: list[str],
        cat: Cat,
        value_day: date | None = None,
    ) -> None:
        self.ing(day, cents, btype, party, lines, cat, account="extra", value_day=value_day)
        self.extra_balance += cents

    def move_to_extra(self, day: date, cents: int, purpose: str, *, standing: bool = False):
        day = bank_day(day)
        out_type = "Dauerauftrag/Terminueberw." if standing else "Ueberweisung"
        in_type = "Gutschrift/Dauerauftrag" if standing else "Gutschrift"
        self.ing(day, -cents, out_type, self.holders, [purpose], None)
        self.extra_entry(day, cents, in_type, self.holders, [purpose], None)

    def move_from_extra(self, day: date, cents: int, reason: str) -> None:
        day = bank_day(day)
        purpose = f"Umbuchung vom Extra-Konto {reason}".strip()
        self.extra_entry(day, -cents, "Ueberweisung", self.holders, [purpose], None)
        self.ing(day, cents, "Gutschrift", self.holders, [purpose], None)

    def saving(self, m: date) -> None:
        rate = self.stepped(
            m, [(2016, 1, 450), (2018, 1, 600), (2020, 9, 800), (2023, 1, 900), (2025, 1, 1000)]
        )
        self.move_to_extra(m.replace(day=1), rate * 100, "Sparrate Extra-Konto", standing=True)
        # ETF savings plan, paid from the Extra-Konto
        plans = [
            (
                0,
                self.stepped(
                    m,
                    [
                        (2016, 1, 100),
                        (2018, 1, 150),
                        (2020, 1, 200),
                        (2021, 7, 250),
                        (2023, 1, 300),
                    ],
                ),
            )
        ]
        if m.month in (1, 4, 7, 10) and m >= date(2017, 1, 1):
            plans.append((1, 150 if m.year < 2023 else 300))
        for i, amount in plans:
            name, isin = ETFS[i][0], ETFS[i][1]
            day = bank_day(m.replace(day=1 if i == 0 else 15) + timedelta(days=1))
            price = self.etf_price(i, day)
            qty = round(amount / price, 6)
            cents = round(qty * price * 100)
            self.order_no += self.rng.randint(1, 40)
            short = name.replace(" UCITS ETF", "").replace(" ", "")
            self.extra_entry(
                day,
                -cents,
                "Wertpapierkauf",
                "",
                [
                    f"WP-ABRECHNUNG{self.order_no:012d}001KaufISIN{isin}{short[:12]}",
                    f"{short[12:]}ETF",
                ],
                None,
                value_day=day + timedelta(days=2),
            )
            self.trades.append((day, i, qty, cents))
        if m.month in (3, 6, 9, 12):  # distributions of the All-World ETF
            shares = sum(q for d, i, q, _c in self.trades if i == 0 and d < m.replace(day=20))
            per_share = self.etf_price(0, m.replace(day=20)) * self.rng.uniform(0.0035, 0.0055)
            cents = round(shares * per_share * 100)
            day = bank_day(m.replace(day=26))
            if cents > 0:
                self.extra_entry(
                    day,
                    cents,
                    "Zins/Dividende WP",
                    "",
                    [f"Zins/DividendeISIN{ETFS[0][1]}VANGUARD FTSE ALL-WORLD"],
                    "einkommen.kapitalertraege",
                    value_day=day - timedelta(days=3),
                )
                self.dividends.append((day, cents))
        # interest on the Extra-Konto: nearly nothing until 2022, quarterly afterwards
        if m.month == 12 and m.year < 2023:
            cents = max(1, round(self.extra_balance * 0.0001))
            self.extra_entry(month_end(m), cents, "Zinsertrag", "", [], "einkommen.kapitalertraege")
        elif m.month in (3, 6, 9, 12) and m.year >= 2023:
            rate = {2023: 0.012, 2024: 0.0175, 2025: 0.0125, 2026: 0.01}[m.year]
            interest = round(self.extra_balance * rate / 4)
            self.extra_entry(
                month_end(m), interest, "Zinsertrag", "", [], "einkommen.kapitalertraege"
            )
            if m.month == 12:  # tax on income above the allowance (1,000 EUR for two)
                year_income = interest * 4 + sum(c for d, c in self.dividends if d.year == m.year)
                excess = year_income - 1_000_00 * 2
                if excess > 0:
                    tax = round(excess * 0.25)
                    self.extra_entry(
                        month_end(m),
                        -tax,
                        "Kapitalertragsteuer",
                        "",
                        [],
                        "steuern.kapitalertragsteuer",
                    )
                    self.extra_entry(
                        month_end(m),
                        -round(tax * 0.055),
                        "Solidaritätszuschlag",
                        "",
                        [],
                        "steuern.kapitalertragsteuer",
                    )
        if m.month == 12 and m.year in SONDERTILGUNG:
            self.extra_entry(
                date(m.year, 12, 15),
                -SONDERTILGUNG[m.year],
                "Ueberweisung",
                "ING-DiBa AG",
                [f"Sondertilgung Darlehen {LOAN_NO}"],
                None,
            )

    def giro_balance(self, until: date) -> int:
        return self.giro_opening + sum(e.cents for e in self.giro if e.day <= until)

    def sweep(self, m: date) -> None:
        """Keep about a month's spending on the Girokonto; the rest goes to the Extra-Konto."""
        day = bank_day(month_end(m))
        outflow = -sum(
            e.cents
            for e in self.giro
            if e.cents < 0
            and e.day.month == m.month
            and e.day.year == m.year
            and abs(e.cents) < 3_000_00
        )
        target = max(outflow, 3_500_00) + 1_000_00
        balance = self.giro_balance(day)
        if balance > target + 5_000_00:
            amount = (balance - target) // 100_00 * 100_00
            self.move_to_extra(day, amount, "Umbuchung auf Extra-Konto")
        elif balance < target - 5_000_00 and self.extra_balance > 3_000_00:
            amount = min(
                math.ceil((target - balance) / 100_00) * 100_00, self.extra_balance - 2_000_00
            )
            self.move_from_extra(day, amount, "")

    # -- Revolut and cash ------------------------------------------------------------------

    def revolut_month(self, m: date) -> None:
        """Papa's Revolut card: lunch, coffee, tickets, subscriptions; topped up as needed."""
        if m < REVOLUT_SINCE:
            return
        rng, work = self.rng, self.covid(m)
        lunch = [
            (4, "Rewe To Go", "lebensmittel.supermarkt"),
            (3, "Backwerk", "lebensmittel.baeckerei"),
            (3, "Subway", "freizeit.restaurants"),
            (2, "dean&david", "freizeit.restaurants"),
            (2, "Asia Gourmet", "freizeit.restaurants"),
            (1, "Vapiano", "freizeit.restaurants"),
            (2, "Kantine Bürohaus", "freizeit.restaurants"),
        ]
        for _ in range(self.pois(4.0 * work * self.away(m))):
            _w, name, cat = self.pick(lunch)
            d = self.day_in(m)
            while d.weekday() >= 5:
                d -= timedelta(days=1)
            if d.month == m.month:
                self.rev(d, name, self.eur(4, 14, m, 7), cat)
        for _ in range(self.pois(1.3 * work)):
            self.rev(
                self.day_in(m),
                rng.choice(["Starbucks", "Coffee Fellows"]),
                self.eur(3, 7, m),
                "freizeit.restaurants",
            )
        if m >= date(2018, 1, 1):
            usd = 5 if m.year < 2023 else 7 if m.year < 2025 else 9
            rate = 1.12 + rng.uniform(-0.06, 0.06)
            self.rev(
                m.replace(day=14),
                "Backblaze",
                round(usd / rate * 100),
                "gebuehren.datensicherung",
                legal="Backblaze Inc, Backblaze.com",
                city="CA",
                fx=("USD", rate),
            )
        if m >= date(2020, 5, 1):
            self.rev(
                m.replace(day=22),
                "Google One",
                199 if m.year < 2024 else 299,
                "gebuehren.datensicherung",
                legal="Google Payment Ireland",
                city="Dublin",
            )
        if m < date(2023, 5, 1):
            for _ in range(self.pois(1.5 * work)):
                self.rev(
                    self.day_in(m),
                    "RMV",
                    self.eur(2.8, 9.5, m),
                    "mobilitaet.oepnv",
                    legal="Rhein-Main-Verkehrsverbund",
                )
        for _ in range(self.pois(0.3)):
            self.rev(
                self.day_in(m),
                "App Store",
                self.eur(1, 12, m),
                "freizeit.medien",
                legal="Apple Distribution International",
                city="Cork",
            )

    def finish_revolut(self, m: date) -> None:
        """Insert top-ups (with the Girokonto Visa card) before the balance runs out."""
        month = sorted(
            (e for e in self.revolut if (e.day.year, e.day.month) == (m.year, m.month)),
            key=lambda e: (e.day, e.seq),
        )
        result: list[Rev] = []
        for e in month:
            if self.revolut_balance + e.cents < 10_00 and e.cents < 0:
                amount = max(150_00, math.ceil((-e.cents + 40_00) / 50_00) * 50_00)
                amount = max(amount, self.rng.choice([150, 200, 200, 250, 300]) * 100)
                pay = "Google Pay " if e.day.year >= 2019 else ""
                result.append(
                    Rev(
                        e.day,
                        amount,
                        f"{pay}Einzahlung von *{CARD_A}",
                        [("Von", f"*{CARD_A}")],
                        None,
                        self._next(),
                    )
                )
                self.revolut_balance += amount
                self.card(
                    e.day, f"REVOLUT**{CARD_A}*", amount, None, who="A", city="DUBLIN", country="IE"
                )
            result.append(e)
            self.revolut_balance += e.cents
        self.revolut = [e for e in self.revolut if (e.day.year, e.day.month) != (m.year, m.month)]
        self.revolut += result

    def cash_month(self, m: date) -> None:
        self.finish_revolut(m)
        rng = self.rng
        spending = {
            "A": [
                (3, "Wochenmarkt", "lebensmittel.supermarkt", 8, 35),
                (2, "Eisdiele", "freizeit.restaurants", 4, 18),
                (1, "Flohmarkt", "kinder.kleidung_spielzeug", 3, 25),
                (1, "Trinkgeld Pizzabote", "freizeit.restaurants", 2, 5),
                (1, "Kuchenbasar Schule", "kinder.weiterbildung_sport", 3, 10),
            ],
            "B": [
                (3, "Wochenmarkt", "lebensmittel.supermarkt", 8, 35),
                (2, "Friseur Salon Locke", "sonstiges.sonstiges", 25, 60),
                (1, "Kindergeburtstag Geschenk", "sonstiges.geschenke", 10, 25),
                (1, "Spendendose", "sonstiges.spenden", 2, 10),
                (1, "Bäckerei am Markt", "lebensmittel.baeckerei", 3, 12),
            ],
        }
        for who in ("A", "B"):
            for _ in range(self.pois(0.75)):
                _w, payee, cat, lo, hi = self.pick(spending[who])
                cents = min(self.eur(lo, hi, m), max(self.cash_balance[who] - 5_00, 0))
                if cents > 0:
                    self.cash_out(who, self.day_in(m, sunday=True), payee, cents, cat)
            if m.month == 12:
                if who == "A":
                    self.cash_out(
                        who,
                        m.replace(day=rng.randint(10, 18)),
                        "Weihnachtsbaum",
                        self.eur(30, 45, m),
                        "sonstiges.sonstiges",
                    )
                else:
                    self.cash_out(
                        who,
                        m.replace(day=rng.randint(1, 20)),
                        "Weihnachtsmarkt",
                        self.eur(15, 40, m),
                        "freizeit.restaurants",
                    )
            if self.cash_balance[who] < 40_00:  # to the cash machine
                card = CARD_A if who == "A" else CARD_B
                amount = rng.choice([100, 150, 200]) * 100
                day = self.day_in(m, 1, 25)
                self.ing(
                    bank_day(day),
                    -amount,
                    "Bargeldauszahlung",
                    "VISA SPARKASSE DARMSTADT",
                    [
                        f"NR XXXX {card} {CITY_UPPER} DE BARGELDAUSZAHLUNG {day:%d.%m} "
                        f"{amount / 100:.2f} {self.digits(6)}"
                    ],
                    None,
                )
                self.cash_balance[who] += amount


# --- statements ------------------------------------------------------------------------


def _by_month(entries, m: date) -> list:
    return sorted(
        (e for e in entries if (e.day.year, e.day.month) == (m.year, m.month)),
        key=lambda e: (e.day, e.seq),
    )


def ing_text(
    world: World, product: str, number: str, iban: str, m: date, opening: int, entries: list[Ing]
) -> str:
    closing = opening + sum(e.cents for e in entries)
    lines = [
        f"{product} Nummer {number}",
        f"Kontoauszug {MONTH_NAMES[m.month - 1]} {m.year}",
        "Buchung Buchung / Verwendungszweck Betrag (EUR)",
        "Valuta",
        *(line for e in entries for line in e.render()),
        f"Neuer Saldo {de(closing)}",
        "Kunden-Information",
        "Bitte beachten Sie auch die Hinweise auf der Folgeseite.",
        "ING-DiBa AG · 60628 Frankfurt am Main",
        f"Datum {month_end(m):%d.%m.%Y}",
        f"{world.holders} Auszugsnummer {m.month}",
        "Eingeräumte Kontoüberziehung 2.000,00 Euro" if product == "Girokonto" else "",
        world.street,
        f"Alter Saldo {de(opening)} Euro",
        f"{world.postcode} {CITY}",
        f"Neuer Saldo {de(closing)} Euro",
        f"IBAN {iban_spaced(iban)}",
        "BIC INGDDEFFXXX",
        "Seite 1 von 1",
    ]
    return "\n".join(line for line in lines if line)


def revolut_text(world: World, start: date, end: date, opening: int, entries: list[Rev]) -> str:
    entity = "Revolut Payments UAB" if start < date(2022, 7, 1) else "Revolut Bank UAB"
    product = "E-Geld" if start < date(2022, 7, 1) else "Girokonto"
    out = -sum(e.cents for e in entries if e.cents < 0)
    in_ = sum(e.cents for e in entries if e.cents > 0)
    closing = opening + in_ - out
    rows, balance = [], opening
    for e in entries:
        balance += e.cents
        rows.append(f"{e.day:%d.%m.%Y} {e.desc} {de(abs(e.cents))}€ {de(balance)}€")
        rows += [f"{k}: {v}" for k, v in e.details]

    def long(d: date) -> str:
        return f"{d.day}. {MONTH_NAMES[d.month - 1]} {d.year}"

    summary = f"{de(opening)}€ {de(out)}€ {de(in_)}€ {de(closing)}€"
    return "\n".join(
        [
            "EUR-Kontoauszug",
            f"Generiert am {min(end + timedelta(days=1), TODAY - timedelta(days=1)):%d.%m.%Y}",
            entity,
            f"{world.father} {world.surname}".upper(),
            f"{world.street} IBAN {REVOLUT_IBAN}",
            f"{world.postcode} BIC REVODEB2",
            CITY,
            "Zusammenfassung der Bilanz",
            "Produkt Eröffnungssaldo Geldausgang Geldeingang Schlusssaldo",
            f"Konto ({product}) {summary}",
            f"Gesamt {summary}",
            f"Kontotransaktionen von {long(start)} bis {long(end)}",
            "Datum Beschreibung Geldausgang Geldeingang Kontostand",
            *rows,
            "Verlorene oder gestohlene Karte melden Revolut ist ein Institut mit Sitz in Litauen.",
            f"© {end.year} Revolut Seite 1 von 1",
        ]
    )


def _imported_at(rng: random.Random, after: date) -> datetime:
    day = after + timedelta(days=rng.randint(2, 9))
    day = min(day, TODAY)
    return datetime(day.year, day.month, day.day, rng.randint(7, 21), rng.randint(0, 59))


def statements(world: World, start: date, end: date) -> list[Statement]:
    """Monthly statements of the Girokonto, Revolut and Extra-Konto, quarterly
    Depotauszüge and yearly loan statements, in the order they are imported."""
    rng = random.Random(world.seed + 1)
    result: list[Statement] = []
    balances = {"giro": world.giro_opening, "extra": world.extra_opening, "revolut": 0}
    loan_balance = world.loan_opening
    m = FIRST_MONTH
    while m <= end:
        current = month_end(m) >= TODAY
        last = min(month_end(m), TODAY - timedelta(days=1))
        for key, entries, product, number, iban in (
            ("giro", world.giro, "Girokonto", GIRO_NO, GIRO_IBAN),
            ("extra", world.extra, "Extra-Konto", EXTRA_NO, EXTRA_IBAN),
        ):
            month = _by_month(entries, m)
            text = ing_text(world, product, number, iban, m, balances[key], month)
            balances[key] += sum(e.cents for e in month)
            if m < start or (current and key == "extra"):
                continue  # not imported (before the range; no Kontoauszug yet)
            parsed = ing_giro_pdf.parse_pages([text])
            name = f"{product}_{number}_Kontoauszug_{month_end(m):%Y%m%d}.pdf"
            if current:  # the current month: ING Umsatzanzeige from online banking
                parsed.importer, parsed.statement_no = "ing_umsatzanzeige_pdf", None
                parsed.period_end = parsed.statement_date = last
                name = f"Umsatzanzeige_{number}_{last:%Y%m%d}.pdf"
            result.append(
                Statement(
                    key, name, text, parsed, [e.cat for e in month], _imported_at(rng, last), m
                )
            )
        month = _by_month(world.revolut, m)
        if m >= REVOLUT_SINCE:
            text = revolut_text(world, m, last, balances["revolut"], month)
            balances["revolut"] += sum(e.cents for e in month)
            if m >= start:
                parsed = revolut_pdf.parse_pages([text])
                export = rng.randint(100000, 999999)
                name = f"account-statement_{m:%Y-%m-%d}_{last:%Y-%m-%d}_de-de_{export}.pdf"
                result.append(
                    Statement(
                        "revolut",
                        name,
                        text,
                        parsed,
                        [e.cat for e in month],
                        _imported_at(rng, last),
                        m,
                    )
                )
        # Depotauszug for the quarter that ended last month, loan statement in January
        quarter_end = m - timedelta(days=1)
        if m.month in (1, 4, 7, 10) and m >= add_months(FIRST_MONTH, 2) and m >= start:
            result.append(depot_statement(world, quarter_end, rng))
        if m.month == 1 and m.year >= 2017:
            year = m.year - 1
            txs = [t for t in world.loan if t.booking_date.year == year]
            closing = loan_balance + sum(t.amount_cents for t in txs)
            if m >= start:
                result.append(loan_statement(year, loan_balance, closing, txs, rng))
            loan_balance = closing
        m = add_months(m, 1)
    for s in result:
        problems = list(s.parsed.warnings)
        if s.parsed.balance_checkable and not s.parsed.balance_ok:
            problems.append("balance check failed")
        if len(s.parsed.transactions) != len(s.cats) and not s.parsed.valuation:
            problems.append(f"{len(s.parsed.transactions)} bookings parsed, {len(s.cats)} made")
        if problems:
            raise RuntimeError(f"{s.name}: {'; '.join(problems)}")
    return result


def depot_statement(world: World, day: date, rng: random.Random) -> Statement:
    holdings = []
    for i, (name, isin, wkn, *_r) in enumerate(ETFS):
        qty = round(sum(q for d, j, q, _c in world.trades if j == i and d <= day), 6)
        if qty <= 0:
            continue
        price = world.etf_price(i, day)
        holdings.append(
            ParsedHolding(name, qty, "Stück", round(qty * price * 100), isin, wkn, price, "EUR")
        )
    parsed = ParsedStatement(
        importer="ing_depot_pdf",
        institution="ING",
        account_name=f"ING Direkt-Depot {DEPOT_NO}",
        account_kind="depot",
        account_iban=DEPOT_NO,
        statement_date=day + timedelta(days=12),
        period_start=day,
        period_end=day,
        closing_balance_cents=sum(h.value_cents for h in holdings),
        valuation=True,
        holdings=holdings,
    )
    text = "\n".join(f"{h.isin} {h.quantity} {h.value_cents}" for h in holdings) + str(day)
    name = f"Direkt-Depot_{DEPOT_NO}_Depotauszug_per_{day:%Y%m%d}.pdf"
    return Statement(
        "depot",
        name,
        text,
        parsed,
        [],
        _imported_at(rng, day + timedelta(days=12)),
        day.replace(day=1),
    )


def loan_statement(
    year: int,
    opening: int,
    closing: int,
    txs: list[ParsedTransaction],
    rng: random.Random,
) -> Statement:
    parsed = ParsedStatement(
        importer="ing_loan_pdf",
        institution="ING",
        account_name=f"ING Baufinanzierung {LOAN_NO}",
        account_kind="loan",
        account_iban=LOAN_IBAN,
        statement_date=date(year, 12, 30),
        period_start=date(year, 1, 1),
        period_end=date(year, 12, 31),
        opening_balance_cents=opening,
        closing_balance_cents=closing,
        transactions=txs,
        valuation=True,
    )
    name = f"Baufinanzierung_{LOAN_NO}_Kontoauszug_{year}.pdf"
    text = f"{LOAN_NO} {year} {opening} {closing}"
    return Statement(
        "loan", name, text, parsed, [], _imported_at(rng, date(year + 1, 1, 3)), date(year, 12, 1)
    )


# --- database ------------------------------------------------------------------------------


@contextmanager
def seeded_uuids(seed: int) -> Iterator[None]:
    """Manual bookings get uuid4 keys; make them reproducible."""
    rng = random.Random(seed)
    original = uuid.uuid4
    uuid.uuid4 = lambda: uuid.UUID(int=rng.getrandbits(128), version=4)
    try:
        yield
    finally:
        uuid.uuid4 = original


def import_statement(session: Session, settings: Settings, s: Statement) -> dict[str, Cat]:
    """Import like the import dialog does; returns the intended category per dedupe key."""
    stmt = s.parsed
    keys = dedupe_keys(stmt.account_iban or stmt.account_name, stmt.transactions)
    preview = ImportPreview(
        path=Path(s.name),
        sha256=hashlib.sha256(s.content.encode()).hexdigest(),
        statement=stmt,
        account_name=stmt.account_name,
        account_exists=True,
        file_already_imported=False,
        keys=keys,
        duplicate_flags=[False] * len(keys),
    )
    result = commit(session, settings, preview, retrain=False)
    session.get_one(ImportBatch, result.batch_id).imported_at = s.imported_at
    return {} if stmt.valuation else dict(zip(keys, s.cats, strict=True))


def categorize_rest(session: Session, intended: dict[str, Cat]) -> Counter[str]:
    """Give every booking still uncategorized its category by hand (manual split)."""
    stats: Counter[str] = Counter()
    cats = {c.key: c.id for c in session.scalars(select(Category))}
    for tx in load_transactions(session):
        if tx.is_counterpart:
            continue
        want = intended.get(tx.dedupe_key)
        source = tx.splits[0].source if tx.splits else None
        if source == SplitSource.RULE:
            got = tx.splits[0].category.key
        elif source in (SplitSource.TRANSFER, SplitSource.MORTGAGE):
            got = None
        else:
            got = want
        if got != want:
            stats["mismatch"] += 1
            print(
                f"  {source} {tx.splits[0].rule_id or ''}: {tx.display_name} -> {got}, meant {want}"
            )
            continue
        if not any(s.source == SplitSource.UNCATEGORIZED for s in tx.splits):
            continue
        if want is None:
            raise RuntimeError(f"no category for {tx.booking_date} {tx.display_name}")
        if isinstance(want, str):
            parts = [SplitInput(tx.amount_cents, category_id=cats[want])]
        else:
            parts = [SplitInput(cents, category_id=cats[key]) for key, cents in want]
        set_splits(session, tx.id, parts)
        stats["manual"] += 1
    return stats


def assign_budget(session: Session, settings: Settings, months: list[str]) -> int:
    """Assign money month by month as a careful household would: regular categories get
    the average of the last twelve months, targets what they ask for, and overspending is
    always covered. Stored as the budget page stores it (``budget_assignment``)."""
    if not months:
        return 0
    flows = budget.BudgetData.load(session, settings).flows
    lines = budget._budget_lines(session, settings)
    activity: dict[tuple[budget.Envelope, str], int] = defaultdict(int)
    for (cat_id, m), cents in flows.activity.items():
        activity[(lines.of_category[cat_id], m)] += cents
    # spending history incl. the months before the budget start
    history: dict[tuple[budget.Envelope, str], int] = defaultdict(int)
    rows = session.execute(
        select(Split.category_id, Transaction.booking_date, Split.amount_cents)
        .join(Transaction, Split.transaction_id == Transaction.id)
        .join(Account, Transaction.account_id == Account.id)
        .join(Category, Split.category_id == Category.id)
        .join(CategoryGroup, Category.group_id == CategoryGroup.id)
        .where(Account.on_budget.is_(True), CategoryGroup.is_income.is_(False))
    )
    for cat_id, booked, cents in rows:
        history[(lines.of_category[cat_id], budget.month_of(booked))] -= cents

    def average(env: budget.Envelope, m: str) -> int:
        spent = sum(history.get((env, budget.month_add(m, -i)), 0) for i in range(1, 13))
        return round_up(round(spent / 12), 10)

    # Big one-off expenses (the new car, the kitchen) are saved for in the months before
    # and do not count as regular spending afterwards.
    saving_for: dict[tuple[budget.Envelope, str], int] = defaultdict(int)
    for env, m in sorted(history, key=lambda k: k[1]):
        spent, usual = history[(env, m)], average(env, m)
        if spent > 3_000_00 and spent > 4 * usual:
            history[(env, m)] = usual
            if m in months:
                i = months.index(m)
                before = months[max(0, i - 8) : i + 1]
                for p in before:
                    saving_for[(env, p)] += round_up(spent // len(before), 10)

    goals = [e for e in lines.envelopes if e.target is not None and e.target.type != "monthly"]

    def plan(limit: int) -> tuple[dict[tuple[budget.Envelope, str], int], int, int]:
        """Assign month by month; money left beyond a reserve goes to the savings goals,
        at most ``limit`` in all. Returns assignments, final Ready to Assign, extras."""
        rta = flows.start_funds + sum(v for m, v in flows.inflow.items() if m < months[0])
        available: dict[budget.Envelope, int] = defaultdict(int)
        assigned: dict[tuple[budget.Envelope, str], int] = {}
        extras = 0
        for m in months:
            rta += flows.inflow.get(m, 0)
            for e in lines.envelopes:
                env = e.envelope
                carry = max(available[env], 0)
                act = activity.get((env, m), 0)
                if e.target is not None:
                    e.carry, e.assigned = carry, 0
                    budget.apply_target(e, m)
                    want = e.target_month
                else:
                    want = average(env, m) + saving_for.get((env, m), 0)
                cents = max(round_up(want, 10), round_up(-(carry + act), 10))
                available[env] = carry + cents + act
                assigned[(env, m)] = cents
                rta -= cents
            for e in goals:
                room = round(e.target.amount * 100) - available[e.envelope]
                extra = min(room, rta - 2_500_00, limit - extras) // 100_00 * 100_00
                if extra > 0:
                    available[e.envelope] += extra
                    assigned[(e.envelope, m)] += extra
                    rta -= extra
                    extras += extra
        return assigned, rta, extras

    # Later months can need more than they bring in (covers, goals asking more once the
    # extras are smaller): put less into the goals up front until a reserve is left.
    assigned, rta, extras = plan(10**12)
    for _ in range(10):
        if rta >= 1_000_00 or extras == 0:
            break
        assigned, rta, extras = plan(max(0, extras - (1_000_00 - rta)))
    rows = []
    for (env, m), cents in assigned.items():
        if cents:
            ids = {"category_id": env.id} if env.kind == "category" else {"group_id": env.id}
            rows.append(BudgetAssignment(month=m, amount_cents=cents, **ids))
    session.add_all(rows)
    session.flush()
    return len(rows)


def renumber_splits(session: Session) -> None:
    """Number the splits by booking: the ORM's flush order (and so the ids of recreated
    splits) differs from run to run. Nothing refers to split ids."""
    session.flush()
    ids = session.scalars(select(Split.id).order_by(Split.transaction_id, Split.id)).all()
    conn = session.connection()
    conn.execute(text("UPDATE split SET id = -id"))
    conn.execute(
        text("UPDATE split SET id = :new WHERE id = :old"),
        [{"new": i, "old": -old} for i, old in enumerate(ids, start=1)],
    )
    session.expunge_all()


def generate(
    db_path: Path,
    *,
    start: str = ym(FIRST_MONTH),
    months: int = 120,
    config_dir: Path = CONFIG_DIR,
    seed: int = SEED,
    quiet: bool = False,
) -> Path:
    """Build the demo database at ``db_path`` (deleted first) for ``months`` months from
    ``start``; the family's history is always simulated from November 2016."""
    t0 = time.perf_counter()
    first = date(int(start[:4]), int(start[5:7]), 1)
    end = min(add_months(first, months - 1), TODAY.replace(day=1))
    if first < FIRST_MONTH or first > end:
        raise ValueError(f"start must be between {ym(FIRST_MONTH)} and {ym(TODAY)}")
    db_path = Path(db_path)
    for p in (db_path, db_path.with_name(db_path.name + "-journal")):
        p.unlink(missing_ok=True)

    world = World(seed)
    world.run(min(add_months(end, 1), TODAY))
    stmts = statements(world, first, end)

    settings = load_settings(config_dir)
    app = App(settings, db_path)
    log = (lambda *a: None) if quiet else print
    intended: dict[str, Cat] = {}
    with seeded_uuids(seed), app.session() as s:
        accounts = {a.name: a.id for a in s.scalars(select(Account))}
        cats = {c.key: c.id for c in s.scalars(select(Category))}
        cash = {w: sorted(e, key=lambda e: e.day) for w, e in world.cash.items()}
        for st in stmts:
            intended.update(import_statement(s, settings, st))
            if st.account == "extra" or (st.account == "giro" and st.parsed.importer != "ing_pdf"):
                for who, entries in cash.items():
                    for e in [
                        e
                        for e in entries
                        if (e.day.year, e.day.month) == (st.month.year, st.month.month)
                    ]:
                        create_manual(
                            s,
                            settings,
                            ManualEntry(
                                accounts[CASH_ACCOUNTS[who]],
                                e.day,
                                e.cents,
                                e.payee,
                                e.purpose,
                                category_id=cats[e.cat],
                            ),
                        )
        if len(list(s.scalars(select(Account)))) != len(accounts):
            raise RuntimeError("a statement did not match a configured account (IBAN?)")
        log(f"imported {len(stmts)} statements in {time.perf_counter() - t0:.1f} s")
        stats = recategorize_all(s, settings)
        log(f"categorization: {stats}")
        manual = categorize_rest(s, intended)
        log(f"categorized by hand: {manual.get('manual', 0)}")
        if manual.get("mismatch"):
            raise RuntimeError(f"{manual['mismatch']} bookings were categorized wrongly")
        last = budget.month_of(end)
        budget_months = [budget.month_add(last, -i) for i in reversed(range(BUDGET_MONTHS))]
        budget_months = [
            m for m in budget_months if m >= max(ym(first), budget.start_month(s, settings))
        ]
        n = assign_budget(s, settings, budget_months)
        log(f"budget: {n} assignments in {len(budget_months)} months")
        renumber_splits(s)
    app.session_factory.kw["bind"].dispose()
    log(f"done in {time.perf_counter() - t0:.1f} s: {db_path}")
    return db_path


# --- summary ---------------------------------------------------------------------------------


def summary(db_path: Path, config_dir: Path = CONFIG_DIR) -> dict:
    """Key figures of a generated database (also checks that the app can open it)."""
    from hiro_budget.services.accounts import account_balances

    app = App(load_settings(config_dir), db_path)
    out: dict = {}
    with app.session() as s:
        rows = s.execute(
            select(Account.name, Account.kind, Transaction.booking_date)
            .select_from(Transaction)
            .join(Transaction.account)
        ).all()
        per_month: Counter[str] = Counter(budget.month_of(d) for _n, _k, d in rows)
        months = sorted(per_month)
        out["accounts"] = dict(Counter(str(a.kind) for a in s.scalars(select(Account))))
        out["bookings"] = f"{len(rows)} in {len(months)} months ({months[0]} .. {months[-1]})"
        values = list(per_month.values())
        out["per_month"] = (round(sum(values) / len(values), 1), min(values), max(values))
        by_account: dict[str, Counter[str]] = defaultdict(Counter)
        for name, kind, d in rows:
            by_account[f"{kind} {name}"][budget.month_of(d)] += 1
        out["per_month_by_account"] = {}
        for key, c in sorted(by_account.items()):
            active = months[months.index(min(c)) :]  # from the account's first booking
            counts = [c.get(m, 0) for m in active]
            out["per_month_by_account"][key] = (
                round(sum(counts) / len(counts), 1),
                min(counts),
                max(counts),
            )
        sources = Counter(str(src) for src in s.scalars(select(Split.source)))
        total = sum(sources.values())
        out["split_sources"] = {k: f"{v / total:.1%}" for k, v in sources.most_common()}
        out["uncategorized"] = sum(
            1
            for tx in load_transactions(s)
            if not tx.splits or any(sp.is_uncategorized for sp in tx.splits)
        )
        counterparts = s.scalar(
            select(func.count()).where(Transaction.counterpart_of_id.is_not(None))
        )
        out["counterparts"] = f"{counterparts} ({counterparts / len(rows):.1%})"
        out["batches"] = s.scalar(select(func.count()).select_from(ImportBatch))
        out["budget_months"] = s.scalar(select(func.count(func.distinct(BudgetAssignment.month))))
        out["budget_assignments"] = s.scalar(select(func.count()).select_from(BudgetAssignment))
        valuations = s.scalars(
            select(ImportBatch)
            .options(selectinload(ImportBatch.holdings))
            .join(Account)
            .where(ImportBatch.valuation.is_(True), Account.kind == "depot")
        ).all()
        out["depot_valuations"] = len(valuations)
        out["holdings"] = sum(len(b.holdings) for b in valuations)
        loans = s.scalars(
            select(ImportBatch)
            .join(Account)
            .where(Account.kind == "loan")
            .order_by(ImportBatch.period_end)
        ).all()
        balances = {b.name: b.balance_cents for b in account_balances(s)}
        if loans:
            out["loan"] = (de(loans[0].opening_balance_cents), de(balances["ING Baufinanzierung"]))
        out["balances"] = {k: de(v) for k, v in balances.items()}
        month = budget.month_of(min(TODAY, date.today()))
        last = s.scalar(select(func.max(BudgetAssignment.month)))
        b = budget.compute_month(s, app.settings, last or month)
        out["budget"] = {
            "month": b.month,
            "ready_to_assign": de(b.ready_to_assign),
            "assigned": de(b.assigned),
            "activity": de(b.activity),
            "available": de(b.available),
            "uncategorized": b.uncategorized_count,
            "overspent": sum(1 for e in b.envelopes if e.overspent),
        }
    app.session_factory.kw["bind"].dispose()
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", type=Path, help="database file (default: from demo/config)")
    p.add_argument("--start", default=ym(FIRST_MONTH), help="first month YYYY-MM")
    p.add_argument("--months", type=int, default=120, help="number of months")
    p.add_argument("--no-summary", action="store_true")
    args = p.parse_args(argv)
    db = args.db or load_settings(CONFIG_DIR).db_path
    generate(db, start=args.start, months=args.months)
    if not args.no_summary:
        for key, value in summary(db).items():
            print(f"{key:>20}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
