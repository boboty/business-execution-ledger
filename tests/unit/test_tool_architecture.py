"""Import boundary tripwires for the public contract and future runtimes."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "bel"


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"Use auditable absolute imports: {path}"
            yield node.module or ""


def test_tool_contract_has_no_storage_or_domain_dependencies():
    for path in (ROOT / "tools").rglob("*.py"):
        for name in imports(path):
            assert name.split(".")[0] in {"__future__", "typing", "uuid", "bel"}
            if name.startswith("bel."):
                assert name.startswith("bel.tools"), (path, name)


def test_core_never_imports_agent_frameworks_or_tool_host():
    forbidden = {"openai", "agents", "pydantic_ai", "langchain", "langgraph", "pi", "mcp"}
    for directory in ("domain", "application"):
        for path in (ROOT / directory).rglob("*.py"):
            for name in imports(path):
                assert name.split(".")[0] not in forbidden, (path, name)
                assert not name.startswith(("bel.tools", "bel.agent", "bel.infrastructure.tool_host")), (path, name)


def test_future_agent_adapters_use_only_public_bel_contract():
    for directory in ("agent", "agent_runtime", "runtimes"):
        for path in (ROOT / directory).rglob("*.py"):
            for name in imports(path):
                assert name.split(".")[0] not in {"sqlalchemy", "psycopg", "sqlite3"}, (path, name)
                if name.startswith("bel."):
                    assert name.startswith(("bel.tools", f"bel.{directory}.")), (path, name)
