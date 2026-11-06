"""Stable command failures shared by work-item operations and controllers."""


class WorkItemValidationError(ValueError):
    pass


class WorkItemConflictError(RuntimeError):
    pass


class WorkItemNotFoundError(LookupError):
    pass
