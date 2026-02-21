"""Tests for code_executor module."""

import shutil
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from shandy.code_executor import (
    ForbiddenImportError,
    execute_code,
    execute_rust_code,
    execute_sparql_code,
    format_execution_result,
    validate_imports,
)

rustc_available = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")

# ─── validate_imports ─────────────────────────────────────────────────


class TestValidateImports:
    """Tests for AST-based import validation."""

    def test_allowed_import(self):
        validate_imports("import pandas", ["pandas", "numpy"])

    def test_allowed_from_import(self):
        validate_imports("from scipy.stats import ttest_ind", ["scipy"])

    def test_forbidden_import_raises(self):
        with pytest.raises(ForbiddenImportError, match="not allowed"):
            validate_imports("import subprocess", ["pandas"])

    def test_forbidden_from_import_raises(self):
        with pytest.raises(ForbiddenImportError, match="not allowed"):
            validate_imports("from os.path import join", ["pandas"])

    def test_syntax_error_raises(self):
        with pytest.raises(SyntaxError):
            validate_imports("def f(\n", ["pandas"])

    def test_multiple_imports(self):
        code = "import pandas\nimport numpy\nimport scipy"
        validate_imports(code, ["pandas", "numpy", "scipy"])

    def test_partial_module_name_checked_at_top_level(self):
        # "from scipy.stats import ttest_ind" → top-level is "scipy"
        validate_imports("from scipy.stats import ttest_ind", ["scipy"])

    def test_os_allowed_when_listed(self):
        validate_imports("import os", ["os"])

    def test_os_forbidden_when_not_listed(self):
        with pytest.raises(ForbiddenImportError):
            validate_imports("import os", ["pandas"])


# ─── execute_code ─────────────────────────────────────────────────────


class TestExecuteCode:
    """Tests for sandboxed code execution."""

    @pytest.fixture
    def plots_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "plots"
        d.mkdir()
        return d

    def test_successful_print(self, plots_dir):
        result = execute_code("print('hello')", data=None, plots_dir=plots_dir)
        assert result["success"] is True
        assert "hello" in result["output"]

    def test_data_available_in_namespace(self, plots_dir):
        df = pd.DataFrame({"x": [1, 2, 3]})
        result = execute_code("print(data.shape)", data=df, plots_dir=plots_dir)
        assert result["success"] is True
        assert "(3, 1)" in result["output"]

    def test_forbidden_import_fails(self, plots_dir):
        result = execute_code("import subprocess", data=None, plots_dir=plots_dir)
        assert result["success"] is False
        assert "not allowed" in result["error"]

    def test_syntax_error_fails(self, plots_dir):
        result = execute_code("def f(\n", data=None, plots_dir=plots_dir)
        assert result["success"] is False
        assert "Syntax" in result["error"] or "syntax" in result["error"].lower()

    def test_runtime_error_captured(self, plots_dir):
        result = execute_code("x = 1/0", data=None, plots_dir=plots_dir)
        assert result["success"] is False
        assert "ZeroDivisionError" in result["error"]

    def test_numpy_available(self, plots_dir):
        result = execute_code(
            "import numpy as np; print(np.array([1,2,3]).sum())",
            data=None,
            plots_dir=plots_dir,
        )
        assert result["success"] is True
        assert "6" in result["output"]

    def test_execution_time_tracked(self, plots_dir):
        result = execute_code("x = sum(range(1000))", data=None, plots_dir=plots_dir)
        assert result["success"] is True
        assert result["execution_time"] >= 0.0

    def test_plot_show_saves_file(self, plots_dir):
        code = """
import matplotlib.pyplot as plt
plt.figure()
plt.plot([1,2,3])
plt.show()
"""
        result = execute_code(code, data=None, plots_dir=plots_dir)
        assert result["success"] is True
        png_files = list(plots_dir.glob("plot_*.png"))
        assert len(png_files) >= 1

    def test_plot_savefig_redirected(self, plots_dir):
        code = """
import matplotlib.pyplot as plt
plt.figure()
plt.plot([1,2,3])
plt.savefig('custom.png')
"""
        result = execute_code(code, data=None, plots_dir=plots_dir)
        assert result["success"] is True
        # savefig should redirect to plots_dir
        assert (plots_dir / "custom.png").exists()

    def test_plot_metadata_saved(self, plots_dir):
        code = """
import matplotlib.pyplot as plt
plt.figure()
plt.plot([1,2,3])
plt.show()
"""
        result = execute_code(
            code,
            data=None,
            plots_dir=plots_dir,
            description="Test plot",
            iteration=3,
        )
        assert result["success"] is True
        json_files = list(plots_dir.glob("plot_*.json"))
        assert len(json_files) >= 1

    def test_data_files_available(self, plots_dir):
        files = [{"path": "/tmp/file.csv", "name": "file.csv", "file_type": "tabular"}]
        code = "print(len(data_files))"
        result = execute_code(code, data=None, plots_dir=plots_dir, data_files=files)
        assert result["success"] is True
        assert "1" in result["output"]

    def test_plot_counter_respects_existing_plots(self, plots_dir):
        """New plots should not overwrite existing ones."""
        # Create an existing plot
        (plots_dir / "plot_5.png").write_bytes(b"fake png")

        code = """
import matplotlib.pyplot as plt
plt.figure()
plt.plot([1,2])
plt.show()
"""
        result = execute_code(code, data=None, plots_dir=plots_dir)
        assert result["success"] is True
        # New plot should be plot_6.png, not plot_1.png
        assert (plots_dir / "plot_6.png").exists()


# ─── execute_rust_code ────────────────────────────────────────────────


class TestExecuteRustCode:
    """Tests for Rust code compilation and execution."""

    @pytest.fixture
    def plots_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "plots"
        d.mkdir()
        return d

    def test_rustc_not_found_returns_error(self, plots_dir):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = execute_rust_code('fn main() { println!("hi"); }', plots_dir)
        assert result["success"] is False
        assert "not found" in result["error"]
        assert result["plots"] == []

    @rustc_available
    def test_hello_world(self, plots_dir):
        code = 'fn main() { println!("hello from rust"); }'
        result = execute_rust_code(code, plots_dir)
        assert result["success"] is True
        assert "hello from rust" in result["output"]
        assert result["plots"] == []

    @rustc_available
    def test_compilation_error_captured(self, plots_dir):
        result = execute_rust_code("fn main() { this is not rust }", plots_dir)
        assert result["success"] is False
        assert "Compilation error" in result["error"]

    @rustc_available
    def test_runtime_exit_code_nonzero(self, plots_dir):
        code = "fn main() { std::process::exit(1); }"
        result = execute_rust_code(code, plots_dir)
        assert result["success"] is False
        assert "exit" in result["error"].lower() or "1" in result["error"]

    @rustc_available
    def test_execution_time_tracked(self, plots_dir):
        code = 'fn main() { println!("done"); }'
        result = execute_rust_code(code, plots_dir)
        assert result["success"] is True
        assert result["execution_time"] >= 0.0

    @rustc_available
    def test_no_plots_produced(self, plots_dir):
        code = 'fn main() { println!("no plots here"); }'
        result = execute_rust_code(code, plots_dir)
        assert result["plots"] == []


# ─── execute_sparql_code ──────────────────────────────────────────────


class TestExecuteSparqlCode:
    """Tests for SPARQL query execution against remote endpoints."""

    @pytest.fixture
    def plots_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "plots"
        d.mkdir()
        return d

    def test_missing_endpoint_returns_error(self, plots_dir):
        query = "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1"
        result = execute_sparql_code(query, plots_dir)
        assert result["success"] is False
        assert "ENDPOINT" in result["error"]
        assert result["plots"] == []

    def test_endpoint_parsed_case_insensitive(self, plots_dir):
        """# endpoint: (lowercase) should also be accepted."""
        sparql_json = {"head": {"vars": ["s"]}, "results": {"bindings": []}}
        query = "# endpoint: https://example.org/sparql\nSELECT ?s WHERE { ?s ?p ?o }"
        with patch("SPARQLWrapper.SPARQLWrapper") as mock_cls:
            mock_instance = mock_cls.return_value
            mock_instance.query.return_value.convert.return_value = sparql_json
            result = execute_sparql_code(query, plots_dir)
        assert result["success"] is True

    def test_zero_results_message(self, plots_dir):
        sparql_json = {"head": {"vars": ["item"]}, "results": {"bindings": []}}
        query = "# ENDPOINT: https://example.org/sparql\nSELECT ?item WHERE { }"
        with patch("SPARQLWrapper.SPARQLWrapper") as mock_cls:
            mock_instance = mock_cls.return_value
            mock_instance.query.return_value.convert.return_value = sparql_json
            result = execute_sparql_code(query, plots_dir)
        assert result["success"] is True
        assert "0 results" in result["output"]

    def test_results_formatted_as_table(self, plots_dir):
        sparql_json = {
            "head": {"vars": ["name", "value"]},
            "results": {
                "bindings": [
                    {"name": {"value": "Alice"}, "value": {"value": "42"}},
                    {"name": {"value": "Bob"}, "value": {"value": "7"}},
                ]
            },
        }
        query = "# ENDPOINT: https://example.org/sparql\nSELECT ?name ?value WHERE { }"
        with patch("SPARQLWrapper.SPARQLWrapper") as mock_cls:
            mock_instance = mock_cls.return_value
            mock_instance.query.return_value.convert.return_value = sparql_json
            result = execute_sparql_code(query, plots_dir)
        assert result["success"] is True
        assert "Alice" in result["output"]
        assert "Bob" in result["output"]
        assert "2 result(s)" in result["output"]
        assert result["plots"] == []

    def test_sparqlwrapper_exception_returns_error(self, plots_dir):
        from SPARQLWrapper.SPARQLExceptions import SPARQLWrapperException

        query = "# ENDPOINT: https://example.org/sparql\nSELECT ?s WHERE { }"
        with patch("SPARQLWrapper.SPARQLWrapper") as mock_cls:
            mock_instance = mock_cls.return_value
            mock_instance.query.side_effect = SPARQLWrapperException("bad query")
            result = execute_sparql_code(query, plots_dir)
        assert result["success"] is False
        assert "SPARQL query error" in result["error"]

    def test_execution_time_tracked(self, plots_dir):
        sparql_json = {"head": {"vars": []}, "results": {"bindings": []}}
        query = "# ENDPOINT: https://example.org/sparql\nSELECT * WHERE { }"
        with patch("SPARQLWrapper.SPARQLWrapper") as mock_cls:
            mock_instance = mock_cls.return_value
            mock_instance.query.return_value.convert.return_value = sparql_json
            result = execute_sparql_code(query, plots_dir)
        assert result["execution_time"] >= 0.0


# ─── format_execution_result ──────────────────────────────────────────


class TestFormatExecutionResult:
    """Tests for result formatting."""

    def test_success_format(self):
        result = {
            "success": True,
            "output": "hello world",
            "plots": ["/tmp/plot_1.png"],
            "execution_time": 1.23,
        }
        text = format_execution_result(result)
        assert "successfully" in text
        assert "hello world" in text
        assert "1 plot" in text
        assert "1.23" in text

    def test_failure_format(self):
        result = {
            "success": False,
            "error": "ZeroDivisionError: division by zero",
            "output": "",
            "plots": [],
        }
        text = format_execution_result(result)
        assert "failed" in text
        assert "ZeroDivisionError" in text

    def test_failure_with_partial_output(self):
        result = {
            "success": False,
            "error": "TimeoutError",
            "output": "partial output",
            "plots": [],
            "traceback": "Traceback ...",
        }
        text = format_execution_result(result)
        assert "partial output" in text
        assert "Traceback" in text
