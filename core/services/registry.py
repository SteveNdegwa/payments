import importlib

from core.providers.base_provider import BaseProvider


_REGISTRY: dict[str, type[BaseProvider]] = {}


def register_provider(class_name: str):
    def decorator(cls: type[BaseProvider]):
        _REGISTRY[class_name] = cls
        return cls
    return decorator


def get_provider_instance(class_name: str, credentials: dict, config: dict | None = None) -> BaseProvider:
    if class_name not in _REGISTRY:
        module_path, _, klass = class_name.rpartition(".")
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, klass)
            _REGISTRY[class_name] = cls
        except (ImportError, AttributeError) as exc:
            raise ValueError(f"Provider '{class_name}' could not be loaded: {exc}") from exc

    return _REGISTRY[class_name](credentials=credentials, config=config)