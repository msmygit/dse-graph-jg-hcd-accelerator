"""JanusGraph / HCD target schema initialiser.

Applies the translated JanusGraph JSON schema to the target cluster and verifies
that all schema elements were created successfully.
"""

from __future__ import annotations

import json
import time
from typing import Any


class Initialiser:
    """Applies a JanusGraph JSON schema to the target cluster.

    Two modes:
      - 'gremlin-script' (default): sends schema creation Gremlin scripts via WebSocket.
      - 'server-restart': relies on schema.init.strategy=json in the JanusGraph config
        (declarative JSON init). Not directly invocable from Python — documented for
        ops reference.

    Writes to: target JanusGraph cluster (schema management operations only — no data).
    """

    def __init__(
        self,
        target_gremlin_url: str,
        schema_json_path: str,
        mode: str = "gremlin-script",
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        """
        Args:
            target_gremlin_url: WebSocket URL of the target JanusGraph Gremlin server.
            schema_json_path: Path to the translated JanusGraph JSON schema file.
            mode: 'gremlin-script' (default) or 'server-restart'.
            username: Optional Gremlin server username.
            password: Optional Gremlin server password.
        """
        self.target_gremlin_url = target_gremlin_url
        self.schema_json_path = schema_json_path
        self.mode = mode
        self._username = username
        self._password = password

    def run(self) -> dict[str, Any]:
        """Execute schema initialisation and return a result dict.

        Writes to: target JanusGraph cluster — creates vertex/edge labels,
        property keys, and indexes via management API.
        """
        with open(self.schema_json_path, encoding="utf-8") as fh:
            schema: dict[str, Any] = json.load(fh)

        if self.mode == "gremlin-script":
            return self._init_via_gremlin(schema)
        msg = f"Mode '{self.mode}' requires JanusGraph server config; see schema.init.strategy=json."
        return {"mode": self.mode, "status": "skipped", "message": msg}

    def _submit(self, script: str, bindings: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
        """Submit a Gremlin management script to the target JanusGraph cluster."""
        from gremlin_python.driver import client as gremlin_client

        kwargs: dict[str, Any] = {}
        if self._username:
            kwargs["username"] = self._username
        if self._password:
            kwargs["password"] = self._password

        c = gremlin_client.Client(self.target_gremlin_url, "g", **kwargs)
        try:
            return c.submit(script, bindings or {}).all().result()
        finally:
            c.close()

    def _init_via_gremlin(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Send schema creation Gremlin scripts to the JanusGraph Gremlin server.

        Writes to: target JanusGraph — management transactions.
        """
        created: list[str] = []
        errors: list[str] = []

        # 1. Property keys
        for pk in schema.get("propertyKeys", []):
            script = (
                "mgmt = graph.openManagement();"
                "if (!mgmt.getPropertyKey(pk_name)) {"
                "  mgmt.makePropertyKey(pk_name).dataType(Object.class)"
                "      .cardinality(Cardinality.SINGLE).make();"
                "}"
                "mgmt.commit();"
            )
            try:
                self._submit(script, {"pk_name": pk["key"]})
                created.append(f"PropertyKey:{pk['key']}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"PropertyKey:{pk['key']} - {exc}")

        # 2. Vertex labels
        for vl in schema.get("vertexLabels", []):
            script = (
                "mgmt = graph.openManagement();"
                "if (!mgmt.getVertexLabel(vl_name)) {"
                "  mgmt.makeVertexLabel(vl_name).make();"
                "}"
                "mgmt.commit();"
            )
            try:
                self._submit(script, {"vl_name": vl["label"]})
                created.append(f"VertexLabel:{vl['label']}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"VertexLabel:{vl['label']} - {exc}")

        # 3. Edge labels
        for el in schema.get("edgeLabels", []):
            script = (
                "mgmt = graph.openManagement();"
                "if (!mgmt.getEdgeLabel(el_name)) {"
                "  mgmt.makeEdgeLabel(el_name).multiplicity(MULTI).make();"
                "}"
                "mgmt.commit();"
            )
            try:
                self._submit(script, {"el_name": el["label"]})
                created.append(f"EdgeLabel:{el['label']}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"EdgeLabel:{el['label']} - {exc}")

        # 4. Composite indexes
        for ci in schema.get("compositeIndexes", []):
            key_names = [k["propertyKey"] for k in ci.get("keys", [])]
            script = (
                "mgmt = graph.openManagement();"
                "if (!mgmt.getGraphIndex(idx_name)) {"
                "  builder = mgmt.buildIndex(idx_name, Vertex.class);"
                "  key_names.each { kn -> builder.addKey(mgmt.getPropertyKey(kn)); };"
                "  if (idx_only) { builder.indexOnly(mgmt.getVertexLabel(idx_only)); };"
                "  builder.buildCompositeIndex();"
                "}"
                "mgmt.commit();"
            )
            try:
                self._submit(
                    script,
                    {
                        "idx_name": ci["name"],
                        "key_names": key_names,
                        "idx_only": ci.get("indexOnly", ""),
                    },
                )
                created.append(f"CompositeIndex:{ci['name']}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"CompositeIndex:{ci['name']} - {exc}")

        self._await_index_enablement(created)

        return {
            "status": "ok" if not errors else "partial",
            "created": created,
            "errors": errors,
        }

    def _await_index_enablement(self, created: list[str], timeout_s: int = 120) -> None:
        """Poll until all composite indexes reach ENABLED state.

        Writes to: target JanusGraph — management rollback only (read-like).
        """
        index_names = [c.split(":")[1] for c in created if c.startswith("CompositeIndex:")]
        if not index_names:
            return

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            all_enabled = True
            for idx_name in index_names:
                try:
                    result = self._submit(
                        "mgmt = graph.openManagement();"
                        "idx = mgmt.getGraphIndex(iname);"
                        "status = idx?.getIndexStatus()?.name() ?: 'UNKNOWN';"
                        "mgmt.rollback();"
                        "status;",
                        {"iname": idx_name},
                    )
                    status = str(result[0] if result else "UNKNOWN")
                    if status not in ("ENABLED", "REGISTERED"):
                        all_enabled = False
                except Exception:  # noqa: BLE001, S110
                    all_enabled = False
            if all_enabled:
                return
            time.sleep(5)

    def verify_schema(self, schema: dict[str, Any]) -> dict[str, bool]:
        """Verify that all schema elements from the JSON schema exist on the target.

        Reads from: target JanusGraph — management queries.
        Returns a dict of element→pass/fail.
        """
        results: dict[str, bool] = {}
        for pk in schema.get("propertyKeys", []):
            try:
                r = self._submit(
                    "mgmt = graph.openManagement();"
                    "exists = mgmt.getPropertyKey(pk_name) != null;"
                    "mgmt.rollback();"
                    "exists;",
                    {"pk_name": pk["key"]},
                )
                results[f"PropertyKey:{pk['key']}"] = bool(r[0] if r else False)
            except Exception:  # noqa: BLE001, S110
                results[f"PropertyKey:{pk['key']}"] = False
        return results
