"""
datajud.py: time to judgment and outcome of medicine supply lawsuits, per court.

For each court, a random sample of first-instance lawsuits (grau G1 and JE) filed in
2021-2023 under the subject "Fornecimento de medicamentos" (TPU 12484, or 11884 used
before it) is read from the CNJ DataJud public API. For each lawsuit it finds the
first judgment on the merits (TPU 219 procedência, 220 improcedência, 221 procedência
em parte) and the days from filing to it.

DataJud does not say which medicine a lawsuit is about, so these figures are per
court and not per medicine. Lawsuits ended without a judgment on the merits (settled,
dismissed) or still running count in `amostra` but not in `com_julgamento`.
A lawsuit that went to appeal is judged again, and only the first instance is read.
Filing years end in 2023 so that most lawsuits have had time to be judged.

Source: Conselho Nacional de Justiça (CNJ), Base Nacional de Dados do Poder
Judiciário (DataJud). The API terms require citing both and telling CNJ about
anything published with the data.
"""

import argparse
import re
import statistics
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
OUTPUT = ROOT / "data" / "judiciario.csv"

API_URL = "https://api-publica.datajud.cnj.jus.br/api_publica_{court}/_search"
KEY_PAGE = "https://datajud-wiki.cnj.jus.br/api-publica/acesso/"
USER_AGENT = "Mozilla/5.0 (compatible; remedio-judicial; +https://github.com/dwoloszin/remedio-judicial)"

COURTS = [
    "tjac", "tjal", "tjam", "tjap", "tjba", "tjce", "tjdft", "tjes", "tjgo", "tjma", "tjmg", "tjms", "tjmt", "tjpa",
    "tjpb", "tjpe", "tjpi", "tjpr", "tjrj", "tjrn", "tjro", "tjrr", "tjrs", "tjsc", "tjse", "tjsp", "tjto",
    "trf1", "trf2", "trf3", "trf4", "trf5", "trf6",
]
SUBJECTS = [12484, 11884]  # Fornecimento de medicamentos: current and earlier TPU code
FIRST_INSTANCE = ["G1", "JE"]
FILED_FROM, FILED_UNTIL = "20210101000000", "20240101000000"
PERIOD = "2021-2023"
JUDGMENTS = {219: "procedentes", 220: "improcedentes", 221: "parciais"}

SAMPLE_SIZE = 1000
PAGE_SIZE = 100
RANDOM_SEED = 2026
REFRESH_DAYS = 30
PAUSE_SECONDS = 3
REQUEST_TIMEOUT = 120
GRACE_MINUTES = 3  # a court still running this long after --minutes is dropped, so the run can save
RETRY_WAITS = (15, 45, 90)

COLUMNS = ["tribunal", "periodo", "acoes", "amostra", "com_julgamento", "procedentes", "parciais",
           "improcedentes", "pct_favoravel", "dias_mediana", "dias_p25", "dias_p75", "atualizado_em"]


class ApiFailed(Exception):
    """DataJud gave no usable answer."""


hard_deadline = float("inf")


def check_time(court: str) -> None:
    if time.monotonic() >= hard_deadline:
        raise ApiFailed(f"{court}: out of time, measured again on the next run")


def api_key() -> str:
    """The public key is published on the DataJud wiki and CNJ can change it at any time."""
    page = requests.get(KEY_PAGE, headers={"User-Agent": USER_AGENT}, timeout=60)
    page.raise_for_status()
    match = re.search(r"APIKey\s+([A-Za-z0-9+/]{30,}=*)", re.sub(r"<[^>]+>", " ", page.text))
    if not match:
        raise ApiFailed(f"public API key not found on {KEY_PAGE}")
    return match.group(1)


def search(session: requests.Session, court: str, body: dict) -> dict:
    """An answer with failed shards is incomplete, so it is retried; the last one is kept."""
    partial = None
    for wait in (*RETRY_WAITS, None):
        check_time(court)
        try:
            response = session.post(API_URL.format(court=court), json=body, timeout=REQUEST_TIMEOUT)
            answer = response.json()
            if "hits" in answer and not answer.get("_shards", {}).get("failed"):
                return answer
            if "hits" in answer:
                partial = answer
                problem = f"{answer['_shards']['failed']} shards failed"
            else:
                problem = str(answer.get("error", f"HTTP {response.status_code}"))[:120]
        except (requests.RequestException, ValueError) as error:
            problem = type(error).__name__
        if wait is None:
            if partial:
                print(f"  {court}: keeping a partial page ({problem})")
                return partial
            raise ApiFailed(f"{court}: {problem}")
        time.sleep(wait)
    raise AssertionError("unreachable")


def lawsuits_query() -> dict:
    return {"bool": {
        "filter": [
            {"terms": {"assuntos.codigo": SUBJECTS}},
            {"range": {"dataAjuizamento": {"gte": FILED_FROM, "lt": FILED_UNTIL}}},
        ],
        "should": [{"match": {"grau": level}} for level in FIRST_INSTANCE],
        "minimum_should_match": 1,
    }}


def parse_day(value: str):
    digits = re.sub(r"\D", "", value or "")[:8]
    try:
        return datetime.strptime(digits, "%Y%m%d").date()
    except ValueError:
        return None


def first_judgment(source: dict):
    """(outcome, days from filing) of the first judgment on the merits, or None."""
    movements = [(parse_day(m.get("dataHora")), m.get("codigo")) for m in source.get("movimentos", [])]
    movements = [(day, code) for day, code in movements if day]
    # Some courts record a filing date later than the lawsuit's first movement.
    starts = [day for day in [parse_day(source.get("dataAjuizamento"))] + [day for day, _ in movements] if day]
    judgments = sorted((day, code) for day, code in movements if code in JUDGMENTS)
    if not starts or not judgments:
        return None
    day, code = judgments[0]
    return JUDGMENTS[code], (day - min(starts)).days


def measure(session: requests.Session, court: str) -> dict:
    query = lawsuits_query()
    total = search(session, court, {"size": 0, "track_total_hits": True, "query": query})["hits"]["total"]["value"]
    sampled, outcomes, days = 0, {name: 0 for name in JUDGMENTS.values()}, []
    random_order = {"function_score": {"query": query, "boost_mode": "replace",
                                       "random_score": {"seed": RANDOM_SEED, "field": "_seq_no"}}}
    for start in range(0, min(total, SAMPLE_SIZE), PAGE_SIZE):
        check_time(court)
        time.sleep(PAUSE_SECONDS)
        body = {"from": start, "size": PAGE_SIZE, "query": random_order,
                "_source": ["dataAjuizamento", "movimentos.codigo", "movimentos.dataHora"]}
        hits = search(session, court, body)["hits"]["hits"]
        if not hits:
            break
        sampled += len(hits)
        for hit in hits:
            judgment = first_judgment(hit["_source"])
            if judgment:
                outcomes[judgment[0]] += 1
                days.append(judgment[1])

    judged = sum(outcomes.values())
    quartiles = statistics.quantiles(days, n=4) if len(days) >= 2 else [None, None, None]
    favorable = outcomes["procedentes"] + outcomes["parciais"]
    return {
        "tribunal": court.upper(), "periodo": PERIOD, "acoes": total, "amostra": sampled,
        "com_julgamento": judged, **outcomes,
        "pct_favoravel": round(100 * favorable / judged, 1) if judged else None,
        "dias_mediana": statistics.median(days) if days else None,
        "dias_p25": quartiles[0], "dias_p75": quartiles[2],
        "atualizado_em": date.today().isoformat(),
    }


def read_results() -> dict:
    if not OUTPUT.exists():
        return {}
    return {row["tribunal"]: row for row in pd.read_csv(OUTPUT, keep_default_na=False).to_dict("records")}


def save_results(rows: dict) -> None:
    table = pd.DataFrame(rows.values(), columns=COLUMNS).sort_values("tribunal")
    table.to_csv(OUTPUT, index=False, encoding="utf-8", lineterminator="\n")


def courts_due(rows: dict, chosen: list) -> list:
    """Courts never measured first, then the oldest, skipping those measured recently."""
    today = date.today()
    due = []
    for court in chosen:
        updated = rows.get(court.upper(), {}).get("atualizado_em", "")
        age = (today - date.fromisoformat(updated)).days if updated else None
        if age is None or age >= REFRESH_DAYS:
            due.append((updated, court))
    return [court for _, court in sorted(due)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=55, help="stop starting courts after this many minutes")
    parser.add_argument("--courts", help="comma-separated list, such as trf5,tjsc (default: all, when due)")
    args = parser.parse_args()

    global hard_deadline
    deadline = time.monotonic() + args.minutes * 60
    hard_deadline = deadline + GRACE_MINUTES * 60
    rows = read_results()
    chosen = args.courts.lower().split(",") if args.courts else COURTS
    queue = chosen if args.courts else courts_due(rows, chosen)
    if not queue:
        print("Every court was measured in the last 30 days")
        return 0

    session = requests.Session()
    try:
        session.headers.update({"User-Agent": USER_AGENT, "Authorization": f"APIKey {api_key()}"})
    except (ApiFailed, requests.RequestException) as error:
        print(f"DataJud unavailable: {error}")
        return 1

    failed = []
    for court in queue:
        if time.monotonic() >= deadline:
            break
        started = time.monotonic()
        try:
            row = measure(session, court)
        except ApiFailed as error:
            failed.append(court)
            print(f"FAILED {error}")
            continue
        rows[row["tribunal"]] = row
        save_results(rows)
        print(f"{row['tribunal']}: {row['acoes']:,} lawsuits, {row['amostra']} sampled, {row['com_julgamento']} judged, "
              f"{row['pct_favoravel']}% favorable, median {row['dias_mediana']} days ({(time.monotonic() - started) / 60:.1f} min)")

    print(f"{len(rows)}/{len(COURTS)} courts measured" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed and len(failed) == len(queue) else 0


if __name__ == "__main__":
    sys.exit(main())
