"""Fetch, verify, and optionally start the pinned Northwind evaluation database."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "backend" / "test-data" / "northwind" / "northwind.sql"
COMPOSE_PATH = ROOT / "docker-compose.eval.yml"
COMMIT = "cd0ef28d66369fbe177778e604e4be0f153c9e5c"
RAW_URL = f"https://raw.githubusercontent.com/pthom/northwind_psql/{COMMIT}/northwind.sql"
EXPECTED_SHA256 = "0ee30c01ba282f7194f38bf7f99cd6be0470b7ee5f67d0f7ca41fb058d735e0c"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_dataset(download: bool) -> None:
    if DATASET_PATH.is_file() and sha256(DATASET_PATH) == EXPECTED_SHA256:
        print(f"dataset=verified path={DATASET_PATH}")
        return
    if not download:
        raise SystemExit(
            "Pinned dataset is missing or has the wrong hash. "
            "Run again with --download to fetch the exact committed source."
        )
    DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATASET_PATH.with_suffix(".sql.download")
    try:
        with urllib.request.urlopen(RAW_URL, timeout=60) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        actual = sha256(temporary)
        if actual != EXPECTED_SHA256:
            raise SystemExit(f"Downloaded dataset hash mismatch: expected {EXPECTED_SHA256}, got {actual}")
        temporary.replace(DATASET_PATH)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"dataset=downloaded_and_verified path={DATASET_PATH}")


def compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_PATH), *args],
        cwd=ROOT,
        check=True,
    )


def verify_database() -> None:
    query = (
        "SELECT (SELECT COUNT(*) FROM customers),"
        "(SELECT COUNT(*) FROM orders),"
        "(SELECT COUNT(*) FROM order_details),"
        "current_setting('default_transaction_read_only');"
    )
    completed = subprocess.run(
        [
            "docker", "compose", "-f", str(COMPOSE_PATH), "exec", "-T",
            "northwind-eval-db", "psql", "-U", "northwind_reader", "-d", "northwind",
            "-At", "-F", "|", "-c", query,
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    actual = completed.stdout.strip()
    expected = "91|830|2155|on"
    if actual != expected:
        raise SystemExit(f"Database verification failed: expected {expected}, got {actual!r}")
    print(f"database=verified signature={actual}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Download the pinned SQL fixture when absent.")
    parser.add_argument("--start", action="store_true", help="Start the isolated PostgreSQL benchmark service.")
    parser.add_argument("--verify-db", action="store_true", help="Verify row counts and the read-only role.")
    args = parser.parse_args()

    ensure_dataset(args.download)
    if args.start:
        compose("up", "-d", "--wait")
    if args.verify_db or args.start:
        verify_database()
    print(f"dataset_sha256={EXPECTED_SHA256}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"bootstrap_failed={exc}", file=sys.stderr)
        raise SystemExit(1) from exc
