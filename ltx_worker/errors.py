class InputError(ValueError):
    """A caller-visible failure whose message never includes credentials or media."""

    def __init__(self, message, code="INVALID_INPUT"):
        super().__init__(message)
        self.code = code
