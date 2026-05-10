"""
event_producer.py
-----------------
Generates and streams synthetic e-commerce events to the Kinesis Data Stream
defined in config.py.

Event schema
------------
{
    "event_id":          str   — UUID v4
    "user_id":           str   — "user_<hex8>"
    "session_id":        str   — UUID v4
    "event_type":        str   — "purchase" | "view" | "cart_add" | "cart_remove"
    "product_id":        str   — "prod_<hex6>"
    "product_category":  str   — one of CATEGORIES
    "product_name":      str   — realistic product label
    "amount":            float — 0.00 for non-purchase events, price for purchases
    "currency":          str   — "GBP" | "EUR" | "USD"
    "quantity":          int   — 1–5
    "discount_pct":      float — 0.0–0.30 (30 % max)
    "timestamp":         str   — ISO 8601 UTC
    "client":            obj
        "platform":      str   — "web" | "ios" | "android"
        "country_code":  str   — ISO 3166-1 alpha-2
        "ip_hash":       str   — sha256 hex (simulated)
}

Usage
-----
    python producer/event_producer.py                 # 100 events, default config
    python producer/event_producer.py --count 500     # send 500 events
    python producer/event_producer.py --delay 0.05    # 50 ms between events
    python producer/event_producer.py --dry-run       # print events, no Kinesis
"""

import argparse
import hashlib
import json
import logging
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# Allow running directly from the project root as well as from inside producer/
sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Synthetic data tables
# ---------------------------------------------------------------------------

EVENT_TYPES = ["purchase", "view", "view", "view", "cart_add", "cart_add", "cart_remove"]
# 'view' appears 3x so the distribution is realistic (most traffic = views)

CATEGORIES = {
    "electronics": [
        ("Wireless Earbuds Pro", 79.99),
        ("4K Smart TV 55\"", 549.99),
        ("USB-C Hub 7-in-1", 34.99),
        ("Mechanical Keyboard", 119.99),
        ("Noise-Cancelling Headphones", 229.99),
    ],
    "clothing": [
        ("Slim Fit Chinos", 49.99),
        ("Merino Wool Jumper", 89.99),
        ("Running Jacket Lightweight", 64.99),
        ("Leather Ankle Boots", 134.99),
        ("Organic Cotton T-Shirt", 24.99),
    ],
    "home_kitchen": [
        ("Stainless Steel Blender", 59.99),
        ("Cast Iron Skillet 26cm", 44.99),
        ("Bamboo Cutting Board Set", 29.99),
        ("Espresso Machine Compact", 149.99),
        ("Air Purifier HEPA", 179.99),
    ],
    "books": [
        ("Data Engineering Fundamentals", 39.99),
        ("Designing Data-Intensive Applications", 44.99),
        ("Clean Code", 34.99),
        ("The Pragmatic Programmer", 39.99),
        ("Python for Data Analysis", 49.99),
    ],
    "sports_outdoors": [
        ("Yoga Mat Premium", 29.99),
        ("Adjustable Dumbbell 20kg", 89.99),
        ("Cycling Helmet Aero", 74.99),
        ("Trail Running Shoes", 119.99),
        ("Hydration Backpack 10L", 54.99),
    ],
}

CURRENCIES = ["GBP", "EUR", "USD"]
PLATFORMS = ["web", "ios", "android"]
COUNTRY_CODES = ["GB", "DE", "FR", "US", "NL", "SE", "PL", "ES", "IT", "CA"]

# Pre-generate a pool of recurring user IDs so some users appear multiple times
USER_POOL = [f"user_{uuid.uuid4().hex[:8]}" for _ in range(40)]


# ---------------------------------------------------------------------------
# Event generation
# ---------------------------------------------------------------------------

def _fake_ip_hash() -> str:
    """Return a deterministic-looking SHA-256 hex string for a random IP."""
    fake_ip = f"{random.randint(1,254)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"
    return hashlib.sha256(fake_ip.encode()).hexdigest()


def generate_event() -> dict:
    """Build and return one synthetic e-commerce event."""
    event_type = random.choice(EVENT_TYPES)
    category = random.choice(list(CATEGORIES.keys()))
    product_name, base_price = random.choice(CATEGORIES[category])
    product_id = f"prod_{abs(hash(product_name)) % 0xFFFFFF:06x}"
    currency = random.choice(CURRENCIES)
    quantity = random.randint(1, 5)
    discount_pct = round(random.uniform(0.0, 0.30), 2)

    # Only purchase events have a meaningful amount
    if event_type == "purchase":
        amount = round(base_price * quantity * (1 - discount_pct), 2)
    else:
        amount = 0.0

    return {
        "event_id": str(uuid.uuid4()),
        "user_id": random.choice(USER_POOL),
        "session_id": str(uuid.uuid4()),
        "event_type": event_type,
        "product_id": product_id,
        "product_category": category,
        "product_name": product_name,
        "amount": amount,
        "currency": currency,
        "quantity": quantity,
        "discount_pct": discount_pct,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "client": {
            "platform": random.choice(PLATFORMS),
            "country_code": random.choice(COUNTRY_CODES),
            "ip_hash": _fake_ip_hash(),
        },
    }


# ---------------------------------------------------------------------------
# Kinesis put helper  (with simple exponential back-off for throttling)
# ---------------------------------------------------------------------------

def put_record(kinesis_client, stream_name: str, event: dict, max_retries: int = 5) -> dict:
    """
    Send *event* to *stream_name*, retrying with exponential back-off on
    ProvisionedThroughputExceededException.
    """
    payload = json.dumps(event, ensure_ascii=False)
    partition_key = event["user_id"]  # user_id distributes load across shards
    attempt = 0
    delay = 0.1

    while attempt < max_retries:
        try:
            resp = kinesis_client.put_record(
                StreamName=stream_name,
                Data=payload.encode("utf-8"),
                PartitionKey=partition_key,
            )
            return resp
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ProvisionedThroughputExceededException":
                log.warning(
                    "Throughput exceeded (attempt %d/%d) — retrying in %.1fs …",
                    attempt + 1,
                    max_retries,
                    delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, 5.0)
                attempt += 1
            else:
                raise
    raise RuntimeError(f"Failed to put record after {max_retries} retries.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Stream synthetic e-commerce events to Kinesis."
    )
    parser.add_argument(
        "--count",
        type=int,
        default=100,
        help="Number of events to send (default: 100).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="Sleep time in seconds between records (default: 0.1).",
    )
    parser.add_argument(
        "--stream",
        default=config.KINESIS_STREAM_NAME,
        help=f"Kinesis stream name (default: {config.KINESIS_STREAM_NAME!r}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print generated events to stdout without sending to Kinesis.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.dry_run:
        log.info("DRY RUN — events will be printed, not sent to Kinesis.")
        for i in range(1, args.count + 1):
            event = generate_event()
            print(json.dumps(event, indent=2))
            if i < args.count:
                time.sleep(args.delay)
        log.info("Dry run complete — %d event(s) generated.", args.count)
        return

    session = boto3.Session(
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
        region_name=config.AWS_REGION,
    )
    kinesis_client = session.client("kinesis")

    log.info(
        "Starting producer — stream: '%s', events: %d, delay: %.2fs",
        args.stream,
        args.count,
        args.delay,
    )

    stats = {"purchase": 0, "view": 0, "cart_add": 0, "cart_remove": 0}
    total_revenue = 0.0

    for i in range(1, args.count + 1):
        event = generate_event()
        resp = put_record(kinesis_client, args.stream, event)
        stats[event["event_type"]] = stats.get(event["event_type"], 0) + 1
        if event["event_type"] == "purchase":
            total_revenue += event["amount"]

        if i % 10 == 0 or i == args.count:
            log.info(
                "[%3d/%d] Sent event_id=%s  type=%-11s  shard=%s",
                i,
                args.count,
                event["event_id"],
                event["event_type"],
                resp["ShardId"],
            )

        if i < args.count:
            time.sleep(args.delay)

    log.info("Producer finished.")
    log.info("Event breakdown: %s", stats)
    log.info(
        "Total simulated revenue: %.2f (purchases only)",
        total_revenue,
    )


if __name__ == "__main__":
    main()
