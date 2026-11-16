#!/usr/bin/env python3
"""One-off: drop superseded graph-backed memory types from TigerGraph."""

import sys

import requests
from requests.auth import HTTPBasicAuth

HOST = "http://tigergraph:14240"
AUTH = HTTPBasicAuth("tigergraph", "tigergraph")
GRAPH = "multihop"
JOB = "drop_legacy_memory"


def gsql(command: str, timeout: int = 60) -> str:
    print(f"GSQL ({timeout}s): {command.replace(chr(10), ' | ')}", flush=True)
    response = requests.post(
        f"{HOST}/gsql/v1/statements",
        auth=AUTH,
        data=command.encode(),
        headers={"Content-Type": "text/plain"},
        timeout=timeout,
    )
    response.raise_for_status()
    text = response.text
    print(text, flush=True)
    return text


def main() -> None:
    schema = gsql(f"USE GRAPH {GRAPH}\nls")
    needed = ("MemorySession", "MemoryTurn", "MemoryFact", "MEMORY_HAS_TURN", "MEMORY_PRODUCED_FACT")
    if not any(name in schema for name in needed):
        print("Nothing to drop")
        return

    if f"CREATE SCHEMA_CHANGE JOB {JOB}" in schema:
        print("Using existing drop job", flush=True)
        result = gsql(f"USE GRAPH {GRAPH}\nRUN SCHEMA_CHANGE JOB {JOB}", timeout=300)
    else:
        result = gsql(
            f"USE GRAPH {GRAPH}\n"
            f"CREATE SCHEMA_CHANGE JOB {JOB} {{\n"
            "  DROP EDGE MEMORY_HAS_TURN;\n"
            "  DROP EDGE MEMORY_PRODUCED_FACT;\n"
            "  DROP VERTEX MemoryFact;\n"
            "  DROP VERTEX MemoryTurn;\n"
            "  DROP VERTEX MemorySession;\n"
            "}\n"
            f"RUN SCHEMA_CHANGE JOB {JOB}",
            timeout=300,
        )
    leftover = gsql(f"USE GRAPH {GRAPH}\nls")
    remaining = [name for name in needed if name in leftover]
    if remaining:
        sys.exit(f"Still present: {remaining}\nLast job output:\n{result}")
    gsql(
        f"USE GRAPH {GRAPH}\n"
        f"DROP JOB {JOB}\n"
        "DROP JOB repair_graphrag_memory_schema\n"
        "DROP JOB add_graphrag_memory_vector"
    )
    print("Legacy memory types removed")


if __name__ == "__main__":
    main()
