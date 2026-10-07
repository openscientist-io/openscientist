---
name: systems-biology-simulation
description: Find, build, and simulate mechanistic (SBML/Antimony) models of biological processes, and perturb them to test hypotheses
category: domain
---

# Systems-Biology Simulation

## When to Use This Skill

- The question concerns the *dynamics* of a process: enzyme kinetics, signalling cascades, protein aggregation, drug pharmacology, disease progression.
- A hypothesis is a "what if": knock out a gene, change a dose, double a rate constant, remove a feedback loop.
- A published mechanistic model exists (BioModels, a paper's supplement, a user-uploaded `.xml`/`.sbml`/`.ant`/`.omex` file), or the mechanism is simple enough to write down as reactions.

Simulation complements, not replaces, data analysis. Use it to turn a mechanistic claim into a quantitative prediction, then compare the prediction to data or literature values.

## Workflow

1. **Find a model.** `search_biomodels(query=...)` for curated SBML models; `search_pubmed` for papers whose supplement has a model. If the user uploaded a model file, it is in `data_files` (file_type `"model"`).
2. **Fetch it.** `fetch_biomodel(model_id=...)` saves `provenance/biomodels/<id>.xml`; inside `execute_code` the path is `/output/biomodels/<id>.xml`.
3. **Static check (no compute).** List species, reactions, parameters and rate laws. Confirm the parameter you want to perturb actually appears in a rate law — models sometimes declare parameters they never read. Note units and the time scale.
4. **Time one run.** Simulate the baseline once and print the wall-clock time. Size every later scan from that number so the whole `execute_code` call fits the `timeout=` you request (60 s default; the server has a configurable maximum, typically 600 s).
5. **Baseline, then perturbations.** One condition or one small scan per call. Save every result to `/output` as CSV plus a plot so later calls and the report can use them.
6. **Record findings** with `update_knowledge_state`: the model ID, the perturbation, the quantitative effect (fold change, time to half-max, steady state), and the comparison to data or literature. Negative results count.
7. **Out of budget?** If the required compute (replicates × conditions × per-run time) clearly exceeds what fits in a call, do not start it. Record a finding with the estimated CPU-hours, the plan (conditions, replicates), and what a single run showed. That estimate is the deliverable; larger batches belong on external compute.

## Recipes (inside `execute_code`)

### Inspect an SBML model

```python
import roadrunner, time
rr = roadrunner.RoadRunner("/output/biomodels/BIOMD0000000462.xml")
print("species:", rr.getFloatingSpeciesIds())
print("params:", rr.getGlobalParameterIds())
print("reactions:", rr.getReactionIds())
print("initial values:", dict(zip(rr.getFloatingSpeciesIds(), rr.getFloatingSpeciesInitialConcentrations())))
# Rate laws, human-readable, via Antimony:
import antimony
antimony.loadSBMLFile("/output/biomodels/BIOMD0000000462.xml")
print(antimony.getAntimonyString(None))   # human-readable reactions + rate laws
```

### Baseline simulation, timed

```python
import roadrunner, time, pandas as pd, matplotlib.pyplot as plt
rr = roadrunner.RoadRunner("/output/biomodels/BIOMD0000000462.xml")
t0 = time.perf_counter()
res = rr.simulate(0, 100, 501)           # start, end, points
print(f"one run: {time.perf_counter()-t0:.2f}s")
df = pd.DataFrame(res, columns=res.colnames)
df.to_csv("/output/sim_baseline.csv", index=False)
df.plot(x="time"); plt.title("baseline"); plt.savefig("/output/sim_baseline.png")
```

### Perturbation: knock down / scale a parameter

```python
rr.resetToOrigin()
k0 = rr["k_plus"]
for scale in (0.25, 0.5, 1.0, 2.0):
    rr.resetToOrigin()
    rr["k_plus"] = k0 * scale
    res = rr.simulate(0, 100, 501)
    pd.DataFrame(res, columns=res.colnames).to_csv(f"/output/sim_kplus_x{scale}.csv", index=False)
```

Knockout = set the species' initial amount to 0 and, if it is produced, set the producing rate constant to 0. Dose = set the drug species' initial concentration or add a boundary species. For events (dosing at time t), add an SBML event via Antimony: `at time > 10: Drug = 5`.

### Build a model from scratch (Antimony)

```python
import antimony, roadrunner
ant = """
model aggregation
  M -> O; k_nuc*M^2        # primary nucleation
  O + M -> F; k_plus*O*M   # elongation
  M = 5e-6; O = 0; F = 0
  k_nuc = 2e-5; k_plus = 3e6
end
"""
assert antimony.loadAntimonyString(ant) >= 0, antimony.getLastError()
rr = roadrunner.RoadRunner(antimony.getSBMLString("aggregation"))
```

Save the Antimony text to `/output/model.ant` and the SBML to `/output/model.xml` so the model is part of the provenance.

### Steady state and sensitivity

```python
rr.steadyState()
print(dict(zip(rr.getFloatingSpeciesIds(), rr.getFloatingSpeciesConcentrations())))
# Scaled control coefficients (metabolic control analysis):
print(rr.getScaledFluxControlCoefficientMatrix())
```

### COPASI via basico (parameter scans, estimation)

```python
from basico import load_model, run_time_course, set_parameters, get_parameters
m = load_model("/output/biomodels/BIOMD0000000462.xml")
print(get_parameters())
set_parameters("k_plus", initial_value=6e6)
tc = run_time_course(duration=100, intervals=500)
tc.to_csv("/output/sim_basico.csv")
```

## Reporting

- Always state the model ID and its source publication, the engine (`roadrunner` version via `roadrunner.__version__`), the perturbation exactly (parameter, original value, new value), and the integration window.
- Report quantitative effects (fold change, time-to-peak, steady-state shift) with the baseline alongside.
- If the model's parameter values came from a paper, say so; if you changed them to fit the question, say that too.
- Figures: one plot per perturbation family, labelled axes with units from the model.

## Limits of the sandbox

- ODE models of a few hundred species run in seconds. Stochastic replicates, agent-based models (CompuCell3D), spatial PDEs and parameter estimation over many conditions do not fit; estimate the cost and record it instead of running it.
- The executor has limited CPU (often 0.5–2 cores) and each call is bounded by its timeout; a timed-out call returns an error and no partial results, so save intermediate CSVs early in long loops.
