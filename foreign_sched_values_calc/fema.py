#!/usr/bin/env python3
"""
FEMA 180-day compliance risk checker.

Given the same input.yaml used by foreign_sched_values_calc.py, this walks every
broker/account's cash ledger and flags any *realized foreign exchange* -- idle
dividends, interest, and sale proceeds sitting in an overseas account -- that is
approaching or past its 180-day deadline to be reinvested, put to a permitted
use, or repatriated to India.

Legal basis
-----------
Regulation 7 of the Foreign Exchange Management (Realisation, Repatriation and
Surrender of Foreign Exchange) Regulations, 2015:

    Received / realised / unspent / unused foreign exchange, unless reinvested,
    shall be repatriated and surrendered to an authorised person within a period
    of 180 days from the date of such receipt / realisation / purchase /
    acquisition or date of return to India.

References (read these, don't take this tool's word for it):
  - https://paasa.com/blog/fema-180-day-rule
  - https://rbi.org.in/Scripts/BS_FemaNotifications.aspx
  - Section 13, FEMA 1999 (penalties: up to 3x the amount involved, or up to
    Rs. 2 lakh where not quantifiable, plus Rs. 5,000/day for a continuing
    contravention).

DISCLAIMER: This is a best-effort heuristic to surface risk, NOT legal or tax
advice. It does not know about permitted-use spends you never recorded, does not
adjudicate what qualifies as "reinvestment", and cannot see money in accounts
absent from your YAML. Verify every alert against your actual statements and,
when in doubt, consult a professional. NRIs are exempt from this rule; RNOR/ROR
residents are covered -- this tool assumes you are a resident it applies to.

Model
-----
The clock starts the day realized foreign currency lands as idle cash:
  * cash_dividend / stock_dividend   -> "dividend"      (net of tax + fees)
  * interest                         -> "interest"      (net of tax + fees)
  * sell_fifo / sell_specific        -> "sale proceeds" (net of tax + fees)
  * bank_to_cash                     -> "LRS inflow"    (idle LRS funds abroad;
                                        toggle off with --exclude-lrs)
  * cash_opening                     -> "opening balance" (origin/age unknown ->
                                        reported as an ADVISORY, never a hard
                                        breach, because we can't date it)

The clock stops when idle cash is consumed by:
  * buy                    -> reinvestment
  * cash_to_bank           -> repatriation to India (no transfer_id)
  * permitted_use_abroad   -> optional activity you may add for travel/education/
                              medical spends abroad (not in the base schema)

Cash is fungible, so uses are matched FIFO (First-In-First-Out) against the
oldest idle money -- the interpretation a taxpayer would actually claim.
Merely moving cash to another fund/broker does NOT stop the clock, so
cash_fund_switch (and vest / gift / receive_gift) are cash-neutral here,
mirroring replay_cash_ledger.py.

Inter-broker transfers (e.g. Schwab -> IBKR)
---------------------------------------------
Moving sale proceeds between two *foreign* brokers is NOT repatriation, so
the 180-day clock must not reset.  Record the transfer as a matched pair:

  cash_to_bank entry (source broker):
    transfer_id: schwab_to_ibkr_2026_01

  bank_to_cash entry (destination broker):
    transfer_id: schwab_to_ibkr_2026_01

When transfer_id is present the tool carries the exact tranche portions
(original receipt date, nature, remaining balance) from the source to the
destination queue.  The destination clock therefore continues from the
original receipt date, not from the transfer date.  Any bank_to_cash with a
transfer_id is silently ignored as the "inbound leg" -- the tranche was
already accounted for on the source side.

Usage
-----
  python3 fema.py
  python3 fema.py --as-of 2026-07-08 --warn-days 45
  python3 fema.py --broker schwab_equity_awards --all
  python3 fema.py --json > fema_report.json

Exit code: 0 = clean, 1 = warnings/advisories only, 2 = at least one breach.

Copyright (C) 2026  Abhinav Anand and contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
import argparse
import json
import sys
from datetime import date as Date, timedelta
from fractions import Fraction
from pathlib import Path

import yaml

# Precision: parse all numbers as Fraction, exactly like the main script and
# replay_cash_ledger.py, so cash math reconciles to the paisa.
yaml.add_constructor("tag:yaml.org,2002:float",
                     lambda l, n: Fraction(l.construct_scalar(n)),
                     Loader=yaml.SafeLoader)
yaml.add_constructor("tag:yaml.org,2002:int",
                     lambda l, n: Fraction(l.construct_scalar(n)),
                     Loader=yaml.SafeLoader)

ZERO = Fraction(0)

FEMA_WINDOW_DAYS = 180

ACTIVITY_SECTIONS = (
    "activity_from_jan_1_prev_fy_to_31_mar_prev_fy",
    "activity_from_apr_1_current_fy_to_31_mar_current_fy",
)

# Realizations that start a 180-day clock -> display label for "Nature".
CLOCK_STARTING_INCOME = {
    "cash_dividend": "dividend",
    "stock_dividend": "dividend",
    "interest": "interest",
    "sell_fifo": "sale proceeds",
    "sell_specific": "sale proceeds",
}

# Activities that consume idle cash and thereby stop the clock.
# cash_to_bank is handled explicitly below (transfer_id changes its semantics).
USE_DEBITS = {"buy", "permitted_use_abroad"}

# Activities with no cash effect (moving between funds/brokers does NOT stop the
# clock; vests/gifts move no cash, whether shares are gifted out or received).
NEUTRAL = {"vest", "gift", "gift_specific", "receive_gift", "cash_fund_switch"}


class Tranche:
    """One idle-cash inflow, FIFO-consumed by later uses."""

    __slots__ = ("date", "nature", "clock_bearing", "origin_known",
                 "original", "remaining", "activity_id")

    def __init__(self, date, nature, amount, activity_id,
                 clock_bearing=True, origin_known=True):
        self.date = date
        self.nature = nature
        self.activity_id = activity_id
        self.clock_bearing = clock_bearing
        self.origin_known = origin_known
        self.original = amount
        self.remaining = amount


def _tax_and_fees(d):
    tw = d.get("tax_withholding") or {}
    tax = tw.get("amount", ZERO) or ZERO
    fees = d.get("misc_fees", ZERO) or ZERO
    return tax, fees


def credit_amount(atype, d):
    """Net idle cash added by a credit activity (mirrors replay_cash_ledger)."""
    tax, fees = _tax_and_fees(d)
    if atype in ("cash_dividend", "stock_dividend", "interest"):
        return d["amount"] - tax - fees
    if atype in ("sell_fifo", "sell_specific"):
        gross = d["units"] * d["stock_price_in_broker_doc"]
        return gross - tax - fees
    if atype == "bank_to_cash":
        return d["amount"] - fees
    if atype == "cash_opening":
        return d["amount"]
    raise ValueError(f"Not a credit activity type: {atype}")


def debit_amount(atype, d):
    """Idle cash consumed by a use activity."""
    if atype == "buy":
        return d["units"] * d["stock_price_in_broker_doc"]
    if atype in ("cash_to_bank", "permitted_use_abroad"):
        _, fees = _tax_and_fees(d)
        return d["amount"] + fees
    raise ValueError(f"Not a debit activity type: {atype}")


def build_broker_currency_map(doc):
    countries = doc.get("countries", {}) or {}
    out = {}
    for broker_id, b in (doc.get("brokers", {}) or {}).items():
        country = countries.get(b.get("country"), {}) if b else {}
        out[broker_id] = country.get("currency", "")
    return out


def collect_events(doc):
    """Return (broker, date, activity_id, atype, d) across all ledger sections,
    ordered as the main script processes them: opening first, then jan-mar, then
    apr-mar; file order preserved within a date via a stable sort."""
    fy_start = doc["metadata"]["current_financial_year_start"]
    events = []

    # Opening ledger is booked at the very start of the window (Jan 1 prev FY).
    opening_date = Date(fy_start.year, 1, 1)
    for act in doc.get("opening_ledger_on_12_AM_jan_1_prev_fy", []) or []:
        if not isinstance(act, dict):
            continue
        aid, d = next(iter(act.items()))
        if d.get("activity_type") == "cash_opening":
            events.append((d.get("broker"), opening_date, aid,
                           "cash_opening", d, (0, 0)))

    # Section index keeps jan-mar strictly before apr-mar in the sort.
    for sect_idx, section in enumerate(ACTIVITY_SECTIONS, start=1):
        for order, act in enumerate(doc.get(section, []) or []):
            if not isinstance(act, dict):
                continue  # skip "TODO / FILL THIS" placeholders
            aid, d = next(iter(act.items()))
            events.append((d.get("broker"), d.get("date"), aid,
                           d["activity_type"], d, (sect_idx, order)))

    # cash_opening always sorts first on its date; otherwise (section, order).
    def sort_key(e):
        _, date, _, atype, _, tiebreak = e
        tb = (-1, 0) if atype == "cash_opening" else tiebreak
        return (date, tb)

    events.sort(key=sort_key)
    return events


def _consume_fifo(queue, need):
    """Debit `need` from the FIFO queue; return (shortfall, taken_tranches).

    taken_tranches is a list of new Tranche objects representing the portions
    actually consumed, preserving the original receipt dates and natures.
    """
    taken = []
    for tr in queue:
        if need <= 0:
            break
        take = min(tr.remaining, need)
        tr.remaining -= take
        need -= take
        if take > 0:
            taken.append(Tranche(tr.date, tr.nature, take, tr.activity_id,
                                 tr.clock_bearing, tr.origin_known))
    return need, taken


def analyze(doc, as_of, window_days, include_lrs):
    """Run the FIFO cash ledger per broker up to `as_of`.

    Returns (findings, overdrawn, trace):
      findings   -- outstanding clock-bearing tranches with remaining balances
      overdrawn  -- data-smell warnings
      trace      -- chronological list of credit/consume/transfer events, used
                    by --trace and --flag-late-use; empty list when not needed
                    (callers pass collect_trace=True to populate it)
    """
    return _analyze(doc, as_of, window_days, include_lrs, collect_trace=False)


def _analyze(doc, as_of, window_days, include_lrs, collect_trace):
    events = collect_events(doc)

    ledgers = {}             # broker_id -> FIFO list of open Tranches
    overdrawn = []           # uses that drew more cash than available
    pending_transfers = {}   # transfer_id -> Tranche portions in-flight
    trace = []               # chronological event log (populated if collect_trace)

    def _trace(event):
        if collect_trace:
            trace.append(event)

    for broker, date, aid, atype, d, _ in events:
        if date is None or date > as_of:
            continue
        queue = ledgers.setdefault(broker, [])

        if atype in CLOCK_STARTING_INCOME:
            amt = credit_amount(atype, d)
            if amt > 0:
                nature = CLOCK_STARTING_INCOME[atype]
                queue.append(Tranche(date, nature, amt, aid))
                _trace({"type": "credit", "broker": broker, "date": date,
                        "activity_id": aid, "activity_type": atype,
                        "nature": nature, "amount": amt})
        elif atype == "cash_opening":
            amt = credit_amount(atype, d)
            if amt > 0:
                queue.append(Tranche(date, "opening balance", amt, aid,
                                     clock_bearing=True, origin_known=False))
                _trace({"type": "credit", "broker": broker, "date": date,
                        "activity_id": aid, "activity_type": atype,
                        "nature": "opening balance", "amount": amt})
        elif atype == "bank_to_cash":
            transfer_id = d.get("transfer_id")
            if transfer_id:
                carried = pending_transfers.pop(transfer_id, None)
                if carried is None:
                    overdrawn.append((broker, date, aid, atype
                                      + f"[transfer_id={transfer_id}:"
                                        f"no matching cash_to_bank]", None))
                else:
                    queue.extend(carried)
                    _trace({"type": "transfer_in", "broker": broker,
                            "date": date, "activity_id": aid,
                            "transfer_id": transfer_id,
                            "portions": _portions(carried)})
            else:
                amt = credit_amount(atype, d)
                if amt > 0:
                    queue.append(Tranche(date, "LRS inflow", amt, aid,
                                         clock_bearing=include_lrs))
                    _trace({"type": "credit", "broker": broker, "date": date,
                            "activity_id": aid, "activity_type": atype,
                            "nature": "LRS inflow", "amount": amt})
        elif atype == "cash_to_bank":
            transfer_id = d.get("transfer_id")
            need = debit_amount(atype, d)
            shortfall, taken = _consume_fifo(queue, need)
            if shortfall > 0:
                overdrawn.append((broker, date, aid, atype, shortfall))
            if transfer_id:
                pending_transfers[transfer_id] = taken
                _trace({"type": "transfer_out", "broker": broker, "date": date,
                        "activity_id": aid, "transfer_id": transfer_id,
                        "portions": _portions(taken),
                        "shortfall": shortfall})
            else:
                _trace({"type": "consume", "broker": broker, "date": date,
                        "activity_id": aid, "activity_type": atype,
                        "portions": _portions_with_days(taken, date),
                        "shortfall": shortfall})
        elif atype in USE_DEBITS:
            need = debit_amount(atype, d)
            shortfall, taken = _consume_fifo(queue, need)
            if shortfall > 0:
                overdrawn.append((broker, date, aid, atype, shortfall))
            _trace({"type": "consume", "broker": broker, "date": date,
                    "activity_id": aid, "activity_type": atype,
                    "portions": _portions_with_days(taken, date),
                    "shortfall": shortfall})
        elif atype in NEUTRAL:
            pass
        else:
            overdrawn.append((broker, date, aid, f"UNKNOWN:{atype}", None))

    for transfer_id, tranches in pending_transfers.items():
        total = sum(t.remaining for t in tranches)
        overdrawn.append((None, None, f"transfer_id={transfer_id}",
                          "UNMATCHED_TRANSFER", total))

    findings = {}
    for broker, queue in ledgers.items():
        rows = []
        for tr in queue:
            if tr.remaining <= 0 or not tr.clock_bearing:
                continue
            deadline = tr.date + timedelta(days=window_days)
            rows.append({
                "activity_id": tr.activity_id,
                "nature": tr.nature,
                "receipt_date": tr.date,
                "deadline": deadline,
                "days_idle": (as_of - tr.date).days,
                "days_left": (deadline - as_of).days,
                "remaining": tr.remaining,
                "origin_known": tr.origin_known,
            })
        if rows:
            findings[broker] = rows
    return findings, overdrawn, trace


def _portions(taken):
    """Trace helper: snapshot of carried/consumed tranche portions."""
    return [{"source_activity_id": t.activity_id, "source_date": t.date,
             "source_nature": t.nature, "amount": t.remaining,
             "clock_bearing": t.clock_bearing}
            for t in taken]


def _portions_with_days(taken, consume_date):
    """Like _portions but also records how long the cash was held."""
    return [{"source_activity_id": t.activity_id, "source_date": t.date,
             "source_nature": t.nature, "amount": t.remaining,
             "clock_bearing": t.clock_bearing,
             "days_held": (consume_date - t.date).days}
            for t in taken]


def status_of(row, warn_days):
    if not row["origin_known"]:
        return "ADVISORY"
    if row["days_left"] < 0:
        return "BREACH"
    if row["days_left"] <= warn_days:
        return "WARN"
    return "OK"


SEV_RANK = {"OK": 0, "ADVISORY": 1, "WARN": 2, "BREACH": 3}


def money(f):
    return f"{float(f):>14,.2f}"


def print_report(findings, overdrawn, currencies, as_of, window_days,
                 warn_days, include_lrs, show_all):
    worst = 0
    n_breach = n_warn = n_adv = 0

    print()
    print("=" * 78)
    print(f"  FEMA {window_days}-day compliance check   as-of {as_of}"
          f"   (warn window: {warn_days}d)")
    print(f"  LRS/idle inflows counted as realized: "
          f"{'YES' if include_lrs else 'NO (--exclude-lrs)'}")
    print("=" * 78)

    if not findings:
        print("\n  No idle realized foreign exchange outstanding. Clean.\n")
        return 0

    for broker in sorted(findings):
        rows = findings[broker]
        ccy = currencies.get(broker, "") or ""
        # Most urgent first: least days_left; advisories (unknown age) last.
        rows.sort(key=lambda r: (r["origin_known"] is False, r["days_left"]))
        printed = []
        for r in rows:
            st = status_of(r, warn_days)
            if st == "BREACH":
                n_breach += 1
            elif st == "WARN":
                n_warn += 1
            elif st == "ADVISORY":
                n_adv += 1
            worst = max(worst, SEV_RANK[st])
            if st == "OK" and not show_all:
                continue
            printed.append((st, r))

        if not printed:
            continue

        print(f"\n  Broker/account: {broker}   (currency: {ccy or '?'})")
        print(f"  {'STATUS':<9} {'NATURE':<14} {'RECEIVED':<11} "
              f"{'DEADLINE':<11} {'LEFT':>6} {('IDLE ' + ccy):>15}  ACTIVITY")
        print("  " + "-" * 90)
        for st, r in printed:
            deadline = str(r["deadline"]) if r["origin_known"] else "?"
            left = f"{r['days_left']:>4}d" if r["origin_known"] else "?"
            print(f"  {st:<9} {r['nature']:<14} {str(r['receipt_date']):<11} "
                  f"{deadline:<11} {left:>6} {money(r['remaining'])}  "
                  f"{r['activity_id']}")

    print()
    print("-" * 78)
    print(f"  BREACH (overdue): {n_breach}   WARN (<= {warn_days}d): {n_warn}"
          f"   ADVISORY (unknown-age): {n_adv}")

    if n_breach:
        print("\n  >> ACTION: overdue idle forex above must be reinvested, put "
              "to a permitted\n     use, or repatriated -- each day late risks "
              "Section 13 penalties:")
        print("       - up to 3x the amount involved (where quantifiable), or")
        print("       - up to Rs. 2 lakh where the amount cannot be "
              "quantified, plus")
        print("       - Rs. 5,000 per day for as long as the contravention "
              "continues,")
        print("       - and confiscation of the sums/assets involved.")
        print("     Read: Section 13, FEMA 1999 -- "
              "https://www.indiacode.nic.in/show-data?actid=AC_CEN_5_23_00035_199942_1517807318288&sectionId=20441&sectionno=13&orderno=13")
        print("     Overview: https://paasa.com/blog/fema-180-day-rule")
    elif n_warn:
        print("\n  >> HEADS-UP: the WARN items cross their 180-day deadline "
              f"within {warn_days} days.\n     Plan to reinvest or repatriate "
              "before the deadline.")
    if n_adv:
        print("\n  NOTE: ADVISORY = opening-balance (or --exclude-lrs) cash "
              "whose true receipt\n     date this tool can't see. If it is "
              "realized forex, date it and re-check.")

    if overdrawn:
        print("\n  DATA WARNING: some uses drew more cash than was on hand, or "
              "an unknown\n     activity type was seen (check your YAML):")
        for broker, date, aid, atype, need in overdrawn:
            extra = f" -- short by {float(need):,.2f}" if need is not None else ""
            loc = f"{broker} {date}" if broker else "(global)"
            print(f"    - {loc} {aid} [{atype}]{extra}")

    print()
    return worst


def print_trace(trace, currencies):
    """Print a chronological log of every credit, consume, and transfer event."""
    print()
    print("=" * 90)
    print("  FEMA ACTIVITY TRACE")
    print("=" * 90)
    if not trace:
        print("\n  (no events)\n")
        return

    LABELS = {
        "credit":       "CREDIT      ",
        "consume":      "CONSUME     ",
        "transfer_out": "TRANSFER-OUT",
        "transfer_in":  "TRANSFER-IN ",
    }

    for ev in trace:
        etype = ev["type"]
        broker = ev["broker"]
        date = ev["date"]
        aid = ev["activity_id"]
        ccy = currencies.get(broker, "")
        label = LABELS.get(etype, etype.upper())

        if etype == "credit":
            print(f"\n  {label}  {broker:<25} {date}  {ev['nature']:<14} "
                  f"{float(ev['amount']):>12,.2f} {ccy}  [{aid}]")

        elif etype == "consume":
            atype = ev.get("activity_type", "")
            shortfall = ev.get("shortfall", ZERO)
            print(f"\n  {label}  {broker:<25} {date}  by [{aid}] ({atype})")
            for p in ev["portions"]:
                days = p["days_held"]
                flag = " *** FEMA OVERDUE at consumption" if p["clock_bearing"] and days > FEMA_WINDOW_DAYS else ""
                print(f"              {'':25}         "
                      f"  {p['source_nature']:<14} {float(p['amount']):>12,.2f} {ccy}"
                      f"  [{p['source_activity_id']}]  rcvd {p['source_date']}  held {days}d{flag}")
            if shortfall > 0:
                print(f"              {'':25}         "
                      f"  {'SHORTFALL':<14} {float(shortfall):>12,.2f} {ccy}")

        elif etype == "transfer_out":
            dst = ev.get("transfer_id", "")
            shortfall = ev.get("shortfall", ZERO)
            print(f"\n  {label}  {broker:<25} {date}  [{aid}]  (transfer_id: {dst})")
            for p in ev["portions"]:
                print(f"              {'':25}         "
                      f"  {p['source_nature']:<14} {float(p['amount']):>12,.2f} {ccy}"
                      f"  [{p['source_activity_id']}]  rcvd {p['source_date']}")
            if shortfall > 0:
                print(f"              {'':25}         "
                      f"  {'SHORTFALL':<14} {float(shortfall):>12,.2f} {ccy}")

        elif etype == "transfer_in":
            src = ev.get("transfer_id", "")
            print(f"\n  {label}  {broker:<25} {date}  [{aid}]  (transfer_id: {src})")
            for p in ev["portions"]:
                print(f"              {'':25}         "
                      f"  {p['source_nature']:<14} {float(p['amount']):>12,.2f} {ccy}"
                      f"  [{p['source_activity_id']}]  rcvd {p['source_date']}")
    print()


def print_late_use(trace, currencies, window_days):
    """Print tranches that were consumed after the FEMA window had already elapsed."""
    late = []
    for ev in trace:
        if ev["type"] != "consume":
            continue
        for p in ev["portions"]:
            if p["clock_bearing"] and p["days_held"] > window_days:
                late.append({**ev, "_portion": p})

    print()
    print("=" * 90)
    print(f"  LATE-USE REPORT  (clock-bearing tranches consumed after {window_days}-day window)")
    print("=" * 90)

    if not late:
        print(f"\n  No clock-bearing tranche was consumed after the {window_days}-day window. Clean.\n")
        return

    for item in late:
        broker = item["broker"]
        ccy = currencies.get(broker, "")
        p = item["_portion"]
        overdue_by = p["days_held"] - window_days
        print(f"\n  Broker : {broker}   ({ccy})")
        print(f"  Consumed by : [{item['activity_id']}] ({item.get('activity_type', '')}) on {item['date']}")
        print(f"  Source      : [{p['source_activity_id']}]  {p['source_nature']}  "
              f"rcvd {p['source_date']}  held {p['days_held']}d  "
              f"(overdue by {overdue_by}d)")
        print(f"  Amount      : {float(p['amount']):,.2f} {ccy}")
    print()


def to_json(findings, overdrawn, currencies, as_of, window_days, warn_days,
            include_lrs):
    out = {
        "as_of": str(as_of),
        "window_days": window_days,
        "warn_days": warn_days,
        "lrs_counted_as_realized": include_lrs,
        "brokers": {},
        "data_warnings": [],
    }
    for broker, rows in findings.items():
        items = []
        for r in rows:
            items.append({
                "activity_id": r["activity_id"],
                "nature": r["nature"],
                "receipt_date": str(r["receipt_date"]),
                "deadline": str(r["deadline"]) if r["origin_known"] else None,
                "days_idle": r["days_idle"] if r["origin_known"] else None,
                "days_left": r["days_left"] if r["origin_known"] else None,
                "remaining_native": round(float(r["remaining"]), 2),
                "status": status_of(r, warn_days),
            })
        items.sort(key=lambda i: (i["days_left"] is None,
                                  i["days_left"] if i["days_left"] is not None
                                  else 0))
        out["brokers"][broker] = {
            "currency": currencies.get(broker, ""),
            "outstanding": items,
        }
    for broker, date, aid, atype, need in overdrawn:
        out["data_warnings"].append({
            "broker": broker, "date": str(date) if date else None,
            "activity_id": aid, "activity_type": atype,
            "shortfall_native": round(float(need), 2) if need is not None
            else None,
        })
    return out


def main():
    default_input = Path(__file__).parent / "data" / "input.yaml"

    ap = argparse.ArgumentParser(
        description="Alert on FEMA 180-day compliance risk for idle foreign "
                    "cash, dividends, interest and sale proceeds.")
    ap.add_argument("--input", default=str(default_input),
                    help="Path to input.yaml (default: data/input.yaml).")
    ap.add_argument("--as-of", default=None,
                    help="Reference date YYYY-MM-DD (default: today).")
    ap.add_argument("--window-days", type=int, default=FEMA_WINDOW_DAYS,
                    help="FEMA window (default: 180).")
    ap.add_argument("--warn-days", type=int, default=30,
                    help="Flag WARN when the deadline is within this many days "
                         "(default: 30).")
    ap.add_argument("--broker", default=None,
                    help="Only check this broker/account key.")
    ap.add_argument("--exclude-lrs", action="store_true",
                    help="Do NOT treat bank_to_cash (LRS) inflows as realized "
                         "forex that starts the clock.")
    ap.add_argument("--all", action="store_true",
                    help="Also list items comfortably within the window (OK).")
    ap.add_argument("--json", action="store_true",
                    help="Emit machine-readable JSON instead of a report.")
    ap.add_argument("--trace", action="store_true",
                    help="After the main report, print a chronological log of "
                         "every credit, consume, and inter-broker transfer event "
                         "showing which tranches were consumed by which activity.")
    ap.add_argument("--flag-late-use", action="store_true",
                    help="After the main report, list clock-bearing tranches that "
                         "were consumed (reinvested / repatriated) only after the "
                         "FEMA window had already elapsed.")
    args = ap.parse_args()

    as_of = Date.fromisoformat(args.as_of) if args.as_of else Date.today()

    with open(args.input) as f:
        doc = yaml.safe_load(f)

    currencies = build_broker_currency_map(doc)
    include_lrs = not args.exclude_lrs
    need_trace = args.trace or args.flag_late_use

    findings, overdrawn, trace = _analyze(doc, as_of, args.window_days,
                                          include_lrs,
                                          collect_trace=need_trace)

    if args.broker:
        findings = {k: v for k, v in findings.items() if k == args.broker}
        overdrawn = [o for o in overdrawn if o[0] == args.broker]
        if need_trace:
            trace = [e for e in trace if e.get("broker") == args.broker]

    if args.json:
        print(json.dumps(
            to_json(findings, overdrawn, currencies, as_of, args.window_days,
                    args.warn_days, include_lrs),
            indent=2))
        worst = 0
        for rows in findings.values():
            for r in rows:
                worst = max(worst, SEV_RANK[status_of(r, args.warn_days)])
    else:
        worst = print_report(findings, overdrawn, currencies, as_of,
                             args.window_days, args.warn_days, include_lrs,
                             args.all)

    if args.trace:
        print_trace(trace, currencies)
    if args.flag_late_use:
        print_late_use(trace, currencies, args.window_days)

    # 0 clean, 1 advisory/warn, 2 breach.
    sys.exit(2 if worst >= 3 else (1 if worst >= 1 else 0))


if __name__ == "__main__":
    main()
