class RealEstateBlockedError(RuntimeError):
    """Raised when realestate.com.au returns a block/challenge shell instead of listing content."""

    code = "realestate_rate_limited_or_blocked"

    def __init__(self, reason: str = "realestate_rate_limited_or_blocked", retry_after_seconds: int | None = None):
        self.reason = reason or self.code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(self.reason)


class RealEstateRateLimitedError(RealEstateBlockedError):
    code = "realestate_rate_limited_or_blocked_http_429"
