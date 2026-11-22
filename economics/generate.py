"""Воспроизводимый сценарный расчёт; запуск: python generate.py."""
from pathlib import Path
import csv
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

ROOT = Path(__file__).resolve().parent
FIG = ROOT / 'graphs'
FIG.mkdir(exist_ok=True)
P = dict(rate_per_1000=0.065, minimum_api=600, ideal_tokens=2500,
         route_tokens=1000, selection_tokens=5700, audit_tokens=4300,
         fixed_ai=10000, fixed_web=30000, monthly_price=75000,
         development=300000, amortization_months=24)
(ROOT / 'assumptions.json').write_text(json.dumps(P, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
C_IDEAL = P['ideal_tokens'] * P['rate_per_1000'] / 1000
C = sum(P[k] for k in ('route_tokens', 'selection_tokens', 'audit_tokens')) * P['rate_per_1000'] / 1000
def api(q, c=C):
    q = np.asarray(q)
    return np.where(q > 0, np.maximum(P['minimum_api'], q*c), 0)
def ai(q, c=C):
    return P['fixed_ai'] + api(q, c)
def total(q):
    return P['fixed_web'] + ai(q)
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11,'axes.spines.top':False,
                     'axes.spines.right':False,'figure.facecolor':'white','axes.titleweight':'bold'})
fmt = FuncFormatter(lambda x, _: f'{x:,.0f}'.replace(',', ' '))
def finish(fig, name):
    fig.text(.02,.015,'Сценарий для защиты • 22.11.2026 • расходы и токены — допущения; тариф условно применим',fontsize=8,color='#526174')
    fig.tight_layout(rect=(0,.05,1,1))
    fig.savefig(FIG / name, dpi=180)
    plt.close(fig)
q = np.geomspace(100, 1000000, 500)
fig, axes = plt.subplots(1,2,figsize=(13,5))
for ax,c,title in zip(axes,[C_IDEAL,C],['Один пользовательский запрос = один вызов','Сценарий: маршрутизация + выбор + аудит']):
    ax.plot(q, ai(q,c)/q, lw=2.5, label='Средние расходы ИИ: (F + API) / Q')
    ax.axhline(c,color='#de6b35',ls='--',label=f'Предел: {c:.4f} ₽/запрос')
    ax.set(xscale='log',yscale='log',xlabel='Q, запросов за месяц',ylabel='₽ за пользовательский запрос',title=title)
    ax.xaxis.set_major_formatter(fmt)
    ax.grid(alpha=.2,which='both'); ax.legend(fontsize=9)
finish(fig,'01-average-cost.png')
q = np.linspace(0,200000,501)
fig,ax=plt.subplots(figsize=(11,6))
ax.stackplot(q,np.full_like(q,P['fixed_web']),np.full_like(q,P['fixed_ai']),api(q),
             labels=['Веб: постоянные 30 000 ₽','ИИ: постоянные 10 000 ₽','Оплата API'],colors=['#c4d8eb','#689cc4','#df9b6c'])
ax.set(xlabel='Q, запросов в ИИ за месяц',ylabel='Расходы, ₽/месяц',title='Полные эксплуатационные расходы сервиса')
ax.xaxis.set_major_formatter(fmt);ax.yaxis.set_major_formatter(fmt);ax.legend(loc='upper left');ax.grid(alpha=.2)
finish(fig,'02-total-cost.png')
fig,ax=plt.subplots(figsize=(11,6))
ax.plot(q,total(q),label='Эксплуатационные расходы',lw=2.5)
ax.plot(q,total(q)+P['development']/P['amortization_months'],label='Расходы с распределением разработки',ls=':',lw=2)
ax.axhline(P['monthly_price'],color='#299578',label='Гипотеза цены: 75 000 ₽/месяц')
limit=(P['monthly_price']-P['fixed_web']-P['fixed_ai'])/C
ax.axvline(limit,color='#de6b35',ls='--',label=f'Предел покрытия текущих расходов: {limit:,.0f} запросов'.replace(',',' '))
ax.set(xlabel='Q, запросов в ИИ за месяц',ylabel='₽/месяц',title='Фиксированная подписка: граница рентабельности')
ax.xaxis.set_major_formatter(fmt);ax.yaxis.set_major_formatter(fmt);ax.legend();ax.grid(alpha=.2)
finish(fig,'03-subscription.png')
fig,ax=plt.subplots(figsize=(11,6))
q=np.geomspace(1000,1000000,500)
for tokens in [2500,11000,22000]:
    c=tokens*P['rate_per_1000']/1000
    ax.plot(q,ai(q,c)/q,label=f'{tokens:,} токенов на запрос; предел {c:.4f} ₽'.replace(',',' '),lw=2)
ax.set(xscale='log',yscale='log',xlabel='Q, запросов за месяц',ylabel='₽ за пользовательский запрос',title='Чувствительность к суммарному объёму токенов')
ax.xaxis.set_major_formatter(fmt);ax.legend();ax.grid(alpha=.2,which='both')
finish(fig,'04-token-sensitivity.png')
rows=[]
for n in [0,100,1000,10000,50000,100000,1000000]:
    bill=float(api(n)); costs=float(total(n))
    rows.append([n,round(bill,2),round(float(ai(n)),2),round(float(ai(n))/n,4) if n else '',round(costs,2),round(P['monthly_price']-costs,2)])
with (ROOT/'calculations.csv').open('w',encoding='utf-8-sig',newline='') as f:
    writer=csv.writer(f);writer.writerow(['Q','API_RUB','AI_RUB','AI_RUB_per_query','SERVICE_RUB','OPERATING_RESULT_RUB']);writer.writerows(rows)
assert np.isclose(C, .715)
assert np.isclose(C_IDEAL, .1625)
assert np.isclose(total(10000), 47150)
assert np.isclose(P['monthly_price']-total(100000), -36500)
assert abs(float(ai(1e12))/1e12-C)<1e-7
assert np.isclose(total(limit),P['monthly_price'])
print(json.dumps({'ideal_call_cost':C_IDEAL,'pipeline_cost':C,'max_Q_at_price':limit,'figures':4,'checks':'passed'},ensure_ascii=False))
