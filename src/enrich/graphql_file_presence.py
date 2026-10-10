from __future__ import annotations

import json
from collections.abc import Mapping

from lib.graphql_batch import DEFAULT_BATCH_SIZE, ParsedBatch, rate_limit_from_payload


class FilePresenceAdapter:
    name = "file_presence"

    def __init__(
        self, path: str, full_names: Mapping[str, str], *, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> None:
        self._path = path
        self._full_names = dict(full_names)
        self.batch_size = batch_size

    def _parts(self, key: str) -> tuple[str, str]:
        owner, _, name = self._full_names[key].partition("/")
        return owner, name

    def build_query(self, aliases: Mapping[str, str]) -> str:
        expression = f"HEAD:{self._path}"
        lines = ["query {"]
        for alias, key in aliases.items():
            owner, name = self._parts(key)
            lines.append(
                f"  {alias}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{"
            )
            lines.append(f"    object(expression: {json.dumps(expression)}) {{ __typename }}")
            lines.append("  }")
        lines.append("  rateLimit { cost used remaining }")
        lines.append("}")
        return "\n".join(lines)

    def parse(self, payload: Mapping[str, object], aliases: Mapping[str, str]) -> ParsedBatch:
        data = payload.get("data")
        values: dict[str, bool] = {}
        rate_limit = rate_limit_from_payload(payload)
        if not isinstance(data, dict):
            return ParsedBatch(values=values, rate_limit=rate_limit)
        for alias, key in aliases.items():
            node = data.get(alias)
            if not isinstance(node, dict):
                continue  # null repository: fall back (rename/deleted handling lives in REST)
            obj = node.get("object")
            typename = obj.get("__typename") if isinstance(obj, dict) else None
            values[key] = typename == "Blob"
        return ParsedBatch(values=values, rate_limit=rate_limit)
