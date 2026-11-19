# OpenScientist

An autonomous AI scientist that generates and tests hypotheses from scientific data. Give it a research question and, optionally, data files; it explores the data, searches the literature, runs analyses and writes a report of its findings.

- **Live site:** [www.openscientist.io](https://www.openscientist.io)
- **Documentation:** [docs.openscientist.io](https://docs.openscientist.io)
- **Paper:** [Nature Medicine](https://www.nature.com/articles/s41591-026-04808-6)

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
