"""
Exception classes that may be raised from the Wayback package.
"""


class WaybackException(Exception):
    "Base exception class for all Wayback-specific errors."


class UnexpectedResponseFormat(WaybackException):
    """
    Raised when data returned by the Wayback Machine is formatted in an
    unexpected or unparseable way.
    """


class BlockedByRobotsError(WaybackException):
    """
    Raised when a URL can't be queried in Wayback because it was blocked by a
    site's `robots.txt` file.
    """


class BlockedSiteError(WaybackException):
    """
    Raised when a URL has been blocked from access or querying in Wayback. This
    is often because of a takedown request. (URLs that are blocked because of
    ``robots.txt`` get a ``BlockedByRobotsError`` instead.)
    """


# TODO: split this up into a family of more specific errors? When playback
# failed partway into a redirect chain, when a redirect goes outside
# redirect_target_window, when a memento was circular?
class MementoPlaybackError(WaybackException):
    """
    Raised when a Memento can't be 'played back' (loaded) by the Wayback
    Machine for some reason. This is a server-side issue, not a problem in
    parsing data from Wayback.
    """


class NoMementoError(MementoPlaybackError):
    """
    Raised when there was no memento available for a given URL. This might mean
    the given URL has no mementos at all or that none that are available for
    playback.

    This also means you should *not* try to request a memento of the same URL
    in a different timeframe. If there may be other mementos of the URL
    available, you'll get a different error.
    """


class NoCompatibleMementoError(MementoPlaybackError):
    """
    Raised when the CDX index has captures of a URL, but none of them satisfy
    the selection criteria for the requested time (time direction, maximum
    distance, status code, media type, or payload requirement).

    Unlike :class:`NoMementoError`, this does not mean the URL was never
    archived -- it means that every available candidate was excluded by an
    explicit restriction. Restrictions are never relaxed automatically (for
    example, a capture in the future is not returned when only past captures
    were requested).

    Attributes
    ----------
    target : datetime.datetime
        The requested target time.
    url : str, optional
        The URL that was queried, when known.
    rejections : list of tuple(str, str)
        Each entry is ``(candidate_label, reason)`` where ``reason`` is the
        value of a :class:`wayback._selection.RejectionReason`, identifying
        which category of restriction excluded that candidate.
    """

    def __init__(self, message, target=None, url=None, rejections=None):
        self.target = target
        self.url = url
        # Keep an independent copy so later mutation of the caller's list
        # cannot change the exception.
        self.rejections = list(rejections or [])
        super().__init__(message)


class WaybackRetryError(WaybackException):
    """
    Raised when a request to the Wayback Machine has been retried and failed
    too many times. The number of tries before this exception is raised
    generally depends on your `WaybackSession` settings.

    Attributes
    ----------
    retries : int
        The number of retries that were attempted.
    cause : Exception
        The actual, underlying error that would have caused a retry.
    time : int
        The total time spent across all retried requests, in seconds.
    """

    def __init__(self, retries, total_time, causal_error):
        self.retries = retries
        self.cause = causal_error
        self.time = total_time
        super().__init__(f'Retried {retries} times over {total_time or "?"} seconds (error: {causal_error})')


class RateLimitError(WaybackException):
    """
    Raised when the Wayback Machine responds with a 429 (too many requests)
    status code. In general, this package's built-in limits should help you
    avoid ever hitting this, but if you are running multiple processes in
    parallel, you could go overboard.

    Attributes
    ----------
    retry_after : int, optional
        Recommended number of seconds to wait before retrying. If the Wayback
        Machine does not include it in the HTTP response, it will be set to
        ``None``.
    """

    def __init__(self, response, retry_after):
        self.response = response
        self.retry_after = retry_after

        message = 'Wayback rate limit exceeded'
        if self.retry_after:
            message = f'{message}, retry after {self.retry_after} s'

        super().__init__(message)


class SessionClosedError(Exception):
    """
    Raised when a Wayback session is used to make a request after it has been
    closed and disabled.
    """
