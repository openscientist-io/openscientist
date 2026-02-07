"""Tests for code_executor module."""

from pathlib import Path

import pandas as pd
import pytest

from shandy.code_executor import (
    ForbiddenImportError,
    execute_code,
    format_execution_result,
    validate_imports,
)

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
