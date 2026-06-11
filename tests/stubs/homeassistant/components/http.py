class StaticPathConfig:
    def __init__(self, url, path, cache_headers=True):
        self.url = url
        self.path = path
        self.cache_headers = cache_headers
