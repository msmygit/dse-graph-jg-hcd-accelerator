// Canned output of schema.describe() for the "food" demo graph (static test data).
//
// Purpose (AGENTS.md §5.4): exercise at least one instance of every rule category
// QP, SC, IX, CF, OL, SE, TP, HC when fed through the Assessor with mocked I/O.
// The trailing "marker" section stands in for application scripts / graph config
// text that the assessor substring-matches (the real cluster returns this text via
// system.graph(name).describe()). TP and HC findings are emitted without markers.

schema.vertexLabel('person').ifNotExists()
  .partitionBy('person_id', Uuid)
  .property('name', Text)
  .property('nickname', setOf(Text))
  .property('badge', mapOf(Text, Date))
  .property('location', Point)
  .property('address', frozen(typeOf('address_type')))
  .property('coords', tupleOf(Double, Double))
  .property('meta_source', Text)
  .create()

schema.vertexLabel('recipe').ifNotExists()
  .partitionBy('recipe_id', Uuid)
  .property('name', Text)
  .property('instructions', Text)
  .create()

schema.edgeLabel('knows').ifNotExists()
  .from('person').to('person').property('since', Date).create()

schema.vertexLabel('legacy_person').fromExistingTable('legacy_person_table').create()

schema.vertexLabel('person').materializedView('person_by_name').ifNotExists()
  .partitionBy('name').clusterBy('person_id', Asc).create()

schema.edgeLabel('knows').from('person').to('person')
  .materializedView('knows_by_since').ifNotExists().inverse()
  .partitionBy(OUT, 'person_id').clusterBy('since', Asc).clusterBy(IN, 'person_id', Asc).create()

schema.vertexLabel('person').secondaryIndex('person_by_nickname').ifNotExists().by('nickname').create()

schema.vertexLabel('recipe').searchIndex().ifNotExists()
  .by('instructions').asText().by('name').asString().create()

// ---- marker section (configuration / application script text) ----
// schema_mode = Development
// allow_scan = true
// restrict_lambda = true
// replication_config = {'class': 'NetworkTopologyStrategy', 'dc1': 3}
// g.with('allow-filtering').V().hasLabel('person')
// label-warning ignore-unindexed consistency=LOCAL_QUORUM
// has('instructions', Search.prefix('veg')) Search.fuzzy Search.regex Search.phrase
// has('location', Geo.inside(Geo.point(1, 2), 5, Unit.KM))
// DseGraph.traversal(session) g.V([1, 2, 3]) graph.tx()
// SparkGraphComputer GraphFrames
// rlac row_level_security rls_policy
