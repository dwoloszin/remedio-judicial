"""
collect.py: count e-NatJus technical notes per search term, within a time budget.

Terms never collected come first: hand-picked extras, then the highest CMED price.
Once every term has a count, the oldest count is refreshed first, so each daily run
continues where the last one stopped.

Only the total for each term is read. Individual notes are never opened, because
they contain patient details.
"""

import argparse
import re
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
CATALOG = ROOT / "data" / "catalogo.csv"
COUNTS = ROOT / "data" / "notas.csv"
HISTORY = ROOT / "data" / "historico.csv"
MEDICINES = ROOT / "data" / "medicamentos.csv"

BASE_URL = "https://www.pje.jus.br/e-natjus"
USER_AGENT = "Mozilla/5.0 (compatible; remedio-judicial; +https://github.com/dwoloszin/remedio-judicial)"
TERM_SEPARATOR = " | "
CANARY_TERM = "PEMBROLIZUMABE"  # known to have thousands of notes

PAUSE_SECONDS = 6
REQUEST_TIMEOUT = 60
RETRY_WAITS = (10, 20)
MAX_FAILURES_IN_A_ROW = 5
COUNT_COLUMNS = ["termo", "notas", "preco_max", "atualizado_em"]


class SearchFailed(Exception):
    """The search gave no usable answer."""


class NatJusSearch:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "X-Requested-With": "XMLHttpRequest"})
        self.token = None

    def _refresh_token(self) -> None:
        page = self.session.get(f"{BASE_URL}/pesquisaPublica.php", timeout=REQUEST_TIMEOUT)
        page.raise_for_status()
        match = re.search(r'name="token" id="token" value="([^"]+)', page.text)
        if not match:
            raise SearchFailed("search token not found: the page changed")
        self.token = match.group(1)

    def _ask(self, term: str) -> int:
        if self.token is None:
            self._refresh_token()
        params = {"token": self.token, "selTipoTecnologia": 1, "txtDcb": term, "pagina": 1}
        response = self.session.post(f"{BASE_URL}/api/pesquisaPublica/listar", params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            raise SearchFailed(f"HTTP {response.status_code}")
        try:
            total = response.json().get("recordsTotal")
        except ValueError as error:
            raise SearchFailed("answer is not JSON") from error
        if total is None:
            raise SearchFailed("answer has no total")
        return int(total)

    def count(self, term: str) -> int:
        """Total notes for one term. A retry gets a new token, in case the old one expired."""
        for wait in (*RETRY_WAITS, None):
            try:
                return self._ask(term)
            except (SearchFailed, requests.RequestException) as error:
                if wait is None:
                    raise SearchFailed(f"{term}: {error}") from error
                self.token = None
                time.sleep(wait)
        raise AssertionError("unreachable")


def read_terms() -> pd.DataFrame:
    """One row per distinct term, since a term can belong to several substances."""
    catalog = pd.read_csv(CATALOG, keep_default_na=False)
    catalog["preco_max"] = pd.to_numeric(catalog["preco_max"], errors="coerce")
    catalog["termo"] = catalog["termos_busca"].str.split(TERM_SEPARATOR, regex=False)
    rows = catalog.explode("termo")
    return rows.groupby("termo", as_index=False).agg(
        preco_max=("preco_max", "max"),
        extra=("origem", lambda s: (s == "extras").any()),
    )


def read_counts() -> pd.DataFrame:
    if COUNTS.exists():
        return pd.read_csv(COUNTS, keep_default_na=False)
    return pd.DataFrame(columns=COUNT_COLUMNS)


def build_queue(terms: pd.DataFrame, counts: pd.DataFrame) -> pd.DataFrame:
    queue = terms.merge(counts[["termo", "atualizado_em"]], on="termo", how="left")
    queue["atualizado_em"] = queue["atualizado_em"].fillna("")  # '' sorts first: never collected
    return queue.sort_values(
        ["atualizado_em", "extra", "preco_max", "termo"],
        ascending=[True, False, False, True],
        na_position="last",
    )


def save_counts(rows: dict) -> None:
    table = pd.DataFrame(rows.values(), columns=COUNT_COLUMNS)
    table["preco_max"] = pd.to_numeric(table["preco_max"], errors="coerce")
    table = table.sort_values(["preco_max", "termo"], ascending=[False, True], na_position="last")
    table.to_csv(COUNTS, index=False, encoding="utf-8", lineterminator="\n")


def save_medicines() -> None:
    """
    One row per substance with a range of notes, because its terms can overlap.

    Some terms share no notes (RIBOCICLIBE, SUCCINATO DE RIBOCICLIBE) and others are
    nested (LOSARTANA, LOSARTANA POTÁSSICA). The true count is therefore between the
    largest term (notas_min) and the sum of the terms (notas_max).
    """
    catalog = pd.read_csv(CATALOG, keep_default_na=False)
    catalog["termo"] = catalog["termos_busca"].str.split(TERM_SEPARATOR, regex=False)
    catalog["termos_total"] = catalog["termo"].str.len()
    rows = catalog.explode("termo").merge(read_counts()[["termo", "notas", "atualizado_em"]], on="termo")
    rows["notas"] = pd.to_numeric(rows["notas"])
    table = rows.groupby("principio_ativo", as_index=False).agg(
        marcas=("marcas", "first"),
        preco_max=("preco_max", "first"),
        notas_min=("notas", "max"),
        notas_max=("notas", "sum"),
        termos_coletados=("termo", "nunique"),
        termos_total=("termos_total", "first"),
        atualizado_em=("atualizado_em", "min"),
    )
    table = table.sort_values(["notas_max", "principio_ativo"], ascending=[False, True])
    table.to_csv(MEDICINES, index=False, encoding="utf-8", lineterminator="
")


def append_history(today: str, term: str, notes: int) -> None:
    is_new = not HISTORY.exists()
    with HISTORY.open("a", encoding="utf-8", newline="\n") as file:
        if is_new:
            file.write("data,termo,notas\n")
        escaped = term.replace('"', '""')
        file.write(f'{today},"{escaped}",{notes}\n')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=55, help="stop asking after this many minutes")
    args = parser.parse_args()

    deadline = time.monotonic() + args.minutes * 60
    today = date.today().isoformat()
    terms = read_terms()
    counts = read_counts()
    rows = {row["termo"]: row for row in counts.to_dict("records")}
    queue = build_queue(terms, counts)
    search = NatJusSearch()

    try:
        if search.count(CANARY_TERM) == 0:
            print(f"{CANARY_TERM} returned 0 notes: the search changed, so nothing was saved")
            return 1
    except SearchFailed as error:
        print(f"Search unavailable: {error}")
        return 1

    done = failures_in_a_row = suspicious = 0
    started = time.monotonic()
    for term in queue.itertuples():
        if time.monotonic() >= deadline:
            break
        time.sleep(PAUSE_SECONDS)
        try:
            notes = search.count(term.termo)
        except SearchFailed as error:
            failures_in_a_row += 1
            print(f"FAILED {error}")
            if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                print("Stopping: too many failures in a row")
                break
            continue
        failures_in_a_row = 0

        previous = rows.get(term.termo, {}).get("notas")
        had_notes = previous not in (None, "") and int(previous) > 0
        if notes == 0 and had_notes:  # keep the count, but move on so the queue does not stall
            suspicious += 1
            print(f"SUSPICIOUS {term.termo}: 0 notes, had {previous}; keeping {previous}")
            notes = int(previous)
        elif previous in (None, "") or int(previous) != notes:
            append_history(today, term.termo, notes)

        price = "" if pd.isna(term.preco_max) else term.preco_max
        rows[term.termo] = {"termo": term.termo, "notas": notes, "preco_max": price, "atualizado_em": today}
        save_counts(rows)
        done += 1

    save_medicines()
    elapsed = time.monotonic() - started
    known = sum(1 for term in terms["termo"] if term in rows)
    print(f"Collected {done} terms in {elapsed / 60:.1f} min ({elapsed / max(done, 1):.1f} s per term)")
    print(f"Catalog progress: {known}/{len(terms)} terms have a count; {suspicious} suspicious")
    return 1 if failures_in_a_row >= MAX_FAILURES_IN_A_ROW else 0


if __name__ == "__main__":
    sys.exit(main())
