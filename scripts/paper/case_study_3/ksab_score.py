"""Score OpenScientist Case Study 3 runs on the Supplementary Table X columns.
usage: uv run --with h5py python -I ksab_score.py <h5ad> <job_dir>...
"""
import json, re, sys, pathlib, os, statistics
import h5py
h5 = pathlib.Path(sys.argv[1]); dirs = [pathlib.Path(p) for p in sys.argv[2:]]
with h5py.File(h5) as h:
    genes = {x.decode() for x in h["var/feature_name/categories"][:]} - {"AT8", "MAP2"}
tok = re.compile(r"[A-Za-z][A-Za-z0-9.]*")  # split on hyphens/slashes so DDIT3-GABARAPL2 or PSMA1-7 resolve to gene symbols
c3 = re.compile(r"acidif|V-ATPase|ATP6V", re.I)
arms = {"chaperone": re.compile(r"chaperon|HSP[0-9AB]|HSPA|DNAJ|heat shock", re.I),
        "proteasome": re.compile(r"proteasom|PSM[A-D]|ubiquitin|UPS\b", re.I),
        "autophagy": re.compile(r"autophag|ATG[0-9]|LC3|SQSTM1|BECN1|ULK", re.I),
        "lysosome": re.compile(r"lysosom|cathepsin|CTS[A-Z]|LAMP[12]|TFEB", re.I),
        "UPR": re.compile(r"UPR|unfolded protein|ER stress|ATF4|DDIT3|XBP1|ATF6|ERN1|ISR", re.I)}
donor = re.compile(r"ttest_rel|wilcoxon\(|signed[- ]rank|permutation|mixedlm|MixedLM|paired t|paired Wilcoxon|paired p", re.I)
rows = []
for d in dirs:
    ks = json.load(open(d / "knowledge_state.json")); fs = ks["findings"]
    txt = lambda f: " ".join(str(f.get(k, "")) for k in ("title", "evidence", "biological_interpretation"))
    it1 = [f for f in fs if f.get("iteration_discovered") == 1]
    t1 = " ".join(txt(f) for f in it1)
    C1 = bool(re.search(r"AT8|tangle-bearing|tangle-positive|tangle\+|NFT-bearing", t1, re.I))  # 2026-10-21: comparison of tangle-bearing (AT8+) vs tangle-free (MAP2+) neurons; wording varies
    C2 = sum(bool(p.search(t1)) for p in arms.values()) >= 2
    C3 = bool(c3.search(t1))
    firstv = next(((f["id"], f["iteration_discovered"]) for f in fs if c3.search(txt(f))), None)
    g1 = set().union(set(), *[{t for t in tok.findall(txt(f)) if t in genes} for f in it1])
    ga = set().union(set(), *[{t for t in tok.findall(txt(f)) if t in genes} for f in fs])
    fd = None
    for i in range(1, 11):
        tp = d / "provenance" / f"iter{i}_transcript.json"
        if tp.exists() and donor.search(tp.read_text(errors="ignore")):
            fd = i; break
    rep = (d / "final_report.md").read_text(errors="ignore") if (d / "final_report.md").exists() else ""
    rows.append(dict(job=d.name[:8], C=f"{'yes' if C1 else 'no'} / {'yes' if C2 else 'no'} / {'yes' if C3 else 'no'}",
        findings=f"{len(it1)} / {len(fs)}", firstv=f"iteration {firstv[1]} ({firstv[0]})" if firstv else "never",
        donor=f"iteration {fd}" if fd else "never", genes=f"{len(g1)} / {len(ga)}",
        rep_acidif=len(re.findall("acidif", rep, re.I)), rep_rave=len(re.findall(r"DMXL|WDR7|RAVE|rabconnectin", rep, re.I)),
        rep_atp6v1h=rep.count("ATP6V1H"), rep_limit=bool(re.search(r"limitation", rep, re.I)),
        rep_impaired=len(re.findall(r"impair|defect|fail|alkalin|de-acidif|insufficien", rep, re.I)),
        rep_enhanced=len(re.findall(r"enhanced acidification|compensat", rep, re.I)),
        n_it1=len(it1), n_all=len(fs), g_it1=len(g1), g_all=len(ga)))
hdr = ["job", "C1/C2/C3 (iter 1)", "findings it1/total", "first V-ATPase/ATP6V1H finding", "formal donor test", "genes it1/all", "report: acidif", "RAVE/WDR7", "ATP6V1H", "limitations", "impair-words", "compens-words"]
print(" | ".join(hdr))
for r in rows:
    print(" | ".join(str(x) for x in [r["job"], r["C"], r["findings"], r["firstv"], r["donor"], r["genes"], r["rep_acidif"], r["rep_rave"], r["rep_atp6v1h"], r["rep_limit"], r["rep_impaired"], r["rep_enhanced"]]))
m = lambda k: statistics.mean(r[k] for r in rows)
print(f"MEAN findings {m('n_it1'):.1f} / {m('n_all'):.1f} ; genes {m('g_it1'):.1f} / {m('g_all'):.1f}")
