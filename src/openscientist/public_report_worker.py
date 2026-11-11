"""Apply process limits before loading the public report renderer."""

from __future__ import annotations

import errno
import json
import resource
import sys
import traceback
from pathlib import Path

EXIT_RESOURCE_LIMIT = 10
EXIT_REPORT_MISSING = 11
EXIT_RENDER_ERROR = 12


def main() -> int:
    """Write one public PDF to standard output under OS resource limits."""
    memory_bytes = int(sys.argv[1]) * 1024 * 1024
    cpu_seconds = int(sys.argv[2])
    output_bytes = int(sys.argv[3])
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_FSIZE, (output_bytes, output_bytes))
    try:
        from openscientist.report.pdf import render_public_report_pdf

        request = json.loads(sys.argv[4])
        rendered = render_public_report_pdf(
            Path(request["job_dir"]),
            tuple(request["excluded_top_level_dirs"]),
            request["run_environment"],
        )
        sys.stdout.buffer.write(rendered)
        sys.stdout.buffer.flush()
    except FileNotFoundError:
        return EXIT_REPORT_MISSING
    except MemoryError:
        return EXIT_RESOURCE_LIMIT
    except OSError as exc:
        if exc.errno in (errno.ENOMEM, errno.EFBIG):
            return EXIT_RESOURCE_LIMIT
        traceback.print_exc()
        return EXIT_RENDER_ERROR
    except Exception:
        traceback.print_exc()
        return EXIT_RENDER_ERROR
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
