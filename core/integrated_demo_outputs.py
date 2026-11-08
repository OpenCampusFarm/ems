"""Five-part integrated demo output, plus optional fan counterfactual comparison."""
import json
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


def build_outputs(inputs, runs, summary, off, out, label, weather, show):
    def save(fig, name):
        for ax in fig.axes:
            ax.grid(alpha=.18)
            handles, labels = ax.get_legend_handles_labels()
            if handles: ax.legend(fontsize=8, loc='upper left', ncol=3)
        fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(out/name, dpi=150)
        if not show: plt.close(fig)
    first = next(iter(runs.values()))
    meta = {'period_start':str(inputs.index[0]),'period_end':str(inputs.index[-1]),
            'weather_file':weather,'weather_note':'Weather scenario mapped to September dates; not a matched historical replay' if 'scenario' in weather else 'Historical input',
            'moer_filled_hours':float(inputs.moer_filled.sum()/60),
            'initial_ev_soc_percent':80,'initial_battery_soc_percent_by_strategy':{k:float(r.battery_soc_percent.iloc[0]) for k,r in runs.items()},
            'thermal_assumptions':{'R_F_per_kW':3,'C_kWh_per_F':1,'sensible_capacity_kW':15670/3412.142,'effective_airflow_fraction':.65}}
    (out/'data'/'setup.json').write_text(json.dumps(meta,indent=2))
    fig, axes = plt.subplots(3,1,figsize=(13,8),sharex=True)
    for ax,col,title in zip(axes,['pv_kw','outdoor_f','moer_lb_per_mwh'],['PV (kW)','Outdoor temperature (°F)','MOER (lb CO₂/MWh)']):
        ax.plot(inputs.index,inputs[col]);ax.set_ylabel(title)
    fig.suptitle(f'1. Simulation setup — {label}\n'+meta['weather_note']+'\nEV initial SoC: 80%; stationary battery initial SoC: '+', '.join(f'{k}: {v:.1f}%' for k,v in meta['initial_battery_soc_percent_by_strategy'].items()),fontsize=10)
    save(fig,'01_simulation_setup.png')
    names=['without-ems','with-ems','staged-ems']
    labels=['No EMS','Binary EMS','Staged EMS']
    colors={'PV':'#d4a800','Compressor':'#2774ae','Fan':'#16a085','EV':'#e87800','Other loads':'#888888'}
    fig,axes=plt.subplots(2,3,figsize=(18,8),sharex=True,sharey='row')
    for j,(name,title) in enumerate(zip(names,labels)):
        r=runs[name]
        for col,key in [('pv_kw','PV'),('compressor_kw','Compressor'),('fan_kw','Fan'),('ev_kw','EV'),('other_load_kw','Other loads')]:
            axes[0,j].plot(r.index,r[col],label=key,color=colors[key],lw=.8)
        axes[0,j].set_title(title)
        for values,key,color in [(r.battery_kw.clip(lower=0),'Battery charge','#16a085'),(-r.battery_kw.clip(upper=0),'Battery discharge','#8054a1'),(r.grid_buy_kw,'Grid import','#c43d35'),(r.grid_sell_kw,'Grid export','#777777')]:
            axes[1,j].plot(r.index,values,label=key,color=color,lw=.8)
    axes[0,0].set_ylabel('Supply and loads (kW)');axes[1,0].set_ylabel('Battery and grid (kW)')
    fig.suptitle('2. Energy management — same inputs, three control strategies')
    save(fig,'02_energy_management.png')
    fig,axes=plt.subplots(2,3,figsize=(18,8),sharex=True,sharey='row')
    for j,(name,title) in enumerate(zip(names,labels)):
        r=runs[name]
        for col,key,color in [('cooler_temp_f','Indoor','#2774ae'),('outdoor_f','Outdoor','#888888'),('setpoint_f','Setpoint','#e87800')]:axes[0,j].plot(r.index,r[col],label=key,color=color,lw=1)
        axes[0,j].axhspan(40,55,color='green',alpha=.08)
        axes[0,j].axhline(55,color='red',ls='--',lw=.7)
        axes[0,j].set_title(title)
        for col,key,color in [('fan_duty','Fan','#16a085'),('compressor_duty','Compressor','#2774ae')]:axes[1,j].step(r.index,r[col],where='post',label=key,color=color,lw=.7)
        note=f"Range: {r.cooler_temp_f.min():.1f}–{r.cooler_temp_f.max():.1f}°F | >55°F: {(r.cooler_temp_f>55).sum()} min\nFan: {r.fan_kw.sum()/60:.2f} kWh | Compressor: {r.compressor_kw.sum()/60:.2f} kWh"
        if name in off:note+=f"\nCooling energy vs fan off: {(r.cooler_kw.sum()-off[name].cooler_kw.sum())/60:+.2f} kWh"
        axes[1,j].set_xlabel(note,fontsize=8)
    axes[0,0].set_ylabel('Temperature (°F)');axes[1,0].set_ylabel('Fraction of minute on')
    fig.suptitle('3. Cooler operation — temperatures and equipment states\nNo EMS targets 38°F; green band is the 40–55°F evaluation range',fontsize=12)
    save(fig,'03_cooler_operation.png')
    fig,axes=plt.subplots(3,3,figsize=(18,10),sharex=True,sharey='row')
    for j,(name,title) in enumerate(zip(names,labels)):
        r=runs[name]
        axes[0,j].plot(r.index,r.ev_soc_percent,label='EV SoC',color='#2774ae')
        axes[0,j].axhline(80,ls='--',color='gray',label='80% cap');axes[0,j].set_ylim(0,90);axes[0,j].set_title(title)
        away=~r.ev_plugged.astype(bool);groups=(away!=away.shift()).cumsum()
        for t in r.index[away & ~away.shift(fill_value=False)]:
            i=r.index.get_loc(t);soc=r.ev_soc_percent.iloc[max(0,i-1)]
            axes[0,j].scatter(t,soc,color='purple',zorder=4)
            axes[0,j].annotate(f'{soc:.1f}%',(t,soc),xytext=(0,-16),textcoords='offset points',fontsize=8)
            axes[0,j].plot([t],[30],marker='_',color='red',ms=14)
        for _,part in r[away].groupby(groups[away]):
            for ax in axes[:,j]:ax.axvspan(part.index[0],part.index[-1],color='purple',alpha=.1)
        axes[1,j].plot(r.index,r.ev_kw,label='EV charging',color='#e87800')
        axes[1,j].plot(r.index,r.ev_surplus_pv_kw,label='Surplus PV',color='gray',alpha=.5)
        axes[1,j].fill_between(r.index,0,r.ev_kw,where=r.ev_reserve_override,color='red',label='Reserve charging')
        axes[2,j].plot(r.index,r.battery_soc_percent,label='Stationary battery',color='#16a085');axes[2,j].set_ylim(0,105)
        axes[2,j].set_xlabel(f"EV: {r.ev_kw.sum()/60:.1f} kWh | Final SoC: {r.ev_soc_percent.iloc[-1]:.1f}%",fontsize=9)
    for i,ylab in enumerate(['EV SoC (%)','Charging power (kW)','Battery SoC (%)']):axes[i,0].set_ylabel(ylab)
    fig.suptitle('4. EV and battery — purple shading: deliveries; red ticks: 30% departure requirement')
    save(fig,'04_ev_and_battery.png')
    fig,axes=plt.subplots(2,2,figsize=(12,8))
    labels=['No EMS','Binary EMS','Staged EMS'];colors=['gray','#2d79bd','#e87800']
    for ax,col,title in zip(axes.flat,['grid_emissions_lb','grid_buy_kwh','ev_final_soc_percent','cooler_unsafe_minutes'],['Grid emissions (lb CO₂)','Grid import (kWh)','End-of-week EV SoC (%)','Cooler outside 40–55°F (minutes)']):
        if col == 'cooler_unsafe_minutes':
            x=np.arange(3)
            ax.bar(x-.18,summary.cooler_below_40_minutes,.36,label='Below 40°F')
            ax.bar(x+.18,summary.cooler_above_55_minutes,.36,label='Above 55°F')
            ax.set_xticks(x,labels);ax.set_title('Temperature excursions (minutes)')
            continue
        vals=summary[col].to_numpy();ax.bar(labels,vals,color=colors);ax.set_title(title)
        for i,v in enumerate(vals):ax.text(i,v,f'{v:.1f}',ha='center',va='bottom')
        ax.margins(y=.2)
    fig.suptitle('5. Overall comparison — assess service alongside emissions\nNo EMS targets 38°F: below-40°F time includes its intended operation',fontsize=12)
    save(fig,'05_overall_comparison.png')
    if off:
        rows=[]
        for name,r in off.items():
            for mode,part in [('Off',r),('Auto',runs[name])]:
                rows.append(dict(controller=name,fan_mode=mode,fan_kwh=part.fan_kw.sum()/60,compressor_kwh=part.compressor_kw.sum()/60,cooling_kwh=part.cooler_kw.sum()/60,grid_co2_lb=(part.grid_buy_kw*part.moer_lb_per_mwh).sum()/60000,unsafe_minutes=int(((part.cooler_temp_f<40)|(part.cooler_temp_f>55)).sum())))
        table=pd.DataFrame(rows);table.to_csv(out/'data'/'fan_power_comparison.csv',index=False)
    (out/'README.md').write_text("""# Weekly demo outputs

Open the five numbered PNG files in order:
1. Setup: shared PV, weather and MOER inputs plus initial states.
2. Energy management: No EMS / Binary / Staged side by side.
3. Cooler: all three strategies, temperatures, states, energy and fan comparison.
4. EV and battery: all three strategies, deliveries and reserve charging.
5. Overall comparison: all three strategies, including temperature and EV service.

`summary.csv` is the single strategy-comparison table. Details are in `data/`:
minute data, delivery records, scenario metadata and optional fan Off/Auto comparison.
Older generated files are retained in `_previous/` for recovery.
Weather scenario and MOER filling details are recorded in `data/setup.json`.
The cooler evaluation range remains 40–55°F; No EMS targets 38°F.
""")
