# Courlan Command-Line Interface

The main functions are also available through the `courlan` command-line utility, which reads URLs from a text file and writes accepted ones to an output file.

```bash
courlan -i INPUTFILE -o OUTPUTFILE [options]
```

## Flags

| Flag | Description |
|------|-------------|
| `-i, --inputfile` | Input file — one URL per line (required) |
| `-o, --outputfile` | Output file (required) |
| `-d, --discardedfile` | Write rejected URLs to this file |
| `-v, --verbose` | Enable debug logging |
| `-p, --parallel` | Worker processes for batch mode (default: number of CPUs) |
| `--strict` | Enable more restrictive filtering |
| `-l, --language` | Keep only URLs matching this ISO 639-1 code (e.g. `en`, `de`) |
| `-r, --redirects` | Check HTTP redirects (slow — see below) |
| `--sample N` | Sample N URLs per domain instead of full processing |
| `--exclude-min N` | Skip domains with fewer than N URLs (sampling only) |
| `--exclude-max N` | Skip domains with more than N URLs (sampling only) |
| `--stratified-budget N` | Select N URLs in total, allocated across site/path/source strata |
| `--seed N` | Seed of the stratified draw, for reproducible samples (default: 0) |
| `--minimum N` | Minimum coverage attempted for every stratum (default: 0) |
| `--weights FILE` | JSON file with relative budget shares per stratum |
| `--source-column` | Read each source from the tab-separated second field |
| `--manifest FILE` | Write a rerunnable JSON manifest with per-stratum quotas |

## Behavior

- **Batch mode** (default): processes all URLs, writes accepted URLs to `--outputfile` and rejected ones to `--discardedfile` if specified. Parallelism controlled by `-p`.
- **Sampling mode** (`--sample`): samples N URLs per domain. Only `-i`, `-o`, `--strict`, `-l`, `-v` and the `--exclude-*` bounds apply. `-p`, `-r` and `-d` are rejected. `--exclude-*` without `--sample` is rejected too, and `-p` and `--sample` take positive integers.
- **Stratified mode** (`--stratified-budget`): selects at most N URLs in total, split across site/path/source strata with `--minimum` floors, optional `--weights`, a `--seed` for reproducible draws and a `--manifest` for per-stratum quotas. It cannot be combined with `--sample`, `-p`, `-r`, `-d` or `--exclude-*`. If every stratum is smaller than the budget, the output is shorter than requested and a warning explains the unused slots.
- **Input**: UTF-8 with or without BOM. Blank lines are skipped. Lines with invalid UTF-8 go to `--discardedfile` in batch mode. In sampling mode they are skipped, and `-v` logs their count. The run is refused if an output file is the input file, or if `--outputfile` and `--discardedfile` are the same file. Unreadable or unwritable files end the run with an error and exit code 2. If the output is piped to a reader that stops early, such as `-o /dev/stdout | head`, the run ends quietly with exit code 141.

## Examples

### Basic filtering

```bash
courlan -i urls.txt -o cleaned.txt -d discarded.txt
```

:::{dropdown} Show input/output
:icon: file-code

**Input** (`urls.txt`):
```
https://www.example.com/page1
https://www.example.com/page2
https://example.com/archive
https://cdn.example.com/image.jpg
https://example.org/article
```

**`cleaned.txt`** (accepted):
```
https://www.example.com/page1
https://www.example.com/page2
https://example.com/archive
https://example.org/article
```

**`discarded.txt`** (rejected):
```
https://cdn.example.com/image.jpg
```
:::

### Strict filtering with language detection

```bash
courlan -i urls.txt -o cleaned.txt -d discarded.txt --strict -l en
```

More restrictive filtering applied; only English URLs kept.

### Parallel processing with verbose output

```bash
courlan -i urls.txt -o cleaned.txt -p 4 -v
```

4 worker processes, debug logging for each URL processing step.

### Sampling by domain

```bash
courlan -i large_urls.txt -o sample.txt --sample 2 --exclude-min 2
```

:::{dropdown} Show input/output
:icon: file-code

**Input** (`large_urls.txt`):
```
https://github.com/adbar/courlan
https://github.com/adbar/trafilatura
https://github.com/adbar/htmldate
https://example.com/page1
https://example.com/page2
https://example.com/page3
https://another.org/article
```

**`sample.txt`** (2 random URLs per domain, domains with <2 URLs excluded):
```
https://github.com/adbar/courlan
https://github.com/adbar/trafilatura
https://example.com/page1
https://example.com/page2
```
:::

### Stratified sampling with a total budget

Protect small sites and keep a minimum per stratum while selecting only 500 URLs:

```bash
courlan -i large_urls.txt -o trial.txt \
  --stratified-budget 500 --seed 42 --minimum 5 --manifest manifest.json
```

The manifest reports the quota, pool size and selected count for every
`(site, category, source)` stratum, along with reasons whenever a minimum or
the global budget could not be met. Inputs carrying an explicit source use a
tab-separated second field with `--source-column`; `--weights weights.json`
steers the remaining budget toward chosen strata:

```json
[
  {"key": ["https://small.example", "news", "unknown"], "weight": 1000}
]
```

## Large inputs

For very large files (>1M URLs), split into chunks to limit memory usage:

```bash
split -l 100000 urls.txt urls_chunk_
for chunk in urls_chunk_*; do
    courlan -i "$chunk" -o "out_$chunk" -p 4
done
```

## Redirect checking (`-r`)

Redirect checks require an HTTP HEAD request per URL and can be slow.

**Use when:**
- You need to resolve redirect chains
- The dataset is small (<10k URLs)

**Avoid when:**
- Doing initial bulk filtering
- Processing large URL lists (>100k)
- Network latency is a concern
