"""Start and end log lines, with the duration, for the steps of a long
operation such as graph setup."""
import logging
import time
from contextlib import contextmanager


@contextmanager
def log_step(logger: logging.Logger, graphname: str, step: str, job: str = None):
    """Log ``step`` on ``graphname`` when it starts and when it ends, with
    its duration; ``job`` names the schema change job of the step."""
    where = f"graph={graphname}" + (f" job={job}" if job else "")
    logger.info(f"setup step start: {step} ({where})")
    started = time.time()
    try:
        yield
    except Exception:
        logger.error(f"setup step failed: {step} ({where}) after {time.time() - started:.1f}s")
        raise
    logger.info(f"setup step done: {step} ({where}) in {time.time() - started:.1f}s")
