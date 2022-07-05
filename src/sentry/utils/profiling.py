from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse

import google.auth.transport.requests
import google.oauth2.id_token
import urllib3
from django.conf import settings
from django.http import StreamingHttpResponse
from parsimonious.exceptions import ParseError
from urllib3.response import HTTPResponse

from sentry.api.event_search import SearchFilter, parse_search_query
from sentry.exceptions import InvalidSearchQuery
from sentry.net.http import connection_from_url
from sentry.utils import metrics


class RetrySkipTimeout(urllib3.Retry):
    """
    urllib3 Retry class does not allow us to retry on read errors but to exclude
    read timeout. Retrying after a timeout adds useless load to Snuba.
    """

    def increment(
        self, method=None, url=None, response=None, error=None, _pool=None, _stacktrace=None
    ):
        """
        Just rely on the parent class unless we have a read timeout. In that case
        immediately give up
        """
        if error and isinstance(error, urllib3.exceptions.ReadTimeoutError):
            raise error.with_traceback(_stacktrace)

        metrics.incr(
            "profiling.client.retry",
            tags={"method": method, "path": urlparse(url).path if url else None},
        )
        return super().increment(
            method=method,
            url=url,
            response=response,
            error=error,
            _pool=_pool,
            _stacktrace=_stacktrace,
        )


_profiling_pool = connection_from_url(
    settings.SENTRY_PROFILING_SERVICE_URL,
    retries=RetrySkipTimeout(
        total=3,
        allowed_methods={"GET", "POST"},
    ),
    timeout=30,
    maxsize=10,
)


def get_from_profiling_service(
    method: str,
    path: str,
    params: Optional[Dict[Any, Any]] = None,
    headers: Optional[Dict[Any, Any]] = None,
) -> HTTPResponse:
    kwargs: Dict[str, Any] = {"headers": {}, "preload_content": False}
    if params:
        path = f"{path}?{urlencode(params, doseq=True)}"
    if headers:
        kwargs["headers"].update(headers)
    if settings.ENVIRONMENT == "production":
        id_token = fetch_id_token_for_service(settings.SENTRY_PROFILING_SERVICE_URL)
        kwargs["headers"].update({"Authorization": f"Bearer {id_token}"})
    return _profiling_pool.urlopen(
        method,
        path,
        **kwargs,
    )


def proxy_profiling_service(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> StreamingHttpResponse:
    profiling_response = get_from_profiling_service(method, path, params=params, headers=headers)

    def stream():
        yield from profiling_response.stream(decode_content=False)

    response = StreamingHttpResponse(
        streaming_content=stream(),
        status=profiling_response.status,
        content_type=profiling_response.headers.get("Content_type", "application/json"),
    )

    for h in ["Content-Encoding", "Vary"]:
        if h in profiling_response.headers:
            response[h] = profiling_response.headers[h]

    return response


def fetch_id_token_for_service(service_url: str) -> str:
    auth_req = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(auth_req, service_url)


PROFILE_FILTERS = {
    "android_api_level",
    "device_classification",
    "device_locale",
    "device_manufacturer",
    "device_model",
    "device_os_build_number",
    "device_os_name",
    "device_os_version",
    "platform",
    "transaction_name",
    "version",
}


def parse_profile_filters(query: str) -> Dict[str, str]:
    try:
        parsed_terms = parse_search_query(query)
    except ParseError as e:
        raise InvalidSearchQuery(f"Parse error: {e.expr.name} (column {e.column():d})")

    profile_filters: Dict[str, str] = {}

    for term in parsed_terms:
        if not isinstance(term, SearchFilter):
            raise InvalidSearchQuery("Invalid query: Unknown filter")
        if term.operator != "=":  # only support equality filters
            raise InvalidSearchQuery("Invalid query: Illegal operator")
        if term.key.name not in PROFILE_FILTERS:
            raise InvalidSearchQuery(f"Invalid query: {term.key.name} is not supported")
        if term.key.name in profile_filters and term.value.value != profile_filters[term.key.name]:
            raise InvalidSearchQuery(f"Invalid query: Multiple filters for {term.key.name}")
        profile_filters[term.key.name] = term.value.value

    return profile_filters
