#!/usr/bin/env python3
"""Remove local dnsmasq entries duplicated upstream and non-CN domains by IPInfo."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import dns.resolver

UPSTREAM_URL = (
    "https://raw.githubusercontent.com/felixonmars/dnsmasq-china-list/"
    "refs/heads/master/accelerated-domains.china.conf"
)
IPINFO_API_BASE = "https://ipinfo.io"
DEFAULT_CACHE_FILE = Path(".upstream_cache.conf")
DEFAULT_CACHE_TTL = 3600
DEFAULT_GEO_CACHE_FILE = Path(".geo_cache.json")
DEFAULT_GEO_CACHE_TTL = 604800  # 7 days
DEFAULT_DNS_SERVER = "114.114.114.114"
IPINFO_BATCH_SIZE = 1000


class DomainGeoResult:
    """Holds domain-level geolocation outcome."""

    def __init__(
        self,
        domain: str,
        ips: set[str],
        cn_ips: set[str],
        non_cn_ips: set[str],
        unknown_country_ips: set[str],
        unresolved: bool,
    ) -> None:
        self.domain = domain
        self.ips = ips
        self.cn_ips = cn_ips
        self.non_cn_ips = non_cn_ips
        self.unknown_country_ips = unknown_country_ips
        self.unresolved = unresolved


def extract_domain_from_line(line: str) -> str | None:
    """Extract domain from a dnsmasq line in format server=/domain/ip.

    Args:
        line: Raw config line.

    Returns:
        Domain string if parsable; otherwise None.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if not stripped.startswith("server=/"):
        return None

    remainder = stripped[len("server=/") :]
    slash_idx = remainder.find("/")
    if slash_idx <= 0:
        return None

    domain = remainder[:slash_idx].strip()
    return domain or None


def extract_domains(lines: list[str]) -> set[str]:
    """Extract all valid domains from dnsmasq config lines.

    Args:
        lines: File content split into lines.

    Returns:
        Set of extracted domains.
    """
    domains: set[str] = set()
    for line in lines:
        domain = extract_domain_from_line(line)
        if domain is not None:
            domains.add(domain)
    return domains


def load_env_file(env_path: Path) -> dict[str, str]:
    """Load key-value pairs from a .env file.

    Args:
        env_path: Path to .env file.

    Returns:
        Mapping of environment-like keys/values.
    """
    values: dict[str, str] = {}
    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            values[key] = val
    return values


def fetch_upstream_lines(
    url: str,
    timeout: float,
    retries: int,
    retry_delay: float,
    verbose: bool,
) -> list[str]:
    """Download upstream file and return lines with retry/backoff.

    Args:
        url: Upstream URL to download.
        timeout: Request timeout seconds.
        retries: Number of retries after the initial attempt.
        retry_delay: Base delay in seconds between retries.
        verbose: Whether to print retry progress.

    Returns:
        Upstream file lines.

    Raises:
        RuntimeError: If download fails or content can't be decoded.
    """
    if retries < 0:
        raise RuntimeError("Retries must be >= 0.")

    req = Request(
        url,
        headers={
            "User-Agent": "dnsmasq-china-list-plus-dedupe/1.0",
            "Accept": "text/plain",
        },
    )

    attempts = retries + 1
    errors: list[str] = []

    for attempt in range(1, attempts + 1):
        suffix = f" (attempt {attempt}/{attempts})" if attempts > 1 else ""
        print(f"Downloading upstream list{suffix}...")

        try:
            with urlopen(req, timeout=timeout) as resp:
                data = resp.read()

            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError("Upstream file is not valid UTF-8.") from exc

            lines = text.splitlines()
            print(f"Downloaded {len(lines)} lines from upstream.")
            return lines
        except HTTPError as exc:
            msg = f"HTTP error {exc.code}: {exc.reason}"
            errors.append(msg)
            print(f"Download failed: {msg}", file=sys.stderr)
            if 400 <= exc.code < 500 and exc.code != 429:
                raise RuntimeError(
                    "Failed downloading upstream; non-retryable client error: "
                    f"{msg}"
                ) from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            msg = f"Network error: {exc}"
            errors.append(msg)
            print(f"Download failed: {msg}", file=sys.stderr)
        except RuntimeError as exc:
            errors.append(str(exc))
            break

        if attempt < attempts:
            sleep_seconds = max(retry_delay * (2 ** (attempt - 1)), 0.0)
            print(f"Retrying in {sleep_seconds:.1f}s...")
            if verbose:
                print(f"  Last error: {errors[-1]}")
            time.sleep(sleep_seconds)

    error_summary = "; ".join(errors[-3:]) if errors else "Unknown error"
    raise RuntimeError(
        "Failed downloading upstream after "
        f"{attempts} attempt(s). Last errors: {error_summary}"
    )


def fetch_upstream_lines_cached(
    url: str,
    timeout: float,
    retries: int,
    retry_delay: float,
    verbose: bool,
    cache_file: Path,
    cache_ttl: float,
    refresh_cache: bool,
) -> list[str]:
    """Return upstream lines from cache if fresh, otherwise download and cache.

    Falls back to a stale cache if the download fails.

    Args:
        url: Upstream URL to download.
        timeout: Request timeout seconds.
        retries: Number of retries after the initial attempt.
        retry_delay: Base delay in seconds between retries.
        verbose: Whether to print cache expiry details.
        cache_file: Path to the local cache file.
        cache_ttl: Cache TTL in seconds.
        refresh_cache: If true, ignore existing cache and re-download.

    Returns:
        Upstream file lines.

    Raises:
        RuntimeError: If download fails and no cache is available.
    """
    if not refresh_cache and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < cache_ttl:
            print(
                f"Using cached upstream list ({age:.0f}s old, TTL {cache_ttl:.0f}s): "
                f"{cache_file}"
            )
            lines = cache_file.read_text(encoding="utf-8").splitlines()
            print(f"Loaded {len(lines)} lines from cache.")
            return lines
        if verbose:
            print(
                f"Cache expired ({age:.0f}s old, TTL {cache_ttl:.0f}s); re-downloading."
            )

    try:
        lines = fetch_upstream_lines(
            url,
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
            verbose=verbose,
        )
        cache_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Upstream list cached to {cache_file}")
        return lines
    except RuntimeError as exc:
        if cache_file.exists():
            print(
                f"WARNING: Download failed ({exc}); using stale cache: {cache_file}",
                file=sys.stderr,
            )
            return cache_file.read_text(encoding="utf-8").splitlines()
        raise


def load_geo_cache(cache_file: Path) -> dict[str, dict]:
    """Load the geolocation cache from a JSON file.

    Args:
        cache_file: Path to the JSON cache file.

    Returns:
        Mapping of cache keys to their stored entries, or empty dict on
        missing or corrupt file.
    """
    if not cache_file.exists():
        return {}
    try:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_geo_cache(cache: dict[str, dict], cache_file: Path) -> None:
    """Persist the geolocation cache to a JSON file.

    Args:
        cache: Mapping of cache keys to their stored entries.
        cache_file: Path to write the JSON cache file.
    """
    cache_file.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")


def resolve_domain_ips(domain: str, dns_server: str) -> set[str]:
    """Resolve both IPv4 and IPv6 addresses for a domain using a specific DNS server.

    Args:
        domain: Domain to resolve.
        dns_server: IP address of the DNS server to query.

    Returns:
        Set of unique IP strings.
    """
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = [dns_server]
    ips: set[str] = set()
    for rdtype in ("A", "AAAA"):
        try:
            answers = resolver.resolve(domain, rdtype)
            for rdata in answers:
                ips.add(rdata.address)
        except (
            dns.resolver.NXDOMAIN,
            dns.resolver.NoAnswer,
            dns.resolver.NoNameservers,
        ):
            pass
        except dns.exception.DNSException:
            pass
    return ips


def batch_lookup_countries(
    ips: list[str],
    token: str,
    timeout: float,
    retries: int,
    retry_delay: float,
    verbose: bool,
) -> dict[str, str | None]:
    """Look up country codes for multiple IPs using the IPInfo batch API.

    Sends up to IPINFO_BATCH_SIZE IPs per POST request with retry/backoff.
    Failed IPs are mapped to None rather than raising.

    Args:
        ips: Sorted list of IP addresses to look up.
        token: IPInfo API token.
        timeout: HTTP timeout seconds.
        retries: Number of retries after initial attempt.
        retry_delay: Base delay in seconds for exponential backoff.
        verbose: Whether to print retry details.

    Returns:
        Mapping of each IP to its country code (uppercase) or None if the
        lookup failed or the country field was absent.
    """
    results: dict[str, str | None] = {}
    total_chunks = (len(ips) + IPINFO_BATCH_SIZE - 1) // IPINFO_BATCH_SIZE

    for chunk_idx, chunk_start in enumerate(
        range(0, len(ips), IPINFO_BATCH_SIZE), start=1
    ):
        chunk = ips[chunk_start : chunk_start + IPINFO_BATCH_SIZE]

        if total_chunks > 1:
            print(f"IPInfo batch {chunk_idx}/{total_chunks} ({len(chunk)} IPs)...")
        else:
            print(f"IPInfo batch request ({len(chunk)} IPs)...")

        url = f"{IPINFO_API_BASE}/batch?token={token}"
        body = json.dumps(chunk).encode("utf-8")
        attempts = retries + 1
        errors: list[str] = []
        success = False

        for attempt in range(1, attempts + 1):
            try:
                req = Request(
                    url,
                    data=body,
                    headers={
                        "User-Agent": "dnsmasq-china-list-plus-dedupe/1.0",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
                with urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))

                for ip in chunk:
                    ip_data = payload.get(ip)
                    if isinstance(ip_data, dict):
                        country = ip_data.get("country")
                        results[ip] = (
                            country.upper()
                            if isinstance(country, str) and country
                            else None
                        )
                    else:
                        results[ip] = None
                success = True
                break
            except HTTPError as exc:
                msg = f"HTTP error {exc.code}: {exc.reason}"
                errors.append(msg)
                print(f"IPInfo batch request failed: {msg}", file=sys.stderr)
                if 400 <= exc.code < 500 and exc.code not in (408, 429):
                    break
            except (
                URLError, TimeoutError, socket.timeout, json.JSONDecodeError
            ) as exc:
                errors.append(str(exc))
                print(f"IPInfo batch request failed: {exc}", file=sys.stderr)

            if attempt < attempts:
                sleep_seconds = max(retry_delay * (2 ** (attempt - 1)), 0.0)
                print(f"Retrying in {sleep_seconds:.1f}s...")
                if verbose:
                    print(f"  Last error: {errors[-1]}")
                time.sleep(sleep_seconds)

        if not success:
            summary = "; ".join(errors[-3:]) if errors else "unknown error"
            print(
                f"WARNING: IPInfo batch lookup failed after {attempts} attempt(s): "
                f"{summary}",
                file=sys.stderr,
            )
            for ip in chunk:
                if ip not in results:
                    results[ip] = None

    return results


def dedupe_local_file(
    local_path: Path,
    upstream_url: str,
    dry_run: bool,
    backup: bool,
    timeout: float,
    show_examples: int,
    retries: int,
    retry_delay: float,
    verbose: bool,
    env_file: Path,
    ipinfo_token_key: str,
    cache_file: Path,
    cache_ttl: float,
    refresh_cache: bool,
    geo_cache_file: Path,
    geo_cache_ttl: float,
    refresh_geo_cache: bool,
    dns_server: str,
) -> int:
    """Remove local duplicate and non-CN domains from local config.

    Args:
        local_path: Path to local accelerated-domains file.
        upstream_url: URL for upstream source list.
        dry_run: If true, do not write changes.
        backup: If true, create a .bak backup before writing.
        timeout: Download timeout in seconds.
        show_examples: Max duplicate examples to print.
        retries: Number of retries after first download attempt.
        retry_delay: Base seconds for exponential backoff.
        verbose: Whether to print download/retry progress.
        env_file: Path to .env file holding API token.
        ipinfo_token_key: Variable name in .env for IPInfo API token.
        cache_file: Path to store the cached upstream file.
        cache_ttl: Cache TTL in seconds.
        refresh_cache: If true, ignore existing cache and re-download.
        geo_cache_file: Path to store the IP geolocation cache.
        geo_cache_ttl: Geo cache TTL in seconds.
        refresh_geo_cache: If true, ignore existing geo cache entries.
        dns_server: IP address of the DNS server to use for resolution.

    Returns:
        Process exit code (0 success, non-zero failure).
    """
    if not local_path.exists():
        print(f"ERROR: Local file not found: {local_path}", file=sys.stderr)
        return 1

    env_values = load_env_file(env_file)
    token = env_values.get(ipinfo_token_key, "").strip()
    if not token:
        print(
            "ERROR: Missing IPInfo token. Add "
            f"{ipinfo_token_key}=<token> to {env_file}",
            file=sys.stderr,
        )
        return 1

    env_ttl_str = env_values.get("UPSTREAM_CACHE_TTL", "").strip()
    effective_ttl = cache_ttl
    if env_ttl_str:
        try:
            effective_ttl = float(env_ttl_str)
        except ValueError:
            print(
                f"WARNING: Invalid UPSTREAM_CACHE_TTL in {env_file}; "
                f"using default {cache_ttl:.0f}s",
                file=sys.stderr,
            )

    env_geo_ttl_str = env_values.get("GEO_CACHE_TTL", "").strip()
    effective_geo_ttl = geo_cache_ttl
    if env_geo_ttl_str:
        try:
            effective_geo_ttl = float(env_geo_ttl_str)
        except ValueError:
            print(
                f"WARNING: Invalid GEO_CACHE_TTL in {env_file}; "
                f"using default {geo_cache_ttl:.0f}s",
                file=sys.stderr,
            )

    geo_cache: dict[str, dict] = (
        {} if refresh_geo_cache else load_geo_cache(geo_cache_file)
    )
    if verbose:
        print(f"Geo cache loaded: {len(geo_cache)} entries from {geo_cache_file}")

    local_text = local_path.read_text(encoding="utf-8")
    local_lines = local_text.splitlines()

    try:
        upstream_lines = fetch_upstream_lines_cached(
            upstream_url,
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
            verbose=verbose,
            cache_file=cache_file,
            cache_ttl=effective_ttl,
            refresh_cache=refresh_cache,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    upstream_domains = extract_domains(upstream_lines)

    post_dedupe_lines: list[str] = []
    removed_duplicate_lines: list[str] = []
    removed_duplicate_domains: list[str] = []

    for line in local_lines:
        domain = extract_domain_from_line(line)
        if domain is None:
            post_dedupe_lines.append(line)
            continue

        if domain in upstream_domains:
            removed_duplicate_lines.append(line)
            removed_duplicate_domains.append(domain)
        else:
            post_dedupe_lines.append(line)

    candidate_domains = sorted(
        {
            domain
            for domain in (
                extract_domain_from_line(line_item) for line_item in post_dedupe_lines
            )
            if domain
        }
    )

    # Phase 1: serve domains from the domain-level cache; collect the rest.
    now = time.time()
    geo_results: dict[str, DomainGeoResult] = {}
    domains_to_resolve: list[str] = []
    domain_cache_hits = 0

    for domain in candidate_domains:
        entry = geo_cache.get(f"domain:{domain}")
        if entry and (now - entry.get("cached_at", 0)) < effective_geo_ttl:
            domain_cache_hits += 1
            geo_results[domain] = DomainGeoResult(
                domain=domain,
                ips=set(
                    entry.get("cn_ips", [])
                    + entry.get("non_cn_ips", [])
                    + entry.get("unknown_ips", [])
                ),
                cn_ips=set(entry.get("cn_ips", [])),
                non_cn_ips=set(entry.get("non_cn_ips", [])),
                unknown_country_ips=set(entry.get("unknown_ips", [])),
                unresolved=entry.get("unresolved", False),
            )
        else:
            domains_to_resolve.append(domain)

    if domain_cache_hits:
        print(
            f"{domain_cache_hits}/{len(candidate_domains)} domain(s) "
            "served from geo cache."
        )

    # Phase 2: resolve IPs for domains not covered by the domain cache.
    domain_ips: dict[str, set[str]] = {}
    if domains_to_resolve:
        print(
            f"Resolving IPs for {len(domains_to_resolve)} domain(s) via {dns_server}..."
        )
        for idx, domain in enumerate(domains_to_resolve, start=1):
            if verbose:
                print(f"  Resolving {idx}/{len(domains_to_resolve)}: {domain}")
            domain_ips[domain] = resolve_domain_ips(domain, dns_server)

    # Phase 3: batch-query IPInfo for any IPs not already in the IP cache.
    all_unique_ips = sorted({ip for ips in domain_ips.values() for ip in ips})
    ip_cache_miss_ips = [
        ip for ip in all_unique_ips
        if not (
            geo_cache.get(ip)
            and (now - geo_cache[ip].get("cached_at", 0)) < effective_geo_ttl
        )
    ]
    ip_cache_hits = len(all_unique_ips) - len(ip_cache_miss_ips)
    ip_cache_misses = len(ip_cache_miss_ips)

    if ip_cache_miss_ips:
        print(
            f"Querying IPInfo for {len(ip_cache_miss_ips)} IP(s) "
            f"({ip_cache_hits} served from IP cache)..."
        )
        fresh = batch_lookup_countries(
            ip_cache_miss_ips,
            token=token,
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
            verbose=verbose,
        )
        now = time.time()
        for ip, country in fresh.items():
            geo_cache[ip] = {"country": country, "cached_at": now}
    elif all_unique_ips:
        print(f"All {len(all_unique_ips)} IP(s) served from IP cache.")

    # Phase 4: build DomainGeoResult for resolved domains and write domain cache.
    now = time.time()
    for domain in domains_to_resolve:
        ips = domain_ips[domain]
        if not ips:
            result = DomainGeoResult(
                domain=domain,
                ips=set(),
                cn_ips=set(),
                non_cn_ips=set(),
                unknown_country_ips=set(),
                unresolved=True,
            )
        else:
            cn_ips: set[str] = set()
            non_cn_ips: set[str] = set()
            unknown_country_ips: set[str] = set()
            for ip in ips:
                entry = geo_cache.get(ip)
                country = entry.get("country") if entry else None
                if country == "CN":
                    cn_ips.add(ip)
                elif country is not None:
                    non_cn_ips.add(ip)
                else:
                    unknown_country_ips.add(ip)
            result = DomainGeoResult(
                domain=domain,
                ips=ips,
                cn_ips=cn_ips,
                non_cn_ips=non_cn_ips,
                unknown_country_ips=unknown_country_ips,
                unresolved=False,
            )
        geo_results[domain] = result
        geo_cache[f"domain:{domain}"] = {
            "cn_ips": sorted(result.cn_ips),
            "non_cn_ips": sorted(result.non_cn_ips),
            "unknown_ips": sorted(result.unknown_country_ips),
            "unresolved": result.unresolved,
            "cached_at": now,
        }

    save_geo_cache(geo_cache, geo_cache_file)
    if verbose:
        print(f"Geo cache saved to {geo_cache_file}")

    removed_non_cn_lines: list[str] = []
    removed_non_cn_domains: list[str] = []
    final_lines: list[str] = []
    unresolved_domains: list[str] = []

    for line in post_dedupe_lines:
        domain = extract_domain_from_line(line)
        if domain is None:
            final_lines.append(line)
            continue

        result = geo_results[domain]

        if result.unresolved:
            unresolved_domains.append(domain)
            removed_non_cn_lines.append(line)
            removed_non_cn_domains.append(domain)
            continue

        if result.cn_ips:
            final_lines.append(line)
            continue

        removed_non_cn_lines.append(line)
        removed_non_cn_domains.append(domain)

    unique_duplicate_domains = sorted(set(removed_duplicate_domains))
    unique_non_cn_domains = sorted(set(removed_non_cn_domains))
    unique_unresolved_domains = sorted(set(unresolved_domains))

    print("=== dnsmasq cleanup report ===")
    print(f"Upstream URL:      {upstream_url}")
    print(f"Upstream cache:    {cache_file}  (TTL {effective_ttl:.0f}s)")
    print(f"Geo cache:         {geo_cache_file}  (TTL {effective_geo_ttl:.0f}s)")
    print(f"DNS server:        {dns_server}")
    print(f"Local file:        {local_path}")
    print(f"Env file:          {env_file}")
    print(f"Upstream lines read: {len(upstream_lines)}")
    print(f"Upstream domains extracted: {len(upstream_domains)}")
    print(f"Local lines read: {len(local_lines)}")
    print(f"Duplicate local entries removed: {len(removed_duplicate_lines)}")
    print(f"Unique duplicate domains removed: {len(unique_duplicate_domains)}")
    print(f"Post-dedupe domains geo-evaluated: {len(candidate_domains)}")
    print(f"Geo domain cache hits: {domain_cache_hits} / {len(candidate_domains)}")
    print(f"Geo IP cache hits / misses: {ip_cache_hits} / {ip_cache_misses}")
    print(f"Non-CN / unresolved entries removed: {len(removed_non_cn_lines)}")
    print(f"Unique non-CN domains removed: {len(unique_non_cn_domains)}")
    print(f"Unresolved domains removed: {len(unique_unresolved_domains)}")
    print(f"Remaining local lines: {len(final_lines)}")

    if unique_duplicate_domains:
        shown = unique_duplicate_domains[:show_examples]
        print("")
        print(f"Sample duplicate domains removed (up to {show_examples}):")
        for domain in shown:
            print(f"  - {domain}")

    if unique_non_cn_domains:
        shown = unique_non_cn_domains[:show_examples]
        print("")
        print(f"Sample non-CN domains removed (up to {show_examples}):")
        for domain in shown:
            result = geo_results.get(domain)
            countries = set()
            if result is not None:
                if result.non_cn_ips:
                    countries.add("non-CN")
                if result.unknown_country_ips:
                    countries.add("unknown")
            country_summary = ", ".join(sorted(countries)) if countries else "non-CN"
            print(f"  - {domain} ({country_summary})")

    if unique_unresolved_domains:
        shown = unique_unresolved_domains[:show_examples]
        print("")
        print(f"Sample unresolved domains removed (up to {show_examples}):")
        for domain in shown:
            print(f"  - {domain}")

    if dry_run:
        print("")
        print("Dry-run mode enabled: no changes written.")
        return 0

    if backup:
        backup_path = local_path.with_suffix(local_path.suffix + ".bak")
        backup_path.write_text(local_text, encoding="utf-8")
        print(f"Backup written: {backup_path}")

    new_text = "\n".join(final_lines)
    if local_text.endswith("\n"):
        new_text += "\n"

    local_path.write_text(new_text, encoding="utf-8")
    print("File updated successfully.")
    return 0


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local-file",
        type=Path,
        default=Path("accelerated-domains.china.conf"),
        help="Path to local accelerated domains conf file.",
    )
    parser.add_argument(
        "--upstream-url",
        default=UPSTREAM_URL,
        help="Upstream URL to fetch latest accelerated domains list.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to .env file containing IPInfo token.",
    )
    parser.add_argument(
        "--ipinfo-token-key",
        default="IPINFO_TOKEN",
        help="Key name in .env for IPInfo API token.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute/report changes but do not modify local file.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create .bak backup before writing changes.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds for upstream/IPInfo requests.",
    )
    parser.add_argument(
        "--show-examples",
        type=int,
        default=25,
        help="How many domain examples to show per section.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=4,
        help="Retries after initial HTTP attempt (default: 4).",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=1.5,
        help="Base delay seconds for exponential backoff (default: 1.5).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print download/retry/geo progress details.",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=DEFAULT_CACHE_FILE,
        help=f"Path to store the cached upstream file (default: {DEFAULT_CACHE_FILE}).",
    )
    parser.add_argument(
        "--cache-ttl",
        type=float,
        default=DEFAULT_CACHE_TTL,
        help=(
            "Cache TTL in seconds; overridden by UPSTREAM_CACHE_TTL in .env "
            f"(default: {DEFAULT_CACHE_TTL})."
        ),
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Ignore existing cache and force a fresh upstream download.",
    )
    parser.add_argument(
        "--geo-cache-file",
        type=Path,
        default=DEFAULT_GEO_CACHE_FILE,
        help=(
            "Path to store the IP geolocation cache "
            f"(default: {DEFAULT_GEO_CACHE_FILE})."
        ),
    )
    parser.add_argument(
        "--geo-cache-ttl",
        type=float,
        default=DEFAULT_GEO_CACHE_TTL,
        help=(
            "Geo cache TTL in seconds; overridden by GEO_CACHE_TTL in .env "
            f"(default: {DEFAULT_GEO_CACHE_TTL})."
        ),
    )
    parser.add_argument(
        "--refresh-geo-cache",
        action="store_true",
        help="Ignore existing geo cache and re-query IPInfo for all IPs.",
    )
    parser.add_argument(
        "--dns-server",
        default=DEFAULT_DNS_SERVER,
        help=(
            "DNS server IP to use for domain resolution "
            f"(default: {DEFAULT_DNS_SERVER})."
        ),
    )
    args = parser.parse_args()

    return dedupe_local_file(
        local_path=args.local_file,
        upstream_url=args.upstream_url,
        dry_run=args.dry_run,
        backup=not args.no_backup,
        timeout=max(args.timeout, 0.1),
        show_examples=max(args.show_examples, 0),
        retries=max(args.retries, 0),
        retry_delay=max(args.retry_delay, 0.0),
        verbose=args.verbose,
        env_file=args.env_file,
        ipinfo_token_key=args.ipinfo_token_key,
        cache_file=args.cache_file,
        cache_ttl=max(args.cache_ttl, 0.0),
        refresh_cache=args.refresh_cache,
        geo_cache_file=args.geo_cache_file,
        geo_cache_ttl=max(args.geo_cache_ttl, 0.0),
        refresh_geo_cache=args.refresh_geo_cache,
        dns_server=args.dns_server,
    )


if __name__ == "__main__":
    sys.exit(main())
