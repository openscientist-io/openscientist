"""Tests for code_executor module."""

import shutil
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call, patch

import matplotlib.pyplot as plt
import pandas as pd
import pytest

from openscientist.code_executor import (
    SPARQL_USER_AGENT,
    ForbiddenImportError,
    execute_code,
    execute_rust_code,
    execute_sparql_code,
    format_execution_result,
    load_data,
    validate_imports,
)

cargo_available = pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")

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

    def test_os_forbidden_when_not_listed(self):
        with pytest.raises(ForbiddenImportError):
            validate_imports("import os", ["pandas"])

    def test_requests_forbidden(self):
        with pytest.raises(ForbiddenImportError):
            validate_imports("import requests", ["pandas", "numpy"])


# ─── restricted sandbox ───────────────────────────────────────────────


class TestRestrictedExecution:
    """Tests for hardened sandbox builtins, imports, and SafeOs."""

    @pytest.fixture
    def plots_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "plots"
        d.mkdir()
        return d

    def test_import_os_blocked(self, plots_dir):
        result = execute_code("import os", data=None, plots_dir=plots_dir)
        assert result["success"] is False
        assert "not allowed" in result["error"]

    def test_import_requests_blocked(self, plots_dir):
        result = execute_code("import requests", data=None, plots_dir=plots_dir)
        assert result["success"] is False
        assert "not allowed" in result["error"]

    def test_dunder_import_os_blocked(self, plots_dir):
        result = execute_code('__import__("os")', data=None, plots_dir=plots_dir)
        assert result["success"] is False
        assert "not allowed" in result["error"]

    def test_os_environ_unavailable(self, plots_dir):
        code = """
try:
    os.environ
    print('available')
except AttributeError:
    print('unavailable')
"""
        result = execute_code(code, data=None, plots_dir=plots_dir)
        assert result["success"] is True
        assert "unavailable" in result["output"]

    def test_os_system_unavailable(self, plots_dir):
        code = """
try:
    os.system('echo hi')
    print('available')
except AttributeError:
    print('unavailable')
"""
        result = execute_code(code, data=None, plots_dir=plots_dir)
        assert result["success"] is True
        assert "unavailable" in result["output"]

    def test_open_blocked(self, plots_dir):
        result = execute_code("open('test.txt')", data=None, plots_dir=plots_dir)
        assert result["success"] is False
        assert "NameError" in result["error"]

    def test_eval_blocked(self, plots_dir):
        result = execute_code("eval('1+1')", data=None, plots_dir=plots_dir)
        assert result["success"] is False
        assert "NameError" in result["error"]

    def test_exec_blocked(self, plots_dir):
        result = execute_code("exec('x=1')", data=None, plots_dir=plots_dir)
        assert result["success"] is False
        assert "NameError" in result["error"]

    def test_compile_blocked(self, plots_dir):
        result = execute_code("compile('x=1', '<s>', 'exec')", data=None, plots_dir=plots_dir)
        assert result["success"] is False
        assert "NameError" in result["error"]

    def test_safe_os_path_helpers_work(self, plots_dir):
        code = """
print(os.path.join('a', 'b'))
print(os.path.basename('/tmp/foo.txt'))
print(os.path.dirname('/tmp/foo.txt'))
print(os.path.splitext('a.csv')[1])
print(os.path.exists('.'))
print(os.path.isfile('.'))
print(os.path.isdir('.'))
print(os.sep)
print(isinstance(os.getcwd(), str))
print(isinstance(os.listdir('.'), list))
"""
        result = execute_code(code, data=None, plots_dir=plots_dir)
        assert result["success"] is True
        assert "a/b" in result["output"] or "a\\b" in result["output"]
        assert "foo.txt" in result["output"]
        assert ".csv" in result["output"]
        assert "True" in result["output"]

    def test_safe_path_no_dict(self, plots_dir):
        code = """
try:
    os.path.__dict__
    print('available')
except AttributeError:
    print('unavailable')
"""
        result = execute_code(code, data=None, plots_dir=plots_dir)
        assert result["success"] is True
        assert "unavailable" in result["output"]

    def test_safe_path_no_real_os_module(self, plots_dir):
        code = "print(hasattr(os.path, 'os'))"
        result = execute_code(code, data=None, plots_dir=plots_dir)
        assert result["success"] is True
        assert "False" in result["output"]

    def test_numpy_still_works(self, plots_dir):
        code = "import numpy.linalg; print(numpy.linalg.norm([3, 4]))"
        result = execute_code(code, data=None, plots_dir=plots_dir)
        assert result["success"] is True
        assert "5.0" in result["output"]

    def test_pandas_still_works(self, plots_dir):
        code = "import pandas as pd; print(pd.DataFrame({'x': [1]}).shape)"
        result = execute_code(code, data=None, plots_dir=plots_dir)
        assert result["success"] is True
        assert "(1, 1)" in result["output"]

    def test_scipy_from_import_still_works(self, plots_dir):
        code = "from scipy import stats; print(hasattr(stats, 'ttest_ind'))"
        result = execute_code(code, data=None, plots_dir=plots_dir)
        assert result["success"] is True
        assert "True" in result["output"]

    def test_sklearn_from_import_still_works(self, plots_dir):
        code = "from sklearn.metrics import accuracy_score; print(accuracy_score([0,1],[0,1]))"
        result = execute_code(code, data=None, plots_dir=plots_dir)
        assert result["success"] is True
        assert "1.0" in result["output"]


# ─── signal alarm helpers ─────────────────────────────────────────────


class TestSignalAlarmHelpers:
    """Tests for cross-platform timeout alarm setup and teardown."""

    def test_set_and_clear_timeout_alarm_unix_like(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import openscientist.code_executor as code_executor
        from openscientist.code_executor import (
            _clear_timeout_alarm,
            _set_timeout_alarm,
            timeout_handler,
        )

        mock_alarm = Mock()
        mock_signal_fn = Mock()
        sigalrm = 14

        monkeypatch.setattr(code_executor, "_signal_alarm", mock_alarm)
        monkeypatch.setattr(code_executor.signal, "signal", mock_signal_fn)
        monkeypatch.setattr(code_executor.signal, "SIGALRM", sigalrm, raising=False)

        _set_timeout_alarm(10)

        mock_signal_fn.assert_called_once_with(sigalrm, timeout_handler)
        mock_alarm.assert_called_once_with(10)

        _clear_timeout_alarm()

        mock_alarm.assert_has_calls([call(10), call(0)])

    def test_set_and_clear_timeout_alarm_windows_like(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import openscientist.code_executor as code_executor
        from openscientist.code_executor import _clear_timeout_alarm, _set_timeout_alarm

        mock_signal_fn = Mock()

        monkeypatch.setattr(code_executor, "_signal_alarm", None)
        monkeypatch.setattr(code_executor.signal, "signal", mock_signal_fn)

        _set_timeout_alarm(10)
        _clear_timeout_alarm()

        mock_signal_fn.assert_not_called()


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

    def test_restores_matplotlib_hooks_after_execution(self, plots_dir):
        """execute_code should not leave global pyplot hooks patched."""
        original_show = plt.show
        original_savefig = plt.savefig

        result = execute_code("print('ok')", data=None, plots_dir=plots_dir)

        assert result["success"] is True
        assert plt.show is original_show
        assert plt.savefig is original_savefig


# ─── load_data ────────────────────────────────────────────────────────


class TestLoadData:
    """Tests for executor data loading helper."""

    def test_none_path_returns_none(self):
        assert load_data(None) is None

    def test_missing_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_data(str(tmp_path / "missing.csv"))

    def test_load_csv(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "data.csv"
        csv_path.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
        df = load_data(str(csv_path))
        assert df is not None
        assert list(df.columns) == ["a", "b"]
        assert len(df) == 2

    def test_load_tsv(self, tmp_path: Path) -> None:
        tsv_path = tmp_path / "data.tsv"
        tsv_path.write_text("a\tb\n1\t2\n", encoding="utf-8")
        df = load_data(str(tsv_path))
        assert df is not None
        assert list(df.columns) == ["a", "b"]
        assert len(df) == 1

    def test_csv_fallback_for_unknown_extension(self, tmp_path: Path) -> None:
        data_path = tmp_path / "data.custom"
        data_path.write_text("x,y\n5,6\n", encoding="utf-8")
        df = load_data(str(data_path))
        assert df is not None
        assert list(df.columns) == ["x", "y"]

    def test_h5ad_returns_none_instead_of_crashing(self, tmp_path: Path) -> None:
        h5ad_path = tmp_path / "data.h5ad"
        h5ad_path.write_bytes(b"\x89HDF\r\n\x1a\nnot really hdf5 but binary")
        assert load_data(str(h5ad_path)) is None

    def test_binary_unknown_extension_returns_none_not_raise(self, tmp_path: Path) -> None:
        data_path = tmp_path / "data.bin"
        data_path.write_bytes(bytes(range(256)))
        assert load_data(str(data_path)) is None


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

    @cargo_available
    def test_hello_world(self, plots_dir):
        code = 'fn main() { println!("hello from rust"); }'
        result = execute_rust_code(code, plots_dir)
        assert result["success"] is True
        assert "hello from rust" in result["output"]
        assert result["plots"] == []

    @cargo_available
    def test_compilation_error_captured(self, plots_dir):
        result = execute_rust_code("fn main() { this is not rust }", plots_dir)
        assert result["success"] is False
        assert result["error"] is not None
        # cargo outputs compiler diagnostics to stderr; the error or output should
        # contain something useful (e.g. rustc's "error[E...]" lines)
        combined = (result["error"] or "") + (result["output"] or "")
        assert "error" in combined.lower()

    @cargo_available
    def test_runtime_exit_code_nonzero(self, plots_dir):
        code = "fn main() { std::process::exit(1); }"
        result = execute_rust_code(code, plots_dir)
        assert result["success"] is False
        assert "exit" in result["error"].lower() or "1" in result["error"]

    @cargo_available
    def test_execution_time_tracked(self, plots_dir):
        code = 'fn main() { println!("done"); }'
        result = execute_rust_code(code, plots_dir)
        assert result["success"] is True
        assert result["execution_time"] >= 0.0

    @cargo_available
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

    def test_sets_descriptive_user_agent(self, plots_dir):
        """A descriptive User-Agent is required by Wikidata's policy (generic
        library defaults get throttled/blocked). The wrapper must carry ours."""
        sparql_json: dict[str, Any] = {"head": {"vars": []}, "results": {"bindings": []}}
        query = "# ENDPOINT: https://example.org/sparql\nSELECT ?s WHERE { }"
        with patch("SPARQLWrapper.SPARQLWrapper") as mock_cls:
            mock_cls.return_value.query.return_value.convert.return_value = sparql_json
            execute_sparql_code(query, plots_dir)
        assert mock_cls.call_args.kwargs.get("agent") == SPARQL_USER_AGENT
        assert "OpenScientist" in SPARQL_USER_AGENT

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
            mock_instance.query.side_effect = SPARQLWrapperException(b"bad query")
            result = execute_sparql_code(query, plots_dir)
        assert result["success"] is False
        assert "SPARQL query error" in result["error"]

    def test_execution_time_tracked(self, plots_dir):
        sparql_json: dict[str, Any] = {"head": {"vars": []}, "results": {"bindings": []}}
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
