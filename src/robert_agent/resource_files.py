from importlib.resources import files


def resource(*parts: str):
    current = files("robert_agent.resources")
    for part in parts:
        current = current.joinpath(part)
    return current
