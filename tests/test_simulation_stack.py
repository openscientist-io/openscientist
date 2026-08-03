"""
Tests for the simulation stack available to agent code in the executor container.

Contains declaration tests (fast, always run) and integration tests that execute
real simulations inside the executor image and pin their numeric answers against
known analytic results, so a dependency bump cannot silently change science.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from openscientist.skill_ingestion import SkillParser

PROJECT_ROOT = Path(__file__).parent.parent
EXECUTOR_REQUIREMENTS = PROJECT_ROOT / "requirements-executor.txt"
SIMULATION_SKILL = PROJECT_ROOT / "skills" / "domain" / "simulation" / "SKILL.md"

# Packages the simulation skill tells the agent to import. Each must be present
# in the executor image, or the skill sends the agent after a failing import.
SIMULATION_PACKAGES = ("libroadrunner", "copasi-basico")

# Packages deliberately excluded from the executor image. Asserted so that
# adding one becomes a conscious decision rather than an accident.
#   cobra      -- caps pandas<3.0, conflicts with the image's pandas>=3.0 pin
#   antimony   -- sdist-only for cp312 linux, adds a multi-minute C++ build
#   tellurium  -- same
#   phrasedml  -- same
EXCLUDED_PACKAGES = ("cobra", "antimony", "tellurium", "phrasedml")

# A0 * exp(-k*t) for A0=10, k=0.3, t=10 -> 0.497871
ANALYTIC_DECAY_AT_T10 = 0.497871
# Solvers agree with the analytic result to ~1e-4, not to machine precision:
# roadrunner returns 0.49785 and basico 0.49787 at default tolerances.
SOLVER_TOLERANCE = 1e-4


def _declared_packages() -> set[str]:
    """Package names declared in requirements-executor.txt, lowercased."""
    declared = set()
    for line in EXECUTOR_REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name = re.split(r"[<>=!~;\[]", stripped.split("#")[0].strip())[0]
        if name:
            declared.add(name.strip().lower())
    return declared


def _parse_marker(output: str, marker: str) -> float:
    """Pull a `MARKER=<float>` value out of captured container stdout."""
    match = re.search(rf"{marker}=(-?\d+\.?\d*)", output)
    assert match is not None, f"No {marker}= in output:\n{output}"
    return float(match.group(1))


class TestSimulationDeclarations:
    """Fast checks on what the executor image declares. No Docker required."""

    def test_simulation_packages_declared(self):
        """Executor requirements declare every package the skill relies on."""
        declared = _declared_packages()

        for package in SIMULATION_PACKAGES:
            assert package.lower() in declared, (
                f"{package} is referenced by the simulation skill but is not "
                f"declared in requirements-executor.txt"
            )

    def test_excluded_packages_not_declared(self):
        """Known-incompatible simulation packages stay out of the image."""
        declared = _declared_packages()

        for package in EXCLUDED_PACKAGES:
            assert package.lower() not in declared, (
                f"{package} is excluded from the executor image on purpose. "
                f"See the comments in requirements-executor.txt before adding it."
            )

    def test_simulation_skill_parses(self):
        """The simulation skill is valid and ingestible by the real parser."""
        assert SIMULATION_SKILL.exists(), f"Missing skill file: {SIMULATION_SKILL}"

        parsed = SkillParser().parse_file(SIMULATION_SKILL)

        assert parsed.name == "simulation"
        assert parsed.category == "domain"
        assert parsed.description

    def test_simulation_skill_documents_installed_packages(self):
        """The skill names the tools it tells the agent to import."""
        content = SIMULATION_SKILL.read_text(encoding="utf-8")

        for import_name in ("basico", "roadrunner"):
            assert import_name in content, f"Simulation skill does not mention {import_name}"

    def test_simulation_skill_declares_fba_unavailable(self):
        """The skill tells the agent FBA is unavailable rather than staying silent.

        Without this the agent will try `import cobra`, fail, and is likely to
        substitute a kinetic model for a constraint-based question.
        """
        content = SIMULATION_SKILL.read_text(encoding="utf-8")

        assert "cobra" in content, "Skill must name cobra to explain its absence"
        assert "not" in content.lower() and "available" in content.lower()


class TestCobraPandasConflict:
    """Guards the reason cobra is excluded, so we notice when it is fixed."""

    def test_cobra_still_conflicts_with_pandas_3(self):
        """cobra still caps pandas<3.0, so it still cannot enter the image.

        This fails once cobra supports pandas 3 -- at which point add
        `cobra>=<new version>` to requirements-executor.txt, drop cobra from
        EXCLUDED_PACKAGES, and restore the FBA section of the simulation skill.
        """
        if shutil.which("uv") is None:
            pytest.skip("uv not available")

        with tempfile.TemporaryDirectory() as tmpdir:
            requirement = Path(tmpdir) / "req.txt"
            requirement.write_text("cobra>=0.31\n")
            constraint = Path(tmpdir) / "constraint.txt"
            constraint.write_text("pandas>=3.0.0\nnumpy>=2.2,<2.4\n")

            try:
                result = subprocess.run(
                    [
                        "uv",
                        "pip",
                        "compile",
                        str(requirement),
                        "-c",
                        str(constraint),
                        "--python-version",
                        "3.12",
                        "-o",
                        "/dev/null",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            except (subprocess.TimeoutExpired, OSError):
                pytest.skip("Could not resolve dependencies (offline?)")

        if result.returncode == 0:
            pytest.fail(
                "cobra>=0.31 now resolves against pandas>=3.0. The FBA blocker "
                "is gone -- add cobra to requirements-executor.txt, remove it "
                "from EXCLUDED_PACKAGES, and restore the FBA guidance in "
                "skills/domain/simulation/SKILL.md."
            )

        assert "pandas" in result.stderr, (
            f"cobra failed to resolve for an unexpected reason:\n{result.stderr}"
        )


@pytest.mark.integration
class TestSimulationStackIntegration:
    """Run real simulations in the executor container and pin the answers.

    Run with: pytest tests/test_simulation_stack.py -v -m integration
    """

    @pytest.fixture(autouse=True)
    def check_docker(self):
        """Skip if Docker is not available."""
        from openscientist.container_manager import ContainerManager

        if not ContainerManager().is_available():
            pytest.skip("Docker not available")

    @pytest.fixture(autouse=True)
    def check_image(self):
        """Skip if executor image is not built."""
        from openscientist.container_manager import ContainerManager

        if not ContainerManager().check_image_available():
            pytest.skip("Executor image not built. Run 'make build-executor' first.")

    def _run(self, code: str, job_id: str) -> dict:
        from openscientist.container_manager import ContainerManager

        with tempfile.TemporaryDirectory() as tmpdir:
            return ContainerManager().execute_code(code=code, job_id=job_id, output_dir=tmpdir)

    def test_roadrunner_matches_analytic_decay(self):
        """roadrunner reproduces A(t) = A0 * exp(-k*t) for first-order decay."""
        code = '''
import roadrunner

SBML = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level3/version1/core" level="3" version="1">
 <model id="decay">
  <listOfCompartments><compartment id="c" size="1" constant="true"/></listOfCompartments>
  <listOfSpecies>
   <species id="A" compartment="c" initialConcentration="10" hasOnlySubstanceUnits="false"
            boundaryCondition="false" constant="false"/>
   <species id="B" compartment="c" initialConcentration="0" hasOnlySubstanceUnits="false"
            boundaryCondition="false" constant="false"/>
  </listOfSpecies>
  <listOfParameters><parameter id="k" value="0.3" constant="true"/></listOfParameters>
  <listOfReactions><reaction id="r" reversible="false">
   <listOfReactants>
     <speciesReference species="A" stoichiometry="1" constant="true"/>
   </listOfReactants>
   <listOfProducts>
     <speciesReference species="B" stoichiometry="1" constant="true"/>
   </listOfProducts>
   <kineticLaw><math xmlns="http://www.w3.org/1998/Math/MathML">
     <apply><times/><ci>k</ci><ci>A</ci><ci>c</ci></apply></math></kineticLaw>
  </reaction></listOfReactions>
 </model></sbml>"""

rr = roadrunner.RoadRunner(SBML)
result = rr.simulate(0, 10, 11)
print("A_FINAL=%.6f" % result[-1, 1])
print("MASS=%.6f" % (result[-1, 1] + result[-1, 2]))
'''
        result = self._run(code, "sim-roadrunner-decay")

        assert result["success"] is True, result.get("output")
        output = result["output"]
        assert _parse_marker(output, "A_FINAL") == pytest.approx(
            ANALYTIC_DECAY_AT_T10, abs=SOLVER_TOLERANCE
        )
        # Closed system: A + B is conserved at the initial 10.0
        assert _parse_marker(output, "MASS") == pytest.approx(10.0, abs=1e-4)

    def test_basico_matches_analytic_decay(self):
        """basico reproduces the same analytic decay as roadrunner."""
        code = """
import basico

basico.new_model(name="decay")
basico.add_reaction("r", "A -> B")
basico.set_species("A", initial_concentration=10.0)
basico.set_species("B", initial_concentration=0.0)
basico.set_reaction_parameters("(r).k1", value=0.3)

tc = basico.run_time_course(duration=10, intervals=10)
print("A_FINAL=%.6f" % tc["A"].iloc[-1])
print("MASS=%.6f" % (tc["A"].iloc[-1] + tc["B"].iloc[-1]))
"""
        result = self._run(code, "sim-basico-decay")

        assert result["success"] is True, result.get("output")
        output = result["output"]
        assert _parse_marker(output, "A_FINAL") == pytest.approx(
            ANALYTIC_DECAY_AT_T10, abs=SOLVER_TOLERANCE
        )
        assert _parse_marker(output, "MASS") == pytest.approx(10.0, abs=1e-4)

    def test_basico_steady_state_is_reachable(self):
        """basico can run the steady-state task, not just time courses."""
        code = """
import basico

basico.new_model(name="decay")
basico.add_reaction("r", "A -> B")
basico.set_species("A", initial_concentration=10.0)
basico.set_species("B", initial_concentration=0.0)
basico.set_reaction_parameters("(r).k1", value=0.3)

print("STATUS=%d" % basico.run_steadystate())
"""
        result = self._run(code, "sim-basico-steadystate")

        assert result["success"] is True, result.get("output")
        # COPASI returns 2 when a steady state was found
        assert _parse_marker(result["output"], "STATUS") == pytest.approx(2)
