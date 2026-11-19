"""纯标准库环境下的 SQLAlchemy 最小垫片，仅用于本地服务层测试。

只覆盖模型声明（Column/类型/DeclarativeBase/sessionmaker）与少量查询符号；
不进入容器/生产路径。
"""


class _Type:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class Column(_Type):
    pass


class Integer(_Type):
    pass


class String(_Type):
    pass


class JSON(_Type):
    pass


class Text(_Type):
    pass


class Float(_Type):
    pass


class Boolean(_Type):
    pass


class DeclarativeBase:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Query:
    def __init__(self, items):
        self._items = list(items)

    def filter_by(self, **kwargs):
        self._items = [x for x in self._items
                       if all(getattr(x, k, None) == v for k, v in kwargs.items())]
        return self

    def order_by(self, *_):
        return self

    def all(self):
        return self._items

    def first(self):
        return self._items[0] if self._items else None

    def count(self):
        return len(self._items)


class sessionmaker:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self):
        raise RuntimeError('垫片不提供真实会话，服务测试使用注入的假 DB')


def create_engine(*args, **kwargs):
    return None


def text(sql):
    return sql
