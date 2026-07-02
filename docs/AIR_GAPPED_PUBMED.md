# Local PubMed Mirror (Air-Gapped Mode)

In normal deployments, `search_pubmed` hits the public NCBI eutils API
(`literature.py`'s `PUBMED_BASE_URL` default). Air-gapped deployments have no
outbound internet, so `docker-compose.yml` defines a local `pubmed-mock` /
`pubmed-mirror` service that speaks the same eutils API on the internal
Docker network. Neither service is started by a plain `docker compose up` —
both are gated behind Compose profiles, and you must pick one.

## Which flavour do I want?

| | `airgap-mock` | `airgap-mirror` |
|---|---|---|
| Data | ~400 hand-picked abstracts baked into the image | full ~40M-article baseline, FTS5-ranked |
| Size | trivial | ~96 GB SQLite index, lives on the host |
| Extra checkout needed | no | yes — sibling repo `openscientist-pubmed-mirror` |
| Use for | CI smoke tests, quick local checks | real research-quality air-gapped runs |

Both expose port 9000 under the same Compose network alias `pubmed-mock`, so
`PUBMED_BASE_URL=http://pubmed-mock:9000/entrez/eutils` works unchanged for
either — only the profile and backing data differ.

## Option A: `airgap-mock` (fast path)

No extra host setup required.

```bash
COMPOSE_PROFILES=airgap-mock make build
COMPOSE_PROFILES=airgap-mock docker compose up -d pubmed-mock
```

## Option B: `airgap-mirror` (full corpus)

1. Check out [`openscientist-pubmed-mirror`](https://github.com/openscientist-io/openscientist-pubmed-mirror)
   as a sibling of this repo (or point `OPENSCIENTIST_PUBMED_MIRROR_REPO` at
   wherever you cloned it):

   ```bash
   git clone https://github.com/openscientist-io/openscientist-pubmed-mirror.git ../openscientist-pubmed-mirror
   cd ../openscientist-pubmed-mirror
   uv sync
   ```

2. Download the full NCBI baseline and build the SQLite + FTS5 index. The
   2026 baseline is 1,340 files; `LAST` must be set explicitly or you'll
   only get file 1 (the `download`/`build-index` Makefile targets both
   default to `FIRST=1 LAST=1`):

   ```bash
   make download LAST=1340 DATA_DIR=/Volumes/PubMed/data
   make build-index LAST=1340 DATA_DIR=/Volumes/PubMed/data DB=/Volumes/PubMed/pubmed.sqlite
   make verify DB=/Volumes/PubMed/pubmed.sqlite
   ```

   This is a ~50 GB download and takes several hours (the mirror repo's own
   README cites ~23h on a laptop for the full build step). For a quick
   functional test instead of the full corpus, run `make smoke` (~1 baseline
   file, ~2 min, produces `smoke.sqlite`) and point `OPENSCIENTIST_PUBMED_DB`
   at that instead — enough to exercise the whole air-gapped path without
   the multi-hour wait.

3. Set `OPENSCIENTIST_PUBMED_DB` in `.env` to the **absolute host path** of
   the resulting `pubmed.sqlite` (or `smoke.sqlite`) file (see `.env.example`).
4. Build and start with the `airgap-mirror` profile:

   ```bash
   COMPOSE_PROFILES=airgap-mirror make build
   COMPOSE_PROFILES=airgap-mirror docker compose up -d pubmed-mirror
   ```

## Gotcha: `required variable OPENSCIENTIST_PUBMED_DB is missing a value`

You will hit this error on `make build` / `docker compose build` even if you
only want `airgap-mock` (or aren't using air-gapped mode at all):

```
error while interpolating services.pubmed-mirror.volumes.[]: required variable
OPENSCIENTIST_PUBMED_DB is missing a value: Set OPENSCIENTIST_PUBMED_DB to the
host path of pubmed.sqlite
```

Docker Compose interpolates `${VAR:?err}` for every service in the file
during config parsing, **before** it filters by active profile — so a
required var on a profile-gated service (`pubmed-mirror`) blocks the build
even when that profile isn't selected. This is a known Compose limitation,
not something specific to this repo.

- If you're using `airgap-mirror`: set `OPENSCIENTIST_PUBMED_DB` for real,
  per Option B above.
- If you're not using `airgap-mirror` (including `airgap-mock` or non-airgap
  builds): set any placeholder value to satisfy interpolation, e.g.
  `OPENSCIENTIST_PUBMED_DB=/dev/null` in `.env`. The `pubmed-mirror` service
  still won't start unless you activate its profile.

## Verifying it's working

```bash
docker compose exec openscientist curl -s http://pubmed-mock:9000/health
```

and from inside an agent job container, confirm `search_pubmed` results come
back with no outbound network call (see `docs/AIR_GAPPED_MODE_RFC.md` §14 for
the fuller egress-verification procedure).
