"""Errors that cross layer boundaries."""


class FetchFailed(Exception):
    """A request could not be completed after all retries."""

    def __init__(self, url: str, reason: str, attempts: int) -> None:
        super().__init__(f"{url}: {reason} after {attempts} attempt(s)")
        self.url = url
        self.reason = reason
        self.attempts = attempts


class UnrecognizedListing(Exception):
    """The page is not a directory listing in any supported format."""


class InvalidTransition(ValueError):
    """A status change that the state machine does not allow."""
