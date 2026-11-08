"""EV charts and delivery records for the integrated weekly demo."""
import pandas as pd
import matplotlib.pyplot as plt


def save_ev_outputs(r, controller, out, charts=True):
    away = ~r.ev_plugged.astype(bool)
    starts = away & ~away.shift(fill_value=False)
    ends = ~away & away.shift(fill_value=False)
    rows = []
    for t in r.index[starts]:
        i = r.index.get_loc(t)
        soc = r.ev_soc_percent.iloc[i-1] if i else r.ev_soc_percent.iloc[i]
        returned = r.index[ends & (r.index > t)]
        rows.append(dict(departure=t, departure_soc_percent=soc, meets_30_percent=soc >= 30-1e-6,
                         returned=returned[0] if len(returned) else None,
                         return_soc_percent=r.loc[returned[0], 'ev_soc_percent'] if len(returned) else None))
    pd.DataFrame(rows).to_csv(out / f'{controller}_ev_deliveries.csv', index=False)
    if not charts:
        return
    fig, ax = plt.subplots(figsize=(12,4))
    ax.plot(r.index, r.ev_soc_percent, label='EV SoC')
    ax.axhline(80, ls='--', color='gray', label='80% limit')
    for row in rows:
        ax.scatter(row['departure'], row['departure_soc_percent'], color='purple', zorder=4)
        ax.annotate(f"Departure: {row['departure_soc_percent']:.1f}%", (row['departure'], row['departure_soc_percent']), xytext=(0,-22), textcoords='offset points')
    ax.set(ylabel='SoC (%)', title=f'{controller}: weekly EV state of charge', ylim=(0,90))
    ax.legend();fig.autofmt_xdate();fig.tight_layout();fig.savefig(out/f'{controller}_ev_soc.png',dpi=160);plt.close(fig)
    fig, ax = plt.subplots(figsize=(12,4))
    ax.plot(r.index,r.ev_surplus_pv_kw,color='gray',alpha=.6,label='Available surplus PV')
    ax.step(r.index,r.ev_kw,where='post',label='EV charging')
    ax.fill_between(r.index,0,r.ev_kw,where=r.ev_reserve_override,color='orange',label='Reserve charging')
    ax.set(ylabel='Power (kW)',title=f'{controller}: charging and surplus solar');ax.legend()
    fig.autofmt_xdate();fig.tight_layout();fig.savefig(out/f'{controller}_ev_power.png',dpi=160);plt.close(fig)
