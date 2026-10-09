"""YAML loading that refuses duplicate keys.

``yaml.safe_load`` keeps the last of two equal keys without a word. In a policy that decides what may merge, that
lets a second ``protected_paths: []`` at the bottom of the file quietly cancel the rule a reviewer read at the top. A
repeated key is therefore an error, at any depth. (A key that overrides a ``<<`` merge is not a duplicate.)
"""

from __future__ import annotations

from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=True)
        try:
            repeated = key in seen
        except TypeError:  # an unhashable key: the normal loader reports it with its own message
            continue
        if repeated:
            raise ConstructorError(
                "while constructing a mapping", node.start_mark, f"found a duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load(text: str) -> Any:
    """Parse one YAML document with safe types and no duplicate keys. Raises ``yaml.YAMLError``."""
    return yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - the loader is a SafeLoader subclass
