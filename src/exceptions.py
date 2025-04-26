"""Custom exceptions"""

class MatchNotFoundError(Exception):
    """Raised when a requested match ID is invalid or does not exist."""

class MatchEndedError(Exception):
    """Raised when trying to interact with a match that has already ended."""
