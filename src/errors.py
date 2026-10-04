"""Expected failures shared by workers and HTTP handlers."""


class BrowserError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Changed(BrowserError):
    def __init__(self):
        super().__init__("The source file changed or disappeared. Refresh the library.", 409)


class CapacityError(BrowserError):
    def __init__(self):
        super().__init__("Insufficient cache space. Stop other playback and try again.", 507)
