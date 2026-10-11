"""
generate_license.py — run this yourself to create a passkey for a customer.

This is an ADMIN tool. Keep it on your own machine only -- never send this
file to a customer (it imports license_manager.py, which holds the secret
that makes passkeys genuine).

USAGE
  python generate_license.py --tier standard --days 365
  python generate_license.py --tier premium --expiry 2027-12-31
  python generate_license.py --tier basic --days 30 --customer "Nile Valley Academy"

--customer is optional and just gets echoed back to you in the output for
your own records -- it isn't embedded in the passkey itself.
"""

import argparse
from datetime import date, timedelta

from license_manager import generate_key


def main():
    parser = argparse.ArgumentParser(description="Generate a Testy Timetables passkey.")
    parser.add_argument("--tier", required=True, choices=["basic", "standard", "premium"])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--days", type=int, help="Valid for this many days from today.")
    group.add_argument("--expiry", type=str, help="Exact expiry date, YYYY-MM-DD.")
    parser.add_argument("--customer", type=str, default=None, help="Optional, for your own records.")
    args = parser.parse_args()

    expiry_date = date.today() + timedelta(days=args.days) if args.days else args.expiry

    key = generate_key(args.tier, expiry_date)

    print()
    if args.customer:
        print(f"Customer: {args.customer}")
    print(f"Package:  {args.tier}")
    print(f"Expires:  {expiry_date if isinstance(expiry_date, str) else expiry_date.isoformat()}")
    print(f"Passkey:  {key}")
    print()


if __name__ == "__main__":
    main()
