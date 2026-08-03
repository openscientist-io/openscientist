---
name: simulation
description: Mechanistic simulation strategies - kinetic (ODE/stochastic), constraint-based (FBA), and model reuse
category: domain
---

# Mechanistic Simulation

## When to Use This Skill

- When a hypothesis is about a **mechanism** rather than a correlation
- When asked to predict system behaviour under a perturbation (knockout,
  dose change, altered initial condition) that was not measured
- When fitting kinetic parameters to time-course data
- When reasoning about metabolic flux distributions
- When a published model (SBML) should be reproduced or extended

**Do not reach for simulation when a statistical test answers the question.**
A simulation asserts a mechanism; it is a much stronger claim than a fitted
curve, and it must be justified.

## Choosing the Right Tool

| Question shape | Tool | Why |
| --- | --- | --- |
| Time evolution of known reaction network | `basico` or `roadrunner` | ODE / stochastic integration |
| Steady state, parameter scan, sensitivity | `basico` | COPASI has these as first-class tasks |
| Fit rate constants to time-course data | `basico` | Built-in parameter estimation |
| Run an existing SBML model, many times, fast | `roadrunner` | Compiled C++ core |
| Genome-scale metabolism, gene knockouts | **not available** | See "Constraint-Based Modelling" below |

**Rule of thumb:** if the model has explicit rate constants, it is kinetic
(`basico`/`roadrunner`) and is supported. If it is a genome-scale
stoichiometric network with no kinetics, it is constraint-based, and the
tooling for it is not currently installed.

## Kinetic Modelling with basico

`basico` accepts readable reaction strings — prefer this over authoring raw
SBML XML, which is verbose and error-prone.

```python
import basico

basico.new_model(name="michaelis_menten")
basico.add_reaction("binding", "E + S -> ES")
basico.add_reaction("catalysis", "ES -> E + P")

basico.set_species("S", initial_concentration=10.0)
basico.set_species("E", initial_concentration=1.0)
basico.set_reaction_parameters("(binding).k1", value=0.3)

tc = basico.run_time_course(duration=10, intervals=100)   # returns a DataFrame
steady = basico.run_steadystate()                          # 2 == found
```

`run_time_course` returns a pandas DataFrame indexed by time, so it composes
directly with the plotting and statistics libraries already in the executor.

### ⚠️ Species default to concentration 1.0, not 0.0

Every species you do not explicitly initialise starts at **1.0**. This
silently offsets mass-balance checks:

```python
basico.add_reaction("r", "A -> B")
basico.set_species("A", initial_concentration=10.0)
# B was never set, so B(0) == 1.0 and total mass is 11.0, not 10.0
```

**Always set the initial concentration of every species explicitly**, including
the ones that start empty.

## Fast SBML Simulation with roadrunner

For an existing SBML model, or when the same model is simulated many times
(parameter sweeps, fitting loops, Monte Carlo):

```python
import roadrunner

rr = roadrunner.RoadRunner("model.xml")     # path, URL, or SBML string
rr.timeCourseSelections = ["time", "A", "B"]
result = rr.simulate(0, 10, 101)            # numpy array

rr.reset()                                  # REQUIRED before re-simulating
rr.setValue("k", 0.5)
result2 = rr.simulate(0, 10, 101)
```

### ⚠️ Call `rr.reset()` between runs

`roadrunner` carries state across `simulate()` calls. In a parameter sweep,
forgetting `reset()` means each run silently starts from the previous run's
final state, producing a plausible but meaningless sweep.

## Constraint-Based Modelling (FBA) — not currently available

**`cobra` is not installed, and `pip install cobra` will not work.** The
current release caps `pandas<3.0`, which conflicts with this image's
`pandas>=3.0` pin. Installing it unpinned resolves to a 2020 release that
then fails at import on numpy 2.

If a research question needs flux balance analysis — genome-scale metabolism,
gene knockout growth phenotypes, flux variability — **say so explicitly and
stop.** Do not substitute a kinetic model for a constraint-based one: they
answer different questions, and a hand-built ODE model of a genome-scale
network is not a valid stand-in for FBA.

Note also that FBA predicts *flux*, not *concentration*, so it would not in
any case answer questions about metabolite pool sizes; see the metabolomics
skill for that distinction.

## Validating Any Simulation

A simulation that runs is not a simulation that is correct. Before drawing a
conclusion from one:

1. **Check against an analytic case.** First-order decay `A -> B` with rate
   `k` must give `A(t) = A(0)·e^(-kt)`. Both `basico` and `roadrunner`
   reproduce this exactly; a solver that does not is misconfigured.
2. **Check conservation.** Closed systems must conserve mass. If total
   species count drifts, the tolerance is too loose or the model is wrong.
3. **Check timestep independence.** Halve the output interval and integration
   tolerance; the trajectory must not move. If it does, the result is a
   numerical artefact.
4. **State the units.** COPASI and SBML both carry explicit units. Report
   them with every number — an unlabelled rate constant is uninterpretable.
5. **Report initial conditions and parameters** alongside results, so the run
   can be reproduced.

### ⚠️ Fixed-timestep integration is not free

If you hand-roll an integrator (Euler steps in a loop), a first-order decay
with `k=0.3`, `dt=1`, `t=10` gives 0.282 where the true answer is 0.498 — a
44% error that looks entirely plausible. Use the solvers above, which are
adaptive, rather than stepping a model manually.

## Model Reuse

Published kinetic models are available as SBML from BioModels
(`https://www.ebi.ac.uk/biomodels/`). Both `roadrunner` and `basico` load
SBML directly, so a published model can usually be simulated without
reimplementation. Prefer reusing a curated published model over rebuilding
one from the paper's equations.

## What Is Not Installed

- **`cobra` / FBA** — see above. Blocked on a pandas 3 conflict.
- **Multi-scale composition frameworks** (`vivarium-core`, `process-bigraph`)
  compose heterogeneous submodels — whole-cell, agent-based colonies — and are
  the right tool for that job, but they are not integrators and should not be
  used for ordinary kinetic simulation.
- **BioSimulators / COMBINE-OMEX tooling** targets standards-based
  reproducibility across ~20 engines rather than direct simulation.

If a research question genuinely requires one of these, say so explicitly
rather than approximating it with the tools above.
