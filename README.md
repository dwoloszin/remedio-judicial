# remedio-judicial

Tracks how often each medicine appears in Brazilian health litigation, using
technical notes from CNJ's public e-NatJus search, and how long courts take to judge
medicine supply lawsuits, using CNJ's DataJud API.

The counts are **technical notes, not lawsuits**: a note exists only when someone
requested one in a case, so the real number of lawsuits is different.

Only totals per medicine are stored. Individual notes are never downloaded or
published, because they contain patient details.

Sources: Conselho Nacional de Justiça (CNJ), e-NatJus and Base Nacional de Dados do
Poder Judiciário (DataJud). CMED/Anvisa for prices.

## Catalog

`data/catalogo.csv` lists the medicines to search. `catalog.py` builds it by looking
up each store EAN in the [CMED/Anvisa price list](https://www.gov.br/anvisa/pt-br/assuntos/medicamentos/cmed/precos),
which gives the active ingredient. It has no store names or store prices.

`data/extras.csv` is edited by hand and adds what that lookup misses, such as cannabidiol.

| Column | Content |
|--------|---------|
| `principio_ativo` | substance as written by CMED; combinations are separated by `;` |
| `preco_max` | highest CMED factory price (PF 18%) among its presentations |
| `termos_busca` | names to search, separated by ` \| ` |
| `marcas` | up to 8 product names from CMED |
| `classe_terapeutica` | therapeutic class from CMED |
| `n_eans`, `n_lojas` | how many EANs and how many stores matched |
| `origem` | `cmed` or `extras` |

The search is accent-sensitive, and e-NatJus can record the same medicine with and
without its salt (`ESILATO DE NINTEDANIBE` and `NINTEDANIBE`). That is why one medicine
can have two search terms.

`.github/workflows/catalog.yml` rebuilds it on day 1 of each month. This is the only
workflow that reads the store databases, using the `STORE_DATABASE_URLS` repo secret
(the `DATABASE_URL_<STORE>` lines of `.env`). The daily collection does not read them.

```bash
pip install -r requirements.txt
cp .env.template .env        # one DATABASE_URL_<STORE> per store database
python catalog.py            # add --refresh-cmed to download the list again
```

## Daily collection

`.github/workflows/collect.yml` runs `collect.py` every day at 08:00 UTC for up to 55
minutes, pausing 6 seconds between searches. Each run continues where the last stopped:
extras first, then the highest price, and then whichever count is oldest. Each term
takes two searches: all notes, and notes with a favorable conclusion.

| File | Content |
|------|---------|
| `data/notas.csv` | latest counts per search term: `notas` and `favoraveis` |
| `data/historico.csv` | one row each time a term's total changes |
| `data/medicamentos.csv` | one row per substance: range of notes and share of favorable opinions |

A substance with several terms gets a range, because terms can overlap: some share no
notes and others are nested. `notas_min` is the largest term and `notas_max` is their
sum. Substances can also share a term, so these rows must not be added together.

`pct_favoravel` is the share of notes whose technical conclusion favored the patient,
taken from the largest term (`termo_principal`). It is the opinion the judge receives,
not the judge's decision.

These counts are per search term. The public search shows fewer notes than the CNJ
dashboard, and how many fewer varies by medicine, so compare a medicine with its own
past rather than ranking medicines against each other.

## Courts

`.github/workflows/judiciario.yml` runs `datajud.py` every day at 10:00 UTC. It measures
each court once every 30 days, so most days it ends at once.

For each of the 27 state courts and 6 federal regional courts, it reads a random sample
of up to 1,000 first-instance lawsuits filed in 2021-2023 under "Fornecimento de
medicamentos" (TPU 12484, or 11884 before it). From each lawsuit it takes the first
judgment on the merits and the days since filing.

| Column of `data/judiciario.csv` | Content |
|--------|---------|
| `acoes` | lawsuits matching the filter |
| `amostra`, `com_julgamento` | lawsuits read, and how many had a judgment on the merits |
| `procedentes`, `parciais`, `improcedentes` | outcome of that judgment (TPU 219, 221, 220) |
| `pct_favoravel` | procedentes plus parciais, over `com_julgamento` |
| `dias_mediana`, `dias_p25`, `dias_p75` | days from filing to judgment |

DataJud does not record which medicine a lawsuit is about, so these figures are per
court only. A favorable judgment does not show that the patient received the medicine,
and later appeals are not read. Lawsuits still running are left out of the times, which
is why filing years stop at 2023.

The DataJud API terms forbid commercial use and require citing CNJ and DataJud as
sources and telling CNJ about anything published with the data.

## Database

After each run, `save_db.py` copies the CSVs into the tables `catalogo`, `notas`,
`medicamentos`, `historico` and `judiciario` of `JUDICIAL_DB_URL`, a repo secret.
The CSVs remain the source of truth.
