# OpenScientist

An autonomous AI scientist that generates and tests hypotheses from scientific data. Give it a research question and, optionally, data files; it explores the data, searches the literature, runs analyses and writes a report of its findings.

- **Live site:** [www.openscientist.io](https://www.openscientist.io)
- **Documentation:** [docs.openscientist.io](https://docs.openscientist.io)
- **Paper:** [medRxiv preprint](https://www.medrxiv.org/content/10.64898/2026.03.15.26348338v1)

## Quick start

Requires Docker, [uv](https://docs.astral.sh/uv/) and credentials for a [model provider](https://docs.openscientist.io/getting-started/#model-providers).

```bash
git clone https://github.com/openscientist-io/openscientist.git
cd openscientist
uv run openscientist
```

The first run starts a setup wizard, then builds and starts the stack at `http://localhost:8080`. See [Getting started](https://docs.openscientist.io/getting-started/) for details.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).

## License

See [LICENSE](LICENSE).
