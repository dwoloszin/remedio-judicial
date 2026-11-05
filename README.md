# remedio-judicial

Tracks how often each medicine appears in Brazilian health litigation, using
technical notes from CNJ's public e-NatJus search.

The counts are **technical notes, not lawsuits**: a note exists only when someone
requested one in a case, so the real number of lawsuits is different.

Only totals per medicine are stored. Individual notes are never downloaded or
published, because they contain patient details.

Source: Conselho Nacional de Justiça (CNJ), e-NatJus.

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

The search matches the whole name and is accent-sensitive, and e-NatJus records the
same medicine with and without its salt (`ESILATO DE NINTEDANIBE` and `NINTEDANIBE`).
That is why one medicine can have two search terms.

```bash
pip install -r requirements.txt
cp .env.template .env        # one DATABASE_URL_<STORE> per store database
python catalog.py            # add --refresh-cmed to download the list again
```

## Daily collection

`.github/workflows/collect.yml` runs `collect.py` every day at 08:00 UTC for up to 55
minutes, pausing 6 seconds between searches. Each run continues where the last stopped:
extras first, then the highest price, and then whichever count is oldest.

| File | Content |
|------|---------|
| `data/notas.csv` | latest count per search term |
| `data/historico.csv` | one row each time a count changes |

After each run, `save_db.py` copies the CSVs into the tables `catalogo`, `notas` and
`historico` of `JUDICIAL_DB_URL`, a repo secret. The CSVs remain the source of truth.

These counts are per search term. The public search shows fewer notes than the CNJ
dashboard, and how many fewer varies by medicine, so compare a medicine with its own
past rather than ranking medicines against each other.

Source: Conselho Nacional de Justiça (CNJ), e-NatJus. CMED/Anvisa for prices.
