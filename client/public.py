import json
import time
import requests
import logging
from json.decoder import JSONDecodeError

from .utils import json_value
from .exceptions import (
    ClientError,
    ClientConnectionError,
    ClientNotFoundError,
    ClientJSONDecodeError,
    ClientForbiddenError,
    ClientBadRequestError,
    ClientGraphqlError,
    ClientThrottledError,
    ClientIncompleteReadError,
    ClientLoginRequired,
    GenericRequestError,
)


class PublicRequest:
    requests_count = 0
    PUBLIC_API_URL = "https://www.instagram.com/"
    GRAPHQL_PUBLIC_API_URL = "https://www.instagram.com/graphql/query/"
    request_logger = logging.getLogger("public_request")
    request_timeout = 1

    def __init__(self, *args, **kwargs):
        self.public = requests.Session()
        self.public.headers.update(
            {
                "Connection": "Keep-Alive",
                "Accept": "*/*",
                "Accept-Encoding": "gzip,deflate",
                "Accept-Language": "en-US",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/11.1.2 Safari/605.1.15",
            }
        )
        self.request_timeout = kwargs.pop("request_timeout", self.request_timeout)
        super().__init__(*args, **kwargs)

    def public_request(
        self,
        url,
        data=None,
        params=None,
        headers=None,
        return_json=False,
        retries_count=10,
        retries_timeout=10,
    ):
        kwargs = dict(
            data=data, params=params, headers=headers, return_json=return_json,
        )
        assert retries_count <= 10, "Retries count is too high"
        assert retries_timeout <= 600, "Retries timeout is too high"
        for iteration in range(retries_count):
            try:
                return self._send_public_request(url, **kwargs)
            except (
                ClientLoginRequired,
                ClientNotFoundError,
                ClientBadRequestError,
            ) as e:
                # Stop retries
                raise e
            except ClientError as e:
                msg = str(e)
                if (
                    isinstance(e, ClientConnectionError)
                    and "SOCKSHTTPSConnectionPool" in msg
                    and "Max retries exceeded with url" in msg
                    and "Failed to establish a new connection" in msg
                ):
                    raise e
                if retries_count > iteration + 1:
                    time.sleep(retries_timeout)
                else:
                    raise e
                continue

    def _send_public_request(
        self, url, data=None, params=None, headers=None, return_json=False
    ):
        self.requests_count += 1
        if headers:
            self.public.headers.update(headers)
        if self.request_timeout:
            time.sleep(self.request_timeout)
        try:
            if data is not None:  # POST
                response = self.public.data(url, data=data, params=params)
            else:  # GET
                response = self.public.get(url, params=params)

            expected_length = int(response.headers.get("Content-Length"))
            actual_length = response.raw.tell()
            if actual_length < expected_length:
                raise ClientIncompleteReadError(
                    "Incomplete read ({} bytes read, {} more expected)".format(
                        actual_length, expected_length
                    ),
                    response=response,
                )

            self.request_logger.debug(
                "public_request %s: %s", response.status_code, response.url
            )

            self.request_logger.info(
                "[%s] [%s] %s %s",
                self.public.proxies.get("https"),
                response.status_code,
                "POST" if data else "GET",
                response.url,
            )

            response.raise_for_status()
            return response.json() if return_json else response.text

        except JSONDecodeError as e:
            if "/login/" in response.url:
                raise ClientLoginRequired(e, response=response)

            self.request_logger.error(
                "Status %s: JSONDecodeError in public_request (url=%s) >>> %s",
                response.status_code,
                response.url,
                response.text,
            )
            raise ClientJSONDecodeError(
                "JSONDecodeError {0!s} while opening {1!s}".format(e, url),
                response=response,
            )
        except requests.HTTPError as e:
            if e.response.status_code == 403:
                raise ClientForbiddenError(e, response=e.response)

            if e.response.status_code == 400:
                raise ClientBadRequestError(e, response=e.response)

            if e.response.status_code == 429:
                raise ClientThrottledError(e, response=e.response)

            if e.response.status_code == 404:
                raise ClientNotFoundError(e, response=e.response)

            raise ClientError(e, response=e.response)

        except requests.ConnectionError as e:
            raise ClientConnectionError("{} {}".format(e.__class__.__name__, str(e)))

    def public_a1_request(self, endpoint, data=None, params=None, headers=None):
        url = self.PUBLIC_API_URL + endpoint.lstrip("/")
        if params:
            params.update({"__a": 1})
        else:
            params = {"__a": 1}

        response = self.public_request(
            url, data=data, params=params, headers=headers, return_json=True
        )
        try:
            return response["graphql"]
        except KeyError as e:
            error_type = response.get("error_type")
            if error_type == "generic_request_error":
                raise GenericRequestError(
                    json_value(response, "errors", "error", 0, default=error_type),
                    **response
                )
            raise e

    def public_graphql_request(
        self,
        variables,
        query_hash=None,
        query_id=None,
        data=None,
        params=None,
        headers=None,
    ):
        assert query_id or query_hash, "Must provide valid one of: query_id, query_hash"
        default_params = {"variables": json.dumps(variables, separators=(",", ":"))}
        if query_id:
            default_params["query_id"] = query_id

        if query_hash:
            default_params["query_hash"] = query_hash

        if params:
            params.update(default_params)
        else:
            params = default_params

        try:
            body_json = self.public_request(
                self.GRAPHQL_PUBLIC_API_URL,
                data=data,
                params=params,
                headers=headers,
                return_json=True,
            )

            if body_json.get("status", None) != "ok":
                raise ClientGraphqlError(
                    "Unexpected status '{}' in response. Message: '{}'".format(
                        body_json.get("status", None), body_json.get("message", None)
                    ),
                    response=body_json,
                )

            return body_json["data"]

        except ClientBadRequestError as e:
            message = None
            try:
                body_json = e.response.json()
                message = body_json.get("message", None)
            except JSONDecodeError:
                pass

            raise ClientGraphqlError(
                "Error: '{}'. Message: '{}'".format(e, message), response=e.response
            )


class TopSearchesPublic:
    def top_search(self, query):
        """Anonymous IG search request
        """
        url = "https://www.instagram.com/web/search/topsearch/"

        params = {
            "context": "blended",
            "query": query,
            "rank_token": 0.7763938004511706,
            "include_reel": "true",
        }

        response = self.public_request(url, params=params, return_json=True)
        return response


class HashtagPublic:
    def hashtag_info(self, hashtag, max_id=None):
        params = {"max_id": max_id} if max_id else None
        data = self.public_a1_request(
            "/explore/tags/{hashtag!s}/".format(**{"hashtag": hashtag}), params=params
        )
        return data["hashtag"]

    def hashtag_info_gql(self, hashtag, count=12, end_cursor=None):
        variables = {
            "tag_name": hashtag,
            "show_ranked": False,
            "first": int(count),
        }
        if end_cursor:
            variables["after"] = end_cursor

        data = self.public_graphql_request(
            variables, query_hash="f92f56d47dc7a55b606908374b43a314"
        )
        return data["hashtag"]

    def hashtag_top_feed(self, hashtag):
        data = self.hashtag_info(hashtag)
        return data["edge_hashtag_to_top_posts"]["edges"]

    def hashtag_related_hashtags(self, hashtag):
        data = self.hashtag_info(hashtag)
        return [
            item["node"]["name"]
            for item in data["edge_hashtag_to_related_tags"]["edges"]
        ]

    def hashtag_feed(self, hashtag, count=70, sleep=2):
        medias = []
        end_cursor = None

        while True:
            data = self.hashtag_info(hashtag, end_cursor)
            end_cursor = data["edge_hashtag_to_media"]["page_info"]["end_cursor"]
            edges = data["edge_hashtag_to_media"]["edges"]
            medias.extend(edges)

            if (
                not data["edge_hashtag_to_media"]["page_info"]["has_next_page"]
                or len(medias) >= count
            ):
                break

            time.sleep(sleep)

        return medias[:count]


class ProfilePublic:
    def location_feed(self, location_id, count=16, end_cursor=None):
        if count > 50:
            raise ValueError("Count cannot be greater than 50")

        variables = {
            "id": location_id,
            "first": int(count),
        }
        if end_cursor:
            variables["after"] = end_cursor

        data = self.public_graphql_request(
            variables, query_hash="1b84447a4d8b6d6d0426fefb34514485"
        )
        return data["location"]

    def profile_related_info(self, profile_id):
        variables = {
            "user_id": profile_id,
            "include_chaining": True,
            "include_reel": True,
            "include_suggested_users": True,
            "include_logged_out_extras": True,
            "include_highlight_reels": True,
            "include_related_profiles": True,
        }

        data = self.public_graphql_request(
            variables, query_hash="e74d51c10ecc0fe6250a295b9bb9db74"
        )
        return data["user"]
