import time
import copy
import functools
import collections
from abc import abstractmethod, ABC
from typing import MutableMapping, Any, Union, TypeVar, Iterable, Type, List, Tuple, Dict, Callable

from datapipelines import DataSource, PipelineContext
from merakicommons.ratelimits import FixedWindowRateLimiter, MultiRateLimiter

from ..common import HTTPClient, HTTPError, Curl
from ...data import Platform


class APIRequestError(HTTPError):
    pass


class APIError(HTTPError):
    pass


class APINotFoundError(HTTPError):
    pass


class APIForbiddenError(APINotFoundError):
    pass


_ERROR_CODES = {
    400: APIRequestError,
    401: APIForbiddenError,
    403: APIRequestError,
    404: APINotFoundError,
    415: RuntimeError,
    429: RuntimeError,
    500: APIError,
    503: APIError
}

T = TypeVar("T")



class RiotAPIRateLimiter(MultiRateLimiter):
    # The application limiter and method limiters will each be an instance of this.

    def __init__(self, limiting_share):
        self.limiting_share = limiting_share
        super().__init__()  # Initialize with no underlying limiters
        self._limiters = []  # Make it a list rather than a tuple so we can append

    def restrict_for(self, seconds: int) -> None:
        for limiter in self._limiters:
            limiter.restrict_for(seconds)

    def _construct_limiters(self, limits: List[List[int]]):
        # Creates the necessary FixedWindowRateLimiters from the rates in the headers
        assert len(self._limiters) == 0
        # Create the rate limiters
        for permits, window in limits:
            self._limiters.append(
                FixedWindowRateLimiter(window_seconds=window, window_permits=permits)
            )

    def adjust_rate_limits_if_necessary(self, limits: List[List[int]]) -> None:
        if len(self._limiters) == 0:
            self._construct_limiters(limits)
        for permits, window in limits:
            permits = permits * self.limiting_share
            for_window = self._get_specific_limiter_for_window(window)
            if permits != for_window._window_permits:
                for_window.set_permits(permits)

    def _get_specific_limiter_for_window(self, window: int) -> FixedWindowRateLimiter:
        for limiter in self._limiters:
            if limiter._window_seconds == window:
                return limiter


def _split_rate_limit_header(header):
    rates = []
    for pw in header.split(","):
        pw = pw.split(":")
        p, w = pw
        p, w = int(p), int(w)
        rates.append((p, w))
    return rates


class RiotAPIService(DataSource):
    def __init__(self, api_key: str, app_rate_limiter: RiotAPIRateLimiter, request_by_id: bool = True, handler_configs: Dict = None, http_client: HTTPClient = None):
        self._limiting_share = app_rate_limiter.limiting_share
        self._request_by_id = request_by_id

        if http_client is None:
            self._client = HTTPClient()
        else:
            self._client = http_client

        self._headers = {
            "X-Riot-Token": api_key
        }

        # Both the application and method rate limiters will be in the same rate limiter
        self._rate_limiters = {
            "application": app_rate_limiter
        }

        default_handler_configs = {
            "404": {
                "strategy": "throw"
            },
            "429": {
                "service": {
                    "strategy": "exponential_backoff",
                    "initial_backoff": 1.0,
                    "backoff_factor": 2.0,
                    "max_attempts": 4
                },
                "method": {
                    "strategy": "retry_from_headers",
                    "max_attempts": 5
                },
                "application": {
                    "strategy": "retry_from_headers",
                    "max_attempts": 5
                }
            },
            "500": {
                "strategy": "throw"
            },
            "503": {
                "strategy": "throw"
            },
            "timeout": {
                "strategy": "throw"
            },
            "403": {
                "strategy": "throw"
            }
        }
        if handler_configs is None:
            handler_configs = default_handler_configs
        else:
            def recursive_update(d, u):
                for k, v in u.items():
                    if isinstance(v, collections.Mapping):
                        r = recursive_update(d.get(k, {}), v)
                        d[k] = r
                    else:
                        d[k] = u[k]
                return d
            recursive_update(handler_configs, default_handler_configs)

        new_handler_instance = {
            "throw": lambda **init_args: ThrowException(),
            "exponential_backoff": lambda **init_args: ExponentialBackoff(**init_args),
            "retry_from_headers": lambda **init_args: RetryFromHeaders(**init_args)
        }
        self._handlers = {429: {}}  # type: Dict[Union[str, int], Union[Dict[Union[str, int], Callable], Callable]]
        for code, config in handler_configs.items():
            config = copy.deepcopy(config)
            if code != "timeout":
                code = int(code)
            if code == 429:
                # config == {service: ..., method: ... , application: ...}
                for app, config in config.items():
                    strategy = config.pop("strategy")
                    self._handlers[code][app] = functools.partial(new_handler_instance[strategy], **config)
            else:
                strategy = config.pop("strategy")
                self._handlers[code] = functools.partial(new_handler_instance[strategy], **config)

    def _get_rate_limiter(self, platform: Platform, endpoint: str):
        try:
            limiter = self._rate_limiters[(platform, endpoint)]
        except KeyError:
            limiter = RiotAPIRateLimiter(self._limiting_share)
            self._rate_limiters[(platform, endpoint)] = limiter
        return limiter

    def _adjust_rate_limiters_from_headers(self, rate_limiter, response_headers):
        # If Riot changes the # of permits allowed in their response headers, change our rate limiters.
        # We are currently ignoring the X-*-Rate-Limit-Count headers and assuming our rate limiter logic agrees.
        if "X-App-Rate-Limit" in response_headers:
            limits = _split_rate_limit_header(response_headers["X-App-Rate-Limit"])
            self._rate_limiters["application"].adjust_rate_limits_if_necessary(limits)
        if "X-Method-Rate-Limit" in response_headers:
            limits = _split_rate_limit_header(response_headers["X-Method-Rate-Limit"])
            rate_limiter.adjust_rate_limits_if_necessary(limits)

    def _get(self, url: str, parameters: MutableMapping[str, Any] = None, rate_limiter: RiotAPIRateLimiter = None, connection: Curl = None) -> Union[dict, list, Any]:
        try:
            body, response_headers = self._client.get(url=url,
                                                      parameters=parameters,
                                                      headers=self._headers,
                                                      rate_limiters=[self._rate_limiters["application"], rate_limiter],
                                                      connection=connection)
            self._adjust_rate_limiters_from_headers(rate_limiter, response_headers)
            return body

        # Retry with the handler
        except HTTPError as error:
            try:
                new_error_type = _ERROR_CODES[error.code]
            except KeyError:
                raise RuntimeError("Encountered unexpected HTTP error code {code} with message \"{message}\". Report this to the Cassiopeia team.".format(code=error.code, message=str(error)))

            # Try to properly handling the 429 and retry the call after the appropriate time limit.
            if error.code == 429:
                # Identify which rate limit was hit (application, method, or service)
                if "X-Rate-Limit-Type" not in error.response_headers:
                    rate_limiting_type = "service"
                elif error.response_headers["X-Rate-Limit-Type"] == "application":
                    rate_limiting_type = "application"
                elif error.response_headers["X-Rate-Limit-Type"] == "method":
                    rate_limiting_type = "method"
                elif error.response_headers["X-Rate-Limit-Type"] == "service":
                    rate_limiting_type = "service"
                else:
                    raise ValueError("Unknown cause of rate limit; aborting. Headers were: {}".format(error.response_headers))

                # Create a new handler
                handler = self._handlers[429][rate_limiting_type]()  # type: FailedRequestHandler
            else:
                handler = self._handlers[error.code]()

            try:
                body, response_headers = handler(error=error,
                                                 requester=self._client.get,
                                                 url=url,
                                                 parameters=parameters,
                                                 headers=self._headers,
                                                 rate_limiters=[self._rate_limiters["application"], rate_limiter],
                                                 connection=connection
                                                 )
                self._adjust_rate_limiters_from_headers(rate_limiter, response_headers)
                return body

            except HTTPError as error:
                # The above error handling didn't work, so raise an appropriate error.
                if new_error_type is RuntimeError:
                    new_error = RuntimeError("Encountered an HTTP error code {code} with message \"{message}\" which should have already been handled. Report this to the Cassiopeia team.".format(code=error.code, message=str(error)))
                elif new_error_type is APIError:
                    new_error = APIError("The Riot API experienced an internal error on the request. You may want to retry the request after a short wait or continue without the result. The received error was {code}: \"{message}\"".format(code=error.code, message=str(error)), error.code)
                elif new_error_type is APINotFoundError:
                    new_error = APINotFoundError("The Riot API returned a NOT FOUND error for the request. The received error was {code}: \"{message}\"".format(code=error.code, message=str(error)), error.code)
                elif new_error_type is APIRequestError:
                    new_error = APIRequestError("The Riot API returned an error on the request. The received error was {code}: \"{message}\"".format(code=error.code, message=str(error)), error.code)
                elif new_error_type is APIForbiddenError:
                    new_error = APIForbiddenError("The Riot API returned a FORBIDDEN error for the request. The received error was {code}: \"{message}\"".format(code=error.code, message=str(error)), error.code)
                else:
                    new_error = new_error_type(str(error))

                raise new_error from error

    @abstractmethod
    def get(self, type: Type[T], query: MutableMapping[str, Any], context: PipelineContext = None) -> T:
        pass

    @abstractmethod
    def get_many(self, type: Type[T], query: MutableMapping[str, Any], context: PipelineContext = None) -> Iterable[T]:
        pass


class FailedRequestHandler(ABC):
    @abstractmethod
    def __call__(self, error, requester, url, parameters, headers, rate_limiters, connection) -> Tuple[Union[dict, list, str, bytes], dict]:
        pass


class ExponentialBackoff(FailedRequestHandler):
    def __init__(self, initial_backoff: int, backoff_factor: int, max_attempts: int):
        self.backoff = initial_backoff
        self.factor = backoff_factor
        self.max_attempts = max_attempts

    def __call__(self, error, requester, url, parameters, headers, rate_limiters, connection) ->  Tuple[Union[dict, list, str, bytes], dict]:
        backoff = self.backoff
        for _ in range(self.max_attempts):
            try:
                print("INFO: Unexpected service rate limit, backing off for {} seconds.".format(backoff))
                time.sleep(backoff)
                backoff = self.backoff * self.factor
                return requester(url, parameters, headers, rate_limiters, connection)
            except error.__class__ as error:
                pass


class RetryFromHeaders(object):
    def __init__(self, max_attempts: int):
        self.max_attempts = int(max_attempts)

    def __call__(self, error, requester, url, parameters, headers, rate_limiters, connection) -> Tuple[Union[dict, list, str, bytes], dict]:
        for _ in range(self.max_attempts):
            try:
                backoff = int(error.response_headers["Retry-After"])
                print("INFO: Unexpected service rate limit, backing off for {} seconds.".format(backoff))
                time.sleep(backoff)
                for rate_limiter in rate_limiters:
                    rate_limiter.restrict_for(backoff)
                return requester(url, parameters, headers, rate_limiters, connection)
            except error.__class__ as error:
                pass


class ThrowException(FailedRequestHandler):
    def __call__(self, error, requester, url, parameters, headers, rate_limiters, connection) -> Tuple[Union[dict, list, str, bytes], dict]:
        raise error
