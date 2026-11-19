"""纯标准库环境下的 FastAPI 最小垫片，仅用于本地服务层测试。

不进入容器/生产路径（requirements 固定 fastapi==0.115.6）；
只实现 HTTPException/APIRouter/Depends 与测试实际用到的语义。
"""


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str = ''):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class APIRouter:
    def __init__(self, *args, **kwargs):
        self.routes = []

    def _register(self, method: str, path: str):
        def deco(fn):
            self.routes.append((method, path, fn))
            return fn
        return deco

    def get(self, path):
        return self._register('GET', path)

    def post(self, path):
        return self._register('POST', path)

    def put(self, path):
        return self._register('PUT', path)


def Depends(fn):
    try:
        return fn()
    except TypeError:
        return fn


class Header:
    def __init__(self, default=''):
        self.default = default


def Header_(*args, **kwargs):
    return kwargs.get('default', '')


class Response:
    def __init__(self, content='', media_type=None, headers=None):
        self.body = content
        self.media_type = media_type
        self.headers = headers or {}
