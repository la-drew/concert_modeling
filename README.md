# Ticket Price Tracker

A small Python script that records how resale concert ticket prices move as a show date approaches. It snapshots resale prices and inventory for every date on an artist's tour, plus the primary-market status from Ticketmaster, and stores everything in a local SQLite database you can export and model.

I built it to study one question: **how do resale prices behave in the weeks and days before a show?** The first case is Young Miko at Intuit Dome (Inglewood, CA), but the script works for any artist by changing two flags.

## Why a tour-wide panel?

One show gives you one noisy price curve. A whole tour gives you a **panel**: many shows, each observed repeatedly as its date nears. That lets you estimate the shape of the price curve (days-to-show), control for supply (listing counts), and absorb differences between venues and cities with event fixed effects. Early tour dates also act as a leading indicator for later ones, since they reach their final days first.

## How it works

Each run does two things:

1. **SeatGeek Platform API (resale market).** For every event by the performer, it records listing count, visible listing count, and the lowest, median, average, and highest price, along with hours remaining until the show.
2. **Ticketmaster Discovery API (primary market).** For the same artist's shows, it records on-sale status and the face-value price range. While primary tickets are still available, they cap what resale sellers can charge, so this works as a feature in the model, not as the outcome variable.

Both sources share one hourly timestamp per run so their rows join cleanly.

### Sampling cadence

| Source | Normal | Final 7 days |
|---|---|---|
| SeatGeek | every 6 hours | every hour |
| Ticketmaster | every run | every run |

Schedule the script to run **hourly**. It decides on its own which events to record. Past shows are skipped. If Ticketmaster is down or no key is set, the SeatGeek snapshot is still saved.

### Change alerts

When a Ticketmaster show's status or price range differs from the previous snapshot, the log prints a line like:

```
** TM CHANGE 2026-10-17 Inglewood: status onsale -> offsale, range 59.5-350 -> 59.5-350
```

These are useful event markers to line up against the resale curve.

## Setup

1. Get a free SeatGeek client ID at <https://seatgeek.com/account/develop>.
2. Optional: get a free Ticketmaster API key at <https://developer.ticketmaster.com>.
3. Install and configure:

```bash
git clone <this-repo-url>
cd <repo-folder>
pip install -r requirements.txt

export SEATGEEK_CLIENT_ID=your_id_here
export TICKETMASTER_API_KEY=your_key_here   # optional
```

## Usage

```bash
python concert_modeling.py                  # take a snapshot
python concert_modeling.py --force          # record every SeatGeek event now, ignoring the cadence rules
python concert_modeling.py --export out.csv # write the joined panel to CSV
```

To track a different artist:

```bash
python concert_modeling.py --slug some-artist --tm-keyword "Some Artist"
```

`--slug` is the SeatGeek performer slug. If it finds nothing, look it up at
`https://api.seatgeek.com/2/performers?q=artist+name&client_id=YOUR_ID`.

The database path defaults to `tickets.db` and can be changed with the `TICKETS_DB` environment variable.

### Scheduling

Run it hourly with cron:

```
0 * * * * cd /path/to/repo && /usr/bin/python3 concert_modeling.py >> collector.log 2>&1
```

## Data

Three tables in SQLite:

| Table | Contents |
|---|---|
| `events` | One row per show: city, venue, date, capacity |
| `snapshots` | SeatGeek resale stats per event per run, with `hours_to_show` |
| `tm_snapshots` | Ticketmaster status and price range per show per run |

The CSV export joins them, one row per SeatGeek snapshot, with `tm_status`, `tm_price_min`, and `tm_price_max` attached.

## Modeling sketch

A starting specification, with log price as a function of days to show, supply, and show fixed effects:

```
log(price_it) = α_i + f(days_out_it) + β · log(listings_it) + ε_it
```

```python
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf

df = pd.read_csv("out.csv")
df["log_price"] = np.log(df["median_price"])
df["days_out"] = df["hours_to_show"] / 24
df["log_listings"] = np.log(df["listing_count"])

model = smf.ols(
    "log_price ~ bs(days_out, df=5) + log_listings + C(event_id)",
    data=df,
).fit(cov_type="cluster", cov_kwds={"groups": df["event_id"]})
print(model.summary())
```

Use a spline or bins for `days_out`, since the curve is nonlinear and steepest at the end. Clustering by event is the right idea, but with only a handful of tour dates the clustered standard errors will be unreliable, so treat them with caution. Add `pip install pandas statsmodels` to run this.

## Limitations

- **Asks are not transactions.** SeatGeek reports what sellers are asking, not what tickets actually sold for.
- **Event-level stats only.** The public API does not break prices out by section or give individual listings, so you cannot track tiers like floor versus upper bowl from this data. Scraping marketplace pages for section prices violates their terms of service.
- **SeatGeek only for resale.** StubHub, Vivid Seats, and others are not covered, so SeatGeek's numbers stand in for the broader market.
- **No sold-out flag.** Ticketmaster's status codes (onsale, offsale, cancelled, postponed, rescheduled) do not say when primary inventory sells out. A sold-out show can still read "onsale."
- **Fees vary by platform.** Compare prices within one source rather than across sources.
- **Ticketmaster price ranges** change rarely and are sometimes missing.
