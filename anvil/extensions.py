"""Extra tool providers for the agent: artifacts and MCP connectors.

A provider has tool_defs(agent), owns(name), validate(name, args), approval(agent, name, args)
-> (needs_approval, dangers), preview(agent, name, args), label(name) and run(agent, name, args).
Keeping them here keeps agent.py's own tool code unchanged.
"""

_PROVIDERS = None


def providers() -> list:
    global _PROVIDERS
    if _PROVIDERS is None:
        from .artifacts import ArtifactTools
        from .connectors import ConnectorTools
        _PROVIDERS = [ArtifactTools(), ConnectorTools()]
    return _PROVIDERS


def find(name: str):
    return next((p for p in providers() if p.owns(name)), None)


def tool_defs(agent) -> list[dict]:
    defs = []
    for p in providers():
        try:
            defs += p.tool_defs(agent)
        except Exception as e:  # a broken connector must never stop the agent
            notice = getattr(getattr(agent, "ui", None), "notice", None)
            if notice:
                notice(f"Couldn't load tools from {type(p).__name__}: {e}")
    return defs


def label(name: str) -> str:
    p = find(name)
    return p.label(name) if p else ""
