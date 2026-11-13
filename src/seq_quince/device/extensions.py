"""Load explicitly exported devices from SEQ_QUINCE_DEVICE_PATH directories."""

import hashlib
import importlib.util
import inspect
import os
import sys
from importlib.abc import ExecutionLoader
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Mapping, Set, Tuple, Type

from seq_quince.device.device_abs import DeviceABC


class DeviceExtensionError(RuntimeError):
    """An extension could not be imported or registered."""


class DeviceExtensionLoader:
    """
    Finds additional external device implementations. 
    One thread at a time can perform registry operations.
    """

    def __init__(self) -> None:
        self._loaded_paths: Set[Path] = set()
        self._loading = False

    def load(self, registry: Mapping[str, Type[DeviceABC]]) -> Dict[str, Type[DeviceABC]]:
        """Return validated additions, without modifying *registry*."""
        if self._loading:
            raise DeviceExtensionError(
                "Recursive device extension discovery: extension imports must "
                "not create devices or query the factory during discovery"
            )
        self._loading = True
        try:
            return self._load(registry)
        finally:
            self._loading = False

    def _load(self, registry: Mapping[str, Type[DeviceABC]]) -> Dict[str, Type[DeviceABC]]:
        roots = self._new_roots()
        if not roots:
            return {}
        additions: Dict[str, Type[DeviceABC]] = {}
        sources: Dict[str, Path] = {}
        namespaces = [self._namespace(root) for root in roots]
        previous_modules = {
            name: module
            for name, module in sys.modules.copy().items()
            if self._belongs_to(name, namespaces)
        }
        try:
            # associate each extension directory with its corresponding Python module
            for root, namespace in zip(roots, namespaces):
                self._create_namespace(root, namespace)
                for entry, is_package in self._entries(root):
                    module = self._import_entry(entry, namespace, is_package)
                    if "DEVICE_CLASSES" not in vars(module):
                        continue
                    exports = vars(module)["DEVICE_CLASSES"]
                    for name, device_cls in self._validate_exports(exports, entry):
                        if name in registry or name in additions:
                            source = (
                                str(sources[name]) if name in additions
                                else self._class_source(registry[name])
                            )
                            raise DeviceExtensionError(
                                f"Duplicate device type {name!r} in extension "
                                f"'{entry}'; already registered by '{source}'"
                            )
                        additions[name] = device_cls
                        sources[name] = entry
        except BaseException:
            # Roll back only our module namespaces, including imported helpers.
            # Preserve KeyboardInterrupt/SystemExit while cleaning up as well.
            for name in list(sys.modules):
                if self._belongs_to(name, namespaces):
                    del sys.modules[name]
            sys.modules.update(previous_modules)
            raise

        self._loaded_paths.update(roots)
        return additions

    def _new_roots(self) -> List[Path]:
        roots: List[Path] = []
        seen: Set[Path] = set()
        for value in os.environ.get("SEQ_QUINCE_DEVICE_PATH", "").split(os.pathsep):
            if not value:
                continue
            root = Path(os.path.realpath(os.path.expanduser(value)))
            if root in seen or root in self._loaded_paths:
                continue
            if not root.is_dir():
                raise DeviceExtensionError(
                    f"SEQ_QUINCE_DEVICE_PATH entry '{value}' is not a directory "
                    f"(resolved to '{root}')"
                )
            seen.add(root)
            roots.append(root)
        return roots

    @staticmethod
    def _namespace(root: Path) -> str:
        digest = hashlib.sha256(os.fsencode(str(root))).hexdigest()
        return "_seq_quince_device_extensions_" + digest

    @staticmethod
    def _belongs_to(name: str, namespaces: List[str]) -> bool:
        return any(name == prefix or name.startswith(prefix + ".")
                   for prefix in namespaces)

    @staticmethod
    def _create_namespace(root: Path, namespace: str) -> None:
        if namespace in sys.modules:
            return
        module = ModuleType(namespace)
        spec = ModuleSpec(namespace, loader=None, is_package=True)
        spec.submodule_search_locations = [str(root)]
        module.__spec__ = spec
        module.__package__ = namespace
        module.__path__ = [str(root)]
        sys.modules[namespace] = module

    @staticmethod
    def _entries(root: Path) -> List[Tuple[Path, bool]]:
        """ Cheks if each path is a package directory """
        try:
            children = sorted(root.iterdir())
            entries: List[Tuple[Path, bool]] = []
            names: Set[str] = set()
            for entry in children:
                if entry.name.startswith(("_", ".")):
                    continue
                is_package = entry.is_dir() and (entry / "__init__.py").is_file()
                if not is_package and not (entry.is_file() and entry.suffix == ".py"):
                    continue
                name = entry.name if is_package else entry.stem
                if not name.isidentifier():
                    raise DeviceExtensionError(f"Extension '{entry}' must have a valid Python module name")
                if name in names:
                    raise DeviceExtensionError(
                        f"Ambiguous extension module name {name!r} in '{root}' "
                        "(both a Python file and package directory exist)"
                    )
                names.add(name)
                entries.append((entry, is_package))
            return entries
        except OSError as exc:
            raise DeviceExtensionError(f"Cannot scan extension directory '{root}': {exc}") from exc

    @staticmethod
    def _import_entry(entry: Path, namespace: str, is_package: bool) -> ModuleType:
        """ Loads a Python file or package from a given path and returns the module object """
        name = entry.name if is_package else entry.stem
        module_name = namespace + "." + name
        if module_name in sys.modules:
            return sys.modules[module_name]
        source = entry / "__init__.py" if is_package else entry
        try:
            # import both, files and directories
            spec = importlib.util.spec_from_file_location(
                module_name, str(source),
                submodule_search_locations=[str(entry)] if is_package else None,
            )
            if spec is None or not isinstance(spec.loader, ExecutionLoader):  # for pyright: SourceFileLoader provides the methods expected from an ExecutionLoader
                raise ImportError(f"Cannot create an executable import specification for '{source}'")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module   # register loaded module
            spec.loader.exec_module(module) # Execute the code of the module object
            parent = sys.modules[namespace] # seq_quince.device.extensions
            setattr(parent, name, module)
            return module
        except Exception as exc:
            raise DeviceExtensionError(
                f"Cannot import device extension '{source}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    @staticmethod
    def _validate_exports(
        exports: Any, entry: Path
    ) -> List[Tuple[str, Type[DeviceABC]]]:
        if not isinstance(exports, Mapping):
            raise DeviceExtensionError(f"DEVICE_CLASSES in extension '{entry}' must be a mapping")
        validated: List[Tuple[str, Type[DeviceABC]]] = []
        for name, device_cls in exports.items():
            if not isinstance(name, str) or not name.strip():
                raise DeviceExtensionError(
                    f"DEVICE_CLASSES key {name!r} in extension '{entry}' "
                    "must be a nonempty string"
                )
            if (not inspect.isclass(device_cls)
                    or not issubclass(device_cls, DeviceABC)
                    or inspect.isabstract(device_cls)):
                raise DeviceExtensionError(
                    f"DEVICE_CLASSES[{name!r}] in extension '{entry}' "
                    "must be a concrete DeviceABC subclass"
                )
            validated.append((name, device_cls))
        return validated

    @staticmethod
    def _class_source(device_cls: Type[DeviceABC]) -> str:
        module = sys.modules.get(device_cls.__module__)
        source = getattr(module, "__file__", device_cls.__module__)
        return f"{source} ({device_cls.__name__})"
