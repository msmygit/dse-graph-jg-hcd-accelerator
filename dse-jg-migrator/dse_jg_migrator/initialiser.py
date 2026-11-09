"""JanusGraph / HCD target schema initialiser.

Applies the translated JanusGraph JSON schema to the target cluster and verifies
that all schema elements were created successfully.
"""

from __future__ import annotations

import json
import time
from typing import Any

DEFAULT_INDEX_TIMEOUT_S = 30 * 60  # Appendix E.8: default wait for index enablement is 30 minutes

# Every script ends in a returned value: a script whose last statement is ``mgmt.commit();`` fails on a live
# JanusGraph server with "enclosing transaction is closed".
_SCHEMA_SCRIPT = """
def m = graph.openManagement()
def created = []
try {
  pks.each { pk ->
    if (m.getPropertyKey(pk.key) == null) {
      m.makePropertyKey(pk.key).dataType(Class.forName(pk.cls)).cardinality(Cardinality.valueOf(pk.card)).make()
      created << ('PropertyKey:' + pk.key)
    }
  }
  vls.each { vl ->
    if (m.getVertexLabel(vl) == null) { m.makeVertexLabel(vl).make(); created << ('VertexLabel:' + vl) }
  }
  els.each { el ->
    if (m.getEdgeLabel(el.label) == null) {
      m.makeEdgeLabel(el.label).multiplicity(Multiplicity.valueOf(el.mult)).make()
      created << ('EdgeLabel:' + el.label)
    }
  }
  idxs.each { ix ->
    if (m.getGraphIndex(ix.name) == null) {
      def b = m.buildIndex(ix.name, ix.edge ? Edge.class : Vertex.class)
      ix.keys.each { k ->
        if (k.mapping) { b.addKey(m.getPropertyKey(k.name), Mapping.valueOf(k.mapping).asParameter()) }
        else { b.addKey(m.getPropertyKey(k.name)) }
      }
      if (ix.only) { b.indexOnly(ix.edge ? m.getEdgeLabel(ix.only) : m.getVertexLabel(ix.only)) }
      if (ix.unique) { b.unique() }
      if (ix.backend) { b.buildMixedIndex(ix.backend) } else { b.buildCompositeIndex() }
      created << ('Index:' + ix.name)
    }
  }
  vcis.each { v ->
    def label = m.getEdgeLabel(v.label)
    if (m.getRelationIndex(label, v.name) == null) {
      m.buildEdgeIndex(label, v.name, Direction.valueOf(v.dir), Order.valueOf(v.order),
                       v.keys.collect { m.getPropertyKey(it) } as PropertyKey[])
      created << ('EdgeIndex:' + v.name)
    }
  }
  m.commit()
  return created
} catch (Exception e) {
  m.rollback()
  throw e
}
"""

# Status is per key; calling getIndexStatus() with no argument fails.
_INDEX_STATUS_SCRIPT = (
    "def m = graph.openManagement(); def i = m.getGraphIndex(iname); "
    "def s = i == null ? [] : i.getFieldKeys().collect { i.getIndexStatus(it).name() }; m.rollback(); s"
)

# An index over a property key committed in an EARLIER transaction stays INSTALLED (live finding: a target shared by
# several graphs, or a re-run adding an index). It is promoted along the documented lifecycle
# (janusgraph docs: schema/index-management): REGISTER_INDEX, then REINDEX, which enables the index when it finishes.
# Each action is its own short request — the server's evaluationTimeout is 30 s, so the reindex job is started, not
# awaited; the status poll waits for ENABLED.
_REGISTER_SCRIPT = (
    "def m = graph.openManagement(); m.updateIndex(m.getGraphIndex(iname), SchemaAction.REGISTER_INDEX); "
    "m.commit(); 'registered'"
)
_REINDEX_SCRIPT = (
    "def m = graph.openManagement(); m.updateIndex(m.getGraphIndex(iname), SchemaAction.REINDEX); "
    "m.commit(); 'reindexing'"
)

_OPEN_INSTANCES_SCRIPT = "def m = graph.openManagement(); def r = m.getOpenInstances().toList(); m.rollback(); r"
_OPEN_TX_SCRIPT = "graph.getOpenTransactions().size()"

_VERIFY_SCRIPT = """
def m = graph.openManagement()
def found = []
pks.each { if (m.getPropertyKey(it) != null) { found << ('PropertyKey:' + it) } }
vls.each { if (m.getVertexLabel(it) != null) { found << ('VertexLabel:' + it) } }
els.each { if (m.getEdgeLabel(it) != null) { found << ('EdgeLabel:' + it) } }
idxs.each { if (m.getGraphIndex(it) != null) { found << ('Index:' + it) } }
m.rollback()
found
""".strip()


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
        index_timeout_s: int = DEFAULT_INDEX_TIMEOUT_S,
    ) -> None:
        """
        Args:
            target_gremlin_url: WebSocket URL of the target JanusGraph Gremlin server.
            schema_json_path: Path to the translated JanusGraph JSON schema file.
            mode: 'gremlin-script' (default) or 'server-restart'.
            username: Optional Gremlin server username.
            password: Optional Gremlin server password.
            index_timeout_s: Max seconds to wait for composite indexes to be enabled
                (Appendix E.8; default 30 minutes).
        """
        self.index_timeout_s = index_timeout_s
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
        """Create the whole schema in ONE management transaction.

        Writes to: target JanusGraph — a single management transaction (rolled back on any error).

        Property keys and the indexes over them must be created in the same transaction: an index created in
        a later transaction than its keys stays INSTALLED and can never be registered or enabled (observed on a
        live JanusGraph 1.x). The script is idempotent — existing elements are skipped.
        """
        bindings = self.build_bindings(schema)
        try:
            created = [str(c) for c in self._submit(_SCHEMA_SCRIPT, bindings)]
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "partial",
                "created": [],
                "errors": [f"Schema creation on {self.target_gremlin_url} failed (rolled back): {exc}"],
            }

        errors: list[str] = []
        # Every schema index, not only the ones created now: a re-run must also heal an index left INSTALLED.
        indexes = [f"Index:{ix['name']}" for ix in bindings["idxs"]]
        pending = self._await_index_enablement(indexes, self.index_timeout_s)
        hint = (self._stale_instance_hint() + self._open_transaction_hint()) if pending else ""
        errors.extend(f"Index:{name} not ENABLED after {self.index_timeout_s}s{hint}" for name in pending)
        return {"status": "ok" if not errors else "partial", "created": created, "errors": errors}

    @staticmethod
    def build_bindings(schema: dict[str, Any]) -> dict[str, Any]:
        """Convert the translated JSON schema into the bindings consumed by ``_SCHEMA_SCRIPT``."""

        def _keys(index: dict[str, Any]) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for k in index.get("keys", []):
                mapping = next(
                    (str(p["value"]).rsplit(".", 1)[-1] for p in k.get("parameters", []) if p.get("key") == "mapping"),
                    "",
                )
                out.append({"name": k["propertyKey"], "mapping": mapping})
            return out

        def _index(index: dict[str, Any], backend: str) -> dict[str, Any]:
            return {
                "name": index["name"],
                "edge": str(index.get("typeClass", "")).endswith(".Edge"),
                "only": index.get("indexOnly", "") or "",
                "unique": bool(index.get("unique", False)),
                "backend": backend,
                "keys": _keys(index),
            }

        idxs = [_index(i, "") for i in schema.get("compositeIndexes", [])]
        idxs += [_index(i, i.get("indexBackend", "search")) for i in schema.get("mixedIndexes", [])]
        return {
            "pks": [
                {
                    "key": pk["key"],
                    "cls": "[B" if pk["className"] == "byte[]" else pk["className"],
                    "card": pk.get("cardinality", "SINGLE"),
                }
                for pk in schema.get("propertyKeys", [])
            ],
            "vls": [vl["label"] for vl in schema.get("vertexLabels", [])],
            "els": [
                {"label": el["label"], "mult": el.get("multiplicity", "MULTI")} for el in schema.get("edgeLabels", [])
            ],
            "idxs": idxs,
            "vcis": [
                {
                    "name": v["name"],
                    "label": v["indexedEdgeLabel"],
                    "dir": v.get("direction", "BOTH"),
                    "order": v.get("order", "asc"),
                    "keys": list(v.get("propertyKeys", [])),
                }
                for v in schema.get("vertexCentricEdgeIndexes", [])
            ],
        }

    def _await_index_enablement(self, created: list[str], timeout_s: int = DEFAULT_INDEX_TIMEOUT_S) -> list[str]:
        """Poll until every graph index reaches ENABLED (Appendix E.8), promoting INSTALLED indexes.

        Reads from / writes to: target JanusGraph — status queries are rolled back; an index with an INSTALLED key
        gets REGISTER_INDEX, and once every key is REGISTERED or ENABLED, one REINDEX (which enables it).
        Returns the names of indexes that were still not ENABLED when the timeout expired.
        """
        pending = [c.split(":", 1)[1] for c in created if c.startswith("Index:")]
        if not pending:
            return []

        registered: set[str] = set()
        reindexed: set[str] = set()
        deadline = time.time() + timeout_s
        while pending and time.time() < deadline:
            still: list[str] = []
            for idx_name in pending:
                try:
                    statuses = {str(s) for s in self._submit(_INDEX_STATUS_SCRIPT, {"iname": idx_name})}
                    if statuses == {"ENABLED"}:
                        continue
                    still.append(idx_name)
                    if "INSTALLED" in statuses and idx_name not in registered:
                        self._submit(_REGISTER_SCRIPT, {"iname": idx_name})
                        registered.add(idx_name)
                    elif (
                        "REGISTERED" in statuses and statuses <= {"REGISTERED", "ENABLED"} and idx_name not in reindexed
                    ):
                        self._submit(_REINDEX_SCRIPT, {"iname": idx_name})
                        reindexed.add(idx_name)
                except Exception:  # noqa: BLE001
                    if idx_name not in still:
                        still.append(idx_name)  # transient failure: retry until the deadline
            pending = still
            if pending:
                time.sleep(5)
        return pending

    def _stale_instance_hint(self) -> str:
        """Explain a stuck index when other JanusGraph instances are registered (diagnostic only).

        Reads from: target JanusGraph — management query, rolled back. Index registration needs an acknowledgement
        from every registered instance, so one that was not shut down cleanly blocks it forever (found live after a
        crash). It is never force-closed here: removing a still-running instance corrupts data (JanusGraph docs,
        operations/recovery).
        """
        try:
            instances = [str(i) for i in self._submit(_OPEN_INSTANCES_SCRIPT)]
        except Exception:  # noqa: BLE001
            return ""
        others = [i for i in instances if not i.endswith("(current)")]
        if not others:
            return ""
        return (
            f" — {len(others)} other JanusGraph instance(s) registered ({', '.join(others)}). An instance that was not"
            " shut down cleanly blocks index registration; if it is no longer running, remove it with"
            " mgmt.forceCloseInstance('<id>'); mgmt.commit() (JanusGraph docs: operations/recovery) and re-run"
        )

    def _open_transaction_hint(self) -> str:
        """Explain a stuck index when the server holds open transactions (diagnostic only).

        Reads from: target JanusGraph. Registration is acknowledged only after every transaction that predates it
        has closed (JanusGraph docs: index-reindexing; the server logs "Stale transaction alert"). Found live: one
        leaked transaction kept a key INSTALLED. They are never rolled back here — they may belong to other clients.
        """
        try:
            result = self._submit(_OPEN_TX_SCRIPT)
            open_tx = int(result[0]) if result else 0
        except Exception:  # noqa: BLE001
            return ""
        if open_tx == 0:
            return ""
        return (
            f" — {open_tx} open transaction(s) on the JanusGraph server predate the index and block its registration"
            " (server log: 'Stale transaction alert'). Close the clients holding them, or restart JanusGraph (then"
            " force-close the old instance id, see above), and re-run"
        )

    def verify_schema(self, schema: dict[str, Any]) -> dict[str, bool]:
        """Verify that every property key, label and index of the schema exists on the target.

        Reads from: target JanusGraph — one management query, always rolled back.
        Returns a dict of ``Kind:name`` -> present.
        """
        b = self.build_bindings(schema)
        wanted = (
            [f"PropertyKey:{pk['key']}" for pk in b["pks"]]
            + [f"VertexLabel:{vl}" for vl in b["vls"]]
            + [f"EdgeLabel:{el['label']}" for el in b["els"]]
            + [f"Index:{ix['name']}" for ix in b["idxs"]]
        )
        try:
            found = self._submit(
                _VERIFY_SCRIPT,
                {
                    "pks": [pk["key"] for pk in b["pks"]],
                    "vls": b["vls"],
                    "els": [el["label"] for el in b["els"]],
                    "idxs": [ix["name"] for ix in b["idxs"]],
                },
            )
        except Exception:  # noqa: BLE001
            return {name: False for name in wanted}
        present = {str(x) for x in (found[0] if found and isinstance(found[0], list) else found or [])}
        return {name: name in present for name in wanted}
