#!/usr/bin/env python3
"""
Standalone cash-ledger replay for a single broker.

Reproduces ONLY the cash-wallet running balance that
foreign_sched_values_calc.py computes for one broker, so you can reconcile
against your real statement without the full run (no network, no ticker/FX
fetch). The closing balance printed for the default cutoff is exactly what
gets prefilled as next year's `cash_opening` amount.

Cash rules mirrored from the main script:
  cash_opening                         : opening balance
  stock_dividend / cash_dividend / interest : credit  amount - tax_withheld - misc_fees
  sell_fifo / sell_specific            : credit  units*price - tax_withheld - misc_fees
  buy                                  : debit   units*price   (fees NOT drawn from cash)
  cash_to_bank                         : debit   amount + misc_fees
  bank_to_cash                         : credit  amount - misc_fees
  vest / gift_specific / receive_gift / cash_fund_switch : no cash effect

Tax withheld uses tax_withholding.amount from the YAML as-is (the main script
can auto-derive it from the gain when rate>0 and amount==0; this helper does
not, so double-check any sell where you left amount at 0 but rate>0).

Usage:
  python3 replay_cash_ledger.py
  python3 replay_cash_ledger.py --broker ibkr
  python3 replay_cash_ledger.py --cutoff 2025-09-22
"""
import argparse
from datetime import date as Date
from fractions import Fraction
from pathlib import Path

import yaml

# Precision: parse all numbers as Fraction, like the main script.
yaml.add_constructor("tag:yaml.org,2002:float",
                     lambda l, n: Fraction(l.construct_scalar(n)),
                     Loader=yaml.SafeLoader)
yaml.add_constructor("tag:yaml.org,2002:int",
                     lambda l, n: Fraction(l.construct_scalar(n)),
                     Loader=yaml.SafeLoader)

ZERO = Fraction(0)

ACTIVITY_SECTIONS = (
    "activity_from_jan_1_prev_fy_to_31_mar_prev_fy",
    "activity_from_apr_1_current_fy_to_31_mar_current_fy",
)


def money(f: Fraction) -> str:
    return f"{float(f):>12,.2f}"


def cash_delta(atype: str, d: dict):
    """Return (delta, note) or (None, reason) when the txn doesn't touch cash."""
    tw = d.get("tax_withholding") or {}
    tax = tw.get("amount", ZERO) or ZERO
    fees = d.get("misc_fees", ZERO) or ZERO

    match atype:
        case "cash_opening":
            return d["amount"], "opening balance"
        case "stock_dividend" | "cash_dividend" | "interest":
            return (d["amount"] - tax - fees,
                    f"gross {float(d['amount']):.2f} - tax {float(tax):.2f}"
                    f" - fees {float(fees):.2f}")
        case "sell_fifo" | "sell_specific":
            price = d["stock_price_in_broker_doc"]
            gross = d["units"] * price
            return (gross - tax - fees,
                    f"{float(d['units']):g} x {float(price):.2f} = "
                    f"{float(gross):.2f} - tax {float(tax):.2f}"
                    f" - fees {float(fees):.2f}")
        case "buy":
            price = d["stock_price_in_broker_doc"]
            gross = d["units"] * price
            return -gross, f"{float(d['units']):g} x {float(price):.2f}"
        case "cash_to_bank":
            return (-(d["amount"] + fees),
                    f"wire out {float(d['amount']):.2f} + fees {float(fees):.2f}")
        case "bank_to_cash":
            return d["amount"] - fees, f"wire in - fees {float(fees):.2f}"
        case "vest" | "gift_specific" | "receive_gift" | "cash_fund_switch":
            return None, "no cash effect"
        case _:
            return None, f"UNHANDLED type '{atype}'"


def main() -> None:
    default_input = Path(__file__).parent / "data" / "input.yaml"

    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(default_input))
    ap.add_argument("--broker", required=True)
    ap.add_argument("--cutoff", default=None,
                    help="YYYY-MM-DD; default = Dec 31 of the CY containing "
                         "the FY start (i.e. next year's opening date - 1).")
    ap.add_argument("--all", action="store_true",
                    help="Also list transactions with no cash effect.")
    args = ap.parse_args()

    with open(args.input) as f:
        doc = yaml.safe_load(f)

    fy_start = doc["metadata"]["current_financial_year_start"]
    cutoff = (Date.fromisoformat(args.cutoff) if args.cutoff
              else Date(fy_start.year, 12, 31))

    # (sort_date, activity_id, atype, dict) collected across all sections.
    entries = []

    # Opening ledger: only cash_opening touches cash. Timestamp it at the very
    # start (the main script books it at prev-CY-end).
    for act in doc.get("opening_ledger_on_12_AM_jan_1_prev_fy", []) or []:
        aid, d = next(iter(act.items()))
        if d.get("broker") == args.broker and d["activity_type"] == "cash_opening":
            entries.append((Date(fy_start.year, 1, 1), aid, "cash_opening", d))

    # Both activity sections, processed jan-mar before apr-mar (as the main
    # script does), so same-date file order is preserved by the stable sort.
    for section in ACTIVITY_SECTIONS:
        for act in doc.get(section, []) or []:
            if not isinstance(act, dict):
                continue  # skip "TODO / FILL THIS SECTION" placeholders
            aid, d = next(iter(act.items()))
            if d.get("broker") != args.broker:
                continue
            entries.append((d.get("date"), aid, d["activity_type"], d))

    # Stable sort by date keeps file order within a date.
    entries.sort(key=lambda e: e[0])

    print(f"\nCash ledger for broker '{args.broker}'  (cutoff <= {cutoff})\n")
    header = f"{'DATE':<12} {'DELTA':>12} {'BALANCE':>14}  ACTIVITY"
    print(header)
    print("-" * len(header))

    balance = ZERO
    credits = ZERO
    debits = ZERO

    for date, aid, atype, d in entries:
        if date > cutoff:
            continue

        delta, note = cash_delta(atype, d)

        if delta is None:
            if args.all:
                print(f"{str(date):<12} {'—':>12} {'—':>14}  "
                      f"{aid} [{atype}: {note}]")
            continue

        balance += delta
        if delta >= 0:
            credits += delta
        else:
            debits += -delta

        print(f"{str(date):<12} {money(delta)} {money(balance)}  "
              f"{aid} [{atype}: {note}]")

    print("-" * len(header))
    print(f"{'CREDITS':<12} {money(credits)}")
    print(f"{'DEBITS':<12} {money(-debits)}")
    print(f"{'CLOSING':<12} {money(balance)}   "
          f"(exact = {balance}  ->  {float(balance):.6f})")
    print(f"\nThis CLOSING is what would prefill next year's cash_opening "
          f"amount for '{args.broker}'.\n")


if __name__ == "__main__":
    main()
