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


class VerificationFailed(Exception):
    """A downloaded file does not match its listed size or its published checksum."""


class ExtractionFailed(Exception):
    """An archive is corrupt, encrypted, or over the extraction limits."""


class UnsupportedArchive(ExtractionFailed):
    """An archive format this tool recognises but does not unpack."""


class StateLocked(Exception):
    """Another process is already using this state database."""
