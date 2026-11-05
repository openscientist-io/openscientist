"""Extended Data Fig. 2 (Case Study 3, Experiment 4): cumulative findings and distinct dataset genes per iteration.
usage: uv run --with h5py,numpy,matplotlib python -I exp4_plot.py <ids_dir> <jobs_dir> <out_dir>
"""
import json, re, sys, pathlib, statistics
import h5py, numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
R = pathlib.Path(sys.argv[1])  # directory holding ksab_*_ids.txt
J = pathlib.Path(sys.argv[2])  # directory holding the job folders (knowledge_state.json per job)
OUT = pathlib.Path(sys.argv[3]); OUT.mkdir(parents=True, exist_ok=True)  # output directory
ids = {k: [l.strip() for l in (R/f).read_text().splitlines() if l.strip()] for k,f in
       [("control","ksab_control_ids.txt"),("ks_ablated","ksab_ablated_ids.txt"),("ks_ablated2","ksab_ablated2_ids.txt"),("memoryless","ksab_memoryless_ids.txt")]}
h5 = next((J/ids["control"][0]/"data").glob("*.h5ad"))
with h5py.File(h5) as h: genes = {x.decode() for x in h["var/feature_name/categories"][:]} - {"AT8","MAP2"}
tok = re.compile(r"[A-Za-z][A-Za-z0-9.]*")  # split on hyphens/slashes so DDIT3-GABARAPL2 or PSMA1-7 resolve to gene symbols
c3 = re.compile(r"acidif|V-ATPase|ATP6V", re.I)
def curves(jid):
    ks = json.load(open(J/jid/"knowledge_state.json")); fs = ks["findings"]
    txt = lambda f: " ".join(str(f.get(k,"")) for k in ("title","evidence","biological_interpretation"))
    nit = max([f.get("iteration_discovered",1) for f in fs]+[ks.get("iteration",10)])
    F=[];G=[]; seen=set()
    for i in range(1,11):
        cur=[f for f in fs if f.get("iteration_discovered",1)<=i]
        F.append(len(cur))
        for f in cur: seen |= {t for t in tok.findall(txt(f)) if t in genes}
        G.append(len(seen))
    firstv = next((f["iteration_discovered"] for f in fs if c3.search(txt(f))), None)
    return F,G,firstv,len(fs),nit
res={}
for arm,lst in ids.items():
    res[arm]=[]
    for jid in lst:
        if not (J/jid/"knowledge_state.json").exists(): print("no KS:",arm,jid[:8]); continue
        res[arm].append((jid[:8],)+curves(jid))
for arm,rows in res.items():
    if not rows: continue
    F=np.array([r[1] for r in rows]); G=np.array([r[2] for r in rows])
    print(f"{arm:12s} n={len(rows)} findings it1/all {F[:,0].mean():.1f}/{F[:,-1].mean():.1f}  genes it1/all {G[:,0].mean():.1f}/{G[:,-1].mean():.1f}  ATP6V1H found {sum(r[3] is not None for r in rows)}/{len(rows)}  (max iter seen {max(r[5] for r in rows)})")
    for r in rows: print("   ",r[0],"F",r[1],"G",r[2],"firstV",r[3])
# ---- Nature-style figure: control arm only (Supp Table 7)
plt.rcParams.update({"font.family":"sans-serif","font.sans-serif":["Helvetica","Arial","DejaVu Sans"],"font.size":7,
    "axes.linewidth":0.5,"xtick.major.width":0.5,"ytick.major.width":0.5,"xtick.major.size":2,"ytick.major.size":2,
    "axes.spines.top":False,"axes.spines.right":False,"pdf.fonttype":42,"ps.fonttype":42})
rows=res["control"]; it=np.arange(1,11)
F=np.array([r[1] for r in rows]); G=np.array([r[2] for r in rows])
fig,axes=plt.subplots(1,2,figsize=(120/25.4,55/25.4))
for ax,Y,lab,panel in [(axes[0],F,"Cumulative findings",'a'),(axes[1],G,"Cumulative dataset genes\nnamed in findings",'b')]:
    for y in Y: ax.plot(it,y,color="#9e9e9e",lw=0.6,alpha=0.8,zorder=1)
    m=Y.mean(0); sd=Y.std(0,ddof=1)
    ax.fill_between(it,m-sd,m+sd,color="#1f77b4",alpha=0.15,lw=0,zorder=2)
    ax.plot(it,m,color="#1f77b4",lw=1.6,marker="o",ms=2.5,zorder=3)
    ax.set_xlabel("Iteration"); ax.set_ylabel(lab); ax.set_xticks(it); ax.set_xlim(0.7,10.3); ax.set_ylim(0,None)
    ax.text(-0.22,1.05,panel,transform=ax.transAxes,fontsize=9,fontweight="bold",va="bottom")
    ax.text(0.03,0.97,f"iteration 1: {m[0]:.1f}\niteration 10: {m[-1]:.1f}",transform=ax.transAxes,va="top",fontsize=6,color="#1f77b4")
fig.text(0.5,0.005,"Case Study 3, 10 replicate runs (grey); mean ± s.d. (blue)",ha="center",fontsize=6,color="#555")
fig.tight_layout(rect=(0,0.03,1,1))
for ext in ("pdf","png","svg"): fig.savefig(OUT/f"exp4_iterations.{ext}",dpi=300)
json.dump({a:[dict(job=r[0],findings=r[1],genes=r[2],first_atp6v1h=r[3]) for r in rows] for a,rows in res.items()},open(OUT/"exp4_curves.json","w"),indent=1)
print("saved", OUT)
