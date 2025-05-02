from urllib.parse import urlparse


class URL:
    def __init__(self, url: str) -> None:
        self.raw_url = url
        parsed = urlparse(url)
        self.scheme = "https" if not parsed.scheme else parsed.scheme
        assert self.scheme in {"https", "wss"}, "Only https:// or wss:// URLs are supported."

        self.authority: str = parsed.netloc
        self.full_path: str = parsed.path or "/"

        if parsed.query:
            self.full_path += "?" + parsed.query

        self.host: str = parsed.hostname
        self.port: int = parsed.port if parsed.port else 443

    # Resolves the conflict between base URL provided as class param and URL provided in the request method
    def resolve(self, other: "URL") -> "URL":
        if other.raw_url.startswith(self.raw_url) or not other.only_path:
            return other
        else:
            url = self.raw_url + other.raw_url

        return self.__class__(url)

    @property
    def only_path(self) -> bool:
        return not self.host


