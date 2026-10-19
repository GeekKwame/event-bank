import importlib
import pkgutil

from connectors.base import Connector

_REGISTRY: dict[str, type[Connector]] = {}
_discovered = False


class UnsupportedSourceError(LookupError):
    pass


def register[T: type[Connector]](connector_cls: T) -> T:
    slug = connector_cls.slug
    existing = _REGISTRY.get(slug)
    if existing is not None and existing is not connector_cls:
        raise ValueError(f"Connector slug {slug!r} is already registered by {existing.__name__}")
    _REGISTRY[slug] = connector_cls
    return connector_cls


def autodiscover() -> None:
    """Import every module in `connectors.platforms` so their `@register` calls run."""
    global _discovered
    if _discovered:
        return
    import connectors.platforms

    for module in pkgutil.iter_modules(connectors.platforms.__path__):
        importlib.import_module(f"connectors.platforms.{module.name}")
    _discovered = True


def all_connectors() -> dict[str, type[Connector]]:
    autodiscover()
    return dict(_REGISTRY)


def get_connector(slug: str) -> type[Connector]:
    autodiscover()
    try:
        return _REGISTRY[slug]
    except KeyError:
        raise UnsupportedSourceError(f"No connector registered with slug {slug!r}") from None


def match_connector(url: str) -> type[Connector]:
    autodiscover()
    matches = [cls for cls in _REGISTRY.values() if cls.matches(url)]
    if not matches:
        raise UnsupportedSourceError("No connector supports this URL")
    if len(matches) > 1:
        slugs = ", ".join(sorted(cls.slug for cls in matches))
        raise UnsupportedSourceError(f"URL matches more than one connector: {slugs}")
    return matches[0]
