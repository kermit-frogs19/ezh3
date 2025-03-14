from urllib.parse import urlparse


class URL:
    def __init__(self, url: str) -> None:
        parsed = urlparse(url)
        assert parsed.scheme in (
            "https",
            "wss",
        ), "Only https:// or wss:// URLs are supported."

        self.authority: str = parsed.netloc
        self.full_path: str = parsed.path or "/"

        if parsed.query:
            self.full_path += "?" + parsed.query

        self.scheme: str = parsed.scheme
        self.host: str = parsed.hostname
        self.port: int = parsed.port if parsed.port else 443