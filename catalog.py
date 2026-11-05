"""
catalog.py: build data/catalogo.csv, the list of active ingredients to search.

A store product name rarely states its active ingredient reliably, so each store
EAN is looked up in the official CMED/Anvisa price list, which gives the substance.
A substance enters the catalog when at least one store sells one of its EANs.
data/extras.csv adds what this misses, such as products outside the CMED list.

The e-NatJus search matches the whole name and is accent-sensitive, and the same
medicine is recorded both with and without its salt. So each row keeps the CMED
spelling and lists every name to search in `termos_busca`.

The output has no store names or store prices, because it is published. `preco_max`
is the highest CMED factory price (PF 18%) among the presentations of the substance.
"""

import argparse
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import psycopg2
import requests

ROOT = Path(__file__).parent
OUTPUT = ROOT / "data" / "catalogo.csv"
EXTRAS = ROOT / "data" / "extras.csv"
CACHE = ROOT / "output" / "cmed_pmc.xlsx"
CMED_PAGE = "https://www.gov.br/anvisa/pt-br/assuntos/medicamentos/cmed/precos"
USER_AGENT = "Mozilla/5.0 (compatible; remedio-judicial; +https://github.com/dwoloszin/remedio-judicial)"
ENV_PREFIX = "DATABASE_URL_"
MAX_BRANDS = 8
MAX_FAILED_STORES = 5  # more than this would drop medicines from the catalog
TERM_SEPARATOR = " | "
MINERALS = {"SODIO", "POTASSIO", "CALCIO", "MAGNESIO", "ZINCO", "FERRO", "ALUMINIO", "LITIO", "BARIO", "PRATA", "SELENIO", "COBRE", "MANGANES", "AMONIO"}
PRICE_COLUMN = "PF 18 %"  # factory price: hospital-only products have no consumer price (PMC)

SALT_PREFIX = re.compile(r"^[A-ZÀ-Ý-]+(?:ATO|ETO|ITO|IDRATO) DE (?=\S)")
SALT_SUFFIX = re.compile(
    r"\s+(?:DI)?(?:SÓDIC|POTÁSSIC|CÁLCIC|MAGNÉSIC)[AO]$"
    r"|\s+(?:MONO|DI|TRI|SESQUI|HEMI-?)?(?:H|-H)?IDRATAD[AO]$"
)


def load_env(path: Path = ROOT / ".env") -> None:
    """Load KEY=VALUE lines without overriding variables already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*([A-Za-z_]\w*)\s*=\s*(.*?)\s*$", line)
        if match:
            os.environ.setdefault(match.group(1), match.group(2).strip("\"'"))


def plain(text) -> str:
    """Uppercase without accents, with single spaces."""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().upper()


def clean_name(text) -> str:
    """Single spaces, keeping the accents the search depends on."""
    return re.sub(r"\s+", " ", str(text)).strip().upper()


def without_salt(name: str) -> str:
    """'CLORIDRATO DE DULOXETINA' -> 'DULOXETINA', 'ENOXAPARINA SÓDICA' -> 'ENOXAPARINA'."""
    base = SALT_PREFIX.sub("", name)
    while (shorter := SALT_SUFFIX.sub("", base)) != base:
        base = shorter
    return name if plain(base) in MINERALS else base


def search_terms(name: str) -> str:
    """The CMED name, plus the name without its salt. Combinations are searched as written."""
    base = name if ";" in name else without_salt(name)
    return TERM_SEPARATOR.join(dict.fromkeys([name, base]))


def to_price(value) -> float:
    """'2098,20' -> 2098.2. Returns NaN when the cell has no price."""
    return pd.to_numeric(str(value).replace(".", "").replace(",", "."), errors="coerce")


def clean_ean(value) -> str:
    """Digits only, without leading zeros. Returns '' for anything too short to be an EAN."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    digits = re.sub(r"\D", "", str(value).removesuffix(".0")).lstrip("0")
    return digits if len(digits) >= 8 else ""


def download_cmed(refresh: bool = False) -> Path:
    """Find the current PMC spreadsheet on the CMED page and download it."""
    if CACHE.exists() and not refresh:
        return CACHE
    headers = {"User-Agent": USER_AGENT}
    page = requests.get(CMED_PAGE, headers=headers, timeout=60)
    page.raise_for_status()
    match = re.search(r'href="([^"]*lista_pmc[^"]*\.xlsx[^"]*)"', page.text)
    if not match:
        raise SystemExit(f"PMC spreadsheet link not found on {CMED_PAGE}")
    url = urljoin(CMED_PAGE, match.group(1))
    print(f"CMED: {url}")
    response = requests.get(url, headers=headers, timeout=180)
    response.raise_for_status()
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_bytes(response.content)
    return CACHE


def read_cmed(path: Path) -> pd.DataFrame:
    """One row per (ean, substance), read from the spreadsheet."""
    for raw in pd.read_excel(path, header=None, dtype=str, sheet_name=None).values():
        is_header = raw.head(100).apply(lambda row: "SUBSTANCIA" in {plain(v) for v in row.dropna()}, axis=1)
        if is_header.any():
            break
    else:
        raise SystemExit("CMED spreadsheet has no SUBSTANCIA column: the format changed")
    start = is_header.idxmax()
    table = raw.iloc[start + 1:].copy()
    table.columns = [plain(c) for c in raw.iloc[start]]

    ean_columns = [c for c in table.columns if c.startswith("EAN")]
    required = {"SUBSTANCIA", "PRODUTO", "CLASSE TERAPEUTICA", PRICE_COLUMN}
    missing = required - set(table.columns)
    if missing or not ean_columns:
        raise SystemExit(f"CMED spreadsheet is missing columns: {sorted(missing) or 'EAN'}")

    rows = table.melt(id_vars=sorted(required), value_vars=ean_columns, value_name="raw_ean")
    rows["ean"] = rows["raw_ean"].map(clean_ean)
    rows = rows[(rows["ean"] != "") & rows["SUBSTANCIA"].notna()]
    return pd.DataFrame({
        "ean": rows["ean"],
        "principio_ativo": rows["SUBSTANCIA"].map(clean_name),
        "marca": rows["PRODUTO"].map(clean_name),
        "classe_terapeutica": rows["CLASSE TERAPEUTICA"].map(clean_name),
        "preco": rows[PRICE_COLUMN].map(to_price),
    }).drop_duplicates()


def store_eans(store: str, url: str) -> pd.DataFrame:
    connection = psycopg2.connect(url, connect_timeout=30)
    try:
        connection.set_session(readonly=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT DISTINCT ean FROM offers WHERE ean IS NOT NULL")
            eans = {clean_ean(row[0]) for row in cursor.fetchall()} - {""}
    finally:
        connection.close()
    return pd.DataFrame({"ean": sorted(eans), "store": store})


def read_stores(workers: int = 12) -> pd.DataFrame:
    urls = {k[len(ENV_PREFIX):].lower(): v for k, v in os.environ.items()
            if k.startswith(ENV_PREFIX) and v.startswith("postgres")}
    if not urls:
        raise SystemExit(f"no {ENV_PREFIX}<STORE> variables found")
    frames, failed = [], []
    with ThreadPoolExecutor(workers) as pool:
        futures = {store: pool.submit(store_eans, store, url) for store, url in urls.items()}
        for store, future in futures.items():
            try:
                frames.append(future.result())
            except psycopg2.Error as error:
                failed.append(store)
                print(f"  {store}: {type(error).__name__}")  # the message could hold the host
    print(f"{len(frames)}/{len(urls)} stores read" + (f"; failed: {', '.join(failed)}" if failed else ""))
    if not frames or len(failed) > MAX_FAILED_STORES:
        raise SystemExit(f"{len(failed)} stores could not be read: the catalog was not changed")
    return pd.concat(frames, ignore_index=True)


COLUMNS = ["principio_ativo", "preco_max", "termos_busca", "marcas", "classe_terapeutica", "n_eans", "n_lojas", "origem"]


def build(cmed: pd.DataFrame, stores: pd.DataFrame) -> pd.DataFrame:
    matched = stores.merge(cmed, on="ean")
    catalog = matched.groupby("principio_ativo").agg(
        marcas=("marca", lambda s: " | ".join(sorted(set(s))[:MAX_BRANDS])),
        classe_terapeutica=("classe_terapeutica", lambda s: s.mode().iat[0]),
        n_eans=("ean", "nunique"),
        n_lojas=("store", "nunique"),
    ).reset_index()
    catalog["termos_busca"] = catalog["principio_ativo"].map(search_terms)
    catalog["origem"] = "cmed"

    extras = pd.read_csv(EXTRAS, dtype=str).fillna("")
    extras = extras[~extras["principio_ativo"].isin(catalog["principio_ativo"])]
    extras = extras.assign(n_eans=0, n_lojas=0, origem="extras")
    catalog = pd.concat([catalog, extras[catalog.columns]], ignore_index=True)
    catalog["preco_max"] = catalog["principio_ativo"].map(cmed.groupby("principio_ativo")["preco"].max()).round(2)
    return catalog[COLUMNS].sort_values("principio_ativo")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-cmed", action="store_true", help="download the CMED list again")
    args = parser.parse_args()

    load_env()
    cmed = read_cmed(download_cmed(args.refresh_cmed))
    stores = read_stores()
    catalog = build(cmed, stores)

    OUTPUT.parent.mkdir(exist_ok=True)
    catalog.to_csv(OUTPUT, index=False, encoding="utf-8", lineterminator="\n")

    store_total = stores["ean"].nunique()
    store_matched = stores.loc[stores["ean"].isin(cmed["ean"]), "ean"].nunique()
    print(f"CMED: {cmed['ean'].nunique():,} EANs, {cmed['principio_ativo'].nunique():,} substances")
    print(f"Stores: {store_total:,} distinct EANs, {store_matched:,} found in CMED")
    terms = catalog["termos_busca"].str.split(TERM_SEPARATOR, regex=False).explode().nunique()
    print(f"Catalog: {len(catalog):,} substances, {terms:,} search terms -> {OUTPUT}")


if __name__ == "__main__":
    main()
