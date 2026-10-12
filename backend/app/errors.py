"""Application errors with stable codes for the API and the agent."""


class DataflowError(Exception):
    def __init__(self, message: str, *, code: str = "dataflow_error", status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
