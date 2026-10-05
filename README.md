# LP-Flow Monitor: public page

This branch holds the generated page served at https://ivan05021982.github.io/lp-flow-monitor/
and the data behind it. It is rebuilt after each weekly run; do not edit it by hand.

- `index.html`: the page
- `data/daily_lp_flow.csv`: per pool and UTC day, the Mint/Burn counts, the token amounts, the two daily prices used and the USD inflow, outflow and net
- `data/monitoring_log.csv`: the weekly log, one row per pool and window, with the source of each row

Code, method and limits are on the `main` branch.
