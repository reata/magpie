"""Inbound HTTP routes, one module per resource group."""

from types import ModuleType


def docs_tag(module_name: str) -> str:
    """The tag for a router module: the last segment of its dotted name.

    The name rather than the file: a router that grows into a package, or one that ships compiled, names its module
    and not its file.
    """
    return module_name.rpartition(".")[2]


def docs_metadata(module: ModuleType) -> dict[str, str]:
    """The OpenAPI tag entry for a router: its name and its own summary.

    The summary is the module docstring's first line, so a router describes itself in one place -- and that line is
    read by API users as markdown.
    """
    name = docs_tag(module.__name__)
    lines = (module.__doc__ or "").strip().splitlines()
    return {"name": name, "description": lines[0] if lines else name}
