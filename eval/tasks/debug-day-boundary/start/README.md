# ledgerd

A small internal service that turns a payment export into a day or month
report. The finance team pastes the console output into the month-end sheet,
so both the row count and the totals are read by humans.

## Data

`data/march.csv` has one row per transaction:

```
id,ts,currency,amount,memo
tx-001,2024-03-01 00:00:00,CNY,10.00,opening float
```

Timestamps are local, naive, and may carry microseconds. Amounts are exact
decimals; the service rounds to cents only when it sums.

**A transaction belongs to the day it is stamped with.** A batch stamped
`2024-03-02 00:00:00` is part of 2 March, not 1 March, even though it settles
the previous day's cards. The month window follows the same rule.

## Commands

```
python -m ledgerd.cli 2024-03-01
python -m ledgerd.cli --month 2024-03
python -m ledgerd.cli --store data/march.csv 2024-03-05
```

`2024-03-01` has three rows and the report must end with:

```
TOTAL CNY 120.50
```

March has ten rows and its month report must end with:

```
TOTAL CNY 438.25
TOTAL EUR 7.77
TOTAL USD 30.00
```

Exit code is 0 on success and 2 on a bad argument or unreadable file, with a
single `error: ...` line on stderr.
