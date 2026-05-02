# dnsmasq-china-list-plus

Clean local `accelerated-domains.china.conf` in two stages:

1. Remove entries already present in upstream dnsmasq China list.
2. Geo-check remaining domains via IPInfo and remove non-China domains.

## What this does

The script:

1. Downloads latest upstream file from:

   - `https://raw.githubusercontent.com/felixonmars/dnsmasq-china-list/refs/heads/master/accelerated-domains.china.conf`

2. Extracts domains from lines like:

   - `server=/example.com/114.114.114.114`

3. Removes local lines whose domain already exists upstream.

4. For every domain left after de-duplication:
   - resolves both IPv4 and IPv6 using DNS
   - queries `ipinfo.io` for each resolved IP
   - keeps the domain if **any** resolved IP is in `CN` (Option A)
   - removes the domain if it has resolved IPs and **none** are `CN`
   - keeps unresolved domains and prints a warning

5. Prints comprehensive report counts and sample domains.

By default, it creates a `.bak` backup before writing changes.

---

## Requirements

- Python 3.11+
- Internet access
- IPInfo API token

No third-party Python dependencies are required (stdlib only).

---

## Setup Python virtual environment

From project root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -V
```

---

## Configure IPInfo token

Create `.env` in project root:

```dotenv
IPINFO_TOKEN=your_ipinfo_token_here
```

If you prefer another key name, pass `--ipinfo-token-key YOUR_KEY`.

---

## Usage

### Dry run (recommended first)

```bash
python3 dedupe_accelerated_domains.py --dry-run --verbose
```

### Apply changes

```bash
python3 dedupe_accelerated_domains.py --verbose
```

### Retry/backoff tuning for flaky networks

```bash
python3 dedupe_accelerated_domains.py --dry-run --verbose --retries 6 --retry-delay 2 --timeout 30
```

### Custom paths

```bash
python3 dedupe_accelerated_domains.py \
  --local-file accelerated-domains.china.conf \
  --env-file .env
```

---

## Key CLI options

- `--dry-run`: report only, no file writes
- `--no-backup`: disable `.bak` creation
- `--retries`: retries after first HTTP attempt (default `4`)
- `--retry-delay`: exponential backoff base seconds (default `1.5`)
- `--timeout`: request timeout in seconds (default `20`)
- `--show-examples`: sample domains shown per section (default `25`)
- `--env-file`: path to `.env` (default `.env`)
- `--ipinfo-token-key`: token key name (default `IPINFO_TOKEN`)
- `--verbose`: print detailed progress and retries

---

## Output behavior details

- Duplicate domains vs upstream are removed first.
- Non-CN domains are removed second.
- Unresolved domains are kept and emitted as warnings like:
  - `WARNING: No resolvable IP for domain: example.com`

---

## Example report excerpt

```text
=== dnsmasq cleanup report ===
Upstream URL: https://raw.githubusercontent.com/felixonmars/dnsmasq-china-list/refs/heads/master/accelerated-domains.china.conf
Local file:   accelerated-domains.china.conf
Env file:     .env
Upstream lines read: 12345
Upstream domains extracted: 12280
Local lines read: 245
Duplicate local entries removed: 190
Unique duplicate domains removed: 190
Post-dedupe domains geo-evaluated: 55
Non-CN entries removed: 12
Unique non-CN domains removed: 12
Unresolved domains kept (warnings): 3
IPInfo lookup errors encountered: 1
Remaining local lines: 40
```

---

## Troubleshooting

- **Missing token error**
  - Ensure `.env` contains `IPINFO_TOKEN=...`

- **IPInfo quota/rate-limit issues**
  - Increase retries and delay, or use a higher quota plan

- **No local file found**
  - Run from project root or pass `--local-file`

- **Restore previous file**
  - `mv accelerated-domains.china.conf.bak accelerated-domains.china.conf`
