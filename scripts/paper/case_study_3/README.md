# Case Study 3 scoring and figure scripts (Nature Medicine manuscript)

Scripts used for the Case Study 3 reproducibility experiments in the OpenScientist manuscript
(Supplementary Tables 5 and 6, Extended Data Fig. 2).

- `ksab_score.py` scores a set of OpenScientist runs: criteria C1-C3 in iteration 1, findings per
  iteration, first V-ATPase/ATP6V1H finding, formal donor-level test, and distinct dataset genes named
  in findings. Usage: `uv run --with h5py python -I ksab_score.py <h5ad> <job_dir>...`
- `exp4_plot.py` draws cumulative findings and distinct dataset genes per iteration for the control
  runs (Extended Data Fig. 2). Usage: `uv run --with h5py,numpy,matplotlib python -I exp4_plot.py <ids_dir> <jobs_dir> <out_dir>`
  where `<ids_dir>` contains `ksab_control_ids.txt` etc. (one job ID per line).

Run archives (knowledge_state.json per job) are on Zenodo (10.5281/zenodo.18852838).

## Tokeniser note (2025-11-05)

A gene is counted as "named in findings" when a token of the finding text (title, evidence,
biological_interpretation) equals a gene symbol in the dataset vocabulary (`var/feature_name` of the
h5ad). The original regex kept hyphens inside tokens, so hyphen-joined names such as
`DDIT3-GABARAPL2` or `PSMA1-7` were never matched and gene counts were undercounted by about 1.5%.
The regex here splits on non-alphanumeric characters; the manuscript's Supplementary Tables 5-6 and
Extended Data Fig. 2 were regenerated with it. Findings counts are unaffected.
