"""Translate bounded process outcomes to the TAPD CLI error contract."""

from robert_agent.adapters.processes.bounded import BoundedCommandError, run_bounded as run_process


class TapdError(RuntimeError):
    """An adapter failure with a safe diagnostic and conservative write status."""

    def __init__(self, code, *, effect_unknown=False):
        self.code = code
        self.effect_unknown = effect_unknown
        super().__init__(f"TAPD CLI: {code.replace('_', ' ')}")


def run_bounded(command, *, cwd, env, input, timeout, max_output_bytes, stop):
    try:
        return run_process(
            command, cwd=cwd, env=env, input=input, timeout=timeout, max_output_bytes=max_output_bytes, stop=stop
        )
    except BoundedCommandError as exc:
        raise TapdError(exc.code) from None
